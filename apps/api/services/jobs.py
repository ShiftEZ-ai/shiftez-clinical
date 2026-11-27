from .workspace_access import shared_clause
from copy import deepcopy
import secrets
from packages.contracts import schemas as s
from sqlalchemy import insert, select, update
from sqlalchemy.orm import defer
from uuid import uuid4
from apps.api import models as m
from apps.api.auth import digest, require_role
from apps.api.errors import require
from apps.api.serialization import record, job_record, job_progress_record, json_value, generation_defaults_record
from apps.api.serialization import job_result_with_reconciliation, retain_job_reconciliation
from apps.api.snapshots import (currency_hint, currency_match, domain_snapshot, fingerprint,
                                preparation_stamp, prepared_event_currency_matches,
                                publication_score_baseline, snapshot_hash, workspace_content)
from .common import scoped, expected
from .planning import draft, changed, chunks, assignment_source
from .generation_resume import (clear_generation_resume, generation_created_at, resume_current, resume_expired,
                                RESUMABLE_GENERATION_RESULTS, has_generation_checkpoint, generation_resume_available,
                                generation_resume_days, generation_admitted)
from packages.domain.generation import assignment_fixed, keep_assignment
from packages.domain.generation_scope import population_allows, selected_session_ids
from apps.api.workspace_baselines import baseline_reference, generation_manifest
from .generation_scope import require_generation_sessions_in_scope


def require_solver_compatible(snapshot):
    """Refuse a CP-SAT snapshot with one API-level compatibility error contract."""
    from packages.domain.solver_rules import compatibility_issues
    issues = compatibility_issues(snapshot)
    require(not issues, 'SOLVER_RULE_INCOMPATIBLE', 'Resolve rule settings before generating', 422, issues)


def catalogue_proof_identity():
    from .catalogue_proof_identity import identity
    return identity()


def catalogue_proof_stale(db, actor, snapshot):
    from .rule_catalogue_v2 import _inputs
    identity = catalogue_proof_identity()
    return (identity is None or snapshot.get('manifest') != _inputs(db, actor)
            or snapshot.get('toolchain') != identity
            or snapshot.get('candidate_hash') != snapshot_hash(snapshot.get('candidate') or {}))


def enqueue_catalogue_proof(db, actor, candidate):
    """Freeze one department conversion; rota snapshots are loaded only by the leased worker."""
    require_role(actor, 'coordinator', 'approver')
    require(not candidate['blocked'], 'CONVERSION_BLOCKED', 'Resolve the department conversion blockers first',
            409, candidate['blocked'])
    require(candidate['bindings'], 'NOTHING_TO_MOVE', 'No legacy sources remain to convert', 409)
    rotas = list(db.scalars(select(m.Workspace.id).where(m.Workspace.department_id == actor.department_id,
        shared_clause(),
        m.Workspace.status.in_(['draft', 'submitted', 'approved'])).order_by(m.Workspace.id)))
    require(rotas, 'NO_CURRENT_ROTA', 'A department without current rotas is checked in the preview', 409)
    toolchain = catalogue_proof_identity()
    require(toolchain is not None, 'BUILD_IDENTITY_REQUIRED', 'Restart on a stable build before checking conversion', 409)
    frozen = {key: value for key, value in candidate.items()
              if key not in {'manifest', 'proof', 'plan_hash', 'proof_job', 'proof_state', 'proof_status'}}
    candidate_hash = snapshot_hash(frozen)
    for existing in db.scalars(select(m.Job).where(m.Job.department_id == actor.department_id,
            m.Job.kind == 'catalogue_proof', m.Job.status.in_(['queued', 'running', 'succeeded']))
            .order_by(m.Job.created_at.desc(), m.Job.seq.desc())):
        snapshot = existing.input_snapshot
        if (snapshot.get('plan_hash') == candidate['plan_hash'] and snapshot.get('manifest') == candidate['manifest']
                and snapshot.get('toolchain') == toolchain and snapshot.get('candidate_hash') == candidate_hash
                and snapshot.get('candidate') == frozen):
            return job_record(existing)
    snapshot = {'candidate': frozen, 'manifest': candidate['manifest'], 'plan_hash': candidate['plan_hash'],
                'candidate_hash': candidate_hash, 'rota_ids': rotas,
                'toolchain': toolchain, 'organisation_id': actor.organisation_id}
    # The anchor satisfies existing Jobs storage/display; the evidence covers every included department rota.
    job = m.Job(department_id=actor.department_id, workspace_id=rotas[0], requested_by=actor.identity_id,
                kind='catalogue_proof', input_fingerprint=candidate['plan_hash'], input_snapshot=snapshot)
    db.add(job)
    db.flush()
    return job_record(job)


def merged(base, over):
    return {**base, **{k: merged(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v for k, v in over.items()}}


def baseline_generation_context(db, workspace):
    """Read generation configuration only from the draft's captured baseline."""
    from apps.api.workspace_baselines import frozen_department_settings, latest, verify
    row = latest(db, workspace.id)
    require(row is not None, 'BASELINE_MISSING',
            'This draft has no captured baseline; refresh or recreate it before scheduling', 409)
    included = set(row.included_sources or [])
    require({'rules', 'score', 'auto'} <= included, 'BASELINE_SCOPE_REQUIRED',
            'This draft baseline does not include rules, score and auto-schedule settings; refresh or recreate it before scheduling',
            409, {'required_sources': ['rules', 'score', 'auto'], 'included_sources': sorted(included)})
    payload = verify(row)
    department = (payload.get('departments') or {}).get(workspace.department_id)
    require(department is not None, 'BASELINE_INVALID',
            'This draft baseline has no captured department settings; refresh or recreate it before scheduling', 409)
    department = frozen_department_settings(payload, workspace.department_id, included)
    defaults = s.GenerationOptions.model_validate(generation_defaults_record(
        department.get('generation_defaults')) or {})
    return defaults, row


def generation_request_from_snapshot(workspace, snapshot):
    """Keep an implicit group selection distinct from an explicit population."""
    return snapshot.get('generation_request') or snapshot.get('_generation_request') or {
        'workspace_id': workspace.id, 'kind': 'generation',
        'session_ids': snapshot.get('generation_session_ids') or [],
        'generation_scope': snapshot.get('generation_scope'),
        'planning_group_version_id': snapshot.get('planning_group_version_id'),
        'lookahead': snapshot.get('lookahead'),
    }


def queue_generation_request(db, actor, workspace, value, settings, defaults, options, baseline, identity,
                             group_metadata=None):
    """Persist a small generation request; the worker freezes the full graph after leasing it."""
    from apps.api.input_changes import watermark
    department = db.get(m.Department, actor.department_id)
    organisation = db.get(m.Organisation, department.organisation_id)
    request = value.model_dump(mode='json', exclude_none=True)
    request_token = snapshot_hash(['generation-request', workspace.id, workspace.revision,
                                   organisation.input_revision, organisation.availability_revision,
                                   baseline.payload_hash, request, options.model_dump(mode='json')])
    snapshot = {
        'workspace': record(workspace, exclude=('validation',)),
        'generation_engine': settings.generation_engine,
        'generation_options': options.model_dump(mode='json'),
        'generation_defaults': defaults.model_dump(mode='json'),
        'baseline': baseline_reference(baseline),
        'generation_options_overridden': value.generation_options is not None,
        'diagnostic_proposal': value.diagnostic_proposal,
        **({'generation_scope': value.generation_scope.model_dump(mode='json'),
            **({'generation_scope_source': identity['source_digest']} if identity else {})}
           if value.generation_scope else {}),
        **({'generation_session_ids': value.session_ids} if value.session_ids else {}),
        'generation_request': request,
        '_deferred_generation': True,
        'fingerprint': request_token,
        'audit_watermark': watermark(db, actor.department_id),
        'currency_hint': currency_hint(db, workspace),
        'repair_unavailable': True,
        'repair_conflicts': True,
        **(group_metadata or {}),
    }
    score_baseline = publication_score_baseline(db, workspace)
    if score_baseline is not None:
        snapshot['score_baseline'] = score_baseline
    job = m.Job(department_id=actor.department_id, workspace_id=workspace.id, requested_by=actor.identity_id,
                kind='generation', input_fingerprint=request_token, input_snapshot=snapshot,
                search_control={'minutes': options.search_minutes, 'stop': False, 'spent_seconds': 0},
                created_at=generation_created_at(db, workspace.id), supersedes=[])
    if not snapshot.get('_generation_authority_pending'):
        clear_generation_resume(db, workspace_id=workspace.id)
    db.add(job)
    db.flush()
    if not snapshot.get('_generation_authority_pending'):
        record_supersessions(db, job)
    job.input_snapshot = {**job.input_snapshot,
                          'generation_manifest': generation_manifest(
                              baseline, job.input_snapshot, job=job, requester_id=actor.identity_id)}
    return {"job": job_record(job)}


def _job_request(db, actor, command):
    value = s.JobInput.model_validate(command.payload)
    require(value.kind != 'fill_options', 'INVALID_JOB_KIND',
            'Use the planner fill-options endpoint to queue candidate checks', 422)
    # Rule checks change nothing, so the approver who activates the rule may run them; scheduling stays with coordinators.
    require_role(actor, "coordinator", *(["approver"] if value.kind == "feasibility" else []))
    workspace = scoped(db, m.Workspace, value.workspace_id, actor)
    from .workspace_access import require_edit, require_visible
    (require_visible if value.kind == 'feasibility' else require_edit)(db, actor, workspace)
    expected(workspace, command.expected_revision)
    require(workspace.status == "draft" or value.kind == "feasibility", "WORKFLOW_STATE", "Jobs operate on draft snapshots", 409)
    require(workspace.archived_at is None, "WORKFLOW_STATE", "This draft was discarded; restore it first", 409)
    require(value.kind == 'validation' or not db.scalar(select(m.Job.id).where(m.Job.workspace_id == workspace.id,
        m.Job.kind == value.kind, m.Job.status.in_(["queued", "running"])).limit(1)),
        "JOB_ALREADY_RUNNING", "This draft already has a queued or running job of this kind", 409)
    require(value.kind == 'generation' or value.generation_options is None,
            'INVALID_OPTIONS', 'Scheduling options apply only to generation jobs')
    return value, workspace


def create_job(db, actor, command, settings):
    value, workspace = _job_request(db, actor, command)
    if value.kind == 'generation':
        defaults, baseline = baseline_generation_context(db, workspace)
        override = value.generation_options.model_dump(exclude_unset=True) if value.generation_options else {}
        requested = override.get('scoring') or {}
        governed = defaults.scoring.model_dump()
        # Older full payloads remain valid when they agree with current department policy.
        changed_fields = [f'scoring.{key}' for key in ('wish_budget_points', 'wish_period',
                          'wish_priority_decay', 'wish_granted_credit', 'wish_history_periods')
                          if key in requested and requested[key] != governed[key]]
        changed_fields += [f'scoring.weights.{key}' for key, amount in (requested.get('weights') or {}).items()
                           if amount != governed['weights'][key]]
        if 'max_search_minutes' in override and override['max_search_minutes'] != defaults.max_search_minutes:
            changed_fields.append('max_search_minutes')
        require(not changed_fields, 'DEPARTMENT_OPTIONS_ONLY',
                'Change department limits, score weights, preference points and renewal in Configuration, not a single run', 422,
                [{'field': 'generation_options.' + field, 'message': 'Use department Auto-schedule & scoring settings'}
                 for field in changed_fields])
        # Resolve run preferences before loading their canonical wish weights and fairness history.
        options = s.GenerationOptions.model_validate(merged(defaults.model_dump(), override))
        preset_weights = s.GENERATION_WEIGHT_PRESETS[options.weight_preset]['weights']
        if preset_weights is not None:
            options.scoring.weights = s.ScoreWeights.model_validate(preset_weights)
        require(not value.automatic, 'INVALID_OPTIONS', 'Only rota checks may run automatically')
        identity = None
        if value.generation_scope is not None:
            from .policies import condition_references
            condition_references(db, actor, value.generation_scope.conditions)
            identity = catalogue_proof_identity()
            require(identity is not None, 'BUILD_IDENTITY_REQUIRED',
                    'Restart on a stable build before scheduling a selected population', 409)
        # The earlier greedy client expressed "keep my manual edits" through the repair checkbox.
        if (settings.generation_engine == 'legacy' and 'repair_conflicts' in value.model_fields_set
                and not value.repair_conflicts and (value.generation_options is None
                    or 'mode' not in value.generation_options.model_fields_set)):
            options = options.model_copy(update={'mode': 'fill_gaps'})
            value = value.model_copy(update={'generation_options': options})
        require(not value.diagnostic_proposal or settings.generation_engine == 'cp_sat',
                'DIAGNOSTIC_ENGINE_REQUIRED', 'Diagnostic proposals require the CP-SAT scheduler')
        require_generation_sessions_in_scope(value.session_ids, db=db, workspace=workspace)
        from .planning_groups import guard_generation
        group_metadata = guard_generation(db, actor, workspace, None, value)
        if 'planning_group_version_id' in group_metadata:
            identity = identity or catalogue_proof_identity()
            require(identity is not None, 'BUILD_IDENTITY_REQUIRED',
                    'Restart on a stable build before scheduling a selected population', 409)
            group_metadata = {**group_metadata, 'generation_scope_source': identity['source_digest']}
        if value.lookahead == 'coupled':
            require(settings.generation_engine == 'cp_sat', 'LOOKAHEAD_ENGINE_REQUIRED',
                    'Joint lookahead requires the CP-SAT scheduler', 422)
        if '_generation_admission_capture' in db.info:
            return {}
        return queue_generation_request(db, actor, workspace, value, settings, defaults, options, baseline, identity,
                                        group_metadata)
    # Validation is a durable request for the worker to read the current rota.
    # Do not materialise the complete domain snapshot in the command writer: on
    # department-sized rotas that work can exceed the API health interval.
    require(not value.session_ids or value.kind == 'generation', 'INVALID_SCOPE',
            'Session selection is only supported for automatic scheduling')
    require(not value.automatic or value.kind == 'validation', 'INVALID_OPTIONS',
            'Only rota checks may run automatically')
    if value.kind == 'validation':
        from .rota_summary import current_check
        if value.automatic and current_check(db, workspace):
            return {'job': None, 'validation_current': True}
        return {'job': job_record(queue_current_validation(db, actor, workspace))}
    preparation = db.info.get('_feasibility_preparation')
    if preparation is None:
        # Direct callers can capture within their existing transaction.
        snapshot = _capture_feasibility(db, actor, workspace, value, settings)
    else:
        require(actor == preparation['actor']
                and record(workspace, exclude=('validation',)) == preparation['workspace']
                and currency_match(preparation['stamp'], preparation_stamp(db, workspace))
                and all(prepared_event_currency_matches(db, events) for events in preparation['event_currencies']),
                'STALE_INPUTS', 'The rota or its scheduling inputs changed during the check. Try again.', 409,
                {'recoverable': True})
        snapshot = preparation['snapshot'].copy()
    from apps.api.input_changes import watermark
    snapshot['audit_watermark'] = watermark(db, actor.department_id)
    snapshot['currency_hint'] = currency_hint(db, workspace)
    snapshot['repair_unavailable'] = False
    snapshot['repair_conflicts'] = False
    job = m.Job(department_id=actor.department_id, workspace_id=workspace.id, requested_by=actor.identity_id,
                kind=value.kind, input_fingerprint=snapshot['fingerprint'], input_snapshot=snapshot,
                search_control=None)
    db.add(job)
    db.flush()
    return {"job": job_record(job)}


def _capture_feasibility(db, actor, workspace, value, settings, baseline_events=None, proposed_events=None):
    operational = workspace.status == 'published'
    baseline = domain_snapshot(db, workspace, operational=operational, _event_currency=baseline_events)
    from .policies import check_policy
    from packages.domain.solver_rules import COMPILER_VERSION
    overrides, fact_overrides, bindings = [], [], []
    for change in value.rule_changes:
        policy = scoped(db, m.Policy, change.policy_id, actor)
        expected(policy, change.expected_revision)
        require(policy.status in {'proposed', 'active'}, 'WORKFLOW_STATE', 'Check a proposed or active rule')
        definition = change.definition.model_dump(mode='json') if change.definition else policy.definition
        checked = check_policy(db, actor, definition, preview=False)
        require(checked['compatible'], 'SOLVER_RULE_INCOMPATIBLE', 'Resolve rule settings before checking', 422, checked['issues'])
        candidate = {**record(policy), 'definition': definition, 'status': 'active'}
        s.PolicyInput.model_validate({key: candidate[key] for key in s.PolicyInput.model_fields})
        overrides.append(candidate)
        bindings.append({'policy_id': policy.id, 'revision': policy.revision,
            'source_hash': snapshot_hash(record(policy)), 'checked_hash': snapshot_hash(candidate),
            'temporary_change': change.definition is not None and definition != policy.definition})
    from packages.domain.rule_lines import fact_policy
    for change in value.fact_changes:
        fact = scoped(db, m.SchedulingFact, change.fact_id, actor)
        expected(fact, change.expected_revision)
        require(fact.status in {'proposed', 'active'}, 'WORKFLOW_STATE', 'Check a proposed or active scheduling fact')
        require(fact.migration_proposal_id is None, 'CONVERSION_CHILD', 'Check converted facts through the department conversion', 409)
        candidate = {**record(fact), 'status': 'active'}
        checked = check_policy(db, actor, fact_policy(candidate)['definition'], preview=False)
        require(checked['compatible'], 'SOLVER_RULE_INCOMPATIBLE', 'Resolve fact settings before checking', 422, checked['issues'])
        fact_overrides.append(candidate)
        bindings.append({'policy_id': fact.id, 'fact_id': fact.id, 'revision': fact.revision,
            'source_hash': snapshot_hash(record(fact)), 'checked_hash': snapshot_hash(candidate), 'temporary_change': False})
    if value.proposed_rule:
        candidate = value.proposed_rule.model_dump(mode='json')
        checked = check_policy(db, actor, candidate['definition'], preview=False)
        require(checked['compatible'], 'SOLVER_RULE_INCOMPATIBLE', 'Resolve rule settings before checking', 422, checked['issues'])
        overrides.append({**candidate, 'id': str(uuid4()), 'department_id': actor.department_id,
                          'revision': 1, 'kind': candidate['definition']['rule']['kind'], 'status': 'active'})
        # No persisted policy binding: this receipt cannot authorise activation.
    proposed = domain_snapshot(db, workspace, operational=operational, policy_overrides=overrides,
                               fact_overrides=fact_overrides, _event_currency=proposed_events)
    from packages.domain.input_manifest import feasibility_token
    return {'baseline': baseline, 'proposed': proposed, 'bindings': bindings,
        'fingerprint': feasibility_token(baseline), 'compiler_version': COMPILER_VERSION,
        'check_seconds': value.check_seconds or 10,
        'second_pass_seconds': max(2 * (value.check_seconds or 10), settings.feasibility_second_pass_seconds)}


def prepare_feasibility(db, actor, command, settings):
    """Capture both proof inputs from one repeatable read before entering the writer."""
    value, workspace = _job_request(db, actor, command)
    require(not value.automatic, 'INVALID_OPTIONS', 'Only rota checks may run automatically')
    stamp = preparation_stamp(db, workspace)
    baseline_events, proposed_events = [], []
    snapshot = _capture_feasibility(db, actor, workspace, value, settings, baseline_events, proposed_events)
    return {'actor': actor, 'workspace': record(workspace, exclude=('validation',)), 'stamp': stamp,
            'event_currencies': (baseline_events, proposed_events), 'snapshot': snapshot}


def queue_validation(db, actor, workspace, data):
    """The serialized command writer coalesces a check, replacing only obsolete queued/running inputs."""
    from .read_projections import input_stamp
    request_key = input_stamp(db, workspace)
    # Compare compact request currencies without loading pending snapshots.
    pending = db.execute(select(m.Job, m.Job.input_snapshot['validation_request_key'].as_string())
        .options(defer(m.Job.input_snapshot), defer(m.Job.result)).where(m.Job.workspace_id == workspace.id,
        m.Job.kind == 'validation', m.Job.status.in_(['queued', 'running']))).all()
    for job, queued_key in pending:
        if queued_key == request_key or (not data.get('validation_basis') and job.input_fingerprint == data['fingerprint']):
            return job
        job.status, job.error_code, job.completed_at = 'failed', 'STALE_INPUTS', m.now()
        job.revision += 1
    job = m.Job(department_id=actor.department_id, workspace_id=workspace.id, requested_by=actor.identity_id,
                kind='validation', input_fingerprint=data['fingerprint'], input_snapshot={**data, 'validation_request_key': request_key})
    db.add(job)
    db.flush()
    return job


def queue_current_validation(db, actor, workspace):
    """Durable intent to check current inputs, bound to this edit; no speculative complete snapshot."""
    value = record(workspace, exclude=('validation',))
    return queue_validation(db, actor, workspace, {'workspace': value, 'validation_basis': 'current',
        'fingerprint': snapshot_hash([workspace.id, workspace.content_revision])})


def queue_planner_preview(db, actor, command):
    """Queue a planner impact preview without doing the expensive mutation in the API writer."""
    from . import planner_tools
    require_role(actor, 'coordinator')
    require(command.kind in planner_tools.PREVIEW_KINDS, 'PREVIEW_UNSUPPORTED',
            'Only supported draft mutations can be previewed')
    workspace = scoped(db, m.Workspace, planner_tools.workspace_id(command), actor)
    from .workspace_access import require_edit
    require_edit(db, actor, workspace)
    expected(workspace, command.expected_revision)
    require(workspace.status == 'draft' and workspace.archived_at is None, 'WORKFLOW_STATE',
            'Impact previews operate on draft rotas', 409)
    source = planner_tools.source_fingerprint(db, actor, workspace, command)
    if command.expected_fingerprint is not None:
        require(command.expected_fingerprint == source, 'STALE_PREVIEW',
                'The rota or scheduling inputs changed. Calculate the impact again.', 409)
    key = planner_tools.command_key(command)
    pending = db.scalars(select(m.Job).where(
        m.Job.department_id == actor.department_id, m.Job.workspace_id == workspace.id,
        m.Job.kind == 'planner_preview', m.Job.status.in_(['queued', 'running']))).all()
    for existing in pending:
        snapshot = existing.input_snapshot or {}
        if (snapshot.get('planner_command_key') == key
                and snapshot.get('planner_source_fingerprint') == source):
            return {'job': job_record(existing), 'source_fingerprint': source, 'joined': True}
    snapshot = {
        'workspace': {'id': workspace.id, 'revision': workspace.revision,
                      'content_revision': workspace.content_revision, 'status': workspace.status},
        'planner_command': command.model_dump(mode='json', exclude_none=True),
        'planner_command_key': key,
        'planner_command_kind': command.kind,
        'planner_source_fingerprint': source,
        'planner_expected_revision': workspace.revision,
        'fingerprint': source,
    }
    job = m.Job(department_id=actor.department_id, workspace_id=workspace.id,
                requested_by=actor.identity_id, kind='planner_preview',
                input_fingerprint=source, input_snapshot=snapshot)
    db.add(job)
    db.flush()
    return {'job': job_record(job), 'source_fingerprint': source, 'joined': False}


_FILL_OPTIONS_CONSUMERS = '_fill_options_consumers'


def _fill_options_request_key(workspace, source, value):
    request = value.model_dump(mode='json', exclude_none=True)
    return request, snapshot_hash(['fill-options', workspace.id, workspace.revision, source, request])


def queue_fill_options(db, actor, value):
    """Queue bounded candidate validation; the HTTP request never evaluates candidate rows."""
    from . import staffing
    value = s.FillOptionsInput.model_validate(value)
    workspace, source = staffing.fill_options_job_scope(db, actor, value)
    request, key = _fill_options_request_key(workspace, source, value)
    pending = db.scalars(select(m.Job).where(
        m.Job.department_id == actor.department_id, m.Job.workspace_id == workspace.id,
        m.Job.kind == staffing.FILL_OPTIONS_JOB_KIND, m.Job.status.in_(['queued', 'running']))).all()
    for existing in pending:
        snapshot = existing.input_snapshot or {}
        if snapshot.get('fill_options_request_key') == key:
            return _attach_fill_options_consumer(db, actor, existing, source, key, joined=True)
    snapshot = {'workspace': {'id': workspace.id, 'revision': workspace.revision, 'status': workspace.status},
                'fill_options': request, 'fill_options_request_key': key, 'fingerprint': source}
    job = m.Job(department_id=actor.department_id, workspace_id=workspace.id,
                requested_by=actor.identity_id, kind=staffing.FILL_OPTIONS_JOB_KIND,
                input_fingerprint=source, input_snapshot=snapshot)
    db.add(job)
    db.flush()
    return _attach_fill_options_consumer(db, actor, job, source, key, joined=False)


def _attach_fill_options_consumer(db, actor, job, source, request_key, *, joined):
    """Record one actor/session-owned consumer on a locked durable fill-options job."""
    db.refresh(job, with_for_update=True)
    snapshot = deepcopy(job.input_snapshot or {})
    require(job.kind == 'fill_options' and job.status in {'queued', 'running'},
            'JOB_STATE', 'This fill-options request has already finished', 409)
    require(snapshot.get('fill_options_request_key') == request_key
            and job.input_fingerprint == source, 'STALE_INPUTS',
            'The fill-options request no longer matches its queued job', 409)
    token = f'{job.id}.{secrets.token_urlsafe(32)}'
    consumers = list(snapshot.get(_FILL_OPTIONS_CONSUMERS) or [])
    consumers.append({'token_hash': digest(token), 'identity_id': actor.identity_id,
                      'session_id': actor.session_id, 'released': False})
    snapshot[_FILL_OPTIONS_CONSUMERS] = consumers
    job.input_snapshot = snapshot
    job.revision += 1
    return {'job': job_record(job), 'source_fingerprint': source, 'joined': joined,
            'consumer_token': token}


def attach_fill_options(db, actor, command):
    """Attach a consumer to an existing matching queued/running fill-options request."""
    from . import staffing
    require_role(actor, 'coordinator')
    payload = command.payload.get('request') or command.payload.get('fill_options') or command.payload
    value = s.FillOptionsInput.model_validate(payload)
    workspace, source = staffing.fill_options_job_scope(db, actor, value)
    _, key = _fill_options_request_key(workspace, source, value)
    job_id = command.target_id or command.payload.get('job_id')
    if job_id:
        job = scoped(db, m.Job, job_id, actor)
        require(job.kind == 'fill_options', 'JOB_KIND', 'Consumer attachment applies only to fill-options jobs', 409)
        snapshot = job.input_snapshot or {}
        require(snapshot.get('fill_options_request_key') == key, 'STALE_INPUTS',
                'The fill-options request no longer matches its queued job', 409)
    else:
        job = next((row for row in db.scalars(select(m.Job).where(
            m.Job.department_id == actor.department_id, m.Job.workspace_id == workspace.id,
            m.Job.kind == 'fill_options', m.Job.status.in_(['queued', 'running']))).all()
                    if (row.input_snapshot or {}).get('fill_options_request_key') == key), None)
        require(job is not None, 'JOB_NOT_FOUND', 'No matching fill-options job is queued', 404)
    return _attach_fill_options_consumer(db, actor, job, source, key, joined=True)


def _cancel_fill_options_locked(job):
    job.status, job.completed_at = 'cancelled', m.now()
    job.progress = job_progress_record(job.progress)
    job.revision += 1


def release_fill_options(db, actor, command):
    """Release one consumer; only the final release cancels an unfinished job."""
    require_role(actor, 'coordinator')
    payload = command.payload or {}
    token = payload.get('token') or payload.get('consumer_token')
    target_id = command.target_id or payload.get('job_id')
    if not token and isinstance(command.target_id, str) and '.' in command.target_id:
        token, target_id = command.target_id, None
    require(isinstance(token, str) and 40 <= len(token) <= 200,
            'CONSUMER_TOKEN_INVALID', 'This fill-options consumer token is invalid', 404)
    if target_id:
        job = scoped(db, m.Job, target_id, actor)
    else:
        job_id = token.split('.', 1)[0]
        job = db.get(m.Job, job_id)
        require(job is not None and job.department_id == actor.department_id,
                'CONSUMER_TOKEN_INVALID', 'This fill-options consumer token is invalid', 404)
    db.refresh(job, with_for_update=True)
    require(job.kind == 'fill_options', 'JOB_KIND', 'Consumer release applies only to fill-options jobs', 409)
    snapshot = deepcopy(job.input_snapshot or {})
    consumers = list(snapshot.get(_FILL_OPTIONS_CONSUMERS) or [])
    match = next((entry for entry in consumers if secrets.compare_digest(entry.get('token_hash', ''), digest(token))), None)
    require(match is not None, 'CONSUMER_TOKEN_INVALID', 'This fill-options consumer token is invalid', 404)
    require(match.get('identity_id') == actor.identity_id and match.get('session_id') == actor.session_id,
            'FORBIDDEN', 'This fill-options consumer belongs to another session', 403)
    if match.get('released'):
        return {'job': job_record(job), 'released': False, 'cancelled': job.status == 'cancelled'}
    match['released'] = True
    snapshot[_FILL_OPTIONS_CONSUMERS] = consumers
    job.input_snapshot = snapshot
    active = [entry for entry in consumers if not entry.get('released')]
    cancelled = job.status in {'queued', 'running'} and not active
    if cancelled:
        _cancel_fill_options_locked(job)
    else:
        job.revision += 1
    return {'job': job_record(job), 'released': True, 'cancelled': cancelled}


# Command-shaped aliases keep the job service vocabulary parallel with cancel_job.
attach_job = attach_fill_options
release_job = release_fill_options


def control_generation(db, actor, command):
    require_role(actor, 'coordinator')
    value = (s.Decision if command.kind == 'job.stop_keep_best' else s.GenerationExtension).model_validate(command.payload)
    job = scoped(db, m.Job, command.target_id, actor)
    from .planning_groups import guard_job_control
    guard_job_control(db, actor, job)
    db.refresh(job, with_for_update=True)
    require(job.kind == 'generation' and job.input_snapshot.get('generation_engine') == 'cp_sat',
            'JOB_KIND', 'Search controls apply only to CP-SAT auto-schedule runs', 409)
    require(job.status in {'queued', 'running'}, 'JOB_STATE', 'This run has already finished; review its result', 409)
    options = s.GenerationOptions.model_validate(job.input_snapshot.get('generation_options') or {})
    control = {'minutes': options.search_minutes, 'stop': False, **(job.search_control or {})}
    if command.kind == 'job.stop_keep_best':
        control['stop'] = True
        control.setdefault('stop_requested_at', m.now().isoformat())
    else:
        require((job.progress or {}).get('stage') not in s.GENERATION_SEARCH_FINISHED_STAGES,
                'JOB_STATE', 'Search has finished; the saved result is being checked and prepared for review', 409)
        require(not control['stop'], 'JOB_STATE', 'This run is already stopping and keeping its best result', 409)
        maximum = s.GenerationOptions.model_validate(
            job.input_snapshot.get('generation_defaults') or {}).max_search_minutes
        require(control['minutes'] + value.minutes <= maximum, 'SEARCH_MINUTES_LIMIT',
                f'Total search time cannot exceed the department maximum of {maximum} minutes', 422)
        control['minutes'] += value.minutes
    job.search_control = control
    job.revision += 1
    return {'job': job_record(job)}


def continue_generation(db, actor, command, settings):
    require_role(actor, 'coordinator')
    value = s.GenerationContinue.model_validate(command.payload)
    previous = scoped(db, m.Job, command.target_id, actor)
    require_latest_run(db, previous)
    from .planning_groups import guard_generation, guard_job_control
    guard_job_control(db, actor, previous)
    checkpoint_candidate = has_generation_checkpoint(previous)
    resumable_result = (previous.result or {}).get('status') in RESUMABLE_GENERATION_RESULTS
    require(previous.kind == 'generation' and previous.status in {'succeeded', 'failed', 'cancelled'}
            and (resumable_result or checkpoint_candidate),
            'JOB_STATE', 'Continue an auto-schedule run that has a saved checkpoint', 409)
    workspace = scoped(db, m.Workspace, previous.workspace_id, actor)
    require(workspace.status == 'draft' and workspace.archived_at is None,
            'WORKFLOW_STATE', 'Continue only on the current draft rota', 409)
    require(not db.scalar(select(m.Job.id).where(m.Job.workspace_id == workspace.id,
        m.Job.kind == 'generation', m.Job.status.in_(['queued', 'running'])).limit(1)),
        'JOB_ALREADY_RUNNING', 'This draft already has a queued or running auto-schedule run', 409)
    require(resume_current(db, previous), 'JOB_STATE', 'Continue the latest auto-schedule attempt', 409)
    snapshot = deepcopy(previous.input_snapshot)
    if snapshot.get('generation_scope') is not None:
        identity = catalogue_proof_identity()
        require(identity is not None and identity['source_digest'] == snapshot.get('generation_scope_source'),
                'STALE_SCHEDULER', 'The scheduler changed; start a fresh run', 409)
    from packages.domain.solver_rules import COMPILER_VERSION
    require(snapshot.get('generation_engine') == settings.generation_engine == 'cp_sat'
            and snapshot.get('compiler_version') == COMPILER_VERSION
            and (previous.result or {}).get('algorithm_version', COMPILER_VERSION) == COMPILER_VERSION,
            'STALE_SCHEDULER', 'The scheduler changed; start a fresh run', 409)
    baseline_defaults, _ = baseline_generation_context(db, workspace)
    options = s.GenerationOptions.model_validate(snapshot.get('generation_options') or {})
    from packages.domain.solver_resume import resume_key
    current_snapshot = {**snapshot, 'generation_defaults': baseline_defaults.model_dump(mode='json')}
    require(resume_key(snapshot, options, False, False) == resume_key(current_snapshot, options, False, False),
            'STALE_INPUTS', 'The draft scheduling inputs changed; start a fresh run', 409)
    minutes = value.minutes if value.minutes is not None else options.search_minutes
    require(minutes <= baseline_defaults.max_search_minutes, 'SEARCH_MINUTES_LIMIT',
            f'Search time cannot exceed the captured maximum of {baseline_defaults.max_search_minutes} minutes', 422)
    snapshot['generation_options'] = {**options.model_dump(mode='json'),
        'max_search_minutes': baseline_defaults.max_search_minutes}
    if value.minutes is not None:
        snapshot['generation_options']['search_minutes'] = value.minutes
    if (previous.result or {}).get('status') == 'requirements_conflict' and has_generation_checkpoint(previous):
        # This is the explicit "Continue anyway" transition: retain the
        # prepared model and tell the worker to enter least-breaks mode.
        snapshot['diagnostic_proposal'] = True
        snapshot['generation_options'] = {**s.GenerationOptions.model_validate(
            snapshot.get('generation_options') or {}).model_dump(mode='json'), 'on_conflict': 'continue_flagged'}
    snapshot['generation_defaults'] = baseline_defaults.model_dump(mode='json')
    if value.minutes is not None:
        snapshot['generation_options_overridden'] = True
    previous_continuation = (snapshot.get('continuation') or {}).get('total_search_seconds', 0)
    if not isinstance(previous_continuation, (int, float)) or previous_continuation < 0:
        previous_continuation = 0
    previous_spent = max(previous_continuation, (previous.search_control or {}).get('spent_seconds', 0) or 0)
    snapshot['continuation'] = {'from_job_id': previous.id,
                                'previous_search_seconds': previous_spent,
                                'search_seconds': 0,
                                'total_search_seconds': previous_spent}
    request = generation_request_from_snapshot(workspace, snapshot)
    scope = guard_generation(db, actor, workspace, None, s.JobInput.model_validate(request),
                             retained_population=snapshot.get('planning_population'))
    require(scope.get('generation_role_bindings') == snapshot.get('generation_role_bindings'),
            'STALE_INPUTS', 'Scheduling population roles changed; start a fresh run', 409)
    if '_generation_admission_capture' in db.info:
        return {}
    snapshot['_generation_request'] = request
    if scope.get('_generation_authority_pending'):
        snapshot['_generation_authority_pending'] = True
    expired = resume_expired(previous, settings.generation_resume_days)
    job = m.Job(department_id=actor.department_id, workspace_id=workspace.id, requested_by=actor.identity_id,
        kind='generation', input_fingerprint=previous.input_fingerprint, input_snapshot=snapshot,
        resume_state=None if expired else previous.resume_state,
        resume_checkpoint=None if expired else previous.resume_checkpoint,
        search_control={'minutes': minutes, 'stop': False, 'spent_seconds': 0},
        created_at=generation_created_at(db, workspace.id), supersedes=[])
    if not snapshot.get('_generation_authority_pending'):
        clear_generation_resume(db, workspace_id=workspace.id)
    db.add(job)
    db.flush()
    if not snapshot.get('_generation_authority_pending'):
        record_supersessions(db, job)
    return {'job': job_record(job), 'preparation_expired': expired}


def cancel_job(db, actor, command):
    require_role(actor, *s.PERMISSIONS["job.cancel"])
    value = s.JobCancelInput.model_validate(command.payload)
    job = scoped(db, m.Job, command.target_id, actor)
    from .planning_groups import guard_job_control
    guard_job_control(db, actor, job)
    if value.only_if_queued:
        completed_before_cancel = False  # only a still-queued job is cancelled on this path
        expired_before_cancel = job.kind == 'generation' and resume_expired(job, generation_resume_days(db))
        completed_at = m.now()
        progress = job_progress_record(job.progress)
        changed = db.execute(update(m.Job).where(m.Job.id == job.id, m.Job.status == 'queued').values(
            status='cancelled', completed_at=completed_at, progress=progress, revision=m.Job.revision + 1)).rowcount
        db.refresh(job)
        if changed != 1:
            return {'job': job_record(job), 'cancelled': False}
    else:
        # Progress and worker claims change the revision while the confirmation is open.
        db.refresh(job, with_for_update=True)
        require(job.status in {"queued", "running"}, "JOB_STATE", "This run has already finished; review its result", 409)
        completed_before_cancel = job.completed_at is not None
        expired_before_cancel = job.kind == 'generation' and resume_expired(job, generation_resume_days(db))
        job.status, job.completed_at = "cancelled", m.now()
        job.progress = job_progress_record(job.progress)
    if job.kind == 'generation':
        if not generation_resume_available(db, job, days=generation_resume_days(db),
                                           error=None,
                                           expired=expired_before_cancel,
                                           completed_before_cancel=completed_before_cancel):
            clear_generation_resume(db, job_id=job.id)
    if job.kind == "swap_audience":
        from .swaps import store_audience
        store_audience(db, job.input_snapshot, None, "CANCELLED")
    if not value.only_if_queued:
        job.revision += 1
    return {"job": job_record(job), **({'cancelled': True} if value.only_if_queued else {})}


def apply_generation(db, actor, command):
    from .generation_reconciliation import GenerationInputsChanged, load_apply_job, proposal_guard_matches

    value = s.GenerationApply.model_validate(command.payload)
    plan = db.info.get('_generation_plan')
    require(plan is not None, 'RECONCILIATION_REQUIRED', 'Prepare this proposal before entering the writer transaction', 409)
    workspace = scoped(db, m.Workspace, value.workspace_id, actor)
    workspace = draft(db, actor, value.workspace_id, workspace.revision)
    db.refresh(workspace, with_for_update={'key_share': True, 'of': m.Workspace})
    workspace = draft(db, actor, value.workspace_id, workspace.revision)
    db.execute(select(m.Job.id).where(m.Job.id == value.job_id, m.Job.department_id == actor.department_id)
               .with_for_update())
    job, version = load_apply_job(db, actor, value.job_id)
    require(job.workspace_id == workspace.id and job.kind == "generation" and job.status == "succeeded",
            "JOB_STATE", "Select a completed generation job for this workspace", 409)
    require(plan['actor_id'] == actor.identity_id and plan['department_id'] == actor.department_id
            and plan['workspace_id'] == workspace.id, 'RECONCILIATION_CONTEXT', 'Reconcile in the current department', 409)
    receipt = plan['receipt']
    require_application_toolchain(job)
    require_latest_run(db, job)
    before = workspace_content(db, workspace)
    from apps.api.snapshots import preparation_stamp
    if plan.get('writer_current_inputs'):
        # Repeated changes retain the existing current-input reply instead of introducing a new error.
        current_data = domain_snapshot(db, workspace, own_shifts=before['shifts'])
    else:
        if not currency_match(plan['current_stamp'], preparation_stamp(db, workspace)):
            raise GenerationInputsChanged()
        current_data = plan['current_data']
    current = current_data['fingerprint']
    unchanged = (workspace.content_revision == receipt['content_revision'] and current == receipt['current_fingerprint']
                 and currency_match(plan['currency_hint'], currency_hint(db, workspace))
                 and proposal_guard_matches(job.result or {}, version, plan['result_guard']))
    if not unchanged or not (receipt['addition_indexes'] or receipt['removal_indexes']):
        if not unchanged:
            from apps.api.input_changes import changed_facts
            facts = changed_facts(db, job.input_snapshot, current_data, actor.department_id,
                                  job.input_snapshot.get('audit_watermark', 0))
            receipt = {**receipt, 'status': 'revalidation_required', 'current_revision': workspace.revision,
                       'current_fingerprint': current, 'validation_current': False,
                       'addition_indexes': [], 'removal_indexes': [],
                       'changed_facts': facts, 'changed_fact_count': len(facts)}
            receipt['id'] = snapshot_hash({k: v for k, v in receipt.items() if k != 'id'})
        retain_job_reconciliation(job, receipt, result=job_result_with_reconciliation(job, job.result))
        job.revision += 1
        return {'workspace': record(workspace, exclude=('validation',)), 'reconciliation': receipt,
                'proposal_retained': True, 'proposal_action': 'reconcile_and_apply',
                'proposal_reviewable': True}
    require(job.result and job.result["status"] in {"candidate_feasible", "candidate_with_gaps", "unknown_time_limit"},
            "NO_CANDIDATE", "This job did not produce an applicable candidate")
    require(bool(job.result.get("additions") or job.result.get('removals')), "NO_ADDITIONS", "The candidate contains no changes")
    def chosen(items, indexes):
        indexes = list(range(len(items))) if indexes is None else sorted(set(indexes))
        require(all(0 <= i < len(items) for i in indexes), "SCHEMA_INVALID", "A chosen suggestion is not part of this proposal", 422)
        return [items[i] for i in indexes], indexes
    removals, removal_indexes = chosen(job.result.get('removals', []), receipt['removal_indexes'])
    additions, addition_indexes = chosen(job.result.get('additions', []), receipt['addition_indexes'])
    require(bool(additions or removals), "NO_ADDITIONS", "Choose at least one suggestion to apply")
    removed = {r["assignment_id"] for r in removals}
    unselected_replacements = {(r["shift_id"], r["person_id"]) for r in job.result.get("removals", [])
                               if r["assignment_id"] not in removed}
    require(not any((a["shift_id"], a["participant"]["person_id"]) in unselected_replacements for a in additions),
            "INCOMPLETE_REPLACEMENT", "Select the matching removal before applying a replacement assignment", 422)
    original_shifts = {r['id']: r for r in before['shifts']}
    from packages.domain.recurrence import inactive, series_candidate_allowed
    proposed_shifts = {r['id']: r for r in job.input_snapshot['shifts']}
    shift_ids = {r['shift_id'] for r in removals} | {a['shift_id'] for a in additions}
    shifts = {row.id: row for chunk in chunks(sorted(shift_ids)) for row in db.scalars(
        select(m.Shift).where(m.Shift.workspace_id == workspace.id, m.Shift.id.in_(chunk)))}
    assignments = {row.id: row for chunk in chunks(sorted(removed)) for row in db.scalars(
        select(m.Assignment).where(m.Assignment.id.in_(chunk)))}
    for removal in removals:
        shift = shifts.get(removal['shift_id'])
        assignment = assignments.get(removal['assignment_id'])
        require(shift is not None and shift.workspace_id == workspace.id and not shift.locked
                and not shift.cancelled and not workspace.base_publication_id
                and assignment is not None and assignment.shift_id == shift.id
                and not assignment_fixed(original_shifts[shift.id], {'fixed': assignment.fixed, 'person_id': assignment.person_id}),
                'STALE_INPUTS', 'A proposed replacement is no longer editable', 409)
        db.delete(assignment)
    db.flush()
    rows = []
    for addition in additions:
        shift = shifts.get(addition["shift_id"])
        require(shift is not None and shift.workspace_id == workspace.id and not shift.locked and not shift.cancelled,
                "STALE_INPUTS", "Candidate session is no longer editable", 409)
        require(shift.id in proposed_shifts, 'STALE_INPUTS',
                'This proposal has no complete frozen inputs; run auto-schedule again', 409)
        p = addition["participant"]
        require(not inactive(proposed_shifts[shift.id])
                and series_candidate_allowed(job.input_snapshot, original_shifts[shift.id], p['person_id']),
                'STALE_INPUTS', 'This recurring session does not permit the proposed replacement', 409)
        parsed = s.ParticipantInput.model_validate({k: p[k] for k in s.ParticipantInput.model_fields if k in p})
        fields = parsed.model_dump()
        fields["segments"] = json_value(fields["segments"])
        rows.append({'id': plan['assignment_ids'][addition_indexes[len(rows)]],
                     'shift_id': shift.id, **fields, **assignment_source(actor, 'auto_schedule', job.id)})
    # One reviewed edit per changed session, including additions without a removal.
    for shift in shifts.values():
        shift.revision += 1
        if shift.series_id:
            shift.series_modified = True
    if rows:
        db.execute(insert(m.Assignment).execution_options(render_nulls=True), rows)
    job.status = 'applied'
    receipt = {**receipt, 'status': 'applied', 'population_recorded': True,
               'population_people': plan['population_contribution'], 'applied_at': m.now().isoformat()}
    retain_job_reconciliation(job, receipt, result=job.result, interim=True)
    db.info['_command_overlap_flags'] = plan['overlap_flags']
    result = changed(db, actor, workspace, before, value.reason, prepared=(plan['validation'], plan['data']),
                     generation_apply=True)
    receipt = {**receipt, 'applied_content_revision': workspace.content_revision}
    result['reconciliation'] = receipt
    job.progress = job_progress_record(job.progress)
    # The edit it made, so undoing that edit can say the proposal is no longer applied (planning.undo).
    retain_job_reconciliation(job, receipt, result={**job.result, "applied_content_revision": workspace.content_revision,
        "applied": s.GenerationApplied(addition_indexes=addition_indexes, removal_indexes=removal_indexes,
            added_count=len(additions), removed_count=len(removals)).model_dump()})
    job.revision += 1
    result['job'] = job_record(job, include_result=False, include_progress_solution=False)
    return result


def application_toolchain_error(job, *, snapshot=None, result=None):
    """Return the scheduler-staleness message, or ``None`` when application is safe to attempt."""
    from packages.domain.generation import ALGORITHM_VERSION
    from packages.domain.solver_rules import COMPILER_VERSION
    from packages.domain.input_manifest import digest, toolchain
    provided_snapshot = snapshot is not None
    snapshot = job.input_snapshot if snapshot is None else snapshot
    result = (job.result or {}) if result is None else (result or {})
    expected = COMPILER_VERSION if snapshot.get('generation_engine') == 'cp_sat' else ALGORITHM_VERSION
    saved_toolchain = (snapshot.get('input_manifest') or {}).get('components', {}).get('toolchain')
    from apps.api.settings import quota_policy_limit
    current_quota_limit = snapshot.get('quota_policy_limit')
    if current_quota_limit is None and provided_snapshot:
        # History/detail projections deliberately defer the frozen input blob.  A
        # legacy metadata row without this field is insufficient to prove the
        # scheduler is current; keep the result unknown instead of lazy-loading it.
        return 'The scheduler metadata is incomplete; generate a fresh proposal before applying'
    if current_quota_limit is None:
        current_quota_limit = quota_policy_limit()
    if not ((not result.get('algorithm_version') or result['algorithm_version'] == expected)
            and (snapshot.get('generation_engine') != 'cp_sat' or snapshot.get('compiler_version') == expected)
            and (not saved_toolchain or saved_toolchain == digest(toolchain(current_quota_limit)))):
        return 'The scheduler changed; generate a fresh proposal before applying'
    if snapshot.get('planning_group_id'):
        if 'planning_population' not in snapshot:
            snapshot = {**snapshot, 'planning_population': (job.input_snapshot or {}).get('planning_population')}
        if 'planning_population' not in result:
            result = {**result, 'planning_population': (job.result or {}).get('planning_population')}
        if not (result.get('planning_population') == snapshot.get('planning_population')
                and result.get('planning_group_version_id') == snapshot.get('planning_group_version_id')):
            return 'This worker does not support group scheduling; request a fresh run'
    if snapshot.get('generation_scope') is not None:
        identity = catalogue_proof_identity()
        if not (identity is not None and identity['source_digest'] == snapshot.get('generation_scope_source')
                and result.get('generation_scope_source') == identity['source_digest']
                and result.get('generation_scope') == snapshot['generation_scope']):
            return 'This worker does not support the selected population; run again with the matching scheduler'
    return None


def application_toolchain_current(job, *, snapshot=None, result=None):
    return application_toolchain_error(job, snapshot=snapshot, result=result) is None


def application_recheckable(snapshot, result, source_digest=None):
    """Whether a retained read projection can still be reconciled by Apply.

    History defers the frozen input/result blobs; Apply checks the full job row.
    """
    from packages.domain.generation import ALGORITHM_VERSION
    from packages.domain.solver_rules import COMPILER_VERSION
    from packages.domain.input_manifest import digest, toolchain
    snapshot, result = snapshot or {}, result or {}
    expected = COMPILER_VERSION if snapshot.get('generation_engine') == 'cp_sat' else ALGORITHM_VERSION
    if result.get('algorithm_version') and result['algorithm_version'] != expected:
        return False
    if snapshot.get('generation_engine') == 'cp_sat' and snapshot.get('compiler_version') != expected:
        return False
    saved_toolchain = (snapshot.get('input_manifest') or {}).get('components', {}).get('toolchain')
    if saved_toolchain and saved_toolchain != digest(toolchain(snapshot.get('quota_policy_limit'))):
        return False
    if snapshot.get('planning_group_id'):
        # Population evidence is deliberately absent from history's bounded projection.
        return False
    if snapshot.get('generation_scope') is None:
        return True
    if not source_digest or snapshot.get('generation_scope_source') != source_digest:
        return False
    return (result.get('generation_scope_source') == source_digest
            and result.get('generation_scope') == snapshot['generation_scope'])


LIVE_RUN_STATUSES = ('queued', 'running', 'succeeded', 'applied')


def supersedes(newer, older):
    """A newer run supersedes an older run only for the same rota, sessions and population."""
    return (newer[0] == older[0] and newer[1] > older[1] and bool(newer[2] & older[2])
            and (newer[3] is None or older[3] is None or newer[3] == older[3]))


def run_facts(db, *where, statuses=LIVE_RUN_STATUSES):
    """Read only the compact facts needed to classify generation-run supersession."""
    J, snapshot = m.Job, m.Job.input_snapshot
    rows = db.execute(select(J.id, J.workspace_id, J.created_at, snapshot['generation_session_ids'],
                             snapshot['focus_ids'], snapshot['generation_scope'], snapshot['_deferred_generation']).where(
        J.kind == 'generation', generation_admitted(J),
        *([J.status.in_(statuses)] if statuses else []), *where)).all()
    full_rota_ids = {row[1] for row in rows if row[6] and not (row[3] or row[4])}
    full_rota = {}
    if full_rota_ids:
        for workspace_id, shift_id in db.execute(select(m.Shift.workspace_id, m.Shift.id).where(
                m.Shift.workspace_id.in_(full_rota_ids))):
            full_rota.setdefault(workspace_id, set()).add(shift_id)
    return {row[0]: (row[1], row[2], set(row[3] if row[3] is not None else row[4] or
                                           full_rota.get(row[1], set())), row[5]) for row in rows}


def superseded_runs(db, jobs):
    """Return succeeded runs replaced by a newer live run in the same planning group."""
    candidates = [job for job in jobs if job.kind == 'generation' and job.status == 'succeeded']
    if not candidates:
        return set()
    runs = run_facts(db, m.Job.workspace_id.in_({job.workspace_id for job in candidates}),
                     m.Job.created_at >= min(job.created_at for job in candidates))
    return {job.id for job in candidates if job.id in runs
            and any(other != job.id and supersedes(run, runs[job.id]) for other, run in runs.items())}


def record_supersessions(db, job):
    """Record the older live runs replaced by a newly-created generation run."""
    facts = run_facts(db, m.Job.workspace_id == job.workspace_id, m.Job.created_at <= job.created_at)
    mine = facts.get(job.id)
    job.supersedes = sorted(other for other, run in facts.items()
                            if mine and other != job.id and supersedes(mine, run))
    db.flush()


def prepare_supersession_backfill(db, *, workspaces=50, rows=200):
    """Read a bounded supersession batch without opening the global writer."""
    from sqlalchemy import and_, or_

    if not 1 <= workspaces or not 1 <= rows:
        raise ValueError('supersession backfill limits must be positive')
    J = m.Job
    pending = (J.kind == 'generation', J.supersedes.is_(None))
    selected = db.execute(select(J.id, J.workspace_id, J.created_at, J.input_fingerprint, J.status)
                          .where(*pending)
                          .order_by(J.workspace_id, J.created_at, J.id).limit(rows)).all()
    if not selected:
        return [], False
    rota_ids = []
    for row in selected:
        if row.workspace_id not in rota_ids:
            if len(rota_ids) == workspaces:
                break
            rota_ids.append(row.workspace_id)
    selected = [row for row in selected if row.workspace_id in rota_ids]
    selected_ids = [row.id for row in selected]
    unrecorded = run_facts(db, J.id.in_(selected_ids), statuses=None)
    latest = {}
    for row in selected:
        latest[row.workspace_id] = max(latest.get(row.workspace_id, row.created_at), row.created_at)
    live_where = or_(*(and_(J.workspace_id == workspace_id, J.created_at <= created_at)
                       for workspace_id, created_at in latest.items()))
    live = run_facts(db, live_where)
    records = []
    for row in selected:
        mine = unrecorded.get(row.id)
        if mine is None:
            continue
        records.append({
            'job_id': row.id,
            'value': sorted(other for other, run in live.items()
                            if other != row.id and supersedes(mine, run)),
            'created_at': row.created_at,
            'input_fingerprint': row.input_fingerprint,
            'status': row.status,
        })
    return records, True


def apply_supersession_backfill(db, records):
    """Write only rows that are still the same unfilled records read by the worker."""
    if not records:
        return 0
    from sqlalchemy import bindparam, update
    table = m.Job.__table__
    result = db.execute(update(table).where(
        table.c.id == bindparam('job_id'),
        table.c.kind == 'generation',
        table.c.supersedes.is_(None),
        table.c.created_at == bindparam('expected_created_at'),
        table.c.input_fingerprint.is_not_distinct_from(bindparam('expected_input_fingerprint')),
        table.c.status == bindparam('expected_status'),
    ).values(supersedes=bindparam('value', type_=table.c.supersedes.type)), [
        {
            'job_id': row['job_id'],
            'value': row['value'],
            'expected_created_at': row['created_at'],
            'expected_input_fingerprint': row['input_fingerprint'],
            'expected_status': row['status'],
        } for row in records])
    return max(0, result.rowcount or 0)


def backfill_supersessions(db, workspaces=50, rows=200):
    """Compatibility helper for callers that already own a transaction."""
    records, _ = prepare_supersession_backfill(db, workspaces=workspaces, rows=rows)
    return apply_supersession_backfill(db, records)


def superseded_flag(db, outer):
    """Build the one-statement predicate used by list, detail and Home reads."""
    from sqlalchemy import and_, cast, exists, func, literal, true
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.orm import aliased
    newer = aliased(m.Job, name='newer_runs')
    live = and_(newer.workspace_id == outer.workspace_id, newer.kind == 'generation',
                newer.status.in_(LIVE_RUN_STATUSES), newer.created_at > outer.created_at)
    # A NULL supersedes value is an in-progress legacy fill.  Fail closed while it
    # is unfilled so an older run never appears current during the read/write gap.
    unfilled = exists(select(literal(1)).select_from(newer).where(live, newer.supersedes.is_(None)))
    if db.get_bind().dialect.name == 'postgresql':
        recorded = exists(select(literal(1)).select_from(newer).where(
            live, cast(newer.supersedes, JSONB).has_key(outer.id)))
        return recorded | unfilled
    listed = func.json_each(newer.supersedes).table_valued('value')
    recorded = exists(select(literal(1)).select_from(newer).join(listed, true()).where(
        live, listed.c.value == outer.id))
    return recorded | unfilled


def require_latest_run(db, job):
    require(job.id not in superseded_runs(db, [job]), s.RUN_SUPERSEDED,
            'A newer auto-schedule run exists for this rota; review and apply that one', 409)


def require_application_toolchain(job):
    require(isinstance(job.input_snapshot.get('shifts'), list)
            and isinstance(job.input_snapshot.get('focus_ids'), list)
            and isinstance(job.input_snapshot.get('people'), dict),
            'STALE_INPUTS', 'This proposal has no complete frozen inputs; run auto-schedule again', 409)
    error = application_toolchain_error(job)
    require(error is None, 'STALE_SCHEDULER',
            error or 'The scheduler changed; generate a fresh proposal before applying', 409)

    if job.input_snapshot.get('generation_scope') is not None:
        selected = set(selected_session_ids(job.input_snapshot))
        source = {r['id']: r for r in job.input_snapshot['shifts']}
        original = {p['id']: (shift, p) for shift in source.values() for p in shift['participants']}
        for removal in job.result.get('removals', []):
            shift, participant = original.get(removal.get('assignment_id'), (None, None))
            require(shift is not None and shift['id'] == removal.get('shift_id') and shift['id'] in selected
                    and not shift['locked'] and not shift.get('cancelled')
                    and not job.input_snapshot['workspace'].get('base_publication_id')
                    and not keep_assignment(job.input_snapshot, shift, participant),
                    'PROTECTED_ASSIGNMENT', 'The proposal changes protected work; generate a fresh candidate', 409)
        proposed_removed = {r.get('assignment_id') for r in job.result.get('removals', [])}
        for addition in job.result.get('additions', []):
            shift = source.get(addition.get('shift_id'))
            participant = s.ParticipantInput.model_validate({k: v for k, v in (addition.get('participant') or {}).items()
                                                           if k in s.ParticipantInput.model_fields})
            require(shift is not None and shift['id'] in selected and not shift['locked'] and not shift.get('cancelled')
                    and population_allows(job.input_snapshot, shift, participant.person_id, participant.model_dump(mode='json')),
                    'INVALID_SCOPE', 'A proposed assignment is outside the selected population or duties; generate a fresh candidate', 409)
            require(all(p['person_id'] != participant.person_id or p['id'] in proposed_removed
                        for p in shift['participants']), 'PROTECTED_ASSIGNMENT',
                    'The proposal overwrites an existing assignment; generate a fresh candidate', 409)


def dismiss_job(db, actor, command):
    require_role(actor, 'coordinator')
    s.Decision.model_validate(command.payload)
    job = scoped(db, m.Job, command.target_id, actor)
    db.refresh(job, with_for_update=True)
    expected(job, command.expected_revision)
    require(job.kind == 'generation' and job.status == 'succeeded',
            'JOB_STATE', 'Only a completed, unapplied proposal can be discarded', 409)
    job.status = 'dismissed'
    clear_generation_resume(db, job_id=job.id)
    job.revision += 1
    return {'job': job_record(job)}
