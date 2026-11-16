"""Upload → check → ordinary imported draft; extraction is evidence, never authority."""
import base64
import binascii
import copy
import csv
import hashlib
import io
from dataclasses import replace
from datetime import date, timedelta
from pathlib import PurePath

from sqlalchemy import func, select

from apps.api import models as m
from apps.api.auth import require_role
from apps.api.errors import Problem, require
from apps.api.serialization import record
from apps.api.snapshots import snapshot_hash, workspace_shifts
from packages.domain.time import aware
from packages.contracts import schemas as s
from packages.domain.time import local_stamp
from . import catalogue, imports, planning
from .common import expected, scoped
from .score_check_parser import prepare_score_check_file, parse_score_check_file, ScoreCheckParseError


def require_enabled(db, department_id):
    require(db.get(m.Department, department_id).score_check_enabled,
            'SCORE_CHECK_DISABLED', 'Score checking is off for this department', 404)


def _planner(actor):
    return replace(actor, roles=actor.roles | {'coordinator'}) if actor.permits('admin') else actor


def _sources(db, batch):
    return list(db.scalars(select(m.ImportSource).where(m.ImportSource.batch_id == batch.id, m.ImportSource.removed_at.is_(None))
                           .order_by(m.ImportSource.created_at, m.ImportSource.id)))


def extraction_context(db, actor, source):
    batch = scoped(db, m.ScoreCheckImportBatch, source.batch_id, actor)
    people = list(db.scalars(select(m.Person).where(m.Person.active.is_(True), m.Person.id.in_(
        select(m.Membership.person_id).where(m.Membership.department_id == actor.department_id)))))
    return {'people': [{key: getattr(p, key) for key in ('id', 'display_name', 'short_code', 'rota_name', 'rota_aliases')} for p in people],
            'duty_types': [{'id': t.id, 'name': t.name} for t in db.scalars(select(m.AssignmentType).where(
                m.AssignmentType.department_id == actor.department_id))],
            'clinics': [{'id': r.id, 'name': r.name, 'kind': kind} for cls, kind in ((m.Service, 'service'), (m.Location, 'location'))
                        for r in db.scalars(select(cls).where(cls.department_id == actor.department_id))],
            'period_start': source.starts_on or (batch.starts_on if not batch.period_inferred else None),
            'period_end': source.ends_on or (batch.ends_on if not batch.period_inferred else None), 'group': source.group}


def validate_extraction(db, actor, source, value, inventory=None):
    """Validate every reference against this department and the actual uploaded file."""
    value = s.RotaExtraction.model_validate(value).model_dump(mode='json')
    context = extraction_context(db, actor, source)
    if inventory is None:
        if source.format in {'csv', 'tsv', 'xlsx'}:
            inventory = prepare_score_check_file(source.content, source.file_name, file_ref=source.file_ref)
        else:
            from .score_check_provider import ProviderError, prepare_file
            try:
                inventory = prepare_file(source.file_ref, source.file_name, source.content)
            except ProviderError as error:
                if error.code != 'SCORE_ISOLATION_UNAVAILABLE':
                    raise
                raise Problem(503, error.code, str(error)) from None
    refs = inventory.cell_refs
    require(inventory.sha256 == source.sha256, 'EXTRACTION_FILE', 'The uploaded file changed')
    require(len(value['files']) == 1 and value['files'][0]['file_ref'] == source.file_ref,
            'EXTRACTION_FILE', 'The extraction must describe exactly the uploaded file')
    require(not source.group or value['files'][0]['group'] == source.group,
            'EXTRACTION_GROUP', 'The extraction must retain the selected group')
    ids = {key: {r['id'] for r in context[key]} for key in ('people', 'duty_types', 'clinics')}
    ids['services'] = {r['id'] for r in context['clinics'] if r['kind'] == 'service'}
    ids['locations'] = {r['id'] for r in context['clinics'] if r['kind'] == 'location'}
    for kind, field, allowed in (('people', 'suggested_person_id', 'people'),
            ('duty_codes', 'suggested_type_id', 'duty_types'), ('clinics', 'suggested_service_id', 'services'),
            ('clinics', 'suggested_location_id', 'locations')):
        for item in value[kind]:
            require(not item.get(field) or item[field] in ids[allowed], 'EXTRACTION_ID',
                    'The extraction suggests an identifier outside the candidate list')
    for kind in ('people', 'duty_codes', 'clinics', 'assignments', 'unparsed', 'warnings'):
        for item in value[kind]:
            require(item['file_ref'] == source.file_ref, 'EXTRACTION_FILE', 'Unknown extraction file reference')
            cells = item.get('cell_refs', [item['cell_ref']] if 'cell_ref' in item else [])
            require(all(cell in refs for cell in cells), 'EXTRACTION_CELL', 'Unknown extraction cell reference')
    for kind, label in (('people', 'as_written'), ('duty_codes', 'code'), ('clinics', 'as_written')):
        labels = [r[label] for r in value[kind]]
        require(len(labels) == len(set(labels)), 'EXTRACTION_DUPLICATE', 'Declare each person, code and clinic once per file')
    declared = {kind: {r[field] for r in value[kind]} for kind, field in
                (('people', 'as_written'), ('duty_codes', 'code'), ('clinics', 'as_written'))}
    for item in value['assignments']:
        require(item['person'] in declared['people'] and item['duty_code'] in declared['duty_codes']
                and (not item['clinic'] or item['clinic'] in declared['clinics']),
                'EXTRACTION_DECLARATION', 'Every assignment must name declared people, codes and clinics')
        day = date.fromisoformat(item['date'])
        require(day < date.max, 'EXTRACTION_PERIOD', 'An extracted date needs a representable exclusive end')
        require((not context['period_start'] or day >= context['period_start']) and
                (not context['period_end'] or day < context['period_end']),
                'EXTRACTION_PERIOD', 'An extracted date is outside the selected period')
    return value


def _corrected(extraction, assignment, *, replace_cell=True):
    candidate = copy.deepcopy(extraction)
    candidate['assignments'] = [a for a in candidate['assignments'] if not replace_cell or a['cell_ref'] != assignment['cell_ref']] + [assignment]
    for collection, label, key in (('people', 'as_written', 'person'), ('duty_codes', 'code', 'duty_code'), ('clinics', 'as_written', 'clinic')):
        text = assignment[key]
        if text and not any(r[label] == text for r in candidate[collection]):
            common = {'file_ref': assignment['file_ref'], 'cell_refs': [assignment['cell_ref']], 'confidence': 0,
                      'reason': 'Label entered while correcting an unreadable cell'}
            extra = ({'suggested_person_id': None} if collection == 'people' else
                     {'suggested_type_id': None, 'kind': 'other'} if collection == 'duty_codes' else
                     {'suggested_service_id': None, 'suggested_location_id': None})
            candidate[collection].append({label: text, **common, **extra})
    return candidate


def _effective_extraction(source, decisions):
    extraction = source.extraction or {}
    tabular = source.format in {'csv', 'tsv', 'xlsx'}
    if tabular and extraction.get('unparsed'):
        # An unreadable spreadsheet cell is omitted until its own correction; page references cover many neighbours.
        extraction = copy.deepcopy(extraction)
        unreadable = {r['cell_ref'] for r in extraction['unparsed']}
        extraction['assignments'] = [a for a in extraction['assignments'] if a['cell_ref'] not in unreadable]
    for decision in decisions.values():
        if decision.source_id == source.id and decision.action == 'resolve':
            extraction = _corrected(extraction, decision.replacement, replace_cell=tabular)
    excluded = {d.gap_id for d in decisions.values() if d.source_id == source.id and d.action == 'exclude'}
    if extraction and excluded:
        extraction = copy.deepcopy(extraction)
        extraction['assignments'] = [a for a in extraction['assignments'] if all(
            snapshot_hash([source.id, entity, a[field]]) not in excluded
            for entity, field in (('person', 'person'), ('duty_code', 'duty_code'), ('clinic', 'clinic')))]
    if extraction and source.starts_on and source.ends_on:
        outside = [a for a in extraction['assignments'] if not source.starts_on <= date.fromisoformat(a['date']) < source.ends_on]
        if outside:
            extraction = copy.deepcopy(extraction)
            extraction['assignments'] = [a for a in extraction['assignments'] if a not in outside]
            already = {r['cell_ref'] for r in extraction['unparsed']}
            extraction['unparsed'].extend({'file_ref': source.file_ref, 'cell_ref': a['cell_ref'],
                'value': a['date'], 'reason': 'This duty is outside the edited file period; correct the date or leave it out'}
                for a in outside if a['cell_ref'] not in already)
    return extraction


def _period_evidence(source, decisions):
    # Leaving an assignment out does not erase the period understood from the file.
    extraction = source.extraction or {}
    for decision in decisions.values():
        if decision.source_id == source.id and decision.action == 'resolve':
            extraction = _corrected(extraction, decision.replacement,
                                   replace_cell=source.format in {'csv', 'tsv', 'xlsx'})
    return extraction


def _unreadable_id(source, index, item):
    return snapshot_hash([source.id, 'unparsed', index, item['cell_ref'], item['value']])


def refresh(db, batch):
    """Rebuild the two-way gaps from extraction evidence; decisions remain separate audit records."""
    sources, gaps, present = _sources(db, batch), [], set()
    dates, ends, decisions = [], [], _decisions(db, batch)
    types = {r.id: r for r in db.scalars(select(m.AssignmentType).where(m.AssignmentType.department_id == batch.department_id))}
    for source in sources:
        extraction = _effective_extraction(source, decisions)
        if not extraction:
            continue
        file = extraction['files'][0]
        inference = _period_evidence(source, decisions)
        days, source_ends = [date.fromisoformat(a['date']) for a in inference.get('assignments', [])], []
        codes = {r['code']: r for r in inference.get('duty_codes', [])}
        for assignment in inference.get('assignments', []):
            code = codes[assignment['duty_code']]
            gap_id = snapshot_hash([source.id, 'duty_code', assignment['duty_code']])
            decision = decisions.get(gap_id)
            type_id = decision.target_id if decision and decision.action in {'match', 'confirm', 'add'} else code.get('suggested_type_id')
            typ = types.get(type_id)
            timing = typ.definition.get('default_timing') or {} if typ else {}
            start = assignment.get('start') or code.get('start') or timing.get('start_time')
            end = assignment.get('end') or code.get('end') or timing.get('end_time')
            day = date.fromisoformat(assignment['date'])
            require(day < date.max - timedelta(days=1), 'EXTRACTION_PERIOD', 'The duty needs a representable exclusive end')
            source_ends.append(day + timedelta(days=2 if start and end and end <= start else 1))
        if source.period_inferred and days:
            source.starts_on, source.ends_on = min(days), max(source_ends)
        if source.starts_on and source.ends_on:
            dates.append(source.starts_on)
            ends.append(source.ends_on)
        if not source.group:
            source.group = file.get('group', '')
        assigned_people = {a['person'] for a in extraction.get('assignments', [])}
        for collection, entity, label, target in (('people', 'person', 'as_written', 'suggested_person_id'),
                ('duty_codes', 'duty_code', 'code', 'suggested_type_id'),
                ('clinics', 'clinic', 'as_written', 'suggested_service_id')):
            for item in extraction.get(collection, []):
                suggested = item.get(target)
                gap_id = snapshot_hash([source.id, entity, item[label]])
                decision = decisions.get(gap_id)
                matched = decision.target_id if decision and decision.action in {'match', 'confirm', 'add'} else (
                    suggested if not decision and item['confidence'] >= batch.confidence_threshold else None)
                if entity == 'person' and matched and item[label] in assigned_people:
                    present.add(matched)
                gaps.append({'id': snapshot_hash([source.id, entity, item[label]]),
                    'kind': 'uncertain' if suggested else 'unknown', 'entity_kind': entity,
                    'source_id': source.id, 'file_ref': source.file_ref, 'cell_refs': item['cell_refs'],
                    'as_written': item[label], 'suggested_target_id': suggested,
                    'suggested_location_id': item.get('suggested_location_id'),
                    'confidence': item['confidence'], 'reason': item['reason']})
        if any(not a['clinic'] for a in extraction.get('assignments', [])):
            gaps.append({'id': snapshot_hash([source.id, 'clinic', '']), 'kind': 'unknown', 'entity_kind': 'clinic',
                'source_id': source.id, 'file_ref': source.file_ref, 'cell_refs': [a['cell_ref'] for a in extraction['assignments'] if not a['clinic']],
                'as_written': '', 'suggested_target_id': None, 'suggested_location_id': None,
                'confidence': 0, 'reason': 'Choose the clinic and location for duties without a clinic label'})
        for index, item in enumerate(extraction.get('unparsed', [])):
            gaps.append({'id': _unreadable_id(source, index, item), 'kind': 'unreadable',
                'entity_kind': 'cell', 'source_id': source.id, 'file_ref': source.file_ref,
                'cell_refs': [item['cell_ref']], 'as_written': item['value'], 'confidence': None,
                'reason': item['reason'], 'suggested_target_id': None})
    if dates and batch.period_inferred:
        batch.starts_on, batch.ends_on = min(dates), max(ends)
    elif batch.period_inferred:
        batch.starts_on, batch.ends_on = None, None
    if batch.starts_on and batch.ends_on:
        memberships = list(db.scalars(select(m.Membership).where(
            m.Membership.department_id == batch.department_id,
            m.Membership.starts_on < batch.ends_on,
            (m.Membership.ends_on.is_(None) | (m.Membership.ends_on > batch.starts_on)))))
        membership_context = {}
        for row in memberships:
            membership_context.setdefault(row.person_id, []).append([row.id, row.revision, row.category_id,
                row.starts_on.isoformat(), row.ends_on.isoformat() if row.ends_on else None])
        people = db.scalars(select(m.Person).where(m.Person.id.in_(membership_context), m.Person.active.is_(True)))
        for person in people:
            if person.id not in present:
                gaps.append({'id': snapshot_hash(['missing', person.id, batch.starts_on.isoformat(), batch.ends_on.isoformat(),
                    person.revision, sorted(membership_context[person.id])]), 'kind': 'missing', 'entity_kind': 'person',
                    'source_id': None, 'file_ref': None, 'cell_refs': [], 'as_written': person.display_name,
                    'confidence': None, 'reason': 'Active department staff member has no matched duty in these files',
                    'suggested_target_id': person.id})
    batch.gaps = gaps
    if batch.status not in {'saved', 'discarded'}:
        batch.status = ('reading' if any(r.status == 'reading' for r in sources) else
                        'failed' if any(r.status == 'failed' for r in sources) else 'check')
    db.flush()


def _decisions(db, batch):
    return {r.gap_id: r for r in db.scalars(select(m.ScoreCheckGapDecision).where(
        m.ScoreCheckGapDecision.batch_id == batch.id, m.ScoreCheckGapDecision.undone_at.is_(None))
        .order_by(m.ScoreCheckGapDecision.created_at, m.ScoreCheckGapDecision.id))}


def automatic_match(gap, threshold):
    return bool(gap.get('suggested_target_id') and (gap.get('confidence') or 0) >= threshold
                and gap['kind'] != 'missing' and (gap['entity_kind'] != 'clinic' or gap.get('suggested_location_id')))


def _gap_state(db, batch):
    decisions, gaps = _decisions(db, batch), []
    counts = {key: 0 for key in s.ScoreCheckGapCounts.model_fields}
    confident = 0
    for raw in batch.gaps or []:
        gap = copy.deepcopy(raw)
        decision = decisions.get(gap['id'])
        if decision:
            gap['decision'] = {'id': decision.id, 'action': decision.action, 'target_id': decision.target_id,
                'replacement': decision.replacement, 'by': decision.created_by, 'at': decision.created_at,
                'location_id': decision.location_id,
                'can_undo': batch.status != 'saved'}
            if decision.action in {'add', 'exclude', 'acknowledge'}:
                counts[{'add': 'added', 'exclude': 'excluded', 'acknowledge': 'acknowledged'}[decision.action]] += 1
        elif automatic_match(gap, batch.confidence_threshold):
            confident += 1
        else:
            counts[gap['kind']] += 1
            counts['unresolved'] += 1
        gaps.append(gap)
    return gaps, counts, confident


def gap_counts(db, batch):
    return _gap_state(db, batch)[1]


def view(db, actor, batch):
    require_role(actor, 'coordinator')
    require(batch.department_id == actor.department_id, 'NOT_FOUND', 'Record not found', 404)
    gaps, counts, confident = _gap_state(db, batch)
    files, effective_decisions = [], _decisions(db, batch)
    for source in _sources(db, batch):
        extraction = _effective_extraction(source, effective_decisions)
        file = extraction.get('files', [{}])[0]
        kinds = {r['code']: r['kind'] for r in extraction.get('duty_codes', [])}
        assignments = extraction.get('assignments', [])
        result = None if not extraction else {'rows_read': file['rows_read'], 'assignment_count': len(assignments),
            'leave_count': sum(kinds.get(a['duty_code']) == 'leave' for a in assignments),
            'off_count': sum(kinds.get(a['duty_code']) == 'off' for a in assignments),
            'people_count': len(extraction.get('people', [])), 'confidence': file['confidence'],
            'layout': file['layout'], 'warnings': [w['message'] for w in extraction.get('warnings', [])]}
        files.append({**{key: getattr(source, key) for key in s.ScoreCheckSource.model_fields if key != 'result'}, 'result': result})
    value = {key: getattr(batch, key) for key in ('id', 'revision', 'name', 'status', 'starts_on', 'ends_on',
             'workspace_id', 'confidence_threshold', 'use_ai', 'profile_id', 'created_at', 'manual_time_hours', 'manual_time_people', 'period_inferred')}
    value.update(files=files, gaps=gaps, counts=counts, confident_matches=confident,
                 can_save=batch.status == 'check' and bool(files) and counts['missing'] == 0 and bool(batch.starts_on and batch.ends_on
                     and 0 < (batch.ends_on - batch.starts_on).days <= 93))
    return {'batch': s.ScoreCheckBatch.model_validate(value).model_dump(mode='json')}


def list_batches(db, actor):
    require_role(actor, 'coordinator')
    return {'batches': [view(db, actor, r)['batch'] for r in db.scalars(select(m.ScoreCheckImportBatch)
        .where(m.ScoreCheckImportBatch.department_id == actor.department_id)
        .order_by(m.ScoreCheckImportBatch.created_at.desc()).limit(50))]}


def upload(db, actor, command, settings):
    value = s.ScoreCheckImportInput.model_validate(command.payload)
    batch = m.ScoreCheckImportBatch(department_id=actor.department_id, created_by=actor.identity_id,
        name=value.name or 'Department (imported)', starts_on=value.starts_on, ends_on=value.ends_on,
        use_ai=value.use_ai, profile_id=value.profile_id, privacy_acknowledged=value.privacy_acknowledged,
        period_inferred=value.starts_on is None, confidence_threshold=value.confidence_threshold, manual_time_hours=value.manual_time_hours,
        manual_time_people=value.manual_time_people)
    db.add(batch)
    db.flush()
    if value.use_ai:
        require(value.profile_id, 'SCORE_AI_PROVIDER', 'Choose an enabled AI provider')
        profile = scoped(db, m.AiProviderProfile, value.profile_id, actor)
        require(profile.archived_at is None and profile.enabled, 'SCORE_PROVIDER_DISABLED', 'Choose an enabled AI provider', 409)
        batch.profile_revision = profile.revision
        batch.provider_snapshot = {'name': profile.name, 'base_url': profile.base_url, 'model': profile.model}
        batch.privacy_acknowledged_at, batch.privacy_acknowledged_by = m.now(), actor.identity_id
    _read_files(db, actor, batch, value.files, settings, group=value.group,
                starts_on=value.starts_on, ends_on=value.ends_on)
    return view(db, actor, batch)


def _read_files(db, actor, batch, files, settings, *, group='', starts_on=None, ends_on=None):
    profile = scoped(db, m.AiProviderProfile, batch.profile_id, actor) if batch.use_ai else None
    if profile:
        require(batch.profile_revision == profile.revision, 'STALE_REVISION', 'The provider changed; acknowledge it again', 409)
    total = db.scalar(select(func.coalesce(func.sum(func.length(m.ImportSource.content)), 0)).where(m.ImportSource.batch_id == batch.id))
    count = db.scalar(select(func.count()).select_from(m.ImportSource).where(m.ImportSource.batch_id == batch.id))
    require(count + len(files) <= s.SCORE_CHECK_LIMITS['files'], 'UPLOAD_LIMIT', 'This import has too many files', 413)
    for file in files:
        try:
            content = base64.b64decode(file.content_base64, validate=True)
        except (ValueError, binascii.Error):
            require(False, 'INVALID_FILE', 'Upload valid base64 file bytes')
        total += len(content)
        require(0 < len(content) <= s.SCORE_CHECK_LIMITS['file_bytes'] and total <= s.SCORE_CHECK_LIMITS['total_file_bytes'],
                'UPLOAD_LIMIT', 'The uploaded files exceed the supported size', 413)
        name = file.file_name.replace('\\', '/').rsplit('/', 1)[-1]
        format = PurePath(name).suffix.lower().lstrip('.')
        source = m.ImportSource(department_id=actor.department_id, batch_id=batch.id, file_name=name,
            file_ref=m.uid(), sha256=hashlib.sha256(content).hexdigest(), format=format,
            group=group, starts_on=starts_on, ends_on=ends_on, period_inferred=starts_on is None, content=content)
        db.add(source)
        db.flush()
        if batch.use_ai:
            from .score_check_ai import queue_file
            queue_file(db, actor, batch, source, profile, settings)
        else:
            require(format in {'csv', 'tsv', 'xlsx'}, 'AI_REQUIRED', 'PDF and image files require an enabled AI provider and consent')
            try:
                extraction = parse_score_check_file(content, name, file_ref=source.file_ref,
                                                    **extraction_context(db, actor, source))
                source.extraction = validate_extraction(db, actor, source, extraction)
            except ScoreCheckParseError as error:
                require(False, 'INVALID_FILE', str(error))
            source.status = 'needs_review' if source.extraction['unparsed'] else 'ready'
    refresh(db, batch)
    _check_coverage(db, actor, batch, settings)


def add_files(db, actor, command, settings):
    value = s.InternalScoreImportAddFilesCommand.model_validate(command.payload)
    batch = scoped(db, m.ScoreCheckImportBatch, value.import_id, actor)
    expected(batch, command.expected_revision)
    require(batch.status not in {'saved', 'discarded'}, 'IMPORT_STATE', 'Add files before saving the import', 409)
    if batch.use_ai:
        require(value.privacy_acknowledged and value.profile_revision == batch.profile_revision,
                'SCORE_AI_CONSENT', 'Acknowledge the current provider before adding files', 409)
        batch.privacy_acknowledged_at, batch.privacy_acknowledged_by = m.now(), actor.identity_id
    _read_files(db, actor, batch, value.files, settings)
    batch.revision += 1
    return view(db, actor, batch)


def _editable(db, actor, batch_id, revision):
    batch = scoped(db, m.ScoreCheckImportBatch, batch_id, actor)
    expected(batch, revision)
    require(batch.status in {'check', 'failed'}, 'IMPORT_STATE', 'Wait for reading to finish; saved imports are edited in the Planner', 409)
    return batch


def update(db, actor, command, settings):
    value = s.ScoreCheckUpdateInput.model_validate(command.payload)
    time_only = value.model_fields_set <= {'batch_id', 'manual_time_hours', 'manual_time_people', 'reason'}
    if time_only:
        batch = scoped(db, m.ScoreCheckImportBatch, value.batch_id, actor)
        expected(batch, command.expected_revision)
        require(batch.status not in {'saved', 'discarded'}, 'IMPORT_STATE', 'Change time before saving the import', 409)
    else:
        batch = _editable(db, actor, value.batch_id, command.expected_revision)
    previous = {'batch_id': batch.id, **{key: getattr(batch, key) for key in
        ('name', 'starts_on', 'ends_on', 'confidence_threshold', 'manual_time_hours', 'manual_time_people', 'period_inferred')}, 'files': []}
    if value.name is not None:
        batch.name = value.name
    for key in ('confidence_threshold', 'manual_time_hours', 'manual_time_people'):
        if key in value.model_fields_set:
            require(key != 'confidence_threshold' or getattr(value, key) is not None,
                    'IMPORT_CONFIDENCE', 'Choose a confidence threshold')
            setattr(batch, key, getattr(value, key))
    start, end = value.starts_on or batch.starts_on, value.ends_on or batch.ends_on
    require(not start or not end or 0 < (end - start).days <= 93, 'IMPORT_PERIOD', 'Choose a period of 1–93 days')
    batch.starts_on, batch.ends_on = start, end
    if 'starts_on' in value.model_fields_set or 'ends_on' in value.model_fields_set:
        batch.period_inferred = False
    if value.period_inferred is not None:
        batch.period_inferred = value.period_inferred
    for edit in value.files:
        source = scoped(db, m.ImportSource, edit.source_id, actor)
        require(source.batch_id == batch.id, 'NOT_FOUND', 'File not found', 404)
        previous['files'].append({'source_id': source.id, **{key: getattr(source, key)
            for key in ('group', 'starts_on', 'ends_on', 'period_inferred', 'work_hours_each', 'made_by', 'people_count_override')}})
        for key in edit.model_fields_set - {'source_id'}:
            require(key not in {'group', 'made_by'} or getattr(edit, key) is not None,
                    'IMPORT_FILE_VALUE', 'Use an empty group or empty people list to clear the value')
            if key == 'made_by' and edit.made_by:
                valid = {r['id'] for r in extraction_context(db, actor, source)['people']}
                require(set(edit.made_by) <= valid, 'IMPORT_PERSON', 'Choose people from this department')
            setattr(source, key, getattr(edit, key))
        require((source.starts_on is None) == (source.ends_on is None) and
                (source.starts_on is None or 0 < (source.ends_on - source.starts_on).days <= 93),
                'IMPORT_PERIOD', 'Choose file period dates spanning 1–93 days')
        if {'starts_on', 'ends_on'} & edit.model_fields_set:
            source.period_inferred = source.starts_on is None
        if edit.period_inferred is not None:
            source.period_inferred = edit.period_inferred
        source.revision += 1
        if 'group' in edit.model_fields_set and source.extraction:
            extraction = copy.deepcopy(source.extraction)
            extraction['files'][0]['group'] = source.group
            source.extraction = extraction
        if {'starts_on', 'ends_on'} & edit.model_fields_set and source.format in {'csv', 'tsv', 'xlsx'}:
            source.extraction = parse_score_check_file(source.content, source.file_name, file_ref=source.file_ref,
                                                       **extraction_context(db, actor, source))
    batch.revision += 1
    if time_only:
        return {**view(db, actor, batch), 'previous_value': previous}
    refresh(db, batch)
    _check_coverage(db, actor, batch, settings)
    return {**view(db, actor, batch), 'previous_value': previous}


def decide(db, actor, command, settings):
    value = s.ScoreCheckGapDecisionInput.model_validate(command.payload)
    replacement = value.replacement.model_dump(mode='json') if value.replacement is not None else None
    batch = _editable(db, actor, value.batch_id, command.expected_revision)
    gap = next((g for g in batch.gaps if g['id'] == value.gap_id), None)
    require(gap is not None, 'NOT_FOUND', 'Gap not found', 404)
    require(value.gap_id not in _decisions(db, batch), 'GAP_DECIDED', 'Undo the current decision before changing it', 409)
    allowed = {'missing': {'acknowledge'}, 'unknown': {'add', 'match', 'exclude'},
               'uncertain': {'confirm', 'match', 'exclude'}, 'unreadable': {'resolve', 'exclude'}}
    require(value.action in allowed[gap['kind']], 'GAP_ACTION', 'Choose an action appropriate to this gap')
    target = value.target_id
    if value.action == 'confirm':
        target = gap['suggested_target_id']
        require(target and value.target_id in (None, target), 'GAP_MATCH', 'Confirm the suggested match or choose Match')
    if value.location_id:
        require(gap['entity_kind'] == 'clinic', 'GAP_LOCATION', 'Only a clinic match has a location')
        scoped(db, m.Location, value.location_id, actor)
    if target:
        cls = {'person': m.Person, 'duty_code': m.AssignmentType, 'clinic': m.Service}[gap['entity_kind']]
        scoped(db, cls, target, actor)
        if cls is m.Person:
            source = db.get(m.ImportSource, gap['source_id'])
            require(target in {p['id'] for p in extraction_context(db, actor, source)['people']},
                    'IMPORT_PERSON', 'Choose a person from this department')
    if value.action == 'add':
        require(gap['entity_kind'] in {'person', 'clinic'}, 'GAP_ACTION',
                'Match an existing duty type or leave this code out; duty types are created in Configuration')
        narrow = s.ScoreCheckGapAddPerson if gap['entity_kind'] == 'person' else s.ScoreCheckGapAddService
        replacement = narrow.model_validate(replacement).model_dump(mode='json')
        kind = {'person': 'people', 'clinic': 'services'}[gap['entity_kind']]
        # The import's Add action creates only this catalogue item; it grants no credentials or rule authority.
        result = catalogue.write_catalogue(db, actor, s.Command(kind='catalogue.create', payload={
            'catalogue': kind, 'value': replacement, 'reason': value.reason or 'Added while checking imported rota'}),
            permission='score_check.add_missing')
        target = result['record']['id']
    if value.action == 'resolve':
        source = db.get(m.ImportSource, gap['source_id'])
        assignment = s.RotaExtractionAssignment.model_validate(value.replacement).model_dump(mode='json')
        require(assignment['file_ref'] == source.file_ref and assignment['cell_ref'] in gap['cell_refs'],
                'EXTRACTION_CELL', 'Resolve the original unreadable cell')
        candidate = _corrected(_effective_extraction(source, _decisions(db, batch)), assignment,
                               replace_cell=source.format in {'csv', 'tsv', 'xlsx'})
        validate_extraction(db, actor, source, candidate)
    decision = m.ScoreCheckGapDecision(department_id=actor.department_id, batch_id=batch.id,
        gap_id=gap['id'], action=value.action, target_id=target, replacement=replacement,
        source_id=gap.get('source_id'), created_by=actor.identity_id, reason=value.reason,
        location_id=value.location_id)
    db.add(decision)
    batch.revision += 1
    db.flush()
    if value.action == 'acknowledge':
        return view(db, actor, batch)
    refresh(db, batch)
    _check_coverage(db, actor, batch, settings)
    return view(db, actor, batch)


def undo(db, actor, command, settings):
    value = s.ScoreCheckUndoInput.model_validate(command.payload)
    batch = _editable(db, actor, value.batch_id, command.expected_revision)
    decision = scoped(db, m.ScoreCheckGapDecision, value.decision_id, actor)
    require(decision.batch_id == batch.id and decision.undone_at is None, 'NOT_FOUND', 'Decision not found', 404)
    if decision.action == 'add':
        gap = next(g for g in batch.gaps if g['id'] == decision.gap_id)
        kind = {'person': 'people', 'duty_code': 'assignment_types', 'clinic': 'services'}[gap['entity_kind']]
        cls = {'people': m.Person, 'assignment_types': m.AssignmentType, 'services': m.Service}[kind]
        row = scoped(db, cls, decision.target_id, actor)
        require(decision.created_by == actor.identity_id, 'FORBIDDEN', 'Only the person who added this item can undo it', 403)
        catalogue.undo_catalogue_created(db, actor, s.Command(
            kind='catalogue.undo_create', target_id=row.id, expected_revision=row.revision,
            payload={'catalogue': kind, 'reason': value.reason or 'Undo imported rota decision'}),
            permission='score_check.add_missing')
    decision.undone_at, decision.undone_by = m.now(), actor.identity_id
    decision.revision += 1
    batch.revision += 1
    db.flush()
    if decision.action == 'acknowledge':
        return view(db, actor, batch)
    refresh(db, batch)
    _check_coverage(db, actor, batch, settings)
    return view(db, actor, batch)


def _materialize(db, actor, batch, settings, *, name=None, reason='Saved checked department rota'):
    decisions = _decisions(db, batch)
    planner = _planner(actor)
    base = db.scalar(select(m.Publication).where(m.Publication.department_id == actor.department_id,
        m.Publication.current.is_(True), m.Publication.starts_on == batch.starts_on,
        m.Publication.ends_on == batch.ends_on))
    workspace = planning.create_workspace(db, planner, s.Command(kind='workspace.create', payload={
        'name': name or batch.name, 'starts_on': batch.starts_on.isoformat(), 'ends_on': batch.ends_on.isoformat()}), settings)['workspace']
    workspace = db.get(m.Workspace, workspace['id'])
    workspace.origin, workspace.base_publication_id = 'import', base.id if base else None
    if base:
        workspace.target_overrides = copy.deepcopy(base.snapshot['workspace'].get('target_overrides', []))
        workspace.rule_adjustments = copy.deepcopy(base.snapshot['workspace'].get('rule_adjustments', []))
    zone = db.get(m.Organisation, actor.organisation_id).timezone
    rows, mappings = [], []
    for source in _sources(db, batch):
        extraction = _effective_extraction(source, decisions)
        by_entity = {}
        for gap in batch.gaps:
            if gap.get('source_id') != source.id or gap['entity_kind'] == 'cell':
                continue
            decision = decisions.get(gap['id'])
            target = (decision.target_id if decision and decision.action in {'match', 'confirm', 'add'} else
                      gap['suggested_target_id'] if not decision and automatic_match(gap, batch.confidence_threshold) else None)
            by_entity[gap['entity_kind'], gap['as_written']] = target
        codes = {r['code']: r for r in extraction.get('duty_codes', [])}
        assignments = extraction.get('assignments', [])
        for item in assignments:
            person = by_entity.get(('person', item['person']))
            typ = by_entity.get(('duty_code', item['duty_code']))
            service = by_entity.get(('clinic', item['clinic']))
            if not person or not typ or (item['clinic'] and not service):
                continue
            type_row = scoped(db, m.AssignmentType, typ, actor)
            code = codes[item['duty_code']]
            if code['kind'] == 'off':
                continue
            timing = type_row.definition.get('default_timing') or {}
            start_time = item.get('start') or code.get('start') or timing.get('start_time')
            end_time = item.get('end') or code.get('end') or timing.get('end_time')
            require(start_time and end_time, 'IMPORT_TIMING', 'A matched duty type needs explicit times or configured default timing')
            day = date.fromisoformat(item['date'])
            require(batch.starts_on <= day < batch.ends_on, 'IMPORT_PERIOD', 'An imported duty is outside the selected period')
            try:
                start = local_stamp(day, start_time, zone)
                end = local_stamp(day + timedelta(days=end_time <= start_time), end_time, zone)
            except ValueError:
                require(False, 'IMPORT_TIMING', 'Correct an ambiguous or invalid local duty time before saving')
            clinic = next((r for r in extraction.get('clinics', []) if r['as_written'] == item['clinic']), {})
            clinic_gap = next((g for g in batch.gaps if g.get('source_id') == source.id
                and g['entity_kind'] == 'clinic' and g['as_written'] == item['clinic']), None)
            clinic_decision = decisions.get(clinic_gap['id']) if clinic_gap else None
            location = (clinic_decision.location_id if clinic_decision else None) or clinic.get('suggested_location_id')
            require(location, 'IMPORT_LOCATION', 'Match the clinic to a location before saving')
            scoped(db, m.Location, location, actor)
            row_no = len(rows) + 2
            rows.append({'assignment_type': type_row.name, 'location': location, 'service': service or '',
                'person': person, 'start': start.isoformat(), 'end': end.isoformat(),
                'overtime': str(item.get('overtime', False)).lower(), 'team_lead': str(item.get('team_lead', False)).lower()})
            mappings.append({'row_number': row_no, 'assignment_type_id': typ, 'location_id': location,
                             'service_id': service, 'person_id': person})
    require(rows, 'EMPTY_IMPORT', 'No resolved duty assignments remain to save')
    # Keep each complete session in one legacy import batch, including when files share the session.
    groups = {}
    for row, mapping in zip(rows, mappings):
        key = tuple(row[k] for k in ('assignment_type', 'location', 'service', 'start', 'end'))
        group = groups.setdefault(key, [])
        require(not any(old_mapping['person_id'] == mapping['person_id'] for _, old_mapping in group),
                'IMPORT_DUPLICATE_ASSIGNMENT', 'A person is listed twice for one session; resolve the overlapping files')
        group.append((row, mapping))
    chunks = [[]]
    for group in groups.values():
        require(len(group) <= 100, 'IMPORT_SESSION_LIMIT', 'One session supports at most 100 people')
        if len(chunks[-1]) + len(group) > 1000:
            chunks.append([])
        chunks[-1].extend(group)
    for index, chunk in enumerate(chunks):
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(row for row, _ in chunk)
        preview = imports.preview_import(db, planner, s.Command(kind='import.preview', expected_revision=workspace.revision,
            payload={'workspace_id': workspace.id, 'source_name': f'{batch.name[:120]} {index + 1}', 'csv_text': out.getvalue()}))
        imports.apply_import(db, planner, s.Command(kind='import.apply', expected_revision=workspace.revision,
            payload={'workspace_id': workspace.id, 'batch_id': preview['batch']['id'],
                'mappings': [{**row, 'row_number': number + 2} for number, (_, row) in enumerate(chunk)],
                'reason': reason}))
    return workspace


def _check_coverage(db, actor, batch, settings):
    if batch.status != 'check' or not batch.starts_on or not batch.ends_on or (batch.ends_on - batch.starts_on).days > 93:
        return
    alarms = []
    nested = db.begin_nested()
    try:
        workspace = _materialize(db, actor, batch, settings)
        shifts = {r['id']: r for r in workspace_shifts(db, workspace.id)}
        policies = {r.id: [r.revision, r.definition] for r in db.scalars(select(m.Policy).where(m.Policy.department_id == batch.department_id))}
        for finding in workspace.validation.get('findings', []):
            if finding['severity'] != 'error' or finding['code'] not in {'COVERAGE_GAP', 'SCOPED_COVERAGE_GAP', 'CAPABILITY_GAP', 'DISTINCT_PERSON_GAP', 'SUPERVISION_GAP'}:
                continue
            shift = shifts.get(finding.get('shift_id'))
            if shift is None:
                continue
            identity = [shift['assignment_type_id'], shift['type_revision'], shift['type_snapshot'], shift['location_id'],
                sorted(shift['service_ids']), aware(shift['start']).isoformat(), aware(shift['end']).isoformat(),
                finding['code'], finding.get('rule_id'), finding.get('detail'), policies.get(finding.get('rule_id')),
                str(finding.get('start')), str(finding.get('end'))]
            alarms.append({'id': snapshot_hash(['coverage', identity]), 'kind': 'missing', 'entity_kind': 'coverage',
                'as_written': f"{aware(shift['start']).isoformat()} · {shift['type_snapshot']['name']}",
                'reason': finding['message'], 'suggested_target_id': None, 'cell_refs': []})
    except Problem as error:
        if error.code not in {'EMPTY_IMPORT', 'IMPORT_TIMING', 'IMPORT_LOCATION', 'IMPORT_DUPLICATE_ASSIGNMENT', 'IMPORT_SESSION_LIMIT'}:
            raise
    finally:
        nested.rollback()
    batch.gaps = [g for g in batch.gaps if g.get('entity_kind') != 'coverage'] + alarms
    db.flush()


def check_current(db, actor, batch, settings):
    if batch.status in {'check', 'failed'}:
        refresh(db, batch)
        _check_coverage(db, actor, batch, settings)


def save(db, actor, command, settings):
    value = s.ScoreCheckSaveInput.model_validate(command.payload)
    batch = _editable(db, actor, value.batch_id, command.expected_revision)
    check_current(db, actor, batch, settings)
    require(view(db, actor, batch)['batch']['can_save'], 'IMPORT_ALARMS',
            'Acknowledge missing staff and cover before saving; the period must be known', 409)
    require(0 < (batch.ends_on - batch.starts_on).days <= 93, 'IMPORT_PERIOD', 'Choose a period of 1–93 days')
    workspace = _materialize(db, actor, batch, settings, name=value.name, reason=value.reason or 'Saved checked department rota')
    batch.workspace_id, batch.status = workspace.id, 'saved'
    batch.revision += 1
    for source in _sources(db, batch):
        source.workspace_id = workspace.id
    return {**view(db, actor, batch), 'workspace': record(workspace)}


def command(db, actor, command, settings):
    require_enabled(db, actor.department_id)
    require_role(actor, 'coordinator')
    action = command.kind.removeprefix('score_check.')
    if action in {'auto', 'benchmark'}:
        from . import score_check_benchmark
        return (score_check_benchmark.create_auto_draft if action == 'auto' else score_check_benchmark.compare)(db, actor, command, settings)
    if action in {'upload', 'save'}:
        return (upload if action == 'upload' else save)(db, actor, command, settings)
    if action == 'check':
        value = s.ScoreCheckSaveInput.model_validate(command.payload)
        batch = _editable(db, actor, value.batch_id, command.expected_revision)
        refresh(db, batch)
        _check_coverage(db, actor, batch, settings)
        batch.revision += 1
        return view(db, actor, batch)
    require(action in {'update', 'decide', 'undo'}, 'UNKNOWN_COMMAND', 'Unsupported score-check command')
    if action == 'decide':
        from pydantic import ValidationError
        try:
            value = s.ScoreCheckGapDecisionInput.model_validate(command.payload)
        except ValidationError:
            # Preserve the semantic refusal for a duty-code Add before the narrow person/clinic input contract
            # reports its field errors (the legacy client used to send a full duty-type value here).
            if command.payload.get('action') == 'add' \
                    and isinstance(command.payload.get('batch_id'), str) and isinstance(command.payload.get('gap_id'), str):
                batch = _editable(db, actor, command.payload['batch_id'], command.expected_revision)
                gap = next((g for g in batch.gaps if g['id'] == command.payload['gap_id']), None)
                if gap is not None and gap['entity_kind'] not in {'person', 'clinic'}:
                    require(False, 'GAP_ACTION',
                            'Match an existing duty type or leave this code out; duty types are created in Configuration')
            raise
        check_current(db, actor, _editable(db, actor, value.batch_id, command.expected_revision), settings)
    return {'update': update, 'decide': decide, 'undo': undo}[action](db, actor, command, settings)
