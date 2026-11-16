"""Read-only, current rota summaries and department date gaps."""
from sqlalchemy import func, select
from apps.api import models as m
from apps.api.snapshots import cheap_fingerprints, domain_snapshot, fingerprints, workspace_shifts
from packages.domain.rota_summary import cause_summary, rota_summary, rota_coverage_gaps, fix_list_context, remember_fix_list
from packages.domain.score import schedule_score, score_dependencies, settings_of, VERSION as SCORE_VERSION
from packages.domain.validation import evaluate


def coverage_gaps(db, department_id):
    rows = db.execute(select(m.Workspace.id, m.Workspace.starts_on, m.Workspace.ends_on).where(
        m.Workspace.department_id == department_id, m.Workspace.archived_at.is_(None)))
    return rota_coverage_gaps([dict(row._mapping) for row in rows])


def validation_job_stamp(db, *, department_id=None, workspace_id=None):
    """Bounded validation-only stamp; the general job stamp is job_reads.job_stamp."""
    scope = m.Job.department_id == department_id if department_id is not None else m.Job.workspace_id == workspace_id
    in_flight = tuple((job_id, status) for job_id, status in db.execute(
        select(m.Job.id, m.Job.status).where(
            scope, m.Job.kind == 'validation', m.Job.status.in_(('queued', 'running')))
        .order_by(m.Job.id)))
    newest = db.scalar(select(m.Job.id).where(
        scope, m.Job.kind == 'validation').order_by(
            m.Job.created_at.desc(), m.Job.seq.desc()).limit(1))
    return in_flight, newest


def checking_workspaces(db, workspace_ids):
    if not workspace_ids:
        return {}
    ranked = select(
        m.Job.workspace_id, m.Job.status, m.Job.created_at,
        func.row_number().over(partition_by=m.Job.workspace_id,
                               order_by=(m.Job.created_at.desc(), m.Job.seq.desc())).label('position')
    ).where(m.Job.workspace_id.in_(workspace_ids), m.Job.kind == 'validation').subquery()
    rows = db.execute(select(ranked.c.workspace_id, ranked.c.status, ranked.c.created_at).where(ranked.c.position == 1))
    return {workspace_id: {'status': status, 'queued_at': queued_at,
                           'checking': status in {'queued', 'running'}}
            for workspace_id, status, queued_at in rows}


def reusable_check(check, current_fingerprint, options):
    """Legacy causes, partial checks and incomplete scores require a current read-only evaluation."""
    from packages.domain.input_manifest import FORMAT as MANIFEST_FORMAT
    from packages.contracts.schemas import ScheduleScore
    from packages.domain.finding_causes import DIAGNOSTIC_VERSION
    from packages.domain.validation import ENGINE_VERSION, check_contracts
    from pydantic import ValidationError
    input_manifest = (check or {}).get('input_manifest') or {}
    manifest_components = input_manifest.get('components') or {}
    manifest_current = (input_manifest.get('format') == MANIFEST_FORMAT
                        and input_manifest.get('token') == current_fingerprint
                        and input_manifest.get('token') == (check or {}).get('fingerprint')
                        and all(isinstance(manifest_components.get(key), str)
                                for key in ('content', 'mandatory_inputs', 'objective_inputs', 'toolchain')))
    if (not check or not manifest_current or check.get('fingerprint') != current_fingerprint or check.get('engine_version') != ENGINE_VERSION
            or 'sessions' not in (check.get('fix_list_context') or {})
            or check.get('diagnostic_version') != DIAGNOSTIC_VERSION or check.get('scope')
            or check.get('score_dependencies') != score_dependencies(options)
            or not check.get('evaluated_at') or '_session_parts' not in check
            or not all(k in check for k in ('valid', 'error_count', 'warning_count', 'findings', 'coverage', 'contracts'))
            or not isinstance(check['findings'], list) or not isinstance(check['coverage'], list)
            or not isinstance(check['_session_parts'], list) or check['contracts'] != check_contracts()
            or check.get('quota_report', {}).get('complete') is False):
        return False
    if (check['error_count'] != sum(f['severity'] == 'error' for f in check['findings'])
            or check['warning_count'] != sum(f['severity'] == 'warning' for f in check['findings'])
            or check['valid'] != (check['error_count'] == 0)):
        return False
    # Ungrouped historical findings count one issue several times: re-evaluate them instead of reusing their verdict.
    from packages.domain.rota_summary import normalized_check
    grouped = normalized_check(check)
    if (grouped['error_count'], grouped['warning_count']) != (check['error_count'], check['warning_count']):
        return False
    try:
        score = ScheduleScore.model_validate(check.get('score'))
    except ValidationError:
        return False
    return score.version == SCORE_VERSION and score.settings == settings_of(options)


def current_check(db, workspace, current_fingerprint=None):
    """Stored evidence must match both current inputs and the evaluator contract."""
    check = workspace.validation
    department = db.get(m.Department, workspace.department_id)
    current = current_fingerprint or fingerprints(db, [workspace])[workspace.id]
    if workspace.base_publication_id:
        from packages.domain.validation import ENGINE_VERSION, check_contracts
        from packages.domain.input_manifest import FORMAT as MANIFEST_FORMAT
        input_manifest = (check or {}).get('input_manifest') or {}
        components = input_manifest.get('components') or {}
        base_until = db.get(m.Publication, workspace.base_publication_id).effective_until
        reuse = bool(check and check.get('scope') == 'published_changes'
            and check.get('base_publication_id') == workspace.base_publication_id
            and 'base_effective_until' in check
            and check['base_effective_until'] == (base_until.isoformat() if base_until else None)
            and check.get('fingerprint') == current and check.get('engine_version') == ENGINE_VERSION
            and check.get('contracts') == check_contracts() and check.get('evaluated_at')
            and input_manifest.get('format') == MANIFEST_FORMAT
            and input_manifest.get('token') == current
            and all(isinstance(components.get(key), str)
                    for key in ('content', 'mandatory_inputs', 'objective_inputs', 'toolchain'))
            and (check.get('quota_report') or {}).get('context_complete',
                (check.get('quota_report') or {}).get('complete', True)))
    else:
        reuse = reusable_check(check, current, department.generation_defaults)
    return bool(reuse and {r['shift_id'] for r in check.get('coverage', [])} == set(db.scalars(
        select(m.Shift.id).where(m.Shift.workspace_id == workspace.id))))


def summary_check(db, workspace, *, include_shifts=True):
    department = db.get(m.Department, workspace.department_id)
    organisation = db.get(m.Organisation, department.organisation_id)
    current_fingerprint = cheap_fingerprints(db, [workspace], checks={workspace.id: workspace.validation}).get(workspace.id)
    if current_fingerprint is None:
        current_fingerprint = fingerprints(db, [workspace])[workspace.id]
    validation = workspace.validation
    check_job = checking_workspaces(db, [workspace.id]).get(workspace.id)
    checking = bool(check_job and check_job['checking'])
    reuse = not workspace.base_publication_id and reusable_check(validation, current_fingerprint, department.generation_defaults)
    shifts = workspace_shifts(db, workspace.id) if reuse and include_shifts else None
    # A complete full check covers even cancelled/closed focus sessions. Missing rows are not a fresh summary.
    reuse = reuse and (not include_shifts or
                       {r['shift_id'] for r in validation['coverage']} == {s['id'] for s in shifts})
    retain = ((checking or (validation or {}).get('scope') == 'command_changes')
              and (not workspace.base_publication_id or (validation or {}).get('scope') == 'published_changes')
              and isinstance((validation or {}).get('coverage'), list)
              and isinstance((validation or {}).get('findings'), list)
              and (validation.get('scope') == 'command_changes' or validation.get('evaluated_at')))
    if reuse or retain:
        if shifts is None and include_shifts:
            shifts = workspace_shifts(db, workspace.id)
        if include_shifts:
            # The fresh check already resolved derived recurrence closure without rebuilding all domain inputs.
            if validation.get('scope') == 'command_changes':
                from packages.domain.recurrence import inactive
                shifts = [s for s in shifts if not inactive(s)]
            else:
                active = {r['shift_id'] for r in validation['coverage'] if r['status'] not in {'closed', 'cancelled'}}
                shifts = [s for s in shifts if s['id'] in active]
        checked_at = validation.get('evaluated_at')
    else:
        from .read_projections import cached
        from packages.domain.input_manifest import digest
        # Scoped checks also compare against the base's mutable in-force window, outside the draft manifest.
        base_until = db.scalar(select(m.Publication.effective_until).where(
            m.Publication.id == workspace.base_publication_id)) if workspace.base_publication_id else None
        def build(_wid):
            if workspace.base_publication_id:
                # Published-change drafts check only changed sessions (maintenance); revalidate owns that scope.
                from .planning import revalidate
                captured = []
                check = revalidate(db, workspace, store=False, data_out=captured)
                data = captured[0]
            else:
                data = db.info.get('read_snapshots', {}).get(workspace.id)
                if data is None:
                    data = domain_snapshot(db, workspace)
                check = evaluate(data)
                check['score'] = schedule_score(data, check, department.generation_defaults)
                check['fix_list_context'] = fix_list_context(data)
                check['evaluated_at'] = m.now().isoformat()
                remember_fix_list(workspace.validation, check, accepted_issues=workspace.accepted_issues or [])
            focus = set(data['focus_ids'])
            return {'validation': check, 'shifts': [s for s in data['shifts'] if s['id'] in focus]}
        value = cached(db, {workspace.id: workspace}, 'summary_check', {workspace.id:
            # The current fingerprint carries the versioned toolchain.  Do not
            # invalidate this durable projection for a source-only deployment.
            digest([current_fingerprint, department.generation_defaults, workspace.validation,
                    workspace.status, workspace.submitted_fingerprint, workspace.base_publication_id,
                    base_until.isoformat() if base_until else None])}, build)[workspace.id]
        validation, shifts = value['validation'], value['shifts']
        checked_at = validation['evaluated_at']
    return current_fingerprint, {**validation, 'checking': checking,
        'check_status': check_job['status'] if check_job else None,
        'check_queued_at': check_job['queued_at'] if check_job else None}, shifts or [], checked_at


def _score_fingerprint(data):
    from packages.domain.input_manifest import manifest
    # Score-only cache: effective safety facts/rules are already hashed; the live staleness marker also
    # includes frozen-out department advisories. Authoritative check currency must retain that marker.
    return manifest({**data, '_live_safety_staleness': None})['token']


def workspace_validation(db, workspace):
    """Expose a current score beside a retained scoped edit check without rewriting its evidence."""
    from copy import deepcopy
    from apps.api.settings import quota_policy_limit
    from packages.domain.input_manifest import digest, toolchain
    from packages.domain.rota_summary import normalized_check
    from .read_projections import cached, CANONICAL_AUDIENCE
    validation = normalized_check(workspace.validation) or {}
    score = validation.get('score')
    if (validation.get('scope') != 'command_changes' or isinstance(score, dict)
            or not validation.get('evaluated_at')):
        return validation
    data = db.info.get('read_snapshots', {}).get(workspace.id)
    if data is None:
        data = domain_snapshot(db, workspace)
    defaults = data['generation_options']
    def build(_wid):
        complete = evaluate(data)
        return schedule_score(data, complete, defaults)
    # Summary checks own complete findings/shifts; stored checks own compact retained evidence.
    # This actor-independent score has one replaceable row per workspace, never one row per stamp/reader.
    score = cached(db, [workspace.id], 'workspace_score', {workspace.id:
        digest([_score_fingerprint(data), workspace.validation, defaults, toolchain(quota_policy_limit())])}, build,
        audience=CANONICAL_AUDIENCE)[workspace.id]
    return {**validation, 'score': deepcopy(score)}


def workspace_summary(db, actor, workspace, *, group_version_id=None):
    from .people import current_people_counts
    from .operations import worker_heartbeat_max_age_seconds, worker_state
    current_fingerprint, validation, shifts, checked_at = summary_check(db, workspace)
    people = current_people_counts(db, actor)
    summary = rota_summary(validation, shifts, workspace.accepted_issues or [])
    summary['fix_list'].update(workspace_id=workspace.id, revision=workspace.revision, fingerprint=current_fingerprint)
    summary['fix_list']['summary'].update(check_status=validation.get('check_status'),
                                          check_queued_at=validation.get('check_queued_at'))
    from .acknowledged_gaps import review_items
    summary['fix_list']['review_items'] = review_items(workspace, current=summary['fix_list'])
    from .planning_groups import fix_list_context
    summary['fix_list'] = fix_list_context(db, actor, workspace, summary['fix_list'],
        group_version_id=group_version_id, validation=validation)
    liveness = worker_state(db, m.now())
    return {'workspace_id': workspace.id, 'revision': workspace.revision, 'fingerprint': current_fingerprint,
            'as_of': m.now().isoformat(), 'checked_at': checked_at,
            'worker_liveness': liveness['worker_liveness'],
            'worker_heartbeat_max_age_seconds': worker_heartbeat_max_age_seconds(db), **summary,
            'people': {key: people[key] for key in ('active_people', 'people_without_current_rank')},
            'coverage_gaps': [gap for gap in coverage_gaps(db, workspace.department_id)
                              if workspace.id in (gap['before_workspace_id'], gap['after_workspace_id'])]}


def lean_fix_list(value):
    """Bounded reader projection over the canonical Fix-list causes."""
    from packages.contracts.schemas import FIX_ACTION_LABELS
    summary = value['summary']
    summary_keys = ('must', 'could', 'checking', 'check_status', 'check_queued_at', 'population_note', 'since', 'checked_at',
                    'places_filled', 'places_total', 'places_open', 'checks_must', 'checks_could',
                    'fairness_checks', 'acknowledged', 'submit_blocked', 'submission_issue', 'score', 'uncapped_score')

    def action(row, cause_shift_ids):
        full_scope = row.get('scope') or {}
        scope = {
            'person_ids': list(full_scope.get('person_ids') or []),
            'dates': full_scope.get('dates') or {
                'starts_on': full_scope.get('starts_on'), 'ends_on': full_scope.get('ends_on')},
            'classification': full_scope.get('classification'),
            'prefills': list(full_scope.get('prefills') or []),
        }
        shift_ids = list(full_scope.get('shift_ids') or [])
        if shift_ids != list(cause_shift_ids or []):
            scope['shift_ids'] = shift_ids
        for key in ('shift_id', 'keep_existing'):
            if key in full_scope:
                scope[key] = full_scope[key]
        return {'kind': row['kind'], 'label': FIX_ACTION_LABELS[row['kind']],
                'available': row.get('available', True), 'unavailable_reason': row.get('unavailable_reason'),
                'scope': scope}

    def attribution(cause):
        value = cause.get('attribution')
        if not value:
            return None
        return {'kind': value.get('classification'), **{key: value.get(key) for key in (
            'classification', 'group_ids', 'responsible_group_ids', 'required_group_ids', 'complete')}}

    def first_rule(cause):
        for row in cause.get('rule_lines') or ():
            if row.get('rule_id'):
                return row
        for row in cause.get('evidence') or ():
            if row.get('rule_id'):
                detail = row.get('detail') or {}
                return {'rule_id': row['rule_id'], 'line_id': row.get('line_id') or detail.get('line_id'),
                        'slot': row.get('slot') or detail.get('slot')}
        return {}

    def cause_row(cause):
        where = cause.get('where')
        session_findings = dict(cause.get('session_findings') or {})
        session_keys = set(session_findings.values())
        finding_keys = []
        seen = set()
        for key in cause.get('finding_keys') or ():
            if key in session_keys or key in seen:
                continue
            seen.add(key)
            finding_keys.append(key)
        rule = first_rule(cause)
        return {
            'id': cause['id'], 'level': cause['level'], 'sentence': cause['sentence'],
            'shift_ids': list(cause.get('shift_ids') or []), 'group': cause.get('group'),
            'action': action(cause['action'], cause.get('shift_ids') or []),
            'fixes': [action(row, cause.get('shift_ids') or []) for row in cause.get('fixes', [])],
            'safety': cause['safety'], 'acknowledgeable': cause['acknowledgeable'], 'new': cause['new'],
            'detail_line': cause.get('detail_line', ''),
            'where': {'label': where['label']} if isinstance(where, dict) and 'label' in where else None,
            'session_findings': session_findings, 'finding_keys': finding_keys,
            'person_ids': list(cause.get('person_ids') or []), 'rule_id': rule.get('rule_id'),
            'line_id': rule.get('line_id'), 'slot': rule.get('slot'), 'attribution': attribution(cause),
            'acknowledgement': cause.get('acknowledgement'),
        }

    return {**{key: value.get(key) for key in ('workspace_id', 'revision', 'fingerprint')},
            'summary': {key: summary.get(key) for key in summary_keys},
            'causes': [cause_row(cause) for cause in value['causes']],
            'review_items': value.get('review_items', []), 'planning_context': value.get('planning_context')}


def workspace_cause_summary(db, actor, workspace, *, group_version_id=None):
    """Project only the fields used by the Fix-cause panel.

    The full summary retains session rows and evidence for command/lifecycle
    callers. The cause panel needs only the bounded references below.
    """
    current_fingerprint, validation, _, _ = summary_check(db, workspace, include_shifts=False)
    projected = cause_summary(validation, workspace.accepted_issues or [])
    return {'workspace_id': workspace.id, 'revision': workspace.revision,
            'fingerprint': current_fingerprint, **projected}
