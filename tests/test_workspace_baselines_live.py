"""Live draft validity, independently proved through checks and refresh previews."""
from copy import deepcopy
from datetime import date

import pytest

from apps.api import models as m, snapshots
from apps.api.db import transaction
from apps.api.snapshots import domain_snapshot, fingerprints, snapshot_inputs
from apps.api.workspace_baselines import latest, live_safety_inputs, verify
from packages.domain.validation import evaluate


BACKENDS = pytest.mark.parametrize("backend", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
SOURCES = pytest.mark.parametrize("source", ["locations", "location_types", "types", "services"])
FIELDS = pytest.mark.parametrize("field,closed", [("active", False), ("starts_on", date(2026, 10, 5)),
                                                ("ends_on", date(2026, 10, 4))])


def target(h, source):
    parent, child = h.seed["locations"]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Location, child).parent_id = parent
        location_type = db.get(m.Location, parent).location_type_id
    return {"locations": (m.Location, parent, "LOCATION_INACTIVE"),
            "location_types": (m.LocationType, location_type, "LOCATION_TYPE_INACTIVE"),
            "types": (m.AssignmentType, h.seed["type"], "TYPE_INACTIVE"),
            "services": (m.Service, h.seed["services"][0], "SERVICE_INACTIVE")}[source]


def change(h, cls, ident, field, value):
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(cls, ident)
        setattr(row, field, value)
        row.revision += 1


def candidate_snapshot(h, draft, candidate, **kwargs):
    # No stored session can trigger the legacy referenced-row enrichment for this draft.
    with transaction(h.app.state.engine) as db:
        data = domain_snapshot(db, db.get(m.Workspace, draft["id"]), **kwargs)
    data["shifts"] = deepcopy(candidate["shifts"])
    data["focus_ids"] = list(candidate["focus_ids"])
    for shift in data["shifts"]:
        shift["workspace_id"] = draft["id"]
    return data


@BACKENDS
@SOURCES
@FIELDS
@pytest.mark.parametrize("frozen_inactive", [False, True], ids=["retire", "reactivate"])
def test_live_validity_finding_for_an_unstored_candidate_in_both_directions(request, backend, source, field, closed, frozen_inactive):
    h = request.getfixturevalue(backend)
    cls, ident, code = target(h, source)
    candidate = h.snapshot(h.shift(h.workspace("Candidate fixture"), location=1)["workspace"])
    with transaction(h.app.state.engine) as db:
        original = getattr(db.get(cls, ident), field)
    change(h, cls, ident, field, closed if frozen_inactive else original)
    draft = h.workspace("Unstored candidate validity")
    change(h, cls, ident, field, original if frozen_inactive else closed)
    findings = evaluate(candidate_snapshot(h, draft, candidate))["findings"]
    assert (code in {row["code"] for row in findings}) is (not frozen_inactive)


@BACKENDS
@SOURCES
@FIELDS
def test_live_validity_does_not_require_a_refresh_decision(request, backend, source, field, closed):
    h = request.getfixturevalue(backend)
    cls, ident, _ = target(h, source)
    draft = h.workspace("No validity drift")
    change(h, cls, ident, field, closed)
    preview = h.command("coordinator", "workspace.baseline.refresh.preview", {}, draft["id"], draft["revision"])
    assert "duties" not in {row["source"] for row in preview["changes"]}


@BACKENDS
@SOURCES
def test_a_missing_live_catalogue_row_is_unusable_for_a_candidate(request, backend, source):
    h = request.getfixturevalue(backend)
    _, ident, code = target(h, source)
    candidate = h.snapshot(h.shift(h.workspace("Missing-row fixture"), location=1)["workspace"])
    draft = h.workspace("Missing live catalogue row")
    with transaction(h.app.state.engine) as db:
        current = snapshot_inputs(db, db.get(m.Organisation, h.seed["organisation_id"]))
    del current[source][ident]
    findings = evaluate(candidate_snapshot(h, draft, candidate, shared_inputs=current))["findings"]
    assert code in {row["code"] for row in findings}


@BACKENDS
@SOURCES
@FIELDS
@pytest.mark.parametrize("frozen_inactive", [False, True], ids=["retire", "reactivate"])
def test_live_validity_invalidates_a_warm_draft_check_in_both_directions(request, backend, source, field, closed, monkeypatch, frozen_inactive):
    h = request.getfixturevalue(backend)
    cls, ident, code = target(h, source)
    with transaction(h.app.state.engine) as db:
        original = getattr(db.get(cls, ident), field)
    change(h, cls, ident, field, closed if frozen_inactive else original)
    draft = h.shift(h.workspace("Warm validity check"), location=1)["workspace"]
    assert h.detail(draft)["workspace"]["validation_current"]
    assert h.detail(draft)["workspace"]["validation_current"]
    with transaction(h.app.state.engine) as db:
        workspace = db.get(m.Workspace, draft["id"])
        checked = workspace.validation["fingerprint"]
        before_revision = db.get(m.Organisation, h.seed["organisation_id"]).input_revision
        with monkeypatch.context() as warm:
            warm.setattr(snapshots, "domain_snapshot", lambda *a, **kw: pytest.fail("Warm check rebuilt its snapshot"))
            assert fingerprints(db, [workspace])[workspace.id] == checked
    change(h, cls, ident, field, original if frozen_inactive else closed)
    assert not h.detail(draft)["workspace"]["validation_current"]
    assert not h.detail(draft)["workspace"]["validation_current"]
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Organisation, h.seed["organisation_id"]).input_revision == before_revision + 1
        assert fingerprints(db, [db.get(m.Workspace, draft["id"])])[draft["id"]] != checked
    assert (code in {finding["code"] for finding in evaluate(h.snapshot(draft))["findings"]}) is (not frozen_inactive)


@BACKENDS
@pytest.mark.parametrize("field,value", [("timezone", "UTC"), ("week_start", 0)])
def test_live_time_change_refuses_publication_without_an_earlier_snapshot_assertion(request, backend, field, value):
    h = request.getfixturevalue(backend)
    draft = h.shift(h.workspace("Live time publication"))["workspace"]
    draft = h.lifecycle(h.lifecycle(draft, "submit")["workspace"], "approve", "approver")["workspace"]
    with transaction(h.app.state.engine, write=True) as db:
        setattr(db.get(m.Organisation, h.seed["organisation_id"]), field, value)
    assert h.lifecycle(draft, "publish", status=409)["error"]["code"] == "STALE_APPROVAL"


@BACKENDS
def test_empty_draft_people_are_live_in_the_domain_snapshot(request, backend):
    h = request.getfixturevalue(backend)
    draft = h.workspace("Empty draft people")
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, h.seed["people"][0]).display_name = "Updated synthetic person"
    assert h.snapshot(draft)["people"][h.seed["people"][0]]["display_name"] == "Updated synthetic person"


@BACKENDS
def test_fallback_excludes_pager_for_a_person_added_after_capture(request, backend):
    h = request.getfixturevalue(backend)
    draft = h.workspace("No pager in scheduling inputs")
    with transaction(h.app.state.engine, write=True) as db:
        person = m.Person(organisation_id=h.seed["organisation_id"], display_name="Synthetic new person", pager="Synthetic pager")
        db.add(person)
        db.flush()
        ident = person.id
    with transaction(h.app.state.engine) as db:
        workspace = db.get(m.Workspace, draft["id"])
        data = live_safety_inputs(db, workspace, verify(latest(db, workspace.id)))
    assert "pager" not in data["people"][ident]


@BACKENDS
def test_prepared_and_fresh_people_have_the_same_complete_public_fields(request, backend):
    h = request.getfixturevalue(backend)
    draft = h.workspace("Live person acquisition parity")
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, h.seed["people"][0]).display_name = "Synthetic live identity"
    with transaction(h.app.state.engine) as db:
        workspace = db.get(m.Workspace, draft["id"])
        frozen = verify(latest(db, workspace.id))
        before = deepcopy(frozen)
        current = snapshot_inputs(db, db.get(m.Organisation, h.seed["organisation_id"]))
        prepared = live_safety_inputs(db, workspace, frozen, current=current)
        fresh = live_safety_inputs(db, workspace, frozen)
    assert prepared["people"] == fresh["people"] == current["people"]
    assert frozen == before


def test_overlay_and_drift_share_the_effective_field_definition(h, monkeypatch):
    from apps.api import workspace_baselines as baselines
    assert baselines.LIVE_VALIDITY_FIELDS == tuple(m.Effective.__annotations__)
    draft = h.workspace("Future live field")
    with transaction(h.app.state.engine) as db:
        workspace = db.get(m.Workspace, draft["id"])
        frozen = verify(latest(db, workspace.id))
        current = snapshot_inputs(db, db.get(m.Organisation, h.seed["organisation_id"]))
        ident = h.seed["locations"][0]
        frozen["locations"][ident]["future_validity"] = "before"
        current["locations"][ident]["future_validity"] = "after"
        monkeypatch.setattr(baselines, "LIVE_VALIDITY_FIELDS", (*baselines.LIVE_VALIDITY_FIELDS, "future_validity"))
        result = live_safety_inputs(db, workspace, frozen, current=current)
    assert result["locations"][ident]["future_validity"] == "after"
    assert baselines._drift_group_hash(frozen, "duties") == baselines._drift_group_hash(current, "duties")


@BACKENDS
@SOURCES
def test_validity_overlay_copies_rows_and_preserves_frozen_configuration(request, backend, source):
    h = request.getfixturevalue(backend)
    cls, ident, _ = target(h, source)
    draft = h.workspace("Copy-on-write validity")
    change(h, cls, ident, "active", False)
    with transaction(h.app.state.engine) as db:
        workspace = db.get(m.Workspace, draft["id"])
        frozen = verify(latest(db, workspace.id))
        current = snapshot_inputs(db, db.get(m.Organisation, h.seed["organisation_id"]))
        current[source][ident]["name"] = "Live name stays outside the captured configuration"
        before_frozen, before_current = deepcopy(frozen), deepcopy(current)
        result = live_safety_inputs(db, workspace, frozen, current=current)
    assert result[source][ident]["active"] is False
    assert result[source][ident]["name"] == frozen[source][ident]["name"]
    assert frozen == before_frozen and current == before_current
