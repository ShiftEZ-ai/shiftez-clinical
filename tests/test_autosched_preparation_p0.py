"""Overtime admission preserves permissions without doing unrelated allocation scoring."""
from collections import Counter
import copy
import time

import pytest

from packages.domain import cp_sat, validation
from packages.domain.conditions import active_rules
from packages.domain.generation import allocation_overtime_allowed, allocation_settings
from tests.test_overtime_offer_index import department_offers, probe, request_offer
from tests.test_overtime_offered import ident
from tests.test_performance_budget import python_budget
from tests.test_schedule_score_scale import department


@pytest.mark.parametrize('case,expected', [
    ('whenever-free', True), ('missing-offer', False), ('approved-offer', True),
    ('partial-offer', False), ('overnight-approved', True), ('overnight-gap', False),
    ('active-cap', True), ('missing-cap', False), ('dated-eligible', True),
    ('eligibility-not-started', False), ('eligibility-ended', False),
    ('overnight-eligibility-gap', False),
])
@pytest.mark.parametrize('priority_field', ['priority', 'session_priority'])
def test_overtime_boolean_matches_full_settings_for_dated_offers(monkeypatch, case, expected, priority_field):
    data, line = department_offers()
    pid, supervisor = data['people']
    overnight = case.startswith('overnight-')
    shift = probe('2026-10-05', '20:00' if overnight else '08:00',
                  '08:00' if overnight else '12:00', '2026-10-06' if overnight else None)
    shift['type_snapshot'] = {'definition': {}}
    shift['participants'] = [{'person_id': supervisor, 'start': shift['start'], 'end': shift['end'],
                              'participation': 'supervisor'}]
    data['shifts'] = [shift]
    if case == 'whenever-free':
        line['settings']['allocation_overtime']['volunteer_required'] = False
    elif case != 'missing-offer':
        fact = request_offer(data, pid, case, '2026-10-05', '2026-10-07',
            {'policy_id': data['policies'][0]['id'], 'line_id': line['id']},
            start='00:00' if overnight else '08:00',
            end='24:00' if overnight else '10:00' if case == 'partial-offer' else '12:00')
        fact['data']['offer_dates'] = ['2026-10-05'] if case == 'overnight-gap' else ['2026-10-05', '2026-10-06']
    if case in {'active-cap', 'missing-cap'}:
        cap = ident('required-cap')
        line['settings']['allocation_overtime']['required_policy_ids'] = [cap]
        if case == 'active-cap':
            data['policies'].append({**copy.deepcopy(data['policies'][0]), 'id': cap,
                'definition': {'severity': 'mandatory', 'scope': {},
                               'rule': {'kind': 'max_consecutive_weekends', 'maximum': 1}}})
    if 'eligib' in case:
        service = ident('eligible-service')
        line['who'] = {'conditions': [{'field': 'eligible_service_ids', 'op': 'any', 'values': [service]}]}
        data['eligibilities'] = [{'person_id': pid, 'service_id': service,
            'starts_on': '2026-10-06' if case == 'eligibility-not-started' else '2026-10-05',
            'ends_on': '2026-10-05' if case == 'eligibility-ended' else '2026-10-06'}]
    data['policies'] = active_rules(data['policies'], context=data)
    data['policies'].append({'id': ident('supervisor-preference'), 'name': 'Synthetic supervisor preference',
        'kind': 'allocation_preference', 'department_id': data['workspace']['department_id'],
        'status': 'active', 'active': True, 'revision': 1, 'starts_on': '2020-01-01', 'ends_on': None,
        'definition': {'scope': {}, 'severity': 'advisory', 'strength': 'medium',
            'rule': {'kind': 'allocation_preference', 'supervisor_ids': [supervisor],
                     'priority': 7, 'session_priority': 4}}})
    scans = []
    original = validation.supervision_commitments

    def commitments(view):
        scans.append(len(view['shifts']))
        return original(view)

    monkeypatch.setattr(validation, 'supervision_commitments', commitments)
    settings = allocation_settings(data, shift, pid, priority_field=priority_field)
    assert settings[0] > 0 and scans  # The parity fixture exercises the former unrelated supervisor scan.
    before = len(scans)
    assert allocation_overtime_allowed(data, shift, pid) is settings[1] is expected
    assert len(scans) == before


@pytest.fixture(scope='module', params=[False, True], ids=['ordinary', 'extra-cover'])
def preparation_work(request):
    data = department()
    people = list(data['people'])[:12]
    data['people'] = {pid: data['people'][pid] for pid in people}
    for name in ('memberships', 'eligibilities'):
        data[name] = [row for row in data[name] if row['person_id'] in people]
    data['availability'] = []
    overtime, preference = copy.deepcopy(data['policies'][0]), copy.deepcopy(data['policies'][20])
    overtime['definition']['scope'] = preference['definition']['scope'] = {}
    preference['definition']['rule'].update(supervisor_ids=[people[0]], priority=7)
    data['policies'] = [overtime, *[{**copy.deepcopy(preference), 'id': ident(f'preference-{n}')} for n in range(8)]]
    data['shifts'] = data['shifts'][::15][:6]
    for shift in data['shifts']:
        shift['locked'] = False
        shift['type_snapshot']['definition']['extra_cover'] = request.param
        shift['participants'] = [{'id': ident(shift['id'] + people[0]), 'person_id': people[0],
            'start': shift['start'], 'end': shift['end'], 'participation': 'supervisor',
            'fixed': True, 'overtime': request.param}]
    data['focus_ids'] = [shift['id'] for shift in data['shifts']]
    data['fingerprint'] = 'synthetic-overtime-preparation'
    calls = Counter(expected_pairs=len(data['shifts']) * len(data['people']))
    original_commitments, original_evidence = validation.supervision_commitments, cp_sat.candidate_evidence

    def commitments(view):
        calls['commitment_scans'] += 1
        return original_commitments(view)

    def evidence(*args):
        calls['candidate_pairs'] += 1
        return original_evidence(*args)

    models = []
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(validation, 'supervision_commitments', commitments)
        monkeypatch.setattr(cp_sat, 'supervision_commitments', commitments)
        monkeypatch.setattr(cp_sat, 'candidate_evidence', evidence)
        started = time.process_time()
        result = cp_sat.generate(data, 5, workers=1, feasibility_only=True, _on_model=models.append)
        cpu = time.process_time() - started
    return result, models, calls, cpu


def test_candidate_preparation_does_not_rebuild_department_supervision_per_candidate(preparation_work):
    result, models, calls, _ = preparation_work
    assert result['status'] == 'model_ready', result
    assert len(models) == 1 and not models[0].validate()
    assert calls['candidate_pairs'] == calls['expected_pairs']
    assert 0 < calls['commitment_scans'] <= 2, calls


@pytest.mark.cpu_budget
def test_candidate_preparation_cpu_budget(preparation_work):
    cpu = preparation_work[3]
    assert cpu < python_budget(3), cpu
