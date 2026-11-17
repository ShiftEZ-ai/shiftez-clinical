"""Department date sets use the catalogue's revision, history, audit and Undo path."""
from types import SimpleNamespace

from sqlalchemy import select

from apps.api import models as m
from apps.api.auth import require_permission, require_role
from apps.api.errors import require
from apps.api.serialization import record
from packages.contracts import schemas as s
from packages.domain.conditions import absolute_values
from packages.domain.date_sets import copy_set_to_next_year, parse_import, resolve_date_sets
from .common import expected, scoped


def references(db, actor, set_ids, names, *, active=False, op='any'):
    rows = []
    for set_id in set_ids:
        row = scoped(db, m.DateSet, set_id, actor)
        require(not active or row.active, 'DATE_SET_INACTIVE', f'Date set "{row.name}" is inactive; restore it first')
        rows.append(row)
    available = {entry['name'] for row in rows for entry in row.entries}
    missing = set(names) - available
    require(not missing, 'DATE_SET_ENTRY_NAMES', 'Missing entries in the selected date sets: ' + ', '.join(sorted(missing)))
    if op == 'all' and names:
        for row in rows:
            require(set(names) & {entry['name'] for entry in row.entries}, 'DATE_SET_ENTRY_NAMES',
                    f'Date set "{row.name}" has none of the selected entries: ' + ', '.join(sorted(names)))


def date_conditions(definition):
    pending = [definition]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            if node.get('field') == 'date_set_ids':
                yield node
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)


def check_policy_references(db, actor, definition):
    for condition in date_conditions(definition):
        references(db, actor, absolute_values(condition) or [], condition.get('entry_names') or [],
                   active=True, op=condition.get('op', 'any'))


def check_write(db, actor, value, row):
    """Preserve recurrence metadata for older clients and keep named references intact."""
    from .scheduling_facts import FACT_LABELS

    if row is None:
        return
    existing = {entry['id']: entry for entry in row.entries}
    for index, entry in enumerate(value.entries):
        before = existing.get(entry.id)
        if before and entry.repeat == before.get('repeat', 'once'):
            kept = {key: before[key] for key in ('start_year', 'fixed_duration')
                    if key in before and key not in entry.model_fields_set}
            if kept:
                value.entries[index] = s.DateSetEntry.model_validate({**entry.model_dump(mode='json'), **kept})
    removed = {entry['name'] for entry in row.entries} - {entry.name for entry in value.entries}
    stopping = row.active and not value.active
    if not removed and not stopping:
        return
    names = {other.id: {entry['name'] for entry in other.entries} for other in db.scalars(
        select(m.DateSet).where(m.DateSet.department_id == actor.department_id, m.DateSet.active.is_(True)))}
    names[row.id] = {entry.name for entry in value.entries} if value.active else set()

    def broken(set_ids, entry_names, op='any'):
        if row.id not in set_ids:
            return False
        selected = set(entry_names)
        available = {name for ident in set_ids for name in names.get(ident, ())}
        return stopping or bool((removed & selected) - available) or (
            op == 'all' and bool(removed & selected) and not selected & names[row.id])

    def rule_uses(definition):
        return any(broken(absolute_values(node) or [], node.get('entry_names') or [], node.get('op', 'any'))
                   for node in date_conditions(definition))

    rules = [policy.name for policy in db.scalars(select(m.Policy).where(
        m.Policy.department_id == actor.department_id, m.Policy.status.in_(('active', 'proposed'))))
        if rule_uses(policy.definition)]
    facts = [FACT_LABELS[fact.kind] for fact in db.scalars(select(m.SchedulingFact).where(
        m.SchedulingFact.department_id == actor.department_id, m.SchedulingFact.status.in_(('active', 'proposed'))))
        if rule_uses(fact.data)]
    calendars = [calendar.name for calendar in db.scalars(select(m.OperatingCalendar).where(
        m.OperatingCalendar.department_id == actor.department_id, m.OperatingCalendar.active.is_(True)))
        if any(broken([rule['date_set_id']], rule.get('entry_names') or [])
               for rule in calendar.definition.get('date_set_rules', []))]
    used = [*(f'Rule: {name}' for name in sorted(rules)), *(f'Fact: {name}' for name in sorted(facts)),
            *(f'Calendar: {name}' for name in sorted(calendars))]
    require(not used, 'DATE_SET_IN_USE', 'Update these references before removing the date set or entry names: '
            + '; '.join(used), 409)


def data(db, department_ids, when=None, *, acquired=None):
    """Canonical records, or the immutable versions existing when a rota was published."""
    if when is None:
        if acquired is not None:
            return {row['id']: row for row in sorted(acquired, key=lambda row: (row['name'], row['id']))}
        return {row.id: record(row) for row in db.scalars(select(m.DateSet).where(
            m.DateSet.department_id.in_(department_ids)).order_by(m.DateSet.name, m.DateSet.id))}
    versions = {}
    from apps.api.snapshots import DATE_SET_HISTORY_KIND
    for row in db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.department_id.in_(department_ids), m.DefinitionHistory.kind == DATE_SET_HISTORY_KIND,
            m.DefinitionHistory.created_at <= when).order_by(
                m.DefinitionHistory.created_at, m.DefinitionHistory.object_revision)):
        versions[row.object_id] = row.snapshot
    return versions


def read(db, actor, include_inactive=False):
    if include_inactive:
        require_permission(actor, 'date_sets.read_all')
    rows = data(db, [actor.department_id])
    return {'date_sets': [row for row in rows.values() if include_inactive or row['active']]}


def resolved(db, actor, first_day, last_day, set_ids=None, entry_names=None, include_inactive=False):
    if include_inactive:
        require_permission(actor, 'date_sets.read_all')
    require(first_day <= last_day, 'DATE_SET_RANGE', 'The last day must be on or after the first day')
    require((last_day - first_day).days <= 3660, 'DATE_SET_RANGE', 'Resolve at most ten years at a time')
    for set_id in set_ids or []:
        scoped(db, m.DateSet, set_id, actor)
    try:
        return resolve_date_sets({'date_sets': data(db, [actor.department_id])}, first_day, last_day,
                                 set_ids=set_ids, entry_names=entry_names, include_inactive=include_inactive, errors={})
    except ValueError as exc:
        require(False, 'DATE_SET_RANGE', str(exc))


def command(db, actor, command):
    from .catalogue import write_catalogue

    require_role(actor, 'admin')
    schemas = {
        'date_set.delete': s.DateSetDeleteInput,
        'date_set.entry.create': s.DateSetEntryWriteInput,
        'date_set.entry.update': s.DateSetEntryWriteInput,
        'date_set.entry.delete': s.DateSetEntryDeleteInput,
        'date_set.adjust': s.DateSetAdjustmentInput,
        'date_set.import': s.DateSetImportInput,
        'date_set.copy_next_year': s.DateSetCopyInput,
    }
    require(command.kind in schemas, 'UNKNOWN_COMMAND', 'The requested date set command is not supported')
    payload = schemas[command.kind].model_validate(command.payload)
    row = scoped(db, m.DateSet, command.target_id, actor)
    expected(row, command.expected_revision)
    current = record(row)
    value = {key: current[key] for key in s.DateSetInput.model_fields}
    entries = value['entries']
    if command.kind == 'date_set.delete':
        value['active'] = False
    elif command.kind.startswith('date_set.entry.'):
        entry_id = payload.entry.id if hasattr(payload, 'entry') else payload.entry_id
        index = next((i for i, entry in enumerate(entries) if entry['id'] == entry_id), None)
        if command.kind == 'date_set.entry.create':
            require(index is None, 'DATE_SET_ENTRY_EXISTS', 'This entry already exists', 409)
            entries.append(payload.entry.model_dump(mode='json'))
        else:
            require(index is not None, 'NOT_FOUND', 'Date set entry not found', 404)
            if command.kind == 'date_set.entry.delete':
                entries.pop(index)
            else:
                entries[index] = payload.entry.model_dump(mode='json', exclude_unset=True)
    elif command.kind == 'date_set.adjust':
        entry = next((entry for entry in entries if entry['id'] == payload.entry_id), None)
        require(entry is not None, 'NOT_FOUND', 'Date set entry not found', 404)
        entry['overrides'] = [override for override in entry.get('overrides', []) if override['year'] != payload.year]
        if payload.override is not None:
            entry['overrides'].append(payload.override.model_dump(mode='json'))
    elif command.kind == 'date_set.import':
        try:
            entries.extend(parse_import(payload.content, payload.format))
        except ValueError as exc:
            require(False, 'DATE_SET_IMPORT', str(exc))
    else:
        try:
            value = copy_set_to_next_year(current, payload.year, payload.name)
        except ValueError as exc:
            require(False, 'DATE_SET_COPY', str(exc))
    copy = command.kind == 'date_set.copy_next_year'
    single = SimpleNamespace(payload={'catalogue': 'date_sets', 'value': value, 'reason': payload.reason},
                             target_id=None if copy else row.id,
                             expected_revision=None if copy else command.expected_revision)
    return write_catalogue(db, actor, single, update=not copy)
