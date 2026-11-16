"""Personal attribution stays faithful to issued scores and never grants peer or draft access to staff."""
import time
import uuid
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import event, insert, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from packages.contracts.schemas import ROLE_NAMES, RequirementSegment
from packages.domain import validation
from packages.domain.score import schedule_score
from packages.domain.generation import coverage_findings
from packages.domain.validation import evaluate
from tests.governance_support import identity
from tests.test_schedule_score import data, policy, shift, warning
from tests.test_performance_budget import python_budget


@pytest.mark.parametrize('maximum', [None, 1])
def test_grouped_leave_cover_offers_safe_spare_without_weakening_the_maximum(h, maximum):
    absent, spare = h.seed['people'][:2]
    typ = h.configure('assignment_types', {'name': 'Grouped leave opportunity', 'starts_on': '2020-01-01',
                    'definition': {'requirements': [{'minimum': 1, 'maximum': maximum}]}})['record']['id']
    w = h.shift(h.workspace(), typ=typ, people=[absent])['workspace']
    h.command('coordinator', 'availability.record', {'person_id': absent, 'kind': 'recorded_leave',
              'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-05T00:00:00+03:00', 'private_reason': 'Synthetic'})
    w = h.lifecycle(w, 'validate')['workspace']
    stored = h.detail(w)
    source = h.snapshot(w)
    duty, = source['shifts']
    before = evaluate(source)
    assert [f['code'] for f in before['findings']] == ['UNAVAILABLE']
    assert before['findings'][0]['detail']['derived_findings'][0]['code'] == 'COVERAGE_GAP'
    slot = {'id': 'expected-spare', 'shift_id': duty['id'], 'person_id': spare,
            'start': duty['start'], 'end': duty['end'], 'participation': 'staff', 'overtime': False}
    trial = {**source, 'shifts': [dict(duty, participants=[*duty['participants'], slot])]}
    after = evaluate(trial)
    assert any(f['code'] == 'UNAVAILABLE' for f in after['findings']) and not after['valid']
    body = h.get('coordinator', '/api/people/' + spare + '/score?open_limit=20&workspace_id=' + w['id'])['draft']
    if maximum is None:
        opportunity, = body['open_places']
        # Shared candidate_status marks a placement-created SURPLUS_COVER advisory as warn.
        expected_status = 'warn' if any(f['code'] == 'SURPLUS_COVER' for f in after['findings']) else 'ok'
        assert opportunity['shift_id'] == duty['id'] and opportunity['status'] == expected_status
        assert opportunity['score_delta'] == round(schedule_score(trial, after)['score']
                                                  - schedule_score(source, before)['score'], 1)
    else:
        assert any(f['code'] == 'MAXIMUM_PARTICIPANTS' for f in after['findings'])
        assert body['open_places'] == []
    assert h.detail(w) == stored


def test_unvalidated_draft_person_score_has_no_attribution_or_opportunities(h):
    from packages.domain.generation import coverage_findings
    assert list(coverage_findings({})) == []
    w = h.workspace('Unchecked legacy draft')
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, w['id']).validation = None
    body = h.get('coordinator', '/api/people/' + h.seed['people'][0] + '/score?workspace_id=' + w['id'])['draft']
    assert body['fresh'] is False and body['score'] is None
    assert body['findings'] == body['open_places'] == [] and body['contributions_complete'] is False


@pytest.mark.parametrize('cancelled', [False, True])
def test_historical_empty_draft_person_score_is_not_applicable_without_rewriting_evidence(h, cancelled):
    w = h.workspace('Historical empty score')
    if cancelled:
        created = h.shift(w)
        w = h.command('coordinator', 'shift.action', {'workspace_id': w['id'],
            'shift_ids': [created['shift_id']], 'action': 'cancel', 'reason': 'No active sessions'},
            revision=created['workspace']['revision'])['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        workspace = db.get(m.Workspace, w['id'])
        workspace.validation = {**workspace.validation, 'score': {**workspace.validation['score'],
            'score': 100, 'raw': 100, 'uncapped_score': 100, 'publishable': True, 'gate': None}}
    stored = h.detail(w)
    body = h.get('coordinator', '/api/people/' + h.seed['people'][0] + '/score?workspace_id=' + w['id'])['draft']
    assert body['fresh'] and body['score'] is None and body['person_score'] is None
    assert body['contributions'] == body['open_places'] == []
    assert h.get('coordinator', '/api/workspaces/' + w['id'] + '/summary')['score'] is None
    assert h.detail(w) == stored
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Workspace, w['id']).validation['score']['score'] == 100


def test_small_coverage_improvement_is_not_lost_to_display_rounding():
    from tests.test_schedule_score import coverage
    source = data([shift('s' + str(i), 1, []) for i in range(500)])
    before = {'coverage': [coverage('s' + str(i), 1, 100, 0) for i in range(500)], 'findings': []}
    after = {**before, 'coverage': [dict(row, available_eligible=1) if i == 0 else row
                                  for i, row in enumerate(before['coverage'])]}
    base, trial = schedule_score(source, before), schedule_score(source, after)
    assert base['components']['coverage']['value'] == trial['components']['coverage']['value']
    assert base['coverage_fraction'] < trial['coverage_fraction']


def test_scorer_preserves_complete_attribution_without_changing_top_summary():
    ids = [str(uuid.uuid4()) for _ in range(20)]
    source = data([shift('s1', 1, ids)], [policy('rest')])
    checked = {'coverage': [], 'findings': [warning('rest', pid) for pid in ids]}
    full = schedule_score(source, checked)
    assert len(full['contributions']) == 20
    assert len(full['why']) == 13 and full['why'][-1]['component'] == 'other'
    assert round(sum(row['points'] for row in full['contributions']), 1) == round(100 - full['score'], 1)
    assert full['rule_weights'][0]['weight'] == full['settings']['strengths']['medium']


@pytest.mark.parametrize('kind', ['allocation_preference', 'run_length'])
def test_preference_contributions_identify_the_policy_and_weight(kind):
    rule = policy('preference', strength='high')
    rule['definition']['rule'] = {'kind': kind, 'priority': -1} if kind == 'allocation_preference' else {
        'kind': kind, 'maximum': 1}
    source = data([shift('s1', 1, ['a'])], [rule])
    checked = {'coverage': [], 'findings': [] if kind == 'allocation_preference' else [warning('preference', 'a')]}
    result = schedule_score(source, checked)
    [loss] = [row for row in result['contributions'] if row['component'] == 'preferences']
    assert loss['rule_id'] == 'preference' and loss['person_id'] == 'a'
    assert result['rule_weights'][0]['weight'] == result['settings']['strengths']['high']
    assert round(sum(row['points'] for row in result['contributions']), 1) == round(100 - result['score'], 1)


@pytest.mark.parametrize('pairs', [False, True])
def test_fairness_contributions_identify_the_goal(pairs):
    rule = policy('balance')
    rule['definition']['rule'] = {'kind': 'balance_goal', 'metric': 'duties', 'aim': 'target', 'amount': 2}
    source = data([shift('s1', 1, ['a'])], [rule])
    report = {'policy_id': 'balance', 'name': 'Balance', 'strength': 'medium', 'score': 50, 'unit': 'duties',
              'people': [{'person_id': 'a', 'actual': 1, 'expected': 2, 'cost': 0.5}],
              'pairs': [{'person_id': 'a', 'other_person_id': 'b', 'expected': 2, 'cost': 0.5}] if pairs else []}
    result = schedule_score(source, {'coverage': [], 'findings': [], 'goals': [report]})
    [loss] = [row for row in result['contributions'] if row['component'] == 'fairness' and row['rule_id'] == 'balance']
    assert loss['rule_id'] == 'balance' and loss['person_id'] == 'a'
    # The single needed duty is indivisible: only the configured goal loses fairness points.
    assert not [row for row in result['contributions'] if row['component'] == 'fairness' and row['rule_id'] is None]
    assert round(sum(row['points'] for row in result['contributions']), 1) == round(100 - result['score'], 1)


@pytest.mark.parametrize('role', ROLE_NAMES)
def test_person_score_role_matrix(h, role):
    if role == 'programme_lead':
        identity(h, 'programme_lead', ['programme_lead'])
    pid = h.seed['people'][0]
    path = '/api/people/' + pid + '/score'
    permitted = role in ('admin', 'coordinator', 'approver', 'auditor', 'programme_lead', 'staff')
    body = h.get(role, path, status=200 if permitted else 403)
    if permitted:
        assert body == {'person_id': pid, 'draft': None, 'published': None}
    if role == 'staff':
        h.get(role, '/api/people/' + h.seed['people'][1] + '/score', status=403)


def test_score_reports_issued_penalties_and_legacy_completeness(h, monkeypatch):
    pid = h.seed['people'][0]
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week'}, severity='advisory')
    workspace = h.shift(h.workspace(), people=[pid])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00', people=[pid])['workspace']
    body = h.get('coordinator', '/api/people/' + pid + '/score?open_limit=20')['draft']
    assert body['contributions_complete'] and body['fresh']
    assert body['contributions'] and body['findings'] and body['rule_weights']
    assert all(row['score_delta'] == -row['points'] for row in body['contributions'])
    [penalty] = [row for row in body['contributions'] if row['component'] == 'rules']
    [finding] = [row for row in body['findings'] if row['code'] == 'MAX_DUTIES']
    assert penalty['rule_strength'] == finding['rule_strength'] == 'medium'
    assert penalty['rule_severity'] == finding['rule_severity'] == 'advisory'
    assert penalty['rule_weight'] == finding['rule_weight'] == body['rule_weights'][0]['weight']
    assert 'detail' not in finding and 'actual' not in finding
    published = h.publish(workspace)
    mine = h.get('staff', '/api/people/' + pid + '/score')
    assert mine['draft'] is None and mine['published']['score'] is None
    assert mine['published']['contributions_complete']
    # Simulate an older scorer at issuance; publication evidence cannot be rewritten afterwards.
    from packages.domain import score
    def old_score(*args, **kwargs):
        return {key: value for key, value in schedule_score(*args, **kwargs).items()
                if key not in ('contributions', 'rule_weights')}
    monkeypatch.setattr(score, 'schedule_score', old_score)
    older = h.workspace(start='2026-10-11', end='2026-10-18')
    h.publish(h.shift(older, start='2026-10-11T08:00:00+03:00',
                      end='2026-10-11T12:00:00+03:00', people=[pid])['workspace'])
    legacy = h.get('staff', '/api/people/' + pid + '/score')['published']
    assert not legacy['contributions_complete'] and legacy['rule_weights'] == []


def test_staff_unlinked_draft_cross_department_and_stale_check(h):
    pid = h.seed['people'][0]
    workspace = h.shift(h.workspace(), people=[pid])['workspace']
    path = '/api/people/' + pid + '/score'
    h.get('staff', path + '?workspace_id=' + workspace['id'], status=403)
    with transaction(h.app.state.engine, write=True) as db:
        organisation = db.get(m.Organisation, h.seed['organisation_id'])
        organisation.input_revision += 1
        outsider = m.Person(organisation_id=organisation.id, display_name='Other department only')
        db.add(outsider)
        db.flush()
        outsider_id = outsider.id
    assert h.get('coordinator', path)['draft']['fresh']
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, pid).active = False
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
    stale = h.get('coordinator', path)['draft']
    assert not stale['fresh'] and stale['open_places'] == []
    h.get('admin', '/api/people/' + outsider_id + '/score', status=404)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed['identities']['staff']).person_id = None
    h.get('staff', path, status=403)


def test_person_score_offers_partial_fill_when_a_duty_needs_two_people(h):
    pid = h.seed['people'][0]
    with transaction(h.app.state.engine, write=True) as db:
        typ = db.get(m.AssignmentType, h.seed['type'])
        typ.definition = {**typ.definition, 'requirements': [RequirementSegment(minimum=2).model_dump()]}
    workspace = h.shift(h.workspace(), people=[])['workspace']
    source = h.snapshot(workspace)
    [duty] = source['shifts']
    baseline = evaluate(source)
    assert any(row['required'] == 2 for row in baseline['coverage'])
    slot = {'id': 'expected-placement', 'shift_id': duty['id'], 'person_id': pid,
            'start': duty['start'], 'end': duty['end'], 'participation': 'staff', 'overtime': False}
    trial = {**source, 'shifts': [dict(duty, participants=[slot])]}
    checked = evaluate(trial)
    assert any(row['required'] == 2 and row['status'] == 'gap' for row in checked['coverage'])
    expected_delta = round(schedule_score(trial, checked)['score'] - schedule_score(source, baseline)['score'], 1)
    body = h.get('coordinator', '/api/people/' + pid + '/score?open_limit=20')['draft']
    [opportunity] = body['open_places']
    assert opportunity['shift_id'] == duty['id'] and opportunity['status'] == 'ok'
    assert opportunity['score_delta'] == expected_delta > 0
    assert h.detail(workspace)['shifts'][0]['participants'] == []


def test_person_score_marks_session_advisory_warning_on_complete_trial(h):
    pid, peer = h.seed['people'][:2]
    h.policy({'kind': 'maximum_participants', 'maximum': 1}, severity='advisory')
    typ = h.configure('assignment_types', {'name': 'Advisory session warning', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2}]}})['record']['id']
    workspace = h.shift(h.workspace(), typ=typ, people=[peer])['workspace']
    [opportunity] = h.get('coordinator', '/api/people/' + pid + '/score?open_limit=20')['draft']['open_places']
    assert opportunity['status'] == 'warn'


@pytest.mark.parametrize('minimum', [2, 3])
def test_person_score_offers_scoped_cover_fill_even_with_zero_score_delta(h, minimum):
    pid, peer = h.seed['people'][:2]
    h.policy({'kind': 'minimum_cover', 'minimum': minimum})
    workspace = h.shift(h.workspace(), people=[peer])['workspace']
    source = h.snapshot(workspace)
    [duty] = source['shifts']
    checked = evaluate(source)
    assert all(row['available_eligible'] >= row['required'] for row in checked['coverage'])
    assert any(row['code'] == 'SCOPED_COVERAGE_GAP' for row in checked['findings'])
    slot = {'id': 'expected-placement', 'shift_id': duty['id'], 'person_id': pid,
            'start': duty['start'], 'end': duty['end'], 'participation': 'staff', 'overtime': False}
    trial = {**source, 'shifts': [dict(duty, participants=[*duty['participants'], slot])]}
    expected = round(schedule_score(trial, evaluate(trial))['score'] - schedule_score(source, checked)['score'], 1)
    [opportunity] = h.get('coordinator', '/api/people/' + pid + '/score?open_limit=20')['draft']['open_places']
    assert opportunity['shift_id'] == duty['id'] and opportunity['score_delta'] == expected
    assert (expected == 0) == (minimum == 3)


def test_person_score_offers_each_missing_rank_while_the_other_gap_keeps_the_score_capped(h):
    first, second, peer = h.seed['people']
    ranks = [h.configure('categories', {'name': name, 'starts_on': '2020-01-01'})['record']['id']
             for name in ('First missing rank', 'Second missing rank')]
    qualified = dict(zip((first, second), ranks))
    with transaction(h.app.state.engine, write=True) as db:
        for membership in db.scalars(select(m.Membership).where(m.Membership.person_id.in_(qualified),
                m.Membership.department_id == h.seed['department_id'])):
            membership.category_id = qualified[membership.person_id]
    typ = h.configure('assignment_types', {'name': 'Counted rank opportunity', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1}]}})['record']['id']
    h.policy({'kind': 'minimum_cover', 'minimum': 1}, scope={'conditions': {'match': 'all', 'conditions': [
        {'field': 'category_ids', 'op': 'any', 'values': ranks, 'count': 'each'}]}})
    workspace = h.shift(h.workspace(), typ=typ, people=[peer])['workspace']
    stored = h.detail(workspace)
    source = h.snapshot(workspace)
    [duty] = source['shifts']
    before = evaluate(source)
    visible = [row for row in before['findings'] if row['code'] == 'SCOPED_COVERAGE_GAP']
    gaps = [row for row in coverage_findings(before) if row['code'] == 'SCOPED_COVERAGE_GAP']
    assert len(visible) == 1 and len(gaps) == 2
    assert {row['detail']['count_bucket'][-1][1] for row in gaps} == set(ranks)
    assert all(row['available_eligible'] >= row['required'] for row in before['coverage'])
    baseline = schedule_score(source, before)
    for pid, rank in qualified.items():
        slot = {'id': 'expected-placement', 'shift_id': duty['id'], 'person_id': pid,
                'start': duty['start'], 'end': duty['end'], 'participation': 'staff', 'overtime': False}
        trial = {**source, 'shifts': [dict(duty, participants=[*duty['participants'], slot])]}
        after = evaluate(trial)
        [remaining] = [row for row in after['findings'] if row['code'] == 'SCOPED_COVERAGE_GAP']
        assert remaining['detail']['count_bucket'][-1][1] == next(other for other in ranks if other != rank)
        score = schedule_score(trial, after)
        assert baseline['capped'] and score['capped']
        expected = round(score['score'] - baseline['score'], 1)
        [opportunity] = h.get('coordinator', '/api/people/' + pid + '/score?open_limit=20&workspace_id=' + workspace['id'])['draft']['open_places']
        # Shared candidate_status marks a placement-created SURPLUS_COVER advisory as warn.
        assert opportunity['shift_id'] == duty['id'] and opportunity['status'] == 'warn'
        assert opportunity['score_delta'] == expected == 0
    unchanged = h.detail(workspace)
    assert unchanged['workspace']['revision'] == stored['workspace']['revision']
    assert unchanged['shifts'] == stored['shifts']


def test_person_score_never_fills_scoped_cover_by_exceeding_the_type_maximum(h):
    pid, peer = h.seed['people'][:2]
    typ = h.configure('assignment_types', {'name': 'One person at most', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']
    h.policy({'kind': 'minimum_cover', 'minimum': 2})
    workspace = h.shift(h.workspace(), typ=typ['id'], people=[peer])['workspace']
    source = h.snapshot(workspace)
    [duty] = source['shifts']
    before = evaluate(source)
    assert any(row['code'] == 'SCOPED_COVERAGE_GAP' for row in before['findings'])
    assert not any(row['code'] == 'MAXIMUM_PARTICIPANTS' for row in before['findings'])
    slot = {'id': 'expected-placement', 'shift_id': duty['id'], 'person_id': pid,
            'start': duty['start'], 'end': duty['end'], 'participation': 'staff', 'overtime': False}
    trial = {**source, 'shifts': [dict(duty, participants=[*duty['participants'], slot])]}
    after = evaluate(trial)
    assert not any(row['code'] == 'SCOPED_COVERAGE_GAP' for row in after['findings'])
    assert any(row['code'] == 'MAXIMUM_PARTICIPANTS' and row['severity'] == 'error' for row in after['findings'])
    assert h.get('coordinator', '/api/people/' + pid + '/score?open_limit=20')['draft']['open_places'] == []
    assert len(h.detail(workspace)['shifts'][0]['participants']) == 1


def test_person_score_keeps_partial_pooled_supervision_improvement_at_the_score_cap(h):
    first, second, supervisor = h.seed['people']
    for trainee in (first, second):
        h.command('coordinator', 'membership.create', {'person_id': trainee,
            'category_id': h.seed['trainee_category'], 'starts_on': '2020-01-01'})
    typ = h.configure('assignment_types', {'name': 'Pooled supervision places', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2}], 'supervision': {
            'trainee_category_ids': [h.seed['trainee_category']], 'capability_id': h.seed['supervisor_capability'],
            'trainees_per_supervisor': 1, 'require_pairing': False}}})['record']
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    participants = [{'person_id': pid, 'start': start, 'end': end, 'participation': 'trainee'}
                    for pid in (first, second)]
    workspace = h.shift(h.workspace(), typ=typ['id'], people=participants)['workspace']
    source = h.snapshot(workspace)
    [duty] = source['shifts']
    before = evaluate(source)
    [gap_before] = [row for row in before['findings'] if row['code'] == 'SUPERVISION_GAP']
    assert gap_before['detail']['trainees'] == 2 and gap_before['detail']['qualified_supervisors'] == 0
    assert all(row['available_eligible'] >= row['required'] for row in before['coverage'])
    slot = {'id': 'expected-placement', 'shift_id': duty['id'], 'person_id': supervisor,
            'start': duty['start'], 'end': duty['end'], 'participation': 'staff', 'overtime': False, 'team_lead': False}
    trial = {**source, 'shifts': [dict(duty, participants=[*duty['participants'], slot])]}
    after = evaluate(trial)
    [gap_after] = [row for row in after['findings'] if row['code'] == 'SUPERVISION_GAP']
    assert gap_after['detail']['trainees'] == 2 and gap_after['detail']['qualified_supervisors'] == 1
    assert all(row['available_eligible'] >= row['required'] for row in after['coverage'])
    assert schedule_score(source, before)['capped'] and schedule_score(trial, after)['capped']
    expected = round(schedule_score(trial, after)['score'] - schedule_score(source, before)['score'], 1)
    [opportunity] = h.get('coordinator', '/api/people/' + supervisor + '/score?open_limit=20')['draft']['open_places']
    assert opportunity['shift_id'] == duty['id'] and opportunity['score_delta'] == expected == 0
    assert opportunity['team_lead'] is False
    assert len(h.detail(workspace)['shifts'][0]['participants']) == 2


def test_person_score_returns_a_qualified_lead_flag_and_reproducible_score_delta(h):
    from tests.test_team_lead import lead_type
    pid, peer = h.seed['people'][:2]
    typ = lead_type(h)
    workspace = h.shift(h.workspace(), typ=typ, people=[peer])['workspace']
    source = h.snapshot(workspace)
    [duty] = source['shifts']
    before = evaluate(source)
    assert any(row['code'] == 'LEAD_GAP' for row in before['findings'])
    [opportunity] = h.get('coordinator', '/api/people/' + pid + '/score?open_limit=20')['draft']['open_places']
    assert opportunity['shift_id'] == duty['id'] and opportunity['team_lead'] is True
    slot = {'id': 'expected-placement', 'shift_id': duty['id'], 'person_id': pid,
            'start': opportunity['start'], 'end': opportunity['end'], 'participation': 'staff',
            'overtime': False, 'team_lead': opportunity['team_lead']}
    trial = {**source, 'shifts': [dict(duty, participants=[*duty['participants'], slot])]}
    after = evaluate(trial)
    assert after['valid'] and not any(row['code'] == 'LEAD_GAP' for row in after['findings'])
    expected = round(schedule_score(trial, after)['score'] - schedule_score(source, before)['score'], 1)
    assert opportunity['score_delta'] == expected > 0
    assert len(h.detail(workspace)['shifts'][0]['participants']) == 1


def test_staffing_candidate_with_session_only_safety_error_is_not_ok(h):
    from apps.api.services.staffing import check_people
    candidate, present = h.seed['people'][:2]
    typ = h.configure('assignment_types', {'name': 'Single-place safety test', 'starts_on': '2020-01-01',
                    'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']['id']
    workspace = h.shift(h.workspace(), typ=typ, people=[present])['workspace']
    source = h.snapshot(workspace)
    shift = source['shifts'][0]
    rows, _ = check_people(source, shift, None, {candidate})
    row = rows[0]
    assert row['status'] == 'blocked'
    assert any(finding['code'] == 'MAXIMUM_PARTICIPANTS' for finding in row['findings'])


def test_staff_published_opportunity_hides_department_baseline(h):
    pid, peer = h.seed['people'][:2]
    with transaction(h.app.state.engine, write=True) as db:
        typ = db.get(m.AssignmentType, h.seed['type'])
        typ.definition = {**typ.definition, 'requirements': [RequirementSegment(minimum=1, target=2).model_dump()]}
    workspace = h.shift(h.workspace(), people=[peer])['workspace']
    h.publish(workspace)
    mine = h.get('staff', '/api/people/' + pid + '/score?open_limit=20')['published']
    admin = h.get('admin', '/api/people/' + pid + '/score?open_limit=20')['published']
    assert mine['score'] is None and mine['opportunity_score'] is None
    assert mine['open_places_total'] is None and mine['open_has_more'] is None
    assert admin['open_places_total'] == 1 and admin['open_has_more'] is False
    assert admin['score'] is not None and admin['opportunity_score'] is not None
    assert len(mine['open_places']) == 1 and mine['open_places'] == admin['open_places']
    assert mine['opportunity_basis'] == 'current_recorded_inputs'


@pytest.mark.parametrize('role', ROLE_NAMES)
def test_person_score_open_place_labels_role_matrix(h, role):
    if role == 'programme_lead':
        identity(h, role, [role])
    pid, peer = h.seed['people'][:2]
    typ = h.configure('assignment_types', {'name': 'Recorded duty label', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'target': 2}]}})['record']
    workspace = h.shift(h.workspace(), typ=typ['id'], people=[peer])['workspace']
    source = h.snapshot(workspace)
    [duty] = source['shifts']
    h.publish(workspace)
    h.configure('assignment_types', {'name': 'New duty name', 'starts_on': '2020-01-01',
        'definition': typ['definition']}, target=typ['id'], revision=typ['revision'])
    permitted = role in {'admin', 'coordinator', 'approver', 'auditor', 'programme_lead', 'staff'}
    result = h.get(role, '/api/people/' + pid + '/score?open_limit=20', status=200 if permitted else 403)
    if permitted:
        [opportunity] = result['published']['open_places']
        assert opportunity['session_label'] == 'Recorded duty label'
        assert opportunity['service_names'] == [source['services'][sid]['name'] for sid in duty['service_ids']]
        assert opportunity['location_name'] == source['locations'][duty['location_id']]['name']


def test_person_score_opportunities_exclude_neighboring_workspace_gaps(h, monkeypatch):
    from apps.api.services import person_score
    pid = h.seed['people'][0]
    workspace = h.shift(h.workspace(), people=[])['workspace']
    source = h.snapshot(workspace)
    [duty] = source['shifts']
    external = dict(duty, id=str(uuid.uuid4()), workspace_id=str(uuid.uuid4()),
                    type_snapshot={**duty['type_snapshot'], 'name': 'Other department duty'})
    source['shifts'].append(external)
    with transaction(h.app.state.engine, write=True) as db:
        stored = db.get(m.Workspace, workspace['id'])
        stored.validation = {**stored.validation, 'coverage': [*stored.validation['coverage'],
            {'shift_id': external['id'], 'status': 'gap'}]}
    monkeypatch.setattr(person_score, 'domain_snapshot', lambda *args, **kwargs: source)
    body = h.get('admin', '/api/people/' + pid + '/score?open_limit=20')['draft']
    assert body['open_places_total'] == 1
    assert [row['shift_id'] for row in body['open_places']] == [duty['id']]
    assert body['open_places'][0]['session_label'] != 'Other department duty'


def test_person_score_does_not_recheck_the_department_when_all_places_are_blocked(h, monkeypatch):
    from apps.api.services import person_score
    from sqlalchemy import delete
    pid = h.seed['people'][0]
    workspace = h.shift(h.workspace(), people=[])['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(delete(m.Eligibility).where(m.Eligibility.person_id == pid))
    workspace = h.lifecycle(workspace, 'validate')['workspace']
    def unexpected_recheck(*args, **kwargs):
        pytest.fail('A blocked person has no score trials to evaluate')
    monkeypatch.setattr(person_score, 'evaluate', unexpected_recheck)
    body = h.get('coordinator', '/api/people/' + pid + '/score?open_limit=20&workspace_id=' + workspace['id'])['draft']
    assert body['fresh']
    assert body['open_places'] == [] and body['open_places_total'] == 1
    assert body['opportunity_score'] is None


@pytest.fixture(scope='module')
def person_score_opportunities_scale(scale_harness):
    h = scale_harness('h')
    with pytest.MonkeyPatch.context() as monkeypatch:
        pid = h.seed['people'][0]
        workspace = h.workspace(start='2026-10-01', end='2026-11-01')
        people = [str(uuid.uuid4()) for _ in range(150)]
        locations = [str(uuid.uuid4()) for _ in range(9)]
        shifts, services, assignments = [], [], []
        with transaction(h.app.state.engine, write=True) as db:
            typ = db.get(m.AssignmentType, h.seed['type'])
            type_name = typ.name
            clinic_name = db.get(m.Service, h.seed['services'][0]).name
            db.execute(insert(m.Person), [{'id': p, 'organisation_id': h.seed['organisation_id'],
                                         'display_name': f'Synthetic person {i}'} for i, p in enumerate(people)])
            db.execute(insert(m.Membership), [{'person_id': p, 'department_id': h.seed['department_id'],
                'category_id': h.seed['category'], 'starts_on': date(2020, 1, 1)} for p in people])
            db.execute(insert(m.Eligibility), [{'person_id': p, 'department_id': h.seed['department_id'],
                'service_id': h.seed['services'][0], 'starts_on': date(2020, 1, 1)} for p in people])
            db.execute(insert(m.Location), [{'id': loc, 'department_id': h.seed['department_id'],
                'location_type_id': h.seed['location_type'], 'name': f'Synthetic room {i}', 'capacity': 2,
                'starts_on': date(2020, 1, 1)} for i, loc in enumerate(locations)])
            for i in range(500):
                shift_id = str(uuid.uuid4())
                starts = datetime(2026, 10, 1, 5, tzinfo=timezone.utc) + timedelta(days=i // 18, hours=4 * (i % 18 // 9))
                ends = starts + timedelta(hours=4)
                shifts.append({'id': shift_id, 'workspace_id': workspace['id'], 'assignment_type_id': typ.id,
                    'type_revision': typ.revision, 'type_snapshot': record(typ), 'location_id': locations[i % 9],
                    'start': starts, 'end': ends})
                services.append({'shift_id': shift_id, 'service_id': h.seed['services'][0]})
                if i >= 2:
                    assignments.append({'shift_id': shift_id, 'person_id': people[i % 150], 'start': starts, 'end': ends})
            db.execute(insert(m.Shift), shifts)
            db.execute(insert(m.ShiftService), services)
            db.execute(insert(m.Assignment), assignments)
        h.lifecycle(workspace, 'validate')
        checked, full_checked, original = Counter(), Counter(), validation.validate_session_setup
        def check_session(data, shift, *args):
            checked[shift['id']] += 1
            # Staffing also checks one candidate with coverage; count the complete
            # department evaluations separately from those reduced focus checks.
            if (data.get('_partial') is None and not data.get('skip_coverage')
                    and len(data['focus_ids']) == len(shifts)):
                full_checked[shift['id']] += 1
            return original(data, shift, *args)
        monkeypatch.setattr(validation, 'validate_session_setup', check_session)
        client = h.client('admin')
        count = []
        def read(*args):
            count.append(1)
        event.listen(h.app.state.engine, 'before_cursor_execute', read)
        start = time.process_time()
        try:
            response = client.get('/api/people/' + pid + '/score?open_limit=2')
        finally:
            event.remove(h.app.state.engine, 'before_cursor_execute', read)
        body = response.json()['draft']
        elapsed = time.process_time() - start
        return response, body, shifts, type_name, clinic_name, checked, full_checked, count, elapsed


def test_person_score_active_opportunities_department_query_and_time_budget(person_score_opportunities_scale, record_property):
    response, body, shifts, type_name, clinic_name, checked, full_checked, count, _ = person_score_opportunities_scale
    assert response.status_code == 200, response.text
    assert body['fresh'] and body['open_places_total'] == 2 and not body['open_has_more']
    ordered = sorted(shifts[:2], key=lambda row: (row['start'], row['id']))
    assert {row['shift_id'] for row in body['open_places']} == {row['id'] for row in ordered[:2]}
    assert all(row['session_label'] == type_name and row['service_names'] == [clinic_name]
               and row['location_name'].startswith('Synthetic room ') for row in body['open_places'])
    # Either placement leaves the other mandatory vacancy: the shared score remains capped.
    assert all(row['status'] == 'ok' and row['score_delta'] == 0 for row in body['open_places'])
    assert all(full_checked[row['id']] == 1 for row in shifts[2:]), 'Unchanged sessions are checked once per read'
    assert all(full_checked[row['id']] >= 2 for row in shifts[:2]), 'Each changed opportunity is checked again'
    record_property('session_setup_checks', sum(checked.values()))
    record_property('full_session_setup_checks', sum(full_checked.values()))
    assert len(count) <= 90, len(count)


@pytest.mark.cpu_budget
def test_person_score_active_opportunities_department_query_and_time_budget_cpu_budget(person_score_opportunities_scale, record_property):
    *_, elapsed = person_score_opportunities_scale
    record_property('score_read_seconds', elapsed)
    assert elapsed < python_budget(1), elapsed
