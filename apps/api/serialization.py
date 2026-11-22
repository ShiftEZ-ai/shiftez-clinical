from datetime import date, datetime
from copy import deepcopy
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.inspection import inspect
from packages.contracts.schemas import AutoscheduleAnimationSettings, SCHEDULE_DETAIL_FIELDS, WishSettings
from packages.domain.time import aware
from packages.domain.rota_summary import normalized_check, normalized_score
from packages.domain.serialization import PLAIN, json_default, json_value
from packages.domain.generation_scope import selected_session_ids


CHECK_SUMMARY = ('valid', 'error_count', 'warning_count', 'evaluated_at', 'fingerprint', 'engine_version', 'scope')
JOB_SUMMARY_FIELDS = ('required_places', 'filled_places', 'open_places_count',
                      'open_extra_cover_places_count', 'open_extra_cover_session_count')
CANDIDATE_EVIDENCE_REF = 'candidate_evidence_ref'
CANDIDATE_EVIDENCE_VERSION = 1
JOB_INPUT_FIELDS = ('generation_engine', 'generation_options', 'compiler_version', 'diagnostic_proposal', 'bindings',
                    'generation_scope', 'generation_scope_source', 'run_context', 'currency_hint', 'input_manifest',
                    'quota_policy_limit', 'planning_group_version_id', 'planning_group_id', 'lookahead', 'planning_context',
                    'proof_scope', 'completion', 'planning_population', 'generation_session_ids', 'generation_manifest', 'continuation',
                    'planner_command_kind', 'planner_source_fingerprint', 'planner_expected_revision', 'fill_options',
                    '_deferred_generation')


def projection_json_value(value):
    """Keep FastAPI's JSON representation while fast-pathing plain projected rows."""
    if type(value) is dict:
        encoded = {}
        for key, child in value.items():
            if isinstance(key, str) and key.startswith('_sa'):
                continue
            key = key if type(key) is str else jsonable_encoder(key)
            encoded[key] = child if type(child) in PLAIN else projection_json_value(child)
        return encoded
    if type(value) is list:
        return [child if type(child) in PLAIN else projection_json_value(child) for child in value]
    return jsonable_encoder(value)


def generation_options_record(options):
    if options is None:
        return None
    value = {k: v for k, v in options.items() if k != 'objective_order'}
    if type(value.get('search_seconds')) is int and value['search_seconds'] in (300, 600):
        value.setdefault('search_minutes', value.pop('search_seconds') // 60)
    if isinstance(value.get('scoring'), dict):
        value['scoring'] = {k: v for k, v in value['scoring'].items() if k != 'tolerance'}
    return value


def generation_defaults_record(options):
    value = generation_options_record(options)
    # Named presets steer individual runs; legacy department settings keep their saved weights.
    if value is not None and 'weight_preset' in value:
        value['weight_preset'] = 'department_defaults'
    return value


def compact_reply(result):
    """A command reply (and its stored receipt) carries the rota check once, as a summary: at department scale the
    full findings are about 0.5 MB and were sent twice per edit. The full check stays on the rota (GET the workspace)."""
    def summary(check):
        return {k: check[k] for k in CHECK_SUMMARY if k in check} if isinstance(check, dict) else check
    workspace = result.get('workspace')
    if isinstance(workspace, dict) and 'validation' in workspace:
        workspace = {k: v for k, v in workspace.items() if k != 'validation'} if 'validation' in result else {
            **workspace, 'validation': summary(workspace['validation'])}
        result = {**result, 'workspace': workspace}
    if isinstance(result.get('validation'), dict) and 'findings' in result['validation']:
        result = {**result, 'validation': summary(result['validation'])}
    return result


_RECORD_EXCLUSIONS = {
    'push_subscription_versions': ('endpoint_ciphertext', 'endpoint_digest', 'p256dh_ciphertext', 'auth_ciphertext'),
    'jobs': ('resume_state', 'resume_checkpoint', 'read_metadata', 'result_summary', 'worker_id', 'supersedes'),
    'rotations': ('mentor_id',),
    'mailbox_connections': ('encrypted_refresh_token', 'encrypted_verifier', 'state_hash'),
    'mailbox_oauth_flows': ('encrypted_refresh_token', 'encrypted_verifier', 'state_hash'),
    'integration_credentials': ('token_ciphertext',),
    'auth_configuration': ('secret_ciphertext', 'previous'),
    'accounts': ('username_normalized', 'email_normalized'),
    'identities': ('subject_normalized',),
    'ai_provider_profiles': ('key_ciphertext',),
    'import_sources': ('content', 'extraction'),
    'feedback_reports': ('screenshot', 'screenshot_media_type'),
}


def _record_fields(mapper, exclude):
    if private := _RECORD_EXCLUSIONS.get(mapper.local_table.name):
        exclude = (*exclude, *private)
    return [field for field in mapper.column_attrs if field.key not in exclude]


def record(row, exclude=()):
    mapper = inspect(row).mapper
    value = {field.key: json_value(getattr(row, field.key)) for field in _record_fields(mapper, exclude)}
    return _finish_record(mapper.local_table.name, value, row)


def _finish_record(table, value, row=None):
    if table == 'jobs' and value.get('search_control') is not None:
        value['search_control'] = {key: value['search_control'][key] for key in ('minutes', 'stop')
                                   if key in value['search_control']}
    if table == "rotations":
        # Bulk rows were detached and never loaded the attachment relationship.
        value['mentor_id'] = row.attachment_mentor_id if row is not None else None
    if table == 'workspaces' and 'validation' in value:
        value['validation'] = normalized_check(value['validation'])
    if table == 'workspaces':
        archived_by_name = None
        if row is not None:
            from sqlalchemy.orm import object_session
            if db := object_session(row):
                from .services.workspace_access import access_record
                value.update(access_record(db, row))
                if row.archived_by:
                    from .models import Identity
                    archived_by_name = db.scalar(select(Identity.display_name).where(Identity.id == row.archived_by))
        value['archived_by_name'] = archived_by_name
        # The durable pre-archive snapshot can contain review/check evidence;
        # expose only lifecycle metadata and keep the full state server-side.
        if isinstance(value.get('archive_state'), dict):
            value['archive_state'] = {key: value['archive_state'][key] for key in (
                'status', 'content_revision', 'visibility', 'purpose') if key in value['archive_state']}
    if table == 'departments' and 'generation_defaults' in value:
        value['generation_defaults'] = generation_defaults_record(value['generation_defaults'])
    if table == 'departments' and 'wish_settings' in value:
        value['wish_settings'] = WishSettings.model_validate(value['wish_settings'] or {}).model_dump(mode='json')
    if table == 'departments' and 'display_settings' in value:
        value['autoschedule_animation'] = AutoscheduleAnimationSettings.model_validate(
            (value['display_settings'] or {}).get('autoschedule_animation') or {}).model_dump(mode='json')
    return value


def records_many(db, sources, *, exclude=None, order_by=None, execution_options=None):
    """Read heterogeneous records together, selecting only their public columns."""
    exclude = exclude or {}
    tables, columns = {}, {}
    for name, (cls, _) in sources.items():
        mapper = inspect(cls)
        tables[name] = mapper.local_table.name
        columns[name] = {field.key: field.columns[0] for field in _record_fields(mapper, exclude.get(name, ()))}
    result = {name: [] for name in sources}
    for name, value in _values_many(db, sources, order_by=order_by, projected_columns=columns,
                                    execution_options=execution_options):
        result[name].append(_finish_record(tables[name], value))
    return result


def shift_link_rows(db, ids, *, include_participants, execution_options=None):
    """Read clinics and public assignment records in one typed statement."""
    from sqlalchemy import Date, DateTime, any_, bindparam, null, union_all
    from sqlalchemy.dialects.postgresql import ARRAY
    from .models import Assignment, ShiftService

    if db.get_bind().dialect.name == 'postgresql':
        shift_ids = bindparam('shift_ids', ids, type_=ARRAY(ShiftService.shift_id.type))
        links = ShiftService.shift_id == any_(shift_ids)
        participant_links = Assignment.shift_id == any_(shift_ids)
    else:
        links = ShiftService.shift_id.in_(ids)
        participant_links = Assignment.shift_id.in_(ids)
    if not include_participants:
        query = select(ShiftService.shift_id, ShiftService.service_id).where(links)
        for shift_id, service_id in db.execute(query.execution_options(**(execution_options or {}))):
            yield shift_id, service_id, None
        return

    fields = _record_fields(inspect(Assignment), ())
    keys = [field.key for field in fields]
    dates = [field.key for field in fields if isinstance(field.columns[0].type, (Date, DateTime))]
    shift_index = keys.index('shift_id')
    assignments = select(*(getattr(Assignment, key) for key in keys), null()).where(participant_links)
    # Keep the assignment columns first in the UNION so results use their SQLAlchemy types.
    services = select(*(ShiftService.shift_id if key == 'shift_id' else null() for key in keys),
                      ShiftService.service_id).where(links)
    for row in db.execute(union_all(assignments, services).execution_options(**(execution_options or {}))):
        shift_id, service_id = row[shift_index], row[-1]
        if service_id is not None:
            yield shift_id, service_id, None
            continue
        value = dict(zip(keys, row))
        for key in dates:
            if (item := value[key]) is not None:
                value[key] = json_default(item)
        yield shift_id, None, value


def rows_many(db, sources, *, order_by=None):
    """Acquire independent typed rows in one statement, without attaching them to the session."""
    result = {name: [] for name in sources}
    for name, value in _values_many(db, sources, order_by=order_by):
        result[name].append(sources[name][0](**value))
    return result


def _values_many(db, sources, *, order_by=None, projected_columns=None, execution_options=None):
    import json
    from sqlalchemy import Boolean, Date, DateTime, JSON, Text, cast, func, literal, union_all
    from . import models as m

    sqlite = db.bind.dialect.name == 'sqlite'
    build = func.json_object if sqlite else func.json_build_object
    order_by = order_by or {}
    queries, converters = [], {}
    for name, (cls, where) in sources.items():
        # Core JSON UNIONs do not receive the Session's ORM loader criteria.
        if issubclass(cls, m.Withdrawable):
            where = where & cls.withdrawn.is_(False)
        elif cls is m.Mentorship:
            from .db import live_mentorship_clause
            where = where & live_mentorship_clause()
        mapper = inspect(cls)
        columns = ((column.key, column) for column in mapper.columns) if projected_columns is None else (
            projected_columns[name].items())
        fields, typed = [], []
        for key, column in columns:
            value = func.json(column) if sqlite and isinstance(column.type, JSON) else column
            fields.extend((literal(key), value))
            if isinstance(column.type, DateTime):
                typed.append((key, datetime.fromisoformat))
            elif isinstance(column.type, Date):
                typed.append((key, date.fromisoformat))
            elif isinstance(column.type, Boolean):
                typed.append((key, bool))
        converters[name] = typed
        # Rank before serialization: private ordering expressions need not be public record fields.
        position = func.row_number().over(order_by=order_by[name]) if name in order_by else literal(0)
        queries.append(select(literal(name).label('source'), cast(build(*fields), Text),
                              position.label('position')).select_from(mapper.persist_selectable).where(where))
    query = union_all(*queries)
    if order_by:
        query = query.order_by('source', 'position')
    if execution_options:
        query = query.execution_options(**execution_options)
    for name, encoded, _ in db.execute(query):
        value = json.loads(encoded)
        for key, convert in converters[name]:
            if (item := value[key]) is not None:
                item = convert(item)
                value[key] = json_default(item) if projected_columns is not None and isinstance(item, date) else item
        # A decoded row already owns its JSON containers.
        yield name, value


def candidate_evidence_count(reference):
    if (isinstance(reference, dict) and reference.get('version') == CANDIDATE_EVIDENCE_VERSION
            and type(reference.get('count')) is int and reference['count'] >= 0
            and isinstance(reference.get('digest'), str) and len(reference['digest']) == 64):
        return reference['count']
    return None


def job_result_summary(result):
    if result is None:
        return None
    value = {k: v for k, v in result.items()
             if k not in {'candidate_evidence', CANDIDATE_EVIDENCE_REF, 'error', 'error_detail'}}
    if (error_detail := job_error_detail(result)) is not None:
        value['error_detail'] = error_detail
    if result.get('status') in {'candidate_feasible', 'candidate_with_gaps', 'requirements_conflict',
                               'needs_more_time', 'unknown_time_limit'}:
        from packages.domain.generation_outcome import generation_outcome
        value['generation_outcome'] = generation_outcome(result)
    context = result.get('fix_list_context') or {}
    if 'focus_ids' in context:
        from packages.domain.rota_summary import submission_prerequisite
        value['submission_issue'] = submission_prerequisite(len(context['focus_ids']))
    if value.get('scope') and 'score' in value:
        value['score'] = None
    if isinstance(value.get('effective_options'), dict):
        value['effective_options'] = generation_options_record(value['effective_options'])
    if isinstance(value.get('summary'), dict):
        value['summary'] = {k: normalized_score(v) if k in {'score_before', 'score_saved', 'score'} else v
                            for k, v in value['summary'].items()}
    if isinstance(value.get('reconciliation'), dict):
        value['reconciliation'] = {key: child[:100] if key in {'changed_facts', 'conflicting_placements'} else child
                                   for key, child in value['reconciliation'].items()}
    validation = value.get('validation')
    if isinstance(validation, dict) and 'score' in validation:
        value['validation'] = {**validation, 'score': None if validation.get('scope') else normalized_score(validation['score'])}
    if (count := candidate_evidence_count(result.get(CANDIDATE_EVIDENCE_REF))) is not None:
        value['candidate_evidence_count'] = count
    elif 'candidate_evidence' in result:
        value['candidate_evidence_count'] = len(result['candidate_evidence'])
    if 'diagnostics' in result:
        value['diagnostics'] = [{**{k: v for k, v in item.items() if k != 'candidates'},
                                 **({'candidate_count': len(item['candidates'])} if 'candidates' in item else {})}
                                for item in result['diagnostics']]
    if 'checks' in result:
        value['checks'] = {phase: job_result_summary(check) for phase, check in result['checks'].items()}
    return value


def job_error_detail(result):
    """Read the public envelope already stored by the worker; do not re-filter it here."""
    if not isinstance(result, dict):
        return None
    detail = result.get('error_detail') or result.get('error')
    if not isinstance(detail, dict) or not isinstance(detail.get('details'), list):
        return None
    return deepcopy(detail)


def job_result_metadata(result, *, include_error_detail=False):
    """History needs the outcome and counts, never the full proposal or its evidence."""
    if result is None:
        return None
    value = {'status': result.get('status')}
    if include_error_detail and (error_detail := job_error_detail(result)) is not None:
        value['error_detail'] = error_detail
    for key in ('solver_status', 'search_stop', 'score_completed'):
        if key in result:
            value[key] = deepcopy(result[key])
    if 'solver_status' in result and 'score' in result:
        value['score'] = deepcopy(result['score'])
    if result.get('diagnostic_proposal'):
        from packages.domain.generation_outcome import generation_outcome
        value['generation_outcome'] = generation_outcome(result)
    elif result.get('generation_outcome') is not None:
        value['generation_outcome'] = deepcopy(result['generation_outcome'])
    elif result.get('status') in {'candidate_feasible', 'candidate_with_gaps', 'requirements_conflict',
                                 'needs_more_time', 'unknown_time_limit'}:
        from packages.domain.generation_outcome import generation_outcome
        value['generation_outcome'] = generation_outcome(result)
    if 'additions' in result or 'removals' in result:
        value['change_count'] = len(result.get('additions') or []) + len(result.get('removals') or [])
    elif 'change_count' in result:
        value['change_count'] = result['change_count']
    if result.get('reconciliation') is not None:
        receipt = result['reconciliation']
        value['reconciliation'] = {key: receipt[key] for key in ('id', 'status', 'changed_fact_count',
            'conflicting_placement_count', 'retention_optimal', 'validation', 'prepared_fingerprint',
            'current_fingerprint', 'validation_current', 'solver_status', 'reconciliation_allowance_exhausted',
            'population_recorded', 'population_people', 'applied_content_revision') if key in receipt}
    # applied_content_revision: Recent runs offers Undo on an applied run that is not the latest job (a check after it).
    for key in ('algorithm_version', 'applied_content_revision', 'generation_scope', 'generation_scope_source',
                'planning_group_version_id', 'planning_context', 'lookahead', 'proof_scope', 'completion',
                'stop_reason', 'continuation', 'on_conflict'):
        if key in result:
            value[key] = deepcopy(result[key])
    if result.get('resume_available'):
        value['resume_available'] = True
    if 'diagnostic_proposal' in result:
        value['diagnostic_proposal'] = result['diagnostic_proposal']
    if result.get('applied') is not None:
        value['applied'] = result['applied']
    if result.get('generation_manifest') is not None:
        value['generation_manifest'] = deepcopy(result['generation_manifest'])
    for key, fields in (('validation', ('valid', 'error_count', 'warning_count')),
                        ('summary', JOB_SUMMARY_FIELDS)):
        part = {field: (result.get(key) or {}).get(field) for field in fields}
        if any(v is not None for v in part.values()):
            value[key] = part
    return value


def job_read_metadata(snapshot, result):
    # Always recorded (None when not recoverable), so a detail read never reloads the frozen input to find it again.
    snapshot = {**snapshot, 'run_context': job_run_context(snapshot) if 'workspace' in snapshot else None}
    if snapshot.get('input_manifest'):
        snapshot['input_manifest'] = {key: snapshot['input_manifest'][key] for key in (
            'format', 'token', 'components', 'dirty_hint') if key in snapshot['input_manifest']}
        snapshot.setdefault('currency_hint', snapshot['input_manifest'].get('dirty_hint'))
    result_value = job_result_metadata(result, include_error_detail=True)
    if snapshot.get('generation_manifest') is not None:
        result_value = {**(result_value or {}), 'generation_manifest': deepcopy(snapshot['generation_manifest'])}
    value = {'input_snapshot': {key: snapshot[key] for key in JOB_INPUT_FIELDS if key in snapshot
                                and (snapshot[key] is not None or key in ('generation_engine', 'run_context'))},
             'result': result_value}
    if snapshot.get('_deferred_generation'):
        # The request token is an internal lease value until the worker freezes the inputs.
        value['input_fingerprint'] = None
    elif snapshot.get('fingerprint') is not None:
        value['input_fingerprint'] = snapshot['fingerprint']
    return value


def retain_job_reconciliation(job, receipt, *, result=None, interim=False, applied_revision=None):
    """Store a new receipt with the result, or change only the application state."""
    metadata = job.read_metadata
    if metadata is None:
        metadata = job_read_metadata(job.input_snapshot, job.result)
        job.result_summary = refresh_job_result_projection(job)
    projected = {**(metadata.get('result') or {})}
    if receipt is not None:
        projected['reconciliation'] = job_result_metadata({'reconciliation': receipt})['reconciliation']
    if applied_revision is not None:
        projected['applied_content_revision'] = applied_revision
    job.read_metadata = {**metadata, 'result': projected}
    if result is not None:
        result = {**result, 'reconciliation': receipt}
        if interim:
            from sqlalchemy.orm.attributes import set_committed_value
            # In-session readers see this receipt without flushing the proposal before the final update.
            set_committed_value(job, 'result', result)
        else:
            job.result = result


def job_result_with_reconciliation(job, result):
    """Read current application state with the retained receipt and proposal."""
    stored = (getattr(job, '__dict__', {}).get('read_metadata') or {}).get('result') or {}
    if result is None or not stored:
        return result
    if 'applied_content_revision' in stored and result.get('applied_content_revision') != stored['applied_content_revision']:
        result = {**result, 'applied_content_revision': stored['applied_content_revision']}
    receipt, projected = result.get('reconciliation'), stored.get('reconciliation')
    if isinstance(receipt, dict) and isinstance(projected, dict) and projected.get('id') == receipt.get('id'):
        state = {key: projected[key] for key in ('status', 'applied_content_revision')
                 if key in projected and projected[key] != receipt.get(key)}
        if state:
            result = {**result, 'reconciliation': {**receipt, **state}}
    return result


def job_reconciliation_columns():
    """Read just the immutable receipt and the revision used by the application-state guard."""
    from . import models as m
    from sqlalchemy import func
    revision = m.Job.result['applied_content_revision']
    current_revision = func.coalesce(
        m.Job.read_metadata['result', 'applied_content_revision'].as_integer(),
        revision.as_integer())
    return m.Job.result['reconciliation'], revision, current_revision


def effective_input_fingerprint(row, *, input_snapshot=..., projection=..., deferred=...):
    """Return the semantic fingerprint safe for a job read."""
    def loaded(name):
        state = getattr(row, '_sa_instance_state', None)
        if state is not None and name in state.unloaded:
            return None
        return getattr(row, name, None)

    if deferred is True:
        return None
    if projection is not ...:
        metadata = projection if isinstance(projection, dict) else {}
        snapshot = metadata.get('input_snapshot')
        if isinstance(snapshot, dict) and snapshot.get('_deferred_generation'):
            return None
        if 'input_fingerprint' in metadata:
            return metadata['input_fingerprint']
        # A populated retained input projection is known to predate the deferred marker;
        # its row column remains the frozen fingerprint.  An empty projection is unknown.
        return loaded('input_fingerprint') if snapshot else None
    snapshot = loaded('input_snapshot') if input_snapshot is ... else input_snapshot
    if isinstance(snapshot, dict) and snapshot.get('_deferred_generation'):
        return None
    metadata = loaded('read_metadata')
    if isinstance(metadata, dict) and 'input_fingerprint' in metadata:
        return metadata['input_fingerprint']
    if isinstance(snapshot, dict) and snapshot.get('fingerprint') is not None:
        return snapshot['fingerprint']
    return loaded('input_fingerprint')


def job_run_context(snapshot):
    """Requested dates and actual duty extent have different meanings; recover only frozen facts."""
    workspace = snapshot.get('workspace') or {}
    if not workspace.get('starts_on') or not workspace.get('ends_on') or 'focus_ids' not in snapshot or 'shifts' not in snapshot:
        return None
    selected = set(selected_session_ids(snapshot))
    shifts = [s for s in snapshot['shifts'] if s['id'] in selected]
    if {s['id'] for s in shifts} != selected:
        return None
    return {'scope_window': {'start': workspace['starts_on'], 'end': workspace['ends_on']},
            'selection_window': {'start': min(aware(s['start']) for s in shifts).isoformat(),
                                 'end': max(aware(s['end']) for s in shifts).isoformat()} if shifts else None,
            'service_ids': sorted({sid for s in shifts for sid in s['service_ids']}),
            'basis': 'frozen_workspace_and_selected_duties'}


def schedule_projection(data):
    session, participant, coverage, check = (SCHEDULE_DETAIL_FIELDS[key]
                                            for key in ('session', 'participant', 'coverage', 'check'))
    shifts = [{**{key: value for key, value in row.items() if key not in session},
               'participants': [{key: value for key, value in p.items() if key not in participant}
                                for p in row['participants']]} for row in data['shifts']]
    workspace = data['workspace']
    if isinstance(workspace.get('validation'), dict):
        validation = {key: value for key, value in workspace['validation'].items() if key not in check}
        if isinstance(validation.get('coverage'), list):
            validation['coverage'] = [{key: value for key, value in row.items() if key not in coverage}
                                      for row in validation['coverage']]
        workspace = {**workspace, 'validation': validation}
    return {**data, 'workspace': workspace, 'shifts': shifts}


def job_detail_result(kind, result):
    value = job_result_summary(result)
    def visible_check(check):
        from packages.contracts.schemas import JOB_PRIVATE_CHECK_FIELDS
        # Reuse inputs belong to the retained job, not its findings/coverage UI.
        return {key: part for key, part in check.items() if key not in JOB_PRIVATE_CHECK_FIELDS}
    if value is not None:
        if kind == 'validation':
            value = visible_check(value)
        if isinstance(value.get('validation'), dict):
            value['validation'] = visible_check(value['validation'])
        if isinstance(value.get('checks'), dict):
            value['checks'] = {phase: job_detail_result('validation', check) for phase, check in value['checks'].items()}
    if kind == 'feasibility' and value is not None and 'checks' in value:
        # Rule review uses diagnostics and validation; score detail and duplicate findings stay available on demand.
        value['checks'] = {phase: {key: part for key, part in check.items() if key not in {'summary', 'findings'}}
                           for phase, check in value['checks'].items()}
    return value


def refresh_job_result_projection(job):
    result = job.result
    retained = (job.result_summary or {}).get('evidence') if CANDIDATE_EVIDENCE_REF in (result or {}) else None
    return job_result_projection(job.kind, result, retained_evidence=retained)


def job_result_projection(kind, result, *, retained_evidence=None):
    if result is None:
        return None

    def counts(rows, fallback=None):
        def empty():
            return {'total': 0, 'eligible': 0, 'reasons': {}}
        value = {'all': empty(), 'shifts': {}, 'shift_id': fallback}
        for row in rows:
            exclusions = row.get('exclusions') or []
            codes = {reason.get('code') for reason in exclusions}
            targets = [value['all']]
            shift = row.get('shift_id', fallback)
            if shift is not None:
                targets.append(value['shifts'].setdefault(shift, empty()))
            for target in targets:
                target['total'] += 1
                target['eligible'] += not exclusions
                for code in codes:
                    target['reasons'][code] = target['reasons'].get(code, 0) + 1
        return value

    def evidence(check):
        return {'candidate_evidence': counts(check.get('candidate_evidence', [])),
                'diagnostics': [counts(item.get('candidates', []), item.get('shift_id'))
                                for item in check.get('diagnostics', [])],
                'checks': {phase: evidence(part) for phase, part in check.get('checks', {}).items()}}
    stats = evidence(result)
    if CANDIDATE_EVIDENCE_REF in result:
        reference = result[CANDIDATE_EVIDENCE_REF]
        stats[CANDIDATE_EVIDENCE_REF] = reference
        if retained_evidence and retained_evidence.get(CANDIDATE_EVIDENCE_REF) == reference:
            stats['candidate_evidence'] = retained_evidence['candidate_evidence']
    projection = {'result': job_detail_result(kind, result), 'evidence': stats}
    if kind == 'validation' and 'input_manifest' in result:
        projection['input_manifest'] = result['input_manifest']
    return projection


def job_outcome(status, result):
    """Keep worker completion separate from the schedule it produced; UNKNOWN is never infeasible."""
    from .services.generation_resume import RESUMABLE_GENERATION_RESULTS

    if status in {'failed', 'cancelled'}:
        return status
    if status not in {'succeeded', 'applied'}:
        return None
    result = result or {}
    from packages.domain.generation_outcome import generation_outcome
    final = (generation_outcome(result) if result.get('diagnostic_proposal')
             else result.get('generation_outcome') or generation_outcome(result))
    if final['candidate'] == 'valid':
        return 'complete'
    if result.get('checkpointed'):
        return 'partial'
    if result.get('diagnostic_proposal') and result.get('status') in {
            'candidate_feasible', 'candidate_with_gaps', 'unknown_time_limit', 'needs_more_time'}:
        return 'partial'
    outcome = {'requirements_conflict': 'infeasible', 'candidate_with_gaps': 'partial',
               'invalid_inputs': 'failed', 'unsupported_rules': 'failed', 'translation_error': 'failed',
               'model_invalid': 'failed', 'cancelled': 'cancelled'}.get(result.get('status'))
    if outcome:
        return outcome
    validation, summary = result.get('validation') or {}, result.get('summary') or {}
    if validation.get('valid') is False or validation.get('error_count', 0) or summary.get('open_places_count', 0):
        return 'partial'
    if result.get('status') == 'candidate_feasible' or validation.get('valid') is True:
        return 'complete'
    return 'needs_more_time' if result.get('status') in RESUMABLE_GENERATION_RESULTS else 'failed'


def job_progress_record(progress, *, include_events=False, focus_ids=()):
    if progress is None or not include_events:
        return (progress if progress is None else
                {key: value for key, value in progress.items() if key not in {'events', 'solution', 'precheck_issues'}})
    # Older running jobs may still hold organisation-wide published context.
    focus = set(focus_ids)
    value = dict(progress)
    if 'precheck_issues' in value:
        value['precheck_issues'] = [{**issue, 'samples': [sample for sample in issue.get('samples', [])
            if not sample.get('shift_id') or sample['shift_id'] in focus]} for issue in value['precheck_issues']]
    if 'events' in value:
        value['events'] = [{**event, **{key: [pair for pair in event.get(key, []) if pair['shift_id'] in focus]
                                      for key in ('adds', 'removes')}} for event in value['events']]
    if value.get('solution') is not None:
        value['solution'] = {**value['solution'], 'pairs': [pair for pair in value['solution']['pairs']
                                                         if pair['shift_id'] in focus]}
    return value


def job_record(row, *, include_result=True, include_events=False, include_progress_solution=True,
               input_snapshot=None, result_metadata=..., result_summary=...,
               input_fingerprint=..., include_error_detail=False):
    snapshot = row.input_snapshot if input_snapshot is None else input_snapshot
    if isinstance(snapshot, dict) and snapshot.get('_deferred_generation'):
        input_fingerprint = None
    elif input_fingerprint is ...:
        input_fingerprint = effective_input_fingerprint(row, input_snapshot=snapshot)
    value = record(row, exclude=('input_snapshot', 'result', 'input_fingerprint'))
    value['input_fingerprint'] = input_fingerprint
    value['progress'] = job_progress_record(value.get('progress'), include_events=include_events,
                                           focus_ids=snapshot.get('focus_ids') or ())
    if value['progress'] is not None and not include_progress_solution and not include_events:
        # The workspace job list needs the current stage and score only. The candidate
        # solution is fetched by the job detail/status endpoints when the planner opens it.
        value['progress'].pop('solution', None)
    result = (row.result if result_summary is ... else result_summary) if include_result else (
        row.result if result_metadata is ... else result_metadata)
    result = job_result_with_reconciliation(row, result)
    error_detail = job_error_detail(result) if include_error_detail else None
    if include_result:
        value['result'] = job_result_summary(result)
    else:
        result = value['result'] = job_result_metadata(result)
        manifest = (result or {}).get('generation_manifest')
        retained = manifest.get('result') if isinstance(manifest, dict) else None
        if isinstance(retained, dict):
            if (count := candidate_evidence_count(retained.get(CANDIDATE_EVIDENCE_REF))) is not None:
                result['candidate_evidence_count'] = count
    if error_detail is not None:
        value['error_detail'] = error_detail
        if isinstance(value['result'], dict):
            value['result'].pop('error_detail', None)
    value['outcome'] = job_outcome(row.status, result) if row.kind == 'generation' and (
        result is not None or row.status in {'failed', 'cancelled'}) else None
    value['generation_engine'] = snapshot.get('generation_engine', 'legacy') if row.kind == 'generation' else None
    if value['generation_engine'] == 'legacy' and value['result'] is not None:
        from packages.contracts.rule_catalogue import GREEDY_NOT_APPLIED
        value['result'].setdefault('not_applied', deepcopy(GREEDY_NOT_APPLIED))
        # Historical artifacts remain immutable; the stable input ID owns current presentation copy.
        current = {item['input']: item for item in GREEDY_NOT_APPLIED}
        value['result']['not_applied'] = [dict(item, message=current[item['input']]['message'])
            if item.get('input') in current else item for item in value['result']['not_applied']]
    options = snapshot.get('generation_options') if value['generation_engine'] == 'cp_sat' else None
    value['effective_options'] = generation_options_record(options)
    if snapshot.get('generation_scope') is not None:
        value['generation_scope'] = deepcopy(snapshot['generation_scope'])
    for key in ('planning_group_version_id', 'planning_group_id', 'lookahead', 'planning_context', 'proof_scope',
                'completion', 'continuation'):
        if key in (result or {}) or key in snapshot:
            value[key] = deepcopy((result or {}).get(key, snapshot.get(key)))
    if 'stop_reason' in (result or {}):
        value['stop_reason'] = result['stop_reason']
    for key in ('solver_status', 'score', 'search_stop'):
        if row.kind == 'generation' and key in (result or {}):
            value[key] = deepcopy(result[key])
    value['compiler_version'] = snapshot.get('compiler_version')
    value['run_context'] = snapshot.get('run_context')
    if row.kind == 'planner_preview':
        for key in ('planner_command_kind', 'planner_source_fingerprint', 'planner_expected_revision'):
            if key in snapshot:
                value[key] = deepcopy(snapshot[key])
    value['diagnostic_proposal'] = bool(snapshot.get('diagnostic_proposal')
        or (result or {}).get('diagnostic_proposal') or (row.progress or {}).get('diagnostic_proposal'))
    if row.kind == 'feasibility':
        value['checked_rules'] = [{'policy_id': b['policy_id'], 'temporary_change': b['temporary_change']}
                                  for b in snapshot.get('bindings', [])]
    return value
