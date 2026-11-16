"""The one resolver (docs/design/rule-catalogue.md §3): a catalogue rule's lines as the old rule copies every check reads,
most specific line wins only between lines with the same where, safety entries never outrank."""
import gc
import time
import uuid
from datetime import datetime, timezone

import pytest

from packages.domain import rule_lines
from packages.domain.conditions import OUTRANKED, active_rules, conditions_match, scope_tree
from packages.domain.solver_rules import definition_issues

A, B, C, PROG, CLINIC, VIP = (str(uuid.uuid4()) for _ in range(6))


def line(**fields):
    return {'id': str(uuid.uuid4()), **fields}


def person(*people):
    return {'conditions': [{'field': 'person_ids', 'values': list(people)}]}


def rule(entry, *lines, settings=None, **row):
    return {'id': str(uuid.uuid4()), 'name': entry, 'kind': 'catalogue', 'status': 'active', 'active': True, 'department_id': 'd',
            'revision': 1, 'starts_on': '2026-01-01', 'ends_on': None, **row,
            'definition': {'rule': {'kind': 'catalogue', 'id': entry, 'settings': settings or {}, 'lines': list(lines)}}}


def internal(copy):
    tree = copy['definition']['scope'].get('conditions') or {'conditions': []}
    return [{**c, 'values': [v.rsplit(':', 1)[-1] for v in c['values']]} if c['field'] == OUTRANKED else c
            for c in tree['conditions'] if 'conditions' not in c and c['field'] in (OUTRANKED, 'person_ids') and c['op'] == 'none']


def test_seeded_lines_attach_only_to_their_exact_revision_and_are_idempotent():
    from copy import deepcopy

    original = rule('fair_share', line(numbers={'apply': {'level': 'medium'}}))
    original['revision'] = 3
    extra = line(numbers={'apply': {'level': 'high'}}, settings={'balance_goal': {'metric': 'hours'}})
    seeds = [{'policy_id': original['id'], 'policy_revision': 3, 'line': extra}]
    before = deepcopy(original)
    [composed] = rule_lines.seeded_policies([original], seeds)
    assert composed['definition']['rule']['lines'] == before['definition']['rule']['lines'] + [extra]
    assert rule_lines.seeded_policies([composed], seeds) == [composed]
    assert original == before
    assert rule_lines.seeded_policies([{**original, 'revision': 2}], seeds) == [{**original, 'revision': 2}]
    assert rule_lines.seeded_policies([{**original, 'revision': 4}], seeds) == [{**original, 'revision': 4}]


def test_nested_rule_defaults_remain_json_values():
    assert rule_lines._defaults('balance_goal')['share_adjustment'] == {'mode': 'percent', 'value': 100}


@pytest.mark.parametrize('complete', [False, True])
@pytest.mark.parametrize('whole_context', [False, True])
def test_active_rules_keeps_caller_data_unchanged_with_fair_share_findings(h, complete, whole_context):
    from copy import deepcopy

    from apps.api.snapshots import DomainSnapshot
    from packages.domain.validation import evaluate, prepare_assignment_trials
    goal = rule('fair_share', line(numbers={'apply': {'level': 'medium'}},
                                 settings={'balance_goal': {'metric': 'duties'} if complete else {}}))
    snapshot = h.snapshot(h.workspace())
    snapshot['policies'].append(goal)
    snapshot.pop('_fair_share_goal_findings', None)
    snapshot.pop('_fair_share_goal_read_quality', None)
    original = deepcopy(snapshot)
    resolved = active_rules(snapshot) if whole_context else active_rules(snapshot['policies'], context=snapshot)
    assert (goal['id'] in {policy['id'] for policy in resolved}) == complete
    assert snapshot == original
    materialized = DomainSnapshot(deepcopy(snapshot), read_quality=deepcopy(snapshot.read_quality))
    materialized['policies'] = rule_lines.bound_policies(materialized['policies'], materialized,
                                                       read_quality=materialized.read_quality)
    materialized['_catalogue_v2_materialized'] = True
    for prepared in (snapshot, prepare_assignment_trials(snapshot),
                     materialized, prepare_assignment_trials(materialized)):
        findings = [finding for finding in evaluate(prepared)['findings']
                    if finding['code'] == 'FAIR_SHARE_GOAL_INCOMPLETE']
        assert [finding['rule_id'] for finding in findings] == ([] if complete else [goal['id']])
        if findings:
            assert findings[0]['severity'] == 'warning'
            assert findings[0]['detail'] == {'stored_incomplete': True, 'policy_id': goal['id']}
    assert snapshot == original


def test_each_number_is_the_old_kind_it_writes_in_the_order_checks_read():
    week = {'block_quota': {'starts_on': '2026-10-04', 'ends_on': '2026-11-01', 'window_alignment': 'calendar_week'}}
    r = rule('duties_per_week', line(numbers={'min': {'value': 1}, 'max': {'value': 4}, 'target': {'value': 2, 'level': 'high'}}, settings=week),
             line(numbers={'max': {'value': 5}}, settings={'max_duties': {}}))
    copies = active_rules([r])
    assert [(c['kind'], c['definition']['rule'].get('comparison'), c['template_line']['slot']) for c in copies] == [
        ('block_quota', 'minimum', 'min'), ('block_quota', 'maximum', 'max'), ('max_duties', None, 'max'), ('block_quota', 'target', 'target')]
    assert {c['id'] for c in copies} == {r['id']} and [c['template_line']['line'] for c in copies] == [0, 0, 1, 0]
    target = copies[-1]['definition']
    assert (target['severity'], target['strength'], target['exception_allowed'], target['rule']['amount']) == ('advisory', 'high', False, 2.0)
    assert copies[2]['definition']['rule'] == {'kind': 'max_duties', 'maximum': 5, 'window': 'week', 'window_days': 7,
                                               'include_overtime': True, 'group_by': 'all'}
    assert active_rules(copies) == copies  # idempotent


def test_numbers_of_one_check_are_one_copy_and_a_preference_is_never_mandatory():
    [run] = active_rules([rule('days_in_a_row', line(numbers={'min': {'value': 2, 'level': 'low'}, 'max': {'value': 5, 'level': 'low'}},
                                                         settings={'run_length': {}}))])
    assert (run['kind'], run['definition']['rule']['minimum'], run['definition']['rule']['maximum'], run['template_line']['slot']) == (
        'run_length', 2, 5, 'min+max')
    [limit] = active_rules([rule('days_in_a_row', line(numbers={'max': {'value': 6}}))])
    assert limit['kind'] == 'max_consecutive_days' and limit['definition']['severity'] == 'mandatory'
    with pytest.raises(ValueError, match='preference'):
        active_rules([rule('days_in_a_row', line(numbers={'max': {'value': 6}}, settings={'run_length': {}}))])


def test_who_and_where_land_where_the_old_scopes_had_them():
    [c] = active_rules([rule('duties_per_week', line(numbers={'max': {'value': 3}}, weekdays=[0, 1], start_time='08:00', end_time='16:00',
        who={'conditions': [{'field': 'programme_ids', 'values': [PROG]}, {'field': 'tags', 'values': ['VIP']}]},
        where={'conditions': [{'field': 'service_ids', 'values': [CLINIC]}, {'field': 'assignment_type_ids', 'values': [A, B]}]}))])
    scope = c['definition']['scope']
    assert (scope['programme_id'], scope['service_ids'], scope['weekdays'], scope['start_time']) == (PROG, [CLINIC], [0, 1], '08:00')
    assert scope['conditions']['conditions'] == [{'field': 'tags', 'op': 'any', 'values': ['VIP']},
                                                 {'field': 'assignment_type_ids', 'op': 'any', 'values': [A, B]}]


def test_most_specific_line_wins_only_on_the_same_where():
    everyone, mine, vip = line(numbers={'max': {'value': 5}}), line(who=person(A), numbers={'max': {'value': 3}}), \
        line(who=person(A), where={'conditions': [{'field': 'service_ids', 'values': [VIP]}]}, numbers={'max': {'value': 2}})
    copies = {c['template_line']['id']: c for c in active_rules([rule('duties_per_week', everyone, mine, vip)])}
    assert internal(copies[everyone['id']]) == [{'field': OUTRANKED, 'op': 'none', 'values': [mine['id']]}]
    assert internal(copies[mine['id']]) == [] and internal(copies[vip['id']]) == []  # a different where: both apply


def test_equal_tiers_other_dates_and_other_settings_both_apply():
    ranks = line(who={'conditions': [{'field': 'category_ids', 'values': [B]}]}, numbers={'max': {'value': 4}})
    tagged = line(who={'conditions': [{'field': 'tags', 'values': ['exceptional_trainee']}]}, numbers={'max': {'value': 6}})
    copies = {c['template_line']['id']: c for c in active_rules([rule('duties_per_week', ranks, tagged)])}
    assert internal(copies[ranks['id']]) == internal(copies[tagged['id']]) == []
    early = line(numbers={'max': {'value': 5}}, ends_on='2026-12-01')
    later = line(who=person(A), numbers={'max': {'value': 3}}, starts_on='2027-01-01')
    other = line(who=person(C), numbers={'max': {'value': 3}}, settings={'max_duties': {'include_overtime': False}})
    copies = {c['template_line']['id']: c for c in active_rules([rule('duties_per_week', early, later, other)])}
    assert all(internal(c) == [] for c in copies.values())


def test_lines_measuring_different_things_neither_outrank_nor_duplicate():
    from packages.domain.rule_lines import duplicates
    block = {'block_quota': {'starts_on': '2026-10-04', 'ends_on': '2026-11-01'}}
    week = line(numbers={'max': {'value': 5}}, settings={'max_duties': {}})
    day = line(who=person(A), numbers={'max': {'value': 1}}, settings={'max_duties': {'window': 'day'}})
    quota = line(who=person(A), numbers={'min': {'value': 1}, 'max': {'value': 3}}, settings=block)
    r = rule('duties_per_period', week, day, quota)
    assert r['definition']['rule']['lines'] and all(internal(c) == [] for c in active_rules([r]))  # a week, a day, a block
    assert duplicates(r['definition']['rule']) == []
    again = line(who=person(A), numbers={'max': {'value': 2}}, settings={'max_duties': {'window': 'day'}})
    assert duplicates(rule('duties_per_period', week, day, again)['definition']['rule']) == [(1, 2)]
    mondays = {**again, 'weekdays': [0]}  # another set of days counts other duties: both apply
    assert duplicates(rule('duties_per_period', week, day, mondays)['definition']['rule']) == []


def test_safety_lines_never_outrank():
    copies = active_rules([rule('people_per_session', line(numbers={'min': {'value': 2}}),
                                line(who={'conditions': [{'field': 'category_ids', 'values': [B]}]}, numbers={'min': {'value': 1}}))])
    assert all(internal(c) == [] for c in copies)


def test_a_quota_line_a_person_outranks_leaves_them_out_for_the_block():
    block = {'block_quota': {'starts_on': '2026-10-04', 'ends_on': '2026-11-01'}}
    everyone, mine = line(numbers={'max': {'value': 5}}, settings=block), line(who=person(A), numbers={'max': {'value': 2}}, settings=block)
    copies = {c['template_line']['id']: c for c in active_rules([rule('duties_per_period', everyone, mine)])}
    assert internal(copies[everyone['id']]) == [{'field': 'person_ids', 'op': 'none', 'values': [A]}]


def holds(data, policy, person_id, day):
    a = datetime.fromisoformat(f'{day}T00:00:00+00:00')
    b = datetime.fromisoformat(f'{day}T23:00:00+00:00')
    return conditions_match(data, scope_tree(policy['definition']['scope']), person_id, None, a, b)


def test_the_outranked_line_stops_for_that_person_only_while_the_winner_holds():
    everyone, mine = line(numbers={'max': {'value': 5}}), line(who=person(A), numbers={'max': {'value': 3}}, starts_on='2026-10-10')
    row = rule('duties_per_week', everyone, mine)
    copies = active_rules([row])
    data = {'organisation': {'timezone': 'UTC'}, 'people': {A: {}, B: {}}, 'policies': copies}
    loser = next(c for c in copies if c['template_line']['id'] == everyone['id'])
    assert not holds(data, loser, A, '2026-10-12') and holds(data, loser, B, '2026-10-12')
    assert holds(data, loser, A, '2026-10-05')  # before A's own line starts, the line for everyone is theirs
    stored = {**data, 'policies': [row]}  # a reader holding the same stored rule reads the same
    assert not holds(stored, loser, A, '2026-10-12') and holds(stored, loser, B, '2026-10-12')


def test_a_rule_switched_off_has_no_copies():
    assert active_rules([rule('keep_off', line(numbers={'apply': {'value': None}}), **{})]) != []
    off = rule('keep_off', line(numbers={'apply': {'value': None}}))
    off['definition']['rule']['on'] = False
    assert active_rules([off]) == []


def test_definition_issues_name_the_line():
    good = rule('duties_per_week', line(numbers={'max': {'value': 5}}))['definition']
    assert definition_issues(good) == []
    # A copy is checked with its own scope (pooled cover needs its clinics): a valid pooled line is no issue.
    pooled_ok = rule('people_per_session', line(where={'conditions': [{'field': 'service_ids', 'values': [CLINIC]}]}, numbers={'min': {'value': 2}},
                                                 settings={'minimum_cover': {'aggregation': 'service_group'}}))['definition']
    assert definition_issues(pooled_ok) == []
    no_block = rule('duties_per_month', line(numbers={'max': {'value': 5}}))['definition']
    [issue] = definition_issues(no_block)
    assert issue['field'] == 'definition.rule.lines.0' and issue['message'].startswith('Line 1:')
    pooled = rule('people_per_session', line(numbers={'min': {'value': 2}}, settings={'minimum_cover': {'aggregation': 'service_group'}}))['definition']
    assert 'Pooled coverage requires' in definition_issues(pooled)[0]['message']  # the old safety validations still apply
    fine = rule('hours_per_period', line(numbers={'max': {'value': 1 / 3}}, settings={'block_quota': {}}, starts_on='2026-10-01',
                                         ends_on='2026-11-01'))['definition']
    assert definition_issues(fine)[0]['code'] == 'UNSUPPORTED_SOLVER_PRECISION'


@pytest.fixture(scope='module')
def department_active_rules():
    clinics = [str(uuid.uuid4()) for _ in range(7)]
    lines = [line(numbers={'max': {'value': 3 + n % 3}}, who=person(str(uuid.uuid4())) if n % 10 else None,
                  where={'conditions': [{'field': 'service_ids', 'values': [clinics[n % 7]]}]}) for n in range(1000)]
    big = rule('duties_per_week', *lines)
    gc.collect()  # Exclude prior fixtures; collection stays enabled during this cold expansion.
    began = time.process_time()
    first = active_rules([big])
    cold = time.process_time() - began
    warm = []
    for _ in range(3):  # the best of three: a collector pause is not the resolver
        began = time.process_time()
        again = active_rules([big])
        warm.append(time.process_time() - began)
    warm = min(warm)
    print('active rules CPU seconds', {'cold': cold, 'warm': warm})
    return first, again, warm, cold, rule_lines._expanded.cache_info().hits


def test_active_rules_stays_within_budget_at_department_scale(department_active_rules):
    first, again, _, _, cache_hits = department_active_rules
    assert len(first) == len(again) == 1000
    assert cache_hits >= 1
    assert again[0] is not first[0] and again[0]['definition']['rule'] is not first[0]['definition']['rule']  # own copies each call


@pytest.mark.cpu_budget
def test_active_rules_stays_within_budget_at_department_scale_cpu_budget(department_active_rules):
    import coverage
    _, _, warm, cold, _ = department_active_rules
    # About 0.08 s cold (once per process and rule version) and 0.005 s warm on a developer machine; coverage tracing
    # (CI) is several times slower. CPU time excludes contention from parallel test workers.
    scale = 6 if coverage.Coverage.current() else 1
    assert warm < 0.05 * scale and cold < 1.0 * scale, (warm, cold)


def test_tags_on_lines_read_as_their_members_and_a_tagged_who_is_a_group():
    from packages.domain.conditions import with_tags
    team, clinic_tag = str(uuid.uuid4()), str(uuid.uuid4())
    tags = {team: {'name': 'exceptional_trainee', 'members': {'people': [A, B]}}, clinic_tag: {'name': 'VIP_Clinics', 'members': {'services': [VIP]}}}
    everyone, group, mine = line(numbers={'max': {'value': 5}}), line(who=person(team), numbers={'max': {'value': 4}}), \
        line(who=person(A), numbers={'max': {'value': 3}})
    vip = line(where={'conditions': [{'field': 'service_ids', 'values': [clinic_tag]}]}, numbers={'max': {'value': 2}})
    data = with_tags({'tags': tags, 'people': {A: {}, B: {}}, 'policies': [rule('duties_per_week', everyone, group, mine, vip)]})
    copies = {c['template_line']['id']: c for c in active_rules(data['policies'])}
    assert copies[vip['id']]['definition']['scope']['service_ids'] == [clinic_tag, VIP]
    assert internal(copies[group['id']]) == [{'field': OUTRANKED, 'op': 'none', 'values': [mine['id']]}]  # the tag is a group
    assert internal(copies[everyone['id']]) == [{'field': OUTRANKED, 'op': 'none', 'values': [group['id'], mine['id']]}]
