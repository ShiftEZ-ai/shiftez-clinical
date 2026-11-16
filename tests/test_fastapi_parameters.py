"""Request semantics used by Rota across FastAPI minor upgrades."""
import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize("body", [None, "null", "{}", '{"username":"staff","password":"synthetic","extra":true}'])
def test_login_requires_a_complete_json_model_without_unknown_fields(h, body):
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        response = client.post('/auth/login', content=body,
                               headers={'origin': h.settings.public_origin, 'content-type': 'application/json'})
    assert response.status_code == 422


def test_query_defaults_aliases_and_dependency_validation(h):
    client = h.client('operator')
    omitted = client.get('/api/audit')
    explicit = client.get('/api/audit', params={'offset': 0, 'limit': 100, 'people': 'true', 'fold': 'false'})
    assert omitted.status_code == explicit.status_code == 200
    assert omitted.json() == explicit.json()
    for query in ({'limit': 0}, {'from': 'invalid-date'}, {'filter': '{"unknown":true}'}):
        assert client.get('/api/audit', params=query).status_code == 422
