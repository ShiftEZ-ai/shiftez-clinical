"""Small SQLite/pure contract checks; no scheduler/model or full validation on context reads."""
import copy
import json
import re
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import event, null, select, update

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import job_read_metadata, job_run_context
from apps.api.services.job_reads import result_stale
from apps.api.services.rule_catalogue import attention_counts
from packages.contracts import schemas as s
from packages.domain.generation import ALGORITHM_VERSION
from packages.domain.rota_summary import requirement_references, repair_counts
from packages.domain.rule_lines import line_validity
from packages.domain.solver_rules import COMPILER_VERSION


def scope(person):
    return {'conditions': {'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': [person]}]}}


def post(h, w, endpoint, payload=None, role='coordinator', status=200):
    current = h.detail(w)['current_fingerprint']
    response = h.client(role).post(f"/api/workspaces/{w['id']}/{endpoint}",
        json={'expected_fingerprint': current, **(payload or {})})
    assert response.status_code == status, response.text
    return response.json()


def forbid_checks(monkeypatch):
    from apps.api.services import rota_summary  # Import before patching its dependency; restore the real alias.
    def forbidden(*_args, **_kwargs):
        raise AssertionError('Context reads must not run a check, score, or construct a model')
    monkeypatch.setattr('packages.domain.validation.evaluate', forbidden)
    monkeypatch.setattr('apps.api.services.rota_summary.evaluate', forbidden)
    monkeypatch.setattr('apps.api.services.rota_summary.schedule_score', forbidden)


def test_currency_has_one_classifier_and_fails_unknown_legacy_metadata():
    job = SimpleNamespace(input_fingerprint='current')
    projection = job_read_metadata({'generation_engine': 'cp_sat', 'compiler_version': COMPILER_VERSION},
                                   {'status': 'candidate_feasible', 'algorithm_version': COMPILER_VERSION})
    assert result_stale(job, projection, 'current', None) is False
    assert result_stale(job, projection, 'changed', None) is True
    bad = copy.deepcopy(projection)
    bad['result']['algorithm_version'] = 'old'
    assert result_stale(job, bad, 'current', None) is True
    del bad['result']['algorithm_version']
    assert result_stale(job, bad, 'current', None) is None
    projection['input_snapshot'].update(generation_scope=scope('person'), generation_scope_source='source')
    assert result_stale(job, projection, 'current', 'source') is None
    projection['result'].update(generation_scope=scope('person'), generation_scope_source='source')
    assert result_stale(job, projection, 'current', 'source') is False
    assert result_stale(job, projection, 'current', 'changed-source') is True


def test_run_metadata_distinguishes_requested_window_from_selected_duty_extent():
    snapshot = {'workspace': {'starts_on': '2026-12-01', 'ends_on': '2027-01-01'},
        'focus_ids': ['a', 'b'], 'generation_session_ids': ['b'], 'shifts': [
            {'id': 'a', 'start': '2026-12-01T08:00:00Z', 'end': '2026-12-01T12:00:00Z', 'service_ids': ['a']},
            {'id': 'b', 'start': '2026-12-20T08:00:00Z', 'end': '2026-12-20T12:00:00Z', 'service_ids': ['b']}]}
    context = s.JobRunContext.model_validate(job_run_context(snapshot))
    assert context.scope_window.start == date(2026, 12, 1)
    assert context.selection_window.start.day == 20 and context.service_ids == ['b']
    assert job_read_metadata(snapshot, None)['input_snapshot']['run_context']['service_ids'] == ['b']
    assert job_run_context({}) is None
    assert job_run_context({**snapshot, 'generation_session_ids': ['missing']}) is None


def test_tasks_use_independent_child_severity_and_preserve_source_ids():
    findings = [{'code': 'COVERAGE_GAP', 'severity': 'warning', 'shift_id': 'a', 'start': '2026-12-01T00:00:00Z',
        'end': '2026-12-01T01:00:00Z', 'detail': {'derived_findings': [
            {'code': 'SCOPED_COVERAGE_GAP', 'severity': 'error', 'rule_id': 'rule-a', 'detail': {'line': 0}},
            {'code': 'SCOPED_COVERAGE_GAP', 'severity': 'error', 'rule_id': 'rule-b', 'detail': {'line': 1}}]}},
        {'code': 'INFORMATION', 'severity': 'info'}]
    _, observations, tasks = requirement_references(findings, set())
    assert len({row['id'] for row in observations}) == 4
    assert len(tasks) == 1 and tasks[0]['severity'] == 'error'
    assert len(tasks[0]['observation_ids']) == 3 and repair_counts(findings)['blocking_tasks'] == 1
    assert s.RotaRepairTask.model_validate({k: v for k, v in tasks[0].items() if k != 'severity'}).severity is None


@pytest.mark.parametrize('start,end,effective', [
    ('2026-12-01', '2027-01-01', 'future'), ('2026-10-01', '2026-11-02', 'ended'),
    ('2026-11-02', None, 'active')])
def test_line_validity_is_separate_from_reach_and_uses_explicit_context(start, end, effective):
    policy = {'definition': {'rule': {'kind': 'catalogue', 'id': 'people_per_session',
        'settings': {}, 'lines': []}}, 'starts_on': '2020-01-01'}
    line = {'id': 'line', 'starts_on': start, 'ends_on': end, 'numbers': {'min': {'value': 1}}}
    assert s.RuleLineValidity.model_validate(line_validity(policy, line, date(2026, 11, 2))).effective == effective
    policy['ends_on'] = '2026-10-01'
    assert line_validity(policy, line, date(2026, 11, 2))['effective'] == 'ended'


def test_attention_counts_unique_rule_and_line_not_issue_units():
    assert attention_counts([{'rule_id': 'a', 'line_ids': ['1', '2']},
        {'rule_id': 'a', 'line_ids': ['2']}, {'rule_id': 'b', 'line_ids': []}]) == {'rules': 2, 'lines': 2}


def test_readiness_is_period_bound_incomplete_and_does_not_revalidate(h, monkeypatch):
    w = h.shift(h.workspace(), people=[])['workspace']
    before = h.detail(w)['workspace']
    forbid_checks(monkeypatch)
    value = post(h, w, 'readiness')
    assert value['checked'] and value['complete'] is False and value['groups'] == []
    assert value['scope_window'] == {'start': '2026-10-04', 'end': '2026-10-11'}
    assert value['sessions'] == 1 and value['records_coverage'] == 'assigned_people_only'
    selected = post(h, w, 'readiness', {'generation_scope': scope(h.seed['people'][0])})
    assert selected['context_fingerprint'] != value['context_fingerprint']
    assert h.detail(w)['workspace'] == before


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_context_access_uses_existing_planning_roles(h, role):
    if role not in h.seed['identities']:
        from tests.governance_support import identity
        identity(h, role, [role])
    w = h.workspace()
    allowed = role in {'coordinator', 'approver', 'programme_lead', 'auditor', 'admin'}
    # Admin inherits coordinator through the existing role hierarchy.
    post(h, w, 'readiness', role=role, status=200 if allowed else 403)


def test_evidence_binds_exact_observation_and_fingerprint_pages_without_private_fields(h, monkeypatch):
    w = h.shift(h.workspace(), people=[])['workspace']
    summary = h.get('coordinator', f"/api/workspaces/{w['id']}/summary")
    observation = summary['observations'][0]
    forbid_checks(monkeypatch)
    page = post(h, w, f"observations/{observation['id']}/evidence", {'limit': 1})
    assert page['observation'] == observation and len(page['candidates']) == 1
    assert page['has_more'] and page['complete'] is False and page['requirement_scope_complete'] is False
    assert page['records_coverage'] == 'paged_duty_candidates'
    assert not any(key in json.dumps(page) for key in ('private_reason', 'email', 'contact', 'token'))
    next_page = post(h, w, f"observations/{observation['id']}/evidence", {'offset': 1, 'limit': 1})
    assert next_page['candidates'][0]['person_id'] != page['candidates'][0]['person_id']
    post(h, w, 'observations/foreign/evidence', status=404)
    post(h, w, 'readiness', {'expected_fingerprint': '0' * 64}, status=409)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, w['id'])
        row.validation = {**row.validation, 'diagnostic_version': 'legacy'}
    assert post(h, w, 'readiness')['checked'] is False
    post(h, w, f"observations/{observation['id']}/evidence", status=409)


def test_population_and_exclusion_details_use_actual_dates_and_protected_context(h):
    from apps.api.services.planning_context import candidate, exclusion
    from packages.domain.validation import index_person_rows
    from tests.test_generation_population import scope as make_scope
    w = h.shift(h.workspace())['workspace']
    data = h.snapshot(w)
    shift = data['shifts'][0]
    first, second = h.seed['people'][:2]
    data['generation_scope'] = make_scope('person_ids', second)
    assert 'OUTSIDE_GENERATION_POPULATION' not in {r['code'] for r in candidate(data, shift, first)['exclusions']}
    shift['participants'] = []
    assert 'OUTSIDE_GENERATION_POPULATION' in {r['code'] for r in candidate(data, shift, first)['exclusions']}
    data['generation_scope'] = make_scope('category_ids', data['memberships'][0]['category_id'])
    data['memberships'] = [{**r, 'ends_on': '2026-10-04'} if r['person_id'] == first else r for r in data['memberships']]
    data['eligibilities'] = [{**r, 'ends_on': '2026-10-04'} if r['person_id'] == first else r for r in data['eligibilities']]
    data['_person_rows'] = index_person_rows(data)
    reasons = candidate(data, shift, first)['exclusions']
    assert {'MEMBERSHIP_MISSING', 'ELIGIBILITY_MISSING', 'OUTSIDE_GENERATION_POPULATION'} <= {r['code'] for r in reasons}
    actor = SimpleNamespace(department_id=h.seed['department_id'], permits=lambda *roles: 'credentialer' in roles)
    detail = exclusion(data, actor, shift, first, next(r for r in reasons if r['code'] == 'ELIGIBILITY_MISSING'))
    assert detail['code'] == 'ELIGIBILITY_ENDED' and detail['related_records']
    assert detail['fix_hints'][0]['action'] == 'open_eligibility'
    assert detail['fix_hints'][0]['prefill']['ends_on'] == '2026-10-05'
    s.PlanningExclusion.model_validate(detail)


def test_home_and_job_history_use_metadata_only_and_share_currency(h):
    w = h.shift(h.workspace(), people=[])['workspace']
    snapshot = h.snapshot(w)
    snapshot.update(generation_engine='legacy')
    with transaction(h.app.state.engine, write=True) as db:
        job = m.Job(department_id=h.seed['department_id'], workspace_id=w['id'],
            requested_by=h.seed['identities']['coordinator'], kind='generation', status='succeeded',
            input_fingerprint=snapshot['fingerprint'], input_snapshot=snapshot,
            result={'status': 'candidate_feasible', 'algorithm_version': ALGORITHM_VERSION,
                    'validation': {'valid': True}, 'summary': {'required_places': 1}, 'additions': ['retained'] * 575})
        db.add(job)
        db.flush()
        jid = job.id
    statements = []
    def capture(_conn, _cursor, sql, *_args):
        statements.append(sql)
    event.listen(h.app.state.engine, 'before_cursor_execute', capture)
    try:
        history = h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs']
        strip = h.get('coordinator', '/api/home/rotas?start=2026-10-01&end=2026-11-01')
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', capture)
    job_reads = [sql for sql in statements if 'FROM jobs' in sql]
    # History uses the stamp aggregate plus one batched metadata read; proposals and in-flight validation share the rest.
    assert len(job_reads) == 4
    assert not any(re.search(r'jobs\.(?:input_snapshot|result)\b', sql) for sql in job_reads)
    assert history[0]['result_stale'] is False and history[0]['run_context']['service_ids'] == [h.seed['services'][0]]
    assert strip['workspaces'][0]['proposal']['job_id'] == jid
    detail = h.get('coordinator', '/api/jobs/' + jid)['job']
    assert detail['result_stale'] == history[0]['result_stale']
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(update(m.Job).where(m.Job.id == jid).values(read_metadata=null()))
    history = h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs']
    assert history[0]['result_stale'] is None and history[0]['run_context'] is None
    assert history[0]['outcome'] is None
    assert 'proposal' not in h.get('coordinator', '/api/home/rotas?start=2026-10-01&end=2026-11-01')['workspaces'][0]
    assert h.get('coordinator', '/api/jobs/' + jid)['job']['run_context'] == detail['run_context']


def test_invalid_selections_and_foreign_workspace_have_no_read_access(h):
    w = h.shift(h.workspace(), people=[])['workspace']
    other = h.shift(h.workspace('Other rota'), people=[])['workspace']
    sid = h.detail(other)['shifts'][0]['id']
    assert post(h, w, 'readiness', {'session_ids': [sid]}, status=422)['error']['code'] == 'INVALID_SCOPE'
    assert post(h, w, 'readiness', {'generation_scope': scope('00000000-0000-4000-8000-000000000000')},
                status=422)['error']['code'] == 'INVALID_SCOPE'
    for tree in ({'match': 'all', 'conditions': []}, {'conditions': [{'field': 'service_ids', 'values': [h.seed['services'][0]]}]}):
        error = post(h, w, 'readiness', {'generation_scope': {'conditions': tree}}, status=422)['error']
        assert error['details'][0]['code'] == 'INVALID_SCOPE'
    post(h, w, 'readiness', {'generation_scope': {'conditions': {'conditions': [{'field': 'person_ids', 'values': []}]}}}, status=422)
    obs = h.get('coordinator', f"/api/workspaces/{other['id']}/summary")['observations'][0]['id']
    post(h, w, f'observations/{obs}/evidence', status=404)
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed['organisation_id'], name='Foreign context')
        db.add(department)
        db.flush()
        foreign = m.Workspace(department_id=department.id, name='Foreign', starts_on=date(2026, 10, 1),
            ends_on=date(2026, 10, 29), created_by=h.seed['identities']['coordinator'],
            last_edited_by=h.seed['identities']['coordinator'])
        db.add(foreign)
        db.flush()
        wid = foreign.id
    response = h.client('coordinator').post(f'/api/workspaces/{wid}/readiness', json={'expected_fingerprint': '0' * 64})
    assert response.status_code == 404


@pytest.mark.parametrize('status,algorithm,applied', [
    ('applied', ALGORITHM_VERSION, False), ('dismissed', ALGORITHM_VERSION, False),
    ('succeeded', 'old', False), ('succeeded', ALGORITHM_VERSION, True),
    ('failed', ALGORITHM_VERSION, False)])
def test_home_never_labels_applied_dismissed_or_stale_artifacts_current(h, status, algorithm, applied):
    w = h.workspace()
    data = h.snapshot(w)
    data['generation_engine'] = 'legacy'
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Job(department_id=h.seed['department_id'], workspace_id=w['id'],
            requested_by=h.seed['identities']['coordinator'], kind='generation', status=status,
            input_fingerprint=data['fingerprint'], input_snapshot=data,
            result={'status': 'candidate_feasible', 'algorithm_version': algorithm,
                    **({'applied': {'kept': 0}} if applied else {})}))
    for role in ('coordinator', 'approver', 'auditor'):
        row = h.get(role, '/api/home/rotas?start=2026-10-01&end=2026-11-01')['workspaces'][0]
        if role == 'coordinator' and status == 'succeeded' and algorithm == 'old' and not applied:
            assert row['proposal']['currency'] == 'unknown'
        else:
            assert 'proposal' not in row


def test_typed_exclusion_counts_reuse_existing_solver_evidence_without_trials(h):
    from packages.domain.solver_summary import summarise
    from packages.domain.validation import evaluate
    w = h.shift(h.workspace(), people=[])['workspace']
    source = h.snapshot(w)
    shift = source['shifts'][0]
    reasons = [{'code': 'ELIGIBILITY_MISSING', 'service_id': shift['service_ids'][0]},
               {'code': 'RESTRICTION', 'rule_id': 'limiting-rule'}]
    evidence = [{'shift_id': shift['id'], 'person_id': pid, 'exclusions': reasons + [reasons[0]]}
                for pid in h.seed['people'][:2]]
    summary = summarise(source, evaluate(source), evidence, {shift['id']})
    counts = summary['open_places'][0]['exclusion_counts']
    assert {item['code']: item['people'] for item in counts} == {'ELIGIBILITY_MISSING': 2, 'RESTRICTION': 2}
    assert all(s.SolverExclusionCount.model_validate(item) for item in counts)


def test_historical_scheduler_copy_uses_input_id_without_mutating_artifact(h):
    from apps.api.serialization import job_record
    from packages.contracts.rule_catalogue import GREEDY_NOT_APPLIED
    w = h.workspace()
    raw = {'status': 'candidate_feasible', 'not_applied': [{'input': 'fairness_targets', 'kinds': [],
            'message': 'Historical optimize_greedy by greedy'}]}
    with transaction(h.app.state.engine, write=True) as db:
        job = m.Job(department_id=h.seed['department_id'], workspace_id=w['id'],
            requested_by=h.seed['identities']['coordinator'], kind='generation', status='succeeded',
            input_fingerprint='historical', input_snapshot={'generation_engine': 'legacy'}, result=copy.deepcopy(raw))
        db.add(job)
        db.flush()
        presented = job_record(job)['result']['not_applied'][0]['message']
        assert presented == next(x['message'] for x in GREEDY_NOT_APPLIED if x['input'] == 'fairness_targets')
        assert 'optimize_greedy' not in presented and job.result == raw


def test_catalogue_projects_historical_validity_and_acknowledged_attention_once(h):
    from tests.test_rule_catalogue_api import line, propose, approve
    active = line(numbers={'max': {'value': 4}}, starts_on='2026-10-01', ends_on='2026-11-01')
    ended = line(numbers={'max': {'value': 4}}, starts_on='2026-09-01', ends_on='2026-10-01')
    future = line(numbers={'max': {'value': 4}}, starts_on='2026-12-01')
    approve(h, propose(h, 'duties_per_week', [active, ended, future])['record'])
    page = h.get('auditor', '/api/rules/catalogue')
    entry = next(item for item in page['entries'] if item['id'] == 'duties_per_week')
    assert {lid: value['effective'] for lid, value in entry['line_validity'].items()} == {
        active['id']: 'active', ended['id']: 'ended', future['id']: 'future'}
    assert page['attention_counts'] == attention_counts([item for item in page['attention'] if not item['acknowledged']])
    history = h.get('auditor', '/api/rules/catalogue?day=2026-12-02')
    entry = next(item for item in history['entries'] if item['id'] == 'duties_per_week')
    assert history['day'] == '2026-12-02' and entry['line_validity'][future['id']]['effective'] == 'active'


def test_home_proposals_are_batched_over_visible_drafts(h):
    from apps.api.services.job_reads import proposals
    from apps.api.snapshots import fingerprint
    workspaces = [h.workspace('Batch ' + str(i)) for i in range(3)]
    with transaction(h.app.state.engine, write=True) as db:
        for w in workspaces:
            workspace = db.get(m.Workspace, w['id'])
            db.add(m.Job(department_id=h.seed['department_id'], workspace_id=w['id'],
                requested_by=h.seed['identities']['coordinator'], kind='generation', status='succeeded',
                input_fingerprint=fingerprint(db, workspace),
                input_snapshot={'generation_engine': 'legacy'},
                result={'status': 'candidate_feasible', 'algorithm_version': ALGORITHM_VERSION}))
    statements = []
    def capture(_conn, _cursor, sql, *_args):
        if 'FROM jobs' in sql:
            statements.append(sql)
    event.listen(h.app.state.engine, 'before_cursor_execute', capture)
    try:
        with transaction(h.app.state.engine) as db:
            rows = list(db.scalars(select(m.Workspace).where(m.Workspace.id.in_([w['id'] for w in workspaces]))))
            actor = SimpleNamespace(department_id=h.seed['department_id'], permits=lambda *roles: 'coordinator' in roles)
            found = proposals(db, actor, rows)
            assert len(found) == 3
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', capture)
    assert len(statements) == 1 and not re.search(r'jobs\.(?:input_snapshot|result)\b', statements[0])


def test_child_requirement_does_not_get_unrelated_parent_action(h):
    w = h.shift(h.workspace(), people=[])['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        workspace = db.get(m.Workspace, w['id'])
        check = copy.deepcopy(workspace.validation)
        finding = check['findings'][0]
        finding['fix_hints'] = [{'action': 'review_assignments', 'prefill': {
            'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'rule_id': 'parent-rule'}}]
        finding['detail'] = {**(finding.get('detail') or {}), 'derived_findings': [
            {'code': 'SCOPED_COVERAGE_GAP', 'severity': 'error', 'rule_id': 'child-rule',
             'detail': {'line': 1, 'required': 3, 'available': 0}}]}
        workspace.validation = check
    summary = h.get('coordinator', f"/api/workspaces/{w['id']}/summary")
    child = next(o for o in summary['observations'] if o['rule_id'] == 'child-rule')
    page = post(h, w, f"observations/{child['id']}/evidence", {'limit': 1})
    assert page['observation']['id'] == child['id'] and page['fix_hints'] == []


def test_existing_directory_ceiling_refuses_before_snapshot_loading(h, monkeypatch):
    from apps.api.services import planning_context
    w = h.workspace()
    monkeypatch.setattr(planning_context, 'MAX_DIRECTORY_MATCHES', 0)
    def forbidden(*_args, **_kwargs):
        raise AssertionError('Oversized directory must be refused before loading the full snapshot')
    monkeypatch.setattr(planning_context, 'domain_snapshot', forbidden)
    assert post(h, w, 'readiness', status=409)['error']['code'] == 'STAFFING_DIRECTORY_LIMIT'
