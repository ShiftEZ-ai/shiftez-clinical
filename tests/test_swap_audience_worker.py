"""Open offers verify a non-empty audience; membership and notifications remain durable background work."""
from .clock_support import freeze_clock
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import threading

import pytest
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.db import retry_write, transaction
from apps.api.services import swaps
from apps.worker import main as worker
from .governance_support import transition
from .test_open_swaps import audience_ready, both, payload  # noqa: F401
from .test_staff_swaps import fixed_clock, source_pair  # noqa: F401


@pytest.mark.parametrize("kind", ["exchange", "give"])
def test_committed_open_offer_is_pending_until_worker_freezes_audience(both, monkeypatch, kind):
    h = both
    pid, first, _, _ = source_pair(h, third=True)
    eligible, calls = swaps.eligible_staff, []

    def compute(*args, **kwargs):
        calls.append(True)
        return eligible(*args, **kwargs)

    monkeypatch.setattr(swaps, "eligible_staff", compute)
    row = h.command("staff", "swap.offer", payload(pid, first, kind))["swap"]
    assert row["audience_pending"] is True and calls == [True]
    assert row['delivery_status'] == 'audience_pending'
    proposer = h.get("staff", "/api/requests/all?id=" + row["id"])["item"]
    assert proposer["audience_pending"] is True
    assert h.get("staff_three", "/api/swaps")["total"] == 0
    assert h.get("staff_three", "/api/requests/all")["total"] == 0
    h.get("staff_three", "/api/swaps/" + row["id"], status=404)
    with transaction(h.app.state.engine) as db:
        event = db.scalar(select(m.Outbox).where(m.Outbox.event_key == f"swap:{row['id']}:1:offered"))
        event_id = event.id
        assert event.payload["audience_pending"] is True and event.payload["person_ids"] == []
        assert not db.scalar(select(m.SwapOfferAudience.swap_id).where(m.SwapOfferAudience.swap_id == row["id"]))
        job = db.scalar(select(m.Job).where(m.Job.kind == "swap_audience"))
        assert job.status == "queued" and job.input_snapshot["swap_id"] == row["id"]
    retry_write(h.app.state.engine, worker.dispatch_batch)
    with transaction(h.app.state.engine) as db:
        event = db.get(m.Outbox, event_id)
        assert event.state == "queued" and event.attempts == 0
        assert not db.scalar(select(m.Notification.id).where(m.Notification.outbox_id == event_id))
    audience_ready(h)
    assert calls == [True, True]
    with transaction(h.app.state.engine) as db:
        audience = list(db.scalars(select(m.SwapOfferAudience).where(m.SwapOfferAudience.swap_id == row["id"])))
        assert {entry.person_id for entry in audience} == set(h.seed["people"][1:3])
        assert all(entry.computed_at is not None for entry in audience)
        event = db.get(m.Outbox, event_id)
        assert event.payload["audience_pending"] is False
        assert set(event.payload["person_ids"]) == {entry.person_id for entry in audience}
        assert db.scalar(select(m.Job).where(m.Job.kind == "swap_audience")).result == {"audience_count": 2}
    assert h.get("staff", "/api/swaps/" + row["id"])["swap"]["audience_pending"] is False
    assert h.get("staff", "/api/requests/all?id=" + row["id"])["item"]["audience_pending"] is False
    assert h.get("staff_three", "/api/requests/all")["total"] == 1


def test_requests_etag_changes_when_audience_worker_freezes_open_offer(both):
    h = both
    pid, first, _, _ = source_pair(h, third=True)
    row = h.command("staff", "swap.offer", payload(pid, first, "give"))["swap"]
    client = h.client("staff")
    path = "/api/requests/all?type=swap"

    pending = client.get(path)
    assert pending.status_code == 200
    pending_tag = pending.headers["etag"]
    assert pending.json()["items"][0]["audience_pending"] is True

    audience_ready(h)
    frozen = client.get(path, headers={"If-None-Match": pending_tag})
    assert frozen.status_code == 200
    assert frozen.headers["etag"] != pending_tag
    item = next(item for item in frozen.json()["items"] if item["id"] == row["id"])
    assert item["audience_pending"] is False

    unchanged = client.get(path, headers={"If-None-Match": frozen.headers["etag"]})
    assert unchanged.status_code == 304


def test_outbox_payload_assignment_bumps_requests_etag_without_double_bumping(both):
    h = both
    pid, first, _, _ = source_pair(h, third=True)
    row = h.command("staff", "swap.offer", payload(pid, first, "give"))["swap"]
    client = h.client("staff")
    path = "/api/requests/all?type=swap"
    pending = client.get(path)
    tag = pending.headers["etag"]
    with transaction(h.app.state.engine, write=True) as db:
        notice = db.scalar(select(m.Outbox).where(m.Outbox.event_key == f"swap:{row['id']}:1:offered"))
        before = notice.revision
        notice.payload = {**notice.payload, "audience_pending": False, "audience_failed": True}
    changed = client.get(path, headers={"If-None-Match": tag})
    assert changed.status_code == 200 and changed.headers["etag"] != tag
    with transaction(h.app.state.engine, write=True) as db:
        notice = db.get(m.Outbox, notice.id)
        assert notice.revision == before + 1
        notice.payload = dict(notice.payload)
    assert client.get(path, headers={"If-None-Match": changed.headers["etag"]}).status_code == 304


def test_store_audience_payload_revision_bumps_once(both):
    h = both
    pid, first, _, _ = source_pair(h, third=True)
    row = h.command("staff", "swap.offer", payload(pid, first, "give"))["swap"]
    key = f"swap:{row['id']}:1:offered"
    with transaction(h.app.state.engine, write=True) as db:
        notice = db.scalar(select(m.Outbox).where(m.Outbox.event_key == key))
        before = notice.revision
        swaps.store_audience(db, {"swap_id": row["id"], "event_key": key},
                             {"person_ids": [h.seed["people"][1]]})
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Outbox.revision).where(m.Outbox.event_key == key)) == before + 1


def test_background_audience_job_is_visible_to_operators_but_not_a_rota_run(h):
    pid, first, _, _ = source_pair(h, third=True)
    h.command("staff", "swap.offer", payload(pid, first, "give"))
    with transaction(h.app.state.engine) as db:
        job = db.scalar(select(m.Job).where(m.Job.kind == "swap_audience"))
        job_id, workspace_id = job.id, job.workspace_id
    assert job_id in {job["id"] for job in h.get("coordinator", "/api/jobs")["jobs"]}
    assert job_id in {job["id"] for job in h.get("operator", "/api/jobs")["jobs"]}
    assert job_id not in {job["id"] for job in h.get("coordinator", "/api/jobs?workspace_id=" + workspace_id)["jobs"]}
    assert job_id not in {job["id"] for job in h.get("approver", "/api/jobs")["jobs"]}
    h.get("staff", "/api/jobs", status=403)
    h.get("staff", "/api/jobs/" + job_id, status=403)


def test_offer_existence_check_stops_at_first_eligible_colleague(h, monkeypatch):
    pid, first, _, _ = source_pair(h, third=True)
    calls = []
    def recipient(self, *args, **kwargs):
        calls.append(args)
        return {'eligible': True}
    monkeypatch.setattr(swaps.Eligibility, 'recipient', recipient)
    h.command('staff', 'swap.offer', payload(pid, first, 'give'))
    assert len(calls) == 1


def test_zero_audience_finishes_pending_state_without_inventing_members(h):
    pid, first, _, _ = source_pair(h)
    row = h.command("staff", "swap.offer", payload(pid, first, "exchange"))["swap"]
    # A colleague can become unavailable after the synchronous non-empty check.
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["staff_two"]).disabled = True
    audience_ready(h)
    with transaction(h.app.state.engine) as db:
        assert not db.scalar(select(m.SwapOfferAudience.swap_id).where(m.SwapOfferAudience.swap_id == row["id"]))
        job = db.scalar(select(m.Job).where(m.Job.kind == "swap_audience"))
        assert job.status == "succeeded" and job.result == {"audience_count": 0}
        event = db.scalar(select(m.Outbox).where(m.Outbox.event_key == f"swap:{row['id']}:1:offered"))
        assert event.payload["audience_pending"] is False and event.payload["person_ids"] == []
    assert h.get("staff", "/api/requests/all?id=" + row["id"])["item"]["audience_pending"] is False
    detail = h.get('staff', '/api/swaps/' + row['id'])['swap']
    assert detail['delivery_status'] == 'no_recipients' and detail['audience_count'] == 0
    assert detail['no_eligible_recipients_reason']
    assert h.get("staff_three", "/api/requests/all")["total"] == 0


def test_no_recipients_refusal_and_worker_notice_share_the_audience_reason(h):
    pid, first, _, _ = source_pair(h, third=True)
    row = h.command('staff', 'swap.offer', payload(pid, first, 'give'))['swap']
    on_leave, protected = h.seed['people'][1:]
    h.command('coordinator', 'availability.record', {'person_id': on_leave, 'kind': 'recorded_leave',
        'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00',
        'private_reason': 'Synthetic leave note'})
    h.policy({'kind': 'restriction', 'weekdays': [6], 'start_time': '08:00', 'end_time': '12:00'},
             {'person_id': protected})
    audience_ready(h)
    reason = 'Colleagues are not free at that time.'
    detail = h.get('staff', '/api/swaps/' + row['id'])
    assert detail['swap']['delivery_status'] == 'no_recipients'
    assert detail['swap']['no_eligible_recipients_reason'] == reason
    inbox = h.get('staff', '/api/notifications?kind=swap')['notifications']
    notice, = [item for item in inbox if item['title'] == 'Nobody eligible can take this duty']
    assert notice['message'] == reason

    transition(h, row, 'withdraw')
    refusal = h.command('staff', 'swap.offer', payload(pid, first, 'give'), status=422)['error']
    assert refusal['code'] == 'SWAP_NO_RECIPIENTS'
    assert refusal['message'].endswith(reason)
    assert refusal['details']['no_eligible_recipients_reason'] == refusal['message']


def test_stored_audience_reason_from_before_the_allowlist_reads_neutral_everywhere(h):
    pid, first, _, _ = source_pair(h)
    row = h.command('staff', 'swap.offer', payload(pid, first, 'exchange'))['swap']
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed['identities']['staff_two']).disabled = True
    audience_ready(h)
    # Offers still open at deploy stored the validator's own sentence after the title.
    with transaction(h.app.state.engine, write=True) as db:
        notice = db.scalar(select(m.Outbox).where(m.Outbox.event_key == f"swap:{row['id']}:1:offered"))
        notice.payload = {**notice.payload, 'no_eligible_recipients_reason':
                          'Nobody eligible can take this duty. The person taking this duty has recorded leave at that time.'}
    reason = swaps.UNKNOWN_AUDIENCE_REASON
    assert h.get('staff', '/api/requests/all?id=' + row['id'])['item']['no_eligible_recipients_reason'] == reason
    home, = [item for item in h.get('staff', '/api/home')['my_requests'] if item['id'] == row['id']]
    assert home['no_eligible_recipients_reason'] == reason
    assert h.get('staff', '/api/swaps/' + row['id'])['swap']['no_eligible_recipients_reason'] == reason


def test_failed_audience_job_clears_pending_without_notifying_or_exposing_error_text(both, monkeypatch):
    h = both
    pid, first, _, _ = source_pair(h, third=True)
    row = h.command("staff", "swap.offer", payload(pid, first, "give"))["swap"]

    def fail(*args, **kwargs):
        raise RuntimeError("Private computation details must never enter a staff response")

    monkeypatch.setattr(swaps, "eligible_staff", fail)
    audience_ready(h)
    with transaction(h.app.state.engine) as db:
        job = db.scalar(select(m.Job).where(m.Job.kind == "swap_audience"))
        assert job.status == "failed" and job.error_code == "RuntimeError"
        assert not db.scalar(select(m.SwapOfferAudience.swap_id).where(m.SwapOfferAudience.swap_id == row["id"]))
        event = db.scalar(select(m.Outbox).where(m.Outbox.event_key == f"swap:{row['id']}:1:offered"))
        assert event.state == "superseded" and event.payload["person_ids"] == []
        assert event.payload["audience_pending"] is False and event.payload["audience_failed"] is True
        assert not db.scalar(select(m.Notification.id).where(m.Notification.outbox_id == event.id))
    detail = h.get("staff", "/api/swaps/" + row["id"])["swap"]
    request = h.get("staff", "/api/requests/all?id=" + row["id"])["item"]
    for result in (detail, request):
        assert result["audience_pending"] is False and result["audience_failed"] is True
        assert "Private computation" not in str(result) and "RuntimeError" not in str(result)
    assert h.get("staff_three", "/api/requests/all")["total"] == 0
    observed = h.get("operator", "/api/operations?state=superseded")
    assert observed["total"] == observed["metrics"]["outbox_states"]["superseded"] == 1
    assert [item["id"] for item in observed["outbox"]] == [event.id]
    assert "payload" not in observed["outbox"][0]
    h.get("staff", "/api/operations?state=superseded", status=403)


@pytest.mark.parametrize("terminal", ["cancelled", "lease_exhausted", "access_revoked"])
def test_terminal_audience_jobs_clear_pending_without_computing(h, monkeypatch, terminal):
    pid, first, _, _ = source_pair(h, third=True)
    row = h.command("staff", "swap.offer", payload(pid, first, "give"))["swap"]
    with transaction(h.app.state.engine) as db:
        job = db.scalar(select(m.Job).where(m.Job.kind == "swap_audience"))
        job_id, revision = job.id, job.revision
    if terminal == "cancelled":
        reason = {"reason": "Stop the queued audience search"}
        assert h.command("staff", "job.cancel", reason, job_id, revision, status=403)["error"]["code"] == "FORBIDDEN"
        assert h.command("coordinator", "job.cancel", reason, job_id, revision)["job"]["status"] == "cancelled"
    else:
        with transaction(h.app.state.engine, write=True) as db:
            job = db.get(m.Job, job_id)
            if terminal == "lease_exhausted":
                job.status, job.attempts = "running", 3
                job.started_at = job.heartbeat_at = m.now() - timedelta(seconds=worker.JOB_LEASE_SECONDS + 1)
            else:
                db.get(m.Identity, h.seed["identities"]["staff"]).disabled = True

    def no_eligibility(*args, **kwargs):
        pytest.fail("A terminal audience job must not perform eligibility work")

    monkeypatch.setattr(swaps, "eligible_staff", no_eligibility)
    audience_ready(h)
    with transaction(h.app.state.engine) as db:
        job = db.get(m.Job, job_id)
        assert job.status == ("cancelled" if terminal == "cancelled" else "failed") and job.completed_at is not None
        if terminal != "cancelled":
            assert job.error_code == ("WORKER_LEASE_EXPIRED" if terminal == "lease_exhausted" else "ACCESS_REVOKED")
        assert not db.scalar(select(m.SwapOfferAudience.swap_id).where(m.SwapOfferAudience.swap_id == row["id"]))
        event = db.scalar(select(m.Outbox).where(m.Outbox.event_key == f"swap:{row['id']}:1:offered"))
        assert event.state == "superseded" and event.payload["audience_pending"] is False
        assert event.payload["audience_failed"] is True
        assert not db.scalar(select(m.Notification.id).where(m.Notification.outbox_id == event.id))
    detail = h.get("coordinator", "/api/swaps/" + row["id"])["swap"]
    assert detail["audience_pending"] is False and detail["audience_failed"] is True


@pytest.mark.postgres
def test_audience_job_recovers_expired_lease_and_repeated_completion_is_harmless(pg, monkeypatch):
    h = pg
    pid, first, _, _ = source_pair(h, third=True)
    row = h.command("staff", "swap.offer", payload(pid, first, "give"))["swap"]
    with transaction(h.app.state.engine, write=True) as db:
        abandoned = worker.claim_job(db)
        assert abandoned["kind"] == "swap_audience"
        db.get(m.Job, abandoned["id"]).heartbeat_at = m.now() - timedelta(seconds=worker.JOB_LEASE_SECONDS + 1)
    eligible, calls = swaps.eligible_staff, []

    def compute(*args, **kwargs):
        calls.append(True)
        return eligible(*args, **kwargs)

    monkeypatch.setattr(swaps, "eligible_staff", compute)
    audience_ready(h)
    assert calls == [True]
    with transaction(h.app.state.engine, write=True) as db:
        job = db.get(m.Job, abandoned["id"])
        assert job.status == "succeeded" and job.attempts == 2
        worker.finish_job(db, abandoned, {"person_ids": [row["proposer_person_id"]]}, None)
        assert job.result == {"audience_count": 2}
        assert db.scalar(select(func.count()).select_from(m.SwapOfferAudience).where(
            m.SwapOfferAudience.swap_id == row["id"])) == 2
        event_id = db.scalar(select(m.Outbox.id).where(m.Outbox.event_key == f"swap:{row['id']}:1:offered"))
        notifications = list(db.scalars(select(m.Notification).where(m.Notification.outbox_id == event_id)))
        assert len({notice.identity_id for notice in notifications}) == len(notifications)
    assert not worker.process_one(h.app.state.engine)
    assert calls == [True]


@pytest.mark.parametrize("closed", ["withdrawn", "expired"])
def test_closed_offer_audience_job_skips_computation_and_initial_notifications(h, monkeypatch, closed):
    pid, first, _, _ = source_pair(h, third=True)
    row = h.command("staff", "swap.offer", payload(pid, first, "give"))["swap"]
    if closed == "withdrawn":
        transition(h, row, "withdraw")
    else:
        moment = m.now() + timedelta(days=2)
        freeze_clock(monkeypatch, lambda: moment)

    def no_eligibility(*args, **kwargs):
        pytest.fail("A closed offer must not compute or notify a new audience")

    monkeypatch.setattr(swaps, "eligible_staff", no_eligibility)
    audience_ready(h)
    with transaction(h.app.state.engine) as db:
        assert not db.scalar(select(m.SwapOfferAudience.swap_id).where(m.SwapOfferAudience.swap_id == row["id"]))
        event = db.scalar(select(m.Outbox).where(m.Outbox.event_key == f"swap:{row['id']}:1:offered"))
        assert event.state == "superseded" and event.payload["audience_pending"] is False
        assert not db.scalar(select(m.Notification.id).where(m.Notification.outbox_id == event.id))
        assert db.scalar(select(m.Job).where(m.Job.kind == "swap_audience")).status == "succeeded"


@pytest.mark.postgres
def test_audience_computation_allows_withdrawal_and_discards_stale_result(pg, monkeypatch):
    h = pg
    pid, first, _, _ = source_pair(h, third=True)
    row = h.command("staff", "swap.offer", payload(pid, first, "give"))["swap"]
    entered, release = threading.Event(), threading.Event()

    def compute(*args, **kwargs):
        entered.set()
        assert release.wait(20), "Audience computation was never released"
        return set(h.seed["people"][1:3])

    def writer():
        return transition(h, row, "withdraw")["swap"]["status"]

    monkeypatch.setattr(swaps, "eligible_staff", compute)
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(worker.process_one, h.app.state.engine)
        try:
            assert entered.wait(15), "Worker never started audience computation"
            assert pool.submit(writer).result(timeout=5) == "withdrawn"
        finally:
            release.set()
        assert running.result(timeout=20)
    audience_ready(h)
    with transaction(h.app.state.engine) as db:
        assert not db.scalar(select(m.SwapOfferAudience.swap_id).where(m.SwapOfferAudience.swap_id == row["id"]))
        event = db.scalar(select(m.Outbox).where(m.Outbox.event_key == f"swap:{row['id']}:1:offered"))
        assert event.state == "superseded" and event.payload["audience_pending"] is False
        assert not db.scalar(select(m.Notification.id).where(m.Notification.outbox_id == event.id))
