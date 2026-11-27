"""Version-bound sequential review; delegated authority never becomes an override."""
from datetime import timedelta
from sqlalchemy import select
from packages.contracts import schemas as s
from packages.domain.time import aware
from apps.api import models as m
from apps.api.auth import current_roles, require_role
from apps.api.errors import require
from apps.api.serialization import record
from apps.api.snapshots import fingerprint, snapshot_hash
from .common import scoped, expected, history, history_version, bump_inputs, range_overlaps, outbox
from . import independence
from .independence import person_key

REVIEW_ROLES = {"approver", "programme_lead"}
DEFAULT_STEP = s.ReviewStep(key="independent_review", label="Independent approval").model_dump(mode="json")
STALE_APPROVAL_MESSAGE = ("Approval is still recorded, but its review is out of date. Reopen this rota, check it, "
                          "submit it again and obtain renewed approval before publishing.")


def live_roles(db, identity_id, department_id):
    identity = db.get(m.Identity, identity_id)
    return current_roles(db, identity_id, department_id) if identity and not identity.disabled else frozenset()


def contributors(db, workspace):
    identities = {workspace.created_by}
    identities.update(db.scalars(select(m.ChangeSet.actor_id).where(m.ChangeSet.workspace_id == workspace.id,
        m.ChangeSet.before_snapshot['_review_action'].as_string().is_(None)).distinct()))
    identities.update(db.scalars(select(m.WorkspaceMergeProposal.actor_id).where(
        m.WorkspaceMergeProposal.target_workspace_id == workspace.id,
        m.WorkspaceMergeProposal.status == 'applied').distinct()))
    for payload in db.scalars(select(m.WorkspaceMergeProposal.payload).where(
            m.WorkspaceMergeProposal.target_workspace_id == workspace.id,
            m.WorkspaceMergeProposal.status == 'applied')):
        identities.update(payload.get('contributor_identity_ids', []))
    for rows in (workspace.rule_adjustments or [], workspace.target_overrides or []):
        identities.update(row.get('by') or row.get('actor_id') or row.get('created_by') for row in rows)
    swap = db.scalar(select(m.SwapRequest).where(m.SwapRequest.workspace_id == workspace.id))
    if swap:
        identities.update({swap.proposer_id, swap.recipient_id})
    identities.discard(None)
    people = dict(db.execute(select(m.Identity.id, m.Identity.person_id).where(m.Identity.id.in_(identities))).all())
    return {'person:' + people[i] if people.get(i) else 'identity:' + i for i in identities}


def contributed(db, workspace, identity_id):
    # The workspace records both authors directly.  Keep this fast path free of
    # identity lookups so callers that already know the author can classify
    # involvement from the workspace row alone (and so an absent DB is safe in
    # pure contract checks).
    if identity_id in {workspace.created_by, workspace.last_edited_by}:
        return True
    return person_key(db, identity_id) in contributors(db, workspace)


def route_command(db, actor, command):
    if command.kind in {"review_route.create", "review_route.update"}:
        require_role(actor, "admin")
        value = s.ReviewRouteInput.model_validate(command.payload)
        if command.kind.endswith("update"):
            row = scoped(db, m.ReviewRoute, command.target_id, actor)
            expected(row, command.expected_revision)
            require(row.status == "proposed", "ROUTE_IMMUTABLE", "Create a replacement proposal for an activated route", 409)
            # Preserve latest ownership; append-only history retains every proposal author.
            row.created_by = actor.identity_id
            for key, val in value.model_dump(exclude={"reason"}).items():
                if key == "purpose" and key not in value.model_fields_set:
                    continue
                setattr(row, key, val)
            row.revision += 1
        else:
            row = m.ReviewRoute(department_id=actor.department_id, created_by=actor.identity_id,
                                **value.model_dump(exclude={"reason"}))
            db.add(row)
    else:
        require_role(actor, "approver")
        value = s.Decision.model_validate(command.payload)
        row = scoped(db, m.ReviewRoute, command.target_id, actor)
        expected(row, command.expected_revision)
        authors = {row.created_by}
        authors.update(entry.actor_id for entry in db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.kind == "review_routes", m.DefinitionHistory.object_id == row.id))
            if entry.snapshot.get("status") == "proposed")
        independence.ensure(db, actor, "route.decide", person_key(db, actor.identity_id) not in {person_key(db, author) for author in authors},
                            "A person who did not author this proposal must activate or retire it", value.reason, row.id)
        if command.kind == "review_route.activate":
            require(row.status == "proposed", "ROUTE_STATE", "Only a proposed route can be activated", 409)
            s.ReviewRouteDefinition.model_validate(row.definition)
            for other in db.scalars(select(m.ReviewRoute).where(m.ReviewRoute.department_id == actor.department_id,
                                                               m.ReviewRoute.status == "active",
                                                               m.ReviewRoute.purpose == "rota")):
                require(row.purpose == "swap" or not range_overlaps(row.starts_on, row.ends_on, other.starts_on, other.ends_on),
                        "ROUTE_OVERLAP", "Retire or bound overlapping routes before activation", 409)
            row.status, row.activated_by = "active", actor.identity_id
        elif command.kind == "review_route.retire":
            require(row.status == "active", "ROUTE_STATE", "Only an active route can be retired", 409)
            require(db.get(m.Department, actor.department_id).swap_review_route_id != row.id,
                    "ROUTE_IN_USE", "Choose another swap review route or clear it in department settings before retiring this route", 409)
            row.status = "retired"
        else:
            require(False, "UNKNOWN_COMMAND", "Unsupported review route command")
        row.revision += 1
        bump_inputs(db, actor)
    history(db, actor, "review_routes", row, value.reason)
    return {"route": record(row)}


def resolve_route(db, workspace):
    swap = db.scalar(select(m.SwapRequest).where(m.SwapRequest.workspace_id == workspace.id))
    selected = db.get(m.Department, workspace.department_id).swap_review_route_id if swap else None
    if selected:
        route = db.get(m.ReviewRoute, selected)
        require(route is not None and route.department_id == workspace.department_id and route.status == "active",
                "REVIEW_ROUTE_REQUIRED", "Activate the department's selected swap review route before proceeding", 409)
        require(route.purpose == "swap", "ROUTE_PURPOSE", "The department's selected review route must be for swaps", 409)
        require(route.starts_on <= workspace.starts_on and (route.ends_on is None or workspace.ends_on <= route.ends_on),
                "REVIEW_ROUTE_BOUNDARY", "The full swap amendment must fit the selected review route", 409)
        return route_snapshot(route, workspace)
    routes = [r for r in db.scalars(select(m.ReviewRoute).where(m.ReviewRoute.department_id == workspace.department_id,
              m.ReviewRoute.purpose == "rota",
              m.ReviewRoute.status.in_(["active", "retired"])))
              if range_overlaps(workspace.starts_on, workspace.ends_on, r.starts_on, r.ends_on)]
    active = [r for r in routes if r.status == "active"]
    if active:
        require(len(active) == 1 and active[0].starts_on <= workspace.starts_on
                and (active[0].ends_on is None or workspace.ends_on <= active[0].ends_on),
                "REVIEW_ROUTE_BOUNDARY", "The full workspace must fit a single active review route; split the period", 409)
        route = active[0]
        return route_snapshot(route, workspace)
    require(not routes, "REVIEW_ROUTE_REQUIRED", "A retired route covers this period; activate an explicit replacement", 409)
    require(workspace.urgency != "urgent", "URGENT_ROUTE_UNCONFIGURED", "Urgency requires an explicitly activated urgent route", 409)
    return {"schema_version": 1, "route_id": None, "route_revision": None,
            "name": "Independent approval (built-in)", "mode": "routine", "steps": [dict(DEFAULT_STEP)]}


def route_snapshot(route, workspace):
    definition = s.ReviewRouteDefinition.model_validate(route.definition)
    steps = definition.urgent_steps if workspace.urgency == "urgent" else definition.steps
    require(bool(steps), "URGENT_ROUTE_UNCONFIGURED", "This route has no independently activated urgent pathway", 409)
    return {"schema_version": 1, "route_id": route.id, "route_revision": route.revision,
            "name": route.name, "mode": workspace.urgency,
            "steps": [step.model_dump(mode="json") for step in steps]}


def require_swap_independence(db, workspace, keys):
    swap = db.scalar(select(m.SwapRequest).where(m.SwapRequest.workspace_id == workspace.id))
    if swap:
        parties = {"person:" + swap.proposer_person_id, "person:" + swap.recipient_person_id,
                   person_key(db, swap.proposer_id), person_key(db, swap.recipient_id)}
        require(not keys & parties, "SELF_APPROVAL", "Swap parties cannot approve their own exchange", 403)


def assert_route_current(db, workspace):
    snap = workspace.review_snapshot
    require(snap is not None and snap == resolve_route(db, workspace),
            "STALE_REVIEW_ROUTE", "The applicable review route changed; reopen and obtain renewed review", 409)


def decisions(db, workspace):
    return list(db.scalars(select(m.ReviewDecision).where(m.ReviewDecision.workspace_id == workspace.id,
                m.ReviewDecision.content_revision == workspace.content_revision).order_by(m.ReviewDecision.created_at, m.ReviewDecision.seq)))


def withdraw(db, actor, workspace, reason, *, return_note=None):
    """Withdraw the current rota review through the same path used by Reopen.

    Review decisions remain immutable evidence; only the live workflow fields
    move back to a draft so an archive can later restore the captured state.
    """
    require(workspace.status in {'submitted', 'approved'}, 'WORKFLOW_STATE',
            'Only a submitted or approved rota has a review to withdraw', 409)
    from . import acknowledged_gaps
    acknowledged_gaps.reopen_undo(db, actor, workspace, reason)
    workspace.status = 'draft'
    workspace.submitted_fingerprint = None
    workspace.submitted_at = None
    workspace.review_snapshot = None
    workspace.validation = None
    workspace.return_note = return_note
    workspace.content_revision += 1
    from .generation_resume import clear_generation_resume
    clear_generation_resume(db, workspace_id=workspace.id)
    if workspace.base_publication_id:
        from .published_changes import on_reviewed
        on_reviewed(db, actor, workspace, reason)
    bump_inputs(db, actor)


def progress(db, workspace, *, current_fingerprint=None):
    snap = workspace.review_snapshot
    if snap is None:
        return {"route": None, "steps": [], "pending_step": None, "complete": False, "legacy": True}
    all_decisions = decisions(db, workspace)
    started = aware(workspace.submitted_at) if workspace.submitted_at else m.now()
    steps = []
    pending = None
    for step in snap["steps"]:
        approvals = [a for a in all_decisions if a.step_key == step["key"]]
        complete = len(approvals) >= step["approvals_required"]
        due = started + timedelta(hours=step["deadline_hours"])
        ready = pending is None
        item = {**step, "approved_count": len(approvals), "complete": complete,
                "due_at": due.isoformat() if ready else None,
                "escalated": ready and not complete and m.now() >= due and bool(step["escalation_role"]),
                "reviewers": [record(a) for a in approvals]}
        steps.append(item)
        if not complete and pending is None:
            pending = item
        if complete:
            started = max(aware(a.created_at) for a in approvals)
    # ME-25: what the coordinator wrote when submitting, for the approver (one indexed read; the empty default note
    # "Routine update" is not a note).
    sent = db.scalar(select(m.AuditEvent).where(m.AuditEvent.department_id == workspace.department_id,
        m.AuditEvent.target_id == workspace.id, m.AuditEvent.action == "workspace.submit").order_by(m.AuditEvent.created_at.desc()).limit(1))
    submission = {"note": sent.reason if sent.reason != "Routine update" else None, "by": sent.actor_id,
                  "at": aware(sent.created_at).isoformat()} if sent else None
    return {"route": snap, "steps": steps, "pending_step": pending, "complete": pending is None, "submission": submission,
            "stale_inputs": workspace.submitted_fingerprint != (
                current_fingerprint if current_fingerprint is not None else fingerprint(db, workspace))}


def delegation_command(db, actor, command):
    if command.kind == "review_delegation.create":
        require_role(actor, *REVIEW_ROLES)
        value = s.ReviewDelegationInput.model_validate(command.payload)
        route = scoped(db, m.ReviewRoute, value.route_id, actor)
        require(route.status == "active", "ROUTE_STATE", "Delegation requires an active route", 409)
        definition = s.ReviewRouteDefinition.model_validate(route.definition)
        step = next((step for step in definition.steps + (definition.urgent_steps or []) if step.key == value.step_key), None)
        require(step is not None, "STEP_NOT_FOUND", "Step does not belong to this route", 404)
        require(step.role in actor.roles, "DELEGATION_AUTHORITY", "Only the primary step authority can delegate its own review", 403)
        require(person_key(db, actor.identity_id) != person_key(db, value.delegate_id), "SELF_DELEGATION", "Choose a different person", 403)
        require(bool(live_roles(db, value.delegate_id, actor.department_id) & REVIEW_ROLES),
                "DELEGATE_INELIGIBLE", "The delegate needs an active departmental reviewer role", 403)
        require(value.starts_at >= m.now() - timedelta(minutes=5) and value.expires_at > m.now(),
                "DELEGATION_DATES", "Do not backdate or create an expired delegation")
        row = m.ReviewDelegation(department_id=actor.department_id, delegator_id=actor.identity_id,
                                 route_revision=route.revision, **value.model_dump())
        db.add(row)
    else:
        require(command.kind in {"review_delegation.revoke", "review_delegation.restore"},
                "UNKNOWN_COMMAND", "Unsupported delegation command")
        require_role(actor, "admin", *REVIEW_ROLES)
        restoring = command.kind == "review_delegation.restore"
        value = (s.AuthorityRestoreInput if restoring else s.Decision).model_validate(command.payload)
        row = scoped(db, m.ReviewDelegation, command.target_id, actor)
        expected(row, command.expected_revision)
        require(row.delegator_id == actor.identity_id or actor.permits("admin"),
                "FORBIDDEN", "Only the delegator or an admin can revoke or restore this delegation", 403)
        if restoring:
            require(row.revoked, "DELEGATION_STATE", "Only a revoked delegation can be restored", 409)
            previous = history_version(db, actor, "review_delegations", row.id)
            require(previous is None or not previous.snapshot.get("restored_to_id"),
                    "DELEGATION_STATE", "This delegation was already restored; reload to use its replacement", 409)
            require(aware(row.expires_at) > m.now(), "DELEGATION_EXPIRED",
                    "This delegation has expired; ask the original reviewer to create a new delegation", 409)
            route = scoped(db, m.ReviewRoute, row.route_id, actor)
            require(route.status == "active" and route.revision == row.route_revision,
                    "STALE_REVIEW_ROUTE", "The review route changed; create a new delegation for the active route", 409)
            definition = s.ReviewRouteDefinition.model_validate(route.definition)
            step = next((step for step in definition.steps + (definition.urgent_steps or []) if step.key == row.step_key), None)
            require(step is not None and step.role in live_roles(db, row.delegator_id, actor.department_id),
                    "DELEGATION_AUTHORITY", "The original delegator no longer has this review authority; restore their departmental role or re-enable their account before retrying", 403)
            require(person_key(db, row.delegator_id) != person_key(db, row.delegate_id),
                    "SELF_DELEGATION", "Choose a delegate representing a different person", 403)
            require(bool(live_roles(db, row.delegate_id, actor.department_id) & REVIEW_ROLES),
                    "DELEGATE_INELIGIBLE", "Restore the delegate's departmental reviewer access and enabled account before retrying", 403)
            source = row
            # Keep revoked evidence immutable; advancing its revision consumes this Undo.
            source.revision += 1
            row = m.ReviewDelegation(**{key: getattr(source, key) for key in (
                "department_id", "route_id", "route_revision", "step_key", "delegator_id", "delegate_id",
                "starts_at", "expires_at", "reason")})
            db.add(row)
            db.flush()
            history(db, actor, "review_delegations", source, value.reason, snapshot_extra={"restored_to_id": row.id})
            history(db, actor, "review_delegations", row, value.reason,
                    snapshot_extra={"restored_from_id": source.id, "restored_from_revision": command.expected_revision})
            return {"delegation": record(row)}
        require(not row.revoked, "DELEGATION_REVOKED", "Delegation is already revoked", 409)
        row.revoked = True
        row.revision += 1
    history(db, actor, "review_delegations", row, value.reason)
    return {"delegation": record(row)}


def validate_delegation(db, workspace, step, delegation_id, approved_by, *, now=None):
    row = db.get(m.ReviewDelegation, delegation_id)
    snap = workspace.review_snapshot
    require(row is not None and row.department_id == workspace.department_id
            and row.route_id == snap["route_id"] and row.route_revision == snap["route_revision"]
            and row.step_key == step["key"] and row.delegate_id == approved_by,
            "DELEGATION_SCOPE", "Delegation does not cover this reviewer, route version and step", 403)
    now = m.now() if now is None else now
    require(not row.revoked and aware(row.starts_at) <= now < aware(row.expires_at),
            "DELEGATION_INACTIVE", "Delegation is expired, not yet effective or revoked", 409)
    require(step["role"] in live_roles(db, row.delegator_id, workspace.department_id)
            and bool(live_roles(db, row.delegate_id, workspace.department_id) & REVIEW_ROLES),
            "APPROVER_REVOKED", "A represented or delegated review authority is no longer active", 409)
    return row


def validate_existing(db, workspace, *, current_fingerprint=None, now=None):
    """Check every prior decision, including identity/person separation and revocation."""
    contributors_keys = contributors(db, workspace)
    used = set()
    steps = {step["key"]: step for step in workspace.review_snapshot["steps"]}
    fp = current_fingerprint if current_fingerprint is not None else fingerprint(db, workspace)
    route_hash = snapshot_hash(workspace.review_snapshot)
    for decision in decisions(db, workspace):
        require(decision.fingerprint == fp and decision.route_hash == route_hash,
                "STALE_APPROVAL", STALE_APPROVAL_MESSAGE, 409)
        step = steps.get(decision.step_key)
        require(step is not None, "STALE_APPROVAL", "Review step is no longer supported", 409)
        keys = {person_key(db, decision.approved_by), person_key(db, decision.authority_id)}
        require_swap_independence(db, workspace, keys)
        # An approval given under a self-approval permission stays valid even after the permission ends.
        require(decision.self_approval_id is not None or not keys & contributors_keys,
                "SELF_APPROVAL", "A change participant cannot approve their own candidate", 403)
        require(not keys & used, "DUPLICATE_REVIEWER", "Each approval seat must represent a different person", 409)
        used.update(keys)
        if decision.delegation_id:
            delegation = validate_delegation(db, workspace, step, decision.delegation_id, decision.approved_by, now=now)
            require(delegation.delegator_id == decision.authority_id and decision.authority_role == step["role"],
                    "DELEGATION_SCOPE", "Represented authority changed", 409)
        else:
            require(decision.approved_by == decision.authority_id and decision.authority_role in {step["role"], step["escalation_role"]}
                    and decision.authority_role in live_roles(db, decision.approved_by, workspace.department_id),
                    "APPROVER_REVOKED", "The approving authority is no longer active; obtain a new review", 409)
    return used


def can_review(db, actor, workspace, *, authority_id=None, context=None):
    authority_id = authority_id or actor.identity_id
    keys = {person_key(db, actor.identity_id), person_key(db, authority_id)}
    involved = contributors(db, workspace)
    swap = db.scalar(select(m.SwapRequest).where(m.SwapRequest.workspace_id == workspace.id))
    if swap and keys & {person_key(db, swap.proposer_id), person_key(db, swap.recipient_id),
                       'person:' + swap.proposer_person_id, 'person:' + swap.recipient_person_id}:
        return False
    independent = not keys & involved
    if authority_id != actor.identity_id and person_key(db, authority_id) in involved:
        return False
    return independence.can_decide(db, actor, 'rota.approve', independent, context=context)


def approve_step(db, actor, workspace, value, *, record_decision=True, current_fingerprint=None):
    fp = current_fingerprint if current_fingerprint is not None else fingerprint(db, workspace)
    if workspace.review_snapshot is None:
        workspace.review_snapshot, workspace.submitted_at = resolve_route(db, workspace), m.now()
    assert_route_current(db, workspace)
    used = validate_existing(db, workspace, current_fingerprint=fp)
    pending = progress(db, workspace, current_fingerprint=fp)["pending_step"]
    require(pending is not None, "REVIEW_COMPLETE", "All review steps are already complete", 409)
    authority_id, role, delegation_id = actor.identity_id, pending["role"], value.delegation_id
    if delegation_id:
        delegation = validate_delegation(db, workspace, pending, delegation_id, actor.identity_id)
        authority_id = delegation.delegator_id
    else:
        if role not in actor.roles and pending["escalated"]:
            role = pending["escalation_role"]
        require(role in actor.roles, "REVIEW_STEP_ROLE", "Your role cannot approve the pending review step", 403)
    keys = {person_key(db, actor.identity_id), person_key(db, authority_id)}
    require_swap_independence(db, workspace, keys)
    involved = contributors(db, workspace)
    # A permission covers the person holding it, never someone who delegated their review to them.
    require(authority_id == actor.identity_id or person_key(db, authority_id) not in involved, "SELF_APPROVAL",
            "A delegated review cannot approve the delegating reviewer's own rota", 403)
    require(can_review(db, actor, workspace, authority_id=authority_id), 'SELF_APPROVAL',
            'Every contributor and swap party is excluded from independent review', 403)
    permission = independence.ensure(db, actor, "rota.approve", not keys & involved,
                                     "Every contributor and swap party is excluded from independent review", value.reason, workspace.id)
    require(not keys & used, "DUPLICATE_REVIEWER", "A person or represented principal cannot fill multiple approval seats", 409)
    if not record_decision:
        return None
    decision = m.ReviewDecision(created_at=m.now(), workspace_id=workspace.id, content_revision=workspace.content_revision,
                                fingerprint=fp, route_hash=snapshot_hash(workspace.review_snapshot),
                                step_key=pending["key"], approved_by=actor.identity_id, authority_id=authority_id,
                                authority_role=role, delegation_id=delegation_id, reason=value.reason,
                                self_approval_id=permission.id if permission else None)
    db.add(decision)
    db.flush()
    complete = progress(db, workspace, current_fingerprint=fp)["complete"]
    if complete:
        # Retain the historical summary table/API; decisions above carry actual route authority.
        db.add(m.Approval(workspace_id=workspace.id, content_revision=workspace.content_revision,
                          fingerprint=fp, approved_by=actor.identity_id, reason=value.reason))
        workspace.status = "approved"
    if workspace.review_snapshot["route_id"]:
        outbox(db, actor, f"review:{workspace.id}:{workspace.content_revision}:{actor.identity_id}", "review",
               {"workspace_id": workspace.id})
    return decision


def require_complete(db, workspace, *, current_fingerprint=None):
    fp = current_fingerprint if current_fingerprint is not None else fingerprint(db, workspace)
    assert_route_current(db, workspace)
    validate_existing(db, workspace, current_fingerprint=fp)
    require(progress(db, workspace, current_fingerprint=fp)["complete"], "REVIEW_INCOMPLETE", "Every configured review step must be completed", 409)


def decision_access(db, actor, workspace, progress, *, context=None):
    """Read the same current route, seats, independence and delegation facts enforced by approve_step."""
    from apps.api.errors import Problem
    result = {'can_decide': False, 'decision_role': None, 'delegation_ids': [], 'decision_reason': None}
    if workspace.status != 'submitted':
        return {**result, 'decision_reason': 'WORKFLOW_STATE'}
    if progress.get('stale_inputs'):
        return {**result, 'decision_reason': 'STALE_INPUTS'}
    now = (context or {}).get('now')
    try:
        if workspace.review_snapshot:
            assert_route_current(db, workspace)
            used = validate_existing(db, workspace, current_fingerprint=workspace.submitted_fingerprint, now=now)
            pending = progress['pending_step']
        else:
            used = set()
            pending = {**resolve_route(db, workspace)['steps'][0], 'escalated': False}
        if pending is None:
            return {**result, 'decision_reason': 'REVIEW_COMPLETE'}
        result['decision_role'] = pending['role']
        own = pending['role'] in actor.roles or pending.get('escalated') and pending.get('escalation_role') in actor.roles
        can_review_own = can_review(db, actor, workspace, context=context)
        own = own and can_review_own and person_key(db, actor.identity_id) not in used
        delegations = []
        if workspace.review_snapshot and actor.roles & REVIEW_ROLES:
            for row in db.scalars(select(m.ReviewDelegation).where(m.ReviewDelegation.department_id == actor.department_id,
                    m.ReviewDelegation.delegate_id == actor.identity_id, m.ReviewDelegation.revoked.is_(False),
                    m.ReviewDelegation.route_id == workspace.review_snapshot['route_id'],
                    m.ReviewDelegation.step_key == pending['key'])):
                try:
                    validate_delegation(db, workspace, pending, row.id, actor.identity_id, now=now)
                    keys = {person_key(db, actor.identity_id), person_key(db, row.delegator_id)}
                    if not keys & used and can_review(db, actor, workspace, authority_id=row.delegator_id, context=context):
                        delegations.append(row.id)
                except Problem:
                    continue
        return {**result, 'can_decide': bool(own or delegations), 'delegation_ids': sorted(delegations),
            'decision_reason': None if own or delegations else 'SELF_APPROVAL' if not can_review_own else 'REVIEW_STEP_ROLE'}
    except Problem as exc:
        return {**result, 'decision_reason': exc.code}


def viewer_progress(db, actor, workspace, *, current_fingerprint=None, context=None):
    value = progress(db, workspace, current_fingerprint=current_fingerprint)
    if value.get('legacy'):
        value = {**value, 'stale_inputs': workspace.submitted_fingerprint != (
            current_fingerprint if current_fingerprint is not None else fingerprint(db, workspace))}
    return {**value, **decision_access(db, actor, workspace, value, context=context)}
