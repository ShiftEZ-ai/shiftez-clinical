"""The worker wakes on committed job notifications and keeps a bounded fallback poll."""

from types import SimpleNamespace
from threading import Event

from apps.worker.main import _wake_job_waiter


def test_job_notification_wakes_claim_loop_but_other_scopes_do_not():
    wake = Event()
    _wake_job_waiter(SimpleNamespace(payload='{"scope":"rules"}'), wake)
    assert not wake.is_set()
    _wake_job_waiter(SimpleNamespace(payload='{"scope":"jobs"}'), wake)
    assert wake.is_set()


def test_malformed_notification_is_ignored():
    wake = Event()
    _wake_job_waiter(SimpleNamespace(payload='not-json'), wake)
    assert not wake.is_set()
