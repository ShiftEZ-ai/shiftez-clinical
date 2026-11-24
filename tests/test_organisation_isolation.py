"""Two organisations on one deployment: nothing of one reaches the other, and probing it changes nothing.

The fixture keeps the seeded organisation, adds a second department inside it through the department command, and
adds a second organisation with its own department, people, sign-ins and catalogue. On PostgreSQL the API runs as
the production-shaped non-owner role from infra/database-roles.sql; setup and the side-effect digest use the owner.
"""
from datetime import date
import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api import models as m
from apps.api.auth import PASSWORDS
from apps.api.db import transaction
from apps.api.services.contacts import seed as seed_contacts
from apps.api.services.event_defaults import seed_department
from apps.api.services.leave_types import seed as seed_leave_types
from apps.api.services.scoring_defaults import seed as seed_scoring_defaults
from apps.api.settings import new_department_session_limits
from packages.contracts.schemas import TypeDefinition
from .conftest import TEST_PASSWORD
from .test_department_create import name_test_administrators, payload, token
from .test_postgres_roles import runtime  # noqa: F401 (the production-shaped role harness)

# What these checks do not cover yet; each line leaves when a check for it lands.
KNOWN_GAPS = (
    "No command creates an organisation: the fixture inserts the second one as an operator tool would.",
    "Isolation rests on query filters alone: there is no row-level security backstop yet.",
    "An identity bound in both organisations, live streams beyond one pulse, push, email and imports are not probed.",
)
ROLES = ("admin", "coordinator", "approver", "staff", "auditor", "operator")
OTHER = "b_"  # sign-in prefix of the second organisation
READS = ("/api/me", "/api/home", "/api/workspaces", "/api/jobs", "/api/directory", "/api/content", "/api/requests",
         "/api/requests/all", "/api/access", "/api/audit", "/api/notifications", "/api/calendar", "/api/publications",
         "/api/swaps", "/api/wishes", "/api/availability", "/api/catalogues", "/api/policies", "/api/rotation-board",
         "/api/schedule/sessions", "/api/events?once=true")
EFFECTIVE = {"starts_on": date(2020, 1, 1), "active": True}


def second_organisation(db, password_hash):
    """A second tenant shaped like the seeded one (scripts/manage.py demo_data), with prefixed sign-ins."""
    organisation = m.Organisation(name="Second synthetic organisation", timezone="UTC", week_start=1)
    db.add(organisation); db.flush()
    department = m.Department(organisation_id=organisation.id, name="Second organisation department (test)",
                              **new_department_session_limits())
    db.add(department); db.flush()
    seed_department(db, department)
    seed_leave_types(db, department.id)
    seed_contacts(db, department.id)
    people = [m.Person(organisation_id=organisation.id, display_name=f"Fictional member B{n}") for n in (1, 2)]
    db.add_all(people); db.flush()
    identities = {}
    for role in ROLES[:5]:
        row = m.Identity(issuer="local-development", subject=OTHER + role, display_name=f"Fictional {OTHER}{role}",
                         password_hash=password_hash, person_id=people[0].id if role == "staff" else None)
        db.add(row); db.flush(); identities[OTHER + role] = row.id
        db.add(m.RoleBinding(identity_id=row.id, department_id=department.id, role=role, starts_on=date(2020, 1, 1)))
    seed_scoring_defaults(db, department.id)
    effective = {"department_id": department.id, **EFFECTIVE}
    location_type = m.LocationType(name="Second organisation room type", description="Synthetic", **effective)
    service = m.Service(name="Second organisation clinic", description="Synthetic", **effective)
    typ = m.AssignmentType(name="Second organisation session", description="Synthetic",
                           definition=TypeDefinition(maximum_duration_minutes=960).model_dump(mode="json"),
                           **effective)
    db.add_all([location_type, service, typ]); db.flush()
    location = m.Location(name="Second organisation room", location_type_id=location_type.id, capacity=2, **effective)
    db.add(location); db.flush()
    return {"organisation_id": organisation.id, "department_id": department.id, "people": [p.id for p in people],
            "identities": identities, "service": service.id, "location": location.id, "type": typ.id}


def digest(engine, organisation_id, department_ids):
    """Every row the organisation owns directly or through its departments, as one hash per table."""
    out = {}
    with engine.connect() as db:
        for name, table in sorted(m.Base.metadata.tables.items()):
            columns = table.columns
            if "department_id" in columns:
                where = columns.department_id.in_(department_ids)
            elif "organisation_id" in columns:
                where = columns.organisation_id == organisation_id
            elif name == "organisations":
                where = columns.id == organisation_id
            else:
                continue
            rows = db.execute(select(table).where(where).order_by(*table.primary_key.columns)).all()
            out[name] = hashlib.sha256(repr(rows).encode()).hexdigest()
    return out


def body(response):
    return response.text if response.headers.get("content-type", "").startswith(("application/json", "text/")) else ""


def leaks(h, roles, markers, paths=READS):
    """Each response that carries one of the other organisation's ids or texts, as (role, path, marker)."""
    found = []
    for role in roles:
        client = h.client(role)
        for path in paths:
            text = body(client.get(path))
            found += [(role, path, marker) for marker in markers if marker in text]
    return found


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def tenants(request):
    if request.param == "sqlite":
        h = request.getfixturevalue("h")
        owner = h.app.state.engine
    else:
        h = request.getfixturevalue("runtime")
        owner = request.getfixturevalue("pg").app.state.engine
    with transaction(owner, write=True) as db:
        password_hash = PASSWORDS.hash(TEST_PASSWORD)
        other = second_organisation(db, password_hash)
    h.seed["identities"].update(other["identities"])
    name_test_administrators(h)
    value = payload(h, name="Second department (test)", reason="A sibling department inside the seeded organisation")
    sibling = h.command("operator", "department.create", value, expected_fingerprint=token(h, value))["department"]
    return h, owner, other, sibling


def content(h, prefix, service, location, typ, person, marker):
    """A private draft with a shift, a leave request, an announcement, a calendar feed and a finished job."""
    w = h.command(prefix + "coordinator", "workspace.create", {"name": marker + " draft", "starts_on": "2026-10-04",
                                                                "ends_on": "2026-10-11"})["workspace"]
    w = h.command(prefix + "coordinator", "shift.create", {
        "workspace_id": w["id"], "assignment_type_id": typ, "location_id": location, "service_ids": [service],
        "start": "2026-10-05T08:00:00+00:00", "end": "2026-10-05T12:00:00+00:00", "attributes": {},
        "participants": [{"person_id": person, "start": "2026-10-05T08:00:00+00:00",
                          "end": "2026-10-05T12:00:00+00:00"}],
        "override_reason": "Synthetic assignment"}, revision=w["revision"])["workspace"]
    request = h.command(prefix + "staff", "request.create", {
        "person_id": person, "kind": "leave", "start": "2026-10-06T00:00:00+00:00", "end": "2026-10-06T23:59:00+00:00",
        "private_reason": marker + " reason"})["record"]
    notice = h.configure("department_content", {"name": marker + " notice", "description": marker + " text",
                                                "starts_on": "2020-01-01", "audience": {"roles": ["staff"]},
                                                "notify": False}, role=prefix + "coordinator")["record"]
    feed = h.command(prefix + "staff", "calendar_feed.create", {})["path"]
    detail = h.get(prefix + "coordinator", "/api/workspaces/" + w["id"])
    w, shift = detail["workspace"], detail["shifts"][0]["id"]
    job = h.command(prefix + "coordinator", "job.create", {"workspace_id": w["id"], "kind": "generation"},
                    revision=w["revision"])["job"]
    h.process_jobs()
    return {"workspace": w["id"], "shift": shift, "request": request["id"], "notice": notice["id"], "feed": feed,
            "job": job["id"]}


def test_two_organisations_see_nothing_of_each_other_and_probes_change_nothing(tenants):
    h, owner, other, sibling = tenants
    seed = h.seed
    mine = content(h, "", seed["services"][0], seed["locations"][0], seed["type"], seed["people"][0], "First-org")
    theirs = content(h, OTHER, other["service"], other["location"], other["type"], other["people"][0], "Second-org")
    first_ids = {seed["organisation_id"], seed["department_id"], sibling["id"], *seed["people"],
                 *(v for v in mine.values() if not v.startswith("/"))}
    second_ids = {other["organisation_id"], other["department_id"], *other["people"], *other["identities"].values(),
                  *(v for v in theirs.values() if not v.startswith("/"))}

    # Lists, search, counts, history and feeds read from each side carry nothing of the other. The second
    # organisation reads first: its own sign-ins and reads are its activity, not a side effect of the probes.
    assert leaks(h, [OTHER + r for r in ROLES[:5]], first_ids | {"First-org"},
                 READS + ("/api/requests/all?q=First-org",)) == []
    job = h.get(OTHER + "coordinator", "/api/jobs/" + theirs["job"])["job"]
    assert job["status"] in ("succeeded", "completed", "failed") and not any(i in json.dumps(job) for i in first_ids)
    before = digest(owner, other["organisation_id"], [other["department_id"]])
    searches = ("/api/requests/all?q=Second-org", "/api/search/references?collection=people&kind=people&q=Fictional")
    assert leaks(h, ROLES, second_ids | {"Second-org", "Fictional member B"}, READS + searches) == []
    feed = TestClient(h.app, base_url=h.settings.public_origin)
    assert not any(marker in feed.get(mine["feed"]).text for marker in second_ids | {"Second-org"})

    # Direct fetches of the other organisation's records find nothing, whoever asks.
    direct = ["/api/workspaces/" + theirs["workspace"], "/api/workspaces/" + theirs["workspace"] + "/summary",
              "/api/workspaces/" + theirs["workspace"] + "/review", "/api/jobs/" + theirs["job"],
              "/api/jobs/" + theirs["job"] + "/evidence", "/api/content/" + theirs["notice"] + "/receipts",
              "/api/people/" + other["people"][0] + "/rotations?start=2026-10-01&end=2026-10-31",
              "/api/people/" + other["people"][0] + "/scheduling-facts",
              f"/api/workspaces/{theirs['workspace']}/shifts/{theirs['shift']}/history"]
    for role in ROLES:
        for path in direct:
            response = h.client(role).get(path)
            assert response.status_code in (403, 404), (role, path, response.status_code)

    # Writes aimed at the other organisation are refused and leave every row it owns as it was.
    refused = (403, 404, 409, 422)
    h.command("coordinator", "workspace.archive", {"reason": "Cross-organisation attempt"}, theirs["workspace"], 1,
              status=refused)
    h.command("coordinator", "job.cancel", {}, theirs["job"], 1, status=refused)
    h.command("approver", "request.decide", {"decision": "approve", "reason": "Cross-organisation attempt"},
              theirs["request"], 1, status=refused)
    h.command("admin", "role.grant", {"identity_id": other["identities"][OTHER + "staff"], "role": "staff",
                                      "starts_on": "2020-01-01", "reason": "Cross-organisation attempt"},
              status=refused)
    after = digest(owner, other["organisation_id"], [other["department_id"]])
    assert [name for name in before if after[name] != before[name]] == []
