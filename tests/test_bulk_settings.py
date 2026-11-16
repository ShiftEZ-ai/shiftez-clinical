"""Operator limits and rate accounting at the real HTTP command boundary."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.auth import digest
from apps.api.db import transaction
from apps.api.settings import Settings
from apps.api.services import bulk_settings
from packages.contracts import schemas as s
from .clock_support import freeze_clock
from .test_bulk import people_change, preview


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_department_settings_preserve_bulk_and_swap_controls_together(request, database):
    h = request.getfixturevalue(database)
    before = h.get('admin', '/api/me')
    org, dept = before['organisation'], before['department']
    original_bulk = h.get('admin', '/api/bulk/settings')['settings']
    base = {'department_name': dept['name'], 'department_revision': dept['revision'],
            'organisation_name': org['name'], 'timezone': org['timezone'], 'week_start': org['week_start'],
            'reason': 'Keep bulk and swap settings in the same department'}
    bulk = {**original_bulk, 'bulk_max_items': 100, 'bulk_direct_max': 10}
    saved = h.command('admin', 'department.settings', {**base, 'bulk_settings': bulk,
        'swap_kinds_allowed': ['give'], 'swap_open_offers': False}, dept['id'], org['revision'])
    assert saved['department']['bulk_settings'] == bulk
    assert saved['department']['swap_kinds_allowed'] == ['give']
    assert saved['department']['swap_open_offers'] is False
    assert saved['previous']['bulk_settings'] == original_bulk
    assert saved['previous']['swap_kinds_allowed'] == dept['swap_kinds_allowed']
    assert saved['previous']['swap_open_offers'] == dept['swap_open_offers']
    unchanged = h.command('admin', 'department.settings', {**base,
        'department_revision': saved['department']['revision']}, dept['id'], saved['organisation']['revision'])
    assert unchanged['department'] == saved['department']
    restored = h.command('admin', 'department.settings', {**saved['previous'],
        'reason': 'Restore bulk and swap settings together'}, dept['id'], saved['organisation']['revision'])
    assert h.get('admin', '/api/bulk/settings')['settings'] == original_bulk
    assert restored['department']['swap_kinds_allowed'] == dept['swap_kinds_allowed']
    assert restored['department']['swap_open_offers'] == dept['swap_open_offers']


def test_bulk_settings_are_exposed_validated_audited_and_undoable(h):
    initial = h.get("admin", "/api/bulk/settings")
    assert initial["settings"] == s.BulkSettings().model_dump()
    payload = {"bulk_max_items": 100, "bulk_direct_max": 10, "bulk_staged_days": 7,
               "rate_bulk_preview_per_minute": 2, "reason": "Bound the department bulk workload"}
    changed = h.command("admin", "department.bulk_settings", payload, h.seed["department_id"],
                        initial["department_revision"])
    assert changed["settings"] == {k: v for k, v in payload.items() if k != "reason"}
    h.command("coordinator", "department.bulk_settings", payload, h.seed["department_id"],
              changed["department"]["revision"], status=403)
    h.command("admin", "department.bulk_settings", {**payload, "bulk_max_items": 5},
              h.seed["department_id"], changed["department"]["revision"], status=422)
    undo = changed["undo"]
    h.command("admin", undo["kind"], undo["payload"], undo["target_id"], undo["expected_revision"])
    assert h.get("admin", "/api/bulk/settings")["settings"] == initial["settings"]
    with transaction(h.app.state.engine) as db:
        assert len(list(db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.kind == "department_bulk_settings")))) == 2


def test_bulk_uses_one_command_hit_and_previews_have_a_separate_department_limit(h, monkeypatch):
    freeze_clock(monkeypatch)
    payload = people_change(h)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed["department_id"]).bulk_settings = {"rate_bulk_preview_per_minute": 1}
    preview(h, "coordinator", payload)
    limited = h.client("coordinator").post("/api/commands/preview", json={"kind": "bulk.apply", "payload": payload})
    assert limited.status_code == 429 and limited.json()["error"]["code"] == "RATE_LIMITED"
    h.command("coordinator", "bulk.apply", payload)
    identity = h.seed["identities"]["coordinator"]
    with transaction(h.app.state.engine) as db:
        start = int(m.now().timestamp()) // 60 * 60
        session_id = db.scalar(select(m.Session.id).where(
            m.Session.token_hash == digest(h.client("coordinator").cookies["rota_session"])))
        command_hits = db.get(m.RateLimit, digest(f"commands:{identity}:{start}"))
        preview_hits = db.get(m.RateLimit, digest(f"bulk-preview:{session_id}:{start}"))
        assert command_hits.hits == 1
        assert preview_hits.hits == 2


def test_bulk_maximum_refuses_the_whole_command_and_payload_ceiling(h):
    payload = people_change(h)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed["department_id"]).bulk_settings = {"bulk_max_items": 1, "bulk_direct_max": 1}
    limited = h.command("coordinator", "bulk.apply", payload, status=422)
    assert limited["error"]["code"] == "BULK_ITEM_LIMIT"
    payload["common"]["unused"] = "x" * 1_048_576
    limited = h.command("coordinator", "bulk.apply", payload, status=413)
    assert limited["error"]["code"] == "BULK_PAYLOAD_LIMIT"


def test_bulk_deployment_defaults_parse_and_validate(monkeypatch):
    monkeypatch.setenv("ROTA_BULK_MAX_ITEMS", "350")
    monkeypatch.setenv("ROTA_BULK_DIRECT_MAX", "25")
    monkeypatch.setenv("ROTA_BULK_STAGED_DAYS", "21")
    monkeypatch.setenv("ROTA_RATE_BULK_PREVIEW_PER_MINUTE", "60")
    value = Settings.from_env()
    assert {key: getattr(value, key) for key in bulk_settings.FIELDS} == {
        "bulk_max_items": 350, "bulk_direct_max": 25, "bulk_staged_days": 21, "rate_bulk_preview_per_minute": 60}
    value.check()


def test_bulk_receipt_replay_rechecks_every_original_action_role(h):
    import uuid
    key = str(uuid.uuid4())
    payload = people_change(h)
    h.command("coordinator", "bulk.apply", payload, key=key)
    with transaction(h.app.state.engine, write=True) as db:
        for binding in db.scalars(select(m.RoleBinding).where(
                m.RoleBinding.identity_id == h.seed["identities"]["coordinator"])):
            binding.role = "auditor"
    replay = h.command("coordinator", "bulk.apply", payload, key=key, status=403)
    assert replay["error"]["code"] == "FORBIDDEN"
    h.get("coordinator", "/api/commands/" + key, status=403)
