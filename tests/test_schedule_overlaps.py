"""Rotas beside each other: overlap detection, replace from a date (publication effective ranges), amendments of a
partly replaced rota, competing drafts and the database guard (docs/design/schedule-unification.md)."""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from apps.api import models as m
from apps.api.db import transaction
from packages.domain import time as domain_time

DAY4, DAY9 = ("2026-10-04T08:00:00+03:00", "2026-10-04T12:00:00+03:00"), ("2026-10-09T08:00:00+03:00", "2026-10-09T12:00:00+03:00")


def published_rota(h):
    """P: 4–11 Oct, the staff member on 4 and 9 Oct, a colleague on 9 Oct."""
    a, b, _ = h.seed["people"]
    w = h.shift(h.workspace("October rota"), *DAY4, people=[a])["workspace"]
    w = h.shift(w, *DAY9, people=[a, b])["workspace"]
    return h.publish(w)["publication_id"]


def replacement(h, pid, start="2026-10-08", end="2026-10-15", people=None):
    """Q replaces P from `start`, with its own session on 9 Oct."""
    w = h.command("coordinator", "workspace.create", {"name": "Replacement", "starts_on": start, "ends_on": end,
                                                      "replaces_publication_id": pid})["workspace"]
    return h.shift(w, "2026-10-09T13:00:00+03:00", "2026-10-09T17:00:00+03:00", people=people or [h.seed["people"][1]])["workspace"]


def pub(h, pid):
    return next(p for p in h.get("coordinator", "/api/publications")["publications"] if p["id"] == pid)


def overlaps(h, **query):
    return h.get("coordinator", "/api/schedule/overlaps?" + "&".join(f"{k}={v}" for k, v in query.items()))


def test_overlap_check_before_creating_offers_three_ways(h):
    pid = published_rota(h)
    found = overlaps(h, starts_on="2026-10-08", ends_on="2026-10-15")
    assert found["conflict"] is True
    assert [(p["id"], p["relation"], p["overlap"]) for p in found["publications"]] == [
        (pid, "conflict", {"starts_on": "2026-10-08", "ends_on": "2026-10-11"})]
    after, replace, amend = found["options"]
    assert (after["choice"], after["default"], after["available"], after["starts_on"]) == ("start_after", True, True, "2026-10-11")
    assert (replace["choice"], replace["publication_id"], replace["earliest"], replace["latest"], replace["starts_on"]) == (
        "replace_from", pid, "2026-10-05", "2026-10-10", "2026-10-08")
    assert (amend["choice"], amend["available"], amend["starts_on"], amend["ends_on"]) == ("amend", True, "2026-10-04", "2026-10-11")
    # Inside the published rota: nothing left after it in these dates.
    inside = overlaps(h, starts_on="2026-10-05", ends_on="2026-10-10")["options"][0]
    assert inside["available"] is False and inside["why"]
    # Acknowledged: an amendment, or a replacement from a day inside it.
    assert overlaps(h, starts_on="2026-10-04", ends_on="2026-10-11", base_publication_id=pid)["conflict"] is False
    assert overlaps(h, starts_on="2026-10-08", ends_on="2026-10-15", replaces_publication_id=pid)["publications"][0]["relation"] == "replaces"
    clear = overlaps(h, starts_on="2026-10-11", ends_on="2026-10-18")
    assert (clear["conflict"], clear["publications"], clear["options"]) == (False, [], [])
    h.get("staff", "/api/schedule/overlaps?starts_on=2026-10-08&ends_on=2026-10-15", status=403)


def test_creating_over_published_days_answers_with_the_overlap_and_publishing_refuses_it(h):
    pid = published_rota(h)
    created = h.command("coordinator", "workspace.create", {"name": "Overlapping", "starts_on": "2026-10-08", "ends_on": "2026-10-15"})
    assert created["overlaps"]["conflict"] and created["overlaps"]["publications"][0]["id"] == pid
    w = h.shift(created["workspace"], "2026-10-12T08:00:00+03:00", "2026-10-12T12:00:00+03:00")["workspace"]
    w = h.lifecycle(w, "submit")["workspace"]
    w = h.lifecycle(w, "approve", "approver")["workspace"]
    refused = h.lifecycle(w, "publish", status=409)["error"]
    assert refused["code"] == "OVERLAPPING_PUBLICATION"
    assert [o["choice"] for o in refused["details"]["options"]] == ["start_after", "replace_from", "amend"]


@pytest.fixture(params=['2026-10-03T06:00:00+00:00', '2026-10-25T00:00:00+00:00'])
def publication_clock(request, monkeypatch):
    instant = datetime.fromisoformat(request.param)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(domain_time, 'datetime', FrozenDateTime)
    monkeypatch.setattr(domain_time, '_offset', timedelta(0))
    assert m.now() == instant


def test_replace_from_a_date_end_to_end(h, publication_clock):
    a, b, _ = h.seed["people"]
    pid = published_rota(h)
    q = replacement(h, pid)
    # Checking Q: P counts only before 8 Oct, so P's 9 Oct session is not part of Q's context.
    context = h.snapshot(q)
    p_shifts = {s["start"][:10] for s in context["shifts"] if s["workspace_id"] != q["id"]}
    assert p_shifts == {"2026-10-04"}
    listed = next(w for w in h.get("coordinator", "/api/workspaces?limit=50")["workspaces"] if w["id"] == q["id"])
    assert listed["overlap"] == {"drafts": [], "publications": []}
    # A wish on a replaced day: decided by P first, then by Q.
    h.command("staff", "wish.create", {"person_id": a, "first_day": "2026-10-12", "last_day": "2026-10-12", "kind": "prefer_off", "points": 1})
    published = h.publish(q)
    qid = published["publication_id"]
    assert published["replaced"] == {"publication_id": pid, "effective_until": "2026-10-08"}
    old = pub(h, pid)
    assert old["current"] is True and old["effective_until"] == "2026-10-08" and old["ends_on"] == "2026-10-11"
    assert pub(h, qid)["current"] is True and pub(h, qid)["effective_until"] is None
    # Published rota (staff and coordinators): P shows only the days it is in force.
    assert [s["start"][:10] for s in h.get("coordinator", f"/api/publications/{pid}")["snapshot"]["shifts"]] == ["2026-10-04"]
    assert [s["start"][:10] for s in h.get("staff", f"/api/publications/{pid}")["snapshot"]["shifts"]] == ["2026-10-04"]
    # Calendars: the staff member keeps 4 Oct from P and loses P's 9 Oct duty; the team sees Q's 9 Oct session.
    mine = h.get("staff", "/api/calendar?start=2026-10-04&end=2026-10-15")["events"]
    assert [(e["start"][:10], e["source_id"]) for e in mine if e["kind"] == "duty"] == [("2026-10-04", pid)]
    team = h.get("coordinator", "/api/calendar?start=2026-10-04&end=2026-10-15")["sessions"]
    assert sorted((s["start"][:16], s["source_id"]) for s in team) == [("2026-10-04T05:00", pid), ("2026-10-09T10:00", qid)]
    # Calendar subscription.
    path = h.command("staff", "calendar_feed.create", {})["path"]
    from fastapi.testclient import TestClient
    assert TestClient(h.app, base_url=h.settings.public_origin).get(path).text.count("BEGIN:VEVENT") == 1
    # A new draft over those days checks against Q there, not against P.
    later = h.workspace("Next", "2026-10-09", "2026-10-16")
    around = {(s["workspace_id"], s["start"][:10]) for s in h.snapshot(later)["shifts"]}
    assert around == {(pub(h, pid)["workspace_id"], "2026-10-04"), (q["id"], "2026-10-09")}
    # Fairness and wish ledgers of P stop at 8 Oct; the wish on 12 Oct now belongs to Q.
    with transaction(h.app.state.engine) as db:
        buckets = list(db.scalars(select(m.FairnessLedger.bucket).where(m.FairnessLedger.publication_id == pid)))
        assert buckets and max(buckets) < date(2026, 10, 8)
        assert set(db.scalars(select(m.WishLedger.period_end).where(m.WishLedger.publication_id == pid))) <= {date(2026, 10, 8)}
        assert db.scalar(select(m.StaffWish.publication_id)) == qid
    # The coverage check of P reads P as in force.
    assert {c["shift_id"] for c in h.get("coordinator", f"/api/publications/{pid}/coverage")["evaluation"]["coverage"]} == {
        s["id"] for s in h.get("coordinator", f"/api/publications/{pid}")["snapshot"]["shifts"]}


def test_a_replacement_must_start_inside_the_rota_it_replaces(h):
    pid = published_rota(h)
    for start in ("2026-10-04", "2026-10-11", "2026-10-02"):
        refused = h.command("coordinator", "workspace.create", {"name": "Bad", "starts_on": start, "ends_on": "2026-10-18",
                                                                "replaces_publication_id": pid}, status=409)
        assert refused["error"]["code"] == "REPLACEMENT_SCOPE"
    h.command("coordinator", "workspace.create", {"name": "Both", "starts_on": "2026-10-04", "ends_on": "2026-10-11",
                                                  "base_publication_id": pid, "replaces_publication_id": pid}, status=422)


def test_amending_a_partly_replaced_rota_keeps_to_its_days_in_force(h):
    pid = published_rota(h)
    qid = h.publish(replacement(h, pid))["publication_id"]
    amendment = h.workspace("Change P", base=pid)
    assert [s["start"][:10] for s in h.detail(amendment)["shifts"]] == ["2026-10-04"]
    # A session on a replaced day cannot be published through the old rota.
    stray = h.shift(amendment, "2026-10-10T08:00:00+03:00", "2026-10-10T12:00:00+03:00", people=[h.seed["people"][2]])["workspace"]
    stray = h.lifecycle(stray, "submit")["workspace"]
    stray = h.lifecycle(stray, "approve", "approver")["workspace"]
    assert h.lifecycle(stray, "publish", status=409)["error"]["code"] == "REPLACED_DAYS"
    # Without it, the amendment supersedes P and stays in force only until 8 Oct, beside Q.
    clean = h.workspace("Change P again", base=pid)
    two = h.publish(h.shift(clean, "2026-10-05T08:00:00+03:00", "2026-10-05T12:00:00+03:00", people=[h.seed["people"][2]])["workspace"])
    assert pub(h, pid)["current"] is False
    assert pub(h, two["publication_id"])["effective_until"] == "2026-10-08" and pub(h, qid)["current"] is True
    changes = h.get("coordinator", f"/api/publications/{two['publication_id']}/changes")["days"]
    assert [(d["date"], d["added"]) for d in changes] == [("2026-10-05", 1)]


def test_choosing_on_an_existing_draft_start_after_replace_from_and_undo(h):
    pid = published_rota(h)
    w = h.command("coordinator", "workspace.create", {"name": "Draft", "starts_on": "2026-10-08", "ends_on": "2026-10-15"})["workspace"]
    w = h.shift(w, "2026-10-12T08:00:00+03:00", "2026-10-12T12:00:00+03:00")["workspace"]
    row = lambda: next(x for x in h.get("coordinator", "/api/workspaces")["workspaces"] if x["id"] == w["id"])
    assert row()["overlap"]["publications"] == [pid]
    choose = lambda w, **p: h.command("coordinator", "workspace.resolve_overlap", {"reason": "Place beside October", **p}, w["id"], w["revision"])
    after = choose(w, choice="start_after")["workspace"]
    assert after["starts_on"] == "2026-10-11" and after["replaces_publication_id"] is None and row()["overlap"]["publications"] == []
    undone = h.command("coordinator", "workspace.undo", {"reason": "Undo"}, after["id"], after["revision"])["workspace"]
    assert undone["starts_on"] == "2026-10-08" and row()["overlap"]["publications"] == [pid]
    replaced = choose(undone, choice="replace_from", starts_on="2026-10-09")["workspace"]
    assert (replaced["starts_on"], replaced["replaces_publication_id"]) == ("2026-10-09", pid) and row()["overlap"]["publications"] == []
    refused = h.command("coordinator", "workspace.resolve_overlap", {"reason": "Too late", "choice": "replace_from", "starts_on": "2026-10-04"},
                        replaced["id"], replaced["revision"], status=409)["error"]["code"]
    assert refused == "REPLACEMENT_SCOPE"
    # Sessions before the new first day must be moved first.
    early = h.shift(h.command("coordinator", "workspace.create", {"name": "Early", "starts_on": "2026-10-08", "ends_on": "2026-10-15"})["workspace"],
                    "2026-10-08T08:00:00+03:00", "2026-10-08T12:00:00+03:00")["workspace"]
    assert h.command("coordinator", "workspace.resolve_overlap", {"reason": "Start after", "choice": "start_after"}, early["id"], early["revision"],
                     status=409)["error"]["code"] == "OUTSIDE_WORKSPACE"
    clear = h.workspace("Clear", "2026-10-20", "2026-10-27")
    assert h.command("coordinator", "workspace.resolve_overlap", {"reason": "Nothing", "choice": "start_after"}, clear["id"], clear["revision"],
                     status=409)["error"]["code"] == "NO_OVERLAP"


def test_competing_drafts_are_flagged_and_publishing_one_marks_the_others(h):
    one = h.shift(h.workspace("Plan A"))["workspace"]
    two = h.shift(h.workspace("Plan B"), people=[h.seed["people"][1]])["workspace"]
    rows = {x["id"]: x["overlap"] for x in h.get("coordinator", "/api/workspaces")["workspaces"]}
    assert rows[one["id"]] == {"drafts": [two["id"]], "publications": []} and rows[two["id"]]["drafts"] == [one["id"]]
    assert [d["id"] for d in overlaps(h, workspace_id=one["id"])["drafts"]] == [two["id"]]
    published = h.publish(one)
    assert [d["id"] for d in published["outdated_drafts"]] == [two["id"]]
    rows = {x["id"]: x["overlap"] for x in h.get("coordinator", "/api/workspaces")["workspaces"]}
    assert rows[two["id"]] == {"drafts": [], "publications": [published["publication_id"]]} and rows[one["id"]] is None


def test_the_replaced_rota_changing_meanwhile_is_caught_before_publishing(h):
    pid = published_rota(h)
    q = replacement(h, pid)
    h.publish(h.workspace("Amend P first", base=pid))
    q = h.lifecycle(q, "submit")["workspace"]
    q = h.lifecycle(q, "approve", "approver")["workspace"]
    refused = h.lifecycle(q, "publish", status=409)["error"]
    assert refused["code"] == "PUBLICATION_HEAD_CHANGED" and refused["details"]["conflict"] is True


def test_concurrent_replacements_of_one_rota_only_one_commits(h):
    pid = published_rota(h)
    candidates = []
    for start in ("2026-10-08", "2026-10-09"):
        w = h.lifecycle(replacement(h, pid, start), "submit")["workspace"]
        candidates.append(h.lifecycle(w, "approve", "approver")["workspace"])
    barrier = threading.Barrier(2)

    def publish(w):
        barrier.wait()
        return h.client("coordinator").post("/api/commands", headers={"idempotency-key": str(uuid.uuid4())}, json={
            "kind": "workspace.publish", "target_id": w["id"], "expected_revision": w["revision"], "payload": {"reason": "Race"}})
    with ThreadPoolExecutor(max_workers=2) as pool:
        winner, loser = sorted(pool.map(publish, candidates), key=lambda response: response.status_code)
    assert winner.status_code == 200, winner.text
    assert loser.status_code in (409, 422), loser.text
    if loser.status_code == 422:
        error = loser.json()["error"]
        assert error["code"] == "VALIDATION_FAILED", loser.text
        assert error["details"]["stale_approval"]["code"] == "STALE_APPROVAL", loser.text
    current = [p for p in h.get("coordinator", "/api/publications")["publications"] if p["current"]]
    assert len(current) == 2 and pub(h, pid)["effective_until"] in {"2026-10-08", "2026-10-09"}


def database_refuses_overlapping_rotas_in_force(h, error):
    old = published_rota(h)
    pid = h.publish(h.workspace("Amended", base=old))["publication_id"]
    h.publish(replacement(h, pid))
    engine = h.app.state.engine
    statements = [
        ("UPDATE publications SET effective_until = :d WHERE id = :id", {"d": date(2026, 10, 10), "id": pid}),  # grows back
        ("UPDATE publications SET effective_until = NULL WHERE id = :id", {"id": pid}),                        # cleared
        ("UPDATE publications SET effective_until = :d WHERE id = :id", {"d": date(2026, 10, 4), "id": pid}),   # empty range
        ("UPDATE publications SET snapshot = '{}' WHERE id = :id", {"id": pid}),                               # still immutable
    ]
    with transaction(engine) as db:
        assert db.get(m.Publication, pid).calendar_lineage is True
    with pytest.raises(error, match="immutable publication"), transaction(engine, write=True) as db:
        db.execute(text("UPDATE publications SET calendar_lineage = NULL WHERE id = :id"), {"id": pid})
    for sql, values in statements:
        with pytest.raises(error), transaction(engine, write=True) as db:
            db.execute(text(sql), values)
    # Shrinking further is allowed (a later replacement); two current rotas on one day are refused.
    with transaction(engine, write=True) as db:
        db.execute(text("UPDATE publications SET effective_until = :d WHERE id = :id"), {"d": date(2026, 10, 6), "id": pid})
    with pytest.raises(error), transaction(engine, write=True) as db:
        db.execute(text("UPDATE publications SET current = true WHERE id = :id"), {"id": old})
    with transaction(engine) as db:
        assert db.get(m.Publication, pid).effective_until == date(2026, 10, 6)


def test_database_refuses_overlapping_rotas_in_force(h):
    database_refuses_overlapping_rotas_in_force(h, IntegrityError)


@pytest.mark.postgres
def test_postgres_refuses_overlapping_rotas_in_force(pg):
    database_refuses_overlapping_rotas_in_force(pg, DBAPIError)


@pytest.mark.postgres
def test_postgres_replace_from_a_date_end_to_end(pg, publication_clock):
    test_replace_from_a_date_end_to_end(pg, publication_clock)


@pytest.mark.postgres
def test_postgres_concurrent_replacements(pg):
    test_concurrent_replacements_of_one_rota_only_one_commits(pg)


def test_migration_keeps_existing_publications_and_guards_new_ones(tmp_path):
    """Data safety: 0050 adds nullable columns only; publications already stored keep every value and stay in force."""
    from alembic import command
    from apps.api.db import make_engine
    from scripts.manage import alembic_config, migrate
    from .migration_support import historical_seed
    url = "sqlite:///" + str(tmp_path / "before.db")
    command.upgrade(alembic_config(url), "0049_workspace_return_note")
    engine = make_engine(url)
    valid = '{"valid": true, "contracts": {"block_quota": "block-quota-v5", "programmes": "programmes-v1"}}'
    with transaction(engine, write=True) as db:
        seed = historical_seed(db)
        dept, who = seed['department_id'], seed['identities']['coordinator']
        for n, (start, end, status, current, supersedes) in enumerate([
                ("2026-09-01", "2026-10-01", "published", True, None), ("2026-10-01", "2026-11-01", "superseded", False, None),
                ("2026-10-01", "2026-11-01", "published", True, "p1")]):
            db.execute(text("INSERT INTO workspaces (id, revision, created_at, department_id, name, starts_on, ends_on, status, "
                            "content_revision, created_by, last_edited_by) VALUES (:id, 1, '2026-08-01 00:00:00', :d, :name, :a, :b, :s, 1, :who, :who)"),
                       {"id": f"w{n}", "d": dept, "name": f"Rota {n}", "a": start, "b": end, "s": status, "who": who})
            db.execute(text("INSERT INTO publications (id, revision, created_at, department_id, workspace_id, sequence, starts_on, ends_on, current, "
                            "supersedes_id, published_by, snapshot, validation, fingerprint) VALUES (:id, 1, '2026-08-01 00:00:00', :d, :w, :n, :a, :b, :c, "
                            ":sup, :who, '{\"shifts\": []}', :v, :fp)"),
                       {"id": f"p{n}", "d": dept, "w": f"w{n}", "n": n + 1, "a": start, "b": end, "c": current, "sup": supersedes, "who": who,
                        "v": valid, "fp": "0" * 64})
        before = [tuple(r) for r in db.execute(text("SELECT * FROM publications ORDER BY id"))]
    engine.dispose()
    migrate(url)
    engine = make_engine(url)
    try:
        with transaction(engine) as db:
            after = [tuple(r) for r in db.execute(text("SELECT * FROM publications ORDER BY id"))]
            assert [r[:len(before[0])] for r in after] == before
            added = list(db.execute(text("SELECT effective_until, calendar_lineage FROM publications ORDER BY id")))
            assert added == [(None, None)] * len(before)
            assert db.scalar(text("SELECT count(*) FROM workspaces WHERE replaces_publication_id IS NOT NULL")) == 0
        with pytest.raises(IntegrityError), transaction(engine, write=True) as db:
            db.execute(text("UPDATE publications SET current = 1 WHERE id = 'p1'"))  # two rotas in force in October
        with transaction(engine, write=True) as db:
            db.execute(text("UPDATE publications SET effective_until = '2026-10-15' WHERE id = 'p2'"))
            db.execute(text("UPDATE publications SET current = 0 WHERE id = 'p0'"))
    finally:
        engine.dispose()
