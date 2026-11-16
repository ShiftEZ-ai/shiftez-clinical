"""Request-scoped viewer facts for the shared condition evaluator; never persist resolved operands."""
from zoneinfo import ZoneInfo

from sqlalchemy import Float, Integer, Numeric, func, or_, select

from apps.api import models as m
from apps.api.auth import held_roles
from apps.api.errors import require
from packages.contracts import schemas as s
from packages.domain import conditions
from .common import scoped


def _live(cls, day):
    return (cls.starts_on <= day, or_(cls.ends_on.is_(None), cls.ends_on > day))


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _field(context, field):
    metadata = s.condition_field_definition(field, context.table_key) if field else None
    require(not field or metadata and (context.context not in {"rule", "audience"} or field in s.CONDITION_FIELDS),
            "CONDITION_FIELD", "Choose a field from this collection")
    return metadata


def _tokens(tree):
    for node in tree.get("conditions", []):
        if "conditions" in node:
            yield from _tokens(node)
        else:
            value = node.get("value")
            for operand in value if isinstance(value, (list, tuple)) else (value,):
                if isinstance(operand, dict) and operand.get("kind") == "relative":
                    yield operand["token"]


def required_tokens(tree):
    tree = tree.model_dump(mode="json") if hasattr(tree, "model_dump") else tree
    return frozenset(_tokens(tree or {}))


def _metrics(db, actor, collection, field, target_id, table_key=None):
    from .bulk_selection import MAX_CANDIDATES, MODELS, _base_query, _project_records
    require(collection in MODELS, "BULK_COLLECTION", "Choose a supported collection")
    cls = MODELS[collection]
    query = _base_query(db, actor, collection)
    definitions = {entry["key"]: entry for entry in s.CONDITION_FIELD_DEFINITIONS}
    if table_key:
        definitions.update({entry["key"]: entry for entry in s.table_condition_definitions(table_key)})
    numeric = {entry["key"]: entry.get("operand_field") or entry["key"] for entry in definitions.values()
               if entry["value_kind"] == "number" and (not field or entry["key"] == field)}
    if collection == "policy_lines":
        sources = list(db.execute(query.with_only_columns(m.Policy.id, m.Policy.revision, m.Policy.name,
            m.Policy.status, m.Policy.definition).limit(MAX_CANDIDATES + 1)).mappings())
        require(len(sources) <= MAX_CANDIDATES, "BULK_SELECTION_LIMIT", "More than 5,000 candidates; narrow the filter", 422)
        require(sum(len(row['definition'].get("rule", {}).get("lines", [])) for row in sources) <= MAX_CANDIDATES,
                "BULK_SELECTION_LIMIT", "More than 5,000 candidates; narrow the filter", 422)
        rows = [{**{key: (value.get("value") if isinstance(value, dict) else value)
                 for key, value in line.get("numbers", {}).items()}, **line}
                for line in _project_records(db, actor, collection, sources)]
        require(len(rows) <= MAX_CANDIDATES, "BULK_SELECTION_LIMIT", "More than 5,000 candidates; narrow the filter", 422)
        metrics = {}
        for key, source_field in numeric.items():
            values = [row[source_field] for row in rows if _number(row.get(source_field))]
            if values:
                metrics[key] = {"target_available": any(source_field in row.get("numbers", {}) and
                    _number(row.get("target")) for row in rows),
                                "average": sum(values) / len(values)}
        preview = next((row for row in rows if row["id"] == target_id), None) if target_id else None
    else:
        numeric = {key: getattr(cls, source_field) for key, source_field in numeric.items()
                   if source_field in cls.__table__.columns and
                   isinstance(getattr(cls, source_field).type, (Integer, Float, Numeric))}
        metrics, preview = {}, None
        if numeric:
            source = query.with_only_columns(*(column.label(key) for key, column in numeric.items())).subquery()
            averages = db.execute(select(*(func.avg(source.c[key]).label(key) for key in numeric))).one()._mapping
            metrics = {key: {"average": float(value)} for key, value in averages.items() if value is not None}
        if target_id:
            preview = db.execute(query.where(cls.id == target_id).with_only_columns(cls.id,
                *(column.label(key) for key, column in numeric.items()))).mappings().one_or_none()
            preview = dict(preview) if preview else None
    require(not target_id or preview is not None, "NOT_FOUND", "Record not found", 404)
    return metrics, preview


def evaluation_context(db, actor, *, as_of=None, workspace_id=None, duty_day=None, context="filter",
                       collection=None, table_key=None, field=None, target_id=None, tokens=None):
    """Load each viewer relationship once per transaction, shared by selections and previews."""
    require(context in {"filter", "selection", "bulk", "rule", "audience"}, "CONDITION_CONTEXT", "Choose a valid condition context")
    require(not target_id or collection, "CONDITION_CONTEXT", "Choose the collection for this record")
    table_key = table_key or collection
    tokens = frozenset(tokens) if tokens is not None else None
    load_viewer = tokens is None or any(entry["token"] in tokens and entry["token"] != "me" and
        set(entry["requirements"]) - {"duty_day", "target", "department_average"}
        for entry in s.RELATIVE_TOKEN_CATALOGUE)
    key = (actor.identity_id, actor.department_id, as_of, workspace_id, duty_day, context, collection, table_key, field, target_id, tokens)
    cache = db.info.setdefault("condition_contexts", {})
    if key in cache:
        return cache[key]
    viewer_key = (actor.identity_id, actor.department_id, as_of, workspace_id, duty_day)
    viewer_cache = db.info.setdefault("condition_viewer_facts", {})
    if not load_viewer:
        org = db.get(m.Organisation, actor.organisation_id)
        roles = frozenset()
        today = m.now().astimezone(ZoneInfo(org.timezone)).date()
        values = {"me": [actor.person_id]} if actor.person_id and tokens and "me" in tokens else {}
    elif viewer_key in viewer_cache:
        org, roles, today, values = viewer_cache[viewer_key]
    else:
        org = db.get(m.Organisation, actor.organisation_id)
        roles = held_roles(db, actor.identity_id, actor.department_id)
        today = m.now().astimezone(ZoneInfo(org.timezone)).date()
        day = as_of or today
        locations = {location_id: {"parent_id": parent_id} for location_id, parent_id in db.execute(
            select(m.Location.id, m.Location.parent_id).where(m.Location.department_id == actor.department_id))}
        values = {"my_department": {"department_ids": [actor.department_id],
            "location_ids": list(locations),
            "service_ids": list(db.scalars(select(m.Service.id).where(m.Service.department_id == actor.department_id)))}}
        values["my_department"]["eligible_service_ids"] = values["my_department"]["service_ids"]
        if actor.person_id:
            values["me"] = [actor.person_id]
            clinics = sorted(set(db.scalars(select(m.Eligibility.service_id).where(
                m.Eligibility.department_id == actor.department_id, m.Eligibility.person_id == actor.person_id,
                *_live(m.Eligibility, duty_day or day)))))
            if clinics:
                from packages.domain.validation import ancestor_ids
                from .bulk_selection import service_locations
                footprint = service_locations(db, actor.department_id)
                roots = set().union(*(footprint.get(service_id, set()) for service_id in clinics))
                values["my_clinics"] = {"service_ids": clinics, "eligible_service_ids": clinics,
                    "location_ids": sorted(location_id for location_id in locations
                        if roots.intersection(ancestor_ids({"locations": locations}, location_id)))}
            for token, cls, source_field, reference in (("my_rank", m.Membership, "category_id", m.Category),
                                      ("my_programme", m.ProgrammeMembership, "programme_id", m.Programme)):
                held = list(db.scalars(select(getattr(cls, source_field)).join(reference, reference.id == getattr(cls, source_field)).where(
                    cls.department_id == actor.department_id, cls.person_id == actor.person_id, *_live(cls, day),
                    reference.active.is_(True), *_live(reference, day))))
                if held:
                    values[token] = sorted(set(held))
            cohorts = list(db.scalars(select(m.Rotation.cohort_id).join(m.Cohort).join(m.Programme).where(
                m.Rotation.department_id == actor.department_id, m.Rotation.person_id == actor.person_id,
                *_live(m.Rotation, day), *_live(m.Cohort, day), m.Programme.active.is_(True), *_live(m.Programme, day))))
            if cohorts:
                values["my_cohort"] = sorted(set(cohorts))
            supervisees = list(db.scalars(select(m.Mentorship.person_id).where(m.Mentorship.department_id == actor.department_id,
                m.Mentorship.mentor_id == actor.person_id, *_live(m.Mentorship, day))))
            if supervisees:
                values["people_i_supervise"] = sorted(set(supervisees))
        if "programme_lead" in roles:
            from .training import lead_programmes
            programmes = list(db.scalars(lead_programmes(db, actor, day=day).where(
                m.Programme.active.is_(True), *_live(m.Programme, day))))
            if programmes:
                values["my_programme"] = sorted(set(programmes))
                cohorts = list(db.scalars(select(m.Cohort.id).where(m.Cohort.department_id == actor.department_id,
                    m.Cohort.programme_id.in_(programmes), *_live(m.Cohort, day))))
                if cohorts:
                    values["my_cohort"] = sorted(set(values.get("my_cohort", []) + cohorts))
        routes = list(db.scalars(select(m.ReviewRoute).where(m.ReviewRoute.department_id == actor.department_id,
            m.ReviewRoute.status == "active", *_live(m.ReviewRoute, day))))
        # Review routes currently select roles for the whole department; they have no person/team scopes.
        routed = any(step.get("role", "approver") in roles for route in routes
            for step in route.definition.get("steps", []) + (route.definition.get("urgent_steps") or []))
        if "approver" in roles or routed:
            members = set(db.scalars(select(m.Membership.person_id).where(m.Membership.department_id == actor.department_id,
                *_live(m.Membership, day))))
            members.update(db.scalars(select(m.Identity.person_id).join(m.RoleBinding).where(
                m.RoleBinding.department_id == actor.department_id, m.RoleBinding.revoked.is_(False),
                m.Identity.disabled.is_(False), *_live(m.RoleBinding, day))))
            members.discard(None)
            if "approver" in roles:
                values["people_i_approve"] = sorted(members)
            if routed:
                values["my_team"] = sorted(members)
        can_read_drafts = actor.permits("coordinator", "approver", "programme_lead", "auditor")
        query = select(m.Workspace).where(m.Workspace.department_id == actor.department_id,
            m.Workspace.archived_at.is_(None), m.Workspace.status != "superseded")
        rotas = list(db.scalars(query if can_read_drafts else query.where(m.Workspace.status == "published")))
        current = [r.id for r in rotas if r.starts_on <= today < r.ends_on]
        future = [r for r in rotas if r.starts_on > today]
        if current:
            values["current_rota"] = current
        if future:
            first = min(r.starts_on for r in future)
            values["next_rota"] = [r.id for r in future if r.starts_on == first]
        viewer_cache[viewer_key] = org, roles, today, values
    values = dict(values)
    if workspace_id:
        workspace = scoped(db, m.Workspace, workspace_id, actor)
        require(actor.permits("coordinator", "approver", "programme_lead", "auditor") or workspace.status == "published",
                "NOT_FOUND", "Record not found", 404)
        values["viewed_rota"] = [workspace.id]
    load_metrics = collection and (tokens is None or {"its_target", "department_average"}.intersection(tokens) or target_id)
    if collection and not load_metrics:
        from .bulk_selection import _base_query
        _base_query(db, actor, collection)  # Preserve collection access checks without computing aggregates.
    metrics, preview = _metrics(db, actor, collection, field, target_id, table_key) if load_metrics else ({}, None)
    if preview:
        values["_preview_row"] = preview
    metric_tokens = {token for token, present in (
        ("its_target", any(metric.get("target_available") for metric in metrics.values())),
        ("department_average", any("average" in metric for metric in metrics.values()))) if present}
    allowed = frozenset(entry["token"] for entry in s.RELATIVE_TOKEN_CATALOGUE
        if context in entry["contexts"] and (not entry["requirements"] or entry["token"] in values or entry["token"] in metric_tokens
            or "duty_day" in entry["requirements"] and (duty_day or context in {"rule", "audience"}))
        and (not entry["roles"] or roles.intersection(entry["roles"])))
    result = conditions.EvaluationContext(timezone=org.timezone, week_start=org.week_start, today=today,
        as_of=as_of, duty_day=duty_day, values=values, allowed_tokens=allowed, context=context, metrics=metrics,
        table_key=table_key)
    cache[key] = result
    return result


def catalogue(db, actor, **kwargs):
    context = evaluation_context(db, actor, **kwargs)
    field = kwargs.get("field")
    metadata = _field(context, field)
    kind = metadata["value_kind"] if metadata else None
    definitions = {entry["key"]: entry for entry in s.CONDITION_FIELD_DEFINITIONS}
    if context.table_key and context.context not in {"rule", "audience"}:
        definitions.update({entry["key"]: entry for entry in s.table_condition_definitions(context.table_key)})
    tokens = []
    for entry in s.RELATIVE_TOKEN_CATALOGUE:
        if entry["token"] not in context.allowed_tokens:
            continue
        fields = entry["fields"]
        if entry["token"] == "its_target":
            fields = tuple(key for key, metric in context.metrics.items() if metric.get("target_available"))
        elif entry["token"] == "department_average":
            fields = tuple(key for key, metric in context.metrics.items() if "average" in metric)
        operand_field = (metadata.get("operand_field") or field) if metadata else field
        if not field or kind in entry["field_kinds"] and (not fields or operand_field in fields or field in fields):
            tokens.append({**entry, "fields": fields})
    return {"tokens": tokens,
            "fields": list(definitions.values()),
            "operators": s.CONDITION_OPERATORS,
            "timezone": context.timezone, "week_start": context.week_start, "today": context.today.isoformat()}


def validate_tree(db, actor, tree, *, as_of=None, workspace_id=None, context="filter", collection=None, table_key=None):
    tree = tree.model_dump(mode="json") if hasattr(tree, "model_dump") else tree
    if tree:
        table_key = table_key or collection
        tokens = required_tokens(tree)
        if not {"its_target", "department_average"}.intersection(tokens):
            collection = None
        result = evaluation_context(db, actor, as_of=as_of, workspace_id=workspace_id, context=context,
                                    collection=collection, table_key=table_key, tokens=tokens)
        for field in s.CollectionConditions.model_validate(tree, context={"table_key": table_key}).fields():
            _field(result, field)
        conditions.validate_relative_conditions(tree, result)
        return result


def resolve(db, actor, value):
    tokens = required_tokens({"conditions": [{"value": value.model_dump(mode="json")["operand"]}]})
    context = evaluation_context(db, actor, **value.model_dump(exclude={"operand"}), tokens=tokens)
    _field(context, value.field)
    return conditions.resolve_operand(value.operand, context, value.field, row=context.values.get("_preview_row"))


def resolve_batch(db, actor, value):
    tokens = required_tokens({"conditions": [{"value": item.model_dump(mode="json")["operand"]} for item in value.operands]})
    context = evaluation_context(db, actor, **value.model_dump(exclude={"operands"}), tokens=tokens)
    for item in value.operands:
        _field(context, item.field)
    results = []
    for item in value.operands:
        resolved = conditions.resolve_operand(item.operand, context, item.field, row=context.values.get("_preview_row"))
        result = {"label": resolved["label"], "display": resolved["display"]}
        if resolved["from"] is not None:
            result.update({"from": resolved["from"], "to": resolved["to"]})
        elif isinstance(resolved["value"], (list, tuple)):
            result["ids"] = list(resolved["value"])
        else:
            result["value"] = resolved["value"]
        results.append(result)
    return {"results": results}


def parse_date(db, actor, value):
    context = evaluation_context(db, actor, **value.model_dump(exclude={"text"}), tokens=())
    return {"matches": conditions.parse_date_short_form(value.text, context)}
