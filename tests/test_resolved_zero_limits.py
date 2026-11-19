"""Approved person lines determine the mandatory maximum, including a zero default."""
from copy import deepcopy

import pytest

from packages.domain.conditions import resolved_rule_context
from packages.domain.cp_sat import generate as cp_sat_generate
from packages.domain.generation import generate as greedy_generate
from packages.domain.time import aware
from packages.domain.validation import policy_applies
from .test_generation_superseded_apply import apply, group, run
from .test_live_zero_limits import empty_for_person, overtime_draft
from .test_rule_catalogue_api import line, person as person_scope
from .test_rule_catalogue_conflicts import tag_people
from .test_rule_catalogue_in_force import add_policy
from .test_rule_catalogue_v2_api import proposed


def open_duties(h, overtime):
    if overtime:
        draft, _ = overtime_draft(h)
        duty_type = h.detail(draft)['shifts'][0]['assignment_type_id']
        return h.shift(draft, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00',
                       people=[], typ=duty_type)['workspace']
    draft = h.workspace('Synthetic workload default')
    for day in ('04', '05'):
        draft = h.shift(draft, f'2026-10-{day}T08:00:00+03:00', f'2026-10-{day}T12:00:00+03:00',
                        people=[])['workspace']
    return draft


def quota_settings(draft):
    return {'metric': 'duties', 'overtime': 'only',
            'starts_on': draft['starts_on'], 'ends_on': draft['ends_on']}


def propose_cap(h, lines, current=None):
    return h.command('coordinator', 'rule.propose', {
        'catalogue_id': 'workload_limit', 'catalogue_version': 2, 'lines': lines,
        'acknowledged_overlaps': [row['id'] for row in lines[:-1]],
        **({'expected_policy_id': current['id'], 'expected_revision': current['revision']} if current else {}),
        'reason': 'Synthetic workload limits'})['record']


def activate(h, waiting):
    return h.command('approver', 'policy.activate', {'reason': 'Independent workload review'},
                     waiting['id'], waiting['revision'])['record']


def convert(h):
    conversion = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent conversion review'},
              conversion['id'], conversion['revision'])


def legacy_cap(h, rule, person, **dates):
    return activate(h, h.command('coordinator', 'policy.create', {
        'name': 'Synthetic scoped workload', 'owner': 'Test policy owner',
        'source': 'Constructed fixture', 'starts_on': '2020-01-01',
        'definition': {'rule': rule, 'scope': {}, 'severity': 'mandatory',
                       'per_person': [{'person_id': person, 'amount': 2, **dates}]},
    })['record'])


def effective_amounts(data, source, shift, person):
    start, end = aware(shift['start']), aware(shift['end'])
    rows = [row['definition']['rule'] for row in data['policies']
            if row.get('_source_policy_id', row['id']) == source['id']
            and row['definition']['severity'] == 'mandatory'
            and policy_applies(data, row, person, shift, start, end)]
    return [row.get('maximum', row.get('amount')) for row in rows]


def check_cap_rows(h, draft, source, expected):
    check = h.lifecycle(draft, 'validate')['validation']
    if source['definition']['rule']['kind'] in {'catalogue', 'block_quota'}:
        for person, amount in expected.items():
            assert [row['amount'] for row in check['quota_report']['rows']
                    if row['policy_id'] == source['id'] and row['person_id'] == person] == [amount]
    resolved = resolved_rule_context(h.snapshot(draft))
    for person, amount in expected.items():
        assert effective_amounts(resolved, source, resolved['shifts'][0], person) == [amount]


@pytest.mark.parametrize('style', ['catalogue', 'legacy_quota', 'legacy_duties'])
def test_approved_person_maximum_resolves_once_in_check_solvers_and_apply(h, style):
    allowed, capped = h.seed['people'][:2]
    draft = open_duties(h, style != 'legacy_duties')
    quota = quota_settings(draft)
    if style == 'catalogue':
        convert(h)
        everyone = line(settings={'block_quota': quota}, numbers={'max': {'value': 0}})
        personal = line(who=person_scope(allowed), settings={'block_quota': quota}, numbers={'max': {'value': 2}})
        source = activate(h, propose_cap(h, [everyone, personal]))
    elif style == 'legacy_quota':
        source = legacy_cap(h, {**quota, 'kind': 'block_quota', 'comparison': 'maximum', 'amount': 0}, allowed)
    else:
        source = legacy_cap(h, {'kind': 'max_duties', 'maximum': 0, 'window': 'week'}, allowed)

    check_cap_rows(h, draft, source, {allowed: 2, capped: 0})
    data = empty_for_person(h.snapshot(draft), allowed)
    for generate in (greedy_generate, cp_sat_generate):
        result = generate(deepcopy(data), 2)
        assert [row['participant']['person_id'] for row in result['additions']] == [allowed, allowed], (
            generate.__module__, result['status'], result.get('message'),
            [(row['code'], row.get('field'), row.get('message')) for row in result.get('issues', [])])
    job = run(h, draft, population=group(allowed))
    assert [row['participant']['person_id'] for row in job['result']['additions']] == [allowed, allowed]
    receipt = apply(h, draft, job)['reconciliation']
    assert receipt['status'] == 'applied' and receipt['addition_indexes'] == [0, 1]
    assert sum(row['person_id'] == allowed for shift in h.snapshot(draft)['shifts']
               for row in shift['participants']) == 2


def test_dated_legacy_person_maximum_leaves_the_zero_in_force_outside_its_dates(h):
    person = h.seed['people'][0]
    draft = open_duties(h, False)
    source = legacy_cap(h, {'kind': 'max_duties', 'maximum': 0, 'window': 'week'}, person,
                        starts_on='2026-10-05', ends_on='2026-10-06')
    data = resolved_rule_context(h.snapshot(draft))
    shifts = sorted(data['shifts'], key=lambda row: row['start'])
    assert [effective_amounts(data, source, shift, person) for shift in shifts] == [[0], [2]]


def test_approved_catalogue_successor_narrows_a_captured_zero(h):
    person = h.seed['people'][0]
    draft = open_duties(h, True)
    convert(h)
    settings = {'block_quota': quota_settings(draft)}
    everyone = line(settings=settings, numbers={'max': {'value': 0}})
    original = activate(h, propose_cap(h, [everyone]))
    draft = h.refresh_draft_baseline(draft)
    personal = line(who=person_scope(person), settings=settings, numbers={'max': {'value': 2}})
    waiting = propose_cap(h, [everyone, personal], current=original)
    check_cap_rows(h, draft, original, {person: 0})

    previous = activate(h, waiting)
    current = activate(h, propose_cap(h, [everyone, personal], current=previous))
    check = h.lifecycle(draft, 'validate')['validation']
    affected = {original['id'], previous['id'], current['id']}
    assert [(row['policy_id'], row['amount']) for row in check['quota_report']['rows']
            if row['person_id'] == person and row['policy_id'] in affected] == [(current['id'], 2)]
    data = empty_for_person(h.snapshot(draft), person)
    for generate in (greedy_generate, cp_sat_generate):
        assert len(generate(deepcopy(data), 2)['additions']) == 2


@pytest.mark.parametrize('control', ['no_person_line', 'person_line_proposed', 'more_specific_zero'])
def test_zero_still_binds_when_no_approved_specific_positive_line_wins(h, control):
    person = h.seed['people'][0]
    draft = open_duties(h, True)
    convert(h)
    settings = {'block_quota': quota_settings(draft)}
    everyone = line(settings=settings, numbers={'max': {'value': 2 if control == 'more_specific_zero' else 0}})
    personal = line(who=person_scope(person), settings=settings,
                    numbers={'max': {'value': 0 if control == 'more_specific_zero' else 2}})
    source = activate(h, propose_cap(h, [everyone, personal] if control == 'more_specific_zero' else [everyone]))
    if control == 'person_line_proposed':
        waiting = propose_cap(h, [everyone, personal], current=source)
        assert waiting['status'] == 'proposed'
    check_cap_rows(h, draft, source, {person: 0})
    data = empty_for_person(h.snapshot(draft), person)
    for generate in (greedy_generate, cp_sat_generate):
        assert generate(deepcopy(data), 2)['additions'] == []


def test_approved_person_maximum_outranks_a_people_tag_zero(h):
    allowed, capped = h.seed['people'][:2]
    tag = tag_people(h, 'synthetic_scope', [allowed, capped])
    draft = open_duties(h, True)
    convert(h)
    settings = {'block_quota': quota_settings(draft)}
    group_zero = line(who=person_scope(tag), settings=settings, numbers={'max': {'value': 0}})
    personal = line(who=person_scope(allowed), settings=settings, numbers={'max': {'value': 2}})
    source = activate(h, propose_cap(h, [group_zero, personal]))
    check_cap_rows(h, draft, source, {allowed: 2, capped: 0})


def test_inherited_advisory_person_maximum_keeps_the_mandatory_zero(h):
    person = h.seed['people'][0]
    tag = tag_people(h, 'synthetic_scope', [person])
    draft = open_duties(h, False)
    convert(h)
    settings = {'max_duties': {'window': 'week'}}
    everyone = line(settings=settings, numbers={'max': {'value': 0}})
    advisory = line(who=person_scope(tag), settings=settings, numbers={'max': {'value': 1, 'level': 'high'}})
    personal = line(who=person_scope(person), settings=settings, numbers={'max': {'value': 2, 'level': None}})
    draft = h.shift(draft, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00',
                    people=[person])['workspace']
    # New proposals store a default strength. Exercise the inherited level retained on stored approved lines.
    source = add_policy(h, {'kind': 'catalogue', 'id': 'workload_limit', 'catalogue_version': 2,
                            'lines': [everyone, advisory, personal]},
                        approved_by=h.seed['identities']['approver'])
    check = h.lifecycle(draft, 'validate')['validation']
    data = resolved_rule_context(h.snapshot(draft))
    assert effective_amounts(data, source, data['shifts'][0], person) == [0]
    assert [row['code'] for row in check['findings'] if row['rule_id'] == source['id']
            and row['person_id'] == person and row['severity'] == 'error'] == ['MAX_DUTIES']
