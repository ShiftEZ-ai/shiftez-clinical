"""Department-scale CPU guard for the assignment trials used by staffing/person scores."""
import copy
import time

import pytest

from packages.domain.validation import evaluate, index_person_rows
from tests.scheduler_departments import department
from tests.test_performance_budget import python_budget


def supervised_department(h):
    data, info = department(h, 7, people=150, days=28, clinics=9, rules='none', leave_rate=0)
    trainees = sorted(info['trainees'])
    supervisors = sorted(set(data['people']) - info['trainees'] - {info['away']})
    for n, shift in enumerate(data['shifts']):
        if shift['assignment_type_id'] == info['types']['pm']:
            people = [supervisors[n % (len(supervisors) - 2)],
                      trainees[(2 * n) % len(trainees)], trainees[(2 * n + 1) % len(trainees)]]
        else:
            people = [supervisors[n % (len(supervisors) - 2)]]
        shift['participants'] = [
            {'id': f'scale-seat-{n}-{i}', 'shift_id': shift['id'], 'person_id': pid,
             'start': shift['start'], 'end': shift['end'], 'participation': 'staff',
             'overtime': False, 'fixed': False, 'revision': 1,
             'supervisor_person_id': people[0] if pid in info['trainees'] else None}
            for i, pid in enumerate(people)]
    # Historical rules count towards stored department size without inventing active obligations.
    seed = data['policies'][0]
    data['policies'] += [{**copy.deepcopy(seed), 'id': f'scale-retired-{n}',
                         'status': 'retired', 'active': False} for n in range(1000 - len(data['policies']))]
    return data


def candidate_trial_measurement(data):
    """Same immutable assignment-copy/cache contract as staffing and person_score."""
    data = {**data, '_person_rows': index_person_rows(data), '_person_dates_cache': {},
            '_scope_interval_cache': {}, '_line_index_cache': {}, '_bucket_keys_cache': {}}
    target = next(shift for shift in data['shifts']
                  if shift['type_snapshot']['definition'].get('supervision'))
    occupied = {p['person_id'] for p in target['participants']}
    candidates = sorted(set(data['people']) - occupied)[:20]
    cache = {}
    started, wall = time.process_time(), time.perf_counter()
    evaluate(data, session_cache=cache)
    for pid in candidates:
        seat = {**target['participants'][0], 'id': f'candidate-{pid}', 'person_id': pid}
        trial = {**data, 'shifts': [dict(s, participants=[*s['participants'], seat])
                                   if s is target else s for s in data['shifts']]}
        evaluate(trial, session_cache=cache)
    return {'cpu_seconds': time.process_time() - started, 'wall_seconds': time.perf_counter() - wall,
            'people': len(data['people']), 'sessions': len(data['shifts']),
            'assignments': sum(len(s['participants']) for s in data['shifts']),
            'supervised_sessions': sum(bool(s['type_snapshot']['definition'].get('supervision'))
                                       for s in data['shifts']),
            'rules_including_history': len(data['policies']),
            'active_rules': sum(p['status'] == 'active' and p.get('active', True) for p in data['policies']),
            'candidate_trials': len(candidates)}


@pytest.mark.cpu_budget
def test_department_supervised_candidate_trials_cpu_budget(scale_harness):
    data = supervised_department(scale_harness('h'))
    assert len(data['people']) == 150 and len(data['shifts']) == 532
    metrics = candidate_trial_measurement(data)
    print('supervised candidate trials', metrics)
    # Before selective invalidation this same 21-check workload took 5.58 CPU seconds; fixed: 3.66.
    assert metrics['cpu_seconds'] < python_budget(4.5), metrics
