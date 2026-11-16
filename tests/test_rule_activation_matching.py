"""Rule impact uses existing immutable match caches for one snapshot, preserving complete checks."""
from collections import Counter

import pytest
from sqlalchemy import select

from apps.api.auth import authenticate
from apps.api.db import transaction
from apps.api import models as m
from apps.api.services import policies
from packages.domain import validation
from .test_backend_scale import scale_department
from .test_rule_catalogue_api import line, person, propose
from .test_rule_templates import propose as propose_policy


MATCH_CACHES = ('_policy_applies_cache', '_policy_applies_people', '_workload_matches_cache')


def compare_evaluation(monkeypatch, observations):
    original = policies.evaluate
    def compared(data, **kwargs):
        results, calls = [], []
        for index, candidate in enumerate(({key: value for key, value in data.items() if key not in MATCH_CACHES}, data)):
            counts = Counter()
            with pytest.MonkeyPatch.context() as patch:
                for name in ('_policy_applies', 'scope_applies', 'workload_matches'):
                    real = getattr(validation, name)
                    def counted(*args, _real=real, _name=name, **kwargs):
                        counts[_name] += 1
                        return _real(*args, **kwargs)
                    patch.setattr(validation, name, counted)
                if index == 0:
                    # The baseline deliberately bypasses caches; install this after
                    # wrapping the primitive so its calls remain measurable.
                    patch.setattr(validation, 'policy_applies', validation._policy_applies)
                results.append(original(candidate, **kwargs))
            calls.append(counts)
        assert results[0] == results[1], 'Every finding, coverage, quota and fairness result must remain identical'
        observations.append((dict(data), calls))
        return results[1]
    monkeypatch.setattr(policies, 'evaluate', compared)


@pytest.fixture(scope='module')
def activation_matching_measurement(scale_harness):
    h = scale_harness('h')
    _, data, _, _ = scale_department(h)
    proposal = propose(h, 'duties_per_week', [
        line(who=person(pid), numbers={'max': {'value': 10}}) for pid in data['people']])['record']
    observations = []
    with pytest.MonkeyPatch.context() as monkeypatch:
        compare_evaluation(monkeypatch, observations)
        activated = h.command('approver', 'policy.activate', {'reason': 'Independent synthetic matching review'},
                              proposal['id'], proposal['revision'])
    return activated, observations


def test_rule_activation_reuses_immutable_matching(activation_matching_measurement):
    activated, observations = activation_matching_measurement
    assert activated['record']['status'] == 'active' and len(observations) == 1
    data, (ordinary, cached) = observations[0]
    assert data['_policy_applies_people'] and all(key in data for key in MATCH_CACHES)
    # Call counts are deterministic on loaded CI workers; the full result comparison above qualifies equivalence.
    assert cached['_policy_applies'] <= ordinary['_policy_applies'] * 0.7, (ordinary, cached)
    assert cached['scope_applies'] <= ordinary['scope_applies'] * 0.7, (ordinary, cached)
    for name, ceiling in {'_policy_applies': 10_000, 'scope_applies': 22_000, 'workload_matches': 22_000}.items():
        assert cached[name] <= ceiling, (name, ordinary, cached)


def test_impact_matching_answers_do_not_cross_workspace_or_command(h, monkeypatch):
    first = h.shift(h.workspace())['workspace']
    second = h.shift(h.workspace(start='2026-10-11', end='2026-10-18'),
                     start='2026-10-11T08:00:00+03:00', end='2026-10-11T12:00:00+03:00')['workspace']
    proposal = propose_policy(h, {'kind': 'max_hours', 'hours': 1, 'window': 'week'})
    observations = []
    compare_evaluation(monkeypatch, observations)
    client = h.client('approver')
    for cycle in range(2):
        with transaction(h.app.state.engine, write=True) as db:
            actor = authenticate(db, client.cookies['rota_session'])
            if cycle:
                for assignment in db.scalars(select(m.Assignment).join(m.Shift).where(m.Shift.workspace_id == first['id'])):
                    assignment.person_id = h.seed['people'][-1]
                db.flush()
            policies.policy_impact(db, actor, db.get(m.Policy, proposal['id']), details=False)
    assert len(observations) == 4
    for key in ('_policy_applies_cache', '_workload_matches_cache'):
        caches = [data[key] for data, _ in observations]
        assert len({id(cache) for cache in caches}) == len(caches), key
    assert {data['workspace']['id'] for data, _ in observations} == {first['id'], second['id']}
