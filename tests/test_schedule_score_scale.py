"""Department-sized rota checks: unchanged receipts and bounded Python work."""
from collections import Counter
import copy
from datetime import date, timedelta
import gc
import hashlib
import json
import sys
import time
from types import SimpleNamespace
import uuid

import pytest

from packages.contracts.schemas import PolicyDefinition, Scope, TypeDefinition


def _id(name):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'schedule-score-scale:' + name))


def department():
    """Synthetic month: 150 people, 450 sessions, named offers and scoped rules."""
    people = [_id(f'person-{n:03}') for n in range(150)]
    services = [_id(f'clinic-{n}') for n in range(6)]
    dept, rank, room, duty = (_id(name) for name in ('department', 'rank', 'room', 'duty'))
    definition = TypeDefinition(requirements=[{'minimum': 2, 'maximum': 3}]).model_dump(mode='json')
    data = {
        'organisation': {'id': _id('organisation'), 'timezone': 'Asia/Riyadh', 'week_start': 6},
        'department': {'id': dept},
        'workspace': {'id': _id('workspace'), 'department_id': dept, 'starts_on': '2026-10-01',
                      'ends_on': '2026-10-31', 'content_revision': 1, 'rule_adjustments': [], 'target_overrides': []},
        'people': {pid: {'id': pid, 'display_name': f'Synthetic clinician {n:03}', 'active': True, 'tags': []}
                   for n, pid in enumerate(people)},
        'categories': {rank: {'id': rank, 'active': True, 'starts_on': '2020-01-01'}},
        'services': {sid: {'id': sid, 'name': f'Clinic {n}', 'active': True, 'mode': 'physical'}
                     for n, sid in enumerate(services)},
        'locations': {room: {'id': room, 'name': 'Synthetic clinic suite', 'parent_id': None,
                             'location_type_id': _id('room-type'), 'capacity': 1000}},
        'location_types': {_id('room-type'): {'id': _id('room-type'), 'active': True}}, 'closures': [],
        'types': {duty: {'id': duty, 'department_id': dept, 'name': 'Clinic', 'active': True,
                         'starts_on': '2020-01-01', 'revision': 1, 'definition': definition}},
        'capabilities': {}, 'programmes': {}, 'rotation_types': {}, 'cohorts': {},
        'memberships': [{'id': _id('membership-' + pid), 'person_id': pid, 'department_id': dept,
                         'category_id': rank, 'starts_on': '2020-01-01'} for pid in people],
        'eligibilities': [{'id': _id('eligibility-' + pid + sid), 'person_id': pid, 'service_id': sid,
                           'starts_on': '2020-01-01', 'active': True} for pid in people for sid in services],
        'grants': [], 'availability': [], 'rotations': [], 'programme_memberships': [],
        'exceptions': [], 'policies': [], 'shifts': [], 'focus_ids': [],
    }
    for day in range(30):
        on = date(2026, 10, 1) + timedelta(days=day)
        for slot in range(15):
            hour = 8 + 4 * (slot % 3)
            start, end = (f'{on}T{h:02}:00:00+03:00' for h in (hour, hour + 4))
            sid = _id(f'session-{day}-{slot}')
            assigned = [people[(day * 15 + slot * 2 + n) % len(people)] for n in range(2)]
            data['shifts'].append({
                'id': sid, 'workspace_id': data['workspace']['id'], 'start': start, 'end': end,
                'cancelled': False, 'assignment_type_id': duty, 'type_revision': 1, 'attributes': {}, 'location_id': room,
                'service_ids': [services[slot % len(services)]],
                'type_snapshot': {'id': duty, 'name': 'Clinic', 'definition': copy.deepcopy(definition)},
                'participants': [{'id': _id(sid + pid), 'person_id': pid, 'start': start, 'end': end,
                                  'participation': 'staff', 'overtime': pid in people[:20] and day % 3 == 0}
                                 for pid in assigned],
            })
            data['focus_ids'].append(sid)
    for n, pid in enumerate(people[::10]):
        data['availability'].append({'id': _id('leave-' + pid), 'person_id': pid, 'kind': 'approved_leave',
            'start': f'2026-10-{1 + n:02}T00:00:00+03:00', 'end': f'2026-10-{8 + n:02}T00:00:00+03:00'})

    def add(name, rule, scope=None, severity='advisory'):
        data['policies'].append({
            'id': _id(name), 'name': name, 'kind': rule['kind'], 'department_id': dept,
            'status': 'active', 'active': True, 'revision': 1, 'starts_on': '2020-01-01', 'ends_on': None,
            'definition': PolicyDefinition.model_validate({
                'rule': rule, 'scope': scope or {}, 'severity': severity,
            }).model_dump(mode='json'),
        })

    for n, pid in enumerate(people[:20]):
        add(f'overtime-{n}', {'kind': 'allocation_overtime'}, {'person_id': pid})
    for n in range(12):
        scope = ({'service_ids': [services[n % 6]]} if n < 6 else
                 {'conditions': {'conditions': [{'field': 'service_ids', 'values': [services[n % 6]]}]},
                  'start_time': '12:00', 'end_time': '16:00'})
        add(f'preference-{n}', {'kind': 'allocation_preference', 'priority': 5 if n % 2 else -3}, scope)
    for n, pid in enumerate(people[:39]):
        # Some people have no opportunity: Fair share must probe all their sessions.
        add(f'restriction-{n}', {'kind': 'restriction', 'weekdays': list(range(7)) if n < 5 else [n % 7],
                                'start_time': '00:00', 'end_time': '24:00'},
            {'person_id': pid}, severity='mandatory')
    add('leave-buffer', {'kind': 'restriction', 'weekdays': list(range(7)), 'start_time': '00:00',
                         'end_time': '24:00', 'leave_buffer_days': 1}, severity='mandatory')
    # The frozen receipt predates the configurable default changing from rota to rolling.
    add('fair-share', {'kind': 'balance_goal', 'metric': 'duties', 'aim': 'equal_share', 'period': 'rota'})
    return data


def revalidate_snapshot(data, monkeypatch):
    """Measure the real revalidate path, with only database loading replaced by the synthetic snapshot."""
    from apps.api.services import planning
    from packages.domain.input_manifest import manifest
    data['input_manifest'] = manifest(data)
    workspace = SimpleNamespace(department_id=data['workspace']['department_id'], status='draft', validation=None)
    db = SimpleNamespace(info={}, flush=lambda: None, get=lambda *args: SimpleNamespace(generation_defaults={}))
    monkeypatch.setattr(planning, 'domain_snapshot', lambda *args, **kwargs: data)
    return planning.revalidate(db, workspace)


def receipt_bytes(result):
    # Wall-clock evaluation time is metadata; every score, finding and report byte is retained.
    return json.dumps({k: v for k, v in result.items() if k != 'evaluated_at'},
                      sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


@pytest.fixture(scope='module')
def department_check():
    from packages.domain import allocation_score, generation, validation
    data = department()
    calls = Counter()
    session_probes = Counter()
    restriction_probes = Counter()
    with pytest.MonkeyPatch.context() as monkeypatch:
        original_overtime = allocation_score.allocation_overtime_allowed
        original_settings = generation.allocation_settings
        original_session = generation.session_matches
        original_restriction = validation.applicable_restriction_intervals
        original_named_people = validation.named_people

        def overtime(*args, **kwargs):
            calls['overtime'] += 1
            return original_overtime(*args, **kwargs)

        def settings(*args, **kwargs):
            calls['allocation_settings'] += 1
            return original_settings(*args, **kwargs)

        def session(view, scope, shift):
            session_probes[id(scope), shift['id']] += 1
            return original_session(view, scope, shift)

        def restriction(view, policy, pid, *args):
            restriction_probes[policy['id'], pid] += 1
            return original_restriction(view, policy, pid, *args)

        def named_people(scope):
            if sys._getframe(1).f_code is validation.blocking_restrictions.__code__:
                calls['restriction_index'] += 1
            return original_named_people(scope)

        monkeypatch.setattr(allocation_score, 'allocation_overtime_allowed', overtime)
        monkeypatch.setattr(generation, 'allocation_settings', settings)
        monkeypatch.setattr(generation, 'session_matches', session)
        monkeypatch.setattr(validation, 'applicable_restriction_intervals', restriction)
        monkeypatch.setattr(validation, 'named_people', named_people)
        gc.collect()
        started = time.process_time()
        result = revalidate_snapshot(data, monkeypatch)
        elapsed = time.process_time() - started
    print(f'schedule check: {elapsed:.3f} CPU s, 150 people, 450 sessions, 73 rules')
    print('schedule check work', {'overtime_probes': calls['overtime'],
                                 'allocation_settings': calls['allocation_settings'],
                                 'session_side_probes': sum(session_probes.values()),
                                 'restriction_index_resolutions': calls['restriction_index'],
                                 'restriction_interval_probes': sum(restriction_probes.values())})
    return data, result, elapsed, calls, session_probes, restriction_probes


def test_realistic_check_receipt_is_byte_identical_to_before_optimization(department_check):
    from packages.domain.score import VERSION, settings_of
    from packages.domain.validation import ENGINE_VERSION
    data, result, *_ = department_check
    assert (len(data['people']), len(data['shifts'])) == (150, 450)
    assert result['findings'] and result['score']['people']
    assert result['engine_version'] == ENGINE_VERSION
    assert result['score']['version'] == VERSION
    assert result['score']['settings'] == settings_of({})
    # Current diagnostics/settings are checked separately; the original findings and score values stay frozen.
    def original_fields(value):
        if isinstance(value, dict):
            return {key: original_fields(part) for key, part in value.items() if key not in {'cause', 'fix_hints'}}
        if isinstance(value, list):
            return [original_fields(part) for part in value]
        return value
    metadata = {'diagnostic_version', 'score_dependencies', 'input_manifest',
                'fix_list_context', 'fix_list_state', 'fix_list_delta', '_person_parts'}
    original = original_fields({key: value for key, value in result.items() if key not in metadata})
    # Audience labels and goal references are additive diagnostics, not changes to findings or fairness quantities.
    for goal in original['goals']:
        assert goal.pop('audience_label') == 'Everyone'
        assert goal.pop('period_start') == data['workspace']['starts_on']
        assert goal.pop('period_end') == data['workspace']['ends_on']
        assert goal.pop('applies_to_nobody') is False
        assert goal.pop('comparison') == 'within_group'
    for finding in original['findings']:
        if finding['code'] == 'BALANCE_GOAL':
            assert finding['detail'].pop('goal_id') == original['goals'][0]['goal_id']
        if finding['code'] == 'UNAVAILABLE':
            assert finding['classification'] == finding['detail']['classification'] == 'duties_during_approved_leave'
            assert finding['leave_shift_ids'] == finding['detail']['leave_shift_ids']
            for part in (finding, finding['detail']):
                part.pop('classification')
                part.pop('leave_shift_ids')
    # maintenance added a check-reuse contract; the original receipt predates that metadata.
    from packages.domain.coverage import COVERAGE_CONTRACT
    assert original['contracts'].pop('cover_places') == COVERAGE_CONTRACT
    # Canonical scoped counts are additive reporting; basic evidence and score remain frozen.
    for row in original['coverage']:
        for key in ('places_needed', 'places_filled', 'places_open'):
            row.pop(key, None)
    # These wish settings were added after the frozen receipt; this fixture has no wishes.
    for key in ('wish_history_periods', 'wish_granted_credit', 'wish_priority_decay'):
        original['score']['settings'].pop(key)
    original['engine_version'] = 'manual-core-46'
    original['score']['version'] = 'score-v10'
    # SHA-256 of the complete canonical receipt from .
    assert hashlib.sha256(receipt_bytes(original)).hexdigest() == 'd48a5f04f6b9e6a2d5294c30366f68c686989b7f0df91ecb232e379eb01fe561'


def test_check_only_visits_named_overtime_people_and_caches_session_sides(department_check):
    _, _, _, calls, session_probes, restriction_probes = department_check
    assert 0 < calls['overtime'] <= 20 * 450
    assert calls['allocation_settings'] <= 20 * 450
    assert session_probes and max(session_probes.values()) == 1
    assert len(session_probes) <= 32 * 450
    # One build for each resolved rule pool (validation and score), never one per person/session probe.
    assert 0 < calls['restriction_index'] <= 2 * 40
    # Named restrictions never reach somebody else through Fair share eligibility probes.
    named = {_id(f'restriction-{n}'): _id(f'person-{n:03}') for n in range(39)}
    assert restriction_probes and all(rid not in named or pid == named[rid] for rid, pid in restriction_probes)


@pytest.mark.cpu_budget
def test_department_revalidate_cpu_budget(department_check):
    import coverage
    cpu = department_check[2]
    # Before optimization: 61.7 CPU s; optimized: 8.7 s. Coverage traces pure Python several times slower.
    budget = 15 * (7 if coverage.Coverage.current() else 1)
    assert cpu < budget, (cpu, budget)


@pytest.mark.parametrize('match,op', [('all', 'any'), ('any', 'any'), ('all', 'none'), ('any', 'none')])
@pytest.mark.parametrize('identified', [True, False])
def test_session_prefilter_preserves_mixed_person_and_session_conditions(match, op, identified):
    from packages.domain.generation import allocation_policy_matches, _allocation_policy_matches
    data = department()
    people = list(data['people'])[:2]
    clinics = list(data['services'])
    policy = data['policies'][20]
    policy['definition']['scope'] = Scope(conditions={
        'match': match, 'conditions': [{'field': 'person_ids', 'values': people[:1], 'op': op},
                                     {'field': 'service_ids', 'values': clinics[:1], 'op': 'any'}],
    }).model_dump(mode='json')
    data['policies'] = [policy]
    cached = {**data, '_allocation_match_cache': {}} if identified else data
    for shift in data['shifts'][:2]:
        if not identified:
            shift = {k: v for k, v in shift.items() if k != 'id'}
        for pid in people:
            expected = _allocation_policy_matches(data, policy, shift, pid)
            assert allocation_policy_matches(cached, policy, shift, pid) == expected
            assert allocation_policy_matches(cached, policy, shift, pid) == expected


def test_session_match_cache_rejects_another_resolved_policy_at_reused_identity():
    from packages.domain.generation import allocation_policy_matches
    data = department()
    policy, other = data['policies'][:2]
    pid = next(iter(data['people']))
    shift = data['shifts'][0]
    key = id(policy), shift['id']
    # Simulate reuse after evaluate's resolved copy dies before schedule_score resolves the same source again.
    data['_allocation_match_cache'] = {key: (other, False)}
    assert allocation_policy_matches(data, policy, shift, pid)
    assert data['_allocation_match_cache'][key][0] is policy


@pytest.mark.parametrize('scope', ['named', 'everyone'])
def test_overtime_scoring_includes_exact_offer_audience(scope):
    from packages.domain.allocation_score import allocation_score_items
    from packages.domain.conditions import active_rules
    from packages.domain.generation import allocation_overtime_allowed, allocation_settings
    data = department()
    people = list(data['people'])[:3]
    data['people'] = {pid: data['people'][pid] for pid in people}
    data['shifts'] = data['shifts'][:1]
    data['focus_ids'] = [data['shifts'][0]['id']]
    offer = data['policies'][0]
    offer['definition']['scope'] = Scope(person_ids=people[:1] if scope == 'named' else []).model_dump(mode='json')
    inactive = copy.deepcopy(data['policies'][1])
    inactive.update(status='retired', active=False)
    off = copy.deepcopy(data['policies'][2])
    off['definition']['strength'] = 'off'
    data['policies'] = active_rules([offer, inactive, off], context=data)
    # Overtime permission ignores preference strength, including historical strength='off'.
    expected = people[::2] if scope == 'named' else people
    items = allocation_score_items(data)
    assert sorted(row['person_id'] for row in items if row['label'] == 'overtime allocation') == sorted(expected)
    for pid in people:
        allowed = allocation_overtime_allowed(data, data['shifts'][0], pid)
        assert allowed == allocation_settings(data, data['shifts'][0], pid)[1] == (pid in expected)


def test_restriction_index_preserves_order_wildcards_and_safety_exceptions():
    from packages.domain.validation import blocking_restrictions
    from packages.domain.time import aware
    data = department()
    person = next(iter(data['people']))
    named = copy.deepcopy(data['policies'][32])
    wildcard = copy.deepcopy(named)
    wildcard.update(id=_id('wildcard-restriction'))
    wildcard['definition']['scope'] = Scope().model_dump(mode='json')
    other = copy.deepcopy(named)
    other.update(id=_id('other-restriction'))
    other['definition']['scope'] = Scope(person_ids=[list(data['people'])[1]]).model_dump(mode='json')
    data['policies'] = [wildcard, other, named]
    data['exceptions'] = [{'status': 'approved', 'policy_id': named['id'], 'policy_revision': named['revision'],
                           'person_id': person, 'starts_on': '2020-01-01'}]
    named['definition']['exception_allowed'] = True
    shift = data['shifts'][0]
    a, b = aware(shift['start']), aware(shift['end'])
    cached = {**data, '_restriction_index_cache': {}}
    expected = [wildcard['id'], named['id']]
    for view in (data, cached, cached):
        assert [p['id'] for p in blocking_restrictions(view, person, shift, a, b)] == expected
    # A new resolved policy pool must not inherit the previous pool's index or order.
    replaced = {**cached, 'policies': [named, wildcard]}
    assert [p['id'] for p in blocking_restrictions(replaced, person, shift, a, b)] == expected[::-1]
