"""Adversarial combinations from the settled catalogue contract (§2): only matching lines replace numbers.

Expected behavior is independent of expansion internals. These cases exercise the canonical applicability reader.
"""
from datetime import datetime

import pytest

from packages.domain.conditions import active_rules
from packages.domain.quotas import applicable_policies
from packages.domain.solver_rules import definition_issues
from packages.domain.validation import policy_applies
from .test_rule_lines import A, B, C, PROG, VIP, line, person, rule
from .test_rule_lines_inherit import n


def world(policies):
    return {'organisation': {'timezone': 'UTC'}, 'people': {A: {'tags': ['selected']}, B: {'tags': []}},
            'types': {'t': {'department_id': 'd'}}, 'categories': {C: {'active': True}, VIP: {'active': True}},
            'programmes': {PROG: {'active': True, 'starts_on': '2020-01-01'}},
            'memberships': [{'person_id': p, 'department_id': 'd', 'category_id': rank, 'starts_on': '2020-01-01'}
                            for p, rank in ((A, C), (B, VIP))],
            'programme_memberships': [{'person_id': A, 'programme_id': PROG, 'starts_on': '2020-01-01', 'ends_on': '2027-01-01'}],
            'policies': policies}


def matches(data, copy, who=A, day='2026-10-05'):
    start, end = (datetime.fromisoformat(f'{day}T{clock}:00+00:00') for clock in ('08:00', '12:00'))
    return policy_applies(data, copy, who, {'id': 's', 'assignment_type_id': 't', 'service_ids': [], 'location_id': None}, start, end)


@pytest.mark.parametrize('field,value', [('person_ids', A), ('category_ids', C), ('programme_ids', PROG), ('tags', 'selected')],
                         ids=['person', 'rank', 'programme', 'tag'])
@pytest.mark.parametrize('expanded', [False, True], ids=['stored', 'expanded'])
def test_explicit_none_clears_only_matching_people_during_its_dates(field, value, expanded):
    # Unlike an omitted max, max=None removes it (schemas.LineNumber; catalogue §2, decision 2).
    everyone = line(numbers={'max': n(5)})
    own = line(who={'conditions': [{'field': field, 'values': [value]}]}, numbers={'max': n(None)},
               starts_on='2026-10-05', ends_on='2026-10-07')
    row = rule('duties_per_week', everyone, own)
    copies = active_rules([row])
    data = world(copies if expanded else [row])
    broad = next(c for c in copies if c['template_line']['id'] == everyone['id'])
    assert [matches(data, broad, day=day) for day in ('2026-10-04', '2026-10-05', '2026-10-06', '2026-10-07')] == [True, False, False, True]
    assert matches(data, broad, B), 'a nonmatching person keeps the broad bound'


@pytest.mark.parametrize('expanded', [False, True], ids=['stored', 'expanded'])
def test_partial_override_of_a_rank_minimum_does_not_reinstate_everyones_maximum(expanded):
    dates = {'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}
    everyone = line(numbers={'min': n(2), 'max': n(5)}, **dates)
    ranked = line(who={'conditions': [{'field': 'category_ids', 'values': [C]}]}, numbers={'min': n(1), 'max': n(7)}, **dates)
    own = line(who=person(A), numbers={'min': n(0)}, **dates)
    row = rule('duties_per_month', everyone, ranked, own)
    copies = active_rules([row])
    data = world(copies if expanded else [row])
    effective = [(c['definition']['rule']['comparison'], c['definition']['rule']['amount']) for c in copies if matches(data, c)]
    assert sorted(effective) == [('maximum', 7), ('minimum', 0)]


def test_line_ids_are_scoped_to_their_policy():
    # The contract requires uniqueness within a rule, not across unrelated catalogue entries.
    weekly_default, weekly_person = line(numbers={'max': n(5)}), line(who=person(A), numbers={'max': n(3)})
    hours_person = {**line(who=person(B), numbers={'max': n(40)}), 'id': weekly_person['id']}
    hours = rule('hours_per_week', line(numbers={'max': n(48)}), hours_person)
    weekly = rule('duties_per_week', weekly_default, weekly_person)
    copies = active_rules([hours, weekly])
    broad = next(c for c in copies if c['template_line']['id'] == weekly_default['id'])
    assert not matches(world(copies), broad), 'another rule sharing a line ID must not change who outranks this bound'
    assert matches(world(copies), broad, B)


@pytest.mark.parametrize('field,value', [('category_ids', C), ('programme_ids', PROG)], ids=['rank', 'programme'])
@pytest.mark.parametrize('member', [False, True], ids=['not-in-group', 'in-group'])
def test_compound_person_quota_replaces_only_when_the_other_who_condition_matches(field, value, member):
    dates = {'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}
    everyone = line(numbers={'max': n(5)}, **dates)
    who = {'match': 'all', 'conditions': [{'field': 'person_ids', 'values': [A]}, {'field': field, 'values': [value]}]}
    row = rule('duties_per_month', everyone, line(who=who, numbers={'max': n(2)}, **dates))
    copies = active_rules([row])
    data = world(copies)
    if not member:
        data['memberships'][0]['category_id'] = VIP
        data['programme_memberships'] = []
    assert [c['definition']['rule']['amount'] for c in copies if matches(data, c)] == ([2] if member else [5])


@pytest.mark.parametrize('unrelated', ['different-people', 'ended'])
@pytest.mark.parametrize('reverse', [False, True], ids=['original-order', 'reversed-order'])
def test_blank_strength_comes_from_the_broader_line_that_covers_the_person(unrelated, reverse):
    # Catalogue §2 says blank strength comes from broader lines that "also cover its people".
    stale = line(who={'conditions': [{'field': 'category_ids', 'values': [VIP if unrelated == 'different-people' else C]}]},
                 numbers={'max': n(3, 'low')}, **({'ends_on': '2026-10-01'} if unrelated == 'ended' else {}))
    current = line(who={'conditions': [{'field': 'category_ids', 'values': [C]}]}, numbers={'max': n(4)}, starts_on='2026-10-01')
    own = line(who=person(A), numbers={'max': n(6, None)}, starts_on='2026-10-01')
    group = [stale, current]
    row = rule('duties_per_week', *(list(reversed(group)) if reverse else group), own)
    copies = active_rules([row])
    applicable = [c for c in copies if matches(world(copies), c)]
    assert [(c['definition']['rule']['maximum'], c['definition']['severity']) for c in applicable] == [(6, 'mandatory')]


def test_quota_explicit_block_does_not_apply_before_its_line_starts():
    row = rule('duties_per_month', line(numbers={'max': n(1)}, starts_on='2026-11-01', ends_on='2026-12-01',
                                      settings={'block_quota': {'starts_on': '2026-10-01', 'ends_on': '2027-01-01'}}))
    data = world([row])
    data['workspace'] = {'department_id': 'd', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}
    assert applicable_policies(data) == [], 'a November line must not constrain an October rota'


@pytest.mark.parametrize('numbers', [{'min': n(5), 'max': n(2)}, {'min': n(2), 'max': n(5), 'target': n(6, 'medium')}])
def test_contradictory_numbers_of_the_same_measure_are_reported(numbers):
    row = rule('duties_per_month', line(numbers=numbers, starts_on='2026-10-01', ends_on='2026-11-01'))
    assert definition_issues(row['definition']), 'the old quota contract requires min <= target <= max'


@pytest.mark.parametrize('change', ['rank', 'programme', 'source-dates', 'equal-tier'])
def test_inherited_quota_strength_keeps_one_budget_across_dated_changes(h, change):
    from packages.domain.cp_sat import generate
    from packages.domain.score import schedule_score
    from packages.domain.validation import evaluate
    pid = h.seed['people'][0]
    w = h.workspace('Dated quota strength', '2026-10-01', '2026-11-01')
    for day in (5, 10, 20):
        w = h.shift(w, f'2026-10-{day:02}T08:00:00+03:00', f'2026-10-{day:02}T12:00:00+03:00', people=[pid])['workspace']
    data = h.snapshot(w)
    data['people'] = {pid: data['people'][pid]}
    data['memberships'] = [m for m in data['memberships'] if m['person_id'] == pid]
    dates = {'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}
    scopes = [{'conditions': [{'field': 'category_ids', 'values': [key]}]} for key in (C, VIP)]
    if change in ('rank', 'programme'):
        field, membership, definitions, key = ('category_ids', 'memberships', 'categories', 'category_id') if change == 'rank' else (
            'programme_ids', 'programme_memberships', 'programmes', 'programme_id')
        scopes = [{'conditions': [{'field': field, 'values': [v]}]} for v in (C, VIP)]
        data.setdefault(definitions, {}).update({v: {'id': v, 'active': True, **dates} for v in (C, VIP)})
        data.setdefault(membership, []).extend([
            {'person_id': pid, 'department_id': data['workspace']['department_id'], key: C, **dates, 'ends_on': '2026-10-15'},
            {'person_id': pid, 'department_id': data['workspace']['department_id'], key: VIP, **dates, 'starts_on': '2026-10-15'}])
        spans = [dates, dates]
    else:
        data['people'][pid]['tags'] = ['selected']
        scopes = [{'conditions': [{'field': 'tags', 'values': ['selected']}]}] * 2
        spans = [{**dates, 'ends_on': '2026-10-15'}, {**dates, 'starts_on': '2026-10-15'}] if change == 'source-dates' else [dates, dates]
    broader = [line(who=scope, numbers={'min': n(2, level), 'max': n(2, level)}, **span)
               for scope, span, level in zip(scopes, spans, ('high', 'low'))]
    own = line(who=person(pid), numbers={'min': n(2, None), 'max': n(2, None)}, **dates)
    row = rule('duties_per_month', *broader, own, department_id=data['workspace']['department_id'])
    data['policies'] = [row]
    data['quota_context'] = {'complete': True, 'start': '2026-10-01T00:00:00+03:00', 'end': '2026-11-01T00:00:00+03:00'}
    for shift in data['shifts']:
        for participant in shift['participants']:
            participant['fixed'] = True
    validation = evaluate(data)
    rows = validation['quota_report']['rows']
    assert sorted((r['comparison'], r['amount'], r['actual'], r['severity'], r['strength']) for r in rows) == [
        ('maximum', 2, 3, 'advisory', 'high'), ('minimum', 2, 3, 'advisory', 'high')]
    assert validation['valid'], validation['findings']
    assert schedule_score(data, validation)['components']['rules']['value'] == pytest.approx(0.75)
    result = generate(data, 2, deterministic=True)
    assert result['status'] == 'candidate_feasible', result
    assert result['validation']['valid']
    assert len(result['validation']['quota_report']['rows']) == 2


def test_canonical_proof_keeps_compound_who_and_normalizes_inherited_source_ids():
    from packages.domain.rule_groups import expanded
    broad = line(numbers={'max': n(5)})
    compound = line(who={'conditions': [{'field': 'person_ids', 'values': [A]}, {'field': 'category_ids', 'values': [C]}]},
                    numbers={'max': n(3)})
    row = rule('duties_per_week', broad, compound)
    plain = {**row, 'definition': {'rule': {**row['definition']['rule'], 'lines': [broad, {**compound, 'who': person(A)}]}}}
    assert expanded([row]) != expanded([plain]), 'a rank condition cannot vanish from a proof of replacement'
    current = line(who={'conditions': [{'field': 'category_ids', 'values': [C]}]}, numbers={'max': n(4, 'high')})
    other = line(who={'conditions': [{'field': 'tags', 'values': ['selected']}]}, numbers={'max': n(4, 'low')})
    own = line(who=person(A), numbers={'max': n(6, None)})
    old = rule('duties_per_week', current, other, own)
    moved = rule('duties_per_week', other, current, own)
    assert expanded([old], {old['id']: moved['id']}) == expanded([moved])
