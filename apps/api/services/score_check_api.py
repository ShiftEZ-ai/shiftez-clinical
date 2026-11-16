"""Frontend views and commands over the shared score-check import workflow."""
from sqlalchemy import select

from apps.api import models as m
from apps.api.auth import require_role
from apps.api.errors import require
from packages.contracts import schemas as s
from . import score_check
from .common import expected, scoped


def view_import(db, actor, batch):
    value = score_check.view(db, actor, batch)['batch']
    sources = {row.id: row for row in score_check._sources(db, batch)}
    decisions, unclear_by_source = score_check._decisions(db, batch), {}
    for gap in value['gaps']:
        if gap['entity_kind'] == 'cell' and gap['id'] not in decisions:
            source_id = gap.get('source_id')
            unclear_by_source[source_id] = unclear_by_source.get(source_id, 0) + 1
    files = []
    for source in value['files']:
        row = sources[source['id']]
        unclear = unclear_by_source.get(row.id, 0)
        status = ('reading' if source['status'] in {'uploaded', 'reading'} else
                  'failed' if source['status'] == 'failed' else 'needs_check' if unclear else 'ready')
        files.append({'id': row.id, 'file_name': row.file_name,
            'format': 'csv' if row.format == 'tsv' else row.format if row.format in {'csv', 'xlsx', 'pdf'} else 'image',
            'group': row.group, 'period_start': row.starts_on, 'period_end': row.ends_on,
            'period_inferred': row.period_inferred,
            'people_count': (row.people_count_override if row.people_count_override is not None else
                             (source['result'] or {}).get('people_count', 0)),
            'status': status, 'unclear_cells': unclear, 'error': row.error_code})
    groups = list(db.scalars(select(m.ImportSource.group).where(
        m.ImportSource.department_id == actor.department_id, m.ImportSource.group != '')
        .distinct().order_by(m.ImportSource.group)))
    time = (None if batch.manual_time_hours is None or batch.manual_time_people is None else
            {'work_hours': batch.manual_time_hours, 'people': batch.manual_time_people})
    return s.ScoreImport(id=batch.id, revision=batch.revision, saved_workspace_id=batch.workspace_id,
        sources=files, time=time, profile_id=batch.profile_id, groups=groups).model_dump(mode='json')


def _target_labels(db, gaps):
    labels = {}
    for kind, cls, field in (('person', m.Person, m.Person.display_name),
                             ('duty_code', m.AssignmentType, m.AssignmentType.name),
                             ('clinic', m.Service, m.Service.name)):
        ids = {target for gap in gaps if gap['entity_kind'] == kind for target in
               (gap.get('suggested_target_id'), (gap.get('decision') or {}).get('target_id')) if target}
        if ids:
            labels.update({(kind, key): label for key, label in db.execute(select(cls.id, field).where(cls.id.in_(ids)))})
    return labels


def view_check(db, actor, batch, settings=None):
    if settings is not None:
        require_role(actor, 'coordinator')
        score_check.check_current(db, actor, batch, settings)
    value = score_check.view(db, actor, batch)['batch']
    labels = _target_labels(db, value['gaps'])
    source_names = {row.id: row.file_name for row in score_check._sources(db, batch)}
    items = []
    for gap in value['gaps']:
        decision = gap.get('decision')
        if not decision and score_check.automatic_match(gap, batch.confidence_threshold):
            continue
        target = gap.get('suggested_target_id')
        suggested = {'id': target, 'label': labels.get((gap['entity_kind'], target), gap['as_written'])} if target else None
        decided = None
        if decision:
            action = decision['action']
            kind = ('skip' if gap['entity_kind'] == 'cell' else 'leave_out') if action == 'exclude' else (
                'confirm' if action == 'resolve' else action)
            label = {'acknowledge': 'Acknowledged', 'add': 'Added', 'match': 'Matched',
                     'leave_out': 'Left out', 'confirm': 'Confirmed', 'skip': 'Skipped'}[kind]
            decided = {'kind': kind, 'label': label}
        detail = gap['reason']
        if gap.get('source_id') in source_names:
            context = [source_names[gap['source_id']], *(gap['cell_refs'][:1])]
            detail = ' · '.join([*context, detail])
        items.append({'id': gap['id'],
            'section': {'missing': 'missing', 'unknown': 'unknown', 'uncertain': 'unsure', 'unreadable': 'cell'}[gap['kind']],
            'kind': 'session' if gap['entity_kind'] == 'coverage' else gap['entity_kind'],
            'as_written': gap['as_written'], 'detail': detail, 'suggested': suggested,
            'confidence': gap.get('confidence'), 'decision': decided,
            'reading_context': ({'file_ref': gap['file_ref'], 'cell_ref': gap['cell_refs'][0]}
                                if gap['entity_kind'] == 'cell' else None)})
    return s.ImportCheck(import_id=batch.id, revision=batch.revision, items=items,
        matched=value['confident_matches'], threshold=batch.confidence_threshold).model_dump(mode='json')


def view_matches(db, actor, batch, *, offset=0, limit=50):
    require_role(actor, 'coordinator')
    require(batch.department_id == actor.department_id, 'NOT_FOUND', 'Record not found', 404)
    require(0 <= offset <= 1_000_000 and 1 <= limit <= 100, 'MATCH_PAGE', 'Choose a valid match page')
    gaps, _, _ = score_check._gap_state(db, batch)
    matches = [gap for gap in gaps if not gap.get('decision')
               and score_check.automatic_match(gap, batch.confidence_threshold)]
    page = matches[offset:offset + limit]
    labels = _target_labels(db, page)
    source_ids = {gap['source_id'] for gap in page}
    # A source removed after this page was captured still supplies its retained filename.
    sources = {row.id: row for row in db.execute(select(m.ImportSource.id, m.ImportSource.file_name).where(
        m.ImportSource.department_id == actor.department_id, m.ImportSource.batch_id == batch.id,
        m.ImportSource.id.in_(source_ids)))}
    return s.ImportMatches(import_id=batch.id, revision=batch.revision, total=len(matches),
        offset=offset, limit=limit, has_more=offset + len(page) < len(matches), items=[s.ImportMatch(
            id=gap['id'], kind=gap['entity_kind'], as_written=gap['as_written'],
            matched=s.CheckSuggested(id=gap['suggested_target_id'],
                label=labels.get((gap['entity_kind'], gap['suggested_target_id']), 'Unavailable match')),
            confidence=gap['confidence'], source_id=gap['source_id'],
            file_name=sources[gap['source_id']].file_name, file_ref=gap['file_ref'], cell_refs=gap['cell_refs'],
            location_id=gap.get('suggested_location_id')) for gap in page]).model_dump(mode='json')


def _editable(db, actor, import_id, revision, *, reading=False, discarded=False):
    batch = scoped(db, m.ScoreCheckImportBatch, import_id, actor)
    expected(batch, revision)
    allowed = {'discarded'} if discarded else {'check', 'failed'} | ({'uploaded', 'reading'} if reading else set())
    require(batch.status in allowed, 'IMPORT_STATE', 'Use an unfinished import in its current state', 409)
    return batch


def _legacy(command, action, payload):
    return command.model_copy(update={'kind': 'score_check.' + action, 'payload': payload})


def _updated(db, actor, command, settings, payload):
    score_check.update(db, actor, _legacy(command, 'update', payload), settings)
    return view_import(db, actor, db.get(m.ScoreCheckImportBatch, payload['batch_id']))


def _restore_reading(db, source):
    if source.status != 'reading' or source.extraction:
        return False
    job = db.get(m.ScoreCheckExtractionJob, source.extraction_job_id) if source.extraction_job_id else None
    if job is not None and job.state in {'queued', 'running'}:
        return False
    source.status = 'failed'
    source.error_code = (job.error_code if job else None) or 'SCORE_READING_INTERRUPTED'
    return True


def _decide(db, actor, command, settings):
    value = s.ScoreImportDecide.model_validate(command.payload)
    batch = _editable(db, actor, value.import_id, command.expected_revision)
    score_check.check_current(db, actor, batch, settings)
    gap = next((item for item in batch.gaps if item['id'] == value.item_id), None)
    require(gap is not None, 'NOT_FOUND', 'Check item not found', 404)
    if value.decision is None:
        decision = score_check._decisions(db, batch).get(value.item_id)
        require(decision is not None, 'NOT_FOUND', 'Decision not found', 404)
        score_check.undo(db, actor, _legacy(command, 'undo', {
            'batch_id': batch.id, 'decision_id': decision.id, 'reason': value.reason}), settings)
    else:
        decision = value.decision
        action = {'leave_out': 'exclude', 'skip': 'exclude'}.get(decision.kind, decision.kind)
        replacement = decision.value
        if decision.kind == 'confirm' and gap['entity_kind'] == 'cell':
            require(decision.reading is not None, 'IMPORT_READING', 'Confirm the complete reading of this cell')
            require(decision.target_id is None and decision.value is None, 'IMPORT_READING',
                    'Use the complete reading to confirm this cell')
            action, replacement = 'resolve', decision.reading.model_dump(mode='json')
        elif decision.kind == 'confirm' and decision.target_id not in (None, gap.get('suggested_target_id')):
            action = 'match'
        require(decision.kind != 'skip' or gap['entity_kind'] == 'cell', 'GAP_ACTION', 'Skip applies to an unreadable cell')
        require(decision.reading is None or action == 'resolve', 'GAP_ACTION', 'A reading applies only to an unreadable cell')
        if gap['entity_kind'] == 'clinic' and (action in {'add', 'match'} or (
                action == 'confirm' and not gap.get('suggested_location_id'))):
            require(decision.location_id is not None, 'IMPORT_LOCATION', 'Choose the location for this clinic')
        if action == 'add':
            catalogue = {'person': 'people', 'clinic': 'services'}.get(gap['entity_kind'])
            require(catalogue is not None, 'GAP_ACTION',
                    'Match an existing duty type or leave this code out; duty types are created in Configuration')
            # The same narrow contract as the gap decision: extra fields are refused, never dropped silently.
            narrow = s.ScoreCheckGapAddPerson if catalogue == 'people' else s.ScoreCheckGapAddService
            replacement = narrow.model_validate(replacement).model_dump(mode='json')
        score_check.decide(db, actor, _legacy(command, 'decide', {
            'batch_id': batch.id, 'gap_id': value.item_id, 'action': action,
            'target_id': decision.target_id if action != 'resolve' else None,
            'location_id': decision.location_id, 'replacement': replacement, 'reason': value.reason}), settings)
    return view_check(db, actor, batch)


def command(db, actor, command, settings):
    score_check.require_enabled(db, actor.department_id)
    require_role(actor, 'coordinator')
    action = command.kind.removeprefix('score_import.')
    if action == 'upload':
        value = s.InternalScoreImportUploadCommand.model_validate(command.payload)
        require(command.expected_revision is None, 'STALE_REVISION', 'Start an import without an existing revision', 409)
        if value.profile_id and value.profile_revision is not None:
            expected(scoped(db, m.AiProviderProfile, value.profile_id, actor), value.profile_revision)
        payload = value.model_dump(mode='json', exclude={'profile_revision'})
        payload['use_ai'] = value.profile_id is not None
        result = score_check.upload(db, actor, _legacy(command, 'upload', payload), settings)
        return view_import(db, actor, db.get(m.ScoreCheckImportBatch, result['batch']['id']))
    if action == 'add_files':
        value = s.InternalScoreImportAddFilesCommand.model_validate(command.payload)
        score_check.add_files(db, actor, command, settings)
        return view_import(db, actor, db.get(m.ScoreCheckImportBatch, value.import_id))
    if action == 'decide':
        return _decide(db, actor, command, settings)
    if action == 'update_source':
        value = s.ScoreImportUpdateSource.model_validate(command.payload)
        source = scoped(db, m.ImportSource, value.source_id, actor)
        require(source.batch_id == value.import_id and source.removed_at is None, 'NOT_FOUND', 'File not found', 404)
        fields = value.model_dump(mode='json', exclude_unset=True, exclude={'import_id'})
        if 'period_start' in fields or 'period_end' in fields:
            fields.setdefault('period_start', source.starts_on)
            fields.setdefault('period_end', source.ends_on)
        fields = {{'period_start': 'starts_on', 'period_end': 'ends_on', 'people_count': 'people_count_override'}.get(key, key): item
                  for key, item in fields.items()}
        payload = {'batch_id': value.import_id, 'files': [fields]}
        return _updated(db, actor, command, settings, payload)
    if action == 'time':
        value = s.ScoreImportSetTime.model_validate(command.payload)
        return _updated(db, actor, command, settings, {'batch_id': value.import_id,
            'manual_time_hours': value.time.work_hours if value.time else None,
            'manual_time_people': value.time.people if value.time else None})
    if action in {'remove_source', 'restore_source'}:
        value = s.ScoreImportSourceAction.model_validate(command.payload)
        batch = _editable(db, actor, value.import_id, command.expected_revision, reading=True)
        source = scoped(db, m.ImportSource, value.source_id, actor)
        require(source.batch_id == batch.id, 'NOT_FOUND', 'File not found', 404)
        require((source.removed_at is None) == (action == 'remove_source'), 'IMPORT_STATE', 'The file changed; reload this import', 409)
        source.removed_at = m.now() if action == 'remove_source' else None
        if action == 'restore_source':
            _restore_reading(db, source)
        source.revision += 1
    elif action == 'profile':
        value = s.ScoreImportProfile.model_validate(command.payload)
        batch = _editable(db, actor, value.import_id, command.expected_revision, reading=True)
        profile = scoped(db, m.AiProviderProfile, value.profile_id, actor) if value.profile_id else None
        if profile:
            require(value.privacy_acknowledged, 'SCORE_AI_CONSENT', 'Acknowledge where the files will be sent', 409)
            expected(profile, value.profile_revision)
            require(profile.archived_at is None and profile.enabled and profile.key_ciphertext,
                    'SCORE_PROVIDER_DISABLED', 'Choose an enabled AI provider with its key set', 409)
            require(db.get(m.Department, actor.department_id).ai_enabled,
                    'SCORE_AI_DISABLED', 'AI reading is off for this department', 409)
        batch.use_ai, batch.profile_id = profile is not None, value.profile_id
        batch.profile_revision = profile.revision if profile else None
        batch.provider_snapshot = ({'name': profile.name, 'base_url': profile.base_url, 'model': profile.model}
                                   if profile else None)
        batch.privacy_acknowledged = profile is not None
        batch.privacy_acknowledged_at = m.now() if profile else None
        batch.privacy_acknowledged_by = actor.identity_id if profile else None
    elif action in {'discard', 'restore'}:
        value = s.ScoreImportAction.model_validate(command.payload)
        batch = _editable(db, actor, value.import_id, command.expected_revision,
                          reading=True, discarded=action == 'restore')
        batch.status = 'discarded' if action == 'discard' else 'uploaded'
        if action == 'restore':
            for source in score_check._sources(db, batch):
                if _restore_reading(db, source):
                    source.revision += 1
    elif action in {'check', 'save'}:
        schema = s.ScoreImportSave if action == 'save' else s.ScoreImportAction
        value = schema.model_validate(command.payload)
        payload = {'batch_id': value.import_id, 'reason': value.reason}
        if action == 'save':
            payload['name'] = value.name
        result = score_check.command(db, actor, _legacy(command, action, payload), settings)
        if action == 'save':
            return {'workspace_id': result['workspace']['id']}
        return view_check(db, actor, db.get(m.ScoreCheckImportBatch, value.import_id))
    else:
        require(False, 'UNKNOWN_COMMAND', 'Unsupported import command')
    batch.revision += 1
    score_check.refresh(db, batch)
    score_check._check_coverage(db, actor, batch, settings)
    return view_import(db, actor, batch)
