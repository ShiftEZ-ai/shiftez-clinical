"""The greedy run's partial re-check (generation.Recheck) decides exactly as a whole check would: every trial's
findings that can decide anything (errors, cover and quota shortfalls, overflow, team lead gaps) and quota rows are
compared with packages/domain/validation.evaluate on the same rota, on small random departments."""
import copy
import json
from random import Random

import pytest

from packages.domain import generation
from packages.domain.validation import evaluate, finding_observations
from tests.scheduler_departments import department

DECIDE = generation.COVERAGE_CODES | {'QUOTA_BELOW_MINIMUM', 'QUOTA_BELOW_TARGET', 'CAPACITY_OVERFLOW', 'LEAD_GAP'}


def decisions(result):
    findings = sorted(json.dumps([f['code'], f['severity'], f['shift_id'], f['person_id'], f['start'], f['end'], f['rule_id'],
                                  f['detail']], sort_keys=True, default=str)
                      for f in finding_observations(result) if f['severity'] == 'error' or f['code'] in DECIDE)
    rows = sorted(json.dumps(r, sort_keys=True, default=str) for r in (result.get('quota_report') or {}).get('rows', []))
    return findings, rows


def test_decision_comparison_ignores_explanations_but_keeps_every_constraint():
    finding = {'code': 'COVERAGE_GAP', 'severity': 'error', 'shift_id': 's', 'person_id': None,
               'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00', 'rule_id': None,
               'detail': {'required': 2, 'available': 1, 'derived_findings': [
                   {'code': 'CAPABILITY_GAP', 'severity': 'error', 'detail': {'required': 1, 'available': 0}}]}}
    plain = {'findings': [finding], 'quota_report': {'rows': [{'person_id': 'p', 'actual': 1, 'amount': 2}]}}
    explained = copy.deepcopy(plain)
    for observation in [explained['findings'][0], *explained['findings'][0]['detail']['derived_findings']]:
        observation.update(cause={'code': 'unfilled'}, fix_hints=[{'action': 'run_auto_schedule'}], cause_groups=[])
    assert decisions(explained) == decisions(plain)
    for detail in [finding['detail'], finding['detail']['derived_findings'][0]['detail'],
                   plain['quota_report']['rows'][0]]:
        key = 'actual' if 'actual' in detail else 'available'
        detail[key] += 1
        assert decisions(explained) != decisions(plain)
        detail[key] -= 1


def test_pooled_cover_keeps_unchanged_primary_on_staggered_peer_recheck(h):
    h.policy({'kind': 'minimum_cover', 'minimum': 1, 'aggregation': 'service_group'},
             {'service_ids': h.seed['services'][:2]})
    w = h.workspace()
    for start, end, service in ((8, 10, 0), (9, 12, 1), (11, 13, 0)):
        w = h.shift(w, f'2026-10-04T{start:02}:00:00+03:00', f'2026-10-04T{end:02}:00:00+03:00',
                    people=[], service=service)['workspace']
    source = h.snapshot(w)
    shifts = sorted(source['shifts'], key=lambda shift: shift['start'])
    for shift, name in zip(shifts, ('C', 'B', 'A')):
        shift['id'] = name
    source['focus_ids'] = ['A', 'B', 'C']
    whole = evaluate(source)
    trial = generation.Recheck(source).trial([shifts[0]], set())
    assert decisions(trial) == decisions(whole)


@pytest.mark.parametrize('catalogue_v2', [False, True])
def test_own_duty_precheck_keeps_limits_without_department_reports(h, monkeypatch, catalogue_v2):
    from sqlalchemy import select
    from apps.api import models as m
    from apps.api.db import transaction
    from packages.domain import fairness, quotas
    from packages.domain.conditions import active_rules
    from packages.domain.rule_catalogue_migration import after_snapshot, plan_v2
    from scripts.system_command import MAINTENANCE_ISSUER, MAINTENANCE_SUBJECT

    pid = h.seed['people'][0]
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week'}, {'person_id': pid})
    w = h.shift(h.workspace(), people=[pid])['workspace']
    w = h.shift(w, start='2026-10-04T13:00:00+03:00', end='2026-10-04T17:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    if catalogue_v2:
        with transaction(h.app.state.engine) as db:
            default_actor_id = db.scalar(select(m.Identity.id).where(m.Identity.issuer == MAINTENANCE_ISSUER,
                m.Identity.subject == MAINTENANCE_SUBJECT, m.Identity.disabled.is_(True)))
        assert default_actor_id
        plan = plan_v2(data['policies'], week_start=data['organisation']['week_start'], department_id=w['department_id'],
                       default_actor_id=default_actor_id)
        assert not plan['blocked']
        data = after_snapshot(data, plan)
    data['policies'] = active_rules(data['policies'], context=data)
    own, shift = sorted(data['shifts'], key=lambda s: s['start'])
    participant = {**copy.deepcopy(own['participants'][0]), 'id': 'trial', 'shift_id': shift['id'],
                   'start': shift['start'], 'end': shift['end']}

    def unrelated_report(*args, **kwargs):
        pytest.fail('An own-duty precheck must not calculate department quota or fairness reports')

    monkeypatch.setattr(quotas, 'evaluate_quotas', unrelated_report)
    monkeypatch.setattr(fairness, 'evaluate_goals', unrelated_report)
    assert not generation.own_duty_blocked(data, shift, participant, [], {})
    assert generation.own_duty_blocked(data, shift, participant, [own], {})
    assert shift['participants'] == []


@pytest.mark.parametrize('seed', range(6))
def test_every_trial_decides_as_a_whole_check_would(h, monkeypatch, seed):
    rng = Random(seed)
    services = h.seed['services'][:2]
    if rng.random() < 0.6:  # cover pooled across clinics: a session's people count for the sessions beside it
        h.policy({'kind': 'minimum_cover', 'minimum': rng.choice((4, 5)), 'aggregation': 'service_group'}, {'service_ids': services},
                 severity=rng.choice(('mandatory', 'advisory')))
    if rng.random() < 0.6:  # a clinic-wide limit counts people across its sessions at the same time
        h.policy({'kind': 'maximum_participants', 'maximum': rng.choice((2, 3)), 'aggregation': 'service'}, {'service_ids': services[:1]})
    if rng.random() < 0.5:
        h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'metric': 'duties',
                  'comparison': 'minimum', 'amount': 2}, {'person_id': h.seed['people'][rng.randrange(3)]})
    data, _ = department(h, 100 + seed, people=rng.randint(10, 18), days=rng.randint(4, 7), rules=rng.choice(('full', 'some')),
                         leave_rate=0.05, clinics=2)
    data['repair_conflicts'] = rng.random() < 0.3
    scope = {'conditions': {'conditions': [{'field': 'service_ids', 'op': 'any',
                                          'values': list(data['services'])[:2], 'count': 'each'}]}}
    data['policies'].append(h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week'}, scope))
    compared, original = [], generation.Recheck.trial

    def trial(self, changed, people):
        result = original(self, changed, people)
        whole = evaluate({k: v for k, v in self.data.items() if not k.startswith('_')})
        assert decisions(result) == decisions(whole)
        compared.append(1)
        return result
    monkeypatch.setattr(generation.Recheck, 'trial', trial)
    # This compares greedy checks; the composed required-cover repair has separate integration coverage.
    first = generation._generate_with_redistribution(data, 300)
    assert first['status'] in {'candidate_feasible', 'candidate_with_gaps'}, first['status']
    assert compared, 'no trial ran'
    # Same input, same proposal.
    monkeypatch.setattr(generation.Recheck, 'trial', original)
    again = generation._generate_with_redistribution(data, 300)
    assert [(a['shift_id'], a['participant']['person_id']) for a in again['additions']] == \
        [(a['shift_id'], a['participant']['person_id']) for a in first['additions']]
