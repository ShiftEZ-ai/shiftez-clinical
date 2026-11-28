"""Saved collection views, shared presets and personal/department default choices."""
from copy import deepcopy
from sqlalchemy import select, func
from packages.contracts import schemas as s
from apps.api import models as m
from apps.api.errors import require
from apps.api.serialization import record
from apps.api.auth import person_access, require_role
from .common import scoped, expected

LIMIT = 20
DEPARTMENT_LIMIT = 50

_VIEW_KEY_LIST_FIELDS = {"hidden", "open", "properties", "wrap", "order"}
_VIEW_KEY_MAP_FIELDS = {"filters", "widths"}
_VIEW_KEY_SCALAR_FIELDS = {"group", "then"}
_VIEW_KEY_PAIR_FIELDS = {"sort"}
_VIEW_KEY_PAIR_LIST_FIELDS = {"group_sort", "then_sort"}


def drop_removed_person_view_keys(definition):
    """Drop person keys removed from the registry without rewriting the stored view."""
    if not isinstance(definition, dict):
        return definition
    value = deepcopy(definition)

    def current(key):
        return not (isinstance(key, str) and s.person_view_key_field(key) is not None
                    and not s.is_current_person_view_key(key))

    for field in _VIEW_KEY_LIST_FIELDS:
        items = value.get(field)
        if not isinstance(items, list):
            continue
        value[field] = [item for item in items if current(item if isinstance(item, str) else item.get("key")
                                                         if isinstance(item, dict) else None)]
    for field in _VIEW_KEY_MAP_FIELDS:
        mapping = value.get(field)
        if isinstance(mapping, dict):
            value[field] = {key: item for key, item in mapping.items() if current(key)}
    for field in _VIEW_KEY_SCALAR_FIELDS:
        if field in value and not current(value[field]):
            value.pop(field)
    for field in _VIEW_KEY_PAIR_FIELDS:
        pair = value.get(field)
        if isinstance(pair, list) and pair and not current(pair[0]):
            value.pop(field)
    for field in _VIEW_KEY_PAIR_LIST_FIELDS:
        pairs = value.get(field)
        if isinstance(pairs, list):
            value[field] = [pair for pair in pairs if not isinstance(pair, list) or not pair or current(pair[0])]
    for section in ("planner", "fairness", "rules", "timeline"):
        if isinstance(value.get(section), dict):
            value[section] = drop_removed_person_view_keys(value[section])
    return value


def can_edit(row, actor):
    return actor.permits("admin", "coordinator") if row.scope == "department" else row.owner_id == actor.identity_id


def require_edit(row, actor):
    require(can_edit(row, actor), "FORBIDDEN" if row.scope == "department" else "NOT_FOUND",
            "Saved view is read-only" if row.scope == "department" else "Saved view not found",
            403 if row.scope == "department" else 404)


def view_record(row, actor, defaults=None):
    value = {**record(row), "can_edit": can_edit(row, actor)}
    value["definition"] = drop_removed_person_view_keys(value.get("definition"))
    if isinstance(row, m.TableView):
        value.update(is_department_default=row.id == (defaults or {}).get("department_view_id"),
                     is_personal_default=row.id == (defaults or {}).get("personal_view_id"))
    return value


def lock_department_views(db, actor):
    db.execute(select(m.Department.id).where(m.Department.id == actor.department_id).with_for_update(key_share=True)).scalar_one()


def check_limit(db, actor, model, scope, table_key=None):
    query = select(func.count()).select_from(model).where(model.department_id == actor.department_id, model.scope == scope)
    if table_key is not None:
        query = query.where(model.table_key == table_key)
    if scope == "personal":
        query = query.where(model.owner_id == actor.identity_id)
    maximum = DEPARTMENT_LIMIT if scope == "department" else LIMIT
    require(db.scalar(query) < maximum, "SAVED_VIEW_LIMIT",
            "Each list keeps up to fifty department views" if scope == "department" else "Each list keeps up to twenty saved views", 409)


def visible(row, actor, table_key):
    return (row is not None and row.department_id == actor.department_id and row.table_key == table_key
            and (row.scope == "department" or row.scope == "personal" and row.owner_id == actor.identity_id))


def referenced_view(db, actor, view_id, table_key):
    row = scoped(db, m.TableView, view_id, actor)
    require(row.scope == "department" or row.owner_id == actor.identity_id,
            "NOT_FOUND", "Saved view not found", 404)
    require(row.table_key == table_key, "VIEW_TABLE_MISMATCH", "Choose a saved view from this list", 409)
    return row


def default_is_set(row, built_in, view, actor):
    return bool(row and (built_in or visible(view, actor, row.table_key)
                        and (row.owner_id is not None or view.scope == "department")))


def phone_choice(row):
    """A personal row's phone choice: its own once split, else the row's one choice (set before the split)."""
    return (row.phone_view_id, row.phone_built_in) if row.phone_split else (row.view_id, row.built_in)


def list_defaults(db, actor, table_key):
    result = s.TableViewDefaults().model_dump()
    rows = list(db.scalars(select(m.TableViewDefault).where(
        m.TableViewDefault.department_id == actor.department_id, m.TableViewDefault.table_key == table_key,
        (m.TableViewDefault.owner_id.is_(None)) | (m.TableViewDefault.owner_id == actor.identity_id))))
    choices = [("department" if row.owner_id is None else "personal", row, row.view_id, row.built_in) for row in rows]
    choices += [("phone", row, *phone_choice(row)) for row in rows if row.owner_id is not None]
    ids = {view_id for *_, view_id, _ in choices if view_id}
    views = {view.id: view for view in db.scalars(select(m.TableView).where(m.TableView.id.in_(ids)))} if ids else {}
    for name, row, view_id, built_in in choices:
        result[name + "_set"] = default_is_set(row, built_in, views.get(view_id), actor)
        result[name + "_view_id"] = view_id if result[name + "_set"] else None
    return result


def validate_planner_references(db, actor, planner):
    """Both saved-view paths refer only to records in the viewer's authorized scope."""
    for cls, ref in ((m.Location, planner.location_id), (m.AssignmentType, planner.assignment_type_id)):
        if ref:
            scoped(db, cls, ref, actor)
    if planner.person_id:
        person_access(db, actor, planner.person_id)
    if planner.colour_tags:
        tags = set(db.scalars(select(m.Tag.id).where(
            m.Tag.department_id == actor.department_id, m.Tag.id.in_(planner.colour_tags))))
        require(tags == set(planner.colour_tags), "NOT_FOUND", "Tag not found", 404)


def set_default(db, actor, command):
    value = s.TableViewDefaultInput.model_validate(command.payload)
    if value.scope == "department":
        require_role(actor, "admin", "coordinator")
    lock_department_views(db, actor)
    owner_id = actor.identity_id if value.scope == "personal" else None
    row = db.scalar(select(m.TableViewDefault).where(m.TableViewDefault.department_id == actor.department_id,
        m.TableViewDefault.owner_id == owner_id, m.TableViewDefault.table_key == value.table_key))
    require(command.target_id is None or row is not None and command.target_id == row.id,
            "NOT_FOUND", "Default choice not found", 404)
    if command.expected_revision is not None:
        require(row is not None, "STALE_REVISION", "The default choice changed; reload and reconcile", 409)
        expected(row, command.expected_revision)
    defaults = list_defaults(db, actor, value.table_key)
    choice = "phone" if value.screen == "phone" else value.scope
    previous = {"table_key": value.table_key, "scope": value.scope, "screen": value.screen,
                "view_id": defaults[choice + "_view_id"], "clear": not defaults[choice + "_set"]}
    if value.view_id is not None:
        view = referenced_view(db, actor, value.view_id, value.table_key)
        require(value.scope != "department" or view.scope == "department", "VIEW_SCOPE_MISMATCH",
                "The department default must be a department view", 409)
    if row is None:
        row = m.TableViewDefault(department_id=actor.department_id, owner_id=owner_id, table_key=value.table_key)
        db.add(row)
    else:
        row.revision += 1
    if owner_id is not None and not row.phone_split:
        # The first choice for one size keeps the other size's: the phone holds what the one choice gave it.
        row.phone_view_id, row.phone_built_in, row.phone_split = row.view_id, bool(row.built_in), True
    built_in = not value.clear and value.view_id is None
    if value.screen == "phone":
        row.phone_view_id, row.phone_built_in = value.view_id, built_in
    else:
        row.view_id, row.built_in = value.view_id, built_in
    row.updated_at = m.now()
    db.flush()
    return {"default": {**record(row), "scope": value.scope, "screen": value.screen, "set": not value.clear},
            "previous_default": previous, "defaults": list_defaults(db, actor, value.table_key)}


def view_command(db, actor, command):
    row = None
    previous = None
    if command.kind != "table_view.create":
        row = scoped(db, m.TableView, command.target_id, actor)
        require_edit(row, actor)
        expected(row, command.expected_revision)
        previous = {key: getattr(row, key) for key in ('table_key', 'name', 'definition', 'scope')}
        previous['definition'] = drop_removed_person_view_keys(previous['definition'])
    if command.kind == "table_view.delete":
        if row.scope == "department":
            lock_department_views(db, actor)
        db.delete(row)
        return {"deleted_view_id": row.id, "previous_view": previous}
    payload = {"scope": row.scope, **command.payload} if row is not None else command.payload
    value = s.TableViewInput.model_validate(payload)
    if value.definition.label is not None:
        s.PersonLabel.model_validate(value.definition.label.model_dump(), context={'label_write': True})
    if value.definition.conditions:
        from .condition_context import validate_tree
        validate_tree(db, actor, value.definition.conditions, as_of=value.definition.as_of,
                      workspace_id=value.workspace_id, table_key=value.table_key,
                      collection=value.table_key if value.table_key in s.BULK_COLLECTIONS else None)
    scope = value.scope
    if scope == "department":
        require_role(actor, "admin", "coordinator")
    planner = value.definition.planner
    if planner:  # A saved planner view may only point at records this person can see.
        validate_planner_references(db, actor, planner)
    if value.definition.print and value.definition.print.view_id:
        view = referenced_view(db, actor, value.definition.print.view_id, "planner")
        if scope == "department":
            definition = s.TableViewDefinition.model_validate(drop_removed_person_view_keys(view.definition))
            s.department_view_filters(definition.planner, definition.filters, definition.conditions)
    if row is not None:
        require(row.table_key == value.table_key, "VIEW_TABLE_MISMATCH", "A saved view belongs to one list", 409)
    if scope == "department" or row is not None and row.scope == "department":
        lock_department_views(db, actor)
    if row is None or scope != row.scope:
        check_limit(db, actor, m.TableView, scope, value.table_key)
    if row is None:
        row = m.TableView(department_id=actor.department_id, owner_id=actor.identity_id, **value.model_dump(mode="json"))
        db.add(row)
    else:
        row.name, row.definition = value.name, value.definition.model_dump(mode="json")
        if row.scope == "department" and scope == "personal":
            row.owner_id = actor.identity_id
        row.scope = scope
        row.revision += 1
    db.flush()
    return {"view": view_record(row, actor, list_defaults(db, actor, row.table_key)), "previous_view": previous}


def list_views(db, actor, table_key):
    visible = ((m.TableView.scope == "personal") & (m.TableView.owner_id == actor.identity_id)) | (m.TableView.scope == "department")
    maximum = LIMIT + DEPARTMENT_LIMIT
    rows = list(db.scalars(select(m.TableView).where(visible, m.TableView.department_id == actor.department_id,
        m.TableView.table_key == table_key).order_by(m.TableView.name, m.TableView.id).limit(maximum + 1)))
    defaults = list_defaults(db, actor, table_key)
    return {"views": [view_record(r, actor, defaults) for r in rows[:maximum]],
            "partial": len(rows) > maximum, "defaults": defaults}
