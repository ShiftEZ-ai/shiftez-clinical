"""Exact, reviewed non-safety gaps on the existing accepted-issues state."""
from apps.api import models as m
from apps.api.errors import require
from apps.api.snapshots import workspace_content
from packages.domain.rota_summary import rota_summary
from . import reviews
from .rota_adjustments import adjustment_review_items, remove_rejected_adjustments


def projection(workspace, validation=None, *, summary=None):
    value = (summary if summary is not None else rota_summary(
        validation if validation is not None else workspace.validation,
        accepted_issues=workspace.accepted_issues or []))['fix_list']
    value = {**value, 'summary': {**value['summary'], 'check_status': None, 'check_queued_at': None}}
    return {**value, 'workspace_id': workspace.id, 'revision': workspace.revision,
            'review_items': review_items(workspace, current=value)}


def require_accepted(workspace, result, *, reviewed=False):
    require(not result.get('scope') or bool(workspace.base_publication_id), 'PARTIAL_CHECK',
            'A partial check cannot qualify a whole rota; check the complete rota before review or publication', 422)
    value = projection(workspace, result)
    must = [c for c in value['causes'] if c['level'] == 'must']
    require(not value['summary']['submit_blocked'], 'VALIDATION_FAILED',
            'Fix safety findings and acknowledge each remaining mandatory cause with a reason', 422,
            {**result, 'fix_list': value})
    if reviewed:
        require(all(c['acknowledgement'].get('decisions') for c in must), 'GAPS_NOT_REVIEWED',
                'An independent approver must decide every acknowledged gap before publication', 409)
    return value


def submit(db, actor, workspace, result, acknowledgements, reason):
    value = projection(workspace, result)
    by_id = {c['id']: c for c in value['causes'] if c['level'] == 'must'}
    selected = [a.cause_id for a in acknowledgements]
    require(len(set(selected)) == len(selected), 'DUPLICATE_ACKNOWLEDGEMENT', 'Give one reason per cause')
    for item in acknowledgements:
        require(item.cause_id in by_id, 'CAUSE_CHANGED', 'The cause changed; read the current Fix list', 409)
        require(by_id[item.cause_id]['acknowledgeable'], 'SAFETY_RULE', 'Safety findings cannot be acknowledged', 409)
    active = {c['id'] for c in value['causes'] if c['acknowledgement']}
    retained = [row for row in workspace.accepted_issues or [] if not row.get('cause_id') or row['cause_id'] in active]
    if acknowledgements or retained != (workspace.accepted_issues or []):
        before = workspace_content(db, workspace)
        replaced = {key for cid in selected for key in by_id[cid]['finding_keys']}
        at = m.now().isoformat()
        workspace.accepted_issues = [a for a in retained if a['key'] not in replaced] + [
            {'key': key, 'cause_id': item.cause_id, 'evidence_hash': by_id[item.cause_id]['evidence_hash'],
             'reason': item.reason, 'by': actor.identity_id, 'at': at,
             'content_revision': workspace.content_revision, 'decisions': []}
            for item in acknowledgements for key in by_id[item.cause_id]['finding_keys']]
        db.add(m.ChangeSet(workspace_id=workspace.id, before_snapshot=before,
            resulting_content_revision=workspace.content_revision, actor_id=actor.identity_id, reason=reason))
    return require_accepted(workspace, result)


def require_gap_decisions(workspace, result, value):
    current = require_accepted(workspace, result)
    must = [c for c in current['causes'] if c['level'] == 'must']
    accepted = {d.item_id for d in value.decisions if d.kind == 'gap' and d.accept}
    require(accepted == {c['id'] for c in must}, 'GAP_DECISION_REQUIRED',
            'Explicitly accept every acknowledged cause or return the rota to its coordinator', 409)


def validate_item_decisions(db, actor, workspace, value):
    items = {item['id']: item for item in review_items(workspace)}
    selected = {d.item_id: d for d in value.decisions}
    require(len(selected) == len(value.decisions) and selected.keys() == items.keys(),
            'REVIEW_DECISIONS_REQUIRED', 'Decide every current adjustment and acknowledged gap once', 409)
    require(all(d.kind == ('gap' if items[ident]['kind'] == 'acknowledged_gap' else 'adjustment')
                for ident, d in selected.items()), 'REVIEW_ITEM_CHANGED', 'Use the current item kind', 409)
    if items:
        require_item_reviewer(db, actor, workspace, value)


def require_item_reviewer(db, actor, workspace, value):
    delegation = db.get(m.ReviewDelegation, value.delegation_id) if value.delegation_id else None
    require(reviews.can_review(db, actor, workspace,
            authority_id=delegation.delegator_id if delegation else None), 'SELF_APPROVAL',
            'Rota review items require an independent approver', 403)


def review_items(workspace, *, current=None):
    current = current if current is not None else rota_summary(workspace.validation,
        accepted_issues=workspace.accepted_issues or [])['fix_list']
    active = {c['id'] for c in current['causes'] if c['acknowledgement']}
    return adjustment_review_items(workspace) + [
        {'id': row['cause_id'], 'kind': 'acknowledged_gap', 'reason': row['reason'], 'source': row}
        for row in audit_rows(workspace) if row['cause_id'] in active]


def reject_items(db, actor, workspace, value):
    """Reject selected entries; a modified candidate always returns for repair and renewed review."""
    from . import planning
    selected = {d.item_id for d in value.decisions if not d.accept}
    items = {item['id']: item for item in review_items(workspace)}
    require(selected <= items.keys(),
            'REVIEW_ITEM_CHANGED', 'Select current review items once each', 409)
    reviews.approve_step(db, actor, workspace, value, record_decision=False)
    before = workspace_content(db, workspace)
    remove_rejected_adjustments(workspace, selected)
    workspace.accepted_issues = [row for row in workspace.accepted_issues or [] if row.get('cause_id') not in selected]
    workspace.status = 'draft'
    workspace.submitted_fingerprint = None
    workspace.submitted_at = None
    workspace.review_snapshot = None
    person = db.get(m.Identity, actor.identity_id)
    workspace.return_note = {'by': actor.identity_id, 'by_name': person.display_name,
                            'at': m.now().isoformat(), 'reason': value.reason}
    planning.outbox(db, actor, f'review_rejected:{workspace.id}:{workspace.content_revision}',
                    'returned', {'workspace_id': workspace.id})
    reply = planning.changed(db, actor, workspace, before, value.reason, review_rejection=True)
    return {**reply, 'fix_list': projection(workspace), 'review_items': review_items(workspace),
            'gap_acknowledgements': audit_rows(workspace),
            'review_item_decisions': decision_rows(actor, value),
            'rejected_review_items': [items[key] for key in sorted(selected)]}


def record_decision(actor, workspace, value, decision):
    if not value.decisions:
        return
    self_approval = {'self_approval_id': decision.self_approval_id} if decision.self_approval_id else {}
    rows = decision_rows(actor, value, self_approval=self_approval)
    workspace.validation = {**(workspace.validation or {}), 'review_item_decisions': rows}
    gaps = {row['item_id']: {'by': row['by'], 'at': row['at'], 'reason': row['reason'],
                           'review_decision_id': decision.id, **self_approval}
            for row in rows if row['kind'] == 'gap'}
    if gaps:
        workspace.accepted_issues = [{**a, 'decisions': [*a.get('decisions', []), gaps[a['cause_id']]]}
                                     if a.get('cause_id') in gaps else a for a in workspace.accepted_issues or []]


def decision_rows(actor, value, *, self_approval=None):
    return [{**item.model_dump(mode='json'), 'reason': item.reason or value.reason,
             'by': actor.identity_id, 'at': m.now().isoformat(), **(self_approval or {})} for item in value.decisions]


def reopen_undo(db, actor, workspace, reason):
    """Reopening increments content revision; retain an acknowledgement-only Undo at that revision."""
    if not any(a.get('cause_id') for a in workspace.accepted_issues or []):
        return
    before = workspace_content(db, workspace)
    before['workspace']['accepted_issues'] = [a for a in workspace.accepted_issues or [] if not a.get('cause_id')]
    db.add(m.ChangeSet(workspace_id=workspace.id, before_snapshot=before,
        resulting_content_revision=workspace.content_revision + 1, actor_id=actor.identity_id, reason=reason))


def audit_rows(workspace):
    grouped = {}
    for row in workspace.accepted_issues or []:
        if row.get('cause_id'):
            grouped[row['cause_id']] = {k: v for k, v in row.items() if k != 'key'}
    return list(grouped.values())
