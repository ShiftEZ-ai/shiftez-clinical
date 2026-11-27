"""Atomic request decisions and one receipt-backed Undo, using the single-record handlers."""
from sqlalchemy import select
from packages.contracts import schemas as s
from apps.api import models as m
from apps.api.auth import held_roles, require_role
from apps.api.errors import Problem, require
from . import availability, availability_patterns, independence, leave_types
from .common import bump_inputs


def _run(db, actor, items, reason, decision=None, batch_id=None):
    # Hold loaded rows in the identity map: each scoped lookup below still checks its department and revision.
    loaded = []
    for source, model in (("request", m.RequestRecord), ("pattern", m.AvailabilityPattern)):
        ids = [item.id for item in items if item.source == source]
        if ids:
            loaded.extend(db.scalars(select(model).where(model.department_id == actor.department_id, model.id.in_(ids))))
    # These records and grants cannot change in a request-only batch; keep them alive through every object check.
    identities = {actor.identity_id} | {row.created_by for row in loaded}
    loaded.extend(db.scalars(select(m.Identity).where(m.Identity.id.in_(identities))))
    loaded.append(db.get(m.Department, actor.department_id))
    org = db.get(m.Organisation, actor.organisation_id)
    with db.no_autoflush:
        approval_context = None
        if decision:
            approval_context = {'held': held_roles(db, actor.identity_id, actor.department_id),
                'permissions': list(independence.active_permissions(db, actor.department_id)), 'scopes': {}}
            for model in (m.RequestRecord, m.AvailabilityPattern):
                ids = [row.id for row in loaded if isinstance(row, model)]
                # A rotation approval can change a programme lead's later person/date scope.
                if not ids or model is m.RequestRecord and actor.permits('programme_lead') and not actor.permits('approver'):
                    continue
                scopes = [availability.decision_scope(db, actor, model, include_independence=independent,
                    approval_context=approval_context) for independent in (False, True)]
                for ident, authority, independent in db.execute(select(model.id, *scopes).where(model.id.in_(ids))):
                    approval_context['scopes'][model, ident, False] = bool(authority)
                    approval_context['scopes'][model, ident, True] = bool(independent)
        result, inputs_changed = _apply(db, actor, items, reason, decision, batch_id, approval_context)
    if inputs_changed:
        bump_inputs(db, actor, availability=True)
    else:
        db.flush()
    leave_types.names(db, actor.department_id, [row for row in result["records"] if row["source"] == "request"])
    availability._links(db, True, result["records"])
    return {**result, "counts": availability._counts(db, actor),
            "input_revision": org.input_revision, "availability_revision": org.availability_revision}


def _apply(db, actor, items, reason, decision, batch_id, approval_context=None):
    records, inputs_changed, affected = [], False, 0
    overtime_cache = {}  # Request-backed facts cannot become eligibility sources within this decision batch.
    for index, item in enumerate(items):
        pattern = item.source == "pattern"
        kind = "availability_pattern." if pattern else "request."
        payload = {"reason": reason}
        if decision:
            payload["decision"] = "decline" if pattern and decision == "reject" else decision
            if not pattern and item.balance_confirmation:
                payload["balance_confirmation"] = item.balance_confirmation
        command = s.Command(kind=kind + ("decide" if decision else "undo"), payload=payload,
                            target_id=item.id, expected_revision=item.expected_revision)
        handler = (availability_patterns.decide if pattern else availability.decide_request) if decision else (
            availability_patterns.undo if pattern else availability.undo_decision)
        try:
            if pattern:
                require_role(actor, "coordinator")
            if handler is availability.decide_request:
                reply = handler(db, actor, command, batch=True, overtime_cache=overtime_cache, approval_context=approval_context)
            elif decision:
                reply = handler(db, actor, command, batch=True, approval_context=approval_context)
            else:
                reply = handler(db, actor, command, batch=True)
        except Problem as error:
            raise Problem(error.status, error.code,
                          f"0 of {len(items)} done. Stopped at request {index + 1}: {error.message}",
                          {**(error.details or {}), "completed": 0, "total": len(items),
                           "failed_index": index, "id": item.id, "source": item.source}) from error
        row = reply["pattern" if pattern else "record"]
        inputs_changed |= reply["inputs_changed"]
        affected += reply.get("affected_count", 1)
        records.append({**row, "source": item.source,
                        **({"retracted_availability": reply["retracted_availability"]} if "retracted_availability" in reply else {}),
                        "stage": (availability.PATTERN_STAGE if pattern else availability.REQUEST_STAGE)[row["status"]],
                        "record_id": None, "record_revision": None, "record_retracted": None})
        db.add(m.AuditEvent(department_id=actor.department_id, actor_id=actor.identity_id,
                            action=command.kind, target_id=item.id, reason=reason,
                            detail={"batch_id": batch_id, "receipt_id": db.info.get("receipt_id"),
                                    **({"leave_balance_overdrawn": True} if reply.get("leave_balance_overdrawn") else {}),
                                    **({"self_approved": used} if (used := db.info.pop("self_approved", None)) else {}),
                                    **({"entered_on_behalf_by": sorted(enterers)}
                                       if (enterers := db.info.pop("entered_on_behalf_by", None)) else {})},
                            correlation_id=db.info.get("correlation_id") or "bulk-preview"))
    return {"batch_id": batch_id, "records": records, "count": len(records), "affected_count": affected}, inputs_changed


def decide(db, actor, command):
    value = s.RequestManyDecision.model_validate(command.payload)
    return _run(db, actor, value.items, value.reason, value.decision, db.info.get("receipt_id"))


def undo(db, actor, value):
    receipt = db.get(m.CommandReceipt, value.batch_id)
    require(receipt is not None and receipt.department_id == actor.department_id
            and receipt.command_kind == "request.decide_many", "NOT_FOUND", "Decision batch not found", 404)
    require(receipt.actor_id == actor.identity_id, "FORBIDDEN", "Only the person who decided this batch can undo it", 403)
    items = [s.RequestDecisionItem(id=row["id"], source=row["source"], expected_revision=row["revision"])
             for row in receipt.result["records"]]
    return {**_run(db, actor, items, value.reason, batch_id=value.batch_id),
            "previous_statuses": [row["status"] for row in receipt.result["records"]]}
