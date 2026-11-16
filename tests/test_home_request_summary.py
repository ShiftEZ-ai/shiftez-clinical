"""Home summarizes the same own requests as Requests without carrying private workflow data."""
import json
import time
from datetime import timedelta

import coverage
import pytest

from packages.domain.time import now as clock_now
from .governance_support import identity
from .test_home import big_department


def mixed_requests(h):
    day = clock_now().date() + timedelta(days=14)
    own, other = h.seed['people'][:2]
    identity(h, 'summary-colleague', ['staff'], other)
    workspace = h.workspace('Home summary duty', day.isoformat(), (day + timedelta(days=7)).isoformat())
    workspace = h.shift(workspace, f'{day}T08:00:00+03:00', f'{day}T12:00:00+03:00')['workspace']
    workspace = h.shift(workspace, f'{day + timedelta(days=1)}T08:00:00+03:00',
                        f'{day + timedelta(days=1)}T12:00:00+03:00', people=[other])['workspace']
    publication = h.publish(workspace)['publication_id']
    duty = h.get('staff', '/api/swaps/options')['assignments'][0]
    swap = h.command('staff', 'swap.offer', {'publication_id': publication, 'assignment_id': duty['assignment_id'],
        'recipient_person_id': other, 'expires_at': (clock_now() + timedelta(hours=4)).isoformat(),
        'reason': 'Summary swap message'})['swap']
    leave = h.command('staff', 'request.create', {'person_id': own, 'kind': 'leave',
        'start': f'{day + timedelta(days=1)}T00:00:00+03:00', 'end': f'{day + timedelta(days=2)}T00:00:00+03:00',
        'private_reason': 'Home must never carry this private request reason'})['record']
    h.command('staff', 'availability_pattern.propose', {'person_id': own, 'weekdays': [1], 'start_time': '13:00',
        'end_time': '17:00', 'starts_on': day.isoformat(), 'note': 'Weekly summary note'})
    h.command('staff', 'wish.create', {'person_id': own, 'first_day': (day + timedelta(days=20)).isoformat(),
        'last_day': (day + timedelta(days=20)).isoformat(), 'kind': 'prefer_off', 'points': 1, 'note': 'Wish summary note'})
    h.command('coordinator', 'availability.record', {'person_id': own, 'kind': 'actual_absence',
        'start': f'{day + timedelta(days=3)}T08:00:00+03:00', 'end': f'{day + timedelta(days=3)}T10:00:00+03:00',
        'private_reason': 'Home must never carry this recorded absence reason'})
    other_request = h.command('summary-colleague', 'request.create', {'person_id': other, 'kind': 'leave',
        'start': f'{day}T00:00:00+03:00', 'end': f'{day + timedelta(days=1)}T00:00:00+03:00',
        'private_reason': 'Other person private reason'})['record']
    return own, swap, leave, other_request


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_home_uses_all_own_request_sources_and_only_summary_fields(request, database):
    h = request.getfixturevalue(database)
    own, swap, leave, other = mixed_requests(h)
    identity(h, 'linked-reviewer', ['approver'], own)
    for role in ('staff', 'linked-reviewer'):
        summary = h.get(role, '/api/home')['my_requests']
        unified = h.get(role, f'/api/requests/all?person_id={own}&limit=5')['items']
        assert [row['id'] for row in summary] == [row['id'] for row in unified]
        assert len(summary) == 5 and {row['source'] for row in summary} == {'request', 'pattern', 'wish', 'swap', 'availability'}
        assert other['id'] not in {row['id'] for row in summary}
        assert next(row for row in summary if row['id'] == leave['id'])['stage'] == 'waiting'
        assert set(next(row for row in summary if row['id'] == swap['id'])['offered_assignment']) == {'start', 'end'}
        serialized = json.dumps(summary)
        for forbidden in ('private_reason', 'decision_reason', 'workspace_id', 'publication_id', 'created_by',
                          'Weekly summary note', 'Wish summary note', 'Summary swap message', 'Other person private reason'):
            assert forbidden not in serialized
    recipient = h.get('summary-colleague', '/api/home')['my_requests']
    assert swap['id'] in {row['id'] for row in recipient}  # recipient summaries retain the same two-sided swap semantics


@pytest.fixture(scope='module', params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def personal_home_summary_scale(request, scale_harness):
    h = scale_harness(request.param)
    people = big_department(h)
    identity(h, 'summary-scale', ['staff'], people[0])
    h.get('summary-scale', '/api/home')
    began, wall_started = time.process_time(), time.perf_counter()
    for _ in range(3):
        summary = h.get('summary-scale', '/api/home')['my_requests']
    elapsed = (time.process_time() - began) / 3
    wall_elapsed = (time.perf_counter() - wall_started) / 3
    print(f'Home own summary, 160 people / 3000 requests: {elapsed * 1000:.0f} CPU ms / {wall_elapsed * 1000:.0f} wall ms ({request.param})')
    return summary, people[0], elapsed, wall_elapsed


def test_personal_home_summary_stays_bounded_at_department_scale(personal_home_summary_scale):
    summary, person, _, _ = personal_home_summary_scale
    assert len(summary) == 5
    assert {row['person_id'] for row in summary} == {person}


@pytest.mark.cpu_budget
def test_personal_home_summary_stays_bounded_at_department_scale_cpu_budget(personal_home_summary_scale):
    _, _, elapsed, _ = personal_home_summary_scale
    assert elapsed < (5 if coverage.Coverage.current() else 1.5)


@pytest.mark.wall_benchmark
def test_personal_home_summary_department_wall_benchmark(personal_home_summary_scale):
    _, _, _, elapsed = personal_home_summary_scale
    assert elapsed < (5 if coverage.Coverage.current() else 1.5)
