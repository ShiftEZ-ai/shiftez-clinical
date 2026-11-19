"""Approved request windows retain their source line identity across policy revisions."""
import copy
from datetime import date, timedelta

import pytest

from packages.domain.conditions import active_rules, resolved_rule_context
from packages.domain.generation import allocation_settings, overtime_cap_ids
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


def _workload_limit_version(data, ident_value, *, status='active', active=True, merged_ids=(), comparison='maximum',
                            level='mandatory', severity='mandatory', approved_by=None, on=True):
    line = {'id': ident('workload-line-' + ident_value), 'measure': 'scheduled_hours',
            'period': {'kind': 'calendar_month'}, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01',
            'numbers': {('max' if comparison == 'maximum' else 'min'): {'value': 40, 'level': level}},
            'settings': {'block_quota': {'metric': 'scheduled_hours', 'window_alignment': 'calendar_month',
                                         'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}}}
    return {'id': ident_value, 'name': 'Synthetic workload limit', 'kind': 'catalogue',
            'status': status, 'active': active, 'approved_by': approved_by,
            'department_id': data['workspace']['department_id'],
            'starts_on': '2020-01-01', 'ends_on': None,
            'definition': {'severity': severity, 'scope': {}, 'merged_ids': list(merged_ids),
                           'rule': {'kind': 'catalogue', 'id': 'workload_limit', 'catalogue_version': 2,
                                    'on': on, 'settings': {}, 'lines': [line]}}}


@pytest.mark.parametrize('reader', [active_rules, overtime_cap_ids, resolved_rule_context],
                         ids=['active-rules', 'overtime-caps', 'resolved-context'])
def test_overtime_readers_leave_the_input_unchanged(reader):
    data, _ = department_offers()
    data['policies'].extend([
        _workload_limit_version(data, ident('read-old'), status='retired', active=False, approved_by=ident('approver')),
        _workload_limit_version(data, ident('read-new')),
    ])
    before = copy.deepcopy(data)
    reader(data)
    assert data == before


def test_overtime_caps_full_subset_full_calls_are_independent():
    data, _ = department_offers()
    old_id, new_id = ident('independent-old'), ident('independent-new')
    old = _workload_limit_version(data, old_id, status='retired', active=False, approved_by=ident('approver'))
    new = _workload_limit_version(data, new_id)
    data['policies'] = [old, new]
    expected = {old_id, new_id}
    assert overtime_cap_ids(data) == expected
    resolved_rule_context({**data, 'policies': list(data['policies'])})
    assert overtime_cap_ids({**data, 'policies': [old]}) == set()
    assert overtime_cap_ids(data) == expected


@pytest.mark.parametrize('prepared', [False, True], ids=['raw-input', 'prepared-input'])
def test_new_workload_limit_version_keeps_offer_pinned_to_the_previous_policy_id(prepared):
    data, line = department_offers()
    pid = next(iter(data['people']))
    old_id, new_id, offer_id = (ident(name) for name in ('workload-old', 'workload-new', 'versioned-offer'))
    old = _workload_limit_version(data, old_id, status='retired', active=False, approved_by=ident('approver'))
    new = _workload_limit_version(data, new_id)
    offer = {**copy.deepcopy(data['policies'][0]), 'id': offer_id, 'kind': 'allocation_overtime',
             'definition': {'severity': 'advisory', 'strength': 'medium', 'scope': {},
                            'rule': {'kind': 'allocation_overtime', 'required_policy_ids': [old_id]}}}
    data['policies'] = [old, new, offer]
    if prepared:
        data['_overtime_originals'] = {p['id']: p for p in data['policies']}
    data = resolved_rule_context(data)
    assert allocation_settings(data, probe('2026-10-05'), pid)[1]


def test_retired_workload_limit_without_an_active_replacement_turns_offer_off():
    data, _ = department_offers()
    pid = next(iter(data['people']))
    cap_id, offer_id = ident('retired-workload'), ident('retired-offer')
    cap = _workload_limit_version(data, cap_id, status='retired', active=False, approved_by=ident('approver'))
    offer = {**copy.deepcopy(data['policies'][0]), 'id': offer_id, 'kind': 'allocation_overtime',
             'definition': {'severity': 'advisory', 'strength': 'medium', 'scope': {},
                            'rule': {'kind': 'allocation_overtime', 'required_policy_ids': [cap_id]}}}
    assert overtime_cap_ids({**data, 'policies': [cap, offer]}) == set()
    data['policies'] = active_rules([cap, offer], context={**data, 'policies': [cap, offer]})
    assert allocation_settings(data, probe('2026-10-05'), pid)[1] is False


@pytest.mark.parametrize('status', ['proposed', 'rejected'])
def test_unapproved_workload_limit_versions_do_not_enable_an_offer(status):
    data, _ = department_offers()
    pid = next(iter(data['people']))
    old_id, new_id, offer_id = (ident(status + '-' + name) for name in ('old', 'new', 'offer'))
    old = _workload_limit_version(data, old_id, status=status, active=False)
    new = _workload_limit_version(data, new_id)
    offer = {**copy.deepcopy(data['policies'][0]), 'id': offer_id, 'kind': 'allocation_overtime',
             'definition': {'severity': 'advisory', 'strength': 'medium', 'scope': {},
                            'rule': {'kind': 'allocation_overtime', 'required_policy_ids': [old_id]}}}
    data['policies'] = active_rules([old, new, offer], context={**data, 'policies': [old, new, offer]})
    assert old_id not in overtime_cap_ids({**data, 'policies': [old, new, offer]})
    assert allocation_settings(data, probe('2026-10-05'), pid)[1] is False


@pytest.mark.parametrize('successor', [
    {'comparison': 'minimum'},
    {'comparison': 'maximum', 'severity': 'advisory', 'level': 'medium'},
    {'comparison': 'maximum', 'active': False},
    {'on': False},
], ids=['minimum', 'advisory', 'switched-off', 'catalogue-off'])
def test_non_cap_successors_do_not_enable_an_offer(successor):
    data, _ = department_offers()
    pid = next(iter(data['people']))
    old_id, new_id, offer_id = (ident('non-cap-' + name) for name in ('old', 'new', 'offer'))
    old = _workload_limit_version(data, old_id, status='retired', active=False, approved_by=ident('approver'))
    new = _workload_limit_version(data, new_id, **successor)
    offer = {**copy.deepcopy(data['policies'][0]), 'id': offer_id, 'kind': 'allocation_overtime',
             'definition': {'severity': 'advisory', 'strength': 'medium', 'scope': {},
                            'rule': {'kind': 'allocation_overtime', 'required_policy_ids': [old_id]}}}
    assert overtime_cap_ids({**data, 'policies': [old, new, offer]}) == set()
    data['policies'] = active_rules([old, new, offer], context={**data, 'policies': [old, new, offer]})
    assert allocation_settings(data, probe('2026-10-05'), pid)[1] is False


def test_overtime_caps_follow_replacement_and_merge_chains_without_cycles():
    data, _ = department_offers()
    ids = [ident('lineage-' + name) for name in ('old', 'middle', 'current')]
    policies = [_workload_limit_version(data, key) for key in ids]
    for policy in policies:
        policy['kind'] = 'max_consecutive_weekends'
        policy['definition']['rule'] = {'kind': 'max_consecutive_weekends', 'maximum': 1}
    for policy in policies[:2]:
        policy.update(status='retired', active=False, approved_by=ident('approver'))
    policies[0]['definition']['replaces_id'] = ids[2]
    policies[1]['definition']['replaces_id'] = ids[0]
    policies[2]['definition']['merged_ids'] = [ids[1]]
    data['policies'] = policies
    assert overtime_cap_ids(data) == set(ids)


@pytest.mark.parametrize(('status', 'approved', 'included'), [
    ('active', False, True), ('retired', True, True), ('retired', False, False),
    ('proposed', False, False), ('rejected', False, False),
])
@pytest.mark.parametrize('same_department', [False, True])
def test_overtime_conversion_lineage_requires_approved_rows_in_the_same_department(status, approved, included,
                                                                                  same_department):
    data, _ = department_offers()
    old_id, new_id = ident('conversion-old'), ident('conversion-new')
    old = _workload_limit_version(data, old_id, status=status, active=status == 'active',
                                  approved_by=ident('approver') if approved else None)
    old['kind'] = 'block_quota'
    old['definition']['rule'] = {'kind': 'block_quota', 'comparison': 'maximum', 'amount': 40,
                                 'metric': 'scheduled_hours', 'window_days': 7}
    if not same_department:
        old['department_id'] = ident('other-department')
    new = _workload_limit_version(data, new_id)
    data['policies'] = [old, new]
    data['_catalogue_v2_materialized'] = True
    data['source_bindings'] = [{'source_policy_id': old_id, 'source_catalogue_id': 'hours_per_week',
                               'department_id': old['department_id'], 'source': old,
                               'destinations': [{'kind': 'policy', 'id': new_id}]}]
    # An active legacy cap is independently in force, even outside the conversion's catalogue entry.
    expected = {new_id, old_id} if status == 'active' or included and same_department else {new_id}
    before = copy.deepcopy(data)
    assert overtime_cap_ids(data) == expected
    assert data == before
