"""reviewed rota archive, restore and permanent draft-delete contract."""
from uuid import uuid4
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.planning import allowed_actions


REASON = "Retain this rota in audited history"


def _make_status(h, status):
    workspace = h.workspace("Lifecycle fixture")
    workspace = h.shift(workspace)["workspace"]
    if status == "draft":
        return workspace
    workspace = h.lifecycle(workspace, "submit")["workspace"]
    if status == "submitted":
        return workspace
    workspace = h.lifecycle(workspace, "approve", role="approver")["workspace"]
    if status == "approved":
        return workspace
    return h.lifecycle(workspace, "publish")["workspace"]


@pytest.mark.parametrize("status", ["draft", "submitted", "approved", "published"])
def test_archive_and_restore_each_lifecycle_status(h, status):
    workspace = _make_status(h, status)
    archived = h.command("coordinator", "workspace.archive", {"reason": REASON},
                         workspace["id"], workspace["revision"])["workspace"]
    assert archived["archived_at"] and archived["archive_state"]["status"] == status
    assert archived["archived_by_name"] == h.get("coordinator", "/api/me")["display_name"]
    assert "workspace.restore" in archived["allowed_actions"]
    archived_rows = h.get("coordinator", "/api/workspaces?archived_only=true&limit=100")["workspaces"]
    assert workspace["id"] in {row["id"] for row in archived_rows}
    restored = h.command("coordinator", "workspace.restore", {"reason": "Restore audited rota"},
                         workspace["id"], archived["revision"])["workspace"]
    assert restored["status"] == status and restored["archived_at"] is None


@pytest.mark.parametrize("role", ["staff", "approver", "auditor"])
def test_archive_permissions_are_coordinator_or_admin(h, role):
    workspace = _make_status(h, "draft")
    denied = h.command(role, "workspace.archive", {"reason": REASON}, workspace["id"], workspace["revision"], status=403)
    assert denied["error"]["code"] == "FORBIDDEN"


def test_archive_refuses_queued_or_running_jobs(h):
    workspace = _make_status(h, "draft")
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Job(department_id=h.seed["department_id"], workspace_id=workspace["id"],
                     requested_by=h.seed["identities"]["coordinator"], kind="validation", status="queued",
                     input_fingerprint="a" * 64, input_snapshot={}))
    refused = h.command("coordinator", "workspace.archive", {"reason": REASON},
                        workspace["id"], workspace["revision"], status=409)
    assert refused["error"]["code"] == "ROTA_JOB_RUNNING"
    assert refused["error"]["message"] == "A check or auto-schedule is running on this rota. Try again when it finishes."


def test_delete_draft_archives_baseline_backed_draft_and_keeps_audit(h):
    workspace = _make_status(h, "draft")
    with transaction(h.app.state.engine) as db:
        baseline_id = db.scalar(select(m.WorkspaceBaseline.id).where(
            m.WorkspaceBaseline.workspace_id == workspace["id"]))
    result = h.command("coordinator", "workspace.delete_draft", {"reason": "Remove untouched draft"},
                       workspace["id"], workspace["revision"])
    assert result["workspace"]["archived_at"] is not None
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Workspace, workspace["id"]).archived_at is not None
        assert db.get(m.WorkspaceBaseline, baseline_id) is not None
        audit = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.target_id == workspace["id"])))
        assert any(row.action == "workspace.delete_draft" and row.reason == "Remove untouched draft" for row in audit)


def test_delete_refuses_draft_that_was_submitted(h):
    workspace = _make_status(h, "submitted")
    archived = h.command("coordinator", "workspace.archive", {"reason": REASON},
                         workspace["id"], workspace["revision"])["workspace"]
    restored = h.command("coordinator", "workspace.restore", {"reason": "Restore for cleanup"},
                         workspace["id"], archived["revision"])["workspace"]
    draft = h.lifecycle(restored, "reopen")["workspace"]
    refused = h.command("coordinator", "workspace.delete_draft", {"reason": "Try permanent delete"},
                        draft["id"], draft["revision"], status=409)
    assert refused["error"]["code"] == "DRAFT_DELETE_FORBIDDEN"


def test_archive_replay_is_idempotent(h):
    workspace = _make_status(h, "draft")
    key = str(uuid4())
    first = h.command("coordinator", "workspace.archive", {"reason": REASON}, workspace["id"], workspace["revision"], key=key)
    replay = h.command("coordinator", "workspace.archive", {"reason": REASON}, workspace["id"], workspace["revision"], key=key)
    assert replay["receipt_id"] == first["receipt_id"] and replay["workspace"]["archived_at"] == first["workspace"]["archived_at"]


def test_private_draft_owned_by_someone_else_has_no_lifecycle_actions(h):
    from tests.test_planning_groups_drafts import private
    workspace = private(h)
    actor = SimpleNamespace(identity_id=str(uuid4()), roles=frozenset({'coordinator'}),
                            permits=lambda *roles: bool(set(roles) & {'coordinator'}))
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, workspace['id'])
        assert allowed_actions(db, actor, row) == []


def test_compact_workspace_read_carries_archive_lifecycle_fields(h):
    workspace = _make_status(h, "draft")
    archived = h.command("coordinator", "workspace.archive", {"reason": REASON},
                         workspace["id"], workspace["revision"])["workspace"]
    compact = h.get("coordinator", f"/api/workspaces/{workspace['id']}?view=compact")["workspace"]
    assert compact["archived_at"] == archived["archived_at"]
    assert compact["archive_state"]["status"] == "draft"
    assert compact["archived_by_name"] == h.get("coordinator", "/api/me")["display_name"]
    assert compact["allowed_actions"] == ["workspace.restore", "workspace.delete_draft"]


def test_archived_published_rota_is_hidden_from_staff_until_restore(h):
    workspace = _make_status(h, "published")
    publication_id = next(row["id"] for row in h.get("coordinator", "/api/publications")["publications"]
                           if row["workspace_id"] == workspace["id"])
    h.command("coordinator", "workspace.archive", {"reason": REASON}, workspace["id"], workspace["revision"])
    assert publication_id not in {row["id"] for row in h.get("staff", "/api/publications")["publications"]}
    h.command("coordinator", "workspace.restore", {"reason": "Restore staff visibility"}, workspace["id"], workspace["revision"] + 1)
    assert publication_id in {row["id"] for row in h.get("staff", "/api/publications")["publications"]}
