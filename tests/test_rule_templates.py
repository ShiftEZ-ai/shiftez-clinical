"""Rule templates (per-person numbers), grouping near-duplicate rules, controlled overrides and protected academic time."""
import copy

import pytest
from pydantic import ValidationError

from packages.contracts.schemas import PolicyDefinition
from packages.domain.conditions import active_rules, implies
from packages.domain.cp_sat import generate
from packages.domain.validation import evaluate
from .test_alpha_calendar import programme
from .test_rule_conditions import line, tree

DAY = ('2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00')
AFTERNOON = ('2026-10-04T13:00:00+03:00', '2026-10-04T17:00:00+03:00')


def propose(h, rule, scope=None, severity='mandatory', status=200, name='Synthetic rule', starts_on='2020-01-01', ends_on=None, **extra):
    result = h.command('coordinator', 'policy.create', {'name': name, 'owner': 'Test policy owner', 'source': 'Constructed fixture',
        'starts_on': starts_on, 'ends_on': ends_on, 'definition': {'rule': rule, 'scope': scope or {}, 'severity': severity, **extra}}, status=status)
    return result if status != 200 else result['record']


def activate(h, record, status=200):
    result = h.command('approver', 'policy.activate', {'reason': 'Independent synthetic review'}, record['id'], record['revision'], status=status)
    return result['record'] if status == 200 else result


def eight_hours(h, w, *people):
    for person in people:
        w = h.shift(w, *DAY, people=[person])['workspace']
        w = h.shift(w, *AFTERNOON, people=[person], location=1)['workspace']
    return w


def flagged(h, w, rule):
    return {f['person_id'] for f in h.lifecycle(w, 'validate')['workspace']['validation']['findings'] if f['rule_id'] == rule['id']}


# Templates -------------------------------------------------------------------------------------------------------------

def test_template_lines_give_each_person_their_own_number_in_the_validator(h):
    a, b, c = h.seed['people']
    rule = activate(h, propose(h, {'kind': 'max_hours', 'hours': 4, 'window': 'day'}, per_person=[
        {'person_id': b, 'amount': 8},
        # c's own number starts the next day: on the 4th c still has the rule's number.
        {'person_id': c, 'amount': 8, 'starts_on': '2026-10-05'}]))
    w = eight_hours(h, h.workspace(), a, b, c)
    assert flagged(h, w, rule) == {a, c}


def test_template_lines_are_quota_blocks_with_their_own_amounts(h):
    a, b, c = h.seed['people']
    quota = {'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'metric': 'duties', 'comparison': 'maximum', 'amount': 3}
    rule = activate(h, propose(h, quota, {'conditions': tree(line('person_ids', a, b, c))}, per_person=[
        {'person_id': b, 'amount': 1}, {'person_id': c, 'amount': 0, 'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}]))
    w = h.workspace()
    for person in (a, b):
        w = h.shift(w, *DAY, people=[person])['workspace']
        w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[person])['workspace']
    result = evaluate(h.snapshot(w))
    rows = {r['person_id']: r for r in result['quota_report']['rows'] if r['policy_id'] == rule['id']}
    assert {p: (r['amount'], r['starts_on'], r['status']) for p, r in rows.items()} == {
        a: (3, '2026-10-01', 'met'), b: (1, '2026-10-01', 'above_maximum'), c: (0, '2026-10-04', 'met')}
    assert {f['person_id'] for f in result['findings'] if f['rule_id'] == rule['id']} == {b}
    assert rule['id'] not in result['quota_report']['empty_policy_ids']


def test_solver_uses_each_persons_number_from_the_template(h):
    a, b, c = h.seed['people']
    # Only a may work; everyone may do one duty a week, a three: the solver must give a all three.
    h.policy({'kind': 'restriction', 'weekdays': list(range(7)), 'start_time': '00:00', 'end_time': '24:00'},
             {'conditions': tree(line('person_ids', a, op='none'))})
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week'})
    w = h.workspace()
    for day in ('04', '05', '06'):
        w = h.shift(w, f'2026-10-{day}T08:00:00+03:00', f'2026-10-{day}T12:00:00+03:00', people=[])['workspace']
    assert generate(copy.deepcopy(h.snapshot(w)), 10)['status'] != 'candidate_feasible'
    activate(h, propose(h, {'kind': 'max_duties', 'maximum': 1, 'window': 'week'}, per_person=[{'person_id': a, 'amount': 3}]))
    # The plain rule above still caps a at one; retire it so only the template speaks.
    plain = next(p for p in h.get('approver', '/api/policies?status=active')['policies']
                 if p['kind'] == 'max_duties' and not p['definition'].get('per_person'))
    h.command('approver', 'policy.retire', {'reason': 'Replaced by the template'}, plain['id'], plain['revision'])
    # TEST-STALE: both reviewed rule changes are non-safety; refresh per 425d832da/6ef713812.
    w = h.refresh_draft_baseline(h.detail(w)['workspace'])
    result = generate(copy.deepcopy(h.snapshot(w)), 10)
    assert result['status'] == 'candidate_feasible', result
    assert [r['participant']['person_id'] for r in result['additions']] == [a, a, a]


def test_template_contract():
    base = {'rule': {'kind': 'max_hours', 'hours': 40}}
    person = '00000000-0000-0000-0000-000000000001'
    assert PolicyDefinition.model_validate({**base, 'per_person': [{'person_id': person, 'amount': 50}]})
    with pytest.raises(ValidationError, match='Lines apply'):
        PolicyDefinition.model_validate({'rule': {'kind': 'supervision_limit', 'mode': 'capacity', 'maximum': 2}, 'per_person': [{'person_id': person, 'amount': 1}]})
    with pytest.raises(ValidationError, match='no number'):
        PolicyDefinition.model_validate({'rule': {'kind': 'restriction'}, 'per_person': [{'person_id': person, 'amount': 1}]})
    with pytest.raises(ValidationError, match='Line 1'):  # each line is checked as the rule would be
        PolicyDefinition.model_validate({'rule': {'kind': 'max_duties', 'maximum': 5}, 'per_person': [{'person_id': person, 'amount': 2.5}]})
    with pytest.raises(ValidationError, match='within the quota block'):
        PolicyDefinition.model_validate({'rule': {'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'metric': 'duties',
            'comparison': 'maximum', 'amount': 3}, 'per_person': [{'person_id': person, 'amount': 1, 'starts_on': '2026-09-01'}]})
    with pytest.raises(ValidationError, match='conditions'):
        PolicyDefinition.model_validate({**base, 'scope': {'person_ids': [person]}, 'per_person': [{'person_id': person, 'amount': 50}]})


def test_expansion_is_idempotent_and_keeps_the_rule_id():
    rule = {'id': 'r', 'status': 'active', 'definition': {'rule': {'kind': 'max_hours', 'hours': 40}, 'scope': {},
            'per_person': [{'person_id': 'p', 'amount': 50}]}}
    once = active_rules([rule])
    assert [(c['id'], c['definition']['rule']['hours']) for c in once] == [('r', 40), ('r', 50)]
    assert active_rules(once) == once


# Grouping near-duplicate rules -------------------------------------------------------------------------------------------

def test_grouping_previews_proposes_retires_on_approval_and_is_reversible(h):
    a, b, c = h.seed['people']
    names = {p['id']: p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people']}
    quota = {'kind': 'block_quota', 'metric': 'session_units', 'comparison': 'target', 'window_alignment': 'calendar_week'}
    old = [activate(h, propose(h, {**quota, 'starts_on': start, 'ends_on': end, 'amount': amount}, {'person_id': who}, 'advisory',
                               name=f'{names[who]} – Weekly clinical target – {start}', starts_on=start, ends_on=end))
           for who, amount, start, end in ((a, 6, '2026-10-04', '2026-10-18'), (b, 8, '2026-10-04', '2026-10-18'), (c, 6, '2026-10-11', '2026-10-25'))]
    activate(h, propose(h, {**quota, 'starts_on': '2026-10-04', 'ends_on': '2026-10-18', 'amount': 5, 'metric': 'duties'}, {'person_id': a},
                        'advisory', starts_on='2026-10-04', ends_on='2026-10-18'))  # another measure: not a duplicate
    w = h.workspace()
    w = h.shift(w, *DAY, people=[a, b])['workspace']
    before = sorted((r['person_id'], r['amount'], r['starts_on']) for r in evaluate(h.snapshot(w))['quota_report']['rows'] if r['metric'] == 'session_units')

    preview = h.get('coordinator', '/api/policies/groups')
    assert h.get('coordinator', '/api/policies/groups') == preview  # a dry run saves nothing
    [group] = preview['groups']
    assert (group['name'], group['people'], group['amounts'], sorted(group['rule_ids'])) == (
        'Weekly clinical target', 3, [6, 8], sorted(p['id'] for p in old))
    template = h.command('coordinator', 'policy.group', {'groups': [group['rule_ids']], 'reason': 'One rule per target'})['records'][0]
    assert template['status'] == 'proposed' and len(template['definition']['per_person']) == 3
    assert h.get('coordinator', '/api/policies/groups')['groups'] == []  # already waiting for approval
    assert all(p['status'] == 'active' for p in h.get('approver', '/api/policies?status=active')['policies'] if p['id'] in group['rule_ids'])

    activate(h, template)
    assert {p['id'] for p in h.get('approver', '/api/policies?status=retired')['policies']} >= set(group['rule_ids'])
    after = sorted((r['person_id'], r['amount'], r['starts_on']) for r in evaluate(h.snapshot(w))['quota_report']['rows'] if r['metric'] == 'session_units')
    assert after == before  # the same targets, now from one rule

    live = next(p for p in h.get('approver', '/api/policies?status=active')['policies'] if p['id'] == template['id'])
    restored = h.command('approver', 'policy.retire', {'reason': 'Undo the grouping'}, live['id'], live['revision'])['restored_ids']
    assert sorted(restored) == sorted(group['rule_ids'])
    assert sorted((r['person_id'], r['amount'], r['starts_on']) for r in evaluate(h.snapshot(w))['quota_report']['rows'] if r['metric'] == 'session_units') == before


def test_grouping_refuses_a_stale_selection(h):
    a, b, _ = h.seed['people']
    ids = [activate(h, propose(h, {'kind': 'max_hours', 'hours': hours}, {'person_id': who}))['id'] for who, hours in ((a, 40), (b, 48))]
    h.command('coordinator', 'policy.group', {'groups': [ids[:1] + ids[:1]], 'reason': 'Twice the same rule'}, status=422)
    h.command('coordinator', 'policy.group', {'groups': [[ids[0], '00000000-0000-0000-0000-000000000009']], 'reason': 'Not a group'}, status=409)
    h.command('staff', 'policy.group', {'groups': [ids], 'reason': 'Not allowed'}, status=403)


# Controlled overrides ----------------------------------------------------------------------------------------------------

def test_implication_is_a_sure_check():
    rank = lambda *v, op='any': {'conditions': tree(line('category_ids', *v, op=op))}
    assert implies(rank('R1'), {}) and not implies({}, rank('R1'))
    assert implies(rank('R1'), rank('R1', 'R2')) and not implies(rank('R1', 'R2'), rank('R1'))
    assert implies(rank('R1', 'R2', op='none'), rank('R1', op='none')) and not implies(rank('R1', op='none'), rank('R1', 'R2', op='none'))
    assert implies({'category_id': 'R1', 'weekdays': [0]}, rank('R1'))           # older flat fields read as lines
    assert not implies({**rank('R1'), 'weekdays': [0, 1]}, {'weekdays': [0]})     # more days is not narrower
    assert implies({'start_time': '09:00', 'end_time': '12:00'}, {'start_time': '08:00', 'end_time': '17:00'})
    assert not implies({'conditions': tree(line('category_ids', 'R1'), line('tags', 'x'), match='any')}, rank('R1'))


SAFETY = [
    ({'kind': 'restriction', 'weekdays': [0], 'start_time': '00:00', 'end_time': '24:00'}, 'mandatory'),
    ({'kind': 'min_rest', 'hours': 11}, 'mandatory'),
    ({'kind': 'supervision_limit', 'mode': 'capacity', 'maximum': 2}, 'mandatory'),
    ({'kind': 'minimum_cover', 'minimum': 1}, 'mandatory'),
    ({'kind': 'maximum_participants', 'maximum': 3}, 'advisory'),  # historical level
]


@pytest.mark.parametrize('rule,severity', SAFETY)
def test_safety_rules_can_never_be_overridden(h, rule, severity):
    # Safety kinds are always mandatory. Stored advisory levels still cannot be overridden.
    target = h.stored_advisory_safety(rule) if severity == 'advisory' else h.policy(rule)
    refused = propose(h, rule, {'conditions': tree(line('category_ids', h.seed['trainee_category']))},
                      status=422, overrides=[target['id']])
    assert refused['error']['code'] == 'OVERRIDE_SAFETY'


def test_forbidden_transition_can_never_be_overridden(h):
    rule = {'kind': 'forbidden_transition', 'from_type_ids': [h.seed['type']], 'to_type_ids': [h.seed['type']]}
    target = h.policy(rule)
    assert propose(h, rule, {'conditions': tree(line('category_ids', h.seed['trainee_category']))}, status=422,
                   overrides=[target['id']])['error']['code'] == 'OVERRIDE_SAFETY'


def test_override_is_allowed_only_for_a_narrower_limit_or_between_advisory_rules(h):
    rank = {'conditions': tree(line('category_ids', h.seed['trainee_category']))}
    hours = h.policy({'kind': 'max_hours', 'hours': 40})
    code = lambda *args, **kw: propose(h, *args, status=422, **kw)['error']['code']
    assert propose(h, {'kind': 'max_hours', 'hours': 50}, rank, overrides=[hours['id']])['definition']['overrides'] == [hours['id']]
    assert code({'kind': 'max_hours', 'hours': 50}, {}, overrides=[hours['id']]) == 'OVERRIDE_NOT_SPECIFIC'
    assert code({'kind': 'max_hours', 'hours': 50}, rank, 'advisory', overrides=[hours['id']]) == 'OVERRIDE_SEVERITY'
    assert code({'kind': 'max_duties', 'maximum': 9}, rank, overrides=[hours['id']]) == 'OVERRIDE_KIND'
    quota = {'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'metric': 'duties', 'amount': 3}
    minimum = h.policy({**quota, 'comparison': 'minimum'})
    assert code({**quota, 'comparison': 'minimum', 'amount': 1}, rank, overrides=[minimum['id']]) == 'OVERRIDE_NOT_ALLOWED'
    maximum = h.policy({**quota, 'comparison': 'maximum'})
    assert propose(h, {**quota, 'comparison': 'maximum', 'amount': 5}, rank, overrides=[maximum['id']])['status'] == 'proposed'
    # Two advisory rules: the narrower-scope requirement does not apply.
    preference = h.policy({'kind': 'allocation_preference', 'priority': 1}, severity='advisory')
    assert propose(h, {'kind': 'allocation_preference', 'priority': 5}, {}, 'advisory', overrides=[preference['id']])['status'] == 'proposed'


def test_overriding_rule_wins_for_its_people_and_everyone_else_is_unchanged(h):
    a, b, c = h.seed['people']
    base = h.policy({'kind': 'max_hours', 'hours': 4, 'window': 'day'})
    wider = activate(h, propose(h, {'kind': 'max_hours', 'hours': 8, 'window': 'day'}, {'conditions': tree(line('person_ids', a))},
                                name='Longer days for a', overrides=[base['id']]))
    w = eight_hours(h, h.workspace(), a, b)
    assert flagged(h, w, base) == {b} and flagged(h, w, wider) == set()

    applies = lambda person: {r['policy_id']: r for r in h.get('coordinator', f'/api/policies/applies?person_id={person}&day=2026-10-04')['rules']}
    for_a, for_b = applies(a), applies(b)
    assert not for_a[base['id']]['applies'] and for_a[base['id']]['overridden_by'] == [wider['id']]
    assert 'Longer days for a' in for_a[base['id']]['why']
    assert for_a[wider['id']]['applies'] and for_a[wider['id']]['rule']['hours'] == 8
    assert for_b[base['id']]['applies'] and wider['id'] not in for_b
    h.get('staff', f'/api/policies/applies?person_id={a}', status=403)


def test_which_rule_applies_shows_the_persons_own_template_number(h):
    a, b, _ = h.seed['people']
    rule = activate(h, propose(h, {'kind': 'max_hours', 'hours': 40}, per_person=[{'person_id': a, 'amount': 30}]))
    mine = {r['policy_id']: r for r in h.get('coordinator', f'/api/policies/applies?person_id={a}&day=2026-10-04')['rules']}[rule['id']]
    theirs = {r['policy_id']: r for r in h.get('coordinator', f'/api/policies/applies?person_id={b}&day=2026-10-04')['rules']}[rule['id']]
    assert (mine['rule']['hours'], mine['own_line']['amount']) == (30, 30) and 'own number' in mine['why']
    assert (theirs['rule']['hours'], theirs['own_line']) == (40, None)


# Academic calendar → protected time --------------------------------------------------------------------------------------

def test_protecting_an_academic_calendar_entry_keeps_programme_members_off_duty(h):
    a, b, _ = h.seed['people']
    p, _, _ = programme(h)  # a is in the programme during October
    entry = h.configure('programme_calendar_entries', {'programme_id': p['id'], 'name': 'Teaching block', 'kind': 'academic_block',
                                                        'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}, role='coordinator')['record']
    rule = activate(h, propose(h, {'kind': 'restriction', 'weekdays': list(range(7)), 'start_time': '08:00', 'end_time': '12:00'},
                               {'conditions': tree(line('programme_ids', p['id']))}, name='Protected time · Teaching block',
                               starts_on='2026-10-01', ends_on='2026-11-01', calendar_entry_id=entry['id']))
    assert rule['definition']['calendar_entry_id'] == entry['id']
    w = h.shift(h.workspace(), *DAY, people=[a], seed_restricted=True)['workspace']
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[b])['workspace']
    findings = h.lifecycle(w, 'validate')['workspace']['validation']['findings']
    assert {(f['code'], f['person_id']) for f in findings if f['rule_id'] == rule['id']} == {('RESTRICTION', a)}
    propose(h, {'kind': 'restriction'}, calendar_entry_id='00000000-0000-0000-0000-000000000009', status=404)
    # The entry changed: an updated rule for the same entry replaces the one in force once approved.
    update = activate(h, propose(h, {'kind': 'restriction', 'weekdays': [0], 'start_time': '08:00', 'end_time': '12:00'},
                                 {'conditions': tree(line('programme_ids', p['id']))}, name='Protected time · Teaching block (moved)',
                                 starts_on='2026-10-01', ends_on='2026-11-01', calendar_entry_id=entry['id']))
    active = [x['id'] for x in h.get('approver', '/api/policies?status=active')['policies'] if x['definition'].get('calendar_entry_id') == entry['id']]
    assert active == [update['id']]
