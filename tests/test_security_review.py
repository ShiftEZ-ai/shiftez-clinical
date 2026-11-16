"""Pre-release security review regressions: independence gaps, cross-organisation access, feeds, sign-in flows, live streams."""
from dataclasses import replace
from datetime import date, timedelta
from fastapi.testclient import TestClient
from apps.api import models as m
from apps.api import oidc
from apps.api.db import transaction
from .governance_support import identity


def absence(person):
    return {"person_id": person, "kind": "recorded_leave", "start": "2026-10-04T08:00:00+03:00",
            "end": "2026-10-04T12:00:00+03:00", "private_reason": "Personal appointment"}


def pattern(person):
    return {"person_id": person, "weekdays": [6], "start_time": "08:00", "end_time": "12:00", "starts_on": "2026-09-01"}


def test_coordinator_cannot_record_or_approve_their_own_absence(h):
    mine, other = h.seed["people"][1], h.seed["people"][2]
    identity(h, "coord_self", ["coordinator"], person=mine)
    assert h.command("coord_self", "availability.record", absence(mine), status=403)["error"]["code"] == "SELF_APPROVAL"
    h.command("coord_self", "availability.record", absence(other))
    # Their own weekly pattern waits for someone else instead of taking effect at once.
    own = h.command("coord_self", "availability_pattern.propose", pattern(mine))["pattern"]
    assert own["status"] == "proposed"
    refused = h.command("coord_self", "availability_pattern.decide", {"decision": "approve", "reason": "Fine"}, own["id"], 1, status=403)
    assert refused["error"]["code"] == "SELF_APPROVAL"
    assert h.command("coordinator", "availability_pattern.decide", {"decision": "approve", "reason": "Agreed"}, own["id"], 1)["pattern"]["status"] == "active"


def test_credentialer_cannot_make_themselves_eligible_for_a_service(h):
    mine = h.seed["people"][1]
    identity(h, "cred_self", ["credentialer"], person=mine)
    payload = {"person_id": mine, "service_id": h.seed["services"][0], "starts_on": "2031-01-01"}
    assert h.command("cred_self", "eligibility.create", payload, status=403)["error"]["code"] == "SELF_APPROVAL"


def test_admin_cannot_see_or_grant_identities_of_another_organisation(h):
    with transaction(h.app.state.engine, write=True) as db:
        org = m.Organisation(name="Other hospital", timezone="Asia/Riyadh", week_start=6)
        db.add(org); db.flush()
        dept = m.Department(organisation_id=org.id, name="Elsewhere")
        db.add(dept); db.flush()
        outsider = m.Identity(issuer="local-development", subject="outsider", display_name="Other org person")
        db.add(outsider); db.flush()
        db.add(m.RoleBinding(identity_id=outsider.id, department_id=dept.id, role="staff", starts_on=date(2020, 1, 1)))
        outsider_id = outsider.id
    listed = {i["id"] for i in h.get("admin", "/api/access")["identities"]}
    assert outsider_id not in listed and h.seed["identities"]["staff"] in listed
    grant = {"identity_id": outsider_id, "role": "staff", "starts_on": "2020-01-01", "reason": "Cross-organisation attempt"}
    assert h.command("admin", "role.grant", grant, status=404)["error"]["code"] == "NOT_FOUND"


def test_calendar_link_stops_when_department_access_ends(h):
    path = h.command("staff", "calendar_feed.create", {})["path"]
    client = TestClient(h.app, base_url=h.settings.public_origin)
    assert client.get(path).status_code == 200
    staff = h.seed["identities"]["staff"]
    binding = next(b for b in h.get("admin", "/api/access")["bindings"] if b["identity_id"] == staff and not b["revoked"])
    h.command("admin", "role.revoke", {"reason": "Left the department"}, binding["id"], binding["revision"])
    assert client.get(path).status_code == 404


def test_expired_sign_in_flows_are_cleared_when_a_new_one_starts(h):
    engine = h.app.state.engine
    settings = replace(h.settings, oidc_redirect_uri=h.settings.public_origin + "/auth/callback",
                       oidc_allowed_hosts="idp.invalid")
    with transaction(engine, write=True) as db:
        db.add(m.OIDCFlow(state_hash="a" * 64, browser_hash="b" * 64, nonce="n", verifier="v", expires_at=m.now() - timedelta(minutes=1)))
    with transaction(engine, write=True) as db:
        oidc.start_flow(db, settings, {"authorization_endpoint": "https://idp.invalid/authorize"})
    with transaction(engine) as db:
        assert db.query(m.OIDCFlow).filter(m.OIDCFlow.state_hash == "a" * 64).count() == 0
        assert db.query(m.OIDCFlow).count() == 1


def test_finished_live_streams_do_not_count_against_the_open_stream_cap(h):
    client = h.client("staff")
    for _ in range(15):  # more than the per-person cap: each finished stream must give its place back
        response = client.get("/api/events?once=true")
        assert response.status_code == 200 and "event: pulse" in response.text


def test_database_errors_never_carry_bound_values_into_logs(h):
    import pytest
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError
    with pytest.raises(SQLAlchemyError) as caught:
        with transaction(h.app.state.engine) as db:
            db.execute(text("SELECT * FROM no_such_table WHERE x = :secret"), {"secret": "private-reason-text"})
    assert "private-reason-text" not in str(caught.value)
