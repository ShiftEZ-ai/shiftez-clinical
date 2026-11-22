"""Explicit units, current read-only API summaries and gaps across paginated rota lists."""
from datetime import date
from types import SimpleNamespace
import copy
import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.planning import health
from packages.domain.rota_summary import rota_summary, rota_coverage_gaps, suggested_rota_name
from packages.domain.validation import evaluate
from packages.domain.score import schedule_score
from packages.contracts.schemas import ScheduleScore, ROLE_NAMES


def test_places_count_once_per_session_across_segments_and_skill_gaps():
    validation = {'valid': False, 'coverage': [
        {'shift_id': 'one', 'required': 2, 'available_eligible': 2, 'status': 'covered'},
        {'shift_id': 'one', 'required': 1, 'available_eligible': 0, 'status': 'gap'},
        {'shift_id': 'two', 'required': 3, 'available_eligible': 5, 'status': 'gap'},
        {'shift_id': 'not-staffed', 'required': 0, 'available_eligible': 0, 'status': 'not_staffed'},
        {'shift_id': 'cancelled', 'required': 10, 'available_eligible': 0, 'status': 'cancelled'}],
        'findings': [{'severity': 'error', 'code': 'COVERAGE_GAP'}, {'severity': 'warning', 'code': 'COVERAGE_TARGET'},
                     {'severity': 'info', 'code': 'INFORMATION'}]}
    summary = rota_summary(validation)
    assert (summary['places_needed'], summary['places_filled'], summary['places_open']) == (5, 4, 1)
    assert summary['sessions_with_gaps'] == 2
    assert summary['issues'] == {'blocking': 1, 'advisory': 1, 'blocking_without_open_places': 1}
    assert summary['places_filled'] + summary['places_open'] == summary['places_needed']
    assert health(SimpleNamespace(validation=validation))['staffed'] == 2
    assert health(SimpleNamespace(validation=validation))['open_places'] == summary['places_open'] == 1
    assert rota_summary(None)['checked'] is False


def test_summary_retains_issue_keys_acceptance_and_normalises_historical_score():
    f = {'code': 'REST', 'severity': 'warning', 'rule_id': 'rule', 'shift_id': 'session',
         'person_id': 'person', 'start': '2026-10-04T00:00:00Z'}
    key = 'REST|rule|session|person|2026-10-04T00:00:00Z'
    summary = rota_summary({'findings': [f], 'coverage': [{'shift_id': 'session', 'status': 'covered'}],
                            'score': {'score': 59, 'raw': 90, 'capped': 31}}, accepted_issues={key})
    assert summary['findings'][0]['key'] == key and summary['findings'][0]['accepted']
    assert summary['score']['uncapped_score'] == 90 and summary['score']['capped'] is True


def test_gaps_use_union_instead_of_adjacent_overlapping_or_nested_rows():
    def rota(i, start, end):
        return {'id': i, 'starts_on': start, 'ends_on': end}
    rows = [rota('outer', '2026-09-01', '2026-09-29'), rota('nested', '2026-09-05', '2026-09-10'),
            rota('next', '2026-10-01', '2026-10-29')]
    assert rota_coverage_gaps(rows) == [{'starts_on': '2026-09-29', 'ends_on': '2026-10-01', 'days': 2,
        'before_workspace_id': 'outer', 'after_workspace_id': 'next', 'suggested_name': 'Rota 29–30 Sep 2026'}]
    assert rota_coverage_gaps(rows + [rota('bridge', '2026-09-25', '2026-10-02')]) == []
    assert suggested_rota_name(date(2026, 9, 29), date(2026, 10, 2)) == 'Rota 29 Sep 2026 – 1 Oct 2026'


@pytest.mark.parametrize('start,end,name', [
    ('2026-10-03', '2026-10-04', 'Rota 3 Oct 2026'),
    ('2026-10-03', '2026-10-10', 'Rota 3–9 Oct 2026'),
    ('2026-12-31', '2027-01-03', 'Rota 31 Dec 2026 – 2 Jan 2027'),
])
def test_suggested_rota_names_use_unpadded_days_with_exact_inclusive_end(start, end, name):
    assert suggested_rota_name(start, end) == name


def test_summary_api_is_current_scoped_and_read_only(h):
    w = h.shift(h.workspace(), people=[])['workspace']
    stored = h.detail(w)['workspace']['validation']
    summary = h.get('coordinator', f"/api/workspaces/{w['id']}/summary")
    expected = rota_summary(stored)
    for field in ('places_needed', 'places_filled', 'places_open', 'sessions_with_gaps', 'issues'):
        assert summary[field] == expected[field]
    assert summary['sessions'][0]['person_ids'] == []
    for field in ('contributions', 'coverage_fraction', 'rule_weights'):
        assert summary['score'][field] == stored['score'][field]
    assert summary['people'] == h.get('coordinator', '/api/people/summary')
    h.get('staff', f"/api/workspaces/{w['id']}/summary", status=403)
    # Inputs change without editing or validating the rota: GET evaluates now but never replaces the stored check.
    h.policy({'kind': 'minimum_cover', 'minimum': 2})
    assert h.get('coordinator', f"/api/workspaces/{w['id']}/summary")['issues']['blocking'] > summary['issues']['blocking']
    attention = {item['key']: item for item in h.get('coordinator', '/api/home')['attention']}
    assert 'unchecked' in attention and 'ready_to_submit' not in attention and 'blocking' not in attention
    assert h.detail(w)['workspace']['validation'] == stored
    assert h.detail(w)['workspace']['revision'] == w['revision']


def test_gap_and_previous_source_metadata_are_independent_of_list_page(h):
    before = h.workspace('September', '2026-09-01', '2026-09-29')
    after = h.workspace('October', '2026-10-01', '2026-10-29')
    future = h.workspace('Future', '2026-12-01', '2026-12-29')
    value = h.get('coordinator', '/api/workspaces?limit=1&copy_starts_on=2026-10-01')
    assert value['previous_workspace_id'] == before['id']
    assert any(g['days'] == 2 and g['after_workspace_id'] == after['id'] for g in value['coverage_gaps'])
    gaps = h.get('coordinator', f"/api/workspaces/{after['id']}/summary")['coverage_gaps']
    assert gaps[0]['suggested_name'] == 'Rota 29–30 Sep 2026'
    h.command('coordinator', 'workspace.archive', {'reason': 'Synthetic archive'}, future['id'], future['revision'])
    assert len(h.get('coordinator', '/api/workspaces')['coverage_gaps']) == 1


def test_summary_cannot_read_a_foreign_department_workspace(h):
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed['organisation_id'], name='Other')
        db.add(department)
        db.flush()
        workspace = m.Workspace(department_id=department.id, name='Foreign', starts_on=date(2026, 10, 1),
            ends_on=date(2026, 10, 29), created_by=h.seed['identities']['coordinator'],
            last_edited_by=h.seed['identities']['coordinator'])
        db.add(workspace)
        db.flush()
        wid = workspace.id
    h.get('coordinator', '/api/workspaces/' + wid + '/summary', status=404)


@pytest.mark.parametrize('minimum,people,expected', [(1, 1, 1), (2, 1, 2), (2, 2, 2)])
def test_leave_groups_only_the_shortage_it_causes_and_preserves_cover(h, minimum, people, expected):
    typ = h.configure('assignment_types', {'name': 'Grouped cover', 'starts_on': '2020-01-01',
                    'definition': {'requirements': [{'minimum': minimum}]}})['record']['id']
    w = h.shift(h.workspace(), typ=typ, people=h.seed['people'][:people])['workspace']
    data = h.snapshot(w)
    start, end = ('05:00:00', '09:00:00') if minimum > people else ('06:00:00', '08:00:00')
    data['availability'] = [{'id': f'leave-{pid}', 'person_id': pid, 'kind': 'recorded_leave',
                            'start': f'2026-10-04T{start}Z', 'end': f'2026-10-04T{end}Z'}
                           for pid in h.seed['people'][:people]]
    source = copy.deepcopy(data)
    check = evaluate(data)
    assert data == source
    assert check['error_count'] == expected and not check['valid']
    assert sum(f['code'] == 'COVERAGE_GAP' for f in check['findings']) == (minimum > people)
    middle = next(row for row in check['coverage'] if row['start'] == f'2026-10-04T{start}+00:00')
    assert middle['required'] == minimum and middle['available_eligible'] == 0 and middle['status'] == 'gap'
    summary = rota_summary(check)
    assert summary['issues']['blocking'] == expected and summary['places_open'] == minimum
    assert summary['sessions_with_gaps'] == 1
    if minimum <= people:
        derived = [f for root in check['findings'] for f in (root.get('detail') or {}).get('derived_findings', [])]
        assert len(derived) == 1 and derived[0]['code'] == 'COVERAGE_GAP'
        assert derived[0]['start'] == middle['start'] and derived[0]['detail']['required'] == minimum


def test_summary_groups_historical_findings_without_mutating_evidence(h):
    from packages.domain.rota_summary import grouped_findings
    w = h.shift(h.workspace())['workspace']
    data = h.snapshot(w)
    data['availability'] = [{'id': 'leave', 'person_id': h.seed['people'][0], 'kind': 'recorded_leave',
                            'start': '2026-10-04T05:00:00Z', 'end': '2026-10-04T09:00:00Z'}]
    current = evaluate(data)
    root = current['findings'][0]
    gap = root['detail']['derived_findings'][0]
    historical = {**current, 'error_count': 2, 'findings': [{**root, 'detail': None}, gap]}
    before = copy.deepcopy(historical)
    summary = rota_summary(historical)
    assert summary['issues']['blocking'] == len(summary['findings']) == 1
    assert historical == before
    assert grouped_findings(current['findings'], current['coverage']) == current['findings']


def test_three_leave_problems_agree_across_api_health_home_and_resolution(h):
    from apps.api.services.issues import issue_key
    w = h.workspace('Three leave problems')
    for day in ('04', '05', '06'):
        w = h.shift(w, f'2026-10-{day}T08:00:00+03:00', f'2026-10-{day}T12:00:00+03:00')['workspace']
    h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0], 'kind': 'recorded_leave',
              'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-07T00:00:00+03:00', 'private_reason': 'Private'})
    w = h.lifecycle(w, 'validate')['workspace']
    check = w['validation']
    assert check['error_count'] == check['score']['blocking_count'] == len(check['findings']) == 3
    assert {f['code'] for f in check['findings']} == {'UNAVAILABLE'}
    summary = h.get('coordinator', f"/api/workspaces/{w['id']}/summary")
    assert summary['issues']['blocking'] == summary['sessions_with_gaps'] == summary['places_open'] == 3
    listed = next(row for row in h.get('coordinator', '/api/workspaces')['workspaces'] if row['id'] == w['id'])
    assert listed['health']['errors'] == 3
    blocking = next(row for row in h.get('coordinator', '/api/home')['attention'] if row['key'] == 'blocking')
    assert blocking['count'] == summary['fix_list']['summary']['must'] == 1
    assert h.lifecycle(w, 'submit', status=422)['error']['code'] == 'VALIDATION_FAILED'
    keys = [issue_key(f) for f in check['findings']]
    preview = h.client('coordinator').post('/api/planner/issues/replace-preview',
                 json={'workspace_id': w['id'], 'expected_revision': w['revision'], 'keys': keys})
    assert preview.status_code == 200 and preview.json()['replaceable'] == 3
    done = h.command('coordinator', 'issues.resolve', {'workspace_id': w['id'], 'action': 'replace', 'keys': keys,
                     'reason': 'Replace unavailable staff', 'plan_hash': preview.json()['plan_hash']}, revision=w['revision'])
    assert done['validation']['error_count'] == done['health']['errors'] == 0
    assert set(done['issues']['removed']) == set(keys)
    undone = h.command('coordinator', 'workspace.undo', {'reason': 'Restore assignments'},
                       done['workspace']['id'], done['workspace']['revision'])
    assert undone['validation']['error_count'] == undone['health']['errors'] == 3


@pytest.mark.parametrize('cancelled', [False, True])
def test_empty_active_rota_has_no_score_and_empty_draft_cannot_submit(h, cancelled):
    w = h.workspace('Empty rota')
    if cancelled:
        result = h.shift(w)
        w = h.command('coordinator', 'shift.action', {'workspace_id': w['id'], 'shift_ids': [result['shift_id']],
                      'action': 'cancel', 'reason': 'No active sessions'}, revision=result['workspace']['revision'])['workspace']
    data = h.snapshot(w)
    # Another rota's sessions never give this empty focus a score.
    other = h.shift(h.workspace('Other rota'))['workspace']
    data['shifts'] += h.snapshot(other)['shifts']
    scored = schedule_score(data, evaluate(data))
    assert scored['score'] is scored['raw'] is scored['uncapped_score'] is None
    assert not scored['publishable'] and scored['gate']['code'] == 'EMPTY_ROTA'
    assert scored['coverage_fraction'] is None and not scored['why'] and not scored['people']
    ScheduleScore.model_validate(scored)
    stored = h.detail(w)['workspace']['validation']['score']
    assert stored['score'] is None and not stored['publishable']
    summary = h.get('coordinator', f"/api/workspaces/{w['id']}/summary")
    assert summary['checked'] and summary['valid'] is False and summary['score'] is None
    assert summary['sessions'] == [] and summary['issues']['blocking'] == 0
    if not cancelled:
        assert h.lifecycle(w, 'submit', status=422)['error']['code'] == 'EMPTY_WORKSPACE'


def test_empty_historical_summary_does_not_report_a_perfect_score():
    summary = rota_summary({'valid': True, 'coverage': [], 'findings': [], 'score': {'score': 100, 'raw': 100}})
    assert summary['checked'] and summary['valid'] is False and summary['score'] is None


@pytest.mark.parametrize('minimum,independent', [(1,0),(3,0)])
def test_matching_cover_intervals_link_requirements_without_counting_them_as_seats(h, minimum, independent):
    from packages.domain.validation import finding_observations
    w = h.shift(h.workspace(), people=[])['workspace']
    data = h.snapshot(w)
    data['policies'] = []
    from .test_finding_causes import policy
    policy(data, {'kind': 'minimum_cover', 'minimum': minimum}, name='matching-cover')
    check = evaluate(data)
    assert {f['code'] for f in check['findings']} == {'COVERAGE_GAP', 'SCOPED_COVERAGE_GAP'}
    summary = rota_summary(check)
    assert summary['issues'] == {'blocking': 2, 'advisory': 0, 'blocking_without_open_places': independent}
    # Scoped minima now contribute to the same open-place count as the base minimum.
    assert summary['places_open'] == minimum
    [task] = summary['repair_tasks']
    assert set(task['finding_keys']) == {f['key'] for f in summary['findings']}
    assert set(task['observation_ids']) == {o['id'] for o in summary['observations']}
    assert len(summary['observations']) == len(list(finding_observations(check))) == 2
    assert summary['repair_counts'] == {'total_tasks': 1, 'blocking_tasks': 1, 'advisory_tasks': 0, 'observations': 2}


def test_cover_links_preserve_distinct_intervals_pools_skills_and_child_severity():
    start, middle, end = '2026-10-04T05:00:00Z', '2026-10-04T07:00:00Z', '2026-10-04T09:00:00Z'
    def f(code, **kw):
        return {'code': code, 'severity': 'error', 'shift_id': 's', 'start': start, 'end': middle,
                'detail': {'required': 1, 'available': 0}, **kw}
    children = [f('SCOPED_COVERAGE_GAP', rule_id='r', end=end,
                 detail={'required': 1, 'available': 0, 'line': 'long', 'slot': 'minimum'}),
                f('SCOPED_COVERAGE_GAP', severity='warning', rule_id='r',
                 detail={'required': 1, 'available': 0, 'line': 'advisory', 'slot': 'target'})]
    check = {'valid': False, 'coverage': [{'shift_id': 's', 'start': start, 'end': middle,
              'status': 'gap', 'required': 1, 'available_eligible': 0},
             {'shift_id': 's', 'start': middle, 'end': end, 'status': 'covered', 'required': 1, 'available_eligible': 1}],
             'findings': [f('COVERAGE_GAP'), f('SCOPED_COVERAGE_GAP', rule_id='r',
                detail={'required': 1, 'available': 0, 'line': 'short', 'slot': 'minimum', 'derived_findings': children}),
                f('CAPABILITY_GAP'), f('SUPERVISION_GAP'), f('LEAD_GAP'),
                f('SCOPED_COVERAGE_GAP', rule_id='pooled', detail={'required': 1, 'available': 0, 'affected_shift_ids': ['s','peer']})]}
    before = copy.deepcopy(check)
    summary = rota_summary(check)
    # The longer mandatory child and pooled contract cannot be subtracted using a different open interval.
    assert summary['issues']['blocking_without_open_places'] == 5
    assert len(summary['observations']) == 8
    assert len({o['id'] for o in summary['observations']}) == 8
    assert {o['severity'] for o in summary['observations'] if o['rule_id'] == 'r'} == {'error', 'warning'}
    assert summary['repair_counts']['total_tasks'] == len(summary['repair_tasks'])
    assert summary['repair_counts']['observations'] == len(summary['observations'])
    assert check == before
    assert rota_summary(check)['observations'] == summary['observations']


def test_compact_summary_skips_reference_graph_construction(h, monkeypatch):
    from packages.domain import rota_summary as module
    data = h.snapshot(h.shift(h.workspace(), people=[])['workspace'])
    check = evaluate(data)
    full = module.rota_summary(check)
    def forbidden(*args, **kwargs):
        pytest.fail('Compact health must not construct reference/cause graphs')
    monkeypatch.setattr(module, 'cause_groups', forbidden)
    monkeypatch.setattr(module, 'requirement_references', forbidden)
    compact = module.rota_summary(check, compact=True)
    assert compact == {k: v for k, v in full.items() if k not in {'sessions','findings','cause_groups','observations','repair_tasks'}}
    short = health(SimpleNamespace(validation=check))
    assert short['summary']['issues'] == full['issues']
    assert short['summary']['repair_counts'] == full['repair_counts']
    strip = h.get('coordinator', '/api/home/rotas?start=2026-10-01&end=2026-11-01')['workspaces'][0]
    assert strip['health']['summary']['repair_counts'] == full['repair_counts']
    attention = next(row for row in h.get('coordinator', '/api/home')['attention'] if row.get('rota_id'))
    assert attention['fix_list']['summary']['must'] == full['fix_list']['summary']['must']
    assert not {'cause_groups','findings','observations','repair_tasks','sessions'} & short['summary'].keys()


def test_fresh_saved_summary_reuses_check_and_score_without_full_snapshot(h, monkeypatch):
    from apps.api.services import rota_summary as service
    w = h.shift(h.workspace(), people=[])['workspace']
    stored = h.detail(w)['workspace']['validation']
    def forbidden(*args, **kwargs):
        pytest.fail('Fresh complete check must not build a full snapshot, evaluate or score')
    monkeypatch.setattr(service, 'domain_snapshot', forbidden)
    from apps.api import snapshots
    monkeypatch.setattr(snapshots, 'domain_snapshot', forbidden)
    monkeypatch.setattr(service, 'evaluate', forbidden)
    monkeypatch.setattr(service, 'schedule_score', forbidden)
    summary = h.get('coordinator', f"/api/workspaces/{w['id']}/summary")
    from packages.domain.time import aware
    assert aware(summary['checked_at']) == aware(stored['evaluated_at'])
    assert summary['fingerprint'] == stored['fingerprint']
    assert summary['score'] == stored['score']
    assert summary['sessions'][0]['person_ids'] == []
    assert h.detail(w)['workspace']['validation'] == stored


@pytest.mark.parametrize('change', ['fingerprint','validator','diagnostic','score_version','score_settings','score_dependencies','missing_score','missing_findings','missing_coverage','empty_coverage','contracts','quota_incomplete','balance_options','scope','published_scope'])
def test_summary_rechecks_stale_legacy_and_incomplete_checks_read_only(h, monkeypatch, change):
    from apps.api.services import rota_summary as service
    w = h.shift(h.workspace(), people=[])['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, w['id'])
        check = copy.deepcopy(row.validation)
        if change in {'fingerprint','validator','diagnostic'}:
            check[{'fingerprint':'fingerprint','validator':'engine_version','diagnostic':'diagnostic_version'}[change]] = 'old'
        elif change == 'score_version': check['score']['version'] = 'old'
        elif change == 'score_settings': check['score']['settings']['desired_cover_factor'] = 0.123
        elif change == 'score_dependencies': check['score_dependencies'] = {}
        elif change == 'contracts': check['contracts'] = {}
        elif change == 'quota_incomplete': check['quota_report'] = {'complete': False}
        elif change == 'empty_coverage': check['coverage'] = []
        elif change == 'balance_options':
            # An older check may have used the live options instead of the draft's.
            check['score_dependencies']['options']['balance_measure'] = 'duties'
        elif change.startswith('missing_'): check.pop(change.removeprefix('missing_'))
        else: check['scope'] = 'published_changes' if change == 'published_scope' else 'swap'
        row.validation = check
    calls, original = [], service.evaluate
    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(service, 'evaluate', counted)
    summary = h.get('coordinator', f"/api/workspaces/{w['id']}/summary")
    assert calls == [1]
    assert summary['issues']['blocking'] == 1
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Workspace, w['id']).validation == check


@pytest.mark.parametrize('role', ROLE_NAMES)
def test_summary_observation_contract_keeps_existing_role_and_privacy_scope(h, role):
    from .governance_support import identity
    identity(h, 'summary-reader', [role])
    w = h.shift(h.workspace(), people=[])['workspace']
    allowed = role in {'admin','coordinator','approver','programme_lead','auditor'}
    summary = h.get('summary-reader', f"/api/workspaces/{w['id']}/summary", status=200 if allowed else 403)
    if allowed:
        assert summary['observations'] and summary['repair_tasks']
        assert all(set(o) == {'id','finding_key','code','severity','shift_id','person_id','rule_id','start','end','line','slot'} for o in summary['observations'])
        assert 'private_reason' not in str(summary)


def test_list_fingerprints_hold_shared_rows_across_workspaces(h):
    from sqlalchemy import event
    for n in range(3):
        h.shift(h.workspace('List ' + str(n)), people=[])
    queries = []
    def counted(connection, cursor, statement, *args):
        if 'FROM departments ' in statement or 'FROM organisations ' in statement:
            queries.append(statement)
    event.listen(h.app.state.engine, 'before_cursor_execute', counted)
    try:
        h.get('coordinator', '/api/workspaces?limit=1')
        one = list(queries)
        queries.clear()
        rows = h.get('coordinator', '/api/workspaces?limit=3')['workspaces']
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', counted)
    assert len(rows) == 3 and all(row['health']['fresh'] for row in rows)
    assert len(queries) == len(one)  # Shared fingerprint reads do not grow once per workspace.


def test_issued_counts_carry_the_fix_lists_causes(h):
    """SX-13: a published rota's footer counts what its Fix list counts (causes), not only its stored checks."""
    w = h.shift(h.workspace())['workspace']
    data = h.snapshot(w)
    data['availability'] = [{'id': 'leave', 'person_id': h.seed['people'][0], 'kind': 'recorded_leave',
                            'start': '2026-10-04T05:00:00Z', 'end': '2026-10-04T09:00:00Z'}]
    summary = rota_summary(evaluate(data))
    causes = summary['fix_list']['summary']
    assert causes['must'] + causes['could'] > 0
    assert (summary['issued_counts']['must'], summary['issued_counts']['could']) == (causes['must'], causes['could'])
