"""One condition model for every rule: the evaluator, the validator, the solver and the rule preview."""
import copy
from datetime import datetime

import pytest
from sqlalchemy import select
from pydantic import ValidationError

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import Audience, PolicyDefinition, Scope
from packages.domain.conditions import OUTRANKED, OVERRIDDEN, ever_matches, evaluate, source_index, scope_applies, scope_matches_throughout, scope_segments
from packages.domain.cp_sat import generate
from packages.domain.quotas import quota_contribution
from packages.domain.rule_lines import scope_of
from packages.domain.validation import person_in_scope, scope_matches, policy_applies, workload_matches

T = datetime.fromisoformat


def line(field, *values, op='any'):
    return {'field': field, 'op': op, 'values': list(values)}


def tree(*lines, match='all'):
    return {'match': match, 'conditions': list(lines)}


def data():
    """Person p: rank R1 until 1 Oct then R2; skill S from 1 Oct; programme P in Oct; rotation type RT and cohort C."""
    return {
        'organisation': {'timezone': 'Asia/Riyadh'},
        'people': {'p': {'tags': ['Night']}, 'q': {'tags': []}},
        'categories': {'R1': {'active': True}, 'R2': {'active': True}},
        'capabilities': {'S': {'active': True}},
        'programmes': {'P': {'active': True, 'starts_on': '2020-01-01'}},
        'rotation_types': {'RT': {'active': True}},
        'cohorts': {'C': {'starts_on': '2026-09-01', 'ends_on': '2026-11-01'}},
        'types': {'T': {'department_id': 'd'}, 'U': {'department_id': 'd'}},
        'locations': {'room': {'parent_id': 'wing'}, 'wing': {'parent_id': None}},
        'memberships': [{'person_id': 'p', 'department_id': 'd', 'category_id': 'R1', 'starts_on': '2020-01-01', 'ends_on': '2026-10-01'},
                        {'person_id': 'p', 'department_id': 'd', 'category_id': 'R2', 'starts_on': '2026-10-01'},
                        {'person_id': 'q', 'department_id': 'd', 'category_id': 'R1', 'starts_on': '2020-01-01'}],
        'grants': [{'person_id': 'p', 'department_id': 'd', 'capability_id': 'S', 'service_id': None, 'starts_on': '2026-10-01'}],
        'programme_memberships': [{'person_id': 'p', 'programme_id': 'P', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}],
        'rotations': [{'person_id': 'p', 'rotation_type_id': 'RT', 'cohort_id': 'C', 'starts_on': '2026-10-10', 'ends_on': '2026-10-20'}],
    }


SHIFT = {'service_ids': ['clinic'], 'location_id': 'room', 'assignment_type_id': 'T'}
SEPT = (T('2026-09-20T08:00:00+03:00'), T('2026-09-20T12:00:00+03:00'))
OCT = (T('2026-10-12T08:00:00+03:00'), T('2026-10-12T12:00:00+03:00'))
NOV = (T('2026-11-12T08:00:00+03:00'), T('2026-11-12T12:00:00+03:00'))


def quota_data():
    source, pid = data(), '00000000-0000-4000-8000-000000000001'
    source['people'][pid] = source['people'].pop('p')
    for name in ('memberships', 'grants', 'programme_memberships', 'rotations'):
        for row in source[name]:
            if row['person_id'] == 'p':
                row['person_id'] = pid
    return source, pid


def matches(conditions, when, person='p', shift=SHIFT):
    return scope_matches(data(), {'conditions': conditions}, person, shift, *when)


@pytest.mark.parametrize('field,value,sept,octo,nov', [
    ('category_ids', 'R1', True, False, False),     # dated rank: promoted on 1 Oct
    ('category_ids', 'R2', False, True, True),
    ('capability_ids', 'S', False, True, True),     # dated skill grant
    ('programme_ids', 'P', False, True, False),     # dated programme membership
    ('rotation_type_ids', 'RT', False, True, False),  # rotation 10–20 Oct
    ('cohort_ids', 'C', False, True, False),        # only while the person's rotation in that cohort is current
    ('tags', 'night', True, True, True),            # tags match regardless of case
    ('person_ids', 'p', True, True, True),
    ('service_ids', 'clinic', True, True, True),
    ('location_ids', 'wing', True, True, True),     # a place includes everything inside it
    ('assignment_type_ids', 'T', True, True, True),
])
def test_every_field_is_evaluated_at_the_time_of_the_duty(field, value, sept, octo, nov):
    assert [matches(tree(line(field, value)), when) for when in (SEPT, OCT, NOV)] == [sept, octo, nov]
    # "none of" is the exact opposite, so "everyone except" works for every field.
    assert [matches(tree(line(field, value, op='none')), when) for when in (SEPT, OCT, NOV)] == [not sept, not octo, not nov]


def test_boundaries_are_exclusive_end_dates():
    last_day_r1 = (T('2026-09-30T20:00:00+03:00'), T('2026-09-30T23:00:00+03:00'))
    across = (T('2026-09-30T20:00:00+03:00'), T('2026-10-01T02:00:00+03:00'))
    assert matches(tree(line('category_ids', 'R1')), last_day_r1)
    # Each rank's obligation applies, but neither rank qualifies throughout the promotion window.
    for rank in ('R1', 'R2'):
        scope = {'conditions': tree(line('category_ids', rank))}
        assert scope_applies(data(), scope, 'p', SHIFT, *across)
        assert not scope_matches_throughout(data(), scope, 'p', SHIFT, *across)
    assert matches(tree(line('rotation_type_ids', 'RT')), (T('2026-10-19T08:00:00+03:00'), T('2026-10-19T12:00:00+03:00')))
    assert not matches(tree(line('rotation_type_ids', 'RT')), (T('2026-10-20T08:00:00+03:00'), T('2026-10-20T12:00:00+03:00')))


@pytest.mark.parametrize('shift', [None, SHIFT])
def test_scope_segments_keep_simultaneous_dated_any_branches(shift):
    scope = {'conditions': tree(line('category_ids', 'R1'), line('programme_ids', 'P'), match='any')}
    points = [T(f'{day}T00:00:00+03:00') for day in
              ('2026-09-30', '2026-10-01', '2026-10-10', '2026-10-20', '2026-11-01', '2026-11-02')]
    assert list(scope_segments(data(), scope, 'p', shift, points[0], points[-1])) == [
        (lo, hi, matched) for lo, hi, matched in zip(points, points[1:], (True, True, True, True, False))]
    assert scope_applies(data(), scope, 'p', shift, points[0], points[-1])
    assert not scope_matches_throughout(data(), scope, 'p', shift, points[0], points[-1])


def test_scope_segments_keep_transitive_override_boundaries():
    source = data()
    source['policies'] = [
        {'id': 'middle', 'definition': {'rule': {'kind': 'max_hours', 'hours': 40},
                                       'scope': {'conditions': tree(line(OVERRIDDEN, 'inner'))}}},
        {'id': 'inner', 'starts_on': '2026-10-04', 'ends_on': '2026-10-06',
         'definition': {'rule': {'kind': 'max_hours', 'hours': 32}, 'scope': {'person_id': 'p'}}}]
    scope = {'conditions': tree(line(OVERRIDDEN, 'middle', op='none'))}
    a, first, last, b = [T(f'2026-10-{day:02}T00:00:00+03:00') for day in (1, 4, 6, 8)]
    assert list(scope_segments(source, scope, 'p', None, a, b, extra_points={first, last})) == [
        (a, first, True), (first, last, False), (last, b, True)]


@pytest.mark.parametrize('a,b', [(OCT[0], OCT[0]), (OCT[1], OCT[0])])
def test_scope_segments_keep_empty_and_inverted_windows_false(a, b):
    assert list(scope_segments(data(), {'person_id': 'p'}, 'p', None, a, b)) == [(a, b, False)]
    assert not scope_applies(data(), {'person_id': 'p'}, 'p', None, a, b)
    assert not scope_matches_throughout(data(), {'person_id': 'p'}, 'p', None, a, b)


def test_static_person_scope_does_not_split_at_unrelated_dated_facts():
    a, b = T('2026-09-30T00:00:00+03:00'), T('2026-11-02T00:00:00+03:00')
    assert list(scope_segments(data(), {'person_id': 'p'}, 'p', None, a, b)) == [(a, b, True)]


@pytest.mark.parametrize('field,value', [('category_ids', 'R2'), ('capability_ids', 'S'), ('programme_ids', 'P'),
                                       ('rotation_type_ids', 'RT'), ('cohort_ids', 'C'), ('tags', 'night')])
@pytest.mark.parametrize('transition', ['join', 'leave'])
@pytest.mark.parametrize('op', ['any', 'all', 'none'])
def test_flat_and_nested_person_scopes_select_the_same_moments(field, value, transition, op):
    source, pid = quota_data()
    dates = {'starts_on': '2026-10-01' if transition == 'join' else '2020-01-01',
             'ends_on': '2026-11-01' if transition == 'join' else '2026-10-01'}
    records = {'category_ids': ('memberships', 'category_id'), 'capability_ids': ('grants', 'capability_id'),
               'programme_ids': ('programme_memberships', 'programme_id'),
               'rotation_type_ids': ('rotations', 'rotation_type_id'), 'cohort_ids': ('rotations', 'cohort_id')}
    if field in records:
        rows, key = records[field]
        for row in source[rows]:
            if row['person_id'] == pid and row[key] == value:
                row.update(dates)
    before, midnight, after = (T('2026-09-30T23:00:00+03:00'), T('2026-10-01T00:00:00+03:00'),
                               T('2026-10-01T02:00:00+03:00'))
    predicate = tree(line(field, value, op=op))
    for who in (predicate, tree(predicate), tree(predicate, match='any')):
        scope = scope_of({'who': who})
        for a, b, held in ((before, midnight, transition == 'leave'), (midnight, after, transition == 'join'),
                           (before, after, True)):
            # A negative predicate also applies if its exclusion is absent during any part of the duty.
            expected = (field == 'tags' or held) if op != 'none' else (field != 'tags' and (not held or a < midnight < b))
            assert scope_applies(source, scope, pid, SHIFT, a, b) == expected
            assert scope_applies(source, scope, pid, None, a, b) == expected
            qualifies = expected and not (field != 'tags' and a < midnight < b)
            assert scope_matches_throughout(source, scope, pid, SHIFT, a, b) == qualifies
            assert person_in_scope(source, scope, pid, a, b) == qualifies
            assert ever_matches(source, who, pid, a, b) == expected
            shift = {**SHIFT, 'start': a.isoformat(), 'end': b.isoformat(), 'type_snapshot': {'definition': {}}}
            participant = {'person_id': pid, 'start': a.isoformat(), 'end': b.isoformat()}
            policy = {'definition': {'scope': scope, 'rule': {'assignment_type_ids': [], 'participation': [],
                      'overtime': 'include', 'metric': 'duties'}}}
            contribution = quota_contribution(source, policy, pid, shift, participant, source['memberships'], before, after)
            contributes = qualifies
            if field in {'rotation_type_ids', 'cohort_ids'} and a < midnight < b:
                contributes = (transition == 'leave') if op != 'none' else (transition == 'join')
            assert (contribution is not None) == contributes


def test_scope_conjunctions_require_one_simultaneous_moment():
    source = data()
    a, b = T('2026-09-30T23:00:00+03:00'), T('2026-10-01T02:00:00+03:00')
    for who in (tree(line('category_ids', 'R1'), line('programme_ids', 'P')),
                tree(tree(line('category_ids', 'R1'), line('programme_ids', 'P'))),
                tree(line('category_ids', 'R1', 'R2', op='all'))):
        scope = scope_of({'who': who})
        assert not scope_applies(source, scope, 'p', SHIFT, a, b)
        assert not scope_applies(source, scope, 'p', None, a, b)
    for who in (tree(line('programme_ids', 'P')), tree(tree(line('programme_ids', 'P')))):
        scope = {**scope_of({'who': who}), 'start_time': '22:00', 'end_time': '24:00'}
        assert not scope_applies(source, scope, 'p', SHIFT, a, b)


@pytest.mark.parametrize('same_rank', [False, True])
def test_quota_rank_counting_accepts_continuous_matching_memberships_equally(same_rank):
    source, pid = quota_data()
    if same_rank:
        source['memberships'][1]['category_id'] = 'R1'
    a, b = T('2026-09-30T23:00:00+03:00'), T('2026-10-01T02:00:00+03:00')
    who = tree(line('category_ids', *(['R1'] if same_rank else ['R1', 'R2'])))
    for selector in (who, tree(who)):
        scope = scope_of({'who': selector})
        assert scope_matches_throughout(source, scope, pid, SHIFT, a, b)
        shift = {**SHIFT, 'start': a.isoformat(), 'end': b.isoformat(), 'type_snapshot': {'definition': {}}}
        participant = {'person_id': pid, 'start': a.isoformat(), 'end': b.isoformat()}
        policy = {'definition': {'scope': scope, 'rule': {'assignment_type_ids': [], 'participation': [],
                  'overtime': 'include', 'metric': 'duties'}}}
        contribution = quota_contribution(source, policy, pid, shift, participant, source['memberships'], a, b)
        assert contribution is not None and contribution['duty']


@pytest.mark.parametrize('precedence', [OUTRANKED, OVERRIDDEN])
@pytest.mark.parametrize('field,value', [('rotation_type_ids', 'RT'), ('cohort_ids', 'C')])
@pytest.mark.parametrize('nested', [False, True])
def test_quota_precedence_uses_the_same_starting_placement_as_contributions(precedence, field, value, nested):
    source, pid = quota_data()
    source['rotations'][0].update(starts_on='2026-10-01')
    who = tree(line(field, value))
    selected = scope_of({'who': tree(who) if nested else who})
    higher = {'id': 'specific', 'starts_on': '2020-01-01', 'definition': {'scope': selected,
              'rule': {'kind': 'block_quota'}}, '_catalogue_sources': {
                  'specific': {'starts_on': '2020-01-01', 'definition': {'scope': selected}}}}
    source['policies'] = [higher]
    general = {'conditions': tree(line(precedence, 'specific', op='none'))}
    before, midnight, after = T('2026-09-30T23:00:00+03:00'), T('2026-10-01T00:00:00+03:00'), T('2026-10-01T02:00:00+03:00')
    for a, b, expected in ((before, midnight, [True, False]), (before, after, [True, False]),
                           (midnight, after, [False, True])):
        shift = {**SHIFT, 'start': a.isoformat(), 'end': b.isoformat(), 'type_snapshot': {'definition': {}}}
        participant = {'person_id': pid, 'start': a.isoformat(), 'end': b.isoformat()}
        counted = []
        for scope in (general, selected):
            policy = {'definition': {'scope': scope, 'rule': {'assignment_type_ids': [], 'participation': [],
                      'overtime': 'include', 'metric': 'duties'}}}
            counted.append(quota_contribution(source, policy, pid, shift, participant,
                                              source['memberships'], before, after) is not None)
        assert counted == expected


def test_source_scope_cache_grows_with_scopes_instead_of_duties():
    source = {**data(), 'policies': [], '_line_index_cache': {}}
    scope = {'conditions': tree(line(OUTRANKED, 'missing', op='none'))}
    for day in range(1, 29):
        a, b = T(f'2026-10-{day:02}T08:00:00+03:00'), T(f'2026-10-{day:02}T12:00:00+03:00')
        assert scope_applies(source, scope, 'p', SHIFT, a, b)
    assert len(source_index(source['policies'], source['_line_index_cache'])[5]) == 1


def test_source_date_cache_is_shared_across_windows_and_people_but_not_policy_pools(monkeypatch):
    from packages.domain import conditions
    source = {**data(), '_line_index_cache': {}, '_person_dates_cache': {}}
    a, b = T('2026-09-30T23:00:00+03:00'), T('2026-10-01T02:00:00+03:00')
    # Warm person boundaries; below we measure only conversion of the source's two dates.
    for pid in ('p', 'q'):
        conditions.change_points(source, pid, a, b)
    replacement = {'id': 'replacement', 'starts_on': '2026-10-01', 'ends_on': '2026-10-02',
                   'definition': {'scope': {'tags': ['Night']}, 'rule': {'kind': 'max_duties'}}}
    source['policies'] = [replacement]
    scope = {'conditions': tree(line(OVERRIDDEN, 'replacement', op='none'))}
    original, converted = conditions.local_midnight, []
    def measured(day, zone):
        converted.append(day)
        return original(day, zone)
    monkeypatch.setattr(conditions, 'local_midnight', measured)
    windows = [(a, T('2026-10-01T00:00:00+03:00'), True, True), (a, b, True, False),
               (T('2026-10-01T00:00:00+03:00'), b, False, False),
               (T('2026-10-02T00:00:00+03:00'), T('2026-10-02T02:00:00+03:00'), True, True)]
    for lo, hi, applies, throughout in windows:
        for pid in ('p', 'q'):
            assert scope_applies(source, scope, pid, SHIFT, lo, hi) == (pid == 'q' or applies)
            assert scope_matches_throughout(source, scope, pid, SHIFT, lo, hi) == (pid == 'q' or throughout)
    assert len(converted) == 2  # Convert each source date once, independent of people and checked windows.
    source['policies'] = [{**replacement, 'starts_on': '2026-10-02', 'ends_on': '2026-10-03'}]
    assert scope_matches_throughout(source, scope, 'p', SHIFT, a, b)
    assert len(converted) == 4  # A new policy pool has its own boundaries, even with the same rule id and scope.


def test_obligation_needs_simultaneous_rule_dates_and_membership():
    source = data()
    source['programme_memberships'][0].update(starts_on='2020-01-01', ends_on='2026-10-01')
    a, b = T('2026-09-30T23:00:00+03:00'), T('2026-10-01T02:00:00+03:00')
    for scope in ({'programme_id': 'P'}, {'conditions': tree(tree(line('programme_ids', 'P')))}):
        policy = {'status': 'active', 'active': True, 'department_id': 'd', 'starts_on': '2026-10-01',
                  'definition': {'scope': scope, 'rule': {'kind': 'min_rest', 'hours': 4}}}
        assert not policy_applies(source, policy, 'p', SHIFT, a, b)
        assert not workload_matches(source, policy, 'p', SHIFT, a, b)


def test_allocation_permission_requires_scope_throughout_duty():
    from packages.domain.generation import allocation_policy_matches
    source = data()
    a, b = T('2026-09-30T23:00:00+03:00'), T('2026-10-01T02:00:00+03:00')
    for scope in ({'programme_id': 'P'}, {'conditions': tree(tree(line('programme_ids', 'P')))}):
        policy = {'status': 'active', 'active': True, 'department_id': 'd', 'starts_on': '2020-01-01',
                  'definition': {'scope': scope, 'rule': {'kind': 'allocation_overtime'}}}
        shift = {**SHIFT, 'start': a.isoformat(), 'end': b.isoformat()}
        assert policy_applies(source, policy, 'p', shift, a, b)
        assert not allocation_policy_matches(source, policy, shift, 'p')
        assert allocation_policy_matches(source, policy, {**shift, 'start': '2026-10-01T00:00:00+03:00'}, 'p')


@pytest.mark.parametrize('field', ['rotation_type_ids', 'cohort_ids'])
@pytest.mark.parametrize('transition', ['join', 'leave'])
def test_rotation_scope_tracks_parent_programme_definition_midnight(field, transition):
    source = data()
    source['rotations'][0].update(starts_on='2020-01-01', ends_on='2026-11-01')
    source['cohorts']['C']['programme_id'] = 'P'
    source['programme_memberships'] = []  # The rotation's parent definition supplies the only boundary.
    source['programmes']['P'].update(starts_on='2026-10-01' if transition == 'join' else '2020-01-01',
                                    ends_on='2026-11-01' if transition == 'join' else '2026-10-01')
    a, b = T('2026-09-30T23:00:00+03:00'), T('2026-10-01T02:00:00+03:00')
    value = 'RT' if field == 'rotation_type_ids' else 'C'
    for who in (tree(line(field, value)), tree(tree(line(field, value)))):
        scope = scope_of({'who': who})
        assert scope_applies(source, scope, 'p', SHIFT, a, b)
        assert scope_applies(source, scope, 'p', None, a, b)
        assert not scope_matches_throughout(source, scope, 'p', SHIFT, a, b)


def test_groups_and_or_with_one_nested_level():
    except_r2 = tree(line('service_ids', 'clinic'), line('category_ids', 'R2', op='none'))
    assert matches(except_r2, SEPT) and not matches(except_r2, OCT)
    either = tree(line('tags', 'locum'), tree(line('category_ids', 'R2'), line('programme_ids', 'P'), match='all'), match='any')
    assert not matches(either, SEPT) and matches(either, OCT) and not matches(either, NOV)
    assert matches(tree(line('person_ids', 'x'), line('person_ids', 'q'), match='any'), SEPT, person='q')
    with pytest.raises(ValidationError):
        Scope(conditions=tree(tree(tree(line('tags', 'a')))))


def test_session_selection_leaves_person_lines_to_the_per_person_check():
    # With no person in view (coverage session selection, solver pre-filters) a person line counts as met;
    # session lines still select sessions.
    rule = tree(line('service_ids', 'clinic'), line('category_ids', 'R2', op='none'))
    assert scope_matches(data(), {'conditions': rule}, None, SHIFT, *OCT)
    assert not scope_matches(data(), {'conditions': rule}, None, {**SHIFT, 'service_ids': ['other']}, *OCT)
    assert evaluate(rule, lambda field: None)


def test_legacy_fields_and_conditions_both_apply():
    scope = {'category_ids': ['R1'], 'tags': ['Night'], 'conditions': tree(line('person_ids', 'q', op='none'))}
    assert scope_matches(data(), scope, 'p', SHIFT, *SEPT)
    assert not scope_matches(data(), scope, 'p', SHIFT, *OCT)          # rank R1 (legacy) no longer holds
    assert not scope_matches(data(), {**scope, 'category_ids': []}, 'q', SHIFT, *SEPT)  # no tag, and excluded


def test_ever_matches_finds_any_moment_in_a_period():
    a, b = T('2026-09-01T00:00:00+03:00'), T('2026-12-01T00:00:00+03:00')
    assert ever_matches(data(), tree(line('programme_ids', 'P')), 'p', a, b)
    assert not ever_matches(data(), tree(line('programme_ids', 'P')), 'q', a, b)
    assert ever_matches(data(), tree(line('category_ids', 'R1', op='none')), 'p', a, b)
    assert not ever_matches(data(), tree(line('category_ids', 'R1', op='none')), 'q', a, b)


def test_contract_fields_per_context():
    with pytest.raises(ValidationError):
        Scope(conditions=tree(line('roles', 'staff')))
    with pytest.raises(ValidationError):
        Audience(where=tree(line('location_ids', 'x')))
    assert Audience(where=tree(line('roles', 'staff'))).where
    service = '00000000-0000-0000-0000-000000000001'
    pooled = {'rule': {'kind': 'minimum_cover', 'minimum': 1, 'aggregation': 'service_group'}}
    with pytest.raises(ValidationError):
        PolicyDefinition.model_validate(pooled)
    assert PolicyDefinition.model_validate({**pooled, 'scope': {'conditions': tree(line('service_ids', service))}})


def consultant(h, person):
    """Make one seed person a 'consultant' (the second seed rank) from 2020."""
    with transaction(h.app.state.engine, write=True) as db:
        db.query(m.Membership).filter_by(person_id=person).delete()
        db.add(m.Membership(person_id=person, category_id=h.seed['trainee_category'], department_id=h.seed['department_id'],
                            starts_on=datetime(2020, 1, 1).date()))
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1


def test_validator_max_hours_for_everyone_except_consultants(h):
    a, b, _ = h.seed['people']
    consultant(h, a)
    rule = h.policy({'kind': 'max_hours', 'hours': 4, 'window': 'day'},
                    {'conditions': tree(line('category_ids', h.seed['trainee_category'], op='none'))})
    w = h.workspace()
    for person in (a, b):
        w = h.shift(w, '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00', people=[person])['workspace']
        w = h.shift(w, '2026-10-04T13:00:00+03:00', '2026-10-04T17:00:00+03:00', people=[person], location=1)['workspace']
    findings = h.lifecycle(w, 'validate')['workspace']['validation']['findings']
    assert {f['person_id'] for f in findings if f['rule_id'] == rule['id']} == {b}


def test_coverage_split_session_fields_select_sessions_person_fields_select_who_counts(h):
    a, b, _ = h.seed['people']
    consultant(h, a)
    clinic, other = h.seed['services']
    rule = h.policy({'kind': 'minimum_cover', 'minimum': 1},
                    {'conditions': tree(line('service_ids', clinic), line('category_ids', h.seed['trainee_category']))})
    w = h.shift(h.workspace(), people=[b])['workspace']                                            # clinic, no consultant: gap
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[a])['workspace']  # clinic, consultant
    w = h.shift(w, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00', people=[b], service=1)['workspace']  # other clinic
    detail = h.detail(w)
    starts = {s['id']: s['start'][:10] for s in detail['shifts']}
    gaps = {starts[f['shift_id']] for f in h.lifecycle(w, 'validate')['workspace']['validation']['findings'] if f['rule_id'] == rule['id']}
    assert gaps == {'2026-10-04'}


def test_solver_honours_exclusions_in_prefilters_and_limits(h):
    a, b, c = h.seed['people']
    consultant(h, a)
    # Only a and b may work (a mandatory restriction for everyone except them), and everyone except
    # consultants may do at most one duty a week: the consultant must take the other two.
    h.policy({'kind': 'restriction', 'weekdays': list(range(7)), 'start_time': '00:00', 'end_time': '24:00'},
             {'conditions': tree(line('person_ids', a, b, op='none'))})
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week'},
             {'conditions': tree(line('category_ids', h.seed['trainee_category'], op='none'))})
    w = h.shift(h.workspace(), people=[])['workspace']
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[])['workspace']
    w = h.shift(w, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    result = generate(copy.deepcopy(data), 10)
    assert result['status'] == 'candidate_feasible', result
    assert result['validation']['valid']
    who = [r['participant']['person_id'] for r in result['additions']]
    assert len(who) == 3 and who.count(a) >= 2 and who.count(b) <= 1 and c not in who


def dimensions(h):
    """Person a gets, from 6 Oct only, a second rank, a new skill, a programme, a rotation type and a cohort; a is
    tagged Night. Sessions on 6 Oct are in the second clinic, in a room inside a wing, of a second duty type.
    Returns {field: value} for one condition line per dimension."""
    from datetime import date
    a, _, _ = h.seed['people']
    dept, oct6 = h.seed['department_id'], date(2026, 10, 6)
    with transaction(h.app.state.engine, write=True) as db:
        seed_type = db.get(m.AssignmentType, h.seed['type'])
        room = db.get(m.Location, h.seed['locations'][1])
        wing = m.Location(department_id=dept, name='Wing', location_type_id=room.location_type_id, capacity=10, starts_on=date(2020, 1, 1))
        skill = m.Capability(department_id=dept, name='Dated skill', starts_on=date(2020, 1, 1))
        programme = m.Programme(department_id=dept, name='Programme', starts_on=date(2020, 1, 1))
        rtype = m.RotationType(department_id=dept, name='Rotation type', starts_on=date(2020, 1, 1))
        other = m.AssignmentType(department_id=dept, name='Second duty', definition=seed_type.definition, starts_on=date(2020, 1, 1))
        db.add_all([wing, skill, programme, rtype, other])
        db.flush()
        room.parent_id = wing.id
        cohort = m.Cohort(department_id=dept, name='Cohort', programme_id=programme.id, starts_on=oct6, ends_on=date(2027, 1, 1))
        db.add(cohort)
        db.flush()
        db.query(m.Membership).filter_by(person_id=a).delete()
        db.add_all([
            m.Membership(person_id=a, department_id=dept, category_id=h.seed['category'], starts_on=date(2020, 1, 1), ends_on=oct6),
            m.Membership(person_id=a, department_id=dept, category_id=h.seed['trainee_category'], starts_on=oct6),
            m.CapabilityGrant(person_id=a, department_id=dept, capability_id=skill.id, granted_by=h.seed['identities']['credentialer'],
                              evidence='Synthetic', starts_on=oct6),
            m.ProgrammeMembership(person_id=a, department_id=dept, programme_id=programme.id, starts_on=oct6, ends_on=date(2027, 1, 1)),
            m.Rotation(person_id=a, department_id=dept, rotation_type_id=rtype.id, cohort_id=cohort.id, starts_on=oct6, ends_on=date(2027, 1, 1))])
        db.get(m.Person, a).tags = ['Night']
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
        return {'person_ids': a, 'category_ids': h.seed['trainee_category'], 'capability_ids': skill.id, 'programme_ids': programme.id,
                'rotation_type_ids': rtype.id, 'cohort_ids': cohort.id, 'tags': 'night', 'service_ids': h.seed['services'][1],
                'location_ids': wing.id, 'assignment_type_ids': other.id}


# Who a restriction scoped by one line hits, as (person index, day) of the six duties: each person on 4 Oct
# (first clinic, first room, seed duty type) and on 6 Oct (second clinic, room in the wing, second duty type).
DIMENSION_HITS = {'person_ids': {(0, 4), (0, 6)}, 'tags': {(0, 4), (0, 6)},
                  **{f: {(0, 6)} for f in ('category_ids', 'capability_ids', 'programme_ids', 'rotation_type_ids', 'cohort_ids')},
                  **{f: {(0, 6), (1, 6), (2, 6)} for f in ('service_ids', 'location_ids', 'assignment_type_ids')}}
ALL_SIX = {(i, d) for i in range(3) for d in (4, 6)}


def six_duties(h, fixed=True, *, restricted=()):
    people, other = h.seed['people'], None
    w = h.workspace()
    for i, person in enumerate(people):
        w = h.shift(w, people=[person] if fixed else [], seed_restricted=(i, 4) in restricted)['workspace']
        if other is None:
            with transaction(h.app.state.engine) as db:
                other = db.scalar(select(m.AssignmentType.id).where(m.AssignmentType.name == 'Second duty'))
        w = h.shift(w, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00', people=[person] if fixed else [],
                    service=1, location=1, typ=other, seed_restricted=(i, 6) in restricted)['workspace']
        if not fixed:
            break
    return w


@pytest.mark.parametrize('field', sorted(DIMENSION_HITS))
@pytest.mark.parametrize('op', ['any', 'none'])
def test_every_dimension_selects_exactly_its_people_and_sessions_in_the_validator(h, field, op):
    value = dimensions(h)[field]
    rule = h.policy({'kind': 'restriction', 'weekdays': list(range(7)), 'start_time': '00:00', 'end_time': '24:00'},
                    {'conditions': tree(line(field, value, op=op))})
    w = six_duties(h, restricted=DIMENSION_HITS[field] if op == 'any' else ALL_SIX - DIMENSION_HITS[field])
    detail = h.detail(w)
    day = {s['id']: int(s['start'][8:10]) for s in detail['shifts']}
    index = {p: i for i, p in enumerate(h.seed['people'])}
    hits = {(index[f['person_id']], day[f['shift_id']]) for f in h.lifecycle(w, 'validate')['workspace']['validation']['findings']
            if f.get('rule_id') == rule['id']}
    assert hits == (DIMENSION_HITS[field] if op == 'any' else ALL_SIX - DIMENSION_HITS[field])


def test_legacy_flat_scope_and_conditions_both_narrow_the_rule(h):
    values = dimensions(h)
    rule = h.policy({'kind': 'restriction', 'weekdays': list(range(7)), 'start_time': '00:00', 'end_time': '24:00'},
                    {'service_ids': [values['service_ids']], 'conditions': tree(line('programme_ids', values['programme_ids']))})
    w = six_duties(h, restricted={(0, 6)})
    findings = [f for f in h.lifecycle(w, 'validate')['workspace']['validation']['findings'] if f.get('rule_id') == rule['id']]
    assert [f['person_id'] for f in findings] == [h.seed['people'][0]]


@pytest.mark.parametrize('field', sorted(DIMENSION_HITS))
def test_solver_keeps_a_restricted_dimension_off_its_duties(h, field):
    """c may never work; a is restricted wherever the line (and a) hold, so b must take exactly those duties."""
    values = dimensions(h)
    a, b, c = h.seed['people']
    everywhere = {'kind': 'restriction', 'weekdays': list(range(7)), 'start_time': '00:00', 'end_time': '24:00'}
    h.policy(everywhere, {'person_ids': [c]})
    h.policy(everywhere, {'conditions': tree(line(field, values[field]), line('person_ids', a))})
    w = six_duties(h, fixed=False)
    result = generate(copy.deepcopy(h.snapshot(w)), 10)
    assert result['status'] == 'candidate_feasible' and result['validation']['valid'], result
    starts = {s['id']: int(s['start'][8:10]) for s in h.detail(w)['shifts']}
    who = {starts[r['shift_id']]: r['participant']['person_id'] for r in result['additions']}
    for (_, d) in DIMENSION_HITS[field] & {(0, 4), (0, 6)}:
        assert who[d] == b
    assert c not in who.values()


def test_rule_check_previews_how_many_people_match_now(h):
    a, _, _ = h.seed['people']
    consultant(h, a)
    check = lambda scope: h.client('coordinator').post('/api/policies/check', json={'definition': {
        'rule': {'kind': 'min_rest', 'hours': 11}, 'scope': scope}}).json()
    assert check({})['people_now'] == 3
    assert check({'conditions': tree(line('category_ids', h.seed['trainee_category'], op='none'))})['people_now'] == 2
    assert check({'conditions': tree(line('category_ids', h.seed['trainee_category']))})['people_now'] == 1
    # Condition values must be records of this department.
    response = h.client('coordinator').post('/api/policies/check', json={'definition': {
        'rule': {'kind': 'min_rest', 'hours': 11}, 'scope': {'conditions': tree(line('category_ids', '00000000-0000-0000-0000-000000000009'))}}})
    assert response.status_code == 404


def test_named_people_shortcut_never_excludes_a_match():
    """The fast "who can this rule ever match" test (department-sized checks skip other people's rules with it)."""
    from packages.domain.validation import named_people
    assert named_people({}) is None and named_people({'person_ids': ['p']}) == {'p'}
    assert named_people({'conditions': tree(line('person_ids', 'p', 'q'), line('tags', 'Night'))}) == {'p', 'q'}
    assert named_people({'conditions': tree(line('person_ids', 'p'), line('tags', 'Night'), match='any')}) is None
    assert named_people({'conditions': tree(line('person_ids', 'p', op='none'))}) is None
    assert named_people({'person_ids': ['p', 'q'], 'conditions': tree(tree(line('person_ids', 'q')))}) == {'q'}
    assert named_people({'conditions': tree(tree(line('person_ids', 'p')), tree(line('person_ids', 'q')))}) == set()
    # Against the full check: q is excluded only where the conditions can never match q.
    for conditions in (tree(line('person_ids', 'p')), tree(line('person_ids', 'p'), line('tags', 'Night'), match='any'),
                       tree(tree(line('person_ids', 'p')))):
        named = named_people({'conditions': conditions})
        assert matches(conditions, OCT, person='q') <= (named is None or 'q' in named)
