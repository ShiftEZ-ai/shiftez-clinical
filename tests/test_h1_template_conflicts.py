"""Recurring setup and both schedulers leave unsafe assignments reviewable, never hidden."""
import copy
import json
from functools import partial

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.worker.main import process_one
from packages.domain.cp_sat import generate as cp_generate
from packages.domain.generation import generate as greedy_generate, citation_finding_key, coverage_findings
from packages.domain.validation import evaluate
from .test_series_reconciliation import template, edit, amend_template, reconcile
from .test_template_scheduler_audit import use_cp_sat


def approved_leave(h, pid):
    request = h.command('coordinator', 'request.create', {'person_id': pid, 'kind': 'leave',
        'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-05T00:00:00+03:00',
        'private_reason': 'Private synthetic leave note'})['record']
    h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Approve synthetic leave'},
              request['id'], request['revision'])


def assigned_template(h, **changes):
    return template(h, days=(6,), acknowledge_overlaps=True, definition={'anchor': '2026-10-04',
        'days': [{'weekday': 6, 'start_time': '08:00', 'end_time': '12:00'}],
        'service_ids': [h.seed['services'][0]], 'default_person_ids': [h.seed['people'][0]],
        'usual_assignees_fixed': False}, **changes)


def capable_fourth(data, first, capability):
    fourth = m.uid()
    data['people'][fourth] = {**data['people'][first], 'id': fourth, 'display_name': 'Synthetic capable fourth person'}
    for field in ('memberships', 'eligibilities', 'grants'):
        data[field].extend({**r, 'id': m.uid(), 'person_id': fourth}
                          for r in list(data[field]) if r['person_id'] == first)
    data['grants'] = [r for r in data['grants'] if r['capability_id'] != capability or r['person_id'] == fourth]
    return fourth


@pytest.mark.parametrize('method', ['create', 'expand'])
@pytest.mark.parametrize('conflict,code', [('leave', 'UNAVAILABLE'), ('absence', 'UNAVAILABLE'),
                                         ('rotation', 'EXTERNAL_ROTATION'), ('booking', 'TIME_OVERLAP')])
def test_templates_leave_absent_or_busy_assignees_open_and_store_generic_reason(h, method, conflict, code):
    pid = h.seed['people'][0]
    if conflict == 'leave':
        approved_leave(h, pid)
    elif conflict == 'absence':
        h.command('coordinator', 'availability.record', {'person_id': pid, 'kind': 'recorded_leave',
            'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-05T00:00:00+03:00',
            'private_reason': 'Private synthetic leave note'})
    elif conflict == 'rotation':
        programme = h.configure('programmes', {'name': 'Synthetic training programme',
            'starts_on': '2020-01-01', 'ends_on': '2030-01-01'})['record']
        cohort = h.configure('cohorts', {'name': 'Synthetic cohort', 'programme_id': programme['id'],
            'starts_on': '2020-01-01', 'ends_on': '2030-01-01'})['record']
        h.configure('programme_memberships', {'person_id': pid, 'programme_id': programme['id'],
            'starts_on': '2026-10-04', 'ends_on': '2026-10-05'})
        h.configure('rotations', {'person_id': pid, 'cohort_id': cohort['id'],
            'service_id': h.seed['services'][0], 'department_available': False,
            'starts_on': '2026-10-04', 'ends_on': '2026-10-05'})
    series = assigned_template(h)
    if method == 'create':
        if conflict == 'booking':
            h.publish(h.shift(h.workspace(), location=1)['workspace'])
        result = h.command('coordinator', 'workspace.create', {'name': 'Synthetic template period',
            'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'series_ids': [series['id']]})
    else:
        w = h.workspace()
        if conflict == 'booking':
            w = h.shift(w, location=1)['workspace']
        result = h.command('coordinator', 'series.expand', {'workspace_id': w['id'],
            'series_id': series['id']}, revision=w['revision'])
    row = next(r for r in h.detail(result['workspace'])['shifts'] if r['series_id'] == series['id'])
    assert row['participants'] == []
    assert [(r['person_id'], r['code']) for r in row['template_skipped']] == [(pid, code)]
    assert result['template_skipped'] == [{'shift_id': row['id'], **row['template_skipped'][0]}]
    assert 'Private synthetic leave note' not in json.dumps(result)
    reloaded = next(r for r in h.detail(result['workspace'])['shifts'] if r['id'] == row['id'])
    assert reloaded['template_skipped'] == row['template_skipped']


def test_multiple_templates_do_not_book_the_same_person_twice(h):
    first = assigned_template(h)
    second = assigned_template(h, location_id=h.seed['locations'][1])
    result = h.command('coordinator', 'workspace.create', {'name': 'Synthetic overlapping templates',
        'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'series_ids': [first['id'], second['id']]})
    rows = {r['series_id']: r for r in h.detail(result['workspace'])['shifts']}
    assert len(rows[first['id']]['participants']) == 1
    assert rows[second['id']]['participants'] == []
    assert rows[second['id']]['template_skipped'][0]['code'] == 'TIME_OVERLAP'


@pytest.mark.parametrize('engine', [greedy_generate, cp_generate])
@pytest.mark.parametrize('template_assignment', [False, True])
def test_explicit_rebalance_replaces_absent_assignments_with_named_reasons(h, engine, template_assignment):
    w = h.shift(h.workspace())['workspace']
    approved_leave(h, h.seed['people'][0])
    data = h.snapshot(w)
    data['generation_options'] = {'mode': 'rebalance'}
    if template_assignment:
        data['shifts'][0].update(series_id='legacy-template', series_modified=False)
        data['shifts'][0]['participants'][0]['fixed'] = True
    original = copy.deepcopy(data)
    result = engine(data, time_budget_seconds=5)
    assert result['status'] == 'candidate_feasible', result
    assert result['summary']['replaced_count'] == 1
    replacement = result['summary']['replacements'][0]
    assert replacement['person_id'] == h.seed['people'][0] and replacement['person_name']
    assert replacement['reason'] and result['summary']['replacement_message'] == 'Replaced 1'
    assert result['additions'] and all(a['participant']['person_id'] != h.seed['people'][0] for a in result['additions'])
    assert data == original
    assert 'Private synthetic leave note' not in json.dumps(result)


@pytest.mark.parametrize('engine', [greedy_generate, cp_generate])
def test_schedulers_replace_double_bookings_automatically(h, engine):
    w = h.shift(h.workspace())['workspace']
    w = h.shift(w, service=1, location=1)['workspace']
    data = h.snapshot(w)
    data['generation_options'] = {'mode': 'rebalance'}
    result = engine(data, time_budget_seconds=5)
    assert result['status'] == 'candidate_feasible', result
    assert result['summary']['replaced_count'] == 1
    assert len(result['additions']) == 1
    assert result['removals'][0]['reason']
    assert not any(f['code'] == 'TIME_OVERLAP' for f in result['validation']['findings'])


@pytest.mark.parametrize('engine', [greedy_generate, cp_generate])
@pytest.mark.parametrize('protection', ['locked', 'manual', 'fixed'])
@pytest.mark.parametrize('capacity_limited', [False, True])
def test_kept_conflicts_do_not_block_scheduling_other_sessions(h, engine, protection, capacity_limited):
    typ = h.seed['type']
    if capacity_limited:
        typ = h.configure('assignment_types', {'name': 'Synthetic strictly one-person duty', 'starts_on': '2020-01-01',
            'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']['id']
    w = h.shift(h.workspace(), typ=typ)['workspace']
    w = h.shift(w, typ=typ, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    approved_leave(h, h.seed['people'][0])
    data = h.snapshot(w)
    first, other = data['shifts']
    if protection == 'locked':
        first['locked'] = True
    elif protection == 'manual':
        data['generation_options'] = {'mode': 'fill_gaps'}
    else:
        first['participants'][0]['fixed'] = True
    result = engine(data, time_budget_seconds=5)
    assert result['status'] == 'candidate_with_gaps', result
    assert result['summary']['replaced_count'] == 0 and not result['removals']
    assert any(a['shift_id'] == other['id'] for a in result['additions'])
    assert any(f['code'] == 'UNAVAILABLE' and f['person_id'] == h.seed['people'][0] for f in result['issues'])
    assert any(f['code'] == 'UNAVAILABLE' for f in result['validation']['findings'])
    assert not any(f['code'] == 'MAXIMUM_PARTICIPANTS' for f in result['validation']['findings'])


def test_explicit_rebalance_of_legacy_template_applies_and_undoes_as_one_edit(h):
    use_cp_sat(h)
    series = assigned_template(h)
    w = h.command('coordinator', 'workspace.create', {'name': 'Synthetic legacy template',
        'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'series_ids': [series['id']]})['workspace']
    original = h.detail(w)['shifts'][0]
    with transaction(h.app.state.engine, write=True) as db:
        assignment = db.scalar(select(m.Assignment).where(m.Assignment.shift_id == original['id']))
        assignment.fixed = True
    original = h.detail(w)['shifts'][0]
    approved_leave(h, h.seed['people'][0])
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation',
        'generation_options': {'mode': 'rebalance'}}, revision=w['revision'])['job']
    process_one(h.app.state.engine, scheduler=partial(cp_generate, time_budget_seconds=5))
    result = h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']
    assert result['status'] == 'candidate_feasible' and result['summary']['replaced_count'] == 1
    applied = h.command('coordinator', 'generation.apply', {'workspace_id': w['id'], 'job_id': job['id'],
        'reason': 'Review synthetic automatic template replacement'}, revision=w['revision'])
    assert applied['validation']['valid']
    assert h.detail(w)['shifts'][0]['participants'][0]['person_id'] != h.seed['people'][0]
    h.command('coordinator', 'workspace.undo', {'reason': 'Restore the original synthetic template'},
              w['id'], applied['workspace']['revision'])
    restored = h.detail(w)['shifts'][0]
    assert {key: value for key, value in restored.items() if key != 'double_booking_conflicts'} == original
    assert restored['double_booking_conflicts'][0]['code'] == 'UNAVAILABLE'


@pytest.mark.parametrize('engine', [greedy_generate, cp_generate])
@pytest.mark.parametrize('keep_manual', [False, True])
def test_overfilled_assignments_are_replaced_or_reported_without_extra_breaches(h, engine, keep_manual):
    typ = h.configure('assignment_types', {'name': 'Synthetic one-person clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']
    w = h.shift(h.workspace(), typ=typ['id'], people=h.seed['people'][:2])['workspace']
    w = h.shift(w, typ=typ['id'], start='2026-10-05T08:00:00+03:00',
        end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    data['generation_options'] = {'mode': 'fill_gaps' if keep_manual else 'rebalance'}
    result = engine(data, time_budget_seconds=5)
    assert result['status'] == ('candidate_with_gaps' if keep_manual else 'candidate_feasible'), result
    assert result['summary']['replaced_count'] == (0 if keep_manual else 1)
    assert any(a['shift_id'] == data['shifts'][1]['id'] for a in result['additions'])
    maximums = [f for f in result['validation']['findings'] if f['code'] == 'MAXIMUM_PARTICIPANTS']
    assert len(maximums) == int(keep_manual)
    if keep_manual:
        assert all(f['shift_id'] == data['shifts'][0]['id'] for f in maximums)


@pytest.mark.parametrize('engine', [greedy_generate, cp_generate])
def test_preserved_rest_conflict_does_not_allow_any_new_rest_breach(h, engine):
    h.policy({'kind': 'min_rest', 'hours': 12})
    w = h.shift(h.workspace())['workspace']
    w = h.shift(w, start='2026-10-04T13:00:00+03:00', end='2026-10-04T17:00:00+03:00')['workspace']
    w = h.shift(w, start='2026-10-04T18:00:00+03:00', end='2026-10-04T22:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    data['generation_options'] = {'mode': 'fill_gaps'}
    result = engine(data, time_budget_seconds=5)
    assert result['status'] == 'candidate_with_gaps', result
    assert not result['removals']
    assert result['additions'] and all(a['participant']['person_id'] != h.seed['people'][0] for a in result['additions'])
    rest = [f for f in result['validation']['findings'] if f['code'] == 'MIN_REST']
    assert len(rest) == 1 and rest[0]['person_id'] == h.seed['people'][0]


@pytest.mark.parametrize('engine', [greedy_generate, cp_generate])
@pytest.mark.parametrize('keep_manual', [False, True])
def test_filling_a_capability_gap_cannot_worsen_kept_overcapacity(h, engine, keep_manual):
    capability = h.seed['supervisor_capability']
    typ = h.configure('assignment_types', {'name': 'Synthetic capped skilled duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 2,
            'capabilities': [{'capability_id': capability, 'minimum': 1}]}]}})['record']
    w = h.shift(h.workspace(), typ=typ['id'], people=h.seed['people'])['workspace']
    data = h.snapshot(w)
    capable_fourth(data, h.seed['people'][0], capability)
    if keep_manual:
        data['generation_options'] = {'mode': 'fill_gaps'}
    else:
        for p in data['shifts'][0]['participants']:
            p['fixed'] = True
    result = engine(data, time_budget_seconds=5)
    assert result['status'] == 'candidate_with_gaps', result
    assert not result['additions'] and not result['removals']
    maximum = next(f for f in result['validation']['findings'] if f['code'] == 'MAXIMUM_PARTICIPANTS')
    assert maximum['detail'] == {'maximum': 2, 'scheduled': 3}


def test_supervisor_capacity_findings_include_actual_count_for_no_worse_checks(h):
    supervisor, first, second = h.seed['people']
    for pid in (first, second):
        h.command('coordinator', 'membership.create', {'person_id': pid,
            'category_id': h.seed['trainee_category'], 'starts_on': '2020-01-01'})
    typ = h.configure('assignment_types', {'name': 'Synthetic capped supervision', 'starts_on': '2020-01-01',
        'definition': {'requirements': [], 'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
            'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1, 'require_pairing': True}}})['record']
    a, b = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    w = h.shift(h.workspace(), typ=typ['id'], people=[
        {'person_id': supervisor, 'start': a, 'end': b, 'participation': 'supervisor'},
        *[{'person_id': pid, 'start': a, 'end': b, 'participation': 'trainee', 'supervisor_person_id': supervisor}
          for pid in (first, second)]])['workspace']
    capacity = next(f for f in w['validation']['findings'] if f['code'] == 'SUPERVISOR_CAPACITY')
    assert capacity['detail'] == {'maximum': 1, 'scheduled': 2}


@pytest.mark.parametrize('engine', [greedy_generate, cp_generate])
@pytest.mark.parametrize('gap_kind', ['capability', 'headcount'])
def test_partial_improvement_of_kept_gaps_does_not_discard_other_scheduling(h, engine, gap_kind):
    capability = h.seed['supervisor_capability']
    requirement = {'minimum': 4 if gap_kind == 'headcount' else 1, 'maximum': 4}
    if gap_kind == 'capability':
        requirement['capabilities'] = [{'capability_id': capability, 'minimum': 2}]
    typ = h.configure('assignment_types', {'name': 'Synthetic partially fillable duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': [requirement]}})['record']
    w = h.shift(h.workspace(), typ=typ['id'], people=h.seed['people'])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    approved_leave(h, h.seed['people'][0])
    data = h.snapshot(w)
    fourth = capable_fourth(data, h.seed['people'][0], capability)
    data['generation_options'] = {'mode': 'fill_gaps'}
    result = engine(data, time_budget_seconds=5)
    assert result['status'] == 'candidate_with_gaps', result
    assert not result['removals']
    assert any(a['shift_id'] == data['shifts'][0]['id'] and a['participant']['person_id'] == fourth
               for a in result['additions'])
    assert any(a['shift_id'] == data['shifts'][1]['id'] for a in result['additions'])
    gap = next(f for f in coverage_findings(result['validation'])
               if f['shift_id'] == data['shifts'][0]['id'] and f['code'] == (
                   'CAPABILITY_GAP' if gap_kind == 'capability' else 'COVERAGE_GAP'))
    assert gap['detail']['available'] == (1 if gap_kind == 'capability' else 3)
    assert not result['validation']['valid']
    if gap_kind == 'headcount':
        leave = next(f for f in result['validation']['findings'] if f['code'] == 'UNAVAILABLE'
                     and f['person_id'] == h.seed['people'][0] and f['shift_id'] == data['shifts'][0]['id'])
        assert gap in leave['detail']['derived_findings']
    assert not any(f['code'] == 'MAXIMUM_PARTICIPANTS' for f in result['validation']['findings'])


@pytest.mark.parametrize('engine', [greedy_generate, cp_generate])
@pytest.mark.parametrize('manual_fixed,mode', [(False, 'fill_gaps'), (True, 'fill_gaps'), (True, 'rebalance')])
def test_preserved_manual_series_assignments_stay_protected_after_reconciliation(h, engine, manual_fixed, mode):
    series = assigned_template(h)
    w = h.command('coordinator', 'workspace.create', {'name': 'Synthetic reconciled manual assignment',
        'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'series_ids': [series['id']]})['workspace']
    row = h.detail(w)['shifts'][0]
    pid = h.seed['people'][1]
    w = edit(h, w, row, participants=[{'person_id': pid, 'start': row['start'], 'end': row['end'], 'fixed': manual_fixed}])
    series = amend_template(h, series)
    w = reconcile(h, w, series, replace_modified=True, from_on='2026-10-04')['workspace']
    retained = h.detail(w)['shifts'][0]
    assert retained['series_modified']
    assert retained['participants'][0]['fixed'] == manual_fixed
    approved_leave(h, pid)
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    data['generation_options'] = {'mode': mode}
    result = engine(data, time_budget_seconds=5)
    assert result['status'] == 'candidate_with_gaps', result
    assert not any(r['person_id'] == pid for r in result['removals'])
    assert any(f['code'] == 'UNAVAILABLE' and f['person_id'] == pid for f in result['issues'])
    assert any(a['shift_id'] == data['shifts'][1]['id'] for a in result['additions'])


def test_template_attachment_repair_never_trades_a_pairing_error_for_a_safety_error(h):
    from .test_attachments import setup, people
    trainee, first_supervisor, attached_supervisor, typ = setup(h)
    w = h.shift(h.workspace(), typ=typ, people=people(trainee, first_supervisor, attached_supervisor,
                                                   paired=first_supervisor))['workspace']
    data = h.snapshot(w)
    other = m.uid()
    data['people'][other] = {**data['people'][trainee], 'id': other, 'display_name': 'Synthetic other trainee'}
    for field in ('memberships', 'eligibilities', 'grants'):
        data[field].extend({**r, 'id': m.uid(), 'person_id': other}
                          for r in list(data[field]) if r['person_id'] == trainee)
    shift = data['shifts'][0]
    shift.update(series_id='synthetic-legacy-template', series_modified=False)
    shift['type_snapshot']['definition']['requirements'] = [{'minimum': 4, 'maximum': 4,
        'from_minute': 0, 'to_minute': None, 'target': None, 'capabilities': [], 'distinct_capability_seats': False}]
    for p in shift['participants']:
        p['fixed'] = True
    prior = next(p for p in shift['participants'] if p['person_id'] == trainee)
    shift['participants'].append({**prior, 'id': m.uid(), 'person_id': other, 'supervisor_person_id': attached_supervisor})
    result = greedy_generate(data, time_budget_seconds=5)
    assert result['status'] == 'candidate_with_gaps', result
    assert not result['removals'] and not result['additions']
    codes = {f['code'] for f in result['validation']['findings'] if f['severity'] == 'error'}
    assert codes == {'ATTACHMENT_PAIRING'}
    assert {f['code'] for f in result['issues']} == {'ATTACHMENT_PAIRING'}


def test_legacy_keep_manual_checkbox_still_preserves_edits_without_blocking_other_duties(h):
    w = h.shift(h.workspace())['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    approved_leave(h, h.seed['people'][0])
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation',
        'repair_unavailable': False, 'repair_conflicts': False}, revision=w['revision'])['job']
    assert job['generation_engine'] == 'legacy'
    with transaction(h.app.state.engine) as db:
        snapshot = db.get(m.Job, job['id']).input_snapshot
        assert snapshot['generation_options']['mode'] == 'fill_gaps'
        assert snapshot['generation_options_overridden']
    process_one(h.app.state.engine)
    result = h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']
    assert result['status'] == 'candidate_with_gaps'
    assert result['additions'] and not result['removals']
    assert any(f['code'] == 'UNAVAILABLE' for f in result['issues'])


@pytest.mark.parametrize('engine', [greedy_generate, cp_generate])
@pytest.mark.parametrize('rule_kind', ['min_rest', 'travel_buffer'])
@pytest.mark.parametrize('next_hour', [12, 13])
def test_conflict_anchored_on_locked_later_duty_replaces_only_its_editable_neighbor(h, engine, rule_kind, next_hour):
    rule = {'kind': 'min_rest', 'hours': 12} if rule_kind == 'min_rest' else {
        'kind': 'travel_buffer', 'minutes': 120, 'from_location_id': h.seed['locations'][0],
        'to_location_id': h.seed['locations'][1]}
    h.policy(rule)
    w = h.shift(h.workspace(start='2026-10-03'), start='2026-10-03T08:00:00+03:00', end='2026-10-03T12:00:00+03:00')['workspace']
    w = h.shift(w)['workspace']
    w = h.shift(w, start=f'2026-10-04T{next_hour:02}:00:00+03:00',
                end=f'2026-10-04T{next_hour+4:02}:00:00+03:00', location=1)['workspace']
    data = h.snapshot(w)
    data['generation_options'] = {'mode': 'rebalance', 'scoring': {'weights': {'fairness': 0, 'stability': 100}}}
    older, editable, locked = sorted(data['shifts'], key=lambda s: s['start'])
    locked['locked'] = True
    result = engine(data, time_budget_seconds=5)
    assert result['status'] == 'candidate_feasible', result
    assert [r['shift_id'] for r in result['removals']] == [editable['id']]
    assert [a['shift_id'] for a in result['additions']] == [editable['id']]
    assert result['additions'][0]['participant']['person_id'] != h.seed['people'][0]
    assert not any(r['shift_id'] in {older['id'], locked['id']} for r in result['removals'])
    assert result['validation']['valid']


@pytest.mark.parametrize('engine', [greedy_generate, cp_generate])
def test_inactive_editable_person_is_replaced_in_a_capped_duty(h, engine):
    typ = h.configure('assignment_types', {'name': 'Synthetic capped active duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']['id']
    data = h.snapshot(h.shift(h.workspace(), typ=typ)['workspace'])
    data['generation_options'] = {'mode': 'rebalance'}
    data['people'][h.seed['people'][0]]['active'] = False
    result = engine(data, time_budget_seconds=5)
    assert result['status'] == 'candidate_feasible', result
    assert result['summary']['replaced_count'] == 1
    assert result['removals'][0]['person_id'] == h.seed['people'][0]
    assert len(result['additions']) == 1
    assert result['additions'][0]['participant']['person_id'] != h.seed['people'][0]
    assert result['validation']['valid']


@pytest.mark.parametrize('engine', [greedy_generate, cp_generate])
def test_editable_partial_assignment_is_replaced_with_full_capped_duty(h, engine):
    typ = h.configure('assignment_types', {'name': 'Synthetic complete capped duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']['id']
    w = h.shift(h.workspace(), typ=typ)['workspace']
    data = h.snapshot(w)
    data['generation_options'] = {'mode': 'rebalance'}
    # Legacy/imported partial timing; current writes reject this input before it reaches scheduling.
    data['shifts'][0]['participants'][0]['start'] = '2026-10-04T09:00:00+03:00'
    result = engine(data, time_budget_seconds=5)
    assert result['status'] == 'candidate_feasible', result
    assert result['summary']['replaced_count'] == 1
    assert len(result['additions']) == 1
    participant = result['additions'][0]['participant']
    assert (participant['start'], participant['end']) == (data['shifts'][0]['start'], data['shifts'][0]['end'])
    assert result['validation']['valid']


@pytest.mark.parametrize('engine', [greedy_generate, cp_generate])
@pytest.mark.parametrize('options', [None, {'search_seconds': 300}])
def test_inherited_assignment_mode_keeps_manual_conflicts_visible(h, options, engine):
    w = h.shift(h.workspace())['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    approved_leave(h, h.seed['people'][0])
    data = h.snapshot(w)
    if options is not None:
        data['generation_options'] = options
    before = copy.deepcopy(data)
    original_errors = {citation_finding_key(f) for f in evaluate(data)['findings'] if f['severity'] == 'error'}
    result = engine(data, time_budget_seconds=5)
    assert data == before
    if engine is cp_generate:
        assert result['effective_options']['mode'] == 'fill_gaps'
    assert result['status'] == 'candidate_with_gaps' and not result['validation']['valid']
    assert result['removals'] == []
    assert data['shifts'][1]['id'] in {a['shift_id'] for a in result['additions']}
    assert {citation_finding_key(f) for f in result['validation']['findings'] if f['severity'] == 'error'} <= original_errors
    assert any(f['code'] == 'UNAVAILABLE' and f['person_id'] == h.seed['people'][0] for f in result['issues'])


@pytest.mark.parametrize('engine', [greedy_generate, cp_generate])
@pytest.mark.parametrize('conflict', ['qualification', 'timing'])
def test_locked_assignment_error_keeps_the_persons_other_healthy_duty(h, engine, conflict):
    w = h.shift(h.workspace())['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', service=1)['workspace']
    w = h.shift(w, start='2026-10-06T08:00:00+03:00', end='2026-10-06T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    kept, healthy, open_session = sorted(data['shifts'], key=lambda s: s['start'])
    # This clinic requests exactly one person; extra staffing is not part of this repair.
    for session in data['shifts']:
        for requirement in session['type_snapshot']['definition']['requirements']:
            requirement['maximum'] = 1
    kept['locked'] = True
    if conflict == 'qualification':
        data['eligibilities'] = [r for r in data['eligibilities']
            if not (r['person_id'] == h.seed['people'][0] and r['service_id'] == h.seed['services'][0])]
    else:
        kept['participants'][0]['start'] = '2026-10-04T09:00:00+03:00'
    result = engine(data, time_budget_seconds=5)
    assert result['status'] == 'candidate_with_gaps', result
    assert not result['removals']
    assert [a['shift_id'] for a in result['additions']] == [open_session['id']]
    assert any(f['code'] == ('NOT_ELIGIBLE' if conflict == 'qualification' else 'FULL_ASSIGNMENT_REQUIRED')
               and f['shift_id'] == kept['id'] for f in result['issues'])
    assert not any(f['severity'] == 'error' and f['shift_id'] == healthy['id'] for f in result['validation']['findings'])
# Include this file in hosted supervision and team-lead qualification.
