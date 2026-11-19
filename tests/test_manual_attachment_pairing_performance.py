"""Manual pairing checks one session, even on a populated department month."""
import time
from types import SimpleNamespace

import pytest

from apps.api.services import planning
from packages.domain import fairness, quotas, validation
from tests.scheduler_departments import department
from tests.test_performance_budget import python_budget


@pytest.fixture(scope='module')
def manual_pairing_month(scale_harness):
    data, info = department(scale_harness('h'), 713, people=150, days=28, rules='full', leave_rate=0, clinics=9)
    trainees = sorted(info['trainees'])[:4]
    supervisors = sorted(pid for pid in data['people']
                         if pid not in info['trainees'] and pid != info['away'])[:20]
    shift = next(s for s in data['shifts'] if s['assignment_type_id'] == info['types']['pm'])

    def participant(session, pid, participation='staff'):
        return {'id': session['id'] + ':' + pid, 'person_id': pid, 'start': session['start'], 'end': session['end'],
                'participation': participation, 'supervisor_person_id': None, 'overtime': False,
                'team_lead': False, 'fixed': False, 'segments': []}

    background = [pid for pid in data['people']
                  if pid not in info['trainees'] and pid not in supervisors and pid != info['away']]
    for index, session in enumerate(data['shifts']):
        session['participants'] = [participant(session, background[(3 * index + offset) % len(background)])
                                   for offset in range(3)]
    shift['participants'] = [participant(shift, pid, 'trainee') for pid in trainees] + [
        participant(shift, pid, 'supervisor') for pid in supervisors]
    data['mentorships'] = [{'id': f'{pid}:{mentor}', 'person_id': pid, 'mentor_id': mentor,
        'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'strength': 'preferred',
        'assignment_type_ids': [], 'service_ids': []} for pid in trainees for mentor in supervisors]
    assignments = {p['id']: SimpleNamespace(supervisor_person_id=None) for p in shift['participants']}
    checks, sessions = [], []
    evaluate, session_setup = planning.evaluate, validation.validate_session_setup

    def checked(trial, **kwargs):
        assert trial['_partial'] == {'shifts': {shift['id']}, 'people': set(trainees)}
        assert kwargs.get('explain_findings') is False
        checks.append(1)
        return evaluate(trial, **kwargs)

    def setup(context, session, *args):
        sessions.append(session['id'])
        return session_setup(context, session, *args)

    def unrelated_report(*args, **kwargs):
        pytest.fail('Naming a supervisor must not rebuild department quota or fairness reports')

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(planning, 'domain_snapshot', lambda db, workspace, **kwargs: data)
        monkeypatch.setattr(planning, 'evaluate', checked)
        monkeypatch.setattr(validation, 'validate_session_setup', setup)
        monkeypatch.setattr(quotas, 'evaluate_quotas', unrelated_report)
        monkeypatch.setattr(fairness, 'evaluate_goals', unrelated_report)
        began = time.process_time()
        planning.name_attached_supervisors(SimpleNamespace(get=lambda model, key: assignments[key], info={}), None,
                                           SimpleNamespace(id=shift['id'], type_snapshot=shift['type_snapshot']))
        elapsed = time.process_time() - began
    metrics = {'people': len(data['people']), 'sessions': len(data['shifts']), 'candidate_pairs': len(trainees) * len(supervisors),
               'checks': len(checks), 'session_checks': len(sessions), 'cpu_seconds': elapsed}
    print('manual attachment pairing department month', metrics)
    return data, shift, assignments, trainees, supervisors, sessions, metrics


def test_manual_pairing_month_checks_only_the_changed_session(manual_pairing_month):
    data, shift, assignments, trainees, supervisors, sessions, metrics = manual_pairing_month
    assert len(data['people']) == 150 and len(data['shifts']) >= 500
    assert metrics['candidate_pairs'] == 80
    assert metrics['candidate_pairs'] <= metrics['checks'] <= 2 * metrics['candidate_pairs'] + 1
    assert sessions == [shift['id']] * metrics['checks']
    for participant in shift['participants']:
        if participant['person_id'] in trainees:
            assert assignments[participant['id']].supervisor_person_id in supervisors


@pytest.mark.cpu_budget
def test_manual_pairing_department_month_cpu_budget(manual_pairing_month):
    assert manual_pairing_month[-1]['cpu_seconds'] < python_budget(4), manual_pairing_month[-1]
# Include this file in hosted supervision and team-lead qualification.
