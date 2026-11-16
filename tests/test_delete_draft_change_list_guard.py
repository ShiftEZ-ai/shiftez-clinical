"""A draft carrying published-change evidence is never hard-deleted."""

from copy import deepcopy

from sqlalchemy import exists, select, update

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import planning


def _publication(h, name):
    workspace = h.workspace(name)
    workspace = h.shift(workspace, start="2026-10-04T08:00:00+03:00", end="2026-10-04T12:00:00+03:00")["workspace"]
    workspace = h.shift(workspace, start="2026-10-05T08:00:00+03:00", end="2026-10-05T12:00:00+03:00")["workspace"]
    return h.publish(workspace)["publication_id"]


def _change(h, publication_id, shift, revision=None):
    return h.command(
        "coordinator",
        "publication.change",
        {
            "kind": "replace",
            "shift_id": shift["id"],
            "assignment_id": shift["participants"][0]["id"],
            "person_id": h.seed["people"][2],
            "reason": "Arrange cover for a published duty",
        },
        target=publication_id,
        revision=revision,
    )


def _urgent_route(h):
    route = h.command(
        "admin",
        "review_route.create",
        {
            "name": "Urgent independent review",
            "starts_on": "2026-01-01",
            "definition": {
                "steps": [{"key": "routine", "label": "Independent review"}],
                "urgent_steps": [{"key": "urgent", "label": "Urgent review"}],
            },
            "reason": "Configure urgent route",
        },
    )["route"]
    h.command(
        "approver",
        "review_route.activate",
        {"reason": "Activate independent pathway"},
        target=route["id"],
        revision=route["revision"],
    )


def _real_moved_case(h):
    _urgent_route(h)
    publication_id = _publication(h, "Moved change source")
    shifts = h.get("coordinator", f"/api/publications/{publication_id}")["snapshot"]["shifts"]
    first = _change(h, publication_id, shifts[0])
    second = _change(h, publication_id, shifts[1], first["workspace"]["revision"])
    submitted = h.command(
        "coordinator",
        "publication.changes.submit",
        {"change_ids": [first["change"]["id"]], "urgency": "urgent", "reason": "Urgent cover for the first duty"},
        target=publication_id,
        revision=second["workspace"]["revision"],
    )
    approved = h.lifecycle(submitted["workspace"], "approve", "approver")["workspace"]
    h.lifecycle(approved, "publish")
    return publication_id


def _legacy_target(h, publication_id, changes=None, template=None, *, archive=True):
    """Move the real change-list evidence onto a legacy no-baseline draft."""
    with transaction(h.app.state.engine) as db:
        row = db.scalar(select(m.PublicationChangeList).where(m.PublicationChangeList.publication_id == publication_id))
        source = db.get(m.Workspace, row.workspace_id) if row is not None else None
        if source is None:
            assert template is not None
            source = m.Workspace(**template)
            db.add(source)
            db.flush()
            row = m.PublicationChangeList(
                department_id=source.department_id,
                publication_id=publication_id,
                workspace_id=source.id,
                changes=deepcopy(changes or []),
            )
            db.add(row)
            db.flush()
            workspace_id, row_id, revision = source.id, row.id, source.revision
        else:
            assert source.status == "draft"
            legacy = m.Workspace(
                department_id=source.department_id,
                name=source.name,
                starts_on=source.starts_on,
                ends_on=source.ends_on,
                created_by=source.created_by,
                last_edited_by=source.last_edited_by,
            )
            db.add(legacy)
            db.flush()
            db.execute(update(m.Shift).where(m.Shift.workspace_id == source.id).values(workspace_id=legacy.id))
            db.execute(update(m.ChangeSet).where(m.ChangeSet.workspace_id == source.id).values(workspace_id=legacy.id))
            row.workspace_id = legacy.id
            row.revision += 1
            if changes is not None:
                row.changes = deepcopy(changes)
            db.flush()
            workspace_id, row_id, revision = legacy.id, row.id, legacy.revision

    if archive:
        revision = h.command(
            "coordinator", "workspace.archive", {"reason": "Inspect retained amendment evidence"}, workspace_id, revision
        )["workspace"]["revision"]
    with transaction(h.app.state.engine) as db:
        history_ids = set(
            db.scalars(
                select(m.DefinitionHistory.id).where(
                    m.DefinitionHistory.kind == "publication_change_lists",
                    m.DefinitionHistory.object_id == row_id,
                )
            )
        )
        audit_ids = set(db.scalars(select(m.AuditEvent.id).where(m.AuditEvent.target_id == workspace_id)))
    return workspace_id, row_id, revision, history_ids, audit_ids


def _attempt_delete(h, workspace_id, row_id, revision, history_ids, audit_ids):
    reply = h.command(
        "coordinator",
        "workspace.delete_draft",
        {"reason": "Try to delete a draft carrying publication evidence"},
        workspace_id,
        revision,
        status=(200, 409),
    )
    with transaction(h.app.state.engine) as db:
        return {
            "error_code": (reply.get("error") or {}).get("code"),
            "error_message": (reply.get("error") or {}).get("message"),
            "workspace": db.get(m.Workspace, workspace_id) is not None,
            "change_list": db.get(m.PublicationChangeList, row_id) is not None,
            "history": history_ids <= set(db.scalars(select(m.DefinitionHistory.id).where(m.DefinitionHistory.id.in_(history_ids))))
            if history_ids
            else True,
            "audit": audit_ids <= set(db.scalars(select(m.AuditEvent.id).where(m.AuditEvent.id.in_(audit_ids))))
            if audit_ids
            else True,
        }


def test_delete_draft_refuses_every_change_list_state(h):
    publication_id = _real_moved_case(h)
    with transaction(h.app.state.engine) as db:
        real_row = db.scalar(select(m.PublicationChangeList).where(m.PublicationChangeList.publication_id == publication_id))
        assert real_row is not None
        published = next(c for c in real_row.changes if c["status"] == "published")
        moved = next(c for c in real_row.changes if c["status"] == "moved")
        source = db.get(m.Workspace, real_row.workspace_id)
        template = {
            "department_id": source.department_id,
            "name": source.name,
            "starts_on": source.starts_on,
            "ends_on": source.ends_on,
            "created_by": source.created_by,
            "last_edited_by": source.last_edited_by,
        }

    variants = [
        ("published", [published]),
        ("moved", [moved]),
        ("withdrawn-only", [{**moved, "status": "withdrawn"}]),
        ("empty", []),
    ]
    workspace_id, row_id, revision, history_ids, audit_ids = _legacy_target(
        h, publication_id, variants[0][1], template
    )
    observed = []
    for label, changes in variants:
        if label != "published":
            with transaction(h.app.state.engine) as db:
                row = db.get(m.PublicationChangeList, row_id)
                workspace = db.get(m.Workspace, workspace_id)
                if row is not None and workspace is not None:
                    row.changes = deepcopy(changes)
                    row.revision += 1
                    db.flush()
                    revision = workspace.revision
                else:
                    workspace_id, row_id, revision, history_ids, audit_ids = _legacy_target(
                        h, publication_id, changes, template
                    )
        observed.append((label, _attempt_delete(h, workspace_id, row_id, revision, history_ids, audit_ids)))

    assert observed == [
        (label, {
            "error_code": "DRAFT_DELETE_FORBIDDEN",
            "error_message": "This draft holds published changes; archive it instead.",
            "workspace": True,
            "change_list": True,
            "history": True,
            "audit": True,
        })
        for label, _ in variants
    ]


def _baseline_change_list_case(h):
    publication_id = _publication(h, "Baseline change-list source")
    shift = h.get("coordinator", f"/api/publications/{publication_id}")["snapshot"]["shifts"][0]
    changed = _change(h, publication_id, shift)
    workspace_id = changed["workspace"]["id"]
    archived = h.command(
        "coordinator", "workspace.archive", {"reason": "Create retained audit evidence"},
        workspace_id, changed["workspace"]["revision"],
    )["workspace"]
    restored = h.command(
        "coordinator", "workspace.restore", {"reason": "Restore the baseline-backed draft"},
        workspace_id, archived["revision"],
    )["workspace"]
    with transaction(h.app.state.engine) as db:
        row = db.scalar(select(m.PublicationChangeList).where(m.PublicationChangeList.publication_id == publication_id))
        assert row is not None
        workspace = db.get(m.Workspace, workspace_id)
        assert workspace.archived_at is None
        assert db.scalar(select(m.WorkspaceBaseline.id).where(m.WorkspaceBaseline.workspace_id == workspace_id)) is not None
        history_ids = set(db.scalars(select(m.DefinitionHistory.id).where(
            m.DefinitionHistory.kind == "publication_change_lists", m.DefinitionHistory.object_id == row.id)))
        audit_ids = set(db.scalars(select(m.AuditEvent.id).where(m.AuditEvent.target_id == workspace_id)))
    return {
        "publication_id": publication_id,
        "workspace_id": workspace_id,
        "row_id": row.id,
        "revision": restored["revision"],
        "history_ids": history_ids,
        "audit_ids": audit_ids,
        "changes": deepcopy(row.changes),
    }


def test_baseline_change_list_archives_and_keeps_evidence(h):
    case = _baseline_change_list_case(h)
    result = h.command(
        "coordinator", "workspace.delete_draft", {"reason": "Archive the baseline-backed amendment"},
        case["workspace_id"], case["revision"],
    )
    assert result["workspace"]["archived_at"] is not None
    with transaction(h.app.state.engine) as db:
        workspace = db.get(m.Workspace, case["workspace_id"])
        assert workspace is not None and workspace.archived_at is not None
        assert db.get(m.PublicationChangeList, case["row_id"]) is not None
        assert db.scalar(select(m.WorkspaceBaseline.id).where(
            m.WorkspaceBaseline.workspace_id == case["workspace_id"])) is not None
        assert case["history_ids"] <= set(db.scalars(select(m.DefinitionHistory.id).where(
            m.DefinitionHistory.id.in_(case["history_ids"]))))
        assert case["audit_ids"] <= set(db.scalars(select(m.AuditEvent.id).where(
            m.AuditEvent.id.in_(case["audit_ids"]))))


def test_allowed_actions_archive_change_list_drafts(h):
    case = _baseline_change_list_case(h)
    baseline_actions = h.detail(case["workspace_id"])["workspace"]["allowed_actions"]
    assert baseline_actions == ["workspace.archive", "workspace.delete_draft"]

    workspace_id, _row_id, revision, _history_ids, _audit_ids = _legacy_target(
        h, case["publication_id"], case["changes"], archive=False
    )
    assert h.detail(workspace_id)["workspace"]["allowed_actions"] == ["workspace.archive"]


def test_workspace_list_allowed_actions_batches_mixed_change_list_states(h):
    baseline = _baseline_change_list_case(h)
    plain = h.workspace("Plain list draft")
    with transaction(h.app.state.engine) as db:
        source = db.get(m.Workspace, baseline["workspace_id"])
        source_publication = db.scalar(select(m.Publication).where(
            m.Publication.department_id == source.department_id).order_by(m.Publication.sequence.desc()).limit(1))
        change_only = m.Workspace(
            department_id=source.department_id,
            name="Change-only list draft",
            starts_on=source.starts_on,
            ends_on=source.ends_on,
            created_by=source.created_by,
            last_edited_by=source.last_edited_by,
        )
        db.add(change_only)
        db.flush()
        publication = m.Publication(
            department_id=source.department_id,
            workspace_id=change_only.id,
            sequence=source_publication.sequence + 1,
            starts_on=source_publication.starts_on,
            ends_on=source_publication.ends_on,
            current=False,
            published_by=source_publication.published_by,
            snapshot=source_publication.snapshot,
            validation=source_publication.validation,
            fingerprint=source_publication.fingerprint,
        )
        db.add(publication)
        db.flush()
        db.add(m.PublicationChangeList(
            department_id=source.department_id,
            publication_id=publication.id,
            workspace_id=change_only.id,
            changes=[],
        ))
        db.flush()
        change_only_id = change_only.id

    rows = h.get("coordinator", "/api/workspaces?include_archived=true&limit=100")["workspaces"]
    actions = {row["id"]: row["allowed_actions"] for row in rows}
    assert actions[baseline["workspace_id"]] == ["workspace.archive", "workspace.delete_draft"]
    assert actions[change_only_id] == ["workspace.archive"]
    assert actions[plain["id"]] == ["workspace.archive", "workspace.delete_draft"]
    with transaction(h.app.state.engine) as db:
        expected_refusal = {
            workspace_id: planning._delete_draft_refused(db, db.get(m.Workspace, workspace_id))
            for workspace_id in (baseline["workspace_id"], change_only_id, plain["id"])
        }
    assert {
        workspace_id: "workspace.delete_draft" not in actions[workspace_id]
        for workspace_id in expected_refusal
    } == expected_refusal
