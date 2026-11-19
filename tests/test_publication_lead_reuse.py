"""Resolve leadership rules once for immutable ledger work; every counted lead turn is unchanged."""
from copy import deepcopy
from packages.domain import leadership
from tests.test_schedule_score_scale import department


def test_department_lead_turns_reuse_rules_and_preserve_source(monkeypatch):
    data = department()
    data['shifts'] += deepcopy(data['shifts'][:100])
    for n, shift in enumerate(data['shifts']):
        shift['id'] = str(n)
        shift['participants'][0]['team_lead'] = True
    source = deepcopy(data)
    expected = {(s['id'], p['person_id']) for s in data['shifts'] for p in s['participants'] if p.get('team_lead')}
    calls, original = [], leadership.active_rules
    def counted(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(leadership, 'active_rules', counted)
    assert leadership.lead_turn_pairs(data) == expected
    assert len(calls) == 1
    assert data == source
    for shift in data['shifts']:
        shift['participants'][0]['team_lead'] = False
    calls.clear()
    assert leadership.lead_turn_pairs(data) == set() and not calls
# Include this file in hosted supervision and team-lead qualification.
