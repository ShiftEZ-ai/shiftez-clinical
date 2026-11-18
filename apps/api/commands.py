"""Explicit command bus: idempotency, reauthorization, scoped audit, single commit."""
from . import input_changes
import json
import logging
import re
import time
from sqlalchemy import select, text
from types import SimpleNamespace
from sqlalchemy.exc import IntegrityError, OperationalError
from packages.contracts import schemas as s
from . import models as m
from .auth import authenticate, require_permission, require_role
from .db import DatabaseWriteTimeout, retry_write, transaction
from . import live
from .schema import require_current_schema
from .errors import Problem, require
from .serialization import compact_reply, json_value, record
from .snapshots import snapshot_hash, fingerprint
from .services import table_views, availability_patterns, request_batches
from .services import staff_import
from .services import planner_tools, catalogue, planning, policies, availability, access, imports, jobs, reviews, swaps, rule_catalogue
from .services.common import scoped, expected, PROGRAMME_CATALOGUES


log = logging.getLogger(__name__)

ALL_ROLES = list(s.ROLE_NAMES)
NON_SERIALIZED_COMMANDS = frozenset({"department_content.seen"})
COMMAND_PERMISSIONS = {
    "department.create": "department.create",
    **{kind: kind for kind in s.BULK_ITEM_KINDS},
    **dict.fromkeys(('workspace.archive', 'workspace.restore', 'workspace.delete_draft'), 'workspace.archive'),
    **dict.fromkeys(('score.settings', 'score.settings.rollback'), 'planning.configure'),
    **dict.fromkeys(('planning_group.create', 'planning_group.update', 'planning_group.rollback',
                     'planning.settings', 'planning.settings.rollback'), 'planning.configure'),
    "membership.withdraw": "membership.withdraw",
    "membership.restore": "membership.withdraw",
    "eligibility.restore": "eligibility.withdraw",
    "programme_membership.withdraw": "catalogue.programmes",
    "programme_membership.restore": "catalogue.programmes",
    "rotation.restore": "rotation.remove",
    "rotation.swap": "rotation.swap",
    "wish.override": "wish.override",
    "policy.withdraw_approval": "policy.retire",
    **{kind: kind for kind in ("bulk.apply", "bulk.stage", "bulk.staged.drop", "department.bulk_settings")},
    # These dispatcher-only commands all retain their existing coordinator-only authority.
    **dict.fromkeys((
        "workspace.share", "workspace.designate", "workspace.archive_due", "workspace.merge.preview",
        "workspace.merge.apply", "swap.prepare", "workspace.create", "workspace.rule_adjust",
        "workspace.target_override", "draft.swap", "draft.move", "workspace.undo", "workspace.redo",
        "workspace.resolve_overlap", "series.reconcile", "series.expand", "workspace.validate",
        "workspace.submit", "request.link_rota", "import.preview", "import.reference_preview",
        "import.refresh", "import.apply", "job.create", "job.attach", "job.release", "job.dismiss",
        "job.add_time", "job.stop_keep_best", "job.continue", "generation.apply"), "planning.draft"),
    **{kind: kind for kind in ('email.settings', 'event_types.import_defaults', 'email.microsoft.connect', 'email.microsoft.disconnect', 'event.occurrence.update', 'department_content.remind', 'event.rsvp', 'department_content.seen', 'notification.acknowledge', 'rotation.plan', 'rotation.copy', 'rotation.undo_plan', 'rotation.remove', 'rotation.request_change', 'trainee_note.create', 'trainee_note.update', 'trainee_note.delete', 'trainee_note.restore')},
    "feedback.update": "feedback.triage",
    "feedback.undo": "feedback.triage",
    "feedback.export_github": "feedback.export_github",
    **dict.fromkeys(("integration.github.connect", "integration.github.test", "integration.github.disconnect"),
                    "integrations.github"),
}

def logged_command_kind(kind):
    return kind if len(kind) <= 64 and re.fullmatch(r"[a-z][a-z_]*(\.[a-z][a-z_]*){1,3}", kind) else "unknown"


def command_roles(command):
    kind = command.kind
    if kind == "person.contact.set":
        return ALL_ROLES
    if kind in {"push.subscribe", "push.rotate", "push.unsubscribe", "push.test", "notification.preferences.update", "notification.preferences.undo"}:
        return ALL_ROLES
    if kind in {"notification.policy.update", "notification.policy.undo"}:
        return list(s.PERMISSIONS["notification.policy.update"])
    if kind in {"pwa.system.update", "pwa.provider.resume"}:
        return ["operator"]
    if kind == "absence_report.settings":
        return ["admin"]
    if kind.startswith("publication.change"):
        return ["coordinator"]
    if kind.startswith(("score_check.", "score_import.")):
        return ["coordinator"]
    if kind.startswith("ai_provider."):
        return list(s.PERMISSIONS['ai.providers'])
    if kind == 'timeline.rotation':
        return list(s.PERMISSIONS['rotation.request_change']) + ['admin']
    if kind == "job.create" and command.payload.get("kind") == "feasibility":
        return ["coordinator", "approver"]  # a rule check changes nothing; the approver may run it before activating
    if kind in COMMAND_PERMISSIONS:
        permission = COMMAND_PERMISSIONS[kind]
        if kind.startswith("catalogue."):
            catalogue = command.payload.get("catalogue")
            if catalogue in PROGRAMME_CATALOGUES:
                permission = "catalogue.programmes"
            elif catalogue in {"people", "department_content", "tags", "events"}:
                permission = "catalogue.department"
        elif kind == "request.decide" and command.payload.get("decision") == "withdraw":
            permission = "request.withdraw"
        roles = list(s.PERMISSIONS[permission])
        if kind.startswith("role."):
            roles += s.PERMISSIONS["access.system_roles"]
        return list(dict.fromkeys(roles))
    for prefix, canonical in (("catalogue.", "catalogue.update"), ("eligibility.", "eligibility.create"),
                              ("grant.", "grant.create"), ("role.", "role.grant")):
        if kind.startswith(prefix):
            return command_roles(command.model_copy(update={"kind": canonical}))
    if kind.startswith("date_set."):
        return ["admin"]
    if kind == 'series.occurrence.cancel':
        return ['admin']
    if kind.startswith("integration.github."):
        return list(s.PERMISSIONS["integrations.github"])
    if kind == "feedback.create":
        return ALL_ROLES
    if kind == 'rule.catalogue.check':
        return ['coordinator', 'approver']
    if kind.startswith("rule.catalogue."):
        action = kind.rsplit(".", 1)[-1]
        if action == 'propose':
            return ['coordinator', 'admin']
        if action in {'decline', 'restore_proposal'}:
            return ['approver', 'coordinator', 'admin']
        return ['approver']
    if kind == "account.end_other_sessions":
        return ALL_ROLES
    if kind in {'branding.update', 'branding.undo', 'department.settings', 'department.display', 'department.display.restore', 'department.sessions'} or kind.startswith('account.'):
        return ['admin']
    if kind.startswith("planner_view."):
        return sorted(planner_tools.PLANNER_ROLES)
    if kind.startswith("staff_import."):
        return ["admin", "coordinator", "credentialer"]  # apply/undo then split the work by these roles
    if kind == "closure.create":
        return ["admin", "coordinator"]
    if kind.startswith(("identity.", "self_approval.")):
        return ["admin"]
    if kind in {"review_route.create", "review_route.update"}:
        return ["admin"]
    if kind in {"review_route.activate", "review_route.retire"}:
        return ["approver"]
    if kind in {"workspace.approve", "review_delegation.create"}:
        return ["approver", "programme_lead"]
    if kind in {"review_delegation.revoke", "review_delegation.restore"}:
        return ["admin", "approver", "programme_lead"]
    if kind in {"swap.offer", "swap.accept", "swap.counteroffer", "swap.confirm", "swap.withdraw"}:
        return ["staff"]
    if kind == "swap.reject":
        return ["coordinator", "approver"]
    if kind == "swap.undo":  # whoever took the step being undone (checked in swaps.undo)
        return ["staff", "coordinator", "approver"]
    if kind == "workspace.publish":
        return list(s.PERMISSIONS["workspace.publish"])
    if kind in {"workspace.baseline.capture", "workspace.baseline.refresh.preview", "workspace.baseline.refresh.apply"}:
        return ["coordinator"]
    if kind == "workspace.reopen":
        return ["coordinator", "approver", "programme_lead"]
    if kind in {"policy.create", "policy.group", "rule.migrate_to_catalogue", "rule.acknowledge", "rule.unacknowledge"}:
        return ["admin", "coordinator"]
    if kind == "request.create":
        return ["staff", "coordinator"]
    if kind in {"notification.ack", "notification.ack_all", "notification.unack", "notification.acknowledge", "session.logout", "session.revoke"} or kind.startswith("table_view.") or kind.startswith("calendar_feed."):
        return ALL_ROLES
    if kind == "outbox.retry":
        return ["operator"]
    require(False, "FORBIDDEN", "Your current role does not permit this action", 403)


def require_workspace_command(db, actor, command):
    if command.kind.startswith("publication.change") or command.kind == "absence_report.settings":
        return
    if command.kind in {'workspace.archive', 'workspace.restore', 'workspace.delete_draft'}:
        # Lifecycle commands are the one supported path for change-list
        # workspaces; editing still remains behind this guard.
        return
    # Rule activation needs receipts for pending-change rotas too; feasibility never edits their duties.
    if command.kind == "job.create" and command.payload.get("kind") == "feasibility":
        return
    targets = [v for v in (command.target_id, command.payload.get('workspace_id')) if isinstance(v, str)]
    if not targets:
        return
    if (bulk := db.info.get('bulk')) is not None:
        # One read per bulk change: change lists are created only on publish and by publication.change commands,
        # neither of which is a bulk item, so the set cannot change during the batch.
        if 'change_list_workspaces' not in bulk:
            bulk['change_list_workspaces'] = set(db.scalars(select(m.PublicationChangeList.workspace_id).where(
                m.PublicationChangeList.department_id == actor.department_id)))
        hidden = set(targets) & bulk['change_list_workspaces'] or None
    else:
        hidden = db.scalar(select(m.PublicationChangeList.id).where(
            m.PublicationChangeList.department_id == actor.department_id,
            m.PublicationChangeList.workspace_id.in_(targets)))
    require(hidden is None, 'PUBLICATION_CHANGE_LIST', 'Edit published duties through their change list', 409)


def dispatch(db, actor, command, settings, *, github_check=None, github_export=None):
    require_workspace_command(db, actor, command)
    if command.kind == "department.create":
        from .services import departments
        return departments.create(db, actor, command, settings)
    if command.kind == "person.contact.set":
        from .services.contacts import set_contact
        return set_contact(db, actor, command)
    if command.kind in {"push.subscribe", "push.rotate", "push.unsubscribe", "push.test", "pwa.system.update", "pwa.provider.resume",
                        "notification.preferences.update", "notification.policy.update", "notification.preferences.undo", "notification.policy.undo"}:
        from .services import pwa
        if command.kind == "push.subscribe":
            return pwa.subscribe(db, actor, command, settings)
        if command.kind == "push.rotate":
            return pwa.subscribe(db, actor, command, settings)
        if command.kind == "push.unsubscribe":
            return pwa.unsubscribe(db, actor, command)
        if command.kind == "notification.preferences.update":
            return pwa.update_preferences(db, actor, command)
        if command.kind == "notification.policy.update":
            return pwa.update_policy(db, actor, command)
        if command.kind == "pwa.provider.resume":
            return pwa.resume_provider(db, actor, command)
        if command.kind == "pwa.system.update":
            return pwa.update_system(db, actor, command)
        if command.kind.endswith('.undo'):
            return pwa.rollback(db, actor, command)
        return pwa.queue_test(db, actor, command, settings)
    if command.kind.startswith("publication.change") or command.kind == "absence_report.settings":
        from .services import published_changes
        return published_changes.command(db, actor, command, settings)
    if command.kind.startswith('planning_group.') or command.kind in {'planning.settings', 'planning.settings.rollback'}:
        from .services import planning_groups
        return planning_groups.configure(db, actor, command)
    if command.kind in {'workspace.share', 'workspace.designate', 'workspace.archive_due',
                        'workspace.merge.preview', 'workspace.merge.apply'}:
        from .services import planning_groups
        if command.kind == 'workspace.merge.preview':
            from .services import workspace_merge
            return workspace_merge.preview(db, actor, command)
        if command.kind == 'workspace.merge.apply':
            from .services import workspace_merge
            return workspace_merge.apply(db, actor, command)
        return planning_groups.workspace_command(db, actor, command, settings)
    if command.kind.startswith("score_import."):
        from .services import score_check_api
        return score_check_api.command(db, actor, command, settings)
    if command.kind.startswith("ai_provider."):
        from .services import score_check_ai
        return score_check_ai.command(db, actor, command, settings)
    if command.kind.startswith("score_check."):
        from .services import score_check
        return score_check.command(db, actor, command, settings)
    if command.kind == "shift.undo_batch":
        require_role(actor, "coordinator")
        return planning.undo(db, actor, command)
    if command.kind == "bulk.apply":
        from .services import bulk
        return bulk.apply(db, actor, command, settings)
    if command.kind in {"bulk.stage", "bulk.staged.drop"}:
        from .services import bulk_staged
        return (bulk_staged.save if command.kind == "bulk.stage" else bulk_staged.drop)(db, actor, command, settings)
    if command.kind == "department.bulk_settings":
        from .services import bulk_settings
        return bulk_settings.update(db, actor, command, settings)
    if command.kind == "availability.update":
        return availability.update_availability(db, actor, command)
    if command.kind == "availability.restore":
        return availability.restore_availability(db, actor, command)
    if command.kind == "availability.undo_update":
        return availability.undo_update_availability(db, actor, command)
    if command.kind == "grant.restore":
        return catalogue.restore_grant(db, actor, command)
    if command.kind == "membership.undo_create":
        return catalogue.undo_created(db, actor, command)
    if command.kind == "catalogue.undo_create":
        return catalogue.undo_catalogue_created(db, actor, command)
    if command.kind in {"eligibility.update_dates", "grant.update_dates", "membership.update_dates"}:
        return catalogue.update_relationship_dates(db, actor, command)
    if command.kind == 'email.settings':
        from .services import email_settings
        return email_settings.save(db, actor, command)
    if command.kind in {'email.microsoft.connect', 'email.microsoft.disconnect'}:
        from .services import mailboxes
        s.Decision.model_validate(command.payload)
        if command.kind.endswith('.connect'):
            db.info['email_authorization_url'] = mailboxes.start(db, actor, settings)['authorization_url']
            return {'connected': False}
        return mailboxes.disconnect(db, actor)
    if command.kind.startswith("date_set."):
        from .services import date_sets
        return date_sets.command(db, actor, command)
    if command.kind.startswith("integration.github."):
        from .services import github_integrations
        if github_check is None and command.kind in {"integration.github.connect", "integration.github.test"}:
            # Direct maintenance dispatch owns an atomic batch; HTTP prepares the GET outside its transaction.
            context, repo, token = github_integrations.prepare_check(db, actor, command, settings)
            ok, expiry = github_integrations.check_repository(repo, token)
            github_check = github_integrations.RepositoryCheck(**context, ok=ok, expiry=expiry)
        return github_integrations.command(db, actor, command, settings, checked=github_check)
    if command.kind == "feedback.create":
        from .services import feedback
        require(command.target_id is None and command.expected_revision is None, "FEEDBACK_CREATE_TARGET",
                "A new feedback report has no existing target or revision")
        return feedback.create(db, actor, s.FeedbackCreate.model_validate(command.payload), settings)
    if command.kind.startswith(("person.workload.", "person.overtime.", "calendar.protection.")):
        from .services import scheduling_facts
        return scheduling_facts.command(db, actor, command, require_checks=settings.generation_engine == 'cp_sat')
    if command.kind.startswith("rule.catalogue."):
        from .services import rule_catalogue_v2
        if command.kind == 'rule.catalogue.check':
            return rule_catalogue_v2.check(db, actor, command)
        return rule_catalogue_v2.transition(db, actor, command)
    kind = command.kind
    if kind == 'event_types.import_defaults':
        from .services import events
        return events.import_defaults(db, actor, command)
    if kind in {'event.rsvp', 'event.occurrence.update'}:
        from .services import events
        return events.rsvp(db, actor, command) if kind == 'event.rsvp' else events.occurrence_update(db, actor, command)
    if kind == "department_content.remind":
        from .services import announcements
        return announcements.remind(db, actor, command)
    if kind == "department_content.seen":
        from .services import announcements
        value = s.DepartmentContentSeen.model_validate(command.payload)
        rows = db.scalars(select(m.DepartmentContent).where(m.DepartmentContent.id.in_(value.ids),
            m.DepartmentContent.department_id == actor.department_id))
        return {"receipts": announcements.mark_read(db, actor, rows)}
    if kind in {"feedback.update", "feedback.undo", "feedback.export_github"}:
        from .services import feedback
        require(command.target_id is None or command.target_id == command.payload.get("id"),
                "FEEDBACK_TARGET", "The feedback target must match the payload")
        require(command.expected_revision is None or command.expected_revision == command.payload.get("revision"),
                "FEEDBACK_REVISION", "The feedback revision must match the payload")
        if kind == "feedback.export_github":
            return feedback.export_github(db, actor, command, settings, exported=github_export)
        return feedback.undo(db, actor, command) if kind == "feedback.undo" else feedback.update(db, actor, command)
    if kind == "session.revoke":
        from .services import person_accounts
        return person_accounts.revoke(db, actor, command, settings)
    if kind == 'department.settings':
        return access.department_settings(db, actor, command, settings)
    if kind in {'score.settings', 'score.settings.rollback'}:
        from .services import score_settings
        return score_settings.configure(db, actor, command)
    if kind in {'branding.update', 'branding.undo'}:
        from .services import branding
        return branding.change(db, actor, command)
    if kind in {'department.display', 'department.display.restore'}:
        return access.display_settings(db, actor, command)
    if kind == 'department.sessions':
        from .services import accounts
        return accounts.session_command(db, actor, command, settings)
    if kind.startswith('account.'):
        from .services import accounts
        return accounts.account_command(db, actor, command, settings)
    if kind == 'person.anonymise':
        from .services import leavers
        return leavers.anonymise(db, actor, command, settings)
    if kind.startswith("planner_view."):
        return planner_tools.view_command(db, actor, command)
    if kind.startswith("review_route."):
        return reviews.route_command(db, actor, command)
    if kind.startswith("review_delegation."):
        return reviews.delegation_command(db, actor, command)
    if kind in {"self_approval.grant", "self_approval.revoke", "self_approval.restore"}:
        from .services import independence
        return independence.permission_command(db, actor, command)
    if kind.startswith("swap."):
        return swaps.swap_command(db, actor, command)
    if kind in {"wish.create", "wish.update", "wish.restore"}:
        from .services import wishes
        return wishes.save(db, actor, command)
    if kind == "wish.withdraw":
        from .services import wishes
        return wishes.withdraw(db, actor, command)
    if kind == "availability_pattern.restore":
        return availability_patterns.restore(db, actor, command)
    if kind == "policy.restore_proposal":
        return policies.restore_proposal(db, actor, command)
    if kind == "wish.override":
        from .services import wishes
        return wishes.override(db, actor, command)
    if kind == "availability_pattern.propose":
        return availability_patterns.propose(db, actor, command)
    if kind == "availability_pattern.decide":
        return availability_patterns.decide(db, actor, command)
    if kind == "availability_pattern.undo":
        return availability_patterns.undo(db, actor, command)
    if kind == "availability_pattern.end":
        return availability_patterns.end(db, actor, command)
    if kind == "table_view.set_default":
        return table_views.set_default(db, actor, command)
    if kind in {"table_view.create", "table_view.update", "table_view.delete"}:
        return table_views.view_command(db, actor, command)
    if kind in {"calendar_feed.create", "calendar_feed.revoke", "calendar_feed.settings"}:
        from .services import calendar_feeds
        return calendar_feeds.feed_command(db, actor, command)
    if kind == "catalogue.bulk_update":
        return catalogue.bulk_catalogue(db, actor, command)
    if kind == "tag.members":
        from .services import tags
        return tags.set_members(db, actor, command)
    if kind in {"catalogue.create", "catalogue.update"}:
        return catalogue.write_catalogue(db, actor, command, update=kind.endswith("update"))
    if kind in {"membership.create", "eligibility.create", "grant.create"}:
        return catalogue.relationship(db, actor, command)
    if kind == "membership.bulk_create":
        return catalogue.bulk_membership(db, actor, command)
    if kind in {"eligibility.bulk_create", "grant.bulk_create"}:
        return catalogue.bulk_access(db, actor, command)
    if kind in {"eligibility.undo_create", "grant.undo_create"}:
        return catalogue.undo_created(db, actor, command)
    if kind == "staff_import.preview":
        return staff_import.preview(db, actor, command)
    if kind == "staff_import.apply":
        return staff_import.apply(db, actor, command)
    if kind == "staff_import.undo":
        return staff_import.undo(db, actor, command)
    if kind == "eligibility.programme":
        return catalogue.programme_eligibility(db, actor, command)
    if kind in {"membership.end", "eligibility.end", "grant.revoke"}:
        return catalogue.end_relationship(db, actor, command)
    if kind in {"membership.withdraw", "eligibility.withdraw", "programme_membership.withdraw",
                "membership.restore", "eligibility.restore", "programme_membership.restore"}:
        return catalogue.withdraw_relationship(db, actor, command)
    if kind == "eligibility.extend":
        return catalogue.extend_eligibility(db, actor, command)
    if kind == "closure.create":
        return catalogue.closure(db, actor, command)
    if kind == "workspace.create":
        return planning.create_workspace(db, actor, command, settings)
    if kind in {"workspace.baseline.refresh.preview", "workspace.baseline.refresh.apply"}:
        from . import workspace_baselines
        return workspace_baselines.refresh(db, actor, command)
    if kind == "workspace.baseline.capture":
        from . import workspace_baselines
        return workspace_baselines.capture_missing(db, actor, command)
    if kind == 'workspace.rule_adjust':
        return planning.rule_adjust(db, actor, command)
    if kind == 'workspace.target_override':
        return planning.target_override(db, actor, command)
    if kind in {"shift.create", "shift.update"}:
        return planning.save_shift(db, actor, command, update=kind.endswith("update"))
    if kind == "shift.action":
        return planning.shift_action(db, actor, command)
    if kind == "issues.resolve":
        from .services import issues
        return issues.batch(db, actor, command)
    if kind == "exception.create_many":
        from .services import issues
        return issues.exceptions(db, actor, command)
    if kind == "shift.copy":
        return planning.copy_shifts(db, actor, command)
    if kind == "draft.swap":
        return planning.swap(db, actor, command)
    if kind == 'draft.move':
        return planning.move(db, actor, command)
    if kind in {"workspace.archive", "workspace.restore", "workspace.delete_draft"}:
        return planning.archive(db, actor, command)
    if kind == "workspace.undo":
        return planning.undo(db, actor, command)
    if kind == "workspace.redo":
        return planning.redo(db, actor, command)
    if kind == "workspace.resolve_overlap":
        return planning.resolve_overlap(db, actor, command)
    if kind == "series.reconcile":
        return planning.reconcile_series(db, actor, command)
    if kind == 'series.occurrence.cancel':
        from .services.series_conflicts import cancel_occurrence
        return cancel_occurrence(db, actor, command)
    if kind == "series.expand":
        return planning.expand_series(db, actor, command)
    if kind in {"workspace.validate", "workspace.submit", "workspace.approve", "workspace.publish", "workspace.reopen"}:
        return planning.lifecycle(db, actor, command)
    if kind == "policy.create":
        return policies.create_policy(db, actor, command)
    if kind == "policy.decline":
        return policies.decline_policy(db, actor, command)
    if kind == "policy.group":
        return policies.group_rules(db, actor, command)
    if kind == "rule.migrate_to_catalogue":
        return rule_catalogue.migrate_to_catalogue(db, actor, command)
    if kind == "rule.propose":
        return rule_catalogue.propose(db, actor, command)
    if kind in {"rule.acknowledge", "rule.unacknowledge"}:
        return rule_catalogue.acknowledge(db, actor, command, kind == "rule.acknowledge")
    if kind in {"policy.activate", "policy.retire", "policy.restore"}:
        return policies.policy_transition(db, actor, command, require_checks=settings.generation_engine == 'cp_sat')
    if kind == "policy.withdraw_approval":
        return policies.withdraw_transition_approval(db, actor, command)
    if kind == "policy.activate_many":
        return policies.activate_many(db, actor, command, require_checks=settings.generation_engine == 'cp_sat')
    if kind in {"exception.create", "exception.approve", "exception.end", "exception.undo"}:
        return policies.policy_exception(db, actor, command)
    if kind == "availability.record":
        return availability.record_availability(db, actor, command)
    if kind == "availability.retract":
        return availability.retract(db, actor, command)
    if kind == 'availability.undo_create':
        return availability.undo_created(db, actor, command)
    if kind == 'timeline.rotation':
        from .services import timeline_commands
        require(command.target_id is None and command.expected_revision is None,
                'TIMELINE_COMMAND_TARGET', 'Put the source rotation and revision in the timeline payload')
        return timeline_commands.apply(db, actor, command)
    if kind.startswith(("rotation.", "trainee_note.")):
        from .services import training
        if kind in {"rotation.plan", "rotation.copy", "rotation.undo_plan", "rotation.remove", "rotation.restore", "rotation.request_change", "rotation.swap", "trainee_note.create"}:
            require(command.target_id is None and command.expected_revision is None, "RESIDENCY_COMMAND_TARGET",
                    "This operation uses the records in its payload, with no top-level target or revision")
        if kind == "rotation.plan":
            return training.plan(db, actor, command)
        if kind == "rotation.copy":
            return training.copy_forward(db, actor, command)
        if kind in {"rotation.undo_plan", "rotation.remove"}:
            return training.remove(db, actor, command, own=kind == "rotation.undo_plan")
        if kind == 'rotation.restore':
            return training.restore(db, actor, command)
        if kind == "rotation.request_change":
            return training.request_change(db, actor, command)
        if kind == "rotation.swap":
            return training.request_swap(db, actor, command)
        if kind in {"trainee_note.create", "trainee_note.update", "trainee_note.delete", "trainee_note.restore"}:
            return training.note_command(db, actor, command)
    if kind == "request.create":
        return availability.create_request(db, actor, command)
    if kind == "request.decide":
        return availability.decide_request(db, actor, command)
    if kind == "request.decide_many":
        return request_batches.decide(db, actor, command)
    if kind == "request.undo":
        value = s.RequestUndo.model_validate(command.payload)
        return request_batches.undo(db, actor, value) if value.batch_id else availability.undo_decision(db, actor, command)
    if kind == "request.link_rota":
        return availability.link_request_rota(db, actor, command)
    if kind.startswith(("identity.", "role.")):
        return access.access_command(db, actor, command, settings)
    if kind == "import.preview":
        return imports.preview_import(db, actor, command)
    if kind == 'import.reference_preview':
        return imports.preview_reference_rota(db, actor, command)
    if kind == "import.refresh":
        return imports.refresh_import(db, actor, command)
    if kind == "import.apply":
        return imports.apply_import(db, actor, command)
    if kind == "job.create":
        return jobs.create_job(db, actor, command, settings)
    if kind == "job.attach":
        return jobs.attach_fill_options(db, actor, command)
    if kind == "job.release":
        return jobs.release_fill_options(db, actor, command)
    if kind == "job.cancel":
        return jobs.cancel_job(db, actor, command)
    if kind == 'job.dismiss':
        return jobs.dismiss_job(db, actor, command)
    if kind in {'job.add_time', 'job.stop_keep_best'}:
        return jobs.control_generation(db, actor, command)
    if kind == 'job.continue':
        return jobs.continue_generation(db, actor, command, settings)
    if kind == "generation.apply":
        return jobs.apply_generation(db, actor, command)
    if kind in {"notification.ack", "notification.ack_all", "notification.unack", "notification.acknowledge"}:
        from .services import notifications
        return notifications.command(db, actor, command)
    if kind == "outbox.retry":
        s.Decision.model_validate(command.payload)
        event = scoped(db, m.Outbox, command.target_id, actor)
        expected(event, command.expected_revision)
        require(event.state == "failed", "OUTBOX_STATE", "Only a failed event can be retried", 409)
        if event.kind == 'email' and event.last_error == 'GRAPH_THROTTLED':
            from packages.domain.time import aware
            require(aware(event.next_attempt_at) <= m.now(), 'EMAIL_THROTTLED',
                    'Wait for Microsoft Retry-After before retrying this email', 429)
        event.state, event.attempts, event.next_attempt_at = "queued", 0, m.now()
        if event.kind == 'email':
            delivery = db.scalar(select(m.EmailDelivery).where(m.EmailDelivery.outbox_id == event.id))
            if delivery:
                delivery.state, delivery.error_code = 'queued', None
                delivery.revision += 1
        event.revision += 1
        return {"event": record(event, exclude=("payload",))}
    require(False, "UNKNOWN_COMMAND", "The requested command is not supported")


def authorization_command(command):
    """Keep only the non-private context needed to reauthorize a historical command."""
    keys = ("catalogue", "decision")
    if command.kind == "job.create":
        keys = (*keys, "kind")
    return {"kind": command.kind, "payload": {key: command.payload[key] for key in keys
                                             if key in command.payload}}


def authorize_command_context(actor, value, checked):
    """Resolve each complete role context once within this actor's current request."""
    key = json.dumps(value, sort_keys=True, separators=(",", ":"))
    if key not in checked:
        command = s.Command.model_validate(value)
        require_role(actor, *command_roles(command))
        checked[key] = command
    return checked[key]


def authorize_receipt(db, actor, receipt):
    """Historical outcome retrieval still requires current role and sensitive object access."""
    manifest = receipt.result.get("authorization_commands")
    checked = {}
    kind = receipt.command_kind
    if kind not in COMMAND_PERMISSIONS:
        require_role(actor, *receipt.required_roles)
    elif kind.startswith("catalogue.") or kind == "request.decide":
        # Legacy catalogue/decision receipts have no context to resolve a narrower capability.
        if manifest is None:
            require_role(actor, *receipt.required_roles)
    elif kind == "job.create":
        # Reauthorize with the command context captured at write time; old receipts use their compact job result.
        context = next((value for value in manifest or ()
                        if value.get("kind") == kind and isinstance(value.get("payload"), dict)
                        and "kind" in value["payload"]), None)
        if context is None:
            job = receipt.result.get("job")
            job_kind = job.get("kind") if isinstance(job, dict) else None
            context = {"kind": kind, "payload": {"kind": job_kind}} if job_kind is not None else None
        if context is None:
            require_role(actor, *command_roles(s.Command(kind=kind)))
        else:
            authorize_command_context(actor, context, checked)
    else:
        require_role(actor, *command_roles(s.Command(kind=kind)))
    if manifest is not None:
        for value in manifest:
            authorize_command_context(actor, value, checked)
    if kind == "bulk.apply":
        rotations, requests = [], []
        for item in receipt.result.get("items", []):
            for reply in item.get("results", []):
                value = reply.get("authorization_command")
                if value is None:
                    require(not actor.permits("programme_lead") or actor.permits("admin", "coordinator"),
                            "FORBIDDEN", "This historical change has no current programme authority context", 403)
                    continue
                command = authorize_command_context(actor, value, checked)
                catalogue_kind = command.payload.get("catalogue")
                if command.kind.startswith("catalogue.") and catalogue_kind == "rotations":
                    rotations.extend(_rotation_result_records(reply))
                elif command.kind in {"request.decide", "request.decide_many", "request.undo"}:
                    requests.append((command.kind, reply))
                else:
                    _authorize_result(db, actor, command.kind, reply, catalogue_kind=catalogue_kind)
        if rotations:
            from .services.training import rotation_records_access
            rotation_records_access(db, actor, rotations)
        _authorize_request_results(db, actor, requests)
        return
    catalogue_kind = next((value.get("payload", {}).get("catalogue") for value in manifest or []
                           if value.get("kind") == kind), None)
    if kind.startswith("catalogue.") and catalogue_kind is None:
        event = db.scalar(select(m.AuditEvent).where(m.AuditEvent.department_id == actor.department_id,
            m.AuditEvent.detail["receipt_id"].as_string() == receipt.id).limit(1))
        catalogue_kind = (event.detail or {}).get("catalogue") if event else None
    _authorize_result(db, actor, kind, receipt.result, catalogue_kind=catalogue_kind)


def _rotation_result_records(result):
    records = result.get("records", [result["record"]] if "record" in result else [])
    previous = result.get("previous_values", [result.get("previous_value")])
    return records + [{**item, **previous[index], "revision": item["revision"] - 1}
                      for index, item in enumerate(records) if index < len(previous) and previous[index]]


def _authorize_request_results(db, actor, results):
    """Recheck current request ownership and decision authority for historical outcomes."""
    ids, decision_ids, patterns = set(), set(), False
    for kind, result in results:
        if kind in {"request.decide_many", "request.undo"} and result.get("batch_id"):
            patterns |= any(row["source"] == "pattern" for row in result["records"])
            for row in result["records"]:
                if row["source"] == "request":
                    ids.add(row["id"])
                    if kind != "request.undo" and row["status"] != "withdrawn":
                        decision_ids.add(row["id"])
        elif kind in {"request.decide", "request.undo"} and not result.get("batch_id"):
            item = result.get("record") or {}
            ids.add(item.get("id"))
            if kind == "request.decide" and item.get("status") != "withdrawn":
                decision_ids.add(item.get("id"))
    if patterns:
        require_role(actor, "coordinator")
    if ids:
        require(all(ids), "TARGET_REQUIRED", "A target record is required")
        rows = {row.id: row for row in db.execute(select(
            m.RequestRecord.id, m.RequestRecord.created_by, m.RequestRecord.person_id,
            availability.decision_scope(db, actor, m.RequestRecord, include_independence=False).label("can_decide")
        ).where(m.RequestRecord.department_id == actor.department_id, m.RequestRecord.id.in_(ids)))}
        require(rows.keys() == ids, "NOT_FOUND", "Record not found", 404)
        require(all(row.can_decide or request_id not in decision_ids and availability.owns_request(actor, row)
                    for request_id, row in rows.items()),
                "FORBIDDEN", "This request is outside your current authority", 403)


def _authorize_result(db, actor, kind, result, *, catalogue_kind=None):
    """Single and nested receipts keep the command's current sensitive-object authority checks."""
    if kind in {'branding.update', 'branding.undo'}:
        from .services import branding
        branding.require_editor(db, actor)
    if kind == 'timeline.rotation':
        from .services import timeline_commands
        timeline_commands.authorize_result(db, actor, result)
    if kind.startswith('programme_membership.'):
        row = scoped(db, m.ProgrammeMembership, result['record']['id'], actor, include_withdrawn=True)
        catalogue.programme_write_access(db, actor, 'programme_memberships', row)
        catalogue.programme_write_access(db, actor, 'programme_memberships', SimpleNamespace(**result['record']))
    if kind.startswith("catalogue."):
        if catalogue_kind in PROGRAMME_CATALOGUES:
            records = result.get("records", [result["record"]] if "record" in result else [])
            previous = result.get("previous_values", [result.get("previous_value")])
            if catalogue_kind == "rotations":
                from .services.training import rotation_records_access
                rotation_records_access(db, actor, _rotation_result_records(result))
            for index, item in enumerate(records):
                if catalogue_kind == "rotations":
                    continue
                catalogue.programme_write_access(db, actor, catalogue_kind, SimpleNamespace(**item))
                if index < len(previous) and previous[index]:
                    catalogue.programme_write_access(db, actor, catalogue_kind, SimpleNamespace(**{**item, **previous[index]}))
    if kind in {"availability.record", "availability.retract", "availability.restore", "availability.undo_create"}:
        item = result["record"]
        availability.lead_protects(db, actor, item["kind"], item["person_id"], item["start"], item["end"])
    if kind in {"request.decide", "request.decide_many", "request.undo"}:
        _authorize_request_results(db, actor, [(kind, result)])
    if kind.startswith("trainee_note."):
        from .services.training import note_access
        note_access(db, actor, result["record"]["person_id"])
    if kind.startswith("rotation."):
        if kind in {"rotation.request_change", "rotation.swap"}:
            item = result["record"]
            row = scoped(db, m.RequestRecord, item["id"], actor)
            from .services.training import change_people
            subjects = change_people(db, row) if kind == "rotation.swap" else {item["person_id"]}
            if subjects != {actor.person_id} and not actor.permits("admin", "coordinator"):
                require(availability.may_decide(db, actor, row),
                        "FORBIDDEN", "This rotation is outside your current programme", 403)
        else:
            records = result.get("created", result.get("records", []))
            from .services.training import rotation_records_access
            rotation_records_access(db, actor, records, scope=(result.get("rotation_scope") or [])
                                    if kind in {"rotation.plan", "rotation.copy"} else None)
    if kind.startswith("swap."):
        source = result.get("undone") or result.get("swap", {})
        row = swaps.get_scoped(db, actor, source.get("id"))
        require(not swaps.public_offer(actor, row), "NOT_FOUND", "Swap request not found", 404)
    if kind == "session.revoke":
        from .services import person_accounts
        person_accounts.session_access(db, actor, result["session"]["id"])
    if kind.startswith(("table_view.", "planner_view.")):
        if any((result.get(key) or {}).get("scope") == "department"
               for key in ("view", "previous_view", "default", "previous_default")):
            require_role(actor, "admin", "coordinator")


def receipt_result(kind, result, *, db=None, actor=None, settings=None):
    """Return safe receipt outcomes while retaining the original stored evidence."""
    if kind == 'department.create' and settings is not None:
        from .services.departments import require_test_administrator, require_test_deployment
        require_test_deployment(settings)
        if db is not None:
            require_test_administrator(db.get(m.Identity, result['first_admin_binding']['identity_id']))
    from .services import bulk_staged
    result = (bulk_staged.receipt_reply(db, actor, result) if db is not None else
              bulk_staged.receipt_result(result))
    if db is not None:
        from .services.leave_disclosure import overlap_labels
        result = overlap_labels(db, actor, result)
    if db is not None and kind.startswith(('person.contact.', 'catalogue.', 'bulk.', 'staff_import.')):
        from .services.contacts import redact_reply
        result = redact_reply(db, actor, result)
    if isinstance(result.get('job'), dict):
        from .serialization import job_progress_record
        job = result['job']
        result = {**result, 'job': {**job, 'progress': job_progress_record(
            job.get('progress'))}}
    if kind == "feedback.export_github" and db is not None:
        from .services import feedback
        return feedback.redact_export_result(db, actor, result, settings)
    return {key: value for key, value in result.items() if key != 'path'} if kind == 'calendar_feed.create' else result


def idempotency_hash_input(command):
    """Return the canonical command fields used by command receipt hashes."""
    canonical = command.model_dump(mode="json")
    # Preserve operation hashes issued before optional impact preconditions existed.
    if command.expected_fingerprint is None:
        canonical.pop("expected_fingerprint")
    if command.preview_job_id is None:
        canonical.pop("preview_job_id")
    return canonical


def _execute(engine, settings, token, csrf, idempotency_key, command, correlation_id, *, assignment_reply=False):
    require(idempotency_key is not None and 16 <= len(idempotency_key) <= 128
            and idempotency_key.isascii() and idempotency_key.isprintable(),
            "IDEMPOTENCY_KEY_REQUIRED", "Every write requires a 16–128 character Idempotency-Key", 428)
    payload_hash = snapshot_hash(idempotency_hash_input(command))
    activation_command = command.kind in {'policy.activate', 'policy.activate_many'}
    fresh_activation = False

    def existing_receipt(db, actor):
        existing = db.scalar(select(m.CommandReceipt).where(m.CommandReceipt.actor_id == actor.identity_id,
                                                            m.CommandReceipt.key == idempotency_key))
        if existing:
            require(existing.department_id == actor.department_id and existing.payload_hash == payload_hash,
                    "IDEMPOTENCY_CONFLICT", "This operation key was already used with different content or scope", 409)
            authorize_receipt(db, actor, existing)
        return existing

    class CheckInputsChanged(Exception):
        """Release the writer before resolving changed scheduling inputs."""

    def check_context(db, actor, workspace, current_data):
        current = (actor.identity_id, actor.department_id, workspace.content_revision,
                   workspace.status, workspace.archived_at, current_data['fingerprint'])
        if current != workspace_check[0]:
            # Publication deliberately rechecks the live snapshot in the writer;
            # a changed input fingerprint must reach the review gate as
            # STALE_APPROVAL rather than being collapsed into a preflight race.
            if command.kind == 'workspace.publish' and current[:-1] == workspace_check[0][:-1]:
                return
            raise Problem(409, 'STALE_INPUTS',
                ('The rota or its scheduling inputs changed before publication; refresh before publishing again.'
                 if command.kind == 'workspace.publish' else
                 'The rota or its scheduling inputs changed during the check; the completed check is retained here.'),
                {'current_revision': workspace.revision, 'validation': workspace_check[1],
                 'recoverable': True, 'current_fingerprint': current[-1],
                 'changed_facts': input_changes.changed_facts(db, workspace_check[2], current_data,
                     actor.department_id, workspace_check[3])})

    workspace_check = None
    if command.kind in {'workspace.validate', 'workspace.submit', 'workspace.approve', 'workspace.publish'}:
        # Freeze a coherent read snapshot without taking the application writer lock.
        reader = (engine.execution_options(isolation_level='REPEATABLE READ', postgresql_readonly=True)
                  if engine.dialect.name == 'postgresql' else engine)
        with transaction(reader) as db:
            if engine.dialect.name == 'sqlite':
                db.execute(text('BEGIN'))
            require_current_schema(db)
            actor = authenticate(db, token, csrf, write=True, settings=settings)
            require_role(actor, *command_roles(command))
            if not existing_receipt(db, actor):
                planner_tools.assert_precondition(db, actor, command)
                workspace = (planning.validation_workspace(db, actor, command) if command.kind == 'workspace.validate'
                             else scoped(db, m.Workspace, command.target_id, actor))
                expected(workspace, command.expected_revision)
                if command.kind != 'workspace.validate':
                    state = {'workspace.submit': 'draft', 'workspace.approve': 'submitted', 'workspace.publish': 'approved'}[command.kind]
                    require(workspace.status == state, 'WORKFLOW_STATE', 'The rota is not ready for this action', 409)
                captured = []
                if command.kind == 'workspace.publish':
                    # This read only detects drift before the serialized writer.
                    # Publication always evaluates fresh inputs inside that writer.
                    captured.append(planning.domain_snapshot(db, workspace))
                    checked = workspace.validation or {}
                else:
                    checked = planning.revalidate(db, workspace, store=False, data_out=captured,
                        reuse=command.kind != 'workspace.validate' or command.payload.get('automatic') is True, include_internal=True)
                context = (actor.identity_id, actor.department_id, workspace.content_revision,
                           workspace.status, workspace.archived_at, captured[0]['fingerprint'])
                workspace_check = (context, checked, captured[0], input_changes.watermark(db, actor.department_id),
                                   planning.validation_stamp(db, workspace))

    generation_plan = None
    if command.kind == 'generation.apply':
        reader = (engine.execution_options(isolation_level='REPEATABLE READ', postgresql_readonly=True)
                  if engine.dialect.name == 'postgresql' else engine)
        with transaction(reader) as db:
            if engine.dialect.name == 'sqlite':
                db.execute(text('BEGIN'))
            require_current_schema(db)
            actor = authenticate(db, token, csrf, write=True, settings=settings)
            require_role(actor, *command_roles(command))
            if not existing_receipt(db, actor):
                from .services.generation_reconciliation import prepare
                generation_plan = prepare(db, actor, command)

    activation_preparation = None
    if command.kind in {'policy.activate', 'policy.activate_many'}:
        # Admission evidence and impact are read-only work.  Prepare them from a
        # repeatable snapshot before retry_write acquires the serialized writer;
        # the writer verifies the compact stamp before making lifecycle changes.
        reader = (engine.execution_options(isolation_level='REPEATABLE READ', postgresql_readonly=True)
                  if engine.dialect.name == 'postgresql' else engine)
        with transaction(reader) as db:
            if engine.dialect.name == 'sqlite':
                db.execute(text('BEGIN'))
            require_current_schema(db)
            actor = authenticate(db, token, csrf, write=True, settings=settings)
            require_role(actor, 'approver')
            if not existing_receipt(db, actor):
                activation_preparation = policies.prepare_activation(
                    db, actor, command, require_checks=settings.generation_engine == 'cp_sat')

    github_check = None
    github_export = None
    external_export = command.kind == "feedback.export_github" and command.payload.get("create_issue") is True
    if external_export:
        from .services import feedback

        def reserve_export(db):
            require_current_schema(db)
            actor = authenticate(db, token, csrf, write=True, settings=settings)
            require_permission(actor, "feedback.export_github")
            if existing_receipt(db, actor):
                return None
            require(command.target_id is None or command.target_id == command.payload.get("id"),
                    "FEEDBACK_TARGET", "The feedback target must match the payload")
            require(command.expected_revision is None or command.expected_revision == command.payload.get("revision"),
                    "FEEDBACK_REVISION", "The feedback revision must match the payload")
            planner_tools.assert_precondition(db, actor, command)
            return feedback.prepare_github_export(db, actor, command, settings)

        prepared = retry_write(engine, reserve_export,
                               context={"correlation_id": correlation_id, "command_kind": command.kind})
        if prepared is not None:
            try:
                # The reservation is committed and its transaction/lock is closed before this external call.
                github_export = feedback.send_github_export(prepared)
            except Problem as error:
                if prepared.create and error.code in {"FEEDBACK_GITHUB_FORBIDDEN", "FEEDBACK_GITHUB_REJECTED"}:
                    def release_rejected_export(db):
                        row = db.get(m.FeedbackReport, prepared.report_id)
                        if row and row.github_export_repo == prepared.repo and row.github_issue_url is None:
                            row.github_export_repo = None
                    retry_write(engine, release_rejected_export)
                raise
    if command.kind in {"integration.github.connect", "integration.github.test"}:
        # Count rejected remote checks too, outside the command transaction. Receipt replays call no upstream.
        from .rate_limit import enforce
        with transaction(engine) as db:
            require_current_schema(db)
            actor = authenticate(db, token, csrf, write=True, settings=settings)
            require_permission(actor, "integrations.github")
            replay = existing_receipt(db, actor)
            department_id = actor.department_id
        if not replay:
            enforce(engine, "integration-github-check", [department_id], 10, 3600,
                    "Too many GitHub connection checks for this department; try again later")
            from .services import github_integrations
            with transaction(engine) as db:
                require_current_schema(db)
                current_actor = authenticate(db, token, csrf, write=True, settings=settings)
                require((current_actor.identity_id, current_actor.department_id) == (actor.identity_id, department_id),
                        "INTEGRATION_CONTEXT_CHANGED", "The signed-in department changed; check the connection again", 409)
                replay = existing_receipt(db, current_actor)
                if not replay:
                    planner_tools.assert_precondition(db, current_actor, command)
                    context, repo, credential_token = github_integrations.prepare_check(db, current_actor, command, settings)
            if not replay:
                # The read transaction is closed and no application writer lock is held during this remote GET.
                ok, expiry = github_integrations.check_repository(repo, credential_token)
                github_check = github_integrations.RepositoryCheck(**context, ok=ok, expiry=expiry)

    from .services import planning_preparation
    planning_plan = None
    background_merge = command.kind in {'workspace.merge.preview', 'workspace.merge.apply'} and command.payload.get('background', True)
    # Batch checks and overlap projections share one final preparation calculation.
    # Publication performs its fresh safety validation at the final writer boundary.
    staged = (not assignment_reply and not background_merge and command.kind != 'workspace.publish'
              and (command.kind in planning_preparation.KINDS or command.kind == 'workspace.approve'
              and (command.payload.get('rejected_review_item_ids') or
                   any(item.get('accept') is False for item in command.payload.get('decisions', []) if isinstance(item, dict)))))
    if staged:
        reader = engine.execution_options(isolation_level='REPEATABLE READ') if engine.dialect.name == 'postgresql' else engine
        planning_plan = {'seed': m.uid(), 'at': m.now(), 'checks': [], 'kind': command.kind}
        from . import db as database
        preparation_settings = reader.get_execution_options().get('rota_write_settings') or settings
        preparation_deadline = database.time.monotonic() + preparation_settings.command_write_budget_ms / 1000
        while True:
            try:
                with transaction(reader, deadline=preparation_deadline) as db:
                    if engine.dialect.name == 'sqlite':
                        # Preparation flushes (then rolls back) writes. A deferred SQLite read cannot upgrade while another
                        # writer holds the lock (instant "database is locked" → 503), so wait for that writer within the
                        # command budget, as PostgreSQL's REPEATABLE READ preparation does.
                        db.execute(text(f'PRAGMA busy_timeout={database._remaining(preparation_deadline)}'))
                        try:
                            db.execute(text('BEGIN IMMEDIATE'))
                        finally:
                            db.execute(text(f'PRAGMA busy_timeout={preparation_settings.command_lock_timeout_ms}'))
                    require_current_schema(db)
                    actor = authenticate(db, token, csrf, write=True, settings=settings)
                    require_role(actor, *command_roles(command))
                    if not existing_receipt(db, actor):
                        planner_tools.assert_precondition(db, actor, command)
                        planning_preparation.bind(db, planning_plan)
                        if command.kind == 'workspace.approve':
                            planning.lifecycle(db, actor, command, validation=(workspace_check[1], workspace_check[2]))
                        else:
                            dispatch(db, actor, command, settings)
                    # Never commit preparation writes, even when a handler returns without a check.
                    raise planning_preparation.RollbackPreparation()
            except planning_preparation.ComputePreparation as pending:
                # Rollback released row locks too: a check of this same rota cannot convoy other writers.
                database._remaining(preparation_deadline)
                planning_plan['checks'].append((pending.key, pending.compute()))
                database._remaining(preparation_deadline)
                if pending.final:
                    break
            except planning_preparation.RollbackPreparation:
                break
            except OperationalError as error:
                if getattr(error.orig, 'sqlstate', None) in {'40001', '40P01'}:
                    # A stored background check also updates the workspace row. Reacquire all
                    # evidence; the original command's revision and fingerprint still gate every pass.
                    database._remaining(preparation_deadline)
                    planning_plan = {key: planning_plan[key] for key in ('seed', 'at', 'kind')}
                    planning_plan['checks'] = []
                    continue
                raise

    changed_department = []

    def operation(db):
        nonlocal fresh_activation
        writer_started = time.monotonic() if activation_command else None
        require_current_schema(db)
        changed_department.clear()
        db.info.pop("self_approved", None)  # a retried attempt starts clean
        db.info.pop("one_time_link", None)
        db.info["correlation_id"] = correlation_id
        db.info['_assignment_reply'] = assignment_reply
        if planning_plan is not None:
            planning_preparation.bind(db, planning_plan, replay=True)
        db.info['_track_input_changes'] = True
        if generation_plan is not None:
            db.info['_generation_plan'] = generation_plan
        if activation_preparation is not None:
            db.info['_activation_preparation'] = activation_preparation
            db.info['_activation_timing'] = True
            db.info['_activation_phase_timings'] = dict(activation_preparation.get('phase_timings') or {})
        actor = authenticate(db, token, csrf, write=True, settings=settings)
        if github_export is not None:
            require((actor.identity_id, actor.department_id) == (github_export.identity_id, github_export.department_id),
                    "INTEGRATION_CONTEXT_CHANGED", "The signed-in department changed; export again", 409)
        allowed = command_roles(command)
        require_role(actor, *allowed)
        if command.kind.startswith(('score_import.', 'score_check.', 'ai_provider.')):
            from .services.score_check import require_enabled
            require_enabled(db, actor.department_id)
        existing = existing_receipt(db, actor)
        if existing:
            return receipt_result(existing.command_kind, existing.result, db=db, actor=actor, settings=settings)
        if activation_command:
            db.info['_activation_timing'] = True
            policies.activation_phase_timings(db)
        if command.kind != "bulk.apply":
            planner_tools.assert_precondition(db, actor, command)
        receipt_id = m.uid()
        db.info["receipt_id"] = receipt_id
        if workspace_check is not None:
            require_workspace_command(db, actor, command)
            workspace = scoped(db, m.Workspace, command.target_id, actor)
            publish_inputs = (planning.snapshots.snapshot_inputs(db, db.get(m.Organisation, actor.organisation_id))
                              if command.kind == 'workspace.publish' else None)
            if command.kind == 'workspace.publish':
                current_data = planning.domain_snapshot(db, workspace, shared_inputs=publish_inputs)
            else:
                if planning.validation_stamp(db, workspace) != workspace_check[4]:
                    raise CheckInputsChanged()
                # This request acquired canonical inputs under REPEATABLE READ. Every input
                # revision is still identical under the writer; never rebuild history here.
                expected(workspace, command.expected_revision)
                current_data = workspace_check[2]
            check_context(db, actor, workspace, current_data)
            reply = planning.lifecycle(db, actor, command, validation=(workspace_check[1] if command.kind == 'workspace.validate'
                else (workspace_check[1], current_data if command.kind == 'workspace.publish' else workspace_check[2])),
                shared_inputs=publish_inputs)
        else:
            reply = dispatch(db, actor, command, settings, github_check=github_check, github_export=github_export)
        if planning_plan is not None:
            require(db.info['_planning_preparation']['next_check'] == len(planning_plan['checks']), 'STALE_INPUTS',
                    'The prepared edit changed; refresh before applying it again', 409, {'recoverable': True})
        # The reply is no longer mutated below its root; retain ready branches instead of copying them again.
        result = {**json_value(compact_reply(reply), copy_containers=False)}
        from .services.workspace_access import audit_envelope, private_operation
        private_operation(db, command, result)
        if command.kind.startswith("catalogue.") or command.kind == "request.decide" or command.kind == "job.create":
            result["authorization_commands"] = [authorization_command(command)]
        authorization_kinds = [value["kind"] for value in result.get("authorization_commands", [])]
        if any(kind.startswith(('identity.', 'role.')) or kind == 'person.anonymise'
               for kind in [command.kind, *authorization_kinds]):
            from .services import mailboxes
            db.flush()
            mailboxes.prune(db)
        conditional_cancel_missed = command.kind == 'job.cancel' and result.get('cancelled') is False
        changes = (None if conditional_cancel_missed else
                   live.command_changes(command.kind, command.payload, result, target_id=command.target_id))
        # Unknown command kinds are deliberately silent: the periodic pulse remains the last-resort
        # recovery path, while no writer emits an unscoped invalidation.
        for change in changes or ():
            live.signal(db, actor.department_id, **change)
        changed_department.append((actor.department_id, changes))
        result["receipt_id"] = receipt_id
        result["activity"] = live.activity(db, actor.department_id)  # the pulse value this change leads to
        if activation_command:
            timings = dict(policies.activation_phase_timings(db))
            timings['serialized_writer'] = time.monotonic() - writer_started
            result['phase_timings'] = timings
            fresh_activation = True
        # A calendar URL is a bearer credential shown once, like invitation/reset links. Keep the committed
        # outcome for retries without storing its secret in receipt evidence or returning it on recovery.
        db.add(m.CommandReceipt(id=receipt_id, department_id=actor.department_id, actor_id=actor.identity_id,
                                command_kind=command.kind, required_roles=allowed, key=idempotency_key,
                                payload_hash=payload_hash, result=receipt_result(command.kind, result)))
        override_blockers = db.info.pop('_assignment_override_blockers', [])
        reason = str(command.payload.get("reason", command.kind))[:500]
        if command.kind == 'request.create' and command.payload.get('kind') == 'absence_report':
            reason = 'Absence reported'
        if command.kind in {'workspace.approve', 'policy.activate', 'policy.activate_many'}:
            approval_schema = (s.ReviewDecisionInput if command.kind == 'workspace.approve' else
                               s.PolicyManyActivation if command.kind == 'policy.activate_many' else s.PolicyActivationDecision)
            reason = approval_schema.model_validate(command.payload).reason
        request_decisions = {'request.decide': s.RequestDecision, 'request.decide_many': s.RequestManyDecision,
                             'availability_pattern.decide': s.PatternDecision}
        if command.kind in request_decisions:
            decision = request_decisions[command.kind].model_validate(command.payload)
            reason = decision.reason
            if (command.kind == 'request.decide' and decision.decision == 'withdraw'
                    and result.get('record', {}).get('kind') == 'absence_report'):
                reason = 'Absence withdrawn'
        if command.kind in {'shift.create', 'shift.update'} and command.payload.get('override_reason'):
            reason = command.payload['override_reason'].strip()
        if command.kind == 'rotation.swap' or command.kind == 'timeline.rotation' and result.get('status') == 'submitted':
            reason = 'Rotation change requested'
        # A configuration create names the record it made, and every configuration change its list (an enum, never
        # private text). Other creates keep no target: their histories add "created" themselves (requests, swaps).
        fact = result.get("fact") if isinstance(result.get("fact"), dict) else {}
        made = (result.get("record") or {}) if command.kind.startswith(("catalogue.", "date_set.", "rule.catalogue.", "rule.migrate_to_catalogue")) else fact
        if command.kind == "catalogue.bulk_update" and command.payload.get("catalogue") == "programmes" and result.get("removed_ids"):
            made = {"id": result["removed_ids"][0]}
        if command.kind == 'rule.catalogue.check':
            made = result.get('job') or {}
        if command.kind.startswith('feedback.'):
            made = result.get('report') or {"id": result.get("id") or command.payload.get("id")}
        if command.kind.startswith('ai_provider.'):
            made = result.get('profile') or {}
        if command.kind.startswith(('score_import.', 'score_check.')):
            made = {'id': result.get('id') or result.get('import_id') or (result.get('batch') or {}).get('id')}
        note = result.get("record", {}) if command.kind.startswith("trainee_note.") else {}
        if note:
            made = note
        if command.kind.startswith(("table_view.", "planner_view.")):
            made = result.get("view") or result.get("default") or {}
        if command.kind.startswith('planning_group.'):
            made = {'id': (result.get('group') or {}).get('group_id')}
        if command.kind == 'department.create':
            made = result['department']
        if command.kind.startswith('planning.settings'):
            made = {'id': actor.department_id}
        listed = command.payload.get("catalogue")
        from .services.audit_log import event_action, subject_detail
        db.flush()
        automatic_check = command.payload.get('automatic') is True and (command.kind == 'workspace.validate'
            or command.kind == 'job.create' and command.payload.get('kind') == 'validation')
        if not automatic_check and not conditional_cancel_missed:
            db.add(audit_envelope(db, command, result, m.AuditEvent(department_id=actor.department_id, actor_id=actor.identity_id,
                                action=event_action(command.kind, result), target_id=(made.get("id") if command.kind == 'date_set.copy_next_year' or command.kind.startswith(('planning_group.', 'planning.settings'))
                                                              else command.target_id or made.get("id")),
                                reason=reason, detail={**subject_detail(db, command.kind, result, department_id=actor.department_id),
                                                      "receipt_id": receipt_id, 'input_changes': list(db.info.get('_input_changes', {}).values()), "payload_fields": sorted(
                                    key for key in command.payload if key != "token" or command.kind != "integration.github.connect"),
                                                       **({'absence_category': command.payload['category']}
                                                          if command.kind == 'request.create'
                                                          and command.payload.get('kind') == 'absence_report'
                                                          and command.payload.get('category') is not None else {}),
                                                       **({"person_id": note["person_id"]} if note else {}),
                                                       **({'person_id': result['record']['person_id'],
                                                           'contact_type_id': result['record']['contact_type_id'],
                                                           'contact_id': result['record']['id']}
                                                          if command.kind == 'person.contact.set' else {}),
                                                       **({'baseline_refresh': {
                                                           'decisions': result.get('decisions', {}),
                                                           'conflicts': result.get('conflicts', []),
                                                           'snapshot_hash': result.get('snapshot_hash') or (result.get('baseline') or {}).get('snapshot_hash')
                                                       }} if command.kind.startswith('workspace.baseline.refresh.') else {}),
                                                       **({'context_check_evidence': result['context_check_evidence']}
                                                          if result.get('context_check_evidence') else {}),
                                                       **({"batch_id": result["batch_id"], "count": result["count"]}
                                                          if command.kind in {"request.decide_many", "request.undo"} and "batch_id" in result else {}),
                                                       **({"batch_id": receipt_id, "count": result["count"],
                                                           "item_kinds": result.get("item_kinds", [command.payload.get("item_kind")])}
                                                          if command.kind == "bulk.apply" else {}),
                                                       **({"person_id": fact.get("person_id"), "fact_revision": fact["revision"],
                                                           "replaces_id": fact.get("replaces_id"), "subject_kind": fact["kind"]} if fact else {}),
                                                       **({"catalogue": listed} if isinstance(listed, str) else {}),
                                                       **({"source_date_set_id": command.target_id}
                                                          if command.kind == 'date_set.copy_next_year' else {}),
                                                       **({"self_approved": used} if (used := db.info.pop("self_approved", None)) else {}),
                                                       **({'assignment_override_blockers': override_blockers}
                                                          if override_blockers else {})},
                                correlation_id=correlation_id)))
        db.flush()
        # A one-time link (invitation, password reset) goes back to the admin once and is never stored: the receipt
        # above was written without it, so a retried request with the same key gets the receipt, not the link.
        link = db.info.pop("one_time_link", None)
        authorization_url = db.info.pop('email_authorization_url', None)
        if authorization_url:
            result = {**result, 'authorization_url': authorization_url}
        from .services.leave_disclosure import overlap_labels
        result = overlap_labels(db, actor, result)
        if command.kind.startswith(('person.contact.', 'catalogue.', 'bulk.', 'staff_import.')):
            from .services.contacts import redact_reply
            result = redact_reply(db, actor, result)
        return {**result, "one_time_link": link} if link else result
    # An uncertain final commit is recovered using the durable reservation and GitHub marker on the next request.
    try:
        for attempt in range(3):
            try:
                result = retry_write(engine, operation, attempts=1 if external_export or command.kind.startswith("integration.github.") else 3,
                                     context={"correlation_id": correlation_id, "command_kind": logged_command_kind(command.kind)},
                                     serialize=command.kind not in NON_SERIALIZED_COMMANDS)
                break
            except CheckInputsChanged:
                # An unrelated revision may leave the canonical fingerprint unchanged. Resolve
                # that (or build the stale-input explanation) without holding up user writes.
                with transaction(reader) as db:
                    if engine.dialect.name == 'sqlite':
                        db.execute(text('BEGIN'))
                    actor = authenticate(db, token, csrf, write=True, settings=settings)
                    require_role(actor, *command_roles(command))
                    if existing := existing_receipt(db, actor):
                        return receipt_result(existing.command_kind, existing.result, db=db, actor=actor, settings=settings)
                    require_workspace_command(db, actor, command)
                    workspace = scoped(db, m.Workspace, command.target_id, actor)
                    current_data = planning.domain_snapshot(db, workspace)
                    check_context(db, actor, workspace, current_data)
                    require(attempt < 2, 'STALE_INPUTS', 'Scheduling inputs are changing; check the rota again', 409,
                            {'recoverable': True, 'validation': workspace_check[1]})
                    if command.kind == 'workspace.validate':
                        command = command.model_copy(update={'expected_revision': workspace.revision})
                    workspace_check = (*workspace_check[:2], current_data, workspace_check[3],
                                       planning.validation_stamp(db, workspace))
    except IntegrityError:
        # Two identical requests can both pass the pre-write receipt read.  The
        # unique (actor, key) constraint makes the loser roll back; recover the
        # winner's committed reply instead of surfacing a false save failure.
        with transaction(engine) as db:
            require_current_schema(db)
            actor = authenticate(db, token, csrf, write=True, settings=settings)
            require_role(actor, *command_roles(command))
            if existing := existing_receipt(db, actor):
                return receipt_result(existing.command_kind, existing.result, db=db, actor=actor, settings=settings)
        raise
    except (OperationalError, DatabaseWriteTimeout):
        if external_export:
            from .services.feedback import GITHUB_UNCERTAIN_MESSAGE
            raise Problem(502, "FEEDBACK_GITHUB_UNCERTAIN", GITHUB_UNCERTAIN_MESSAGE) from None
        raise
    if activation_command and fresh_activation and isinstance(result.get('phase_timings'), dict):
        log.info('policy_activation_phases', extra={
            'command_kind': command.kind,
            'phase_timings': result['phase_timings'],
            'correlation_id': correlation_id,
        })
    return result


def execute(engine, settings, token, csrf, idempotency_key, command, correlation_id):
    actor = None
    try:
        # Refuse obvious forbidden roles before queuing for the department writer.
        with transaction(engine) as db:
            require_current_schema(db)
            actor = authenticate(db, token, csrf, write=True, settings=settings)
            require_role(actor, *command_roles(command))
            if command.kind == 'department.create':
                from .services.departments import require_test_deployment
                require_test_deployment(settings)
            assignment_reply = planning.assignment_reply_available(db, command)
            automatic_check = command.kind == 'workspace.validate' and command.payload.get('automatic') is True
            if automatic_check:
                planning.validation_workspace(db, actor, command)
        if automatic_check and engine.dialect.name == 'postgresql':
            # The workerless fallback shares CPU work across API processes too. Hold only this rota's lock;
            # the next reader starts its fresh snapshot after the first result has committed.
            key = int(snapshot_hash(['automatic-validation', actor.department_id, command.target_id])[:15], 16)
            try:
                with transaction(engine) as guard:
                    guard.execute(text("SELECT set_config('lock_timeout', :wait, true)"),
                                  {'wait': f'{settings.command_write_budget_ms}ms'})
                    guard.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})
                    return _execute(engine, settings, token, csrf, idempotency_key, command, correlation_id,
                                    assignment_reply=assignment_reply)
            except OperationalError as error:
                if getattr(error.orig, 'sqlstate', None) == '55P03':
                    raise Problem(409, 'CHECK_IN_PROGRESS', 'This rota is already being checked; its result will refresh here') from error
                raise
        return _execute(engine, settings, token, csrf, idempotency_key, command, correlation_id, assignment_reply=assignment_reply)
    except Problem as error:
        if error.code == 'STALE_REVISION' and command.kind.startswith(('shift.', 'workspace.', 'generation.')):
            submitted = command.model_dump(mode='json')
            if command.expected_fingerprint is None:
                submitted.pop('expected_fingerprint')
            error.details = {**(error.details or {}), 'recoverable': True, 'submitted_edit': submitted}
            # Draft editors can keep their optimistic copy only when the
            # command replaced exactly the revision they displayed.  Make the
            # concurrency outcome explicit so the client performs a full read.
            if (command.kind.startswith(('shift.', 'workspace.'))
                    or command.kind in {'draft.swap', 'draft.move', 'issues.resolve', 'bulk.apply'}):
                error.details['stale'] = True
        if actor is not None and (error.status == 403 or error.code in {
                'SAFETY_RULE', 'SAFETY_EXCEPTION', 'ACCEPT_ADVISORY_ONLY', 'PUBLISH_TEST_PEOPLE_ONLY'}):
            from .services.command_refusals import record_refusal
            record_refusal(engine, actor, command, error, idempotency_key, correlation_id)
        raise
