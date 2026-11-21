import pytest

from packages.domain import fairness


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_cold_fairness_goals_resolve_the_snapshot_policies_once(h, monkeypatch):
    workspace = h.workspace('Fairness draft')
    route = f"/api/fairness/goals?workspace_id={workspace['id']}"
    calls = []
    original = fairness.balance_goals

    def counted(data):
        calls.append(data['workspace']['id'])
        return original(data)

    monkeypatch.setattr(fairness, 'balance_goals', counted)
    cold = h.get('coordinator', route)
    assert cold['workspace']['id'] == workspace['id']
    assert calls == [workspace['id']]
    assert h.get('coordinator', route) == cold
    assert calls == [workspace['id']]
