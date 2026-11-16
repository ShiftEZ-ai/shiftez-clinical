"""The load simulator delays concurrent requests independently of the browser dispatcher."""
import pytest

from .test_browser import browser, live_server, sign_in  # noqa: F401

pytestmark = pytest.mark.browser


@pytest.fixture
def delayed_server(monkeypatch, request):
    monkeypatch.setenv("PW_API_DELAY_MS", "800")
    monkeypatch.setattr("random.random", lambda: 1.0)
    return request.getfixturevalue("live_server")


def test_api_delay_allows_save_and_parallel_reads_to_complete(browser, delayed_server):
    context, page = sign_in(browser, delayed_server, "coordinator")
    try:
        # Keep app reloads and its event stream out of this delay-dispatch oracle.
        page.goto(delayed_server + "/health/ready")
        # Start inside the state wait so its deadline includes route dispatch.
        page.wait_for_function("""() => {
            if (!window.delayProbe) {
                const state = window.delayProbe = {done: false, error: null, statuses: []};
                const completed = async response => {
                    state.statuses.push(response.status);
                    await response.arrayBuffer();
                    if (!response.ok) throw new Error(response.url + ': HTTP ' + response.status);
                };
                const reads = Array.from({length: 24}, (_, i) =>
                    fetch('/api/me?delay_probe=' + i).then(completed));
                const csrf = document.cookie.split('; ').find(c => c.startsWith('rota_csrf=')).split('=')[1];
                const save = fetch('/api/commands', {method: 'POST', headers: {
                    'Content-Type': 'application/json', 'X-CSRF-Token': csrf, 'Idempotency-Key': 'parallel-delay-save'
                }, body: JSON.stringify({kind: 'workspace.create', payload: {
                    name: 'Concurrent delayed save', starts_on: '2026-10-04', ends_on: '2026-10-11'
                }})}).then(completed);
                Promise.all([...reads, save]).then(() => state.done = true,
                    error => state.error = String(error));
            }
            return window.delayProbe.done || window.delayProbe.error;
        }""", timeout=8000)
        probe = page.evaluate("window.delayProbe")
        assert probe["error"] is None, probe
        assert probe["statuses"] == [200] * 25, probe
    finally:
        context.close()
