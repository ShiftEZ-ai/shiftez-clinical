"""Open pages learn when someone else changes a rota, swap or request (pulse activity moves)."""
import pytest



def test_pulse_build_uses_the_complete_bundle_identity(h, tmp_path, monkeypatch):
    from apps.api import main
    from packages.web_assets import bundle_directory
    assert h.get('staff', '/api/pulse')['build'] == bundle_directory().name
    monkeypatch.setattr(main, 'WEB', tmp_path)
    for version in ('a' * 64, 'b' * 64):
        (tmp_path / 'index.html').write_text(f'<script type="module" src="/assets/{version}/app.js"></script>')
        assert h.get('staff', '/api/pulse')['build'] == version


def test_pulse_activity_moves_when_a_rota_changes(h):
    before = h.get('coordinator', '/api/pulse')['activity']
    w = h.workspace('Pulse rota')
    after_create = h.get('approver', '/api/pulse')['activity']
    h.shift(w)
    assert before < after_create < h.get('coordinator', '/api/pulse')['activity']


def test_live_updates_stream_the_pulse_to_signed_in_people_only(h):
    import json
    from fastapi.testclient import TestClient
    with h.client('coordinator').stream('GET', '/api/events?once=true') as response:
        assert response.headers['content-type'].startswith('text/event-stream')
        lines = response.iter_lines()
        assert next(lines) == ': connected'
        assert next(lines) == ''
        assert next(lines) == 'event: pulse'
        assert json.loads(next(lines).removeprefix('data: '))['activity'] >= 0
    anonymous = TestClient(h.app, base_url=h.settings.public_origin)
    assert anonymous.get('/api/events').status_code == 401


@pytest.mark.postgres
def test_pulse_and_stream_survive_absent_or_malformed_web_index(pg, tmp_path, monkeypatch):
    import json
    from apps.api import main
    monkeypatch.setattr(main, 'WEB', tmp_path)
    for index in (None, '<html>No module yet</html>'):
        if index is not None:
            (tmp_path / 'index.html').write_text(index)
        assert pg.get('staff', '/api/pulse')['build'] is None
        with pg.client('coordinator').stream('GET', '/api/events?once=true') as response:
            assert response.status_code == 200
            lines = response.iter_lines()
            assert next(lines) == ': connected'
            assert next(lines) == ''
            assert next(lines) == 'event: pulse'
            assert json.loads(next(lines).removeprefix('data: '))['build'] is None
