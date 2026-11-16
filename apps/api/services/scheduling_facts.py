"""Versioned personal workload, overtime offers and calendar protection decisions."""
from datetime import date
from types import SimpleNamespace
from sqlalchemy import false, or_, select
from packages.contracts import schemas as s
from packages.domain.conditions import active_rules, check_definition_count_budget, with_tags, scope_applies
from packages.domain.rule_lines import counting_attention, expand, fact_policy, fair_share_goal_settings
from packages.domain.rule_serialization import stored_definition
from packages.domain.solver_rules import definition_issues
from apps.api import models as m
from apps.api.auth import require_role
from apps.api.errors import require
from apps.api.serialization import json_value, record
from apps.api.snapshots import scheduling_inputs
from .common import scoped, expected, bump_inputs, history, history_version
from .date_sets import check_policy_references
from .person_record import require_person
from . import independence

READERS = s.PERMISSIONS["fairness.read_team"]
FAMILIES = {"person.workload": {"workload", "target"}, "person.overtime": {"overtime_offer"},
            "calendar.protection": {"protected_time"}}
FACT_LABELS = {"workload": "Personal workload", "target": "Personal target", "overtime_offer": "Overtime offer",
               "protected_time": "Calendar protection"}
FACT_STAGES = {"proposed": "waiting", "active": "approved", "declined": "declined", "retired": "withdrawn"}


def scoped_query(db, actor):
    """Programme visibility is independent of a fact's workflow state."""
    F = m.SchedulingFact
    query = select(F).where(F.department_id == actor.department_id)
    if not actor.permits(*READERS):
        return query.where(false())
    if actor.permits("programme_lead") and not actor.permits("admin", "coordinator", "approver", "auditor"):
        from .training import lead_programmes
        M, day = m.ProgrammeMembership, independence.today(db, actor.department_id)
        programmes = lead_programmes(db, actor)
        people = select(M.person_id).where(M.department_id == actor.department_id,
            M.programme_id.in_(programmes), M.starts_on <= day, M.ends_on > day)
        entries = select(m.ProgrammeCalendarEntry.id).where(m.ProgrammeCalendarEntry.department_id == actor.department_id,
            m.ProgrammeCalendarEntry.programme_id.in_(programmes))
        query = query.where(or_(F.person_id.is_(None), F.person_id.in_(people)),
                            or_(F.calendar_entry_id.is_(None), F.calendar_entry_id.in_(entries)))
    return query


def pending_query(db, actor, *, decidable=False):
    """One scoped source for Requests, its decision count and Home's Needs you."""
    F = m.SchedulingFact
    query = scoped_query(db, actor).where(F.status == "proposed")
    if decidable and not actor.permits("approver"):
        return query.where(false())
    if decidable:
        query = query.where(F.migration_proposal_id.is_(None))
        if independence.permission_for(db, actor, "rule.activate") is None:
            own = select(m.Identity.id).where(m.Identity.person_id == actor.person_id) if actor.person_id else [actor.identity_id]
            query = query.where(F.proposed_by != actor.identity_id, F.proposed_by.not_in(own))
    return query


def approval_item(db, actor, row, names):
    """Plain proposal metadata only; the detail read supplies the definition and audit history."""
    from pydantic import ValidationError
    from packages.contracts.rule_catalogue import workload_fields
    line = row.data["line"]
    # Historical/imported lines can retain writer metadata; RuleLine fields remain the projection SSOT.
    stored_fields = {key: value for key, value in line.items() if key in s.RuleLine.model_fields}
    resolved_fields = workload_fields({"id": row.data["catalogue_id"],
                                       "catalogue_version": row.data.get("catalogue_version", 2)}, line)
    rule_line = s.RuleLine.model_validate({**resolved_fields, **stored_fields})
    title = FACT_LABELS[row.kind]
    row_title = title if row.on else "Stop " + title.lower()
    goal_measure = goal_period = None
    if row.kind == "target":
        settings = line.get("settings", {}).get("balance_goal", {})
        if row.data.get("goal_ref"):
            from packages.domain.rule_lines import bound_policies
            if "scheduling_fact_approval_goals" not in db.info:
                policies = [record(p) for p in db.scalars(select(m.Policy).where(m.Policy.department_id == actor.department_id))]
                db.info["scheduling_fact_approval_goals"] = {p["id"]: p for p in bound_policies(
                    policies, scheduling_inputs(db, [actor.department_id]))}
            reference = row.data["goal_ref"]
            goal = db.info["scheduling_fact_approval_goals"].get(reference["policy_id"])
            rule = goal["definition"]["rule"] if goal else {}
            source = next((r for r in rule.get("lines", []) if r.get("_source_identity", r["id"]) ==
                           reference.get("line_id")), None)
            if rule.get("kind") == "balance_goal":
                settings = fair_share_goal_settings(rule) or settings
            elif source:
                settings = fair_share_goal_settings(rule, source) or settings
        if settings.get("metric"):
            goal_values = {**settings, "kind": "balance_goal"}
            target_value = (line.get("numbers", {}).get("target") or {}).get("value")
            if goal_values.get("amount") is None and not goal_values.get("person_amounts") and target_value is not None:
                # amount filled only to satisfy validation; not used for scheduling
                goal_values["amount"] = target_value
            resolved_goal = s.BalanceGoal.model_validate(s.BalanceGoal.measure_defaults(
                goal_values))
            goal_measure = resolved_goal.metric
            goal_period = s.BalanceGoalPeriod(kind=resolved_goal.period, days=resolved_goal.period_days,
                                               months=resolved_goal.period_months, starts_on=resolved_goal.starts_on,
                                               ends_on=resolved_goal.ends_on)
            if rule_line.measure is None:
                try:
                    rule_line.measure = s.TypeAdapter(s.WorkloadMeasure).validate_python(resolved_goal.metric)
                except ValidationError:
                    pass
            if rule_line.period is None:
                try:
                    rule_line.period = s.WorkloadPeriod.model_validate({
                        "kind": resolved_goal.period, "days": resolved_goal.period_days,
                        "starts_on": resolved_goal.starts_on, "ends_on": resolved_goal.ends_on})
                except ValidationError:
                    pass

    starts_on, ends_on, weekdays = row.starts_on, row.ends_on, rule_line.weekdays
    entry = db.get(m.ProgrammeCalendarEntry, row.calendar_entry_id) if row.calendar_entry_id else None
    proposer = db.get(m.Identity, row.proposed_by)
    can_decide = row.status == "proposed" and actor.permits("approver") and row.migration_proposal_id is None and (
        independence.person_key(db, row.proposed_by) != independence.person_key(db, actor.identity_id)
        or independence.permission_for(db, actor, "rule.activate") is not None)
    return s.SchedulingFactApproval(id=row.id, revision=row.revision, kind=row.kind,
        title=row_title, catalogue_id=row.data["catalogue_id"],
        status=row.status, stage=FACT_STAGES[row.status],
        person_id=row.person_id, person_name=names(row.person_id) if row.person_id else None,
        calendar_entry_id=row.calendar_entry_id, calendar_entry_name=entry.name if entry else None,
        proposed_by=row.proposed_by, proposer_name=proposer.display_name, proposed_at=json_value(row.created_at),
        starts_on=starts_on, ends_on=ends_on, start_time=rule_line.start_time, end_time=rule_line.end_time,
        weekdays=weekdays, numbers=rule_line.numbers, measure=rule_line.measure, period=rule_line.period,
        goal_measure=goal_measure, goal_period=goal_period,
        link_target="#requests?open=" + row.id, can_decide=can_decide).model_dump(mode="json")


def _family(command):
    family, action = command.kind.rsplit(".", 1)
    require(family in FAMILIES, "UNKNOWN_COMMAND", "Unknown scheduling fact command")
    return FAMILIES[family], action


def _references(db, actor, value, *, exception_allowed=False):
    from .policies import effective_source, policy_references, people_data
    if value.person_id:
        require_person(db, actor, value.person_id, READERS)
    programme_id = None
    if value.calendar_entry_id:
        entry = scoped(db, m.ProgrammeCalendarEntry, value.calendar_entry_id, actor)
        require(entry.kind == "academic_block", "CALENDAR_PROTECTION_KIND", "Only an academic block can hold calendar protection")
        programme_id = entry.programme_id
        require(value.line.starts_on is not None and value.line.ends_on is not None
                and entry.starts_on <= value.line.starts_on < value.line.ends_on <= entry.ends_on,
                "CALENDAR_PROTECTION_WINDOW", "Calendar protection needs explicit dates within its academic block")
    require(value.line.calendar_entry_id in (None, value.calendar_entry_id), "CALENDAR_PROTECTION_LINK",
            "The fact and its line must name the same calendar entry")
    if value.goal_ref:
        goal = scoped(db, m.Policy, value.goal_ref.policy_id, actor)
        source = effective_source(db, actor, goal)
        require(source["status"] == "active" and (value.line.source_bindings
                or source["revision"] == value.goal_ref.revision),
                "STALE_GOAL", "The personal target no longer matches its approved goal", 409)
        rule = source["definition"]["rule"]
        require(rule["kind"] == "balance_goal" or rule["kind"] == "catalogue" and rule["id"] == "fair_share",
                "TARGET_GOAL_KIND", "Personal fairness targets need a Fair-share goal")
        if value.goal_ref.line_id:
            require(any(r.get("_source_identity", r["id"]) == value.goal_ref.line_id for r in rule.get("lines", [])),
                    "TARGET_GOAL_LINE", "The personal target source line does not exist")
        if not value.line.source_bindings:
            body = source["definition"]["rule"]
            level = source["definition"].get("strength") or "medium"
            if body["kind"] == "catalogue":
                matches = [r for r in body["lines"] if r.get("_source_identity", r["id"]) == value.goal_ref.line_id]
                require(len(matches) == 1, "TARGET_GOAL_LINE", "The personal target needs one exact goal line")
                [line] = matches
                number = line["numbers"].get("target") or line["numbers"].get("apply") or {}
                level = number.get("level", "medium")
                body = {**body.get("settings", {}).get("balance_goal", {}),
                        **line.get("settings", {}).get("balance_goal", {}), "kind": "balance_goal"}
                if "amount" not in body and number.get("value") is not None:
                    body["amount"] = number["value"]
            settings = s.BalanceGoal.model_validate(body).model_dump(mode="json")
            require(settings["aim"] == "target", "TARGET_GOAL_AIM", "Personal amounts belong to an amount-target Fair-share goal")
            require(set(value.line.numbers) == {"target"} and value.line.numbers["target"].level in (None, level),
                    "TARGET_GOAL_STRENGTH", "Personal amounts inherit their source goal strength")
            require(not value.line.who and not value.line.where and set(value.line.weekdays) == set(range(7))
                    and value.line.start_time == "00:00" and value.line.end_time == "24:00"
                    and value.line.starts_on is None and value.line.ends_on is None,
                    "TARGET_GOAL_SCOPE", "A personal amount uses its original goal's conditions and period")
            supplied = value.line.model_dump(mode="json")["settings"].get("balance_goal", {})
            require(all(key not in ("kind", "amount", "person_amounts") and settings.get(key) == amount
                        for key, amount in supplied.items()),
                    "TARGET_GOAL_SETTINGS", "Personal target settings must match the referenced goal")
    policy = fact_policy({"id": m.uid(), "department_id": actor.department_id, "revision": 1,
                          "status": "active", "on": True, "kind": value.kind,
                          "person_id": value.person_id, "calendar_entry_id": value.calendar_entry_id,
                          "data": {**value.model_dump(mode="json", exclude={"reason", "replaces_id", "expected_revision"}),
                                   "exception_allowed": exception_allowed,
                                   "catalogue_version": 1 if value.catalogue_id in ("overtime_offers", "protected_time") else value.catalogue_version,
                                   "programme_id": programme_id}})
    policy["definition"] = s.PolicyDefinition.model_validate(policy["definition"]).model_dump(mode="json")
    check_policy_references(db, actor, policy["definition"])
    require(not definition_issues(policy["definition"]), "SOLVER_RULE_INCOMPATIBLE", "Adjust the fact settings before proposing it")
    data, _, _ = people_data(db, actor, independence.today(db, actor.department_id))
    check_definition_count_budget(data, policy["definition"])
    # Reference validation uses the same resolved primitives as scheduling, including overtime cap dependencies.
    for copy in expand(policy):
        policy_references(db, actor, s.PolicyDefinition.model_validate(copy["definition"], context={
            "historical": True, 'resolved_catalogue_line': True}))


def propose(db, actor, command):
    require_role(actor, "coordinator", "admin")
    kinds, _ = _family(command)
    value = s.SchedulingFactInput.model_validate(command.payload)
    from .rule_catalogue import with_blocks
    [line] = with_blocks(value.catalogue_id, {}, [value.line.model_dump(mode='json', exclude_unset=True)],
                         independence.today(db, actor.department_id), value.catalogue_version)
    value.line = s.RuleLine.model_validate(line)
    require(value.kind in kinds, "FACT_KIND", "This command does not author that fact kind")
    require(db.scalar(select(m.CatalogueConversion.id).where(m.CatalogueConversion.department_id == actor.department_id,
                m.CatalogueConversion.status == "proposed")) is None,
            "CONVERSION_WAITING", "Decide the department catalogue conversion before proposing personal changes", 409)
    previous = scoped(db, m.SchedulingFact, value.replaces_id, actor) if value.replaces_id else None
    if previous:
        require(previous.request_id is None, "REQUEST_FACT_LINKED", "Change this overtime offer through its request", 409)
        expected(previous, value.expected_revision)
        require(previous.status == "active", "STALE_REVISION", "Only the current approved fact can be replaced", 409)
        require((previous.kind, previous.person_id, previous.calendar_entry_id) ==
                (value.kind, value.person_id, value.calendar_entry_id), "FACT_SUBJECT", "A replacement keeps the same person and fact kind")
        require((previous.data["catalogue_id"], previous.data["line"]["id"], previous.data.get("goal_ref")) ==
                (value.catalogue_id, value.line.id, value.goal_ref.model_dump(mode="json") if value.goal_ref else None),
                "FACT_SOURCE", "A replacement retains its source identity")
        supplied = (command.payload["line"].get("settings") or {}).get("allocation_overtime") or {}
        if value.kind == "overtime_offer" and "volunteer_required" not in supplied:
            prior = s.CatalogueRule.model_validate({"kind": "catalogue", "id": "overtime_offers",
                "catalogue_version": 1, "lines": [previous.data["line"]]}, context={"historical": True})
            value.line.settings["allocation_overtime"]["volunteer_required"] = (
                prior.lines[0].settings["allocation_overtime"]["volunteer_required"])
    bindings = value.line.model_dump(mode="json").get("source_bindings") or {}
    require(bindings == ((previous.data["line"].get("source_bindings") or {}) if previous else {}),
            "FACT_SOURCE_IMMUTABLE", "Source bindings are frozen; only a department conversion may create them")
    require(not bindings or set(value.line.numbers) <= set(bindings), "SOURCE_NUMBER_IMMUTABLE",
            "Add a new numeric obligation as a separate fact; this replacement retains its bound source numbers", 409)
    inherited_default = (previous.data["line"].get("source_default") if previous else None)
    require(value.line.source_default is None or value.line.source_default.model_dump(mode="json") == inherited_default,
            "FACT_SOURCE_IMMUTABLE", "An inherited source default can only be changed through its reviewed rule")
    if inherited_default:
        require(value.on, "FACT_SOURCE_DEFAULT", "This fact retains an inherited rule default; change its reviewed source rule before disabling it")
        value.line.source_default = s.RuleLine.model_validate(inherited_default)
    require_available_proposal(db, actor, value, previous)
    _references(db, actor, value, exception_allowed=bool(previous and previous.data.get('exception_allowed')))
    data = {**(previous.data if previous else {}), **value.model_dump(mode="json", exclude={
        "reason", "replaces_id", "expected_revision", "kind", "person_id", "calendar_entry_id", "on"})}
    data["catalogue_version"] = 1 if value.catalogue_id in ("overtime_offers", "protected_time") else value.catalogue_version
    if value.calendar_entry_id:
        data["programme_id"] = scoped(db, m.ProgrammeCalendarEntry, value.calendar_entry_id, actor).programme_id
    row = m.SchedulingFact(department_id=actor.department_id, kind=value.kind, person_id=value.person_id,
        calendar_entry_id=value.calendar_entry_id, on=value.on, replaces_id=value.replaces_id,
        replaces_revision=value.expected_revision, starts_on=value.line.starts_on, ends_on=value.line.ends_on,
        proposed_by=actor.identity_id, reason=value.reason, data=stored_definition(data))
    db.add(row)
    history(db, actor, "scheduling_fact", row, value.reason)
    return {"fact": record(row), "message": "Scheduling fact proposed for independent approval."}


def require_available_proposal(db, actor, value, previous):
    candidates = list(db.scalars(select(m.SchedulingFact).where(m.SchedulingFact.department_id == actor.department_id,
            m.SchedulingFact.kind == value.kind, m.SchedulingFact.person_id == value.person_id,
            m.SchedulingFact.calendar_entry_id == value.calendar_entry_id,
            m.SchedulingFact.status.in_(("active", "proposed")))))
    candidates.extend(row for row in [*db.new, *db.dirty] if isinstance(row, m.SchedulingFact))
    seen = set()
    for other in candidates:
        key = other.id or id(other)
        if key in seen or other.status not in {"active", "proposed"} or (
                other.department_id, other.kind, other.person_id, other.calendar_entry_id) != (
                    actor.department_id, value.kind, value.person_id, value.calendar_entry_id):
            continue
        seen.add(key)
        if value.goal_ref and other.id != (previous.id if previous else None):
            reference = other.data.get("goal_ref") or {}
            require((reference.get("policy_id"), reference.get("line_id")) !=
                    (value.goal_ref.policy_id, value.goal_ref.line_id),
                    "PROPOSAL_WAITING", "This person already has an approved or waiting amount for that exact goal", 409)
        if other.id != (previous.id if previous else None) and (other.replaces_id == value.replaces_id and value.replaces_id
                or other.data["line"]["id"] == value.line.id):
            require(False, "PROPOSAL_WAITING", "This fact already has an approved version or a waiting proposal", 409)


def command(db, actor, command, require_checks=False):
    kinds, action = _family(command)
    if action == "propose":
        return propose(db, actor, command)
    require_role(actor, *(READERS[:3] if action in ("decline", "undo", "restore_proposal") else ("approver",)))
    value = s.FactDecisionInput.model_validate({**command.payload,
        "fact_id": command.payload.get("fact_id", command.target_id),
        "expected_revision": command.payload.get("expected_revision", command.expected_revision)})
    require(command.target_id in (None, value.fact_id) and command.expected_revision in (None, value.expected_revision),
            "FACT_TARGET", "The decision must name one exact fact revision")
    require(not value.replaces_ids, "FACT_REPLACEMENT", "A fact replaces only the predecessor reviewed in its proposal")
    row = scoped(db, m.SchedulingFact, value.fact_id, actor)
    require(row.request_id is None, "REQUEST_FACT_LINKED", "Change this overtime offer through its request", 409)
    expected(row, value.expected_revision)
    require(row.kind in kinds, "FACT_KIND", "This command does not decide that fact kind")
    require(row.migration_proposal_id is None, "CONVERSION_CHILD", "Decide or undo converted facts through the department conversion", 409)
    require(value.predecessor_revision is None or action == "undo" and row.status == "retired" and row.replaces_id,
            "FACT_PREDECESSOR", "A predecessor revision belongs only to restoring an approved replacement", 409)
    changed = False
    restored = None
    evidence = []
    if action == "activate":
        require_role(actor, "approver")
        require(row.status == "proposed", "WORKFLOW_STATE", "Only a proposed fact can be activated", 409)
        independence.ensure(db, actor, "rule.activate", independence.person_key(db, row.proposed_by) !=
            independence.person_key(db, actor.identity_id), "Another authorised person must approve this scheduling fact", value.reason, row.id)
        if row.replaces_id:
            guard_goal_change(db, actor, [row.replaces_id])
        _references(db, actor, s.SchedulingFactInput.model_validate({**{k: v for k, v in row.data.items() if k in s.SchedulingFactInput.model_fields}, "kind": row.kind,
            "person_id": row.person_id, "calendar_entry_id": row.calendar_entry_id, "on": row.on, "reason": row.reason}),
            exception_allowed=bool(row.data.get('exception_allowed')))
        if require_checks:
            from .policies import require_context_receipts
            source = record(row)
            candidate = {**source, "status": "active"}
            policy = fact_policy(candidate)
            adapter = SimpleNamespace(id=row.id, revision=row.revision, kind=row.kind, active=policy["active"],
                starts_on=row.starts_on or date.min, ends_on=row.ends_on, definition=policy["definition"])
            evidence = require_context_receipts(db, actor, [(adapter, value.reason)], [value],
                source_records={row.id: source}, checked_records={row.id: candidate})
        if row.replaces_id:
            previous = scoped(db, m.SchedulingFact, row.replaces_id, actor)
            expected(previous, row.replaces_revision)
            require(previous.status == "active", "STALE_REVISION", "The fact being replaced is no longer in force", 409)
            previous.status, previous.revision = "retired", previous.revision + 1
            history(db, actor, "scheduling_fact", previous, value.reason)
        row.status, row.approved_by, row.approved_at = "active", actor.identity_id, m.now()
        changed = True
    elif action == "decline" or action == "undo" and row.status == "proposed":
        require(row.status == "proposed", "WORKFLOW_STATE", "Only a proposed fact can be declined", 409)
        if action == "undo":
            require(row.proposed_by == actor.identity_id, "FORBIDDEN", "Only the proposer can withdraw this fact", 403)
        row.status = "declined"
    elif action == "restore_proposal":
        require(row.status == "declined" and row.approved_by is None, "WORKFLOW_STATE",
                "Only a declined fact proposal can be put back for approval", 409)
        retained = history_version(db, actor, "scheduling_fact", row.id, row.revision - 1)
        require(retained is not None and retained.snapshot.get("status") == "proposed", "WORKFLOW_STATE",
                "The previous proposal state is unavailable; propose the fact again", 409)
        require(db.scalar(select(m.CatalogueConversion.id).where(m.CatalogueConversion.department_id == actor.department_id,
                m.CatalogueConversion.status == "proposed")) is None,
                "CONVERSION_WAITING", "Decide the department catalogue conversion before proposing personal changes", 409)
        candidate = s.SchedulingFactInput.model_validate({**{k: v for k, v in row.data.items()
            if k in s.SchedulingFactInput.model_fields}, "kind": row.kind, "person_id": row.person_id,
            "calendar_entry_id": row.calendar_entry_id, "on": row.on, "reason": row.reason,
            "replaces_id": row.replaces_id, "expected_revision": row.replaces_revision})
        previous = scoped(db, m.SchedulingFact, row.replaces_id, actor) if row.replaces_id else None
        if previous:
            expected(previous, row.replaces_revision)
            require(previous.status == "active", "STALE_REVISION", "Only the current approved fact can be replaced", 409)
        require_available_proposal(db, actor, candidate, previous)
        _references(db, actor, candidate, exception_allowed=bool(row.data.get('exception_allowed')))
        row.status = "proposed"
    elif action == "retire":
        require(row.status == "active", "WORKFLOW_STATE", "Only an active fact can be retired", 409)
        require(not row.data["line"].get("source_default"), "FACT_SOURCE_DEFAULT",
                "This fact retains an inherited rule default; use a reviewed replacement instead", 409)
        guard_goal_change(db, actor, [row.id])
        row.status, changed = "retired", True
    elif action == "undo":
        require_role(actor, "approver")
        require(row.status in ("active", "retired") and row.approved_by,
                "WORKFLOW_STATE", "Only an approved fact can be restored or undone", 409)
        replacements = list(db.scalars(select(m.SchedulingFact).where(m.SchedulingFact.department_id == actor.department_id,
            m.SchedulingFact.replaces_id == row.id, m.SchedulingFact.status.in_(("active", "proposed")))))
        replacements.extend(other for other in [*db.new, *db.dirty] if isinstance(other, m.SchedulingFact))
        require(not any(other not in db.deleted and other.department_id == actor.department_id
                and other.replaces_id == row.id and other.status in {"active", "proposed"} for other in replacements),
            "LATER_REPLACEMENT", "A later replacement blocks restoring this fact", 409)
        guard_goal_change(db, actor, [row.id])
        if row.status == "active":
            row.status = "retired"
            if row.replaces_id:
                previous = scoped(db, m.SchedulingFact, row.replaces_id, actor)
                require(previous.revision == row.replaces_revision + 1, "STALE_REVISION",
                        f"{previous.id}: Another session changed this record; reload and reconcile", 409,
                        {"current_revision": previous.revision, "id": previous.id})
                require(previous.status == "retired", "LATER_REPLACEMENT", "The predecessor changed after approval", 409)
                _references(db, actor, s.SchedulingFactInput.model_validate({**{k: v for k, v in previous.data.items()
                    if k in s.SchedulingFactInput.model_fields}, "kind": previous.kind, "person_id": previous.person_id,
                    "calendar_entry_id": previous.calendar_entry_id, "on": previous.on, "reason": previous.reason}),
                    exception_allowed=bool(previous.data.get('exception_allowed')))
                previous.status, previous.revision = "active", previous.revision + 1
                history(db, actor, "scheduling_fact", previous, value.reason)
                restored = record(previous)
        else:
            candidate = s.SchedulingFactInput.model_validate({**{k: v for k, v in row.data.items()
                if k in s.SchedulingFactInput.model_fields}, "kind": row.kind, "person_id": row.person_id,
                "calendar_entry_id": row.calendar_entry_id, "on": row.on, "reason": row.reason,
                "replaces_id": row.replaces_id if value.predecessor_revision is not None else None,
                "expected_revision": value.predecessor_revision})
            if row.replaces_id:
                previous = scoped(db, m.SchedulingFact, row.replaces_id, actor)
                if value.predecessor_revision is not None:
                    require(previous.revision == value.predecessor_revision, "STALE_REVISION",
                            f"{previous.id}: Another session changed this record; reload and reconcile", 409,
                            {"current_revision": previous.revision, "id": previous.id})
                    require(previous.status == "active", "LATER_REPLACEMENT", "The predecessor is no longer in force", 409)
                    require_available_proposal(db, actor, candidate, previous)
                    guard_goal_change(db, actor, [previous.id])
                else:
                    require(previous.status == "retired", "LATER_REPLACEMENT", "Its predecessor is in force; propose a replacement instead", 409)
            _references(db, actor, candidate, exception_allowed=bool(row.data.get('exception_allowed')))
            if row.replaces_id:
                if value.predecessor_revision is not None:
                    previous.status, previous.revision = "retired", previous.revision + 1
                    history(db, actor, "scheduling_fact", previous, value.reason)
                row.replaces_revision = previous.revision - 1
            row.status = "active"
        changed = True
    else:
        require(False, "UNKNOWN_COMMAND", "Unknown scheduling fact decision")
    row.revision += 1
    history(db, actor, "scheduling_fact", row, value.reason)
    if changed:
        bump_inputs(db, actor)
    return {"fact": record(row), **({"restored": restored} if restored else {}),
            **({"context_check_evidence": evidence} if evidence else {})}


def _personal_projection(value, person_id):
    """Self-service exposes one's amounts, without department conversion evidence or peer amounts."""
    if isinstance(value, list):
        return [_personal_projection(item, person_id) for item in value]
    if not isinstance(value, dict):
        return value
    projected = {}
    for key, item in value.items():
        if key in ("source_bindings", "source_default", "source_manifest"):
            continue
        if key == "person_amounts":
            projected[key] = {person_id: item[person_id]} if person_id in item else {}
        elif key == "per_person":
            projected[key] = [_personal_projection(row, person_id) for row in item
                              if row.get("person_id") in (None, person_id)]
        else:
            projected[key] = _personal_projection(item, person_id)
    return projected


def read_person_facts(db, actor, person_id, on=None):
    """Only this department's records; effective values come from the domain's one resolver."""
    from .policies import people_data
    from packages.domain.validation import within_dates
    require_person(db, actor, person_id, READERS)
    rows = [record(r) for r in db.scalars(select(m.SchedulingFact).where(
        m.SchedulingFact.department_id == actor.department_id, m.SchedulingFact.person_id == person_id).order_by(
            m.SchedulingFact.created_at, m.SchedulingFact.seq))]
    from packages.contracts.rule_catalogue import workload_fields
    for row in rows:
        if row['status'] in ('active', 'proposed') and row.get('on', True):
            row['counting_attention'] = counting_attention(fact_policy(row))
        if row["data"]["catalogue_id"] == "workload_limit":
            line = row["data"]["line"]
            row["data"] = {**row["data"], "line": {**line, **workload_fields(
                {"id": "workload_limit", "catalogue_version": 2}, line)}}
    day = on or independence.today(db, actor.department_id)
    if isinstance(day, str):
        day = date.fromisoformat(day)
    data, a, b = people_data(db, actor, day)
    data.update(scheduling_inputs(db, [actor.department_id]))
    data["policies"] = [record(r) for r in db.scalars(select(m.Policy).where(
        m.Policy.department_id == actor.department_id))
        if not r.definition.get("workspace_id")]
    data.pop("_catalogue_v2_materialized", None)
    with_tags(data)
    effective = [{"policy_id": p["id"], "revision": p["revision"], "name": p["name"],
                  "template_line": p.get("template_line"), "definition": p["definition"]}
        for p in active_rules(data["policies"], context=data) if within_dates(p, a, b, data["organisation"]["timezone"])
        and scope_applies(data, p['definition']['scope'], person_id, None, a, b)]
    result = {"person_id": person_id, "department_id": actor.department_id, "on": day.isoformat(),
            "active": [r for r in rows if r["status"] == "active"],
            "proposed": [r for r in rows if r["status"] == "proposed"],
            "history": [r for r in rows if r["status"] in ("retired", "declined")], "effective": effective}
    return result if actor.permits(*READERS) else _personal_projection(result, person_id)


def read_calendar_protections(db, actor, entry_id):
    require_role(actor, *READERS)
    entry = scoped(db, m.ProgrammeCalendarEntry, entry_id, actor)
    if actor.permits("programme_lead") and not actor.permits("admin", "coordinator", "approver", "auditor"):
        from .catalogue import programme_access
        programme_access(db, actor, entry.programme_id)
    return {"calendar_entry_id": entry_id, "protections": [record(r) for r in db.scalars(
        scoped_query(db, actor).where(m.SchedulingFact.calendar_entry_id == entry_id))]}


def guard_calendar_edit(db, actor, entry, value):
    """Descriptive changes cannot silently move an approved protection's dates or programme."""
    if any(getattr(entry, key) != getattr(value, key) for key in ("programme_id", "kind", "starts_on", "ends_on")):
        require(db.scalar(select(m.SchedulingFact.id).where(m.SchedulingFact.department_id == actor.department_id,
            m.SchedulingFact.calendar_entry_id == entry.id, m.SchedulingFact.status.in_(("active", "proposed")))) is None,
            "CALENDAR_PROTECTION_APPROVAL", "Retire or decline the linked protection before changing its calendar dates or programme", 409)


def guard_goal_change(db, actor, policy_ids):
    """Exact native goal references also protect their converted destination replacement chains."""
    ids = set(policy_ids)
    candidates = list(db.scalars(select(m.SchedulingFact).where(
        m.SchedulingFact.department_id == actor.department_id, m.SchedulingFact.kind == "target",
        m.SchedulingFact.status.in_(("active", "proposed")))))
    candidates.extend(row for row in [*db.new, *db.dirty] if isinstance(row, m.SchedulingFact))
    targets = [row for row in dict.fromkeys(candidates) if row not in db.deleted
        and row.department_id == actor.department_id and row.kind == "target" and row.status in {"active", "proposed"}
        and row.data.get("goal_ref") and not row.data["line"].get("source_bindings")]
    if not targets:
        return
    ancestors = {row.id: [row.definition.get("replaces_id"), *(row.definition.get("merged_ids") or [])]
        for row in db.scalars(select(m.Policy).where(m.Policy.department_id == actor.department_id))}
    ancestors.update({row.id: [row.replaces_id] for row in db.scalars(select(m.SchedulingFact).where(
        m.SchedulingFact.department_id == actor.department_id))})
    queue = list(ids)
    while queue:
        for previous in ancestors.get(queue.pop(), ()):
            if previous and previous not in ids:
                ids.add(previous)
                queue.append(previous)
    for binding in db.scalars(select(m.RuleSourceBinding).join(m.CatalogueConversion,
            m.CatalogueConversion.id == m.RuleSourceBinding.migration_proposal_id).where(
                m.RuleSourceBinding.department_id == actor.department_id, m.CatalogueConversion.status == "active")):
        if any(destination["id"] in ids for destination in binding.data["destinations"]):
            ids.add(binding.source_policy_id)
    for row in targets:
        reference = row.data.get("goal_ref") or {}
        require(reference.get("policy_id") not in ids,
                "GOAL_HAS_PERSONAL_TARGETS", "Retire or decline its personal target facts before replacing this goal", 409)
