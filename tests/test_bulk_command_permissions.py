"""Merged bulk commands and receipts use the current shared permission map."""
import ast
import inspect
import re
import textwrap
from types import SimpleNamespace
import uuid

import pytest

from apps.api.auth import Actor
from apps.api import commands
from apps.api.commands import COMMAND_PERMISSIONS, authorize_receipt, command_roles
from apps.api.errors import Problem
from packages.contracts import schemas as s
from .test_bulk import people_change, preview


# Snapshot of the head's fall-through result.  These concrete dispatcher kinds are
# intentionally kept as a role-preservation snapshot, including job.attach/release
# which are handled but omitted from the audit catalogue.
HEAD_FALLTHROUGH_ROLES = {
    kind: ("coordinator",) for kind in (
        "workspace.share", "workspace.designate", "workspace.archive_due", "workspace.merge.preview",
        "workspace.merge.apply", "swap.prepare", "workspace.create", "workspace.rule_adjust",
        "workspace.target_override", "draft.swap", "draft.move", "workspace.undo", "workspace.redo",
        "workspace.resolve_overlap", "series.reconcile", "series.expand", "workspace.validate",
        "workspace.submit", "request.link_rota", "import.preview", "import.reference_preview",
        "import.refresh", "import.apply", "job.create", "job.attach", "job.release", "job.dismiss",
        "job.add_time", "job.stop_keep_best", "job.continue", "generation.apply")}
HEAD_FALLTHROUGH_ROLES["membership.withdraw"] = ("coordinator",)  # fell through on the head as well
# The ONE deliberate difference (Master, 10 Nov): the gate now follows the declared permission
# ('admin', 'coordinator') that the handler already enforced. Any other difference fails the test below.
DELIBERATE_ROLE_CHANGES = {"membership.withdraw": ("admin", "coordinator")}


# Generated once from command_roles() on the maintenance head ee5e5c464 for every dispatcher-registry kind
# (empty payload). The new gate must match it exactly, except DELIBERATE_ROLE_CHANGES.
HEAD_ROLES = {
    'absence_report.settings': ('admin',),
    'account.end_other_sessions': ('staff', 'coordinator', 'approver', 'credentialer', 'admin', 'auditor', 'programme_lead', 'operator'),
    'account.end_sessions': ('admin',),
    'account.first_sign_in': ('admin',),
    'account.invite': ('admin',),
    'account.invite_accepted': ('admin',),
    'account.password_change': ('admin',),
    'account.password_reset': ('admin',),
    'account.resend': ('admin',),
    'account.reset_link': ('admin',),
    'account.revoke_invite': ('admin',),
    'ai_provider.delete': ('operator',),
    'ai_provider.enabled': ('operator',),
    'ai_provider.key': ('operator',),
    'ai_provider.restore': ('operator',),
    'ai_provider.save': ('operator',),
    'ai_provider.test': ('operator',),
    'availability.record': ('coordinator', 'programme_lead'),
    'availability.restore': ('coordinator', 'programme_lead'),
    'availability.retract': ('coordinator', 'programme_lead'),
    'availability.undo_create': ('coordinator', 'programme_lead'),
    'availability.undo_update': ('coordinator',),
    'availability.update': ('coordinator',),
    'availability_pattern.decide': ('coordinator',),
    'availability_pattern.end': ('staff', 'coordinator'),
    'availability_pattern.propose': ('staff', 'coordinator'),
    'availability_pattern.restore': ('staff', 'coordinator'),
    'availability_pattern.undo': ('coordinator',),
    'branding.undo': ('admin',),
    'branding.update': ('admin',),
    'bulk.apply': ('staff', 'coordinator', 'approver', 'credentialer', 'admin', 'auditor', 'programme_lead', 'operator'),
    'bulk.stage': ('coordinator', 'approver', 'credentialer', 'admin', 'programme_lead', 'operator'),
    'bulk.staged.drop': ('staff', 'coordinator', 'approver', 'credentialer', 'admin', 'auditor', 'programme_lead', 'operator'),
    'calendar_feed.create': ('staff', 'coordinator', 'approver', 'credentialer', 'admin', 'auditor', 'programme_lead', 'operator'),
    'calendar_feed.revoke': ('staff', 'coordinator', 'approver', 'credentialer', 'admin', 'auditor', 'programme_lead', 'operator'),
    'calendar_feed.settings': ('staff', 'coordinator', 'approver', 'credentialer', 'admin', 'auditor', 'programme_lead', 'operator'),
    'catalogue.bulk_update': ('admin',),
    'catalogue.create': ('admin',),
    'catalogue.undo_create': ('admin',),
    'catalogue.update': ('admin',),
    'closure.create': ('admin', 'coordinator'),
    'date_set.adjust': ('admin',),
    'date_set.copy_next_year': ('admin',),
    'date_set.delete': ('admin',),
    'date_set.entry.create': ('admin',),
    'date_set.entry.delete': ('admin',),
    'date_set.entry.update': ('admin',),
    'date_set.import': ('admin',),
    'department.bulk_settings': ('admin',),
    'department.create': ('operator',),  # maintenance create-department, operator-only; maintenance governance PASS 11 Nov
    'department.display': ('admin',),
    'department.display.restore': ('admin',),
    'department.sessions': ('admin',),
    'department.settings': ('admin',),
    'department_content.remind': ('admin', 'coordinator'),
    'department_content.seen': ('staff', 'coordinator', 'approver', 'credentialer', 'admin', 'auditor', 'programme_lead', 'operator'),
    'draft.move': ('coordinator',),
    'draft.swap': ('coordinator',),
    'eligibility.bulk_create': ('credentialer',),
    'eligibility.create': ('credentialer',),
    'eligibility.end': ('credentialer',),
    'eligibility.extend': ('credentialer',),
    'eligibility.programme': ('credentialer',),
    'eligibility.undo_create': ('credentialer',),
    'eligibility.update_dates': ('credentialer',),
    'eligibility.withdraw': ('credentialer',),
    'email.microsoft.connect': ('admin', 'coordinator'),
    'email.microsoft.disconnect': ('admin', 'coordinator'),
    'email.settings': ('admin',),
    'event.occurrence.update': ('admin', 'coordinator'),
    'event.rsvp': ('staff', 'coordinator', 'approver', 'credentialer', 'admin', 'auditor', 'programme_lead', 'operator'),
    'event_types.import_defaults': ('admin',),
    'exception.approve': ('approver',),
    'exception.create': ('coordinator',),
    'exception.create_many': ('coordinator',),
    'exception.end': ('coordinator', 'approver'),
    'exception.undo': ('coordinator', 'approver'),
    'feedback.create': ('staff', 'coordinator', 'approver', 'credentialer', 'admin', 'auditor', 'programme_lead', 'operator'),
    'feedback.export_github': ('operator',),
    'feedback.undo': ('operator',),
    'feedback.update': ('operator',),
    'generation.apply': ('coordinator',),
    'grant.bulk_create': ('credentialer',),
    'grant.create': ('credentialer',),
    'grant.restore': ('credentialer',),
    'grant.revoke': ('credentialer',),
    'grant.undo_create': ('credentialer',),
    'grant.update_dates': ('credentialer',),
    'identity.bind': ('admin',),
    'identity.disable': ('admin',),
    'identity.enable': ('admin',),
    'import.apply': ('coordinator',),
    'import.preview': ('coordinator',),
    'import.reference_preview': ('coordinator',),
    'import.refresh': ('coordinator',),
    'integration.github.connect': ('operator',),
    'integration.github.disconnect': ('operator',),
    'integration.github.test': ('operator',),
    'issues.resolve': ('coordinator',),
    'job.add_time': ('coordinator',),
    'job.attach': ('coordinator',),
    'job.cancel': ('coordinator', 'operator'),
    'job.continue': ('coordinator',),
    'job.create': ('coordinator',),
    'job.dismiss': ('coordinator',),
    'job.release': ('coordinator',),
    'job.stop_keep_best': ('coordinator',),
    'membership.bulk_create': ('admin', 'coordinator'),
    'membership.create': ('admin', 'coordinator'),
    'membership.end': ('admin', 'coordinator'),
    'membership.undo_create': ('admin', 'coordinator'),
    'membership.update_dates': ('admin', 'coordinator'),
    'membership.withdraw': ('coordinator',),
    'notification.ack': ('staff', 'coordinator', 'approver', 'credentialer', 'admin', 'auditor', 'programme_lead', 'operator'),
    'notification.ack_all': ('staff', 'coordinator', 'approver', 'credentialer', 'admin', 'auditor', 'programme_lead', 'operator'),
    'notification.acknowledge': ('staff', 'coordinator', 'approver', 'credentialer', 'admin', 'auditor', 'programme_lead', 'operator'),
    'notification.policy.undo': ('admin',),
    'notification.policy.update': ('admin',),
    'notification.preferences.undo': ('staff', 'coordinator', 'approver', 'credentialer', 'admin', 'auditor', 'programme_lead', 'operator'),
    'notification.preferences.update': ('staff', 'coordinator', 'approver', 'credentialer', 'admin', 'auditor', 'programme_lead', 'operator'),
    'notification.unack': ('staff', 'coordinator', 'approver', 'credentialer', 'admin', 'auditor', 'programme_lead', 'operator'),
    'outbox.retry': ('operator',),
    'person.anonymise': ('admin',),
    'person.contact.set': ('staff', 'coordinator', 'approver', 'credentialer', 'admin', 'auditor', 'programme_lead', 'operator'),
    'planner_view.create': ('approver', 'auditor', 'coordinator', 'programme_lead'),
    'planner_view.delete': ('approver', 'auditor', 'coordinator', 'programme_lead'),
    'planner_view.update': ('approver', 'auditor', 'coordinator', 'programme_lead'),
    'planning.settings': ('admin',),
    'planning.settings.rollback': ('admin',),
    'planning_group.create': ('admin',),
    'planning_group.rollback': ('admin',),
    'planning_group.update': ('admin',),
    'policy.activate': ('approver',),
    'policy.activate_many': ('approver',),
    'policy.create': ('admin', 'coordinator'),
    'policy.decline': ('approver', 'coordinator', 'admin'),
    'policy.group': ('admin', 'coordinator'),
    'policy.restore': ('approver',),
    'policy.restore_proposal': ('approver', 'coordinator', 'admin'),
    'policy.retire': ('approver',),
    'policy.withdraw_approval': ('approver',),
    'publication.change': ('coordinator',),
    'publication.change.undo': ('coordinator',),
    'publication.changes.prepare': ('coordinator',),
    'publication.changes.submit': ('coordinator',),
    'publication.changes.withdraw': ('coordinator',),
    'push.rotate': ('staff', 'coordinator', 'approver', 'credentialer', 'admin', 'auditor', 'programme_lead', 'operator'),
    'push.subscribe': ('staff', 'coordinator', 'approver', 'credentialer', 'admin', 'auditor', 'programme_lead', 'operator'),
    'push.test': ('staff', 'coordinator', 'approver', 'credentialer', 'admin', 'auditor', 'programme_lead', 'operator'),
    'push.unsubscribe': ('staff', 'coordinator', 'approver', 'credentialer', 'admin', 'auditor', 'programme_lead', 'operator'),
    'pwa.provider.resume': ('operator',),
    'pwa.system.update': ('operator',),
    'request.create': ('staff', 'coordinator'),
    'request.edit': ('staff', 'coordinator'),
    'request.undo_edit': ('staff', 'coordinator'),
    'request.decide': ('approver', 'coordinator', 'programme_lead'),
    'request.decide_many': ('approver', 'coordinator', 'programme_lead'),
    'request.link_rota': ('coordinator',),
    'request.undo': ('staff', 'coordinator', 'approver', 'credentialer', 'admin', 'auditor', 'programme_lead', 'operator'),
    'review_delegation.create': ('approver', 'programme_lead'),
    'review_delegation.restore': ('admin', 'approver', 'programme_lead'),
    'review_delegation.revoke': ('admin', 'approver', 'programme_lead'),
    'review_route.activate': ('approver',),
    'review_route.create': ('admin',),
    'review_route.retire': ('approver',),
    'review_route.update': ('admin',),
    'role.grant': ('admin', 'operator'),
    'role.restore': ('admin', 'operator'),
    'role.revoke': ('admin', 'operator'),
    'rotation.copy': ('admin', 'coordinator', 'programme_lead'),
    'rotation.plan': ('admin', 'coordinator', 'programme_lead'),
    'rotation.remove': ('admin', 'coordinator', 'programme_lead'),
    'rotation.request_change': ('staff', 'coordinator', 'programme_lead'),
    'rotation.swap': ('staff', 'coordinator', 'programme_lead'),
    'rotation.undo_plan': ('admin', 'coordinator', 'programme_lead'),
    'rule.acknowledge': ('admin', 'coordinator'),
    'rule.catalogue.check': ('coordinator', 'approver'),
    'rule.migrate_to_catalogue': ('admin', 'coordinator'),
    'rule.propose': ('admin', 'coordinator'),
    'rule.unacknowledge': ('admin', 'coordinator'),
    'score.settings': ('admin',),
    'score.settings.rollback': ('admin',),
    'score_check.auto': ('coordinator',),
    'score_check.benchmark': ('coordinator',),
    'score_check.check': ('coordinator',),
    'score_check.decide': ('coordinator',),
    'score_check.save': ('coordinator',),
    'score_check.undo': ('coordinator',),
    'score_check.update': ('coordinator',),
    'score_check.upload': ('coordinator',),
    'score_import.add_files': ('coordinator',),
    'score_import.check': ('coordinator',),
    'score_import.decide': ('coordinator',),
    'score_import.discard': ('coordinator',),
    'score_import.profile': ('coordinator',),
    'score_import.remove_source': ('coordinator',),
    'score_import.restore': ('coordinator',),
    'score_import.restore_source': ('coordinator',),
    'score_import.save': ('coordinator',),
    'score_import.time': ('coordinator',),
    'score_import.update_source': ('coordinator',),
    'score_import.upload': ('coordinator',),
    'self_approval.grant': ('admin',),
    'self_approval.restore': ('admin',),
    'self_approval.revoke': ('admin',),
    'series.expand': ('coordinator',),
    'series.occurrence.cancel': ('admin',),
    'series.reconcile': ('coordinator',),
    'session.revoke': ('staff', 'coordinator', 'approver', 'credentialer', 'admin', 'auditor', 'programme_lead', 'operator'),
    'shift.action': ('coordinator',),
    'shift.copy': ('coordinator',),
    'shift.create': ('coordinator',),
    'shift.undo_batch': ('coordinator',),
    'shift.update': ('coordinator',),
    'staff_import.apply': ('admin', 'coordinator', 'credentialer'),
    'staff_import.preview': ('admin', 'coordinator', 'credentialer'),
    'staff_import.undo': ('admin', 'coordinator', 'credentialer'),
    'swap.accept': ('staff',),
    'swap.confirm': ('staff',),
    'swap.counteroffer': ('staff',),
    'swap.offer': ('staff',),
    'swap.prepare': ('coordinator',),
    'swap.reject': ('coordinator', 'approver'),
    'swap.undo': ('staff', 'coordinator', 'approver'),
    'swap.withdraw': ('staff',),
    'table_view.create': ('staff', 'coordinator', 'approver', 'credentialer', 'admin', 'auditor', 'programme_lead', 'operator'),
    'table_view.delete': ('staff', 'coordinator', 'approver', 'credentialer', 'admin', 'auditor', 'programme_lead', 'operator'),
    'table_view.set_default': ('staff', 'coordinator', 'approver', 'credentialer', 'admin', 'auditor', 'programme_lead', 'operator'),
    'table_view.update': ('staff', 'coordinator', 'approver', 'credentialer', 'admin', 'auditor', 'programme_lead', 'operator'),
    'tag.members': ('admin', 'coordinator'),
    'timeline.rotation': ('staff', 'coordinator', 'programme_lead', 'admin'),
    'trainee_note.create': ('programme_lead',),
    'trainee_note.delete': ('programme_lead',),
    'trainee_note.restore': ('programme_lead',),
    'trainee_note.update': ('programme_lead',),
    'wish.create': ('staff', 'coordinator'),
    'wish.override': ('staff', 'coordinator'),
    'wish.restore': ('staff', 'coordinator'),
    'wish.update': ('staff', 'coordinator'),
    'wish.withdraw': ('staff', 'coordinator'),
    'workspace.approve': ('approver', 'programme_lead'),
    'workspace.archive': ('admin', 'coordinator'),
    'workspace.archive_due': ('coordinator',),
    'workspace.baseline.capture': ('coordinator',),
    'workspace.baseline.refresh.apply': ('coordinator',),
    'workspace.baseline.refresh.preview': ('coordinator',),
    'workspace.create': ('coordinator',),
    'workspace.delete_draft': ('admin', 'coordinator'),
    'workspace.designate': ('coordinator',),
    'workspace.merge.apply': ('coordinator',),
    'workspace.merge.preview': ('coordinator',),
    'workspace.publish': ('coordinator', 'approver'),
    'workspace.redo': ('coordinator',),
    'workspace.reopen': ('coordinator', 'approver', 'programme_lead'),
    'workspace.resolve_overlap': ('coordinator',),
    'workspace.restore': ('admin', 'coordinator'),
    'workspace.rule_adjust': ('coordinator',),
    'workspace.share': ('coordinator',),
    'workspace.submit': ('coordinator',),
    'workspace.target_override': ('coordinator',),
    'workspace.undo': ('coordinator',),
    'workspace.validate': ('coordinator',),
}


_COMMAND_KIND = re.compile(r"[a-z][a-z_]*(?:\.[a-z][a-z_]*){1,3}")


def _dispatcher_registry():
    source = textwrap.dedent(inspect.getsource(commands.dispatch))
    tree = ast.parse(source)
    literals = {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and _COMMAND_KIND.fullmatch(node.value)
    }
    prefixes = {
        value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "startswith"
        for value_node in ([node.args[0]] if node.args else [])
        for value in (
            [value_node.value]
            if isinstance(value_node, ast.Constant)
            else [item.value for item in value_node.elts]
            if isinstance(value_node, (ast.Tuple, ast.List, ast.Set))
            else []
        )
    }
    # Literals without the command-kind shape (payload labels, service names and
    # other protocol strings) are intentionally excluded from the dispatcher set.
    # Audit entries are the concrete registry for prefix families.  Entries outside a
    # dispatcher prefix are audit-only events (for example command.refused), not commands.
    return literals | {
        kind for kind in s.AUDIT_ACTION_CATALOGUE
        if any(kind.startswith(prefix) for prefix in prefixes)
    }


def test_registered_commands_have_explicit_authorization():
    registered = _dispatcher_registry()
    assert set(HEAD_FALLTHROUGH_ROLES) <= registered
    for kind in sorted(registered):
        try:
            command_roles(s.Command(kind=kind))
        except Problem as error:
            if error.status == 403 and error.code == "FORBIDDEN":
                pytest.fail(f"registered command {kind!r} has no explicit authorization")
            raise


def test_fallthrough_command_roles_are_preserved_after_mapping():
    assert set(DELIBERATE_ROLE_CHANGES) <= set(HEAD_FALLTHROUGH_ROLES)
    for kind, head_roles in HEAD_FALLTHROUGH_ROLES.items():
        expected = DELIBERATE_ROLE_CHANGES.get(kind, head_roles)
        assert tuple(command_roles(s.Command(kind=kind))) == expected, kind
        if kind not in DELIBERATE_ROLE_CHANGES:
            assert COMMAND_PERMISSIONS[kind] == "planning.draft"
    assert command_roles(s.Command(kind="job.create", payload={"kind": "feasibility"})) == ["coordinator", "approver"]


@pytest.mark.parametrize("role", s.ROLE_NAMES)
def test_unknown_command_is_forbidden_for_every_role(role):
    with pytest.raises(Problem) as caught:
        command_roles(s.Command(kind="unknown.g11.command"))
    assert role in s.ROLE_NAMES
    assert caught.value.status == 403 and caught.value.code == "FORBIDDEN"


@pytest.mark.parametrize("kind", [*s.BULK_ITEM_KINDS, "bulk.apply", "bulk.stage", "bulk.staged.drop", "department.bulk_settings"])
def test_every_bulk_kind_reads_the_shared_permission_map(kind, monkeypatch):
    permission = {'membership.restore': 'membership.withdraw', 'eligibility.restore': 'eligibility.withdraw',
                  'programme_membership.withdraw': 'catalogue.programmes',
                  'programme_membership.restore': 'catalogue.programmes', 'rotation.restore': 'rotation.remove'}.get(kind, kind)
    assert COMMAND_PERMISSIONS[kind] == permission
    monkeypatch.setitem(s.PERMISSIONS, permission, ("auditor",))
    monkeypatch.setitem(s.PERMISSIONS, "access.system_roles", ())
    assert command_roles(s.Command(kind=kind)) == ["auditor"]


@pytest.mark.parametrize("kind", ["catalogue.create", "catalogue.update", "catalogue.undo_create", "catalogue.bulk_update"])
@pytest.mark.parametrize("catalogue,permission", [("services", None), ("people", "catalogue.department"),
                                                 ("programmes", "catalogue.programmes")])
def test_catalogue_roles_preserve_the_catalogue_context(kind, catalogue, permission, monkeypatch):
    monkeypatch.setitem(s.PERMISSIONS, permission or kind, ("auditor",))
    assert command_roles(s.Command(kind=kind, payload={"catalogue": catalogue})) == ["auditor"]


def test_withdrawal_and_system_role_authority_keep_their_current_permission_context(monkeypatch):
    monkeypatch.setitem(s.PERMISSIONS, "request.decide", ("approver",))
    monkeypatch.setitem(s.PERMISSIONS, "request.withdraw", ("staff",))
    assert command_roles(s.Command(kind="request.decide", payload={"decision": "approve"})) == ["approver"]
    assert command_roles(s.Command(kind="request.decide", payload={"decision": "withdraw"})) == ["staff"]
    monkeypatch.setitem(s.PERMISSIONS, "access.system_roles", ("coordinator",))
    for kind in ("role.grant", "role.revoke", "role.restore"):
        assert command_roles(s.Command(kind=kind)) == ["admin", "coordinator"]


@pytest.mark.parametrize("bulk", [False, True])
def test_item_permission_narrowing_reauthorizes_preview_write_retry_and_receipt(h, monkeypatch, bulk):
    value = people_change(h)
    kind = "bulk.apply" if bulk else "catalogue.create"
    payload = value if bulk else {"catalogue": "people", "value": {"display_name": "Synthetic permission receipt"},
                                 "reason": "Synthetic permission receipt"}
    key = str(uuid.uuid4())
    result = h.command("coordinator", kind, payload, key=key)
    manifest = result["authorization_commands"]
    assert all(part == {"kind": "catalogue.update" if bulk else kind, "payload": {"catalogue": "people"}}
               for part in manifest)
    monkeypatch.setitem(s.PERMISSIONS, "catalogue.department", ("staff",))
    h.get("coordinator", "/api/commands/" + key, status=403)
    h.command("coordinator", kind, payload, key=key, status=403)
    check = preview(h, "coordinator", value)
    assert all(row["issues"][0]["code"] == "FORBIDDEN" for row in check["items"])
    monkeypatch.setitem(s.PERMISSIONS, "catalogue.department", ("coordinator",))
    assert h.get("coordinator", "/api/commands/" + key)["result"] == result
    assert h.command("coordinator", kind, payload, key=key) == result


def test_bulk_wrapper_permission_applies_to_preview_write_and_receipt(h, monkeypatch):
    value = people_change(h)
    key = str(uuid.uuid4())
    h.command("coordinator", "bulk.apply", value, key=key)
    monkeypatch.setitem(s.PERMISSIONS, "bulk.apply", ("approver",))
    response = h.client("coordinator").post("/api/commands/preview", json={"kind": "bulk.apply", "payload": value})
    assert response.status_code == 403
    h.command("coordinator", "bulk.apply", value, key=key, status=403)
    h.get("coordinator", "/api/commands/" + key, status=403)


def test_legacy_dynamic_receipt_retains_its_original_authorization_without_payload_context():
    actor = Actor("identity", "department", "organisation", "Synthetic", frozenset({"staff"}), None, "session")
    for kind in ("catalogue.update", "request.decide"):
        receipt = SimpleNamespace(command_kind=kind, required_roles=["approver"], result={})
        with pytest.raises(Problem) as caught:
            authorize_receipt(None, actor, receipt)
        assert caught.value.status == 403


def test_job_feasibility_receipt_replay_uses_recorded_kind():
    actor = Actor("identity", "department", "organisation", "Synthetic", frozenset({"approver"}), None, "session")
    assert commands.authorization_command(s.Command(kind="job.create", payload={"kind": "feasibility"})) == {
        "kind": "job.create", "payload": {"kind": "feasibility"}}
    receipt = SimpleNamespace(command_kind="job.create", required_roles=["coordinator", "approver"], result={
        "authorization_commands": [{"kind": "job.create", "payload": {"kind": "feasibility"}}],
        "job": {"kind": "generation"},
    })
    authorize_receipt(None, actor, receipt)


def test_job_generation_receipt_replay_rejects_approver():
    actor = Actor("identity", "department", "organisation", "Synthetic", frozenset({"approver"}), None, "session")
    receipt = SimpleNamespace(command_kind="job.create", required_roles=["coordinator"], result={
        "authorization_commands": [{"kind": "job.create", "payload": {"kind": "generation"}}],
        "job": {"kind": "generation"},
    })
    with pytest.raises(Problem) as caught:
        authorize_receipt(None, actor, receipt)
    assert caught.value.status == 403


def test_job_feasibility_receipt_replay_rechecks_current_role():
    actor = Actor("identity", "department", "organisation", "Synthetic", frozenset({"staff"}), None, "session")
    receipt = SimpleNamespace(command_kind="job.create", required_roles=["coordinator", "approver"], result={
        "authorization_commands": [{"kind": "job.create", "payload": {"kind": "feasibility"}}],
        "job": {"kind": "feasibility"},
    })
    with pytest.raises(Problem) as caught:
        authorize_receipt(None, actor, receipt)
    assert caught.value.status == 403


def test_legacy_job_receipt_falls_back_to_result_kind_or_coordinator_only():
    approver = Actor("identity", "department", "organisation", "Synthetic", frozenset({"approver"}), None, "session")
    legacy = SimpleNamespace(command_kind="job.create", required_roles=["coordinator", "approver"],
                             result={"job": {"kind": "feasibility"}})
    authorize_receipt(None, approver, legacy)
    without_context = SimpleNamespace(command_kind="job.create", required_roles=["coordinator", "approver"], result={})
    with pytest.raises(Problem) as caught:
        authorize_receipt(None, approver, without_context)
    assert caught.value.status == 403
    coordinator = Actor("identity", "department", "organisation", "Synthetic", frozenset({"coordinator"}), None, "session")
    authorize_receipt(None, coordinator, without_context)
    legacy.result["job"]["kind"] = "generation"
    with pytest.raises(Problem) as caught:
        authorize_receipt(None, approver, legacy)
    assert caught.value.status == 403


@pytest.mark.parametrize("legacy", [False, True])
def test_job_receipt_replay_uses_the_current_permission_map(monkeypatch, legacy):
    context = {"kind": "job.create", "payload": {"kind": "generation"}}
    result = {"job": {"kind": "generation"}}
    if not legacy:
        result["authorization_commands"] = [context]
    receipt = SimpleNamespace(command_kind="job.create", required_roles=["coordinator"], result=result)
    actor = Actor("identity", "department", "organisation", "Synthetic", frozenset({"coordinator"}), None, "session")
    monkeypatch.setitem(s.PERMISSIONS, COMMAND_PERMISSIONS["job.create"], ("auditor",))
    with pytest.raises(Problem) as caught:
        authorize_receipt(None, actor, receipt)
    assert caught.value.status == 403
    auditor = Actor("identity", "department", "organisation", "Synthetic", frozenset({"auditor"}), None, "session")
    authorize_receipt(None, auditor, receipt)


def test_job_receipt_without_kind_uses_the_current_permission_map(monkeypatch):
    receipt = SimpleNamespace(command_kind="job.create", required_roles=["coordinator"], result={})
    actor = Actor("identity", "department", "organisation", "Synthetic", frozenset({"coordinator"}), None, "session")
    monkeypatch.setitem(s.PERMISSIONS, COMMAND_PERMISSIONS["job.create"], ("auditor",))
    with pytest.raises(Problem) as caught:
        authorize_receipt(None, actor, receipt)
    assert caught.value.status == 403
    auditor = Actor("identity", "department", "organisation", "Synthetic", frozenset({"auditor"}), None, "session")
    authorize_receipt(None, auditor, receipt)


def test_every_registered_command_keeps_its_head_roles_except_the_decided_exception():
    """Whole-registry before/after (maintenance): one decided difference only (Master, 10 Nov: membership.withdraw)."""
    registered = _dispatcher_registry()
    added = {'membership.restore': s.PERMISSIONS['membership.withdraw'],
             'eligibility.restore': s.PERMISSIONS['eligibility.withdraw'],
             'programme_membership.withdraw': s.PERMISSIONS['catalogue.programmes'],
             'programme_membership.restore': s.PERMISSIONS['catalogue.programmes'],
             'rotation.restore': s.PERMISSIONS['rotation.remove']}
    expected = HEAD_ROLES | added
    assert registered == set(expected), sorted(registered ^ set(expected))
    for kind, head_roles in expected.items():
        assert tuple(command_roles(s.Command(kind=kind))) == DELIBERATE_ROLE_CHANGES.get(kind, head_roles), kind
