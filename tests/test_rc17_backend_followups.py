"""Retained backend review findings use the same versioned rules and viewer scope as collections."""
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from apps.api.services.scoring_defaults import line_id, seed
from packages.contracts.schemas import TypeDefinition
from .governance_support import identity
from .legacy_lead import set_historical_lead
from .test_relative_filter_api import post, relative


DATABASES = ["h", pytest.param("pg", marks=pytest.mark.postgres)]


@pytest.mark.parametrize("database", DATABASES)
@pytest.mark.parametrize("workspace", [False, True])
def test_seeded_line_reach_details_accept_the_visible_version_bound_line(request, database, workspace):
    h = request.getfixturevalue(database)
    set_historical_lead(h, h.seed["type"], {"minimum": 1, "maximum": 1})
    with transaction(h.app.state.engine) as db:
        source = db.scalar(select(m.Policy).where(m.Policy.department_id == h.seed["department_id"],
            m.Policy.definition["rule"]["id"].as_string() == "fair_share"))
        retained = record(source)
        seeded_id = line_id(h.seed["department_id"], "lead:" + h.seed["type"])
        assert seeded_id not in {line["id"] for line in source.definition["rule"]["lines"]}
    visible = next(entry['rule'] for entry in h.get('coordinator', '/api/rules/catalogue')['entries']
                   if entry['id'] == 'fair_share')
    assert visible['id'] == retained['id'] and visible['revision'] == retained['revision']
    assert seeded_id in {line['id'] for line in visible['definition']['rule']['lines']}
    rota = h.workspace() if workspace else None
    query = f"/api/policies/reach?day=2026-10-04&details=true&policy_id={retained['id']}&line_id={seeded_id}"
    if rota:
        query += "&workspace_id=" + rota["id"]
    result = h.get("coordinator", query)
    assert [line["line_id"] for line in result["details"][retained["id"]]["lines"]] == [seeded_id]
    if rota:
        assert (result["starts_on"], result["ends_on"]) == (rota["starts_on"], rota["ends_on"])
    h.get("coordinator", query.replace(seeded_id, str(uuid4())), status=404)
    with transaction(h.app.state.engine) as db:
        assert record(db.get(m.Policy, retained["id"])) == retained


@pytest.mark.parametrize("database", DATABASES)
@pytest.mark.parametrize("linked", [False, True])
def test_programme_relative_tokens_union_owned_and_as_of_membership_programmes(request, database, linked):
    h = request.getfixturevalue(database)
    person = h.seed["people"][0] if linked else None
    owner = identity(h, "relative_owner", ["programme_lead"], person=person)
    peer = identity(h, "relative_peer", ["programme_lead"])
    with transaction(h.app.state.engine, write=True) as db:
        programmes = [m.Programme(department_id=h.seed["department_id"], name=name,
            owner_identity_id=authority, starts_on=start, ends_on=end, active=active)
            for name, authority, start, end, active in [
                ("Owned programme", owner, date(2020, 1, 1), None, True),
                ("Member programme", None, date(2020, 1, 1), None, True),
                ("Other owner's programme", peer, date(2020, 1, 1), None, True),
                ("Inactive owned programme", owner, date(2020, 1, 1), None, False),
                ("Future owned programme", owner, date(2027, 1, 1), None, True),
                ("Ended owned programme", owner, date(2020, 1, 1), date(2026, 10, 1), True),
            ]]
        db.add_all(programmes)
        db.flush()
        cohorts = [m.Cohort(department_id=h.seed["department_id"], programme_id=programme.id,
            name="Cohort " + programme.name, starts_on=date(2026, 1, 1), ends_on=date(2027, 1, 1))
            for programme in programmes]
        db.add_all(cohorts)
        if person:
            db.add(m.ProgrammeMembership(department_id=h.seed["department_id"], person_id=person,
                programme_id=programmes[1].id, starts_on=date(2026, 10, 18), ends_on=date(2026, 10, 20)))
        db.flush()
        programme_ids, cohort_ids = [row.id for row in programmes], [row.id for row in cohorts]
    for day in ("2026-10-17", "2026-10-18", "2026-10-20"):
        indices = [0, 1] if linked and day == "2026-10-18" else [0]
        for token, field, identifiers in (("my_programme", "programme_ids", programme_ids),
                                           ("my_cohort", "cohort_ids", cohort_ids)):
            resolved = post(h, "/api/conditions/resolve-operand", {"operand": relative(token),
                "field": field, "as_of": day}, role="relative_owner")
            assert resolved["value"] == sorted(identifiers[index] for index in indices)
    peer_tokens = h.get("relative_peer", "/api/conditions/catalogue?as_of=2026-10-18")["tokens"]
    assert {"my_programme", "my_cohort"} <= {entry["token"] for entry in peer_tokens}


@pytest.mark.parametrize("database", DATABASES)
def test_policy_line_average_revision_uses_row_metadata_and_visible_seeded_lines(request, database):
    h = request.getfixturevalue(database)
    with transaction(h.app.state.engine, write=True) as db:
        duty = db.get(m.AssignmentType, h.seed["type"])
        duty.definition = TypeDefinition.model_validate({**duty.definition,
            "lead": {"minimum": 1, "maximum": 1}}).model_dump(mode="json")
        seed(db, h.seed["department_id"], types=[duty], workload=False)
        db.add(m.Policy(department_id=h.seed["department_id"], name="Revision metadata fixture", kind="catalogue",
            revision=7, starts_on=date(2020, 1, 1), created_by=h.seed["identities"]["coordinator"],
            owner="Synthetic owner", source="Synthetic test", definition={"scope": {}, "rule": {
                "kind": "catalogue", "id": "duties_per_week", "lines": [
                    {"id": str(uuid4()), "numbers": {"max": {"value": 3}, "target": {"value": 5}}}]}}))
    rows = post(h, "/api/bulk/rows", {"selection": {"collection": "policy_lines"}, "columns": ["revision"]})["rows"]
    resolved = post(h, "/api/conditions/resolve-operand", {"operand": relative("department_average"),
        "collection": "policy_lines", "field": "revision"})
    assert resolved["value"] == sum(row["revision"] for row in rows) / len(rows)
    tokens = h.get("coordinator", "/api/conditions/catalogue?collection=policy_lines&field=revision")["tokens"]
    assert "its_target" not in {entry["token"] for entry in tokens}
