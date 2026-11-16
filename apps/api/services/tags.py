"""Department tags (Configuration › Tags): one flat list; members are references (tag_members rows).

people.tags (free text on the person) is the older form: until its cleanup it is kept in step with the people a tag
labels, both ways: saving a person's tags sets their memberships (creating a tag for a new name), and renaming a tag
or changing its people renames or adds/removes the name on those people. Rules read memberships through
packages/domain/conditions.with_tags."""
import json
from sqlalchemy import event, func, inspect, select
from packages.contracts import schemas as s
from packages.domain.conditions import absolute_values, with_tags
from apps.api import models as m
from apps.api.errors import require
from apps.api.serialization import record
from .common import bump_inputs, expected, history, scoped


def _clear_batch_rows(db, *_):
    if bulk := db.info.get('bulk'):
        bulk.pop('tag_rows', None)


def _pending_rows(db, cls, query, matches, department_id=None):
    with db.no_autoflush:
        bulk = db.info.get('bulk')
        if bulk is not None and department_id is not None and cls in {m.Tag, m.TagMember, m.Policy}:
            if not db.info.get('tag_row_events'):
                event.listen(db, 'after_flush', _clear_batch_rows)
                event.listen(db, 'after_soft_rollback', _clear_batch_rows)
                db.info['tag_row_events'] = True
            cache = bulk.setdefault('tag_rows', {})
            key = (cls, department_id)
            if key not in cache:
                scope = cls.department_id == department_id
                if cls is m.TagMember:
                    # A tag's existing FK rows remain binding even if their stored department is inconsistent.
                    scope |= cls.tag_id.in_(select(m.Tag.id).where(m.Tag.department_id == department_id))
                cache[key] = list(db.scalars(select(cls).where(scope)))
            rows = cache[key]
        else:
            rows = list(db.scalars(query))
    rows = dict.fromkeys([*rows, *(row for row in db.new if isinstance(row, cls))])
    return [row for row in rows if row in db and row not in db.deleted and matches(row)]


def department_tags(db, department_id):
    return _pending_rows(db, m.Tag, select(m.Tag).where(m.Tag.department_id == department_id),
                         lambda row: row.department_id == department_id, department_id)


def members(db, **where):
    department_id = where.get('department_id')
    if department_id is None and where.get('tag_id') and db.info.get('bulk'):
        tag = db.get(m.Tag, where['tag_id'])
        department_id = tag.department_id if tag else None
    return _pending_rows(db, m.TagMember, select(m.TagMember).filter_by(**where),
                         lambda row: all(getattr(row, key) == value for key, value in where.items()), department_id)


def check_write(db, actor, value, row, removed=()):
    """A tag's name is unique in the department whatever its case; a kind it labels cannot be dropped while it has
    members of that kind; a name rules still use as text cannot change (they would stop matching)."""
    clash = any(tag.id != (row.id if row else None) and tag.name.lower() == value.name.lower()
                for tag in department_tags(db, actor.department_id))
    require(not clash, "TAG_NAME_TAKEN", f"A tag named {value.name} already exists", 409)
    if row is None:
        return
    dropped = set(row.labels or []) - set(value.labels)
    used = next((member.kind for member in members(db, tag_id=row.id)
                 if member.kind in dropped and (member.kind, member.member_id) not in removed), None) if dropped else None
    require(used is None, "TAG_IN_USE", f"Remove its {str(used).replace('_', ' ')} first, then stop it labelling them", 409)
    if value.name != row.name:
        policies = _pending_rows(db, m.Policy, select(m.Policy).where(m.Policy.department_id == actor.department_id,
            m.Policy.status.in_(("proposed", "active"))), lambda policy: policy.department_id == actor.department_id
            and policy.status in {'proposed', 'active'})
        named = [p.name for p in policies
                 if row.name.lower() in names_used(p.definition)]
        require(not named, "TAG_NAME_IN_USE", "These rules name the tag by its name; choose the tag in them first: " + ", ".join(named[:5]), 409)


def names_used(definition):
    """Tag names (lower case) in native selectors, older flat scopes and condition lines."""
    found, todo = set(), [definition or {}]
    while todo:
        value = todo.pop()
        if isinstance(value, dict):
            groups = [value.get('tags'), absolute_values(value) if value.get('field') == 'tags' else None]
            found.update(name.lower() for group in groups if isinstance(group, list)
                         for name in group if isinstance(name, str))
            todo.extend(value.values())
        elif isinstance(value, list):
            todo.extend(value)
    return found


def renamed(db, actor, tag, old, reason):
    """The tag's people carry its new name in their older tags list."""
    if old is None or old == tag.name:
        return
    for person in _people(db, tag.id):
        _set_names(db, actor, person, [tag.name if n.lower() == old.lower() else n for n in person.tags or []], reason)


def sync_person(db, actor, person, reason, *, created=False):
    """After a person's tags were saved: they are a member of exactly the department tags they name (a new name
    becomes a tag labelling people; a tag that did not label people now does)."""
    names = {n.lower(): n for n in person.tags or []}
    tags = {t.name.lower(): t for t in department_tags(db, actor.department_id)}
    held = {r.tag_id: r for r in members(db, department_id=actor.department_id, kind="people", member_id=person.id)}
    by_id = {t.id: t for t in tags.values()}
    affected = 0
    for key, name in names.items():
        tag = tags.get(key)
        if tag is None:
            tag = m.Tag(department_id=actor.department_id, name=name, colour="", description="", labels=["people"], active=True)
            db.add(tag)
            history(db, actor, "tags", tag, reason)
            affected += 1
        elif "people" not in (tag.labels or []):
            tag.labels, tag.revision = [*(tag.labels or []), "people"], tag.revision + 1
            history(db, actor, "tags", tag, reason)
            affected += 1
        if tag.id not in held:
            add_member(db, actor, tag, "people", person.id, reason, created_with_person_id=person.id if created else None)
            affected += 1
    for tag_id, row in held.items():
        if by_id[tag_id].name.lower() not in names:
            remove_member(db, actor, row, reason)
            affected += 1
    return affected


def add_member(db, actor, tag, kind, member_id, reason, *, created_with_person_id=None, created_with_tag_id=None):
    row = m.TagMember(department_id=actor.department_id, tag_id=tag.id, kind=kind, member_id=member_id)
    db.add(row)
    provenance = {key: value for key, value in (("created_with_person_id", created_with_person_id),
                                               ("created_with_tag_id", created_with_tag_id)) if value}
    history(db, actor, "tag.member_add", row, reason, snapshot_extra=provenance or None)
    return row


def remove_member(db, actor, row, reason):
    row.revision += 1
    history(db, actor, "tag.member_remove", row, reason)
    if inspect(row).pending:
        db.expunge(row)
    else:
        db.delete(row)
        if not db.info.get('bulk'):
            db.flush()


def _people(db, tag_id):
    ids = {row.member_id for row in members(db, tag_id=tag_id, kind="people")}
    return _pending_rows(db, m.Person, select(m.Person).where(m.Person.id.in_(ids)), lambda person: person.id in ids)


def _set_names(db, actor, person, names, reason):
    if names == list(person.tags or []):
        return
    person.tags, person.revision = names, person.revision + 1
    history(db, actor, "people", person, reason)


MEMBER_CLASSES = {"people": m.Person, "services": m.Service, "locations": m.Location, "assignment_types": m.AssignmentType,
                  "programmes": m.Programme, "policies": m.Policy, "events": m.Event, "department_content": m.DepartmentContent}


def checked_changes(db, actor, tag, value, proposed=None):
    """Additions use the proposed kinds; removals use the existing ones. Every addition stays scoped."""
    for refs, labels in ((value.add, proposed.labels if proposed else tag.labels),
                         (value.remove, tag.labels if tag else [])):
        for ref in refs:
            require(ref.kind in (labels or []), "TAG_KIND", f"The tag does not label {ref.kind.replace('_', ' ')}")
    for ref in value.add:
        scoped(db, MEMBER_CLASSES[ref.kind], ref.id, actor)
    add, remove = {(r.kind, r.id) for r in value.add}, {(r.kind, r.id) for r in value.remove}
    require(not add & remove, "TAG_MEMBER_CONFLICT", "A record cannot be added and removed at once")
    return add, remove


def apply_members(db, actor, tag, add, remove, reason, *, created=False):
    """Apply checked deltas in the caller's transaction; its one logical command owns the tag/input revision."""
    rows = {(r.kind, r.member_id): r for r in members(db, tag_id=tag.id)}
    added, removed = sorted(add - set(rows)), sorted(remove & set(rows))
    for kind, member_id in added:
        add_member(db, actor, tag, kind, member_id, reason, created_with_tag_id=tag.id if created else None)
    for key in removed:
        remove_member(db, actor, rows[key], reason)
    event_ids = {i for k, i in added + removed if k == "events"}
    for event in db.scalars(select(m.Event).where(m.Event.department_id == actor.department_id, m.Event.id.in_(event_ids))) if event_ids else []:
        event.revision += 1
        history(db, actor, "events", event, reason)
        pending = db.scalar(select(m.Outbox.id).where(m.Outbox.department_id == actor.department_id,
            m.Outbox.kind == "event", m.Outbox.state.in_(["queued", "retry"]),
            m.Outbox.payload["event_id"].as_string() == event.id).limit(1))
        if pending:
            from . import event_delivery
            # Carry undelivered occurrence changes into the new revision without announcing label-only changes.
            event_delivery.queue(db, actor, event, occurrence_ids=[])
    people = [i for k, i in added + removed if k == "people"]
    changed_people = ([scoped(db, m.Person, ident, actor) for ident in people] if db.info.get('bulk') else
        _pending_rows(db, m.Person, select(m.Person).where(m.Person.id.in_(people)),
                      lambda person: person.id in people)) if people else []
    for person in changed_people:
        names = [n for n in person.tags or [] if n.lower() != tag.name.lower()]
        _set_names(db, actor, person, names + [tag.name] if ("people", person.id) in add else names, reason)
    refs = lambda pairs: [{"kind": k, "id": i} for k, i in pairs]  # noqa: E731
    return {"added": refs(added), "removed": refs(removed)}


def set_members(db, actor, command):
    """tag.members: add and remove members of one tag in one audited step (Undo: the same command, swapped).
    Records already in (or already out) are left as they are and not counted."""
    value = s.TagMembersInput.model_validate(command.payload)
    tag = scoped(db, m.Tag, command.target_id, actor)
    expected(tag, command.expected_revision)
    add, remove = checked_changes(db, actor, tag, value)
    changes = apply_members(db, actor, tag, add, remove, value.reason)
    added, removed = changes["added"], changes["removed"]
    if added or removed:
        tag.revision += 1
        history(db, actor, "tags", tag, value.reason)
        bump_inputs(db, actor)
    return {"record": record(tag), **changes,
            "message": f"{len(added)} added, {len(removed)} removed."}


def preview(db, actor, tag_id, value):
    """What changing a tag's members would change, nothing saved: the rules naming the tag with how many people each
    reaches today before and after (rule_map.reach, the evaluator every check uses), the saved views that filter on
    it, and the sessions of rotas still being planned whose clinic, place, duty type or people are changing."""
    from copy import deepcopy
    from packages.domain.rule_map import reach
    from apps.api.snapshots import tag_data
    from .independence import today
    from .policies import people_data
    tag = scoped(db, m.Tag, tag_id, actor)
    proposed = value.value
    add, remove = checked_changes(db, actor, tag, value, proposed)
    if proposed:
        check_write(db, actor, proposed, tag, remove)
    day = today(db, actor.department_id)
    data, a, b = people_data(db, actor, day)
    # Reconstruct from the complete approved lineage before selecting affected sources. Native personal goals
    # can depend on a source which does not itself name this tag.
    raw = [record(p) for p in db.scalars(select(m.Policy).where(m.Policy.department_id == actor.department_id))
           if not (p.definition or {}).get("workspace_id")]
    now = tag_data(db, [actor.department_id])
    current = {(kind, identifier) for kind, identifiers in now[tag.id]["members"].items() for identifier in identifiers}
    add, remove = add - current, remove & current
    then = deepcopy(now)
    if proposed:
        then[tag.id].update(name=proposed.name, labels=proposed.labels)
    members = then[tag.id]["members"]
    for kind, member_id in add:
        members.setdefault(kind, []).append(member_id)
    for kind, member_id in remove:
        members[kind] = [i for i in members.get(kind, []) if i != member_id]

    def counts(tags):
        # people_data already gave people their tags' names: take this tag's off, then read it from `tags`.
        people = {pid: {**p, "tags": [t for t in p.get("tags") or [] if t.lower() != tag.name.lower()]} for pid, p in data["people"].items()}
        view = {**data, "people": people, "tags": tags, "policies": deepcopy(raw)}
        view.pop("_catalogue_v2_materialized", None)
        with_tags(view)
        policies = [p for p in view["policies"] if p["status"] in ("active", "proposed")]
        return [p for p in policies if lines_naming(p["definition"], tag)], reach(view, policies, a, b)
    policies, before = counts(now)
    _, after = counts(then)
    # A view's filter names a tag by id, or by its name as a list shows it ("Name" on People, "#Name" elsewhere).
    named = lambda text: tag.id in text or any(f'"{n}{tag.name.lower()}"' in text.lower() for n in ("", "#"))  # noqa: E731
    views = [v for v in db.scalars(select(m.TableView).where(m.TableView.department_id == actor.department_id)) if named(json.dumps(v.definition))]
    sessions, drafts = _draft_sessions(db, actor, add | remove, day)
    rules = [{"policy_id": p["id"], "name": p["name"], "status": p["status"], "lines": lines_naming(p["definition"], tag),
              "people_before": before.get(p["id"], 0), "people_after": after.get(p["id"], 0)} for p in policies]
    return {"tag_id": tag.id, "day": day.isoformat(), "rules": rules, "rule_lines": sum(r["lines"] for r in rules),
            "saved_views": [{"id": v.id, "name": v.name, "table_key": v.table_key} for v in views],
            "draft_sessions": sessions, "drafts": drafts}


def usage(db, department_id, tags, *, include_workspace=False, include_retired=False, include_proposed=True):
    """Per tag: {lines, rules} of the department's active and proposed rules naming it (the Tags page's Used by)."""
    statuses = {'active'} | ({'proposed'} if include_proposed else set()) | ({'retired'} if include_retired else set())
    policies = [p.definition for p in _pending_rows(db, m.Policy, select(m.Policy).where(
        m.Policy.department_id == department_id, m.Policy.status.in_(statuses)),
        lambda policy: policy.department_id == department_id and policy.status in statuses, department_id)
                if include_workspace or not (p.definition or {}).get("workspace_id")]
    out = {}
    for tag in tags:
        counts = [n for n in (lines_naming(d, tag) for d in policies) if n]
        out[tag.id] = {"lines": sum(counts), "rules": len(counts)}
    return out


def lines_naming(definition, tag):
    """Count tag references in native lines, embedded selectors and legacy scopes."""
    todo, count = [definition or {}], 0
    while todo:
        value = todo.pop()
        if isinstance(value, dict):
            literals = absolute_values(value) if 'field' in value else None
            names = [group for group in (value.get('tags'), literals if value.get('field') == 'tags' else None)
                     if isinstance(group, list)]
            count += sum(any(isinstance(name, str) and name != tag.id and name.lower() == tag.name.lower()
                             for name in group) for group in names)
            if 'field' in value:
                count += sum(literal == tag.id for literal in literals or [])
                todo.extend(item for key, item in value.items() if key not in {'value', 'values'})
            else:
                todo.extend(value.values())
        elif isinstance(value, list):
            todo.extend(value)
        elif value == tag.id:
            count += 1
    return count


def _draft_sessions(db, actor, changed, day):
    """Sessions (and their rotas) still being planned whose clinic, place (or one inside it), duty type or people
    are among `changed`: rotas not published or archived that end after today."""
    if not changed:
        return 0, 0
    ids = lambda kind: {i for k, i in changed if k == kind}  # noqa: E731
    places, children = ids("locations"), {}
    for child, parent in db.execute(select(m.Location.id, m.Location.parent_id).where(m.Location.department_id == actor.department_id)) if places else []:
        children.setdefault(parent, []).append(child)
    todo = list(places)
    while todo:
        for child in children.get(todo.pop(), []):
            if child not in places:
                places.add(child)
                todo.append(child)
    drafts = select(m.Workspace.id).where(m.Workspace.department_id == actor.department_id, m.Workspace.archived_at.is_(None),
                                          m.Workspace.status.in_(("draft", "submitted", "approved")), m.Workspace.ends_on > day)
    hit = (m.Shift.location_id.in_(places) | m.Shift.assignment_type_id.in_(ids("assignment_types"))
           | m.Shift.id.in_(select(m.ShiftService.shift_id).where(m.ShiftService.service_id.in_(ids("services"))))
           | m.Shift.id.in_(select(m.Assignment.shift_id).where(m.Assignment.person_id.in_(ids("people")))))
    found = db.execute(select(func.count(), func.count(func.distinct(m.Shift.workspace_id))).where(
        m.Shift.workspace_id.in_(drafts), m.Shift.cancelled.is_(False), hit)).one()
    return found[0], found[1]
