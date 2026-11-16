"""A refused simulated row cannot change the meaning or privacy of its neighbours."""
import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.services import bulk
from .test_bulk import REASON, preview
from .test_bulk_anonymise_limits import setup, states
from .test_bulk_nested_limits import limits
from .test_bulk_pending_privacy import DATABASES


@pytest.mark.parametrize("database", DATABASES)
@pytest.mark.parametrize("diagnostics", [False, True])
def test_failed_anonymise_preview_preserves_later_row_and_undo(request, database, diagnostics):
    h = request.getfixturevalue(database)
    person, refs, _ = setup(h, "private")
    limits(h, 2)
    change = {"item_kind": "catalogue.update", "target_id": person["id"],
        "expected_revision": person["revision"], "payload": {"catalogue": "people"},
        "changes": [{"field": "title", "value": "New title"}]}
    body = {"items": [{"item_kind": "person.anonymise", "target_id": person["id"],
        "expected_revision": person["revision"], "payload": {}}, change], "reason": REASON}
    if diagnostics:
        body["resolutions"] = {"unused-group": "skip"}
    before = states(h, refs)
    expected = preview(h, "admin", {"items": [change], "reason": REASON})["items"][0]
    checked = preview(h, "admin", body)
    assert checked["items"][0]["issues"][0]["code"] == "BULK_ITEM_LIMIT"
    assert {key: value for key, value in checked["items"][1].items() if key != "index"} == {
        key: value for key, value in expected.items() if key != "index"}
    assert checked["items"][1]["before"] == {"title": ""}
    assert checked["items"][1]["after"] == {"title": "New title"}
    assert checked["items"][1]["undoable"] is True
    assert states(h, refs) == before
    applied = h.command("admin", "bulk.apply", {"items": [change], "reason": REASON})
    h.command("admin", "bulk.apply", applied["undo"]["payload"])
    assert states(h, refs)[0]["display_name"] == person["display_name"]
    assert states(h, refs)[0]["title"] == before[0]["title"]


@pytest.mark.parametrize("database", DATABASES)
def test_failed_anonymise_keeps_prior_successful_privacy_and_unrelated_rows(request, database):
    h = request.getfixturevalue(database)
    erased = h.configure("people", {"display_name": "Synthetic erased colleague", "active": False})["record"]
    person, refs, _ = setup(h, "private")
    limits(h, 3)
    body = {"items": [
        {"item_kind": "person.anonymise", "target_id": row["id"],
         "expected_revision": row["revision"], "payload": {}} for row in (erased, person)] + [
        {"item_kind": "catalogue.update", "target_id": person["id"], "expected_revision": person["revision"],
         "payload": {"catalogue": "people"},
         "changes": [{"field": "title", "value": "Unrelated title"}]}], "reason": REASON}
    before = states(h, refs)
    checked = preview(h, "admin", body)
    first, failed, last = checked["items"]
    assert first["result"] == "ready" and first["undoable"] is False
    assert failed["issues"][0]["code"] == "BULK_ITEM_LIMIT"
    assert last["after"] == {"title": "Unrelated title"} and last["undoable"] is True
    assert states(h, refs) == before
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Person, erased["id"]).display_name == erased["display_name"]


def test_preview_savepoint_restores_queued_evidence_revisions_and_cached_context(h):
    """A handler can mutate earlier evidence and bookkeeping before a later check refuses it."""
    with transaction(h.app.state.engine) as db:
        person = db.get(m.Person, h.seed["people"][0])
        snapshot = {"revision": person.revision, "nested": {"private_reason": "Synthetic private reason"}}
        state = {"history": [("availability.update", person, REASON, snapshot)],
                 "advanced": {(m.Person, person.id): person.revision}, "operations": 1,
                 "authorization_commands": [{"kind": "catalogue.update"}], "workspace_changes": {"draft": person}}
        db.info.update(bulk=state, anonymised_people={"already-erased"},
                       event_recipient_contexts={"existing": {"name": "Synthetic recipient"}})
        authority_key = ("director_programmes", "synthetic-director")
        db.info[authority_key] = {"synthetic-programme"}
        with pytest.raises(Problem, match="Synthetic refusal"):
            with bulk._preview_savepoint(db):
                state["history"][0][3]["nested"].clear()
                state["history"].append(("people", person, REASON, {}))
                state["advanced"][(m.Person, person.id)] += 1
                state["operations"] += 2
                state["authorization_commands"].append({"kind": "person.anonymise"})
                state["workspace_changes"].clear()
                db.info[authority_key].clear()
                db.info["anonymised_people"].add(person.id)
                db.info["anonymised_records"] = {person.id: {"display_name": "Former staff member"}}
                db.info["event_recipient_contexts"]["existing"]["name"] = "Former staff member"
                raise Problem(409, "SYNTHETIC_REFUSAL", "Synthetic refusal")
        assert db.info["bulk"] is state
        assert state["history"] == [("availability.update", person, REASON,
            {"revision": person.revision, "nested": {"private_reason": "Synthetic private reason"}})]
        assert state["advanced"] == {(m.Person, person.id): person.revision}
        assert state["operations"] == 1 and state["authorization_commands"] == [{"kind": "catalogue.update"}]
        assert state["workspace_changes"]["draft"] is person
        assert db.info[authority_key] == {"synthetic-programme"}
        assert db.info["anonymised_people"] == {"already-erased"} and "anonymised_records" not in db.info
        assert db.info["event_recipient_contexts"]["existing"]["name"] == "Synthetic recipient"


def test_completed_preview_does_not_reuse_simulated_recipient_or_approval_context(h, monkeypatch):
    def simulate(db, *args):
        db.info["event_recipient_contexts"]["existing"]["name"] = "Simulated recipient"
        db.info["scheduling_fact_approval_goals"] = {"simulated-policy": {"revision": 2}}
        return [], [], []

    monkeypatch.setattr(bulk, "_simulate_rows", simulate)
    with transaction(h.app.state.engine) as db:
        db.info["event_recipient_contexts"] = {"existing": {"name": "Original recipient"}}
        assert bulk._check_rows(db, None, {}, [], h.settings) == ([], [], [])
        assert db.info["event_recipient_contexts"] == {"existing": {"name": "Original recipient"}}
        assert "scheduling_fact_approval_goals" not in db.info
