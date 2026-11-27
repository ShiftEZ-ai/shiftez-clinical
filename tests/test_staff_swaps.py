from .clock_support import freeze_clock
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import copy
import threading
import uuid
from itertools import count
from contextlib import contextmanager
import pytest
from sqlalchemy import select, func, text, event
from sqlalchemy.exc import DBAPIError, IntegrityError
from apps.api import models as m
from apps.api import auth
from apps.api.db import transaction
from tests.outbox_support import process_one_after_undo as process_one
from packages.contracts.schemas import AssignmentSource, ROLE_NAMES
from packages.domain.time import aware
from .governance_support import identity, swap_automation, transition


@contextmanager
def ordered_clock(monkeypatch):
    # These are explicitly synthetic future duties, reproducible after October 2026 too.
    value = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)
    freeze_clock(monkeypatch, value)
    monkeypatch.setattr(auth, "now", lambda: value)
    ticks = count()
    def stamp(mapper, connection, row):
        row.created_at = value + timedelta(microseconds=next(ticks))

    # Journey histories need ordered event dates while request/provenance clocks stay frozen.
    for model in (m.AuditEvent, m.SwapEvent):
        event.listen(model, 'before_insert', stamp)
    try:
        yield
    finally:
        for model in (m.AuditEvent, m.SwapEvent):
            event.remove(model, 'before_insert', stamp)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    with ordered_clock(monkeypatch):
        yield


@pytest.fixture(autouse=True)
def manual_swap_workflow(request):
    """These regressions exercise the coordinator workflow; automation has its own default-on suite."""
    h = request.getfixturevalue("pg" if "pg" in request.fixturenames else "h")
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed["department_id"])
        department.swap_auto_prepare = department.swap_auto_publish = False


def source_pair(h, third=False):
    identity(h, "staff_two", ["staff"], h.seed["people"][1])
    identity(h, "staff_three", ["staff"], h.seed["people"][2])
    w = h.shift(h.workspace())["workspace"]
    w = h.shift(w, "2026-10-05T08:00:00+03:00", "2026-10-05T12:00:00+03:00", people=[h.seed["people"][1]])["workspace"]
    if third:
        w = h.shift(w, "2026-10-06T08:00:00+03:00", "2026-10-06T12:00:00+03:00", people=[h.seed["people"][2]])["workspace"]
    pub = h.publish(w)
    source = h.get("coordinator", "/api/publications/" + pub["publication_id"])["snapshot"]
    first, second = [shift["participants"][0]["id"] for shift in source["shifts"][:2]]
    return pub["publication_id"], first, second, source


def offered(h, pid, first, key=None, kind="exchange"):
    return h.command("staff", "swap.offer", {"publication_id": pid, "assignment_id": first,
        "recipient_person_id": h.seed["people"][1], "expires_at": (m.now()+timedelta(days=1)).isoformat(),
        "reason": "Shared operational exchange request; no private medical details",
        **({"kind": kind} if kind != "exchange" else {})}, key=key)["swap"]


def swap_settings(h, **settings):
    me = h.get('admin', '/api/me')
    org, dept = me['organisation'], me['department']
    return h.command('admin', 'department.settings', {'department_name': dept['name'],
        'department_revision': dept['revision'], 'organisation_name': org['name'],
        'timezone': org['timezone'], 'week_start': org['week_start'], 'reason': 'Synthetic swap configuration',
        **settings}, dept['id'], org['revision'])


def accepted(h, kind="exchange", third=False):
    swap_automation(h, prepare=False, publish=False)
    pid, first, second, source = source_pair(h, third=third)
    row = offered(h, pid, first, kind=kind)
    row = (transition(h, row, "accept", "staff_two") if kind == "give" else
           transition(h, row, "counteroffer", "staff_two", {"assignment_id": second, "reason": "I offer this exact duty"}))["swap"]
    row = transition(h, row, "confirm")["swap"]
    return row, source


def prepared(h, kind="exchange"):
    row, source = accepted(h, kind)
    result = transition(h, row, "prepare", "coordinator")
    return result["swap"], result["workspace"], source


def test_swap_prepare_captures_draft_baseline(h):
    _, workspace, _ = prepared(h)
    assert h.detail(workspace)["workspace"]["baseline"]["snapshot_hash"]


def test_complete_two_sided_consent_review_and_atomic_publication(h):
    row, source = accepted(h)
    assert row["status"] == "accepted"
    assert h.get("coordinator", "/api/publications/" + row["publication_id"])["snapshot"] == source
    result = transition(h, row, "prepare", "coordinator")
    row, w = result["swap"], result["workspace"]
    assert row["status"] == "reviewing" and w["status"] == "draft"
    assert h.get("coordinator", "/api/publications/" + row["publication_id"])["publication"]["current"]
    new = h.publish(w)
    final = h.get("staff", "/api/swaps/" + row["id"])
    assert final["swap"]["status"] == "published" and final["swap"]["result_publication_id"] == new["publication_id"]
    after = h.get("coordinator", "/api/publications/" + new["publication_id"])["snapshot"]
    assert [s["participants"][0]["person_id"] for s in after["shifts"]] == [h.seed["people"][1], h.seed["people"][0]]
    for shift in after["shifts"]:
        participant = shift["participants"][0]
        assert participant["source"] == "swap" and participant["source_run_id"] is None
        assert participant["source_actor_id"] == h.seed["identities"]["coordinator"]
        assert aware(participant["source_at"]) == m.now()
    old = h.get("coordinator", "/api/publications/" + row["publication_id"])
    assert old["snapshot"] == source and not old["publication"]["current"]
    assert [e["action"] for e in final["events"]] == ["offered", "counteroffer", "confirm", "prepare", "published"]
    report = h.get("coordinator", "/api/reports/" + new["publication_id"])
    assert sorted(r["scheduled_hours"] for r in report["rows"]) == [4, 4]


@pytest.mark.postgres
def test_give_away_transfers_only_offered_duty_through_review_and_publication(pg):
    h = pg
    pid, first, second, source = source_pair(h, third=True)
    row = offered(h, pid, first, kind="give")
    assert row["kind"] == "give" and row["second_assignment_id"] is None
    assert h.get("staff", "/api/swaps")["swaps"][0]["kind"] == "give"
    for who in ("staff", "staff_two"):
        for items in (h.get(who, "/api/requests/all")["items"], h.get(who, "/api/home")["my_requests"]):
            item = next(item for item in items if item["id"] == row["id"])
            assert item["kind"] == "give" and item["type"] == "swap"
    row = transition(h, row, "accept", "staff_two")["swap"]
    assert row["status"] == "counteroffered" and row["confirmed_at"] is None
    assert row["recipient_id"] == h.seed["identities"]["staff_two"]
    assert row["counteroffered_at"] is not None and row["counteroffered_assignment"] is None
    assert transition(h, row, "prepare", "coordinator", status=409)["error"]["code"] == "SWAP_STATE"
    row = transition(h, row, "confirm")["swap"]
    result = transition(h, row, "prepare", "coordinator")
    row, w = result["swap"], result["workspace"]
    assert row["status"] == "reviewing" and result["validation"]["valid"]
    assert h.get("coordinator", "/api/publications/" + pid)["snapshot"] == source
    new = h.publish(w)
    final = h.get("staff_two", "/api/swaps/" + row["id"])
    assert final["swap"]["kind"] == "give" and final["swap"]["status"] == "published"
    assert final["swap"]["second_assignment_id"] is None
    assert final["swap"]["result_publication_id"] == new["publication_id"]
    after = h.get("coordinator", "/api/publications/" + new["publication_id"])["snapshot"]
    assert [s["participants"][0]["person_id"] for s in after["shifts"]] == [
        h.seed["people"][1], h.seed["people"][1], h.seed["people"][2]]
    assert [s["start"] for s in after["shifts"]] == [s["start"] for s in source["shifts"]]
    transferred = after["shifts"][0]["participants"][0]
    assert transferred["source"] == "swap" and transferred["source_run_id"] is None
    assert transferred["source_actor_id"] == h.seed["identities"]["coordinator"]
    assert aware(transferred["source_at"]) == m.now()
    for original, retained in zip(source["shifts"][1:], after["shifts"][1:]):
        assert {key: retained["participants"][0][key] for key in AssignmentSource.model_fields} == {
            key: original["participants"][0][key] for key in AssignmentSource.model_fields}
    old = h.get("coordinator", "/api/publications/" + pid)
    assert old["snapshot"] == source and not old["publication"]["current"]
    assert [e["action"] for e in final["events"]] == ["offered", "accept", "confirm", "prepare", "published"]
    with pytest.raises(DBAPIError, match="immutable authority or consent material"), transaction(h.app.state.engine, write=True) as db:
        db.execute(text("UPDATE swap_requests SET kind='exchange' WHERE id=:id"), {"id": row["id"]})
    for _ in range(30):
        if not process_one(h.app.state.engine):
            break
    notices = {who: [n for n in h.get(who, "/api/notifications")["notifications"]
                     if n["title"].startswith("Give-away")] for who in ("staff", "staff_two", "staff_three")}
    assert {n["title"] for n in notices["staff"]} == {
        "Give-away accepted by recipient", "Give-away rota change in review", "Give-away published"}
    assert {n["title"] for n in notices["staff_two"]} == {
        "Give-away offered", "Give-away agreed", "Give-away rota change in review", "Give-away published"}
    assert not notices["staff_three"]
    acceptance = next(n for n in notices["staff"] if n["title"] == "Give-away accepted by recipient")
    assert "still needed" in acceptance["message"] and "exact pair" not in acceptance["message"]
    assert second not in str(notices) and "private medical" not in str(notices)


def test_give_away_acceptance_checks_recipient_revision_and_original_proposer(h):
    pid, first, second, _ = source_pair(h)
    payload = {"publication_id": pid, "assignment_id": first, "recipient_person_id": h.seed["people"][1],
        "expires_at": (m.now()+timedelta(days=1)).isoformat(), "reason": "Swap kind must be a known contract value"}
    for kind in ("other", "give away", None):
        h.command("staff", "swap.offer", {**payload, "kind": kind}, status=422)
    h.get("staff", "/api/swaps/options?kind=other", status=422)
    assert not h.get("staff", "/api/swaps")["swaps"]
    exchange = offered(h, pid, first)
    assert exchange["kind"] == "exchange"
    assert h.get("staff", "/api/swaps/options")["kind"] == "exchange"
    assert transition(h, exchange, "accept", "staff_two", status=409)["error"]["code"] == "SWAP_KIND"
    transition(h, exchange, "withdraw")
    row = offered(h, pid, first, kind="give")
    assert transition(h, row, "confirm", status=409)["error"]["code"] == "SWAP_STATE"
    assert transition(h, row, "counteroffer", "staff_two", {"assignment_id": second,
        "reason": "Give-aways do not return a duty"}, status=409)["error"]["code"] == "SWAP_KIND"
    assert transition(h, row, "accept", status=403)["error"]["code"] == "FORBIDDEN"
    transition(h, row, "accept", "staff_three", status=404)
    transition(h, row, "accept", "coordinator", status=403)
    transition(h, row, "accept", "staff_two", {"reason": "x" * 501}, status=422)
    transition(h, row, "accept", "staff_two", {"reason": "Not a return", "assignment_id": second}, status=422)
    before = row
    key = str(uuid.uuid4())
    row = transition(h, before, "accept", "staff_two", key=key)["swap"]
    assert transition(h, before, "accept", "staff_two", key=key)["swap"] == row
    assert transition(h, before, "accept", "staff_two", status=409)["error"]["code"] == "STALE_REVISION"
    assert transition(h, row, "accept", "staff_two", status=409)["error"]["code"] == "SWAP_STATE"
    assert transition(h, row, "confirm", "staff_two", status=403)["error"]["code"] == "FORBIDDEN"
    identity(h, "proposer_alias", ["staff"], h.seed["people"][0])
    assert transition(h, row, "confirm", "proposer_alias", status=403)["error"]["code"] == "FORBIDDEN"
    row = transition(h, row, "confirm")["swap"]
    assert row["status"] == "accepted" and row["second_assignment_id"] is None
    assert h.command("staff", "swap.offer", {"kind": "give", "publication_id": pid, "assignment_id": first,
        "recipient_person_id": h.seed["people"][0], "expires_at": (m.now()+timedelta(days=1)).isoformat(),
        "reason": "Self acceptance is not a transfer"}, status=422)["error"]["code"] == "INVALID_SWAP"


@pytest.mark.parametrize("action,actor,stage", [("accept", "staff_two", "offered"),
    ("confirm", "staff", "counteroffered"), ("withdraw", "staff", "accepted")])
def test_give_away_undo_preserves_kind_without_inventing_a_return_duty(h, action, actor, stage):
    pid, first, _, _ = source_pair(h)
    row = offered(h, pid, first, kind="give")
    row = transition(h, row, "accept", "staff_two")["swap"]
    if action != "accept":
        row = transition(h, row, "confirm")["swap"]
    if action == "withdraw":
        row = transition(h, row, "withdraw")["swap"]
    result = transition(h, row, "undo", actor)
    again = result["swap"]
    assert result["undone"]["status"] == "withdrawn" and result["undone"]["kind"] == "give"
    assert again["id"] != row["id"] and again["reopened_from_id"] == row["id"]
    assert again["kind"] == "give" and again["status"] == stage
    assert again["first_assignment_id"] == first and again["second_assignment_id"] is None
    assert again["counteroffered_assignment"] is None
    assert (again["recipient_id"] is None) == (stage == "offered")
    assert (again["counteroffered_at"] is None) == (stage == "offered")
    assert (again["confirmed_at"] is not None) == (stage == "accepted")
    if stage == "offered":
        again = transition(h, again, "accept", "staff_two")["swap"]
    if stage != "accepted":
        again = transition(h, again, "confirm")["swap"]
    assert transition(h, again, "prepare", "coordinator")["validation"]["valid"]


def test_swap_readiness_tracks_active_binding_without_relaxing_personal_access(h):
    pid, first, _, _ = source_pair(h)
    assert h.get('staff', '/api/me')['staff_swap_access'] is True
    person = next(row for row in h.get('admin', '/api/catalogues')['people'] if row['id'] == h.seed['people'][0])
    h.command('admin', 'catalogue.bulk_update', {'catalogue': 'people',
        'items': [{'id': person['id'], 'revision': person['revision']}], 'active': False,
        'reason': 'Synthetic inactive staff binding'})
    me = h.get('staff', '/api/me')
    assert me['person_id'] == person['id'] and me['roles'] == ['staff'] and me['staff_swap_access'] is False
    for path in ('/api/swaps', '/api/swaps/options'):
        assert h.get('staff', path, status=403)['error']['code'] == 'STAFF_BINDING_REQUIRED'
    assert h.command('staff', 'swap.offer', {'publication_id': pid, 'assignment_id': first,
        'recipient_person_id': h.seed['people'][1], 'expires_at': (m.now() + timedelta(days=1)).isoformat(),
        'reason': 'Synthetic inactive binding must not consent'}, status=403)['error']['code'] == 'STAFF_BINDING_REQUIRED'
    for role in ('coordinator', 'approver'):
        assert h.get(role, '/api/me')['staff_swap_access'] is False
        assert h.get(role, '/api/swaps')['swaps'] == []
    assert h.get('staff', '/api/reports/' + pid)['rows'][0]['person_id'] == person['id']


def test_personal_options_and_swap_details_do_not_expose_unrelated_people_or_custom_attributes(h):
    pid, first, second, _ = source_pair(h)
    options = h.get("staff", "/api/swaps/options")
    assert {a["person_id"] for a in options["assignments"]} == {h.seed["people"][0]}
    assert {r["id"] for r in options["recipients"]} == set(h.seed["people"][1:])
    assert "private_reason" not in str(options) and "type_snapshot" not in str(options)
    row = offered(h, pid, first)
    assert h.get("staff_two", "/api/swaps/" + row["id"])["swap"]["offered_assignment"]["assignment_id"] == first
    assert not h.get("staff_three", "/api/swaps")["swaps"]
    h.get("staff_three", "/api/swaps/" + row["id"], status=404)
    h.get("auditor", "/api/swaps", status=403)
    h.get("staff", "/api/review-routes", status=403)
    h.get("coordinator", "/api/swaps/options", status=403)


@pytest.mark.parametrize('days', [1, 7, 14, 2_147_483_647])
@pytest.mark.parametrize('kind', ['exchange', 'give'])
@pytest.mark.parametrize('open_offer', [False, True], ids=['named', 'open'])
def test_configured_window_bounds_new_offers_without_date_overflow(h, days, kind, open_offer):
    pid, first, _, source = source_pair(h)
    swap_settings(h, swap_offer_window_days=days)
    start = datetime.fromisoformat(source['shifts'][0]['participants'][0]['start'])
    expiry = min(m.now() + timedelta(days=min(days, 30)), start)
    payload = {'kind': kind, 'publication_id': pid, 'assignment_id': first,
               'recipient_person_id': None if open_offer else h.seed['people'][1],
               'expires_at': (expiry + timedelta(microseconds=1)).isoformat(), 'reason': 'Window boundary'}
    assert h.command('staff', 'swap.offer', payload, status=422)['error']['code'] == 'SWAP_EXPIRY'
    row = h.command('staff', 'swap.offer', {**payload, 'expires_at': expiry.isoformat()})['swap']
    assert datetime.fromisoformat(row['expires_at']) == expiry
    swap_settings(h, swap_offer_window_days=1)
    assert h.get('staff', '/api/swaps/' + row['id'])['swap']['effective_status'] == 'offered'


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('kind', ['exchange', 'give'])
@pytest.mark.parametrize('open_offer', [False, True], ids=['named', 'open'])
def test_configured_cap_enforces_new_and_reopened_offers_and_frees_closed_capacity(request, database, kind, open_offer):
    h = request.getfixturevalue(database)
    pid, first, _, _ = source_pair(h, third=True)
    swap_settings(h, swap_open_offer_cap=1)
    row = h.command('staff', 'swap.offer', {'kind': kind, 'publication_id': pid, 'assignment_id': first,
        'recipient_person_id': None if open_offer else h.seed['people'][1],
        'expires_at': (m.now() + timedelta(days=1)).isoformat(), 'reason': 'Configured backlog capacity'})['swap']
    payload = {'kind': kind, 'publication_id': pid, 'assignment_id': first, 'recipient_person_id': h.seed['people'][2],
               'expires_at': (m.now() + timedelta(days=1)).isoformat(), 'reason': 'Another invitation'}
    assert h.command('staff', 'swap.offer', payload, status=409)['error']['code'] == 'SWAP_LIMIT'
    withdrawn = transition(h, row, 'withdraw')['swap']
    other = h.command('staff', 'swap.offer', payload)['swap']
    assert transition(h, withdrawn, 'undo', status=409)['error']['code'] == 'SWAP_LIMIT'
    transition(h, other, 'withdraw')
    reopened = transition(h, withdrawn, 'undo')['swap']
    assert reopened['effective_status'] == 'offered'


def test_cap_can_exceed_old_limit_and_is_department_scoped(h):
    pid, first, _, _ = source_pair(h)
    colleagues = extra_staff(h, 25)
    # Seed existing invitations; the new offer below still uses full eligibility validation.
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all([m.SwapRequest(department_id=h.seed['department_id'], publication_id=pid,
            proposer_id=h.seed['identities']['staff'], proposer_person_id=h.seed['people'][0],
            recipient_person_id=person_id, first_assignment_id=first, expires_at=m.now() + timedelta(days=1),
            reason='Separate live colleague invitation') for person_id in colleagues])
    assert h.command('staff', 'swap.offer', {'publication_id': pid, 'assignment_id': first,
        'recipient_person_id': h.seed['people'][1], 'expires_at': (m.now() + timedelta(days=1)).isoformat(),
        'reason': 'Default cap reached'}, status=409)['error']['code'] == 'SWAP_LIMIT'
    swap_settings(h, swap_open_offer_cap=26)
    assert offered(h, pid, first)['effective_status'] == 'offered'
    from apps.api.services.swaps import require_offer_capacity
    with transaction(h.app.state.engine, write=True) as db:
        other = m.Department(organisation_id=db.get(m.Department, h.seed['department_id']).organisation_id,
                             name='Another department', swap_open_offer_cap=1)
        db.add(other); db.flush()
        # These 26 invitations consume none of the same sign-in's capacity in a different department.
        require_offer_capacity(db, other.id, h.seed['identities']['staff'])


def test_undo_replacing_open_consent_does_not_need_capacity_after_cap_is_lowered(h):
    row, _ = accepted(h, third=True)
    h.command('staff', 'swap.offer', {'publication_id': row['publication_id'], 'assignment_id': row['first_assignment_id'],
        'recipient_person_id': h.seed['people'][2], 'expires_at': (m.now() + timedelta(days=1)).isoformat(),
        'reason': 'Another invitation before lowering the cap'})
    swap_settings(h, swap_open_offer_cap=1)
    assert transition(h, row, 'undo')['swap']['effective_status'] == 'counteroffered'


def test_expired_offers_free_capacity(h, monkeypatch):
    pid, first, _, _ = source_pair(h)
    swap_settings(h, swap_open_offer_cap=1)
    offered(h, pid, first)
    later = m.now() + timedelta(days=1, seconds=1)
    freeze_clock(monkeypatch, lambda: later)
    assert offered(h, pid, first)['effective_status'] == 'offered'


def test_recipient_selects_own_duty_and_proposer_must_confirm_exact_pair(h):
    pid, first, second, _ = source_pair(h)
    row = offered(h, pid, first)
    assert transition(h, row, "prepare", "coordinator", status=409)["error"]["code"] == "SWAP_STATE"
    assert transition(h, row, "confirm", status=409)["error"]["code"] == "SWAP_STATE"
    assert transition(h, row, "counteroffer", "staff", {"assignment_id": second, "reason": "Cannot impersonate recipient"}, status=403)["error"]["code"] == "FORBIDDEN"
    assert transition(h, row, "counteroffer", "staff_two", {"assignment_id": first, "reason": "Not my assignment"}, status=404)["error"]["code"] == "SWAP_ASSIGNMENT_NOT_FOUND"
    row = transition(h, row, "counteroffer", "staff_two", {"assignment_id": second, "reason": "Exact counteroffer"})["swap"]
    assert transition(h, row, "confirm", "staff_two", status=403)["error"]["code"] == "FORBIDDEN"
    assert transition(h, row, "confirm")["swap"]["status"] == "accepted"


@pytest.mark.postgres
def test_duplicate_live_duty_offer_is_refused_and_closed_offer_can_be_replaced(pg):
    h = pg
    pid, first, _, _ = source_pair(h, third=True)
    row = offered(h, pid, first)
    payload = {"publication_id": pid, "assignment_id": first, "recipient_person_id": h.seed["people"][1],
               "expires_at": (m.now() + timedelta(days=1)).isoformat(), "reason": "Duplicate invitation"}
    with transaction(h.app.state.engine) as db:
        before = [db.scalar(select(func.count()).select_from(model)) for model in (m.SwapRequest, m.SwapEvent, m.Outbox)]
    refused = h.command("staff", "swap.offer", payload, status=409)["error"]
    assert refused["code"] == "SWAP_DUPLICATE" and refused["message"] == "This duty is already offered to this colleague"
    # Person and duty are the key, even if the same person has another staff sign-in.
    identity(h, "staff_alias", ["staff"], h.seed["people"][0])
    assert h.command("staff_alias", "swap.offer", payload, status=409)["error"]["code"] == "SWAP_DUPLICATE"
    with transaction(h.app.state.engine) as db:
        assert [db.scalar(select(func.count()).select_from(model)) for model in (m.SwapRequest, m.SwapEvent, m.Outbox)] == before
    third = h.command("staff", "swap.offer", {**payload, "recipient_person_id": h.seed["people"][2]})["swap"]
    assert third["recipient_person_id"] != row["recipient_person_id"]
    withdrawn = transition(h, row, "withdraw")["swap"]
    assert offered(h, pid, first)["id"] != row["id"]
    assert transition(h, withdrawn, "undo", status=409)["error"]["code"] == "SWAP_DUPLICATE"
    assert h.get("staff", "/api/swaps/" + withdrawn["id"])["swap"]["status"] == "withdrawn"


@pytest.mark.postgres
def test_concurrent_duplicate_offers_save_only_one_request(pg):
    h = pg
    pid, first, _, _ = source_pair(h)
    client, barrier = h.client("staff"), threading.Barrier(2)
    def attempt(_):
        barrier.wait()
        return client.post("/api/commands", headers={"idempotency-key": str(uuid.uuid4())}, json={
            "kind": "swap.offer", "payload": {"publication_id": pid, "assignment_id": first,
            "recipient_person_id": h.seed["people"][1], "expires_at": (m.now() + timedelta(days=1)).isoformat(),
            "reason": "Concurrent invitation"}}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == [200, 409]
    assert len(h.get("staff", "/api/swaps")["swaps"]) == 1


@pytest.mark.parametrize("kind", ["exchange", "give"])
def test_withdrawal_during_approved_review_blocks_publication(h, kind):
    row, w, source = prepared(h, kind)
    w = h.lifecycle(h.lifecycle(w, "submit")["workspace"], "approve", "approver")["workspace"]
    row = transition(h, row, "withdraw", "staff_two")["swap"]
    assert row["status"] == "withdrawn"
    assert h.lifecycle(w, "publish", status=409)["error"]["code"] == "SWAP_WITHDRAWN"
    assert h.get("coordinator", "/api/publications/" + row["publication_id"])["snapshot"] == source
    assert len(h.get("coordinator", "/api/publications")["publications"]) == 1


@pytest.mark.parametrize("state,code", [("expired", "SWAP_EXPIRED"), ("stale", "SWAP_STALE"), ("access_revoked", "SWAP_ACCESS_REVOKED")])
@pytest.mark.parametrize("kind", ["exchange", "give"])
def test_consent_is_rechecked_before_preparation_without_saving_half_amendment(h, monkeypatch, state, code, kind):
    row, source = accepted(h, kind)
    if state == "stale":
        other = h.workspace("Concurrent ordinary amendment", base=row["publication_id"])
        h.publish(other)
    elif state == "access_revoked":
        h.command("admin", "identity.disable", {"reason": "Recipient access revoked"}, h.seed["identities"]["staff_two"], 1)
    else:
        expired_now = m.now() + timedelta(days=1, seconds=5)
        freeze_clock(monkeypatch, lambda: expired_now)
    count = len(h.get("coordinator", "/api/workspaces")["workspaces"])
    assert h.get("coordinator", "/api/swaps/" + row["id"])["swap"]["effective_status"] == state
    assert transition(h, row, "prepare", "coordinator", status=409)["error"]["code"] == code
    assert len(h.get("coordinator", "/api/workspaces")["workspaces"]) == count


@pytest.mark.parametrize("kind", ["exchange", "give"])
def test_revoked_recipient_after_approval_cannot_complete_publication(h, kind):
    row, w, _ = prepared(h, kind)
    w = h.lifecycle(h.lifecycle(w, "submit")["workspace"], "approve", "approver")["workspace"]
    h.command("admin", "identity.disable", {"reason": "Consent authority ended"}, h.seed["identities"]["staff_two"], 1)
    assert h.lifecycle(w, "publish", status=409)["error"]["code"] == "SWAP_ACCESS_REVOKED"
    assert len(h.get("coordinator", "/api/publications")["publications"]) == 1


@pytest.mark.parametrize("kind", ["exchange", "give"])
def test_failed_complete_validation_rolls_back_substitutions_workspace_and_events(h, kind):
    row, source = accepted(h, kind)
    h.command("coordinator", "availability.record", {"person_id": h.seed["people"][1],
        "start": "2026-10-04T08:00:00+03:00", "end": "2026-10-04T12:00:00+03:00",
        "kind": "actual_absence", "private_reason": "Must not appear in a swap projection"})
    with transaction(h.app.state.engine) as db:
        before = [db.scalar(select(func.count()).select_from(model)) for model in (m.Workspace, m.Assignment, m.SwapEvent, m.Outbox)]
    assert transition(h, row, "prepare", "coordinator", status=422)["error"]["code"] == "SWAP_INVALID"
    with transaction(h.app.state.engine) as db:
        assert [db.scalar(select(func.count()).select_from(model)) for model in (m.Workspace, m.Assignment, m.SwapEvent, m.Outbox)] == before
    assert h.get("coordinator", "/api/publications/" + row["publication_id"])["snapshot"] == source
    assert h.get("staff", "/api/swaps/" + row["id"])["swap"]["status"] == "accepted"


@pytest.mark.parametrize("kind", ["exchange", "give"])
def test_exact_consented_content_rejects_piggyback_changes(h, kind):
    row, w, _ = prepared(h, kind)
    shift = h.detail(w)["shifts"][0]
    w = h.command("coordinator", "shift.action", {"workspace_id": w["id"], "shift_ids": [shift["id"]],
        "action": "lock", "reason": "Unconsented metadata change"}, revision=w["revision"])["workspace"]
    assert h.lifecycle(w, "submit", status=409)["error"]["code"] == "SWAP_CONTENT_CHANGED"
    assert h.get("staff", "/api/swaps/" + row["id"])["swap"]["status"] == "reviewing"


@pytest.mark.parametrize("kind", ["exchange", "give"])
def test_swap_participants_cannot_review_their_own_swap_even_with_approver_role(h, kind):
    row, w, _ = prepared(h, kind)
    h.command("admin", "role.grant", {"identity_id": h.seed["identities"]["staff_two"], "role": "approver",
        "starts_on": "2020-01-01", "reason": "Controlled synthetic dual role"})
    w = h.lifecycle(w, "submit")["workspace"]
    assert h.lifecycle(w, "approve", "staff_two", status=403)["error"]["code"] == "SELF_APPROVAL"


def test_concurrent_preparation_has_one_result_not_two_amendments(h):
    row, _ = accepted(h)
    client = h.client("coordinator"); barrier = threading.Barrier(2)
    def attempt(_):
        barrier.wait()
        return client.post("/api/commands", headers={"idempotency-key": str(uuid.uuid4())}, json={
            "kind": "swap.prepare", "target_id": row["id"], "expected_revision": row["revision"],
            "payload": {"reason": "Concurrent complete preparation"}}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == [200, 409]
    assert len(h.get("coordinator", "/api/workspaces")["workspaces"]) == 2
    assert len(h.get("coordinator", "/api/publications")["publications"]) == 1


def test_swap_receipt_reauthorizes_current_person_binding(h):
    pid, first, _, _ = source_pair(h)
    key = str(uuid.uuid4()); row = offered(h, pid, first, key)
    assert offered(h, pid, first, key) == row
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["staff"]).person_id = h.seed["people"][2]
    h.get("staff", "/api/commands/" + key, status=404)
    payload = {"publication_id": pid, "assignment_id": first, "recipient_person_id": h.seed["people"][1],
               "expires_at": (m.now()+timedelta(days=1)).isoformat(),
               "reason": "Shared operational exchange request; no private medical details"}
    assert h.command("staff", "swap.offer", payload, key=key, status=404)["error"]["code"] == "NOT_FOUND"


def test_consent_events_are_append_only_and_notifications_are_scoped(h):
    row, _ = accepted(h)
    for _ in range(30):
        if not process_one(h.app.state.engine):
            break
    one = h.get("staff", "/api/notifications")["notifications"]
    two = h.get("staff_two", "/api/notifications")["notifications"]
    three = h.get("staff_three", "/api/notifications")["notifications"]
    # Each person hears about the other's steps, not their own: staff offered and confirmed, staff_two offered back.
    assert [n["title"] for n in one if n["title"].startswith("Swap")] == ["Swap: duty offered in return"]
    assert sorted(n["title"] for n in two if n["title"].startswith("Swap")) == ["Swap agreed", "Swap offered"]
    assert not three
    for statement in ("UPDATE swap_events SET reason='altered'", "DELETE FROM swap_events"):
        with pytest.raises(IntegrityError), transaction(h.app.state.engine, write=True) as db:
            db.execute(text(statement))


@pytest.mark.parametrize("expiry", ["2026-09-18T09:00:00+00:00", "2026-10-01T12:00:00+00:00", "2026-09-19T12:00:00"])
def test_swap_expiry_requires_offset_future_and_seven_day_bound(h, expiry):
    pid, first, _, _ = source_pair(h)
    h.command("staff", "swap.offer", {"publication_id": pid, "assignment_id": first,
        "recipient_person_id": h.seed["people"][1], "expires_at": expiry, "reason": "Invalid expiry fixture"}, status=422)
    assert not h.get("staff", "/api/swaps")["swaps"]


def test_pagination_reports_more_results_and_rejects_unbounded_queries(h):
    pid, first, _, _ = source_pair(h, third=True)
    offered(h, pid, first)
    h.command("staff", "swap.offer", {"publication_id": pid, "assignment_id": first,
        "recipient_person_id": h.seed["people"][2], "expires_at": (m.now()+timedelta(days=1)).isoformat(),
        "reason": "Separate colleague invitation"})
    page = h.get("staff", "/api/swaps?limit=1")
    assert page["has_more"] and page["total"] == 2 and len(page["swaps"]) == 1
    second = h.get("staff", "/api/swaps?limit=1&offset=1")
    assert not second["has_more"] and page["swaps"][0]["id"] != second["swaps"][0]["id"]
    h.get("staff", "/api/swaps?limit=100000", status=422)


def test_swap_notifications_say_what_happened(h):
    accepted(h)
    for _ in range(30):
        if not process_one(h.app.state.engine):
            break
    titles = {n["title"] for who in ("staff", "staff_two") for n in h.get(who, "/api/notifications")["notifications"]}
    assert {"Swap offered", "Swap: duty offered in return", "Swap agreed"} <= titles


def stored_reasons(h, swap_id):
    with transaction(h.app.state.engine) as db:
        return db.get(m.SwapRequest, swap_id).reason, {e.action: e.reason for e in db.scalars(
            select(m.SwapEvent).where(m.SwapEvent.swap_id == swap_id))}


def test_an_exchange_needs_no_typed_text_from_offer_to_confirmation(h):
    pid, first, second, _ = source_pair(h)
    offer = {"publication_id": pid, "assignment_id": first, "recipient_person_id": h.seed["people"][1],
             "expires_at": (m.now()+timedelta(days=1)).isoformat()}
    h.command("staff", "swap.offer", {**offer, "reason": "x" * 501}, status=422)
    row = h.command("staff", "swap.offer", offer)["swap"]
    h.command("staff_two", "swap.counteroffer", {"assignment_id": second, "reason": "x" * 501}, row["id"], row["revision"], status=422)
    row = h.command("staff_two", "swap.counteroffer", {"assignment_id": second}, row["id"], row["revision"])["swap"]
    row = h.command("staff", "swap.confirm", {}, row["id"], row["revision"])["swap"]
    assert row["status"] == "accepted"
    assert stored_reasons(h, row["id"]) == ("", {"offered": "", "counteroffer": "", "confirm": ""})


def test_only_a_refusal_by_a_coordinator_or_approver_keeps_its_required_reason(h):
    row, _ = accepted(h)
    assert stored_reasons(h, row["id"])[0] == "Shared operational exchange request; no private medical details"
    for payload in ({}, {"reason": ""}, {"reason": "no"}):
        refused = h.command("coordinator", "swap.reject", payload, row["id"], row["revision"], status=422)
        assert refused["error"]["code"] == "SCHEMA_INVALID"
    row = h.command("coordinator", "swap.reject", {"reason": "The department cannot spare this cover"}, row["id"], row["revision"])["swap"]
    assert row["status"] == "rejected"
    assert stored_reasons(h, row["id"])[1]["reject"] == "The department cannot spare this cover"


@pytest.mark.parametrize("withdraw_note", [{}, {"reason": ""}, {"reason": "Routine update"}])
def test_a_give_away_is_accepted_confirmed_and_withdrawn_without_typed_text(h, withdraw_note):
    pid, first, _, _ = source_pair(h)
    row = offered(h, pid, first, kind="give")
    row = h.command("staff_two", "swap.accept", {}, row["id"], row["revision"])["swap"]
    assert row["status"] == "counteroffered"
    row = h.command("staff", "swap.confirm", {}, row["id"], row["revision"])["swap"]
    assert row["status"] == "accepted"
    row = h.command("staff", "swap.withdraw", withdraw_note, row["id"], row["revision"])["swap"]
    assert row["status"] == "withdrawn"
    reasons = stored_reasons(h, row["id"])[1]
    assert (reasons["accept"], reasons["confirm"], reasons["withdraw"]) == ("", "", "")
    with transaction(h.app.state.engine) as db:
        audit = db.scalars(select(m.AuditEvent).where(m.AuditEvent.target_id == row["id"],
            m.AuditEvent.action.in_(("swap.accept", "swap.confirm", "swap.withdraw"))))
        assert {event.action: event.reason for event in audit} == {
            "swap.accept": "", "swap.confirm": "", "swap.withdraw": ""}


def extra_staff(h, count):
    with transaction(h.app.state.engine, write=True) as db:
        organisation_id = db.get(m.Department, h.seed['department_id']).organisation_id
        people = [m.Person(id=m.uid(), organisation_id=organisation_id, display_name=f'Extra staff {i:03}')
                  for i in range(count)]
        db.add_all(people)
        db.flush()
        identities = [m.Identity(id=m.uid(), issuer='local-development', subject=p.id,
                                display_name=p.display_name, person_id=p.id) for p in people]
        db.add_all(identities)
        db.flush()
        db.add_all([m.RoleBinding(identity_id=i.id, department_id=h.seed['department_id'], role='staff',
                                  starts_on=m.now().date()) for i in identities])
        return [p.id for p in people]


@pytest.mark.parametrize('role', ROLE_NAMES)
def test_recipient_options_and_offers_use_effective_staff_permission(h, role):
    pid, first, _, _ = source_pair(h, third=True)
    person_id = h.seed['people'][2]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed['identities']['staff_three']).disabled = True
    identity(h, 'candidate', [role], person_id)
    eligible = role in {'staff', 'admin'}
    assert h.get('candidate', '/api/me')['staff_swap_access'] is eligible
    recipients = h.get('staff', '/api/swaps/options')['recipients']
    assert (person_id in {r['id'] for r in recipients}) is eligible
    reply = h.command('staff', 'swap.offer', {'publication_id': pid, 'assignment_id': first,
        'recipient_person_id': person_id, 'expires_at': (m.now() + timedelta(days=1)).isoformat(),
        'reason': 'Current effective permission'}, status=200 if eligible else 404)
    if eligible:
        assert reply['swap']['recipient_person_id'] == person_id
    else:
        assert reply['error']['code'] == 'RECIPIENT_UNAVAILABLE'


@pytest.mark.parametrize('state', ['disabled', 'inactive', 'revoked', 'future', 'ended', 'other_department', 'other_organisation'])
def test_ineligible_bindings_are_neither_recipients_nor_offer_targets(h, state):
    pid, first, _, _ = source_pair(h)
    person_id = h.seed['people'][2]
    with transaction(h.app.state.engine, write=True) as db:
        sign_in = db.get(m.Identity, h.seed['identities']['staff_three'])
        person = db.get(m.Person, person_id)
        binding = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == sign_in.id))
        if state == 'disabled':
            sign_in.disabled = True
        elif state == 'inactive':
            person.active = False
        elif state == 'revoked':
            binding.revoked = True
        elif state == 'future':
            binding.starts_on = m.now().date() + timedelta(days=1)
        elif state == 'ended':
            binding.ends_on = m.now().date()
        elif state == 'other_department':
            department = m.Department(organisation_id=person.organisation_id, name='Other department')
            db.add(department); db.flush()
            binding.department_id = department.id
        else:
            organisation = m.Organisation(name='Other organisation')
            db.add(organisation); db.flush()
            person.organisation_id = organisation.id
    assert person_id not in {r['id'] for r in h.get('staff', '/api/swaps/options')['recipients']}
    assert h.command('staff', 'swap.offer', {'publication_id': pid, 'assignment_id': first,
        'recipient_person_id': person_id, 'expires_at': (m.now() + timedelta(days=1)).isoformat(),
        'reason': 'Invalid current binding'}, status=404)['error']['code'] == 'RECIPIENT_UNAVAILABLE'


def test_recipient_options_batch_eligibility_at_department_scale_and_deduplicate_people(h):
    source_pair(h)
    identity(h, 'staff_alias', ['admin'], h.seed['people'][1])
    reads = []
    def read(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith('SELECT'):
            reads.append(statement)
    event.listen(h.app.state.engine, 'before_cursor_execute', read)
    try:
        small = h.get('staff', '/api/swaps/options')
        small_reads = len(reads)
        added = extra_staff(h, 150)
        reads.clear()
        large = h.get('staff', '/api/swaps/options')
        assert len(reads) <= small_reads + 2  # no role/person queries per recipient
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', read)
    assert len(small['recipients']) == 2
    assert {r['id'] for r in large['recipients']} == set(h.seed['people'][1:] + added)
    assert not large['recipients_partial']
