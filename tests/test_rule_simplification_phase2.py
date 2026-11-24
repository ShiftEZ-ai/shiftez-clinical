"""Rule simplification, phase 2 (docs/design/rule-simplification.md): rules under different names that are one rule
become one (an allocation order, weekly ceilings for several programmes, staffing floors for several ranks, clinic
placements, exclusions), checked exactly as before (validator findings, quota rows, the rules the scheduler reads and
the CP-SAT model); and the changes that are not identical (a balance as a Balance goal, overtime switched back on)
are separate proposals that say what changes, with undo."""
import itertools
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from packages.contracts.schemas import PolicyDefinition, PolicyInput
from packages.domain.conditions import active_rules, conditions_match
from packages.domain.rule_groups import (_weekend, canonical_model, expanded, goal_conversions, overtime_switch_on, plan, same_checks,
                                         same_model)
from packages.domain.validation import evaluate
from .test_rule_simplification import LABELS, PERSON, PROGRAMME, SERVICE, TYPE, WEEK_START, joined, quota, rule, weeks
from .test_rule_templates import activate, propose

ids = (f'00000000-0000-4000-9000-{n:012d}' for n in itertools.count(1))
RANK = {name: next(ids) for name in ('Consultant', 'Fellow', 'Resident')}


def grouped(name, kind_rule, lines, scope=None, merged=2, **extra):
    """A rule grouped earlier: for its lines only, standing for `merged` rules."""
    return rule(name, kind_rule, scope, listed_only=True, per_person=lines, merged_ids=[next(ids) for _ in range(merged)], **extra)


def same(members, group):
    made = {**group['payload'], 'id': 'G', 'status': 'active', 'active': True, 'kind': group['kind'], 'department_id': 'd'}
    return expanded(members, {m['id']: 'G' for m in members}, WEEK_START) == expanded([made], None, WEEK_START)


def test_an_allocation_order_becomes_one_ordered_rule():
    clinics = list(SERVICE.values())
    ranks = {'category_ids': list(RANK.values())}
    order = [rule(f'Allocation order - {name}', {'kind': 'allocation_preference', 'priority': 0, 'session_priority': priority},
                  {**ranks, 'service_ids': [clinic]}, ends_on=None, severity='advisory')
             for name, clinic, priority in (('ZZ shifts', clinics[0], 50), ('Procedure clinics', clinics[2], 40), ('AA clinics', clinics[3], 20))]
    # A clinic priority with named supervisors is another rule.
    consultants = rule('Priority consultant clinics', {'kind': 'allocation_preference', 'priority': 0, 'session_priority': 5,
                                                       'supervisor_ids': [PERSON['Emad']]}, {**ranks, 'service_ids': clinics[2:]}, ends_on=None, severity='advisory')
    [group] = joined(plan(order + [consultants], LABELS, WEEK_START)).values()
    d = group['payload']['definition']
    assert group['name'] == 'Allocation order' and sorted(group['rule_ids']) == sorted(r['id'] for r in order)
    assert d['line_number'] == 'session_priority' and d['listed_only'] and d['rule']['session_priority'] == 0
    assert [(r['amount'], r['service_ids']) for r in d['per_person']] == [(50, clinics[:1]), (40, clinics[2:3]), (20, clinics[3:])]
    assert same(order, group)
    PolicyInput.model_validate(group['payload'])
    with pytest.raises(ValidationError, match='cannot set that number'):
        PolicyDefinition.model_validate({**d, 'line_number': 'overtime_priority'})


def test_rules_under_different_names_that_are_one_rule_become_one():
    clinics = list(SERVICE.values())
    ucc = clinics[:2]
    # Weekly ZZ ceilings for two programmes: one a weekly series grouped before, one with dated lines.
    residents = rule('Residents weekly ZZ ceiling', quota('2026-08-23', '2027-01-03', 1, 'maximum', 'duties', window_alignment='calendar_week'),
                     {'programme_id': PROGRAMME['Residency'], 'service_ids': ucc}, merged_ids=[next(ids)])
    rotators = grouped('Rotators weekly ZZ ceiling', quota('2026-08-23', '2027-01-03', 1, 'maximum', 'duties', window_alignment='calendar_week'),
                       [{'programme_id': PROGRAMME['Visiting rotation'], 'ends_on': '2026-10-04'},
                        {'programme_id': PROGRAMME['Visiting rotation'], 'starts_on': '2026-11-01'}], {'service_ids': ucc})
    # Staffing floors for two ranks, one already with session lines.
    seniors = grouped('ZZ senior floor', {'kind': 'minimum_cover', 'minimum': 2}, [
        {'service_ids': ucc[:1], 'weekdays': [0, 1, 2, 3, 6], 'assignment_type_ids': [TYPE['Day shift']], 'amount': 3},
        {'service_ids': ucc[:1], 'weekdays': [4, 5]}], {'category_ids': [RANK['Consultant']]}, starts_on='2026-10-01', ends_on='2027-01-01')
    trainees = rule('ZZ weekday morning physical trainee floor', {'kind': 'minimum_cover', 'minimum': 1},
                    {'category_ids': [RANK['Fellow'], RANK['Resident']], 'service_ids': ucc[:1], 'assignment_type_id': TYPE['Day shift'],
                     'weekdays': [0, 1, 2, 3, 6]}, '2026-10-01', '2027-01-01')
    # One person's clinic placement per rule: person lines on an attendance level.
    placements = [rule(f'Sport Medicine longitudinal clinic placement 1 / {n}', {'kind': 'minimum_cover', 'minimum': 1},
                       {'person_id': PERSON[who], 'service_id': clinics[3], 'programme_id': PROGRAMME['Residency']})
                  for n, who in enumerate(('Kopofiku', 'Badr'), 1)]
    # Personal preferences: a grouped rule and single rules for other people, priorities up and down.
    preferences = [grouped('Preferred staff session', {'kind': 'allocation_preference', 'priority': 1},
                           [{'person_id': PERSON['Carla'], 'assignment_type_ids': [TYPE['Evening shift']]}], {'service_ids': ucc}, severity='advisory'),
                   rule('Quartz · Lower staff allocation priority', {'kind': 'allocation_preference', 'priority': -1},
                        {'person_id': PERSON['Quartz'], 'service_ids': ucc}, severity='advisory'),
                   rule('Emad · Prefer weekend mornings', {'kind': 'allocation_preference', 'priority': 1},
                        {'person_id': PERSON['Emad'], 'service_ids': ucc, 'assignment_type_id': TYPE['Day shift'], 'weekdays': [4, 5]}, severity='advisory')]
    # Supervision places that differ by clinic and number.
    places = [rule(f'Trainee places per supervisor - {kind} clinics', {'kind': 'supervision_limit', 'mode': 'capacity',
                   'trainee_category_ids': [RANK['Resident']], 'maximum': maximum}, {'service_ids': chosen}, ends_on=None)
              for kind, chosen, maximum in (('procedure', clinics[2:3], 2), ('standard', clinics[3:], 1))]
    families = {'Weekly ZZ ceiling': [residents, rotators], 'ZZ floor': [seniors, trainees],
                'Sport Medicine longitudinal clinic placement': placements, 'Personal preferences': preferences,
                'Trainee places per supervisor': places}
    everything = [r for f in families.values() for r in f]
    made = joined(plan(everything, LABELS, WEEK_START))
    assert sorted(made) == sorted(families)
    for name, members in families.items():
        assert sorted(made[name]['rule_ids']) == sorted(m['id'] for m in members), name
        assert same(members, made[name]), name
        PolicyInput.model_validate(made[name]['payload'])
    floor = made['ZZ floor']['payload']['definition']
    assert floor['scope']['category_ids'] == [] and {tuple(r['category_ids']) for r in floor['per_person']} == {
        (RANK['Consultant'],), (RANK['Fellow'], RANK['Resident'])}
    placement = made['Sport Medicine longitudinal clinic placement']['payload']['definition']
    assert {r['person_id'] for r in placement['per_person']} == {PERSON['Kopofiku'], PERSON['Badr']} and placement['listed_only']
    ceiling = made['Weekly ZZ ceiling']['payload']['definition']
    assert {r['programme_id'] for r in ceiling['per_person']} == {PROGRAMME['Residency'], PROGRAMME['Visiting rotation']}
    # Replacing a rule grouped before: its merged rules stay retired, it is the one retired now.
    assert rotators['id'] in made['Weekly ZZ ceiling']['rule_ids']


def test_exclusions_become_one_table_and_protected_time_stays_apart():
    clinics = list(SERVICE.values())
    exclusions = [rule('Excluded clinics', {'kind': 'restriction'}, {'programme_id': PROGRAMME['Internship'], 'service_ids': clinics[3:]}),
                  rule('Excluded weekdays', {'kind': 'restriction'}, {'programme_id': PROGRAMME['Internship'], 'weekdays': [4, 5]}),
                  rule('Urgent care · No fellow weekend duties', {'kind': 'restriction'},
                       {'category_ids': [RANK['Fellow']], 'service_ids': clinics[:2], 'weekdays': [4, 5]})]
    academic = rule('Residents protected academic day', {'kind': 'restriction'}, {'programme_id': PROGRAMME['Residency'], 'weekdays': [3]})
    [group] = joined(plan(exclusions + [academic], LABELS, WEEK_START)).values()
    assert group['name'] == 'Exclusions' and academic['id'] not in group['rule_ids']
    # The clinic and weekday lines overlap (an Internship Friday at the clinic breaks both): each still reports.
    assert same(exclusions, group)


def test_rules_that_mean_different_things_stay_apart():
    clinics = list(SERVICE.values())
    target = rule('ZZ weekday trainee target', {'kind': 'minimum_cover', 'minimum': 1}, {'category_ids': [RANK['Resident']], 'service_ids': clinics[:1]},
                  severity='advisory')
    placement = rule('Sport Medicine placement', {'kind': 'minimum_cover', 'minimum': 1}, {'person_id': PERSON['Kopofiku'], 'service_id': clinics[3]},
                     severity='advisory')
    assert not joined(plan([target, placement], LABELS, WEEK_START))  # same settings, names with nothing in common
    balances = [rule(f'ZZ allocation balance · {n}', {'kind': 'allocation_preference', 'priority': 0, 'balance_by': 'duties', 'balance_weight': w},
                     {'service_ids': clinics[:2]}, severity='advisory') for n, w in (('evening', 0.12), ('weekend', 0.35))]
    assert not joined(plan(balances, LABELS, WEEK_START))  # different weights: different rules


def test_line_contract_for_people_ranks_and_numbers():
    person, rank, service = PERSON['Kopofiku'], RANK['Fellow'], SERVICE['Clinic A']
    cover = {'kind': 'minimum_cover', 'minimum': 1}
    # A placement: the person who must attend, as a line of an attendance level.
    assert PolicyDefinition.model_validate({'rule': cover, 'listed_only': True, 'per_person': [{'person_id': person, 'service_ids': [service]}]})
    with pytest.raises(ValidationError, match='Lines apply'):  # a person line of a staffing level is a line of a rule for its lines only
        PolicyDefinition.model_validate({'rule': cover, 'per_person': [{'person_id': person}]})
    with pytest.raises(ValidationError, match='already names its ranks'):
        PolicyDefinition.model_validate({'rule': cover, 'scope': {'category_ids': [rank]}, 'listed_only': True, 'per_person': [{'category_ids': [rank]}]})
    with pytest.raises(ValidationError, match='people only'):
        PolicyDefinition.model_validate({'rule': {'kind': 'max_hours', 'hours': 40}, 'per_person': [{'person_id': person, 'category_ids': [rank]}]})
    [copy] = active_rules([rule('Floor', cover, listed_only=True, per_person=[{'category_ids': [rank], 'amount': 2}])])
    assert copy['definition']['scope']['category_ids'] == [rank] and copy['definition']['rule']['minimum'] == 2
    order = rule('Order', {'kind': 'allocation_preference', 'priority': 0}, listed_only=True, line_number='session_priority',
                 per_person=[{'service_ids': [service], 'amount': 30}], severity='advisory')
    [copy] = active_rules([order])
    assert copy['definition']['rule']['session_priority'] == 30 and copy['definition']['rule']['priority'] == 0


def test_balance_preferences_become_balance_goals_with_what_changes():
    physical, virtual = SERVICE['Physical ZZ'], SERVICE['Virtual ZZ']
    services = {physical: {'mode': 'physical'}, virtual: {'mode': 'virtual'}}
    ucc = {'service_ids': [physical, virtual]}
    pref = lambda name, scope=ucc, **r: rule(name, {'kind': 'allocation_preference', 'priority': 0, **r}, scope, severity='advisory')  # noqa: E731
    balances = [pref('ZZ allocation balance · perWeek', balance_by='duties', balance_weight=0.25, balance_window='week'),
                pref('ZZ allocation balance · evening', {**ucc, 'assignment_type_id': TYPE['Evening shift']}, balance_by='duties', balance_weight=0.12,
                     balance_window='rota'),
                pref('ZZ allocation balance · weekend', {**ucc, 'weekdays': [4, 5]}, balance_by='duties', balance_weight=0.35, balance_window='rota'),
                pref('ZZ allocation balance · clinicType', balance_by='service_mode', balance_weight=1.5, balance_window='rota',
                     balance_target_mode='virtual', balance_target_share=0.28)]
    exposure = pref('Programme director team exposure', priority=10, balance_by='supervisor_group', supervisor_ids=[PERSON['Emad']])
    changes = {c['name'].split(' · ')[1] if ' · ' in c['name'] else c['name']: c for c in goal_conversions(balances + [exposure], services)}
    # A preference that also sets a priority stays for it; its supervisor balancing becomes a pair-exposure goal.
    kept = changes.pop('Programme director team exposure')
    assert kept['keeps'] and kept['payload']['definition']['merged_ids'] == [] and 'stays for its priority' in kept['now']
    assert kept['payload']['definition']['rule']['metric'] == 'pair_exposure' and kept['payload']['definition']['rule']['partners']
    assert sorted(changes) == ['clinicType', 'evening', 'perWeek', 'weekend'] and not any(c['keeps'] for c in changes.values())
    goal = {k: c['payload']['definition']['rule'] for k, c in changes.items()}
    assert goal['perWeek'] == {**goal['perWeek'], 'metric': 'duties', 'service_ids': [physical, virtual], 'period': 'rota', 'aim': 'equal_share'}
    assert goal['evening']['assignment_type_ids'] == [TYPE['Evening shift']] and 'service_ids' not in goal['evening']
    assert (goal['weekend']['metric'], goal['weekend']['weekend_start'], goal['weekend']['weekend_days']) == ('weekends', 4, 2)
    assert goal['clinicType']['service_ids'] == [virtual]
    assert {k: c['payload']['definition']['strength'] for k, c in changes.items()} == {'perWeek': 'low', 'evening': 'low', 'weekend': 'low', 'clinicType': 'medium'}
    for c in changes.values():
        PolicyInput.model_validate(c['payload'])
        assert c['payload']['definition']['merged_ids'] == c['rule_ids'] and c['was'] and c['now']
        # Shared among the people who can work at the preference's clinics, not the whole department.
        assert c['payload']['definition']['scope']['conditions']['conditions'] == [
            {'field': 'eligible_service_ids', 'op': 'any', 'values': [physical, virtual]}]
    assert 'balanced over the whole rota' in changes['perWeek']['now']
    assert _weekend([5, 6, 0]) == (5, 3) and _weekend([0, 2]) is None


def test_overtime_switched_off_by_retired_limits_points_at_the_limits_in_force():
    from packages.domain.generation import allocation_settings
    person = PERSON['Kopofiku']
    retired = [{**rule(f'Kopofiku · ZZ overtime cap · {s}', quota(s, e, 2, 'maximum', 'duties', overtime='only'), {'person_id': person}, s, e),
                'status': 'retired', 'active': False} for s, e in (('2026-10-01', '2026-11-01'), ('2026-11-01', '2026-12-01'))]
    # The rebuild: one monthly cap with a line per person, checking the same months.
    rebuilt = rule('ZZ overtime cap', quota('2026-10-01', '2026-12-01', 1, 'maximum', 'duties', overtime='only', window_alignment='calendar_month'),
                   listed_only=True, per_person=[{'person_id': person, 'amount': 2}, {'person_id': PERSON['Badr']}], starts_on='2026-10-01', ends_on='2026-12-01')
    offer = rule('ZZ overtime slot', {'kind': 'allocation_overtime', 'required_policy_ids': [r['id'] for r in retired]}, severity='advisory',
                 listed_only=True, per_person=[{'person_id': person, 'weekdays': [4], 'assignment_type_ids': [TYPE['Day shift']]}],
                 starts_on='2026-10-01', ends_on='2026-12-01')
    [change] = overtime_switch_on(retired + [rebuilt, offer], WEEK_START)
    assert change['mapped'] == {r['id']: rebuilt['id'] for r in retired} and not change['missing'] and not change['switched_off']
    payload = change['payload']
    assert payload['definition']['rule']['required_policy_ids'] == [rebuilt['id']] and payload['definition']['merged_ids'] == [offer['id']]
    assert [(r['person_id'], r['weekdays'], r['assignment_type_ids']) for r in payload['definition']['per_person']] == [(person, [4], [TYPE['Day shift']])]
    # A cap that asks more of fewer months does not stand for them: the offer stays off until someone decides.
    looser = {**rebuilt, 'id': next(ids), 'definition': {**rebuilt['definition'], 'per_person': [{'person_id': person, 'amount': 3}]}}
    [still] = overtime_switch_on(retired + [looser, offer], WEEK_START)
    assert still['missing'] == [r['id'] for r in retired]
    shift = {'id': 's', 'start': '2026-10-02T08:00:00+03:00', 'end': '2026-10-02T12:00:00+03:00', 'service_ids': [], 'location_id': None,
             'assignment_type_id': TYPE['Day shift'], 'participants': []}
    data = {'organisation': {'timezone': 'Asia/Riyadh'}, 'people': {person: {}}, 'memberships': [], 'grants': [], 'policies': [],
            'types': {TYPE['Day shift']: {'department_id': 'd'}}}
    before = {**data, 'policies': active_rules(retired + [rebuilt, offer])}
    after = {**data, 'policies': active_rules(retired + [rebuilt, {**payload, 'id': next(ids), 'status': 'active', 'active': True, 'department_id': 'd'}])}
    assert allocation_settings(before, shift, person)[1] is False and allocation_settings(after, shift, person)[1] is True


def test_can_work_at_is_a_dated_person_condition():
    from datetime import datetime
    clinic = SERVICE['Physical ZZ']
    data = {'organisation': {'timezone': 'Asia/Riyadh'}, 'people': {PERSON['Kopofiku']: {}},
            'eligibilities': [{'id': 'e', 'person_id': PERSON['Kopofiku'], 'service_id': clinic, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}]}
    tree = {'match': 'all', 'conditions': [{'field': 'eligible_service_ids', 'op': 'any', 'values': [clinic]}]}
    at = lambda day: (datetime.fromisoformat(f'{day}T06:00:00+00:00'), datetime.fromisoformat(f'{day}T10:00:00+00:00'))  # noqa: E731
    assert conditions_match(data, tree, PERSON['Kopofiku'], None, *at('2026-10-05'))
    assert not conditions_match(data, tree, PERSON['Kopofiku'], None, *at('2026-11-05'))
    assert not conditions_match(data, tree, PERSON['Badr'], None, *at('2026-10-05'))


# Through the API: preview, rename, propose groups and changes, approve, the same checks, undo -----------------------------

def test_phase_two_through_governance_checks_the_same_and_undoes(h):
    from sqlalchemy import select
    from apps.api import models as m
    from apps.api.db import transaction
    from apps.api.serialization import record
    a, b, c = h.seed['people']
    clinic, other = h.seed['services'][:2]
    made = []
    for name, service, priority in (('Allocation order - First clinic', clinic, 50), ('Allocation order - Second clinic', other, 40)):
        made.append(activate(h, propose(h, {'kind': 'allocation_preference', 'priority': 0, 'session_priority': priority}, {'service_ids': [service]},
                                        severity='advisory', name=name)))
    for name, who in (('Residents weekly duty ceiling', a), ('Rotators weekly duty ceiling', b)):
        made.append(activate(h, propose(h, quota('2026-10-04', '2026-10-18', 1, 'maximum', 'duties', window_alignment='calendar_week'),
                                        {'person_id': who}, name=name, starts_on='2026-10-04', ends_on='2026-10-18')))
    balance = activate(h, propose(h, {'kind': 'allocation_preference', 'priority': 0, 'balance_by': 'duties', 'balance_window': 'rota'},
                                  {'service_ids': [clinic]}, severity='advisory', name='Clinic allocation balance'))
    w = h.workspace(start='2026-10-04', end='2026-10-11')
    for day in ('04', '05', '06'):
        w = h.shift(w, f'2026-10-{day}T08:00:00+03:00', f'2026-10-{day}T12:00:00+03:00', people=[a, b, c])['workspace']
    before = h.snapshot(w)
    checked = evaluate(before)

    preview = h.get('coordinator', '/api/policies/groups')
    groups = {g['name']: g for g in preview['groups'] if 'payload' in g}
    assert set(groups) == {'Allocation order', 'Weekly duty ceiling'} and preview['after'] == preview['active'] - 2
    [goal] = [x for x in preview['changes'] if x['shape'] == 'goal']
    assert goal['rule_ids'] == [balance['id']] and goal['was'] and goal['now']
    order = groups['Allocation order']
    result = h.command('coordinator', 'policy.group', {'groups': [g['rule_ids'] for g in groups.values()], 'changes': [goal['rule_ids']],
                                                       'names': {order['rule_ids'][0]: 'Clinic fill order'}, 'reason': 'Phase 2 of one rule for similar rules'})
    assert sorted(r['name'] for r in result['records']) == ['Clinic allocation balance', 'Clinic fill order', 'Weekly duty ceiling']
    for rec in result['records']:
        activate(h, rec)
    # TEST-STALE: phase-two changes are non-safety; refresh per freeze-matrix records 425d832da/6ef713812 before comparing counting windows.
    # Validity uses focal duties while block_quota retains its dates (G3-23, ae329a509).
    w = h.refresh_draft_baseline(h.detail(w)['workspace'])
    after = h.snapshot(w)
    grouped_ids = {r['id'] for r in result['records'] if r['kind'] != 'balance_goal'}
    id_map = {i: r['id'] for r in result['records'] if r['id'] in grouped_ids for i in r['definition']['merged_ids']}
    diff = same_checks(checked, evaluate(after), id_map)
    assert not diff['findings_only_before'] and not diff['findings_only_after'] and diff['quota_equal']
    assert expanded([p for p in before['policies'] if p['id'] != balance['id']], id_map, WEEK_START) == \
        expanded([p for p in after['policies'] if p['kind'] != 'balance_goal'], None, WEEK_START)
    # The CP-SAT model changes only by the balance: without it on both sides, the models are the same.
    assert same_model(canonical_model({**before, 'policies': [p for p in before['policies'] if p['id'] != balance['id']]}),
                      canonical_model({**after, 'policies': [p for p in after['policies'] if p['kind'] != 'balance_goal']}), id_map)
    with transaction(h.app.state.engine) as db:
        history = [p for p in db.scalars(select(m.Policy).where(m.Policy.id == balance['id']))]
        assert history[0].status == 'retired'
        assert any(g.definition.get('merged_ids') == [balance['id']] for g in db.scalars(select(m.Policy).where(m.Policy.kind == 'balance_goal')))

    # Undo: retiring each new rule brings back exactly what it replaced (the balance too).
    for rec in result['records']:
        live = next(p for p in h.get('approver', '/api/policies?status=active')['policies'] if p['id'] == rec['id'])
        restored = h.command('approver', 'policy.retire', {'reason': 'Undo phase 2'}, live['id'], live['revision'])['restored_ids']
        assert sorted(restored) == sorted(rec['definition']['merged_ids'])
    # TEST-STALE: undo is also a reviewed non-safety source change under 425d832da/6ef713812.
    w = h.refresh_draft_baseline(h.detail(w)['workspace'])
    assert evaluate(h.snapshot(w))['findings'] == checked['findings']
    with transaction(h.app.state.engine) as db:
        assert {p.id for p in db.scalars(select(m.Policy).where(m.Policy.status == 'active'))} >= {r['id'] for r in made} | {balance['id']}
        assert record(db.get(m.Policy, balance['id']))['status'] == 'active'


def test_baseline_mode_refuses_a_department_in_use_and_acts_as_system_identities(h, monkeypatch):
    import scripts.group_rules as tool
    from apps.api.db import transaction
    from sqlalchemy import select
    from apps.api import models as m
    with transaction(h.app.state.engine) as db:
        refusals = tool.baseline_refusals(db)
    assert refusals.get('enabled sign-ins') and refusals.get('roles')
    args = SimpleNamespace(propose=True, undo=False, changes=True, model=False, no_calendar=False, name=[], reason='Baseline rehearsal', only_reason=None)
    with pytest.raises(SystemExit, match='Baseline refused'):
        tool.baseline(h.app.state.engine, args)
    # The same department with the refusals set aside (as a fresh copy would be): proposed and activated by two
    # disabled system identities, with command receipts and audit events.
    for name, who in (('Residents weekly duty ceiling', h.seed['people'][0]), ('Rotators weekly duty ceiling', h.seed['people'][1])):
        activate(h, propose(h, quota('2026-10-04', '2026-10-18', 1, 'maximum', 'duties', window_alignment='calendar_week'),
                            {'person_id': who}, name=name, starts_on='2026-10-04', ends_on='2026-10-18'))
    monkeypatch.setattr(tool, 'baseline_refusals', lambda db: {})
    tool.baseline(h.app.state.engine, args)
    with transaction(h.app.state.engine) as db:
        made = next(p for p in db.scalars(select(m.Policy).where(m.Policy.status == 'active')) if p.name == 'Weekly duty ceiling')
        proposer, approver = db.get(m.Identity, made.created_by), db.get(m.Identity, made.approved_by)
        assert proposer.disabled and approver.disabled and proposer.id != approver.id
        assert proposer.issuer == approver.issuer == 'urn:fmuc:maintenance'
        events = [e for e in db.scalars(select(m.AuditEvent).where(m.AuditEvent.actor_id.in_([proposer.id, approver.id])))]
        assert {e.action for e in events} == {'policy.group', 'policy.activate'} and all(e.detail['baseline'] for e in events)
    undo = SimpleNamespace(**{**vars(args), 'undo': True, 'propose': False, 'only_reason': 'Baseline rehearsal', 'reason': 'Undo the rehearsal'})
    tool.baseline(h.app.state.engine, undo)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, made.id).status == 'retired'
