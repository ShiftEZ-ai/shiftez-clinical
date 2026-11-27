"""Rule simplification, phase 3 (docs/design/rule-simplification.md section 8): a range quota, lines with their own
settings (supervision limits, companion covers, supervised allocation lines) proven identical; the changes that are
not identical (an implied companion line dropped, a balancing preference split into an order line and a Balance goal,
a weekly limit as a ceiling line, ended rules retired) proposed apart with what changes; clinic permissions that end
while rules still staff the clinic; undo for all of it."""
import itertools
from datetime import date
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from packages.contracts.schemas import ENDED_REASON, PolicyDefinition, PolicyInput
from packages.domain.conditions import active_rules
from packages.domain.rule_groups import canonical_model, ended, expanded, same_checks, same_model, simplify
from packages.domain.validation import evaluate
from .test_rule_simplification import PERSON, SERVICE, WEEK_START, configure_baseline_rehearsal, quota, rule
from .test_rule_templates import activate, propose

ids = (f'00000000-0000-4000-a000-{n:012d}' for n in itertools.count(1))
RANK = {name: next(ids) for name in ('Consultant', 'Fellow', 'Resident', 'Junior', 'Student')}
UCC = [SERVICE['Physical UCC'], SERVICE['Virtual UCC']]
TODAY = '2026-09-29'


def same(members, item):
    made = {**item['payload'], 'id': 'G', 'status': 'active', 'active': True, 'kind': item['kind'], 'department_id': 'd'}
    return expanded(members, {m['id']: 'G' for m in members}, WEEK_START) == expanded([made], None, WEEK_START)


def by_shape(items):
    return {i['shape']: i for i in items}


# The contract ------------------------------------------------------------------------------------------------------

def test_a_range_quota_is_checked_as_its_minimum_and_maximum():
    low = rule('Internship weekly clinical minimum', quota('2026-09-01', '2027-01-01', 6, 'minimum', window_alignment='calendar_week'),
               {'service_ids': UCC}, starts_on='2026-09-01', ends_on='2027-01-01')
    high = rule('Internship weekly clinical maximum', quota('2026-09-01', '2027-01-01', 7, 'maximum', window_alignment='calendar_week'),
                {'service_ids': UCC}, starts_on='2026-09-01', ends_on='2027-01-01')
    groups, changes = simplify([low, high], {}, WEEK_START, today=TODAY)
    [item] = groups
    assert not changes and item['shape'] == 'range' and item['name'] == 'Internship weekly clinical range'
    r = item['payload']['definition']['rule']
    assert (r['comparison'], r['amount'], r['maximum_amount']) == ('range', 6, 7)
    assert same([low, high], item)
    made = {**item['payload'], 'id': 'R', 'status': 'active', 'active': True, 'kind': 'block_quota'}
    assert sorted((c['id'], c['definition']['rule']['comparison'], c['definition']['rule']['amount']) for c in active_rules([made])) == \
        [('R', 'maximum', 7), ('R', 'minimum', 6)]
    for bad, message in (({'maximum_amount': None}, 'both numbers'), ({'maximum_amount': 5}, 'most cannot be below'),
                         ({'comparison': 'maximum'}, 'both numbers'), ({'metric': 'duties', 'maximum_amount': 7.5}, 'whole-number')):
        with pytest.raises(ValidationError, match=message):
            PolicyDefinition.model_validate({**item['payload']['definition'], 'rule': {**r, **bad}})
    # A line may set its own numbers (at least, at most, target), checked like the rule's.
    PolicyDefinition.model_validate({**item['payload']['definition'], 'listed_only': True,
                                     'per_person': [{'person_id': PERSON['Kopofiku'], 'amount': 3, 'settings': {'maximum_amount': 5, 'target_amount': 4}}]})
    with pytest.raises(ValidationError, match='Line 1: The most cannot be below'):
        PolicyDefinition.model_validate({**item['payload']['definition'], 'listed_only': True,
                                         'per_person': [{'person_id': PERSON['Kopofiku'], 'amount': 8}]})


def test_line_settings_follow_the_contract():
    base = {'rule': {'kind': 'minimum_cover', 'minimum': 1}, 'scope': {'category_ids': [RANK['Consultant']]}, 'listed_only': True}
    line = {'service_ids': UCC, 'amount': 2, 'settings': {'when_person_ids': [PERSON['Kopofiku']]}}
    PolicyDefinition.model_validate({**base, 'per_person': [line]})
    with pytest.raises(ValidationError, match='cannot set its own supervisor ids'):
        PolicyDefinition.model_validate({**base, 'per_person': [{**line, 'settings': {'supervisor_ids': [PERSON['Kopofiku']]}}]})
    with pytest.raises(ValidationError, match='cannot set its own'):  # only rules for their lines only
        PolicyDefinition.model_validate({'rule': {'kind': 'allocation_preference', 'priority': 1}, 'severity': 'advisory',
                                         'per_person': [{'person_id': PERSON['Kopofiku'], 'settings': {'supervisor_ids': [PERSON['Badr']]}}]})
    supervision = {'rule': {'kind': 'supervision_limit', 'mode': 'capacity', 'trainee_category_ids': [RANK['Student']], 'maximum': 2},
                   'listed_only': True, 'per_person': [{'person_id': PERSON['Kopofiku'], 'settings': {'mode': 'allowed_ranks', 'trainee_category_ids': [RANK['Junior']]}}]}
    PolicyDefinition.model_validate(supervision)
    with pytest.raises(ValidationError, match='allowed ranks has no number'):
        PolicyDefinition.model_validate({**supervision, 'per_person': [{**supervision['per_person'][0], 'amount': 1}]})
    [copy] = active_rules([{'id': 'S', 'status': 'active', 'active': True, 'definition': PolicyDefinition.model_validate(supervision).model_dump(mode='json')}])
    assert copy['definition']['rule'] | {} == {'kind': 'supervision_limit', 'mode': 'allowed_ranks', 'trainee_category_ids': [RANK['Junior']],
                                             'maximum': None, 'overflow_maximum': 0, 'overflow_category_ids': []}
    [cover] = active_rules([{'id': 'C', 'status': 'active', 'active': True, 'definition': PolicyDefinition.model_validate({**base, 'per_person': [line]}).model_dump(mode='json')}])
    assert cover['definition']['rule']['when_person_ids'] == [PERSON['Kopofiku']] and cover['definition']['rule']['minimum'] == 2


# Identical: one table ------------------------------------------------------------------------------------------------

def supervision_family():
    willow = PERSON['Carla']
    allowed = {'kind': 'supervision_limit', 'mode': 'allowed_ranks', 'trainee_category_ids': [RANK['Junior'], RANK['Student']]}
    return [
        rule('Dr. Carla · Allowed trainee ranks', allowed, {'person_id': willow, 'service_ids': UCC}, '2026-10-01', '2027-01-01'),
        rule('Dr. Carla · Junior-only supervision', allowed, {'person_id': willow, 'service_ids': UCC}, '2026-10-01', '2027-01-01'),
        rule('UCC supervision restriction', {'kind': 'supervision_limit', 'mode': 'capacity', 'trainee_category_ids': [RANK['Resident']], 'maximum': 0},
             {'person_ids': [PERSON['Kopofiku'], willow], 'service_ids': UCC}, '2026-10-01', '2027-01-01'),
        rule('Non-student trainee places per supervisor', {'kind': 'supervision_limit', 'mode': 'capacity', 'maximum': 2, 'overflow_maximum': 2,
                                                           'trainee_category_ids': [RANK['Fellow'], RANK['Resident']], 'overflow_category_ids': [RANK['Resident']]},
             {}, '2026-09-01', None, listed_only=True, merged_ids=[next(ids)],
             per_person=[{'service_ids': [SERVICE['Clinic A']], 'amount': 1}, {'service_ids': [SERVICE['Clinic B']]}]),
    ]


def test_supervision_limits_become_one_table_that_checks_the_same():
    family = supervision_family()
    groups, changes = simplify(family, {}, WEEK_START, today=TODAY)
    [item] = [g for g in groups if g['shape'] == 'supervision']
    assert item['name'] == 'Supervision limits' and sorted(item['rule_ids']) == sorted(p['id'] for p in family)
    assert same(family, item)  # the duplicate is one line; the two supervisors of one limit are two lines
    lines = item['payload']['definition']['per_person']
    assert len(lines) == 5 and item['payload']['ends_on'] is None
    assert {(r.get('settings') or {}).get('mode') for r in lines} >= {'capacity'}
    PolicyInput.model_validate(item['payload'])


def test_companion_covers_join_and_an_implied_line_is_a_change():
    seniors = {'category_ids': [RANK['Consultant'], RANK['Fellow']], 'service_ids': UCC}
    cover = lambda who, n, name: rule(name, {'kind': 'minimum_cover', 'minimum': n, 'when_person_ids': [PERSON[who]]}, seniors, '2026-10-01', '2027-01-01')  # noqa: E731
    pebble, willow2, willow1 = cover('Quartz', 1, 'Dr. Quartz · UCC companion cover'), cover('Carla', 2, 'Dr. Carla · Other senior cover required'), \
        cover('Carla', 1, 'Dr. Carla · UCC companion cover')
    groups, changes = simplify([pebble, willow2], {}, WEEK_START, today=TODAY)
    assert [g['shape'] for g in groups] == ['companions'] and not changes and same([pebble, willow2], groups[0])
    groups, changes = simplify([pebble, willow2, willow1], {'people': {PERSON['Carla']: 'Dr. Carla'}}, WEEK_START, today=TODAY)
    [change] = changes
    assert not groups and change['shape'] == 'companions' and change['name'] == 'Companion cover' and len(change['rule_ids']) == 3
    assert 'when Dr. Carla is on duty, at least 1' in change['was'] and 'dropped' in change['now']
    lines = change['payload']['definition']['per_person']
    assert sorted((r['settings']['when_person_ids'][0], r.get('amount') or 1) for r in lines) == sorted([(PERSON['Quartz'], 1), (PERSON['Carla'], 2)])
    # The implied line checks nothing the stronger one lets through: every session short of 1 is short of 2.
    kept = {**change['payload'], 'id': 'G', 'status': 'active', 'active': True, 'kind': 'minimum_cover'}
    assert expanded([kept], None, WEEK_START) == expanded([pebble, willow2], {pebble['id']: 'G', willow2['id']: 'G'}, WEEK_START)


def test_priorities_join_the_allocation_order_and_a_balancing_one_becomes_a_goal():
    ranks = {'category_ids': [RANK['Fellow'], RANK['Resident']]}
    order = rule('Allocation order', {'kind': 'allocation_preference', 'priority': 0, 'session_priority': 0}, ranks, '2026-09-01', None,
                 severity='advisory', listed_only=True, line_number='session_priority', merged_ids=[next(ids)],
                 per_person=[{'service_ids': UCC, 'amount': 50}, {'service_ids': [SERVICE['Clinic A']], 'amount': 30}])
    consultants = rule('Priority consultant clinics', {'kind': 'allocation_preference', 'priority': 0, 'session_priority': 5,
                                                       'supervisor_ids': [PERSON['Emad']]}, {**ranks, 'service_ids': [SERVICE['Clinic B']]},
                       '2026-09-01', None, severity='advisory')
    groups, changes = simplify([order, consultants], {}, WEEK_START, today=TODAY)
    [item] = groups
    assert not changes and item['shape'] == 'order' and same([order, consultants], item)
    assert [r['amount'] for r in item['payload']['definition']['per_person']] == [50, 30, 5]
    director = rule('Programme director team exposure', {'kind': 'allocation_preference', 'priority': 10, 'supervisor_ids': [PERSON['Kopofiku'], PERSON['Badr']],
                                                         'balance_by': 'supervisor_group', 'balance_weight': 10},
                    {**ranks, 'service_ids': [SERVICE['Clinic B']]}, '2026-09-01', None, severity='advisory')
    groups, changes = simplify([order, consultants, director], {}, WEEK_START, today=TODAY)
    [change] = changes
    assert not groups and change['shape'] == 'order' and len(change['rule_ids']) == 3
    [goal] = change['extra']
    assert goal['definition']['rule']['kind'] == 'balance_goal' and goal['definition']['merged_ids'] == [director['id']]
    assert 'Balance goal' in change['now'] and 'priority 10' in change['was']
    line = next(r for r in change['payload']['definition']['per_person'] if r.get('amount') == 10)
    assert line['settings'] == {'supervisor_ids': [PERSON['Kopofiku'], PERSON['Badr']]}


def test_a_weekly_limit_becomes_a_line_of_the_weekly_ceiling():
    ceiling = rule('Weekly duty ceiling', quota('2026-08-23', '2027-01-03', 1, 'maximum', 'duties', window_alignment='calendar_week'), {},
                   listed_only=True, merged_ids=[next(ids)], per_person=[{'programme_id': next(ids), 'service_ids': UCC}])
    limit = rule('Resident weekly UCC limit', {'kind': 'max_duties', 'maximum': 1, 'window': 'week', 'window_days': 7},
                 {'category_ids': [RANK['Resident']], 'service_ids': UCC}, '2026-10-01', '2027-01-01')
    groups, changes = simplify([ceiling, limit], {}, WEEK_START, today=TODAY)
    [change] = changes
    assert not groups and change['shape'] == 'ceiling' and change['rule_ids'] == [ceiling['id'], limit['id']]
    added = change['payload']['definition']['per_person'][-1]
    assert added == {'category_ids': [RANK['Resident']], 'service_ids': UCC, 'starts_on': '2026-10-01', 'ends_on': '2027-01-01'}
    PolicyInput.model_validate(change['payload'])


def test_ended_rules_are_retired_unless_a_rota_covers_them():
    old = rule('Student capacity per supervisor', {'kind': 'supervision_limit', 'mode': 'capacity', 'trainee_category_ids': [RANK['Student']], 'maximum': 2},
               {}, '2026-08-23', '2026-09-18')
    assert ended(old, TODAY, []) and not ended(old, '2026-09-17', []) and not ended(old, TODAY, [('2026-09-01', '2026-10-01')])
    assert ended(old, TODAY, [('2026-09-18', '2026-10-01')])  # a rota from the end date on does not cover it
    _, changes = simplify([old], {}, WEEK_START, today=TODAY)
    assert [(c['shape'], c['retire']) for c in changes] == [('ended', True)] and 'payload' not in changes[0]
    assert simplify([old], {}, WEEK_START, today=TODAY, rotas=[('2026-09-01', '2026-10-01')]) == ([], [])


# Through the app: preview, propose, activate, the same checks, retire ended, restore, open permissions, undo --------------

@pytest.mark.parametrize('mode', ['fill_gaps', 'rebalance'])
def test_phase_three_through_governance_checks_the_same_and_undoes(h, mode):
    from sqlalchemy import select
    from apps.api import models as m
    from apps.api.db import transaction
    a, b, c = h.seed['people']
    clinic, other = h.seed['services'][:2]
    w = h.workspace(start='2026-10-04', end='2026-10-11')
    for day in ('04', '05', '06'):
        w = h.shift(w, f'2026-10-{day}T08:00:00+03:00', f'2026-10-{day}T12:00:00+03:00', people=[a, b])['workspace']
    low = activate(h, propose(h, quota('2026-10-04', '2026-10-18', 2, 'minimum', 'duties', window_alignment='calendar_week'), {'service_ids': [clinic]},
                              name='Weekly clinic minimum', starts_on='2026-10-04', ends_on='2026-10-18'))
    high = activate(h, propose(h, quota('2026-10-04', '2026-10-18', 2, 'maximum', 'duties', window_alignment='calendar_week'), {'service_ids': [clinic]},
                               name='Weekly clinic maximum', starts_on='2026-10-04', ends_on='2026-10-18'))
    covers = [activate(h, propose(h, {'kind': 'minimum_cover', 'minimum': n, 'when_person_ids': [who]}, {'service_ids': [clinic]},
                                  name=name, starts_on='2026-10-01', ends_on='2027-01-01'))
              for who, n, name in ((a, 3, 'A · Companion cover'), (b, 2, 'B · Other cover required'))]
    ended_rule = activate(h, propose(h, {'kind': 'max_duties', 'maximum': 3, 'window': 'week', 'window_days': 7}, {'service_ids': [other]},
                                     name='Old clinic limit', starts_on='2026-08-01', ends_on='2026-09-01'))
    before = h.snapshot(w)
    before['generation_options'] = {'mode': mode}
    checked = evaluate(before)

    preview = h.get('coordinator', '/api/policies/groups')
    groups = {g['shape']: g for g in preview['groups'] if 'payload' in g}
    assert set(groups) == {'range', 'companions'}
    [gone] = [x for x in preview['changes'] if x.get('retire')]
    assert gone['rule_ids'] == [ended_rule['id']]
    h.command('coordinator', 'policy.group', {'groups': [], 'changes': [gone['rule_ids']], 'reason': 'Ended'}, status=422)
    result = h.command('coordinator', 'policy.group', {'groups': [g['rule_ids'] for g in groups.values()], 'reason': 'Phase 3 of one rule for similar rules'})
    for rec in result['records']:
        activate(h, rec)
    after = h.snapshot(w)
    after['generation_options'] = {'mode': mode}
    id_map = {i: r['id'] for r in result['records'] for i in r['definition']['merged_ids']}
    diff = same_checks(checked, evaluate(after), id_map)
    assert not diff['findings_only_before'] and not diff['findings_only_after'] and diff['quota_equal']
    assert expanded(before['policies'], id_map, WEEK_START) == expanded(after['policies'], None, WEEK_START)
    assert same_model(canonical_model(before), canonical_model(after), id_map)

    # An approver retires the ended rule (nothing it replaced comes back), and can restore it.
    live = next(p for p in h.get('approver', '/api/policies?status=active')['policies'] if p['id'] == ended_rule['id'])
    out = h.command('approver', 'policy.retire', {'reason': f'{ENDED_REASON}: phase 3'}, live['id'], live['revision'])
    assert out['restored_ids'] == []
    back = h.command('approver', 'policy.restore', {'reason': 'Needed again'}, live['id'], out['record']['revision'])
    assert back['record']['status'] == 'active'
    # A rule another rule in force replaced cannot be restored: retire that one instead.
    with transaction(h.app.state.engine) as db:
        replaced = db.get(m.Policy, low['id'])
        assert replaced.status == 'retired'
    h.command('approver', 'policy.restore', {'reason': 'Not this one'}, low['id'], replaced.revision, status=409)

    # Undo: retiring each new rule brings back what it replaced.
    for rec in result['records']:
        live = next(p for p in h.get('approver', '/api/policies?status=active')['policies'] if p['id'] == rec['id'])
        restored = h.command('approver', 'policy.retire', {'reason': 'Undo phase 3'}, live['id'], live['revision'])['restored_ids']
        assert sorted(restored) == sorted(rec['definition']['merged_ids'])
    assert {f['code'] for f in evaluate(h.snapshot(w))['findings']} == {f['code'] for f in checked['findings']}
    assert {p['id'] for p in h.get('approver', '/api/policies?status=active')['policies']} >= {low['id'], high['id'], *(x['id'] for x in covers)}


def test_clinic_permissions_that_end_while_rules_staff_the_clinic_open_until_changed(h):
    from sqlalchemy import select
    from apps.api import models as m
    from apps.api.db import transaction
    from apps.api.services.catalogue import eligibility_gaps
    clinic = h.seed['services'][0]
    with transaction(h.app.state.engine, write=True) as db:
        rows = list(db.scalars(select(m.Eligibility).where(m.Eligibility.service_id == clinic)))
        for r in rows:
            r.ends_on = date(2026, 10, 1)
        department = rows[0].department_id
    with transaction(h.app.state.engine) as db:
        assert eligibility_gaps(db, department) == []  # no rule staffs the clinic after that
    # A target that merely counts the clinic is not a gap (rotators' dated permissions end with their rotation).
    activate(h, propose(h, quota('2026-10-04', '2026-10-18', 2, 'target', 'duties'), {'service_ids': [clinic]}, severity='advisory',
                        name='Clinic target', starts_on='2026-10-04', ends_on='2026-10-18'))
    with transaction(h.app.state.engine) as db:
        assert eligibility_gaps(db, department) == []
    activate(h, propose(h, {'kind': 'minimum_cover', 'minimum': 1}, {'service_ids': [clinic]}, name='Clinic cover',
                        starts_on='2026-09-01', ends_on='2027-01-01'))
    with transaction(h.app.state.engine) as db:
        gaps = eligibility_gaps(db, department)
    assert {g['id'] for g in gaps} == {r.id for r in rows}
    g = gaps[0]
    h.command('coordinator', 'eligibility.extend', {'ends_on': None, 'reason': 'Trial month permissions'}, g['id'], g['revision'], status=403)
    out = h.command('credentialer', 'eligibility.extend', {'ends_on': None, 'reason': 'Trial month permissions'}, g['id'], g['revision'])
    assert out['record']['ends_on'] is None
    h.command('credentialer', 'eligibility.extend', {'ends_on': '2026-11-01', 'reason': 'Earlier'}, g['id'], out['record']['revision'], status=422)
    undone = h.command('credentialer', 'eligibility.end', {'ends_on': '2026-10-01', 'reason': 'Undo'}, g['id'], out['record']['revision'])
    assert undone['record']['ends_on'] == '2026-10-01'


def test_baseline_runs_every_round_opens_permissions_and_undoes_it_all(h, monkeypatch):
    import scripts.group_rules as tool
    from sqlalchemy import select
    from apps.api import models as m
    from apps.api.db import transaction
    from apps.api.serialization import record
    clinic = h.seed['services'][0]
    for comparison, n in (('minimum', 1), ('maximum', 3)):
        activate(h, propose(h, quota('2026-10-04', '2026-10-18', n, comparison, 'duties', window_alignment='calendar_week'), {'service_ids': [clinic]},
                            name=f'Weekly clinic {comparison}', starts_on='2026-10-04', ends_on='2026-10-18'))
    gone = activate(h, propose(h, {'kind': 'max_duties', 'maximum': 3, 'window': 'week', 'window_days': 7}, {'service_ids': [clinic]},
                               name='Old clinic limit', starts_on='2026-08-01', ends_on='2026-09-01'))
    activate(h, propose(h, {'kind': 'minimum_cover', 'minimum': 1}, {'service_ids': [clinic]}, name='Clinic cover',
                        starts_on='2026-09-01', ends_on='2027-01-01'))
    with transaction(h.app.state.engine, write=True) as db:
        for r in db.scalars(select(m.Eligibility).where(m.Eligibility.service_id == clinic)):
            r.ends_on = date(2026, 10, 1)
        active_before = sorted(p.id for p in db.scalars(select(m.Policy).where(m.Policy.status == 'active')))
        identities_before = {r.id: record(r) for r in db.scalars(select(m.Identity).where(m.Identity.issuer == tool.SYSTEM_ISSUER))}
    configure_baseline_rehearsal(monkeypatch, tool)
    monkeypatch.setattr('apps.api.services.independence.today', lambda db, department_id: date(2026, 9, 29))
    args = SimpleNamespace(propose=False, undo=False, changes=True, model=False, no_calendar=False, name=[], reason='Baseline rehearsal', only_reason=None)
    tool.baseline(h.app.state.engine, args)  # dry run: rolled back
    with transaction(h.app.state.engine) as db:
        assert sorted(p.id for p in db.scalars(select(m.Policy).where(m.Policy.status == 'active'))) == active_before
        assert {r.id: record(r) for r in db.scalars(select(m.Identity).where(m.Identity.issuer == tool.SYSTEM_ISSUER))} == identities_before
    tool.baseline(h.app.state.engine, SimpleNamespace(**{**vars(args), 'propose': True}))
    with transaction(h.app.state.engine) as db:
        names = {p.name: p for p in db.scalars(select(m.Policy).where(m.Policy.status == 'active'))}
        assert 'Weekly clinic range' in names and 'Old clinic limit' not in names
        assert all(r.ends_on is None for r in db.scalars(select(m.Eligibility).where(m.Eligibility.service_id == clinic)))
        actions = {e.action for e in db.scalars(select(m.AuditEvent).where(m.AuditEvent.detail['baseline'].as_boolean()))} \
            if db.bind.dialect.name == 'postgresql' else {e.action for e in db.scalars(select(m.AuditEvent)) if (e.detail or {}).get('baseline')}
        assert {'policy.group', 'policy.activate', 'policy.retire', 'eligibility.extend'} <= actions
    tool.baseline(h.app.state.engine, SimpleNamespace(**{**vars(args), 'undo': True, 'only_reason': 'Baseline rehearsal', 'reason': 'Undo the rehearsal'}))
    with transaction(h.app.state.engine) as db:
        assert sorted(p.id for p in db.scalars(select(m.Policy).where(m.Policy.status == 'active'))) == active_before
        assert db.get(m.Policy, gone['id']).status == 'active'
        assert all(r.ends_on == date(2026, 10, 1) for r in db.scalars(select(m.Eligibility).where(m.Eligibility.service_id == clinic)))
