"""Fairness ledger and Balance goals: ledger build/backfill/idempotency, validator and score, the solver's
history-aware balancing, and the dashboard API with its permissions."""
from tests.legacy_lead import historical_type

import json
from copy import deepcopy
from datetime import date

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.fairness import backfill, rebuild
from packages.contracts.schemas import PolicyDefinition
from packages.domain.cp_sat import generate
from packages.domain.fairness import goal_report
from packages.domain.score import flags, schedule_score
from packages.domain.validation import evaluate
from tests.scheduler_departments import apply


def at(day, hour=8, length=4):
    return f'2026-10-{day:02}T{hour:02}:00:00+03:00', f'2026-10-{day:02}T{hour + length:02}:00:00+03:00'


def session(h, w, day, people, overtime=(), hour=8):
    start, end = at(day, hour)
    return h.shift(w, start=start, end=end, people=[{'person_id': p, 'start': start, 'end': end, 'overtime': p in overtime} for p in people])['workspace']


def goal(h, metric='duties', aim='equal_share', period='month', scope=None, strength='high', **rule):
    return h.policy({'kind': 'balance_goal', 'metric': metric, 'aim': aim, 'period': period, **rule}, scope or {}, severity='advisory') | {}


def ledger(h, **where):
    with transaction(h.app.state.engine) as db:
        query = select(m.FairnessLedger)
        for key, value in where.items():
            query = query.where(getattr(m.FairnessLedger, key) == value)
        return [(r.person_id, r.other_person_id, r.metric_key, r.bucket, r.value, r.expected) for r in db.scalars(query)]


def test_goal_schema_rejects_incoherent_settings():
    base = {'severity': 'advisory', 'rule': {'kind': 'balance_goal', 'metric': 'duties'}}
    assert PolicyDefinition.model_validate(base).rule.aim == 'equal_share'
    for bad in ({'metric': 'pair_exposure'}, {'aim': 'min_per_pair', 'amount': 2}, {'aim': 'target'}, {'period': 'rolling'},
                {'period': 'custom', 'starts_on': '2026-01-01'}, {'assignment_type_ids': ['00000000-0000-0000-0000-000000000001'],
                                                                 'service_ids': ['00000000-0000-0000-0000-000000000002']}):
        with pytest.raises(ValidationError):
            PolicyDefinition.model_validate({**base, 'rule': {**base['rule'], **bad}})
    with pytest.raises(ValidationError):
        PolicyDefinition.model_validate({**base, 'severity': 'mandatory'})
    with pytest.raises(ValidationError):  # what is counted belongs on the goal, not in its scope
        PolicyDefinition.model_validate({**base, 'scope': {'weekdays': [0, 1]}})


def test_ledger_is_written_on_publish_rebuilt_idempotently_and_replaced_by_an_amendment(h):
    a, b, c = h.seed['people']
    w = h.workspace()
    w = session(h, w, 4, [a, b])
    w = session(h, w, 5, [a], overtime=[a])
    published = h.publish(w)
    pub = published['publication_id']
    rows = ledger(h, publication_id=pub)
    facts = {(p, o, k): v for p, o, k, _, v, _ in rows if k in {'duties', 'hours', 'overtime_duties', 'pair', 'available_days', 'days_worked'}}
    assert facts[a, None, 'duties'] == 1 and facts[a, None, 'hours'] == 4 and facts[a, None, 'overtime_duties'] == 1
    assert facts[a, b, 'pair'] == 1 and facts[b, a, 'pair'] == 1
    assert sum(1 for p, o, k, *_ in rows if k == 'available_days' and p == c) == 7  # a week in the department, no duties
    typed = [r for r in rows if r[2] == 'duties|type:' + h.seed['type']]
    assert len(typed) == 3  # a twice (two days) and b once
    with transaction(h.app.state.engine, write=True) as db:
        again = rebuild(db, db.get(m.Publication, pub))
        assert again == len(rows)
        assert backfill(db) == {'publications': 1, 'rows': len(rows)}
    assert sorted(map(repr, ledger(h, publication_id=pub))) == sorted(map(repr, rows))
    # An amendment replaces the publication it supersedes, rather than adding to it.
    amendment = h.workspace(base=pub)
    h.publish(h.detail(amendment)['workspace'])
    assert ledger(h, publication_id=pub) == []
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.FairnessLedger)) == len(rows)


def leave(h, person, first, last):
    h.command('coordinator', 'availability.record', {'person_id': person, 'kind': 'recorded_leave', 'private_reason': 'Leave',
              'start': f'2026-10-{first:02}T00:00:00+03:00', 'end': f'2026-10-{last:02}T00:00:00+03:00'})


def policy_of(data, record):
    return next(p for p in data['policies'] if p['id'] == record['id'])


def assert_weighted_candidate(data, result, comparison):
    after = apply(data, result)
    options = result['effective_options']
    baseline = {(s['id'], p['person_id']): flags(p) for s in data['shifts'] for p in s['participants']}
    score = schedule_score(after, evaluate(after), options, baseline=baseline)
    validation = evaluate(comparison)
    assert validation['valid']
    other = schedule_score(comparison, validation, options, baseline=baseline)
    assert result['objective_components'] == score['components']
    for name, value in result['compiled_component_values'].items():
        expected = score['components'][name]['value']
        assert value == pytest.approx(expected, abs=0.000051) if expected is not None else value is None
    assert result['weighted_status'] == 'OPTIMAL' and result['optimality_proven']
    names = ('rules', 'fairness', 'preferences', 'stability')
    loss = lambda value: sum(value['components'][name]['weight'] * (1 - value['components'][name]['value'])
                             for name in names if value['components'][name]['value'] is not None)
    assert loss(score) <= loss(other)


def test_goal_findings_and_score_use_shares_pro_rata_to_days_available(h):
    a, b, c = h.seed['people']
    leave(h, c, 4, 11)  # away all week: no share expected of them
    g = goal(h, period='rota')
    w = h.workspace()
    for day in (4, 5, 6, 7):
        w = session(h, w, day, [a])
    data = h.snapshot(w)
    rows = {r['person_id']: r for r in goal_report(data, policy_of(data, g))['people']}
    assert c not in rows  # no eligible available session in this rota
    assert rows[a]['expected'] == rows[b]['expected'] == 2
    # Only the distance outside the band (2 ± 1) counts, over the aim: 1 of 2.
    assert (rows[a]['status'], rows[b]['status']) == ('above', 'below') and rows[a]['cost'] == rows[b]['cost'] == 0.5
    validation = evaluate(data)
    found = {f['person_id']: f for f in validation['findings'] if f['code'] == 'BALANCE_GOAL'}
    assert set(found) == {a, b} and found[a]['severity'] == 'warning' and found[a]['detail']['expected'] == 2
    from packages.domain.score import schedule_score
    score = schedule_score(data, validation)
    assert score['components']['fairness']['value'] < 1 and score['components']['rules']['value'] in (None, 1)
    assert any(g['name'] in line['text'] and line['component'] == 'fairness' for line in score['why'])
    assert round(sum(line['points'] for line in score['why']), 1) == round(100 - score['score'], 1)
    stored = h.detail(w)['workspace']['validation']
    assert any(report['policy_id'] == g['id'] for report in stored['goals'])


def test_solver_normalizes_shares_by_days_available(h):
    a, b, _ = h.seed['people']
    leave(h, a, 4, 7)
    g = goal(h, period='rota', scope={'person_ids': [a, b]})
    w = h.workspace()
    for day in range(4, 10):
        w = h.shift(w, start=at(day)[0], end=at(day)[1], people=[])['workspace']
    shares, aims = {}, {}
    for share_by in ('available_days', 'equal'):
        data = h.snapshot(w)
        data['people'] = {p: data['people'][p] for p in (a, b)}
        data['generation_options'] = {'shortlist_width': 0}
        policy_of(data, g)['definition']['rule']['share_by'] = share_by
        result = generate(data, 20, deterministic=True)
        assert result['status'] == 'candidate_feasible', result.get('status')
        shares[share_by] = sum(x['participant']['person_id'] == a for x in result['additions'])
        report = next(r for r in result['validation']['goals'] if r['policy_id'] == g['id'])
        aims[share_by] = next(row['expected'] for row in report['people'] if row['person_id'] == a)
        assert report['score'] == 100
        comparison = deepcopy(apply(data, result))
        for i, shift in enumerate(sorted(comparison['shifts'], key=lambda s: s['start'])):
            shift['participants'][0]['person_id'] = a if i >= 4 else b
        assert_weighted_candidate(data, result, comparison)
    # Both splits are inside the pro-rata band's tolerance; the built-in workload goal prefers 3/3.
    assert shares == {'available_days': 3, 'equal': 3}
    assert aims == {'available_days': 2.18, 'equal': 3}


def test_overtime_evens_out_given_history(h):
    a, b, c = h.seed['people']
    h.policy({'kind': 'max_hours', 'hours': 4, 'window': 'week', 'include_overtime': False})
    cap = h.policy({'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'metric': 'duties',
                    'comparison': 'maximum', 'amount': 10, 'overtime': 'only'})
    h.policy({'kind': 'allocation_overtime', 'required_policy_ids': [cap['id']]}, severity='advisory')
    g = goal(h, metric='overtime_duties', period='month', scope={'person_ids': [a, b]})
    w = h.workspace()
    w = session(h, w, 4, [a])
    for day in (5, 6):
        w = session(h, w, day, [a], overtime=[a])
    h.publish(w)
    nxt = h.workspace(start='2026-10-11', end='2026-10-18')
    for day in range(11, 15):
        nxt = h.shift(nxt, start=at(day)[0], end=at(day)[1], people=[])['workspace']
    leave(h, c, 11, 18)  # two candidates: each can take one regular duty; two duties must be overtime
    data = h.snapshot(nxt)
    assert data['fairness_history'][g['id']]['people'][a]['value'] == 2
    data['generation_options'] = {'shortlist_width': 0}
    result = generate(data, 30, deterministic=True)
    assert result['status'] == 'candidate_feasible', result.get('status')
    overtime = {p: sum(x['participant']['person_id'] == p and x['participant']['overtime'] for x in result['additions']) for p in (a, b)}
    assert overtime == {a: 1, b: 1}
    report = next(r for r in result['validation']['goals'] if r['policy_id'] == g['id'])
    # History 3/1 is within the 2 +/- 1 band, while splitting this week's duties keeps workload even.
    assert {r['person_id']: r['actual'] for r in report['people']} == {a: 3, b: 1}
    assert report['score'] == 100 and all(row['status'] == 'within' for row in report['people'])
    comparison = deepcopy(apply(data, result))
    for i, shift in enumerate(sorted(comparison['shifts'], key=lambda s: s['start'])):
        participant = shift['participants'][0]
        participant.update(person_id=a if i == 0 else b, overtime=i >= 2)
    assert_weighted_candidate(data, result, comparison)


def test_each_trainee_reaches_the_minimum_with_each_partner_over_the_block(h):
    a, b, c = h.seed['people']
    pair_type = h.configure('assignment_types', {'name': 'Paired clinic', 'starts_on': '2020-01-01',
                            'definition': {'requirements': [{'minimum': 2, 'maximum': 2}]}})['record']['id']
    g = goal(h, metric='pair_exposure', aim='min_per_pair', amount=1, period='custom', starts_on='2026-10-04', ends_on='2026-10-18',
             scope={'conditions': {'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': [a]}]}},
             partners={'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': [b, c]}]})
    w = h.workspace()
    start, end = at(4)
    w = h.shift(w, start=start, end=end, typ=pair_type, people=[a, b])['workspace']
    h.publish(w)
    nxt = h.workspace(start='2026-10-11', end='2026-10-18')
    start, end = at(12)
    nxt = h.shift(nxt, start=start, end=end, typ=pair_type, people=[a, b])['workspace']
    start, end = at(13)
    nxt = h.shift(nxt, start=start, end=end, typ=pair_type, people=[])['workspace']
    data = h.snapshot(nxt)
    assert data['fairness_history'][g['id']]['pairs'][a] == {b: 1.0}
    before = {r['partner_id']: r['status'] for r in goal_report(data, policy_of(data, g))['pairs']}
    assert before == {b: 'within', c: 'below'}
    data['generation_options'] = {'mode': 'fill_gaps'}
    result = generate(data, 20)
    assert result['status'] == 'candidate_feasible', result.get('status')
    assert {x['participant']['person_id'] for x in result['additions']} == {a, c}
    report = next(r for r in result['validation']['goals'] if r['policy_id'] == g['id'])
    assert all(r['status'] == 'within' for r in report['pairs'])
    # The block is each person's rotation: halfway through it, half the minimum is due.
    data['rotations'].append({'person_id': a, 'starts_on': '2026-10-11', 'ends_on': '2026-10-25'})
    policy_of(data, g)['definition']['rule'].update(period='block', amount=4, starts_on=None, ends_on=None)
    assert {r['expected'] for r in goal_report(data, policy_of(data, g))['pairs']} == {2.0}


def test_dashboard_reads_filters_and_permissions(h):
    a, b, c = h.seed['people']
    g = goal(h, metric='overtime_duties', period='month')
    w = h.workspace()
    w = session(h, w, 4, [a, b], overtime=[a])
    w = session(h, w, 5, [b, c])
    h.publish(w)
    who = json.dumps({'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': [a, b]}]})
    goals = h.get('coordinator', '/api/fairness/goals?who=' + who)
    card = next(report for report in goals['goals'] if report['policy_id'] == g['id'])
    assert goals['workspace']['name']
    assert {r['person_id'] for r in card['people']} == {a, b} and card['score'] is not None
    assert card['trend'] and card['trend'][0]['starts_on'] == '2026-10-04'
    mine = h.get('staff', '/api/fairness/goals')  # the staff account is person a
    mine = next(report for report in mine['goals'] if report['policy_id'] == g['id'])
    assert [r['person_id'] for r in mine['people']] == [a] and mine['score'] is None
    h.get('staff', '/api/fairness/goals?workspace_id=' + w['id'], status=403)
    period = '&starts_on=2026-10-01&ends_on=2026-11-01'
    matrix = h.get('coordinator', '/api/fairness/exposure?rows=' + who + period)
    rows, cols = [r['person_id'] for r in matrix['rows']], [r['person_id'] for r in matrix['cols']]
    assert set(rows) == {a, b} and set(cols) == {a, b, c}
    assert matrix['cells'][rows.index(b)][cols.index(c)] == 1 and matrix['cells'][rows.index(a)][cols.index(c)] == 0
    assert [r['person_id'] for r in h.get('staff', '/api/fairness/exposure?' + period[1:])['rows']] == [a]
    spread = h.get('coordinator', '/api/fairness/distribution?metric=overtime_duties' + period)
    people = {r['person_id']: r for r in spread['people']}
    assert people[a]['value'] == 1 and people[b]['value'] == 0 and people[a]['months'] == {'2026-10': 1}
    assert people[a]['available_days'] == 7 and people[a]['expected'] == 0
    assert people[a]['overtime_offered_hours'] == 0 and people[a]['status'] == 'within'
    assert [r['person_id'] for r in h.get('staff', '/api/fairness/distribution?metric=duties' + period)['people']] == [a]
    h.get('coordinator', '/api/fairness/distribution?metric=nonsense', status=422)
    h.get('coordinator', '/api/fairness/exposure?rows=%7Bbad', status=422)
    # Preview an unsaved goal on a draft: each person now against the aim.
    draft = session(h, h.workspace(start='2026-10-11', end='2026-10-18'), 11, [a])
    definition = {'severity': 'advisory', 'rule': {'kind': 'balance_goal', 'metric': 'duties', 'period': 'month'}}
    preview = h.client('coordinator').post('/api/fairness/preview', json={'workspace_id': draft['id'], 'definition': definition})
    assert preview.status_code == 200, preview.text
    people = {r['person_id']: r for r in preview.json()['people']}
    assert people[a]['prior'] == 1 and people[a]['planned'] == 1 and people[b]['prior'] == 2 and people[a]['name']
    assert h.client('staff').post('/api/fairness/preview', json={'workspace_id': draft['id'], 'definition': definition}).status_code == 403


@pytest.mark.parametrize('reverse', [False, True])
def test_lead_turns_share_out_between_the_people_who_may_lead(h, reverse):
    a, b, c = h.seed['people']
    typ = historical_type(h, {'name': 'Led pair', 'starts_on': '2020-01-01', 'definition': {
        'requirements': [{'minimum': 2, 'maximum': 2}],
        'lead': {'minimum': 1, 'maximum': 1, 'capability_ids': [h.seed['supervisor_capability']], 'category_ids': []}}})['id']
    g = goal(h, metric='lead_turns', period='rota', scope={'person_ids': [a, b]}, tolerance_units=0)
    w = h.workspace()
    for day in range(4, 8):  # a and b work every session; who leads is the only choice
        start, end = at(day)
        w = h.shift(w, start=start, end=end, typ=typ, people=[a, b])['workspace']
    data = h.snapshot(w)
    data['people'] = {p: data['people'][p] for p in ((b, a) if reverse else (a, b))}
    if reverse:
        data['shifts'].reverse()
        data['policies'].reverse()
        for shift in data['shifts']:
            shift['participants'].reverse()
    # This asserts the optimum, so spend deterministic solver work rather than shared-runner wall time.
    result = generate(data, 20, deterministic=True)
    assert result['status'] == 'candidate_feasible', result.get('status')
    report = next(r for r in result['validation']['goals'] if r['policy_id'] == g['id'])
    assert {r['person_id']: r['actual'] for r in report['people']} == {a: 2, b: 2}


def test_lead_turn_ties_follow_the_configured_tolerance(h):
    a, b, _ = h.seed['people']
    from .test_governed_leadership import activate as activate_lead, line as lead_line, proposal as propose_lead
    typ = h.configure('assignment_types', {'name': 'Led pair', 'starts_on': '2020-01-01', 'definition': {
        'requirements': [{'minimum': 2, 'maximum': 2}]}})['record']['id']
    activate_lead(h, propose_lead(h, [lead_line(typ)]))
    g = goal(h, metric='lead_turns', period='rota', scope={'person_ids': [a, b]})
    w = h.workspace()
    for day in range(4, 8):
        start, end = at(day)
        w = h.shift(w, start=start, end=end, typ=typ, people=[a, b])['workspace']
    data = h.snapshot(w)
    policy = policy_of(data, g)
    shifts = sorted(data['shifts'], key=lambda s: s['start'])
    for leads in ((a, a, b, b), (a, a, a, b)):
        for shift, lead in zip(shifts, leads):
            for participant in shift['participants']:
                participant['team_lead'] = participant['person_id'] == lead
        report = goal_report(data, policy)
        # Default +/- one turn permits both 2:2 and 3:1; picking either is not a product regression.
        assert report['score'] == 100
        assert all(row['cost'] == 0 for row in report['people'])
    strict = deepcopy(policy)
    strict['definition']['rule']['tolerance_units'] = 0
    assert goal_report(data, strict)['score'] < 100


def test_rebuild_equals_publish_time_rows_after_an_earlier_amendment_and_dashboard_equals_validator(h):
    """Later publications read earlier ones as goal history: amending (or publishing) an earlier rota rebuilds their goal
    rows, so `rebuild-fairness` (backfill) finds exactly the rows publishing wrote. The dashboard's goal card is the
    validator's calculation: the same people outside the band, the same numbers and score."""
    a, b, c = h.seed['people']
    g = goal(h, metric='duties', period='month')
    w1 = session(h, session(h, h.workspace('W1', '2026-10-04', '2026-10-11'), 4, [a, b]), 5, [a])
    p1 = h.publish(w1)
    p2 = h.publish(session(h, session(h, h.workspace('W2', '2026-10-11', '2026-10-18'), 11, [a]), 12, [a, c]))
    # Compare the second publication explicitly; the default now selects the nearer first rota.
    url = '/api/fairness/goals?starts_on=2026-10-01&ends_on=2026-11-01&workspace_id=' + p2['workspace']['id']
    card = next(x for x in h.get('coordinator', url)['goals'] if x['policy_id'] == g['id'])
    with transaction(h.app.state.engine) as db:
        validation = db.get(m.Publication, p2['publication_id']).validation
    findings = {f['person_id']: f['detail'] for f in validation['findings'] if f['code'] == 'BALANCE_GOAL'}
    outside = {r['person_id']: r for r in card['people'] if r['status'] != 'within'}
    assert outside and set(outside) == set(findings)
    assert all((r['actual'], r['expected']) == (findings[p]['actual'], findings[p]['expected']) for p, r in outside.items())
    assert card['score'] == next(x for x in validation['goals'] if x['policy_id'] == g['id'])['score'] == card['trend'][-1]['score']
    amendment = session(h, h.detail(h.workspace(base=p1['publication_id']))['workspace'], 6, [b])  # b gains a duty in week 1
    h.publish(amendment)
    published = sorted(map(repr, ledger(h)))
    assert (b, None, 'goal:' + g['id'], date(2026, 10, 11), 2.0) in [r[:5] for r in ledger(h)]  # week 2 reads the amended history
    with transaction(h.app.state.engine, write=True) as db:
        backfill(db)
    assert sorted(map(repr, ledger(h))) == published


def test_a_goal_scores_100_when_everyone_is_inside_their_band(h):
    """Shares 3 and 2 against 2.5 each are uneven, but both inside the band (2.5 ± 1): no finding, full score, and the
    schedule score loses nothing to the goal. Outside the band only the distance past its edge counts."""
    from packages.domain.score import schedule_score
    a, b, _ = h.seed['people']
    g = goal(h, period='rota', scope={'person_ids': [a, b]})
    w = h.workspace()
    for day, who in ((4, a), (5, a), (6, a), (7, b), (8, b)):
        w = session(h, w, day, [who])
    data = h.snapshot(w)
    report = goal_report(data, policy_of(data, g))
    assert {r['person_id']: (r['actual'], r['status'], r['cost']) for r in report['people']} == {a: (3, 'within', 0), b: (2, 'within', 0)}
    assert report['score'] == 100
    validation = evaluate(data)
    assert not [f for f in validation['findings'] if f['code'] == 'BALANCE_GOAL' and f['rule_id'] == g['id']]
    assert not [line for line in schedule_score(data, validation)['why'] if g['name'] in line['text']]
    # Two more for a: 5 against 3.5 is 0.5 past the band's edge, over the aim; b (2) is 0.5 below it.
    w = session(h, session(h, w, 9, [a]), 10, [a])
    data = h.snapshot(w)
    report = goal_report(data, policy_of(data, g))
    rows = {r['person_id']: r for r in report['people']}
    assert (rows[a]['actual'], rows[a]['expected']) == (5, 3.5) and rows[a]['cost'] == rows[b]['cost'] == round(0.5 / 3.5, 4)
    assert 0 < report['score'] < 100
    why = [line for line in schedule_score(data, evaluate(data))['why'] if g['name'] in line['text']]
    assert {line['person_id'] for line in why} == {a, b}


def test_per_person_targets_in_validator_score_preview_and_solver(h):
    from packages.domain.score import schedule_score
    a, b, _ = h.seed['people']
    g = goal(h, aim='target', period='rota', amount=1, person_amounts={a: 4}, scope={'person_ids': [a, b]})
    w = h.workspace()
    for day in range(4, 9):
        w = h.shift(w, start=at(day)[0], end=at(day)[1], people=[b] if day == 4 else [])['workspace']
    data = h.snapshot(w)
    rows = {r['person_id']: r for r in goal_report(data, policy_of(data, g))['people']}
    assert (rows[a]['expected'], rows[a]['status'], rows[b]['expected'], rows[b]['status']) == (4, 'below', 1, 'within')
    validation = evaluate(data)
    assert {f['person_id'] for f in validation['findings'] if f['code'] == 'BALANCE_GOAL'} == {a}
    why = [line for line in schedule_score(data, validation)['why'] if g['name'] in line['text']]
    assert [line['person_id'] for line in why] == [a] and 'aim 4.0' in why[0]['text']
    definition = {'severity': 'advisory', 'scope': {'person_ids': [a, b]},
                  'rule': {'kind': 'balance_goal', 'metric': 'duties', 'aim': 'target', 'period': 'rota', 'amount': 1, 'person_amounts': {a: 4}}}
    preview = h.client('coordinator').post('/api/fairness/preview', json={'workspace_id': w['id'], 'definition': definition})
    assert {r['person_id']: r['expected'] for r in preview.json()['people']} == {a: 4, b: 1}
    data['people'] = {p: data['people'][p] for p in (a, b)}
    data['generation_options'] = {'shortlist_width': 0}
    result = generate(data, 20, deterministic=True)
    assert result['status'] == 'candidate_feasible', result.get('status')
    added = {p: sum(x['participant']['person_id'] == p for x in result['additions']) for p in (a, b)}
    assert added == {a: 3, b: 1}  # 3/2 stays inside both target bands and improves whole-rota workload fairness.
    report = next(r for r in result['validation']['goals'] if r['policy_id'] == g['id'])
    assert report['score'] == 100
    comparison = deepcopy(apply(data, result))
    for shift in comparison['shifts']:
        if not next(s for s in data['shifts'] if s['id'] == shift['id'])['participants']:
            shift['participants'][0]['person_id'] = a
    assert_weighted_candidate(data, result, comparison)


def test_session_rules_show_a_goal_warning_for_people_outside_the_band(h):
    a, b, _ = h.seed['people']
    g = goal(h, period='rota', scope={'person_ids': [a, b]})
    w = h.workspace()
    for day in (4, 5, 6, 7):
        w = session(h, w, day, [a])
    data = h.snapshot(w)
    sid = next(s['id'] for s in data['shifts'] if s['id'] in data['focus_ids'])
    rules = h.get('coordinator', f"/api/workspaces/{w['id']}/shifts/{sid}/rules")
    row = next(r for r in rules['people'][0]['rules'] if r['id'] == g['id'])
    assert rules['fresh'] and row['status'] == 'warning' and 'aim about 2' in row['messages'][0]
    mapped = next(r for r in h.get('coordinator', f"/api/workspaces/{w['id']}/rule-map")['rules'] if r['id'] == g['id'])
    assert mapped['status'] == {'state': 'warning', 'errors': 0, 'warnings': 2}


def test_rule_map_reads_a_check_as_fresh_when_a_balance_goal_has_history(h):
    """The rule map's own snapshot omits fairness history; its freshness must still use the canonical fingerprint."""
    from tests.test_rule_templates import activate, propose
    a = h.seed['people'][0]
    limit = activate(h, propose(h, {'kind': 'max_hours', 'hours': 6, 'window': 'day'}, name='Daily hours'))
    w = h.workspace('September draft', '2026-09-01', '2026-10-01')
    h.shift(w, '2026-09-06T08:00:00+03:00', '2026-09-06T16:00:00+03:00', people=[a])
    mapped = h.get('coordinator', f"/api/workspaces/{w['id']}/rule-map")
    assert mapped['checked'] and mapped['fresh']
    row = next(r for r in mapped['rules'] if r['id'] == limit['id'])
    assert row['status']['state'] == 'breach' and [d[0] for d in row['short_days']] == [5]


@pytest.mark.parametrize('role', ['auditor', 'programme_lead'])
def test_auditors_and_programme_leads_read_fairness_and_wishes_but_change_nothing(h, role):
    from tests.governance_support import identity
    if role == 'programme_lead':
        identity(h, role, ['programme_lead'])
    a, b, c = h.seed['people']
    g = goal(h, metric='duties', period='month')
    h.publish(session(h, h.workspace(), 4, [a, b]))
    goals = h.get(role, '/api/fairness/goals')
    card = next(x for x in goals['goals'] if x['policy_id'] == g['id'])
    assert {r['person_id'] for r in card['people']} == {a, b, c} and card['score'] is not None and card['trend']
    draft = session(h, h.workspace(start='2026-10-11', end='2026-10-18'), 11, [a])
    assert h.get(role, '/api/fairness/goals?workspace_id=' + draft['id'])['workspace']['id'] == draft['id']
    period = 'starts_on=2026-10-01&ends_on=2026-11-01'
    assert len(h.get(role, '/api/fairness/distribution?metric=duties&group_by=category_ids&' + period)['people']) == 3
    assert len(h.get(role, '/api/fairness/exposure?' + period)['rows']) == 3
    assert h.get(role, f'/api/fairness/sessions?person_id={b}&' + period)['publications']
    definition = {'severity': 'advisory', 'rule': {'kind': 'balance_goal', 'metric': 'duties', 'period': 'month'}}
    assert h.client(role).post('/api/fairness/preview', json={'workspace_id': draft['id'], 'definition': definition}).status_code == 403
    h.command('staff', 'wish.create', {'person_id': a, 'first_day': '2026-11-02', 'last_day': '2026-11-02', 'kind': 'prefer_off', 'points': 1})
    wishes = h.get(role, '/api/wishes?start=2026-11-01&end=2026-12-01')['wishes']
    assert [x['person_id'] for x in wishes] == [a]
    wish = {'person_id': a, 'first_day': '2026-11-03', 'last_day': '2026-11-03', 'kind': 'prefer_off', 'points': 1}
    h.command(role, 'wish.create', wish, status=403)
    h.command(role, 'wish.update', wish, wishes[0]['id'], wishes[0]['revision'], status=403)
    h.command(role, 'wish.withdraw', {'reason': 'Not mine'}, wishes[0]['id'], wishes[0]['revision'], status=403)
    h.command(role, 'policy.create', {'name': 'Goal', 'owner': 'Test', 'source': 'Test', 'starts_on': '2026-10-01',
                                      'definition': {**definition, 'scope': {}}}, status=403)
