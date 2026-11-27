"""Recurring-series warnings share the validator's protected reservation semantics."""
import copy
from datetime import date, timedelta
import time

import pytest

from packages.domain.overlap import evaluate_overlaps, overlap_envelope, per_session_overlaps
from packages.contracts.schemas import FINDING_MESSAGES, MAX_DUTY_DURATION_MINUTES
from packages.domain.rota_summary import rota_summary
from packages.domain.time import aware, local_midnight
from packages.domain.validation import evaluate, finding_observations


@pytest.mark.parametrize('zone', ['America/New_York', 'Asia/Riyadh', 'Pacific/Kiritimati', 'Etc/GMT+12'])
def test_overlap_envelope_includes_both_overnight_margins(zone):
    from zoneinfo import ZoneInfo
    start, end = (local_midnight(day, zone).astimezone(ZoneInfo(zone))
                  for day in (date(2026, 11, 1), date(2026, 11, 2)))
    span = timedelta(minutes=MAX_DUTY_DURATION_MINUTES)
    assert overlap_envelope(start, end) == (aware(start) - span, aware(end) + span)


def shift(sid, start='08:00', end='12:00', *, day='2026-10-05', person='person', series=None, **extra):
    a, b = f'{day}T{start}:00+03:00', f'{day}T{end}:00+03:00'
    return {'id': sid, 'workspace_id': 'draft', 'start': a, 'end': b, 'assignment_type_id': 'type',
            'type_snapshot': {'name': 'Clinic session'}, 'service_ids': ['clinic'],
            'participants': [{'person_id': person, 'start': a, 'end': b}],
            'series_id': series, 'occurrence_on': day if series else None,
            'series_snapshot': {'name': series, 'definition': {'timezone': 'Asia/Riyadh'}} if series else None, **extra}


def data(*shifts, **extra):
    return {'organisation': {'timezone': 'Asia/Riyadh'}, 'shifts': list(shifts), 'availability': [],
            'services': {'clinic': {'name': 'Synthetic clinic'}},
            'people': {'person': {'display_name': 'Synthetic person'}}, **extra}


def test_series_oneoff_and_second_series_are_named_and_mirrored_without_mutation():
    source = data(shift('a', series='Pediatrics'), shift('b', '09:00', '10:00', series='Autism'),
                  shift('c', '11:00', '13:00'))
    before = copy.deepcopy(source)
    conflicts = evaluate_overlaps(source)
    assert [(r['shift_id'], r['other_shift_id']) for r in conflicts] == [('a', 'b'), ('a', 'c')]
    first = conflicts[0]
    assert {k: first[k] for k in ('code', 'date', 'start', 'end', 'person_name', 'session_name',
                                  'other_session_name', 'clinic_names', 'other_clinic_names')} == {
        'code': 'TIME_OVERLAP', 'date': '2026-10-05', 'start': '2026-10-05T06:00:00+00:00',
        'end': '2026-10-05T07:00:00+00:00', 'person_name': 'Synthetic person',
        'session_name': 'Pediatrics', 'other_session_name': 'Autism',
        'clinic_names': ['Synthetic clinic'], 'other_clinic_names': ['Synthetic clinic']}
    flags = per_session_overlaps(source, conflicts)
    assert {sid: [r['other_shift_id'] for r in rows] for sid, rows in flags.items()} == {
        'a': ['b', 'c'], 'b': ['a'], 'c': ['a']}
    assert flags['b'][0]['series_id'] == 'Autism'
    assert flags['b'][0]['other_series_id'] == 'Pediatrics'
    assert flags['b'][0]['other_workspace_id'] == 'draft'
    assert source == before


def test_half_open_boundaries_people_and_inactive_occurrences_do_not_conflict():
    source = data(shift('a'), shift('touch', '12:00', '16:00'), shift('different', person='colleague'),
                  shift('cancelled', cancelled=True), shift('closed', closed=True))
    assert evaluate_overlaps(source) == []


def test_reserved_presence_gap_and_cross_midnight_still_conflict():
    original = shift('a', '00:00', '04:00', series='Night clinic')
    original['participants'][0]['segments'] = [
        {'start': '2026-10-05T00:00:00+03:00', 'end': '2026-10-05T01:00:00+03:00'},
        {'start': '2026-10-05T03:00:00+03:00', 'end': '2026-10-05T04:00:00+03:00'}]
    source = data(original, shift('gap', '01:00', '02:00'))
    conflict, = evaluate_overlaps(source)
    assert conflict['date'] == '2026-10-05'  # UTC is still the preceding day.
    assert conflict['start'] == '2026-10-04T22:00:00+00:00'


def test_leave_time_away_and_unassigned_fixed_people_use_same_sweep_without_private_reasons():
    original = shift('a', series='Fixed clinic', participants=[])
    original['series_snapshot']['definition'].update(default_person_ids=['person'], usual_assignees_fixed=True)
    source = data(original, availability=[{'id': 'leave', 'person_id': 'person',
        'start': '2026-10-05T09:00:00+03:00', 'end': '2026-10-05T10:00:00+03:00',
        'private_reason': 'Never disclose this reason'}], rotations=[{'id': 'rotation', 'person_id': 'person',
        'starts_on': '2026-10-05', 'ends_on': '2026-10-06', 'department_available': False}])
    assert evaluate_overlaps(source) == []
    conflicts = evaluate_overlaps(source, include_fixed=True)
    assert {r['code'] for r in conflicts} == {'UNAVAILABLE', 'EXTERNAL_ROTATION'}
    assert {r['availability_id'] for r in conflicts} == {'leave', 'rotation'}
    assert all(r['other_shift_id'] is None for r in conflicts)
    assert 'Never disclose' not in str(conflicts)
    assert all(r['person_name'] == 'Synthetic person' for r in conflicts)
    assert per_session_overlaps(source, conflicts) == {'a': conflicts}


def test_focus_and_partial_checks_retain_other_session_context():
    source = data(shift('context', series='Other clinic'), shift('focus', series='Focused clinic'))
    conflict, = evaluate_overlaps(source, focus_ids={'focus'})
    assert (conflict['shift_id'], conflict['other_shift_id']) == ('focus', 'context')
    assert evaluate_overlaps(source, focus_ids=set()) == []
    assert evaluate_overlaps({**source, 'person_rule_subjects': []}) == []
    assert evaluate_overlaps({**source, '_partial': {'people': [], 'shifts': ['focus']}}) == []


def test_rota_check_keeps_overlap_protected_and_names_both_sessions(h):
    workspace = h.shift(h.workspace())['workspace']
    workspace = h.shift(workspace)['workspace']
    source = h.snapshot(workspace)
    first, second = source['shifts']
    first['series_snapshot'] = {'name': 'Pediatrics', 'definition': {}}
    second['series_snapshot'] = {'name': 'Autism', 'definition': {}}
    result = evaluate(source)
    conflicts = [row for row in result['findings'] if row['code'] == 'TIME_OVERLAP']
    assert conflicts and not result['valid']
    assert all(row['severity'] == 'error' and 'Pediatrics' in row['message'] and 'Autism' in row['message']
               and row['detail']['overlap']['other_shift_id'] == second['id'] for row in conflicts)
    causes = [cause for cause in rota_summary(result)['fix_list']['causes']
              if any(row['code'] == 'TIME_OVERLAP' for row in cause['evidence'])]
    assert causes and all(cause['safety'] and cause['level'] == 'must' for cause in causes)
    # The fix list uses the finding contract's wording for an overlap.
    assert all(cause['sentence'] == FINDING_MESSAGES['TIME_OVERLAP'] for cause in causes)
    assert all('mix of duties' not in cause['sentence'] for cause in causes)


@pytest.mark.parametrize('different_ends', [False, True])
def test_multiple_pairs_keep_one_selectable_issue_and_independent_generation_evidence(h, different_ends):
    from apps.api.services.issues import issue_key
    from packages.domain.generation import citation_finding_key

    workspace = h.shift(h.workspace())['workspace']
    source = h.snapshot(workspace)
    original = source['shifts'][0]
    original['series_snapshot'] = {'name': 'First clinic', 'definition': {}}
    second, third = copy.deepcopy(original), copy.deepcopy(original)
    for index, row in enumerate((second, third), 2):
        row['id'] = f'other-{index}'
        row['series_snapshot']['name'] = f'Clinic {index}'
    if different_ends:
        third['end'] = '2026-10-04T11:00:00+03:00'
        for person in third['participants']:
            person['end'] = third['end']
    source['shifts'].extend((second, third))
    source['focus_ids'].extend((second['id'], third['id']))
    checked = evaluate(source)
    visible = [row for row in checked['findings'] if row['code'] == 'TIME_OVERLAP']
    assert len({issue_key(row) for row in visible}) == len(visible) == 2
    assert all(name in visible[0]['message'] for name in ('First clinic', 'Clinic 2', 'Clinic 3'))
    pairs = [row for row in finding_observations(checked) if row['code'] == 'TIME_OVERLAP']
    assert len(pairs) == 3 and all(row['severity'] == 'error' for row in pairs)
    assert len({citation_finding_key(row) for row in pairs}) == 3
    # Resolving one occurrence preserves the identity of every unchanged pair.
    keys = {citation_finding_key(row) for row in pairs}
    second['cancelled'] = True
    remaining = [row for row in finding_observations(evaluate(source)) if row['code'] == 'TIME_OVERLAP']
    assert len(remaining) == 1 and citation_finding_key(remaining[0]) in keys


@pytest.fixture(scope='module')
def department_overlaps():
    rows = [shift(str(i), day=str(date(2026, 10, 1) + timedelta(days=i // 20)),
                  person=str(i % 150), series=f'series-{i % 50}') for i in range(500)]
    rows[-1] = {**copy.deepcopy(rows[0]), 'id': '499', 'series_id': 'other-series'}
    source = data(*rows, people={str(i): {'display_name': f'Person {i}'} for i in range(150)},
        availability=[{'id': str(i), 'person_id': str(i), 'start': '2026-09-01T00:00:00+03:00',
                       'end': '2026-09-02T00:00:00+03:00'} for i in range(150)])
    started = time.process_time()
    conflicts = evaluate_overlaps(source)
    return source, conflicts, time.process_time() - started


def test_department_scale_reports_complete_pair_set(department_overlaps):
    source, conflicts, _ = department_overlaps
    assert len(source['shifts']) == 500 and len(source['people']) == 150
    assert len({s['series_id'] for s in source['shifts']}) >= 50
    assert [(r['shift_id'], r['other_shift_id']) for r in conflicts] == [('0', '499')]


@pytest.mark.cpu_budget
def test_department_scale_overlap_cpu_budget(department_overlaps):
    from tests.test_scheduler_performance import _budget
    assert department_overlaps[2] < _budget(0.5), department_overlaps[2]


@pytest.mark.parametrize('focus', [[], ['a'], ['b'], ['c'], ['a', 'b'], ['a', 'b', 'c']])
def test_session_flag_projection_builds_only_requested_mirrors_without_changing_rows(monkeypatch, focus):
    from packages.domain import overlap

    source = data(shift('a', series='Series A'), shift('b', '09:00', '10:00', series='Series B'),
                  shift('c', '11:00', '13:00'))
    before = copy.deepcopy(source)
    conflicts = evaluate_overlaps(source)
    expected = per_session_overlaps(source, conflicts)
    built, original = [], overlap._conflict

    def counted(data, pid, session, *args, **kwargs):
        built.append(session['id'])
        return original(data, pid, session, *args, **kwargs)

    monkeypatch.setattr(overlap, '_conflict', counted)
    assert per_session_overlaps(source, conflicts, focus_ids=focus) == {
        sid: rows for sid, rows in expected.items() if sid in focus}
    assert built == [row['other_shift_id'] for row in conflicts if row['other_shift_id'] in focus]
    assert source == before
