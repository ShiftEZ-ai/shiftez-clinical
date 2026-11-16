"""Approved request windows retain their source line identity across policy revisions."""
import copy
from datetime import date, timedelta

import pytest

from packages.domain.conditions import active_rules
from packages.domain.generation import allocation_settings
from tests.test_overtime_offered import ident, offer, snapshot


def department_offers(people=2, first='2026-10-01', last='2026-11-01'):
    data = snapshot(people, first, last)
    line = {'id': ident('stable-source-line'), 'numbers': {'apply': {'value': None, 'level': 'medium'}},
            'settings': {'allocation_overtime': {'volunteer_required': True}}}
    policy = {'id': ident('revised-policy'), 'name': 'Synthetic overtime offers', 'kind': 'catalogue',
              'status': 'active', 'active': True, 'department_id': data['workspace']['department_id'],
              'starts_on': '2020-01-01', 'ends_on': None,
              'definition': {'rule': {'kind': 'catalogue', 'id': 'overtime_offers', 'lines': [line]}}}
    data['policies'] = [policy]
    return data, line


def request_offer(data, pid, name, first, last, source, **line):
    fact = offer(data, pid, name, first=first, last=last, **line)
    fact['request_id'] = ident('request-' + name)
    fact['data']['offer_source'] = source
    return fact


def probe(day, start='08:00', end='12:00', end_day=None):
    return {'id': 'probe-' + day, 'assignment_type_id': ident('type'), 'service_ids': [],
            'start': f'{day}T{start}:00+03:00', 'end': f'{end_day or day}T{end}:00+03:00', 'participants': []}


@pytest.mark.parametrize('compact', [False, True], ids=['legacy-daily', 'compact-range'])
@pytest.mark.parametrize('converted', [False, True], ids=['native-line', 'converted-source-identity'])
def test_request_offers_follow_the_stable_line_after_policy_and_position_change(compact, converted):
    data, line = department_offers()
    [pid, other] = data['people']
    source = {'policy_id': ident('previous-policy'), 'line_id': line['id'], 'line_index': 0}
    if compact:
        request_offer(data, pid, 'compact', '2026-10-05', '2026-10-07', source, weekdays=[0, 1])
    else:
        for n, day in enumerate(['2026-10-05', '2026-10-06']):
            request_offer(data, pid, 'daily-' + day, day, str(date.fromisoformat(day) + timedelta(days=1)), source,
                          weekdays=[n])
    policy = data['policies'][0]
    if converted:
        line.update(_source_identity=line['id'], _source_index=4, id=ident('converted-destination-line'))
    # A different source line at the old position must not take over the approved request.
    policy['definition']['rule']['lines'].insert(0, {**copy.deepcopy(line), 'id': ident('inserted-line'),
        '_source_identity': ident('inserted-source'), 'where': {'conditions': [
            {'field': 'service_ids', 'op': 'any', 'values': [ident('other-clinic')]}]}})
    data['policies'] = active_rules(data['policies'], context=data)
    for day, expected in [('2026-10-05', True), ('2026-10-06', True), ('2026-10-07', False)]:
        assert allocation_settings(data, probe(day), pid)[1] is expected
        assert allocation_settings(data, probe(day), other)[1] is False


def test_compact_exact_dates_cannot_widen_approval_to_an_unoffered_week_or_overnight_gap():
    data, line = department_offers()
    pid = next(iter(data['people']))
    fact = request_offer(data, pid, 'irregular', '2026-10-05', '2026-10-21',
                         {'policy_id': ident('old-policy'), 'line_id': line['id'], 'line_index': 0},
                         start='00:00', end='24:00', weekdays=[0, 1])
    fact['data']['offer_dates'] = ['2026-10-05', '2026-10-19', '2026-10-20']
    data['policies'] = active_rules(data['policies'], context=data)
    assert allocation_settings(data, probe('2026-10-05'), pid)[1]
    assert not allocation_settings(data, probe('2026-10-12'), pid)[1]
    assert not allocation_settings(data, probe('2026-10-05', '20:00', '08:00', '2026-10-06'), pid)[1]
    assert allocation_settings(data, probe('2026-10-19', '20:00', '08:00', '2026-10-20'), pid)[1]


def test_missing_stable_source_does_not_rebind_to_the_same_position_and_cache_rebuilds():
    data, line = department_offers()
    pid = next(iter(data['people']))
    request_offer(data, pid, 'source-removed', '2026-10-05', '2026-10-06',
                  {'policy_id': data['policies'][0]['id'], 'line_id': line['id'], 'line_index': 0})
    data['policies'] = active_rules(data['policies'], context=data)
    assert allocation_settings(data, probe('2026-10-05'), pid)[1]
    data['policies'] = copy.deepcopy(data['policies'])
    data['policies'][0]['template_line']['id'] = ident('replacement-line')
    assert not allocation_settings(data, probe('2026-10-05'), pid)[1]


def test_legacy_source_without_line_id_still_requires_the_same_policy_and_position():
    data, _ = department_offers()
    pid = next(iter(data['people']))
    request_offer(data, pid, 'legacy-source', '2026-10-05', '2026-10-06',
                  {'policy_id': data['policies'][0]['id'], 'line_index': 0})
    data['policies'] = active_rules(data['policies'], context=data)
    assert allocation_settings(data, probe('2026-10-05'), pid)[1]
    data['policies'] = copy.deepcopy(data['policies'])
    data['policies'][0]['template_line']['line'] = 1
    assert not allocation_settings(data, probe('2026-10-05'), pid)[1]


def test_index_extends_for_explicit_probes_outside_the_workspace():
    data, line = department_offers(last='2026-10-11')
    pid = next(iter(data['people']))
    request_offer(data, pid, 'outside-workspace', '2026-10-01', '2026-11-01',
                  {'policy_id': ident('old-policy'), 'line_id': line['id']})
    data['policies'] = active_rules(data['policies'], context=data)
    assert allocation_settings(data, probe('2026-10-05'), pid)[1]
    assert allocation_settings(data, probe('2026-10-19'), pid)[1]
    assert allocation_settings(data, probe('2026-09-30'), pid)[1] is False


def test_stable_identity_still_requires_current_scope_and_active_caps():
    data, line = department_offers()
    pid = next(iter(data['people']))
    cap_id, service = ident('cap'), ident('permitted-service')
    line['where'] = {'conditions': [{'field': 'service_ids', 'op': 'any', 'values': [service]}]}
    line['settings']['allocation_overtime']['required_policy_ids'] = [cap_id]
    cap = {**copy.deepcopy(data['policies'][0]), 'id': cap_id, 'kind': 'max_consecutive_weekends',
           'definition': {'severity': 'mandatory', 'scope': {},
                          'rule': {'kind': 'max_consecutive_weekends', 'maximum': 1}}}
    request_offer(data, pid, 'capped', '2026-10-05', '2026-10-06',
                  {'policy_id': ident('old-policy'), 'line_id': line['id']},
                  settings={'allocation_overtime': {'required_policy_ids': [cap_id]}})
    data['policies'].append(cap)
    data['policies'] = active_rules(data['policies'], context=data)
    matching = {**probe('2026-10-05'), 'service_ids': [service]}
    assert allocation_settings(data, matching, pid)[1]
    assert not allocation_settings(data, probe('2026-10-05'), pid)[1]
    # Policy activation creates a new resolved input list, invalidating the cached caps and request index.
    data['policies'] = [p for p in data['policies'] if p['id'] != cap_id]
    assert not allocation_settings(data, matching, pid)[1]
