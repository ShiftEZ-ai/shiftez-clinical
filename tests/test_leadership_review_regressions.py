"""Lawful internal-presence gaps from the independent leadership review."""
from tests.historical_staffing import retained_staffing
import copy

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.leadership import lead_context
from packages.contracts import schemas as s
from packages.domain import leadership
from packages.domain.conditions import active_rules
from packages.domain.generation import generate as greedy
from packages.domain.rule_map import _reach_leadership
from packages.domain.time import aware
from packages.domain.validation import evaluate, finding_observations
from .test_governed_leadership import refresh_leadership_baseline  # noqa: F401 (autouse fixture)
from .test_governed_leadership import START, END, activate, context_actor, line, prepared, proposal

pytestmark = pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)

NINE = '2026-10-04T09:00:00+03:00'
TEN = '2026-10-04T10:00:00+03:00'
ELEVEN = '2026-10-04T11:00:00+03:00'


def presence(gap_start, gap_end):
    return [{'start': START, 'end': gap_start}, {'start': gap_end, 'end': END}]


def continuous(h, *, flagged=False, maximum=2, who=None):
    a, b, c = h.seed['people']
    typ, w = prepared(h, [a, b, c])
    approved = activate(h, proposal(h, [line(typ['id'], [a, b] if who is None else who,
        maximum=maximum, continuity='continuous')]))
    data = h.snapshot(w)
    for p in data['shifts'][0]['participants']:
        if p['person_id'] in {a, b}:
            p.update(segments=presence(NINE, TEN) if p['person_id'] == a else presence(TEN, ELEVEN),
                     team_lead=flagged)
        s.ParticipantInput.model_validate({k: p[k] for k in s.ParticipantInput.model_fields if k in p})
    return data, approved


@pytest.mark.parametrize('minimum', [0, 1])
def test_every_whole_duty_flag_requires_continuity_even_when_count_is_satisfied(h, minimum):
    a, b, c = h.seed['people']
    typ, w = prepared(h, [a, b, c])
    approved = activate(h, proposal(h, [line(typ['id'], minimum=minimum, maximum=None)]))
    data = h.snapshot(w)
    shift = data['shifts'][0]
    for p in shift['participants']:
        p['team_lead'] = p['person_id'] in {a, b}
        if p['person_id'] == a:
            p['segments'] = presence(NINE, TEN)
    findings = [f for f in finding_observations(evaluate(data)) if f['severity'] == 'error']
    assert {f['code'] for f in findings} == {'LEAD_NOT_QUALIFIED'}
    assert {f['person_id'] for f in findings} == {a}
    assert all(f['rule_id'] == approved['id'] and f['detail']['lead_sources'] for f in findings)
    # The same ordinary attendance is valid; an optional role is never compulsory.
    next(p for p in shift['participants'] if p['person_id'] == a)['team_lead'] = False
    assert evaluate(data)['valid']


def test_whole_duty_only_requires_presence_in_required_windows(h):
    a, b, c = h.seed['people']
    typ, w = prepared(h, [a, b, c])
    activate(h, proposal(h, [line(typ['id'], minimum=0, maximum=None, start_time=lo, end_time=hi)
                            for lo, hi in [('08:00', '09:00'), ('10:00', '12:00')]]))
    data = h.snapshot(w)
    next(p for p in data['shifts'][0]['participants'] if p['person_id'] == a).update(
        segments=presence(NINE, TEN), team_lead=True)
    assert evaluate(data)['valid']


@pytest.mark.parametrize('stage', ['submit', 'approve', 'publish'])
def test_optional_whole_duty_context_and_publication_agree(h, stage, monkeypatch):
    a, b, c = h.seed['people']
    typ, _ = prepared(h)
    activate(h, proposal(h, [line(typ['id'], minimum=0, maximum=None)]))
    w = retained_staffing(h, h.workspace('Optional whole-duty continuity'), typ=typ['id'], people=[
        {'person_id': a, 'start': START, 'end': END, 'segments': presence(NINE, TEN), 'team_lead': True}, b, c])['workspace']
    shift = h.snapshot(w)['shifts'][0]
    value = {k: shift[k] for k in s.ShiftInput.model_fields if k in shift}
    value['participants'] = [{k: p[k] for k in s.ParticipantInput.model_fields if k in p} for p in shift['participants']]
    with transaction(h.app.state.engine) as db:
        context = lead_context(db, context_actor(h), w['id'], s.LeadContextInput(
            shift=value, shift_id=shift['id'], person_ids=[a]))
    assert context.people[0].can_lead is False
    # Emulate an unsafe candidate submitted/approved by the previous validator.
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, w['id']).validation = None
    with monkeypatch.context() as old_validator:
        old_validator.setattr(leadership, 'covers_required', lambda *args: True)
        if stage in {'approve', 'publish'}:
            w = h.lifecycle(w, 'submit')['workspace']
        if stage == 'publish':
            w = h.lifecycle(w, 'approve', 'approver')['workspace']
    if stage != 'submit':
        with transaction(h.app.state.engine, write=True) as db:
            historical = db.get(m.Workspace, w['id'])
            assert historical.validation['valid']
            historical.validation = {**historical.validation, 'engine_version': 'previous-continuity-validator'}
    assert h.lifecycle(w, stage, 'approver' if stage == 'approve' else 'coordinator', status=422)['error']['code'] == 'VALIDATION_FAILED'
    if stage != 'submit':
        w = h.lifecycle(w, 'reopen')['workspace']
    next(p for p in value['participants'] if p['person_id'] == a)['team_lead'] = False
    w = h.command('coordinator', 'shift.update', {'workspace_id': w['id'], 'shift_id': shift['id'],
        **value}, revision=w['revision'])['workspace']
    assert h.publish(w)['publication_id']


@pytest.mark.parametrize('already_flagged', [False, True])
def test_greedy_fills_each_continuous_lead_gap(h, already_flagged):
    data, _ = continuous(h)
    a, b, c = h.seed['people']
    if already_flagged:
        next(p for p in data['shifts'][0]['participants'] if p['person_id'] == a)['team_lead'] = True
    assert {f['code'] for f in evaluate(data)['findings'] if f['severity'] == 'error'} == {'LEAD_GAP'}
    result = greedy(data, 20)
    assert result['validation']['valid'], result['validation']['findings']
    assert {r['participant']['person_id'] for r in result['additions'] if r['participant'].get('team_lead')} == (
        {b} if already_flagged else {a, b})


@pytest.mark.parametrize('guard', ['maximum', 'fixed', 'who'])
def test_greedy_handover_keeps_maximum_fixed_and_who_guards(h, guard):
    a, b, c = h.seed['people']
    data, _ = continuous(h, maximum=1 if guard == 'maximum' else 2, who=[a] if guard == 'who' else None)
    if guard == 'fixed':
        next(p for p in data['shifts'][0]['participants'] if p['person_id'] == b)['fixed'] = True
    result = greedy(data, 20)
    findings = list(finding_observations(result['validation']))
    assert any(f['code'] == 'LEAD_GAP' and 'required' in f['detail'] for f in findings)
    assert not any(f['code'] == 'LEAD_GAP' and 'maximum' in f['detail'] for f in findings)
    flags = {r['participant']['person_id'] for r in result['additions'] if r['participant'].get('team_lead')}
    assert flags <= ({a, b} if guard == 'maximum' else {a})
    assert len(flags) <= 1


def test_greedy_prioritizes_a_new_leader_despite_an_existing_flag(h):
    data, _ = continuous(h)
    a, b, c = h.seed['people']
    shift = data['shifts'][0]
    shift['participants'] = [p for p in shift['participants'] if p['person_id'] != b]
    next(p for p in shift['participants'] if p['person_id'] == a)['team_lead'] = True
    result = greedy(data, 20)
    assert result['validation']['valid'], result['validation']['findings']
    assert [(r['participant']['person_id'], r['participant'].get('team_lead')) for r in result['additions']] == [(b, True)]


def test_reach_counts_every_continuously_covered_presence_interval(h):
    data, approved = continuous(h, flagged=True)
    assert evaluate(data)['valid']
    policy = next(p for p in active_rules(data) if p['id'] == approved['id'])
    rows = _reach_leadership(data, policy, aware(START), aware(END))
    expected = [(START, NINE, 2), (NINE, TEN, 1), (TEN, ELEVEN, 1), (ELEVEN, END, 2)]
    for comparison in ('minimum', 'maximum'):
        assert [(aware(r['starts_on']), aware(r['ends_on']), r['actual']) for r in rows
                if r['comparison'] == comparison] == [(aware(lo), aware(hi), n) for lo, hi, n in expected]
    assert all(r['lead_sources'][0]['policy_id'] == approved['id'] for r in rows)


@pytest.mark.parametrize('boundary', ['availability', 'restriction', 'authority'])
def test_reach_and_validator_share_availability_restriction_and_authority_cuts(h, boundary):
    a, b, c = h.seed['people']
    typ, w = prepared(h, [a, b, c])
    approved = activate(h, proposal(h, [line(typ['id'], [a], continuity='continuous')]))
    if boundary == 'availability':
        h.command('coordinator', 'availability.record', {'person_id': a, 'start': NINE, 'end': TEN,
            'kind': 'actual_absence', 'private_reason': 'Fictional interval boundary'})
    if boundary == 'restriction':
        h.policy({'kind': 'restriction', 'start_time': '09:00', 'end_time': '10:00'}, {'person_id': a})
    data = h.snapshot(w)
    shift = data['shifts'][0]
    next(p for p in shift['participants'] if p['person_id'] == a)['team_lead'] = True
    start, end = START, END
    expected = [(START, NINE, 1), (NINE, TEN, 0), (TEN, END, 1)]
    if boundary == 'authority':
        start, midnight, end = '2026-10-04T22:00:00+03:00', '2026-10-05T00:00:00+03:00', '2026-10-05T02:00:00+03:00'
        shift.update(start=start, end=end)
        for p in shift['participants']:
            p.update(start=start, end=end)
        for membership in data['memberships']:
            if membership['person_id'] == a:
                membership['ends_on'] = '2026-10-05'
        expected = [(start, midnight, 1), (midnight, end, 0)]
    policies = active_rules(data)
    policy = next(p for p in policies if p['id'] == approved['id'])
    rows = _reach_leadership({**data, 'policies': policies}, policy, aware(start), aware(end))
    assert [(aware(r['starts_on']), aware(r['ends_on']), r['actual']) for r in rows if r['comparison'] == 'minimum'] == [
        (aware(lo), aware(hi), n) for lo, hi, n in expected]
    gaps = [f for f in finding_observations(evaluate(data)) if f['code'] == 'LEAD_GAP' and 'required' in f['detail']]
    assert [(aware(f['start']), aware(f['end'])) for f in gaps] == [
        (aware(lo), aware(hi)) for lo, hi, n in expected if n == 0]
    assert all(r['actual'] == 1 for r in rows if r['comparison'] == 'maximum')


def test_whole_duty_why_reports_empty_qualified_pool_and_keeps_facts(h):
    a, b, c = h.seed['people']
    typ, w = prepared(h, [a, b, c])
    approved = activate(h, proposal(h, [line(typ['id'], [a], start_time='08:00', end_time='10:00'),
                                       line(typ['id'], [b], start_time='10:00', end_time='12:00')]))
    data = h.snapshot(w)
    shift = data['shifts'][0]
    for p in shift['participants']:
        p['team_lead'] = p['person_id'] in {a, b}
    assert not any(leadership.qualified(data, pid, shift, aware(START), aware(END)) for pid in data['people'])
    gaps = [f for f in finding_observations(evaluate(data)) if f['code'] == 'LEAD_GAP' and 'required' in f['detail']]
    assert len(gaps) == 2
    for f in gaps:
        assert f['cause']['code'] == 'no_eligible_people'
        assert f['cause']['eligible_people'] == f['cause']['possible'] == f['cause']['actual'] == f['detail']['available'] == 0
        assert [hint['action'] for hint in f['fix_hints']] == ['open_eligibility']
        assert f['detail']['lead_sources'][0]['policy_id'] == approved['id']


def test_continuous_who_still_applies_during_reserved_off_cover_time(h):
    from packages.domain.cp_sat import generate
    from packages.domain.fairness import lead_eligible
    a, b, c = h.seed['people']
    typ, w = prepared(h, [a, b, c])
    activate(h, proposal(h, [line(typ['id'], who, maximum=2, continuity='continuous', start_time=lo, end_time=hi)
        for lo, hi, who in [('08:00', '09:00', [a, b]), ('09:00', '10:00', [b]), ('10:00', '12:00', [a, b])]]))
    data = h.snapshot(w)
    next(p for p in data['shifts'][0]['participants'] if p['person_id'] == a).update(segments=presence(NINE, TEN), team_lead=True)
    assert any(f['code'] == 'LEAD_NOT_QUALIFIED' and f['person_id'] == a for f in evaluate(data)['findings'])
    assert lead_eligible(data, [], {a, b, c}) == {b}
    models = []
    assert generate(copy.deepcopy(data), 20, workers=1, feasibility_only=True, _on_model=models.append)['status'] == 'model_ready'
    names = [v.name for v in models[0].proto.variables if v.name.startswith('lead:')]
    assert any(b in name for name in names) and not any(a in name for name in names)
