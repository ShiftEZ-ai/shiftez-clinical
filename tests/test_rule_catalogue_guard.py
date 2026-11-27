"""Guard (docs/design/rule-catalogue.md §3): a catalogue rule reaches every evaluator only as the old kinds its lines
write. Old rules and a catalogue rule saying the same give identical findings, quota rows, scores, CP-SAT models and
schedules, so kind 'catalogue' never reaches an evaluator directly."""
import copy
import uuid

import pytest

from packages.domain import cp_sat, generation
from packages.domain.conditions import active_rules
from packages.domain.fairness import parse_quota_goal_id, quota_goal_id, quota_goal_reports
from packages.domain.rule_groups import canonical_model, same_checks, same_model
from packages.domain.score import schedule_score
from packages.domain.validation import evaluate
from .test_rule_templates import activate, propose

BLOCK = {'starts_on': '2026-10-04', 'ends_on': '2026-10-18'}


def line(**fields):
    return {'id': str(uuid.uuid4()), **fields}


def catalogue(entry, *lines, settings=None):
    return {'id': str(uuid.uuid4()), 'name': entry, 'kind': 'catalogue', 'status': 'active', 'active': True, 'revision': 2,
            'owner': 'Test policy owner', 'source': 'Constructed fixture', 'starts_on': '2020-01-01', 'ends_on': None,
            'definition': {'rule': {'kind': 'catalogue', 'id': entry, 'settings': settings or {}, 'lines': list(lines)}}}


def legacy_rules(h):
    """Old rules of the kinds production holds, with person lines."""
    a, b, c = h.seed['people']
    service = h.seed['services'][0]
    return {
        'cap': activate(h, propose(h, {'kind': 'max_duties', 'maximum': 3}, per_person=[{'person_id': a, 'amount': 1}])),
        'quota': activate(h, propose(h, {'kind': 'block_quota', **BLOCK, 'metric': 'duties', 'comparison': 'range', 'amount': 1, 'maximum_amount': 3,
                                         'target_amount': 2, 'target_strength': 'high', 'window_alignment': 'calendar_week'},
                                     per_person=[{'person_id': b, 'amount': 0, 'settings': {'maximum_amount': 2}}])),
        'cover': activate(h, propose(h, {'kind': 'minimum_cover', 'minimum': 2}, {'service_ids': [service]})),
        'off': activate(h, propose(h, {'kind': 'restriction'}, {'person_id': c, 'weekdays': [1]})),
        'prefer': activate(h, propose(h, {'kind': 'allocation_preference', 'priority': 3}, {'person_id': b}, 'advisory')),
        'fair': activate(h, propose(h, {'kind': 'balance_goal', 'metric': 'duties'}, severity='advisory')),
    }


def world(h):
    """Legacy rules and a week of sessions with people on them."""
    a, b, c = h.seed['people']
    service = h.seed['services'][0]
    old = legacy_rules(h)
    w = h.workspace(start='2026-10-04', end='2026-10-11')
    for day, people in (('04', [a, b]), ('05', [a]), ('06', [a, b, c]), ('07', [c]), ('08', [])):
        w = h.shift(w, f'2026-10-{day}T08:00:00+03:00', f'2026-10-{day}T12:00:00+03:00', people=people,
                    seed_restricted=c in people and day == '06')['workspace']
    person = lambda p: {'conditions': [{'field': 'person_ids', 'values': [p]}]}  # noqa: E731
    made = {
        'cap': catalogue('duties_per_week', line(numbers={'max': {'value': 3}}), line(who=person(a), numbers={'max': {'value': 1}}),
                         settings={'max_duties': {'window': 'week'}}),
        'quota': catalogue('duties_per_period', *[line(who=who, settings={'block_quota': {**BLOCK, 'window_alignment': 'calendar_week'}}, numbers={
            'min': {'value': low}, 'max': {'value': high}, 'target': {'value': 2, 'level': 'high'}}) for who, low, high in ((None, 1, 3), (person(b), 0, 2))]),
        'cover': catalogue('people_per_session', line(where={'conditions': [{'field': 'service_ids', 'values': [service]}]}, numbers={'min': {'value': 2}})),
        'off': catalogue('keep_off', line(who=person(c), weekdays=[1], numbers={'apply': {'value': None}})),
        'prefer': catalogue('preferences', line(who=person(b), numbers={'priority': {'value': 3, 'level': 'medium'}})),
        'fair': catalogue('fair_share', line(numbers={'apply': {'value': None, 'level': 'medium'}}, settings={'balance_goal': {'metric': 'duties'}})),
    }
    return w, old, made


def swapped(data, old, made):
    """The rota's data with the old rules replaced by the catalogue rules (as activating them would)."""
    out = copy.deepcopy(data)
    gone = {p['id'] for p in old.values()}
    out['policies'] = [p for p in out['policies'] if p['id'] not in gone] + [{**p, 'department_id': out['workspace']['department_id']}
                                                                           for p in made.values()]
    return out


@pytest.mark.parametrize('old_detail,new_detail,unmapped_detail', [
    ({'goal_id': 'old'}, {'goal_id': 'catalogue'}, {'goal_id': 'unknown'}),
    ({'rule_ids': ['old', 'unchanged']}, {'rule_ids': ['catalogue', 'unchanged']},
     {'rule_ids': ['unknown', 'unchanged']}),
    ({'lead_sources': [{'policy_id': 'old', 'revision': 1, 'line_id': 'line'}]},
     {'lead_sources': [{'policy_id': 'catalogue', 'revision': 1, 'line_id': 'line'}]},
     {'lead_sources': [{'policy_id': 'unknown', 'revision': 1, 'line_id': 'line'}]}),
])
def test_finding_proof_maps_policy_references_and_keeps_real_differences(old_detail, new_detail, unmapped_detail):
    before = {'code': 'BALANCE_GOAL', 'rule_id': 'old', 'person_id': 'person', 'severity': 'warning',
              'detail': {**old_detail, 'actual': 2, 'expected': 1, 'partner_id': 'old'}}
    after = {**before, 'rule_id': 'catalogue', 'detail': {**before['detail'], **new_detail}}
    original = copy.deepcopy(before)
    mapping = {'old': 'catalogue'}

    def differs(finding):
        proof = same_checks({'findings': [before]}, {'findings': [finding]}, mapping)
        assert proof['quota_equal']
        return bool(proof['findings_only_before']) and bool(proof['findings_only_after'])

    assert not differs(after)
    for changed in (unmapped_detail, {'actual': 3}, {'expected': 2}, {'partner_id': 'catalogue'}):
        assert differs({**after, 'detail': {**after['detail'], **changed}})
    assert before == original


@pytest.mark.parametrize('balance_between_people', [True, False])
def test_quota_goal_proof_requires_matching_keys_numbers_and_mapped_policies(balance_between_people):
    def checked(policy_id, **changes):
        row = {'policy_id': policy_id, 'policy_name': 'Synthetic quota', 'person_id': 'person',
               'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'metric': 'duties',
               'comparison': 'target', 'status': 'above', 'actual': 3, 'amount': 2,
               'severity': 'advisory', 'strength': 'medium', 'balance_between_people': balance_between_people,
               **changes}
        report = {'rows': [row]}
        [goal] = quota_goal_reports({'policies': []}, report)
        [person] = goal['people']
        return {'quota_report': report, 'findings': [
            {'code': 'BALANCE_GOAL', 'rule_id': policy_id, 'person_id': 'person', 'severity': 'warning',
             'detail': {'goal_id': goal['goal_id'], 'actual': person['actual'], 'expected': person['expected']}}]}

    before, after = checked('old'), checked('catalogue')
    original = copy.deepcopy((before, after))
    mapping = {'old': 'catalogue'}
    assert before['findings'][0]['detail']['goal_id'] != after['findings'][0]['detail']['goal_id']
    proof = same_checks(before, after, mapping)
    assert not proof['findings_only_before'] and not proof['findings_only_after'] and proof['quota_equal']

    for changes in ({'actual': 4}, {'expected': 3}, {'goal_id': 'unknown:quota:unmapped'},
                    {'goal_id': after['findings'][0]['detail']['goal_id'] + '-unmapped'}):
        changed = copy.deepcopy(after)
        changed['findings'][0]['detail'].update(changes)
        proof = same_checks(before, changed, mapping)
        assert proof['findings_only_before'] and proof['findings_only_after'] and proof['quota_equal']
    for changed, ids in ((checked('catalogue', starts_on='2026-10-05'), mapping),
                         (after, {'unrelated': 'catalogue'})):
        proof = same_checks(before, changed, ids)
        assert proof['findings_only_before'] and proof['findings_only_after'] and not proof['quota_equal']
    assert (before, after) == original


def test_quota_goal_id_round_trips_through_parser():
    policy_id = '00000000-0000-4000-8000-000000000001'
    goal_id = quota_goal_id(policy_id, ('2026-10-04', '2026-10-11', 'duties'))
    policy, digest = parse_quota_goal_id(goal_id)
    assert policy == policy_id and goal_id == f'{policy}:quota:{digest}'


def test_old_rules_and_their_catalogue_rule_check_the_same_everywhere(h):
    w, old, made = world(h)
    before = h.snapshot(w)
    after = swapped(before, old, made)
    id_map = {old[k]['id']: made[k]['id'] for k in old}
    assert not [p for p in active_rules(after['policies']) if p['definition']['rule']['kind'] == 'catalogue' or p['kind'] == 'catalogue']

    a, b = evaluate(before), evaluate(after)
    assert a['findings'] and a['quota_report']['rows']
    diff = same_checks(a, b, id_map)
    assert not diff['findings_only_before'] and not diff['findings_only_after'] and diff['quota_equal'], diff
    assert schedule_score(before, a, {})['score'] == schedule_score(after, b, {})['score']

    assert same_model(canonical_model(before), canonical_model(after), id_map)
    solved = [cp_sat.generate(copy.deepcopy(d), 5, 1, deterministic=True) for d in (before, after)]
    assert solved[0]['status'] == solved[1]['status'] and solved[0]['additions'] == solved[1]['additions']
    assert solved[0]['removals'] == solved[1]['removals']
    greedy = [generation.generate(copy.deepcopy(d)) for d in (before, after)]
    assert greedy[0]['status'] == greedy[1]['status'] and greedy[0].get('additions') == greedy[1].get('additions')


def test_a_rule_switched_off_checks_nothing(h):
    w, old, made = world(h)
    before = h.snapshot(w)
    off = {k: {**p, 'definition': {**p['definition'], 'rule': {**p['definition']['rule'], 'on': False}}} for k, p in made.items()}
    after = swapped(before, old, off)
    assert not [p for p in active_rules(after['policies']) if p['id'] in {x['id'] for x in made.values()}]
    assert not [f for f in evaluate(after)['findings'] if f.get('rule_id') in {x['id'] for x in made.values()}]
