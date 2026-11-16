from dataclasses import replace
import uuid
from fastapi.responses import Response
from fastapi.testclient import TestClient
import pytest
from starlette.routing import Route
from apps.api.settings import Settings
from packages.contracts.schemas import EXPLICIT_ROLES, ROLE_NAMES

@pytest.mark.parametrize("path",["/api/me","/api/catalogues","/api/workspaces","/api/publications","/api/reports/unknown","/api/notifications","/api/access","/api/audit","/api/operations","/api/imports","/api/jobs","/api/openapi.json","/api/contracts"])
def test_anonymous_api_is_not_public(h,path):
    with TestClient(h.app,base_url=h.settings.public_origin) as c:
        assert c.get(path).status_code==401

@pytest.mark.parametrize("path",["/api/catalogues","/api/workspaces","/api/access","/api/audit","/api/operations","/api/imports","/api/jobs"])
def test_staff_cannot_read_privileged_surfaces(h,path):
    h.get("staff",path,status=403)

def test_audit_names_are_from_the_recorded_actor_without_identity_secrets(h):
    h.workspace('Audit name check')
    event=next(e for e in h.get('auditor','/api/audit')['events'] if e['action']=='workspace.create')
    assert event['actor_id']==h.seed['identities']['coordinator']
    assert event['actor_display_name']==h.get('coordinator','/api/me')['display_name']
    assert not {'password_hash','subject','issuer'} & event.keys()

@pytest.mark.parametrize("role,kind",[("staff","workspace.create"),("coordinator","grant.create"),("credentialer","workspace.publish"),("operator","policy.activate"),("approver","catalogue.create")])
def test_server_permissions_not_ui_visibility(h,role,kind):
    assert h.command(role,kind,{"catalogue":"categories"},status=403)["error"]["code"]=="FORBIDDEN"


def test_admin_holds_department_roles_manages_access_alone_but_never_removes_the_last_admin(h):
    assert set(h.get("admin","/api/me")["roles"]) == set(ROLE_NAMES) - EXPLICIT_ROLES
    # An administrator changes their own access without asking another administrator.
    h.command("admin","role.grant",{"identity_id":h.seed["identities"]["admin"],"role":"approver","starts_on":"2020-01-01","reason":"Own approver seat"})
    # They can make someone else an administrator, and the department can never be left without one.
    own=next(b for b in h.get("admin","/api/access")["bindings"] if b["identity_id"]==h.seed["identities"]["admin"] and b["role"]=="admin" and not b["revoked"])
    assert h.command("admin","role.revoke",{"reason":"Leaving"},own["id"],own["revision"],status=409)["error"]["code"]=="LAST_ADMIN"
    assert h.command("admin","identity.disable",{"reason":"Leaving"},h.seed["identities"]["admin"],1,status=409)["error"]["code"]=="LAST_ADMIN"
    # An administrator whose access ends does not count as cover.
    h.command("admin","role.grant",{"identity_id":h.seed["identities"]["staff"],"role":"admin","starts_on":"2020-01-01","ends_on":"2099-01-01","reason":"Temporary administrator"})
    assert h.command("admin","role.revoke",{"reason":"Leaving"},own["id"],own["revision"],status=409)["error"]["code"]=="LAST_ADMIN"
    h.command("admin","role.grant",{"identity_id":h.seed["identities"]["coordinator"],"role":"admin","starts_on":"2020-01-01","reason":"Second administrator"})
    h.command("admin","role.revoke",{"reason":"Handing over"},own["id"],own["revision"])


def test_forged_approver_actor_fields_rejected(h):
    w=h.shift(h.workspace())["workspace"];w=h.lifecycle(w,"submit")["workspace"]
    h.command("approver","workspace.approve",{"reason":"Real logged-in actor only","approved_by":h.seed["identities"]["admin"]},w["id"],w["revision"],status=422)
    assert h.detail(w)["workspace"]["status"]=="submitted"


def test_origin_csrf_and_idempotency_requirements(h):
    c=h.client("coordinator");body={"kind":"workspace.create","payload":{"name":"No CSRF","starts_on":"2026-10-04","ends_on":"2026-10-11"}}
    assert c.post("/api/commands",json=body,headers={"origin":"https://attacker.invalid"}).status_code==403
    assert c.post("/api/commands",json=body,headers={"x-csrf-token":"invalid","idempotency-key":str(uuid.uuid4())}).status_code==403
    assert c.post("/api/commands",json=body).status_code==428


def test_logout_revokes_database_session_and_clears_cookie(h):
    c=h.client("staff");token=c.cookies["rota_session"]
    assert c.post("/auth/logout").status_code==200
    c.cookies.set("rota_session",token)
    assert c.get("/api/me").status_code==401


def test_private_directory_reference_and_git_never_static_assets(h):
    with TestClient(h.app,base_url=h.settings.public_origin) as c:
        for path in ("/reference/rota.zip","/.git/config","/.run/demo-credentials.json","/apps/api/models.py","/assets/../../reference/rota.zip"):
            assert c.get(path).status_code==404
        response=c.get("/")
        assert response.headers["x-frame-options"]=="DENY"
        assert "script-src 'self'" in response.headers["content-security-policy"]
        assert "unsafe-inline" not in response.headers["content-security-policy"]


def test_unexpected_calendar_failure_logs_route_template_without_private_token(h, monkeypatch, caplog):
    def unavailable(db, token):
        raise RuntimeError('Synthetic calendar failure')

    monkeypatch.setattr('apps.api.services.calendar_feeds.feed_calendar', unavailable)
    with TestClient(h.app, base_url=h.settings.public_origin, raise_server_exceptions=False) as client:
        response = client.get('/calendar/synthetic-private-calendar-token.ics')
    assert response.status_code == 500
    event, = [r for r in caplog.records if r.name == 'rota.api' and r.message == 'unhandled_error']
    assert event.path == '/calendar/{token}.ics'
    assert 'synthetic-private-calendar-token' not in caplog.text


@pytest.mark.parametrize("path,status,policy,expected", [
    ("/cache-policy-test", 200, "private, no-cache", "private, no-cache"),
    ("/cache-policy-test", 304, "private, no-cache", "private, no-cache"),
    ("/assets/cache-policy-test", 200, "private, no-store", "private, no-store"),
    ("/cache-policy-test", 200, None, "no-store"),
    ("/assets/cache-policy-test", 200, None, "no-cache"),
    ("/assets/cache-policy-test", 404, None, "no-store"),
])
def test_security_headers_preserve_explicit_cache_control_and_default_otherwise(h, path, status, policy, expected):
    async def probe(request):
        return Response(status_code=status, headers={"cache-control": policy} if policy is not None else {})

    h.app.router.routes.insert(0, Route(path, probe))
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        response = client.get(path)
        assert response.status_code == status
        assert response.headers["cache-control"] == expected


def test_login_validation_does_not_reflect_password_input(h):
    with TestClient(h.app,base_url=h.settings.public_origin) as c:
        response=c.post("/auth/login",headers={"origin":h.settings.public_origin},json={"username":"","password":"private-sensitive-password"})
        assert response.status_code==422
        assert "private-sensitive-password" not in response.text


def test_staff_person_export_is_scoped(h):
    saved=h.shift(h.workspace(),people=h.seed["people"][:2]);pub=h.publish(saved["workspace"])["publication_id"]
    personal=h.get("staff","/api/publications/"+pub)
    assert set(personal["snapshot"]["people"])=={h.seed["people"][0]}
    assert len(personal["snapshot"]["shifts"][0]["participants"])==1
    h.get("staff",f"/api/reports/{pub}?person_id={h.seed['people'][1]}",status=403)

@pytest.mark.parametrize("patch",[{}, {"database_url":"postgresql+psycopg://user:pass@db/test"},{"database_url":"postgresql+psycopg://user:pass@db/test","public_origin":"https://rota.example.invalid"}])
def test_production_fails_closed_without_explicit_institution_configuration(patch):
    with pytest.raises(RuntimeError):Settings(environment="production",**patch).check()
