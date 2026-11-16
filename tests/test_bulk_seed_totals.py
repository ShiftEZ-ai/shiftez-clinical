"""Collection totals include the same version-bound defaults as resolved and paged rule lines."""
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.scoring_defaults import line_id, seed
from packages.contracts.schemas import TypeDefinition
from .test_bulk_linked_people import post


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("status", ["active", "proposed", "retired"])
def test_seeded_rule_line_totals_match_resolve_and_rows_without_changing_scope_or_status(request, database, status):
    h = request.getfixturevalue(database)
    baseline = {collection: post(h, "resolve", {"collection": collection})["count"]
                for collection in ("policies", "policy_lines")}
    with transaction(h.app.state.engine, write=True) as db:
        duty = db.get(m.AssignmentType, h.seed["type"])
        duty.definition = TypeDefinition.model_validate({**duty.definition,
            "lead": {"minimum": 1, "maximum": 1}}).model_dump(mode="json")
        seed(db, h.seed["department_id"], types=[duty], workload=False)
        source = db.scalar(select(m.Policy).where(m.Policy.department_id == h.seed["department_id"],
            m.Policy.definition["rule"]["id"].as_string() == "fair_share"))
        seeded_id = line_id(h.seed["department_id"], "lead:" + duty.id)
        seed_row = db.get(m.PolicySeedLine, seeded_id)
        assert seed_row.policy_id == source.id and seed_row.policy_revision == source.revision
        assert seeded_id not in {line["id"] for line in source.definition["rule"]["lines"]}
        source.status, source.active = status, status != "retired"
        other = m.Department(organisation_id=h.seed["organisation_id"], name="Other seeded department")
        db.add(other)
        db.flush()
        foreign = m.Policy(department_id=other.id, name="Foreign seeded rule", kind="catalogue",
            definition=deepcopy(source.definition), status=status, active=source.active,
            starts_on=source.starts_on, owner="Department", source="Synthetic totals scope",
            created_by=h.seed["identities"]["coordinator"])
        db.add(foreign)
        db.flush()
        foreign_line = {**deepcopy(seed_row.line), "id": str(uuid4())}
        db.add(m.PolicySeedLine(id=foreign_line["id"], department_id=other.id, policy_id=foreign.id,
            policy_revision=foreign.revision, line=foreign_line))

    totals = h.get("coordinator", "/api/bulk/collections")["collections"]
    for collection in ("policies", "policy_lines"):
        selection = {"collection": collection}
        found = post(h, "resolve", selection)
        expected = baseline[collection] + (collection == "policy_lines")
        assert totals[collection]["count"] == found["count"] == expected
        pages = [post(h, "rows", {"selection": selection, "columns": ["status"], "limit": 1, "offset": offset})
                 for offset in range(expected + 1)]
        assert all(page["count"] == expected for page in pages)
        assert sum(len(page["rows"]) for page in pages) == expected and not pages[-1]["rows"]
        assert all(row["status"] == status for page in pages for row in page["rows"])
        if collection == "policy_lines":
            actual = {(row["policy_id"], row["id"]) for page in pages for row in page["rows"]}
            assert (source.id, seeded_id) in actual
            assert not any(policy_id == foreign.id for policy_id, _ in actual)
            filtered = {"collection": collection, "where": {"filters": {"status": [status]}}}
            assert post(h, "resolve", filtered)["count"] == post(h, "rows", {"selection": filtered})["count"] == expected
    assert not {"policies", "policy_lines"} & h.get("staff", "/api/bulk/collections")["collections"].keys()
