"""Remember duration is an audited environment setting, fixed at authentication."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from apps.api import auth, models as m
from apps.api.db import transaction
from apps.api.errors import Problem
from tests.test_signin_config import PATH, payload
from tests.test_signin_password import attempt
from tests.conftest import TEST_PASSWORD


def test_remember_duration_defaults_validates_audits_and_undoes(h):
    admin = h.client("admin")
    assert admin.get(PATH).json()["remember_me_days"] == 30
    for invalid in (0, 91):
        assert admin.put(PATH, json=payload(remember_me_days=invalid)).status_code == 422
    saved = admin.put(PATH, json=payload(remember_me_days=7))
    assert saved.status_code == 200 and saved.json()["remember_me_days"] == 7
    with transaction(h.app.state.engine) as db:
        event = db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == "auth.config.update"))
        assert "remember_me_days" in event.detail["changed_fields"]
    undone = admin.post(PATH + "/undo", json=payload(expected_revision=1))
    assert undone.status_code == 200 and undone.json()["remember_me_days"] == 30


@pytest.mark.parametrize("days", [1, 7, 90])
def test_remembered_session_honors_setting_across_idle_rotation_and_policy_changes(h, monkeypatch, days):
    admin = h.client("admin")
    assert admin.put(PATH, json=payload(remember_me_days=days)).status_code == 200
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed["department_id"])
        department.session_idle_minutes, department.session_max_hours = 5, 1
    result = attempt(h, h.settings, "staff", TEST_PASSWORD, remember_me=True)
    assert not isinstance(result, Problem)
    with transaction(h.app.state.engine) as db:
        session = db.get(m.Session, result["session_id"])
        created = auth.aware(session.created_at)
        deadline = created + timedelta(days=days)
        assert auth.aware(session.expires_at) == deadline
        assert auth.aware(result["cookie_expires_at"]) == deadline
    # Changing the setting applies to future authentication, never extending an existing session.
    assert admin.put(PATH, json=payload(expected_revision=1, remember_me_days=30)).status_code == 200
    moment = [created + timedelta(minutes=10)]
    monkeypatch.setattr(auth, "now", lambda: moment[0])
    auth.keep_alive(h.app.state.engine, result["token"], h.settings)
    with transaction(h.app.state.engine, write=True) as db:
        actor = auth.authenticate(db, result["token"], settings=h.settings)
        auth.reconcile_session_limits(db, actor.department_id, (5, 1), (1, 1))
        rotated = auth.rotate_session(db, actor, h.settings)
        session = db.get(m.Session, result["session_id"])
        assert auth.aware(rotated["cookie_expires_at"]) == deadline
        assert auth.aware(session.expires_at) == deadline
        assert session.created_at == created.replace(tzinfo=session.created_at.tzinfo)
    moment[0] = deadline
    with transaction(h.app.state.engine) as db:
        with pytest.raises(Problem) as expired:
            auth.authenticate(db, rotated["token"], settings=h.settings)
        assert expired.value.code == "SESSION_EXPIRED"


def test_existing_idle_capped_remembered_session_keeps_its_earlier_deadline(h, monkeypatch):
    result = attempt(h, h.settings, "staff", TEST_PASSWORD, remember_me=True)
    with transaction(h.app.state.engine, write=True) as db:
        session = db.get(m.Session, result["session_id"])
        created = auth.aware(session.created_at)
        deadline = created + timedelta(minutes=10)
        session.expires_at = deadline  # A remembered session issued by the earlier idle-capped implementation.
    moment = [created + timedelta(minutes=5)]
    monkeypatch.setattr(auth, "now", lambda: moment[0])
    auth.keep_alive(h.app.state.engine, result["token"], h.settings)
    with transaction(h.app.state.engine, write=True) as db:
        actor = auth.authenticate(db, result["token"], settings=h.settings)
        auth.reconcile_session_limits(db, actor.department_id, (10, 8), (None, None))
        rotated = auth.rotate_session(db, actor, h.settings)
        assert auth.aware(rotated["cookie_expires_at"]) == deadline
        assert auth.aware(db.get(m.Session, result["session_id"]).expires_at) == deadline
    moment[0] = deadline
    with transaction(h.app.state.engine) as db:
        with pytest.raises(Problem) as expired:
            auth.authenticate(db, rotated["token"], settings=h.settings)
        assert expired.value.code == "SESSION_EXPIRED"
