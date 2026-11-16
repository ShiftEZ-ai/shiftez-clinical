"""Bounded competition proofs use complete canonical replacement checks, never another solve."""
import copy
import time
import uuid

import pytest

from packages.domain.generation import Recheck
from packages.domain.validation import evaluate, finding_observations
from packages.domain.wish_reasons import declined_wishes
from tests.test_performance_budget import python_budget
from tests.test_staff_wishes import wish


@pytest.fixture(scope='module')
def department_replacement_proofs(scale_harness):
    h = scale_harness('h')
    typ = h.configure('assignment_types', {'name': 'Scale competition place', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 0, 'maximum': 1}]}})['record']['id']
    workspace = h.shift(h.workspace(start='2026-10-01', end='2026-11-01'), typ=typ)['workspace']
    data = h.snapshot(workspace)
    source = h.seed['people'][0]
    ids = [str(uuid.UUID(int=i + 1)) for i in range(150)]
    winner = ids[1]
    profile = data['people'][source]
    data['people'] = {pid: {**profile, 'id': pid, 'display_name': f'Synthetic person {i}'}
                      for i, pid in enumerate(ids)}
    for name in ('memberships', 'eligibilities', 'grants'):
        rows = [r for r in data[name] if r['person_id'] == source]
        data[name] = [{**row, 'id': str(uuid.uuid5(uuid.NAMESPACE_URL, f'competition-scale:{name}:{pid}:{i}')),
                       'person_id': pid} for pid in ids for i, row in enumerate(rows)]
    location = next(iter(data['locations'].values()))
    locations = [str(uuid.UUID(int=1000 + i)) for i in range(50)]
    data['locations'] = {lid: {**location, 'id': lid, 'capacity': 1} for lid in locations}
    template = data['shifts'][0]
    participant = template['participants'][0]
    data['shifts'], data['wishes'] = [], []
    for day in range(4, 14):
        first, last = f'2026-10-{day:02}T08:00:00+03:00', f'2026-10-{day:02}T12:00:00+03:00'
        for clinic, lid in enumerate(locations):
            # The honoured incumbent is on each day's representative session; all 50 sessions overlap.
            sid = str(uuid.UUID(int=10000 + day * 50 + clinic))
            data['shifts'].append({**template, 'id': sid, 'location_id': lid, 'start': first, 'end': last,
                'participants': [{**participant, 'id': str(uuid.UUID(int=20000 + day)), 'shift_id': sid,
                                  'person_id': winner, 'start': first, 'end': last}] if clinic == 0 else []})
    for pid in ids:
        for day in range(4, 14):
            data['wishes'].append({'id': str(uuid.uuid5(uuid.NAMESPACE_URL, f'competition-scale:{pid}:{day}')),
                'person_id': pid, 'kind': 'prefer_on', 'first_day': f'2026-10-{day:02}',
                'last_day': f'2026-10-{day:02}', 'weight': .4 if pid == winner else .1})
    data['focus_ids'] = [s['id'] for s in data['shifts']]
    checked = evaluate(data)
    assert checked['valid'], checked['findings']
    original = copy.deepcopy(data)
    calls, original_trial = [], Recheck.trial

    def counted(self, changed, people):
        calls.append(1)
        return original_trial(self, changed, people)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(Recheck, 'trial', counted)
        started = time.process_time()
        receipts = declined_wishes(data, checked)
        elapsed = time.process_time() - started
    return data, original, receipts, calls, elapsed


def test_on_duty_replacement_proofs_have_a_department_scale_ceiling(department_replacement_proofs, request):
    data, original, receipts, calls, elapsed = department_replacement_proofs
    assert len(data['people']) == 150 and len(data['shifts']) == 500 and len(data['wishes']) == 1500
    assert len(receipts) == 1490
    assert len(calls) == 32
    reasons = [row['reasons'][0]['kind'] for row in receipts]
    assert reasons.count('competing_wishes') == 32
    assert reasons.count('tradeoff') == 1490 - 32
    assert data == original
    request.node.user_properties.append(('replacement_proof_cpu_seconds', elapsed))


@pytest.mark.cpu_budget
def test_department_replacement_proof_cpu_budget(department_replacement_proofs):
    elapsed = department_replacement_proofs[-1]
    assert elapsed < python_budget(10), elapsed


def test_competitor_replacement_preserves_the_incumbents_mandatory_minimum(h, monkeypatch):
    person, incumbent = h.seed['people'][:2]
    quota = h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
                      'metric': 'duties', 'comparison': 'minimum', 'amount': 1}, {'person_id': incumbent})
    typ = h.configure('assignment_types', {'name': 'Quota competition place', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 0, 'maximum': 1}]}})['record']['id']
    lower = wish(h, '2026-10-04', kind='prefer_on', points=1)['wish']
    wish(h, '2026-10-04', kind='prefer_on', points=4, person=incumbent, role='coordinator')
    workspace = h.shift(h.workspace(), people=[incumbent], typ=typ)['workspace']
    data = h.snapshot(workspace)
    checked = evaluate(data)
    assert checked['valid'], checked['findings']
    replacement = copy.deepcopy(data)
    replacement['shifts'][0]['participants'][0]['person_id'] = person
    refused = evaluate(replacement)
    assert any(f['code'] == 'QUOTA_BELOW_MINIMUM' and f['person_id'] == incumbent and f['rule_id'] == quota['id']
               for f in finding_observations(refused))
    original, original_trial = copy.deepcopy(data), Recheck.trial
    trials = []

    def counted(self, changed, people):
        trials.append(1)
        assert {person, incumbent} <= set(people)
        return original_trial(self, changed, people)

    monkeypatch.setattr(Recheck, 'trial', counted)
    receipt = next(row for row in declined_wishes(data, checked) if row['wish_id'] == lower['id'])
    assert receipt['reasons'][0]['kind'] == 'tradeoff'
    assert trials == [1] and data == original


def test_competitor_replacement_preserves_cover_on_overlapping_peer_sessions(h, monkeypatch):
    person, incumbent, colleague = h.seed['people'][:3]
    h.policy({'kind': 'minimum_cover', 'minimum': 2, 'aggregation': 'service_group'},
             {'service_ids': h.seed['services'][:2], 'category_ids': [h.seed['category']],
              'start_time': '09:00', 'end_time': '12:00'})
    typ = h.configure('assignment_types', {'name': 'Pooled competition place', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 0, 'maximum': 1}]}})['record']['id']
    lower = wish(h, '2026-10-04', kind='prefer_on', points=1)['wish']
    wish(h, '2026-10-04', kind='prefer_on', points=4, person=incumbent, role='coordinator')
    workspace = h.shift(h.workspace(), people=[incumbent], typ=typ)['workspace']
    workspace = h.shift(workspace, start='2026-10-04T09:00:00+03:00', end='2026-10-04T13:00:00+03:00',
                        people=[colleague], typ=typ, service=1, location=1)['workspace']
    data = h.snapshot(workspace)
    for row in data['memberships']:
        if row['person_id'] == person:
            row['category_id'] = h.seed['trainee_category']
    target, peer = sorted(data['shifts'], key=lambda s: s['start'])
    # Canonical pooled findings are owned by the lowest ID, deliberately the unchanged peer.
    for shift, number in ((target, 2), (peer, 1)):
        shift['id'] = str(uuid.UUID(int=number))
        for participant in shift['participants']:
            participant['shift_id'] = shift['id']
    data['focus_ids'] = [target['id'], peer['id']]
    checked = evaluate(data)
    assert checked['valid'], checked['findings']
    replacement = copy.deepcopy(data)
    next(s for s in replacement['shifts'] if s['id'] == target['id'])['participants'][0]['person_id'] = person
    refused = evaluate(replacement)
    assert any(f['code'] == 'SCOPED_COVERAGE_GAP' and f['shift_id'] == peer['id']
               for f in finding_observations(refused))
    original, original_trial = copy.deepcopy(data), Recheck.trial
    trials = []

    def counted(self, changed, people):
        trials.append(1)
        assert {target['id'], peer['id']} <= self.touched(changed)
        return original_trial(self, changed, people)

    monkeypatch.setattr(Recheck, 'trial', counted)
    receipt = next(row for row in declined_wishes(data, checked) if row['wish_id'] == lower['id'])
    assert receipt['reasons'][0]['kind'] == 'tradeoff'
    assert trials == [1] and data == original
