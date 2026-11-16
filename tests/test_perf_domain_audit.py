"""Fairness reuses the evaluation's dated-person index without changing its reports."""
import copy
from datetime import date
import hashlib
import json

import pytest

from packages.domain import fairness
from packages.domain.validation import index_person_rows
from tests.test_engine_fairness_review import DEPT, duty, policy, uid, world


class CountedRows(list):
    def __init__(self, rows):
        super().__init__(rows)
        self.visited = 0

    def __iter__(self):
        for row in super().__iter__():
            self.visited += 1
            yield row


def department():
    people = [uid(1000 + n) for n in range(150)]
    data = world([duty(n, n % 31 + 1, [people[n % 150]]) for n in range(500)])
    # Retain the supported historical-publication audience while exercising its complete reports.
    data['people'] = {pid: {'display_name': pid, 'tags': []} for pid in people}
    data['memberships'], data['rotations'] = [], []
    for n, pid in enumerate(people):
        for year in range(2020, 2026):
            data['memberships'].append({'person_id': pid, 'department_id': DEPT,
                                       'starts_on': f'{year}-01-01', 'ends_on': f'{year + 1}-01-01'})
            data['rotations'].append({'person_id': pid, 'starts_on': f'{year}-01-01',
                                      'ends_on': f'{year}-12-31', 'department_available': False})
            data['availability'].append({'person_id': pid, 'kind': 'approved_leave',
                                         'start': f'{year}-06-01T00:00:00Z', 'end': f'{year}-06-03T00:00:00Z'})
        data['memberships'] += [
            {'person_id': pid, 'department_id': DEPT, 'starts_on': '2026-01-01', 'ends_on': '2026-10-16'},
            {'person_id': pid, 'department_id': DEPT, 'starts_on': '2026-10-16', 'ends_on': None},
            {'person_id': pid, 'department_id': 'other', 'starts_on': '2026-01-01'}]
        data['rotations'] += [
            {'person_id': pid, 'starts_on': '2026-09-20', 'ends_on': '2026-10-21', 'department_available': n % 3 != 0},
            {'person_id': pid, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'department_available': True}]
        data['availability'] += [
            {'person_id': pid, 'kind': 'recorded_leave', 'start': '2026-10-04T12:00:00Z', 'end': '2026-10-05T12:00:00Z'},
            {'person_id': pid, 'kind': 'unavailable', 'start': '2026-10-08T00:00:00Z', 'end': '2026-10-09T00:00:00Z'}]
    goal = policy({'metric': 'duties', 'period': 'rota'})
    data['policies'] = [{**goal, 'id': uid(2000 + n), 'status': 'retired'} for n in range(1000)]
    return data


def dated_results(data):
    first, last = date(2026, 10, 1), date(2026, 11, 1)
    return {pid: (fairness.member_days(data, pid, first, last), fairness.block_of(data, pid, first))
            for pid in [*data['people'], 'missing-person']}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def test_indexed_fairness_days_and_blocks_keep_complete_reports():
    data = department()
    original = copy.deepcopy(data)
    indexed = {**data, '_person_rows': index_person_rows(data)}
    plain = dated_results(data)
    assert dated_results(indexed) == plain
    pid = next(iter(data['people']))
    assert plain[pid][1] == (date(2026, 9, 20), date(2026, 10, 21))
    assert fairness.block_of(indexed, pid, date(2026, 10, 21)) == (date(2026, 10, 1), date(2026, 11, 1))
    assert fairness.block_of(indexed, pid, date(2026, 11, 1)) is None
    assert fairness.member_days(indexed, pid, date(2026, 11, 1), date(2026, 11, 1)) == []
    reports = []
    for period in ('rota', 'block'):
        goal = policy({'metric': 'duties', 'period': period})
        expected = fairness.goal_report(data, goal)
        assert fairness.goal_report(indexed, goal) == expected
        reports.append(expected)
    # maintenance added the report's audience and period metadata; the indexed and complete
    # paths are equal, so this seal follows the complete report content.
    assert digest((plain, reports)) == 'b74df4bc97e9b2f57914d356465b83d84b5192d5710c2e0a99f15219305866f7'
    assert data == original


def test_fairness_dated_reads_do_not_rescan_other_people():
    data = department()
    names = ('memberships', 'rotations', 'availability')
    for name in names:
        data[name] = CountedRows(data[name])
    expected = dated_results(data)
    indexed = {**data, '_person_rows': index_person_rows(data)}
    # Building the request-local index visits each source row once.  The
    # department-scale fixture includes 150 people, 500 sessions and six years
    # of dated history; an accidental people x rows scan would exceed this
    # linear bound before any report is evaluated.
    assert {name: data[name].visited for name in names} == {
        name: len(data[name]) for name in names}
    for name in names:
        data[name].visited = 0
    assert dated_results(indexed) == expected
    assert {name: data[name].visited for name in names} == dict.fromkeys(names, 0)


def test_unindexed_rotation_lookup_keeps_its_early_return():
    data = department()
    data['rotations'] = CountedRows(data['rotations'])
    pid = next(iter(data['people']))
    assert fairness.block_of(data, pid, date(2026, 10, 1)) == (date(2026, 9, 20), date(2026, 10, 21))
    assert data['rotations'].visited == 7


@pytest.mark.parametrize('day', [date(2026, 3, 29), date(2026, 10, 25)])
@pytest.mark.parametrize('start,end,available', [('11:00', '12:00', True), ('12:00', '13:00', False)])
def test_indexed_fairness_keeps_local_noon_leave_boundaries(day, start, end, available):
    from datetime import timedelta
    from packages.domain.time import local_stamp
    from tests.test_engine_fairness_review import A

    data = world()
    data['organisation']['timezone'] = 'Europe/Berlin'
    data['availability'] = [{'person_id': A, 'kind': 'recorded_leave',
                             'start': local_stamp(day, start, 'Europe/Berlin'),
                             'end': local_stamp(day, end, 'Europe/Berlin')}]
    indexed = {**data, '_person_rows': index_person_rows(data)}
    expected = [day] if available else []
    assert fairness.member_days(data, A, day, day + timedelta(days=1)) == expected
    assert fairness.member_days(indexed, A, day, day + timedelta(days=1)) == expected
