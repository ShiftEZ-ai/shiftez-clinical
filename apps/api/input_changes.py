"""Sanitized source references on the existing command audit, including bulk histories."""
from sqlalchemy import event, select, func, column, JSON, true, inspect, case, literal
from sqlalchemy.orm import Session
from . import models as m

KINDS = {m.Organisation: 'organisation', m.Department: 'department',
         m.Person: 'people', m.Membership: 'memberships', m.Eligibility: 'eligibilities',
         m.CapabilityGrant: 'grants', m.Availability: 'availability', m.StaffWish: 'wishes',
         m.WishOverride: 'wish_overrides', m.Policy: 'policies', m.PolicyException: 'exceptions',
         m.SchedulingFact: 'scheduling_facts', m.Shift: 'shifts', m.Assignment: 'assignments',
         m.Publication: 'publications', m.Rotation: 'rotations', m.ProgrammeMembership: 'programme_memberships',
         m.Mentorship: 'mentorships', m.AvailabilityPattern: 'availability_patterns',
         m.EventOccurrence: 'event_occurrences', m.TagMember: 'tag_members'}
POLICY_INPUT_FIELDS = ('definition', 'status', 'active', 'starts_on', 'ends_on')


def rules_changed(facts):
    """Classify live governed-rule drift, excluding display edits and generated person facts."""
    kind = KINDS[m.Policy]
    for fact in facts:
        if fact['kind'] == 'live_safety':
            def active(side):
                value = fact.get(side) or {}
                rows = value.get(kind, [])
                if 'rule_lines' in value:
                    return rows, value['rule_lines'], value.get('policy_definition_issues', {})
                return {row['id']: (row.get('revision'), {key: row.get(key) for key in POLICY_INPUT_FIELDS})
                        for row in rows if row.get('status') == 'active' and row.get('active', True)}
            if active('before') != active('after'):
                return True
    return False


@event.listens_for(Session, 'before_flush')
def collect(db, _context, _instances):
    if not db.info.get('_track_input_changes'):
        return
    refs = db.info.setdefault('_input_changes', {})
    metadata_only = db.info.setdefault('_input_metadata_only', set())
    rows = set(db.new) | set(db.dirty) | set(db.deleted)
    # Classify ORM edits before their history records, retaining the classification across flushes.
    for row in sorted(rows, key=lambda row: isinstance(row, m.DefinitionHistory)):
        if isinstance(row, m.DefinitionHistory):
            kind, ident, version = row.kind, row.object_id, row.object_revision
        elif type(row) in KINDS:
            kind, ident, version = KINDS[type(row)], row.id, row.revision
            if isinstance(row, m.Department):
                from packages.domain.input_manifest import generation_input
                previous = inspect(row).attrs.generation_defaults.history.deleted
                if previous and generation_input(previous[0]) != generation_input(row.generation_defaults):
                    refs['generation_options', ident] = {'kind': 'generation_options', 'id': ident, 'revision': version}
            if row in db.dirty:
                from packages.domain.input_manifest import FIELDS, SHIFT_FIELDS, PARTICIPANT_FIELDS
                fields = FIELDS.get(kind, ())
                if isinstance(row, m.Policy):
                    fields = POLICY_INPUT_FIELDS
                elif isinstance(row, m.Shift):
                    fields = SHIFT_FIELDS
                elif isinstance(row, m.Assignment):
                    fields = PARTICIPANT_FIELDS
                # A later display-name edit must not take credit for an earlier scheduling change.
                if fields and not any(inspect(row).attrs[key].history.has_changes()
                                      for key in fields if key not in {'id', 'revision'} and key in inspect(row).attrs):
                    metadata_only.add((kind, ident))
                    continue
                metadata_only.discard((kind, ident))
        else:
            continue
        if ident is None:
            row.id = ident = m.uid()
        kind = {'policy': 'policies', 'scheduling_fact': 'scheduling_facts', 'exception': 'exceptions',
                'department_settings': 'department', 'organisation_settings': 'organisation'}.get(kind, kind)
        if isinstance(row, m.DefinitionHistory) and (kind, ident) in metadata_only:
            continue
        if isinstance(row, m.WishOverride):
            refs['wishes', row.wish_id] = {'kind': 'wishes', 'id': row.wish_id, 'revision': None}
        refs[kind, ident] = {'kind': kind, 'id': ident, 'revision': version}


def watermark(db, department_id):
    return db.scalar(select(func.max(m.AuditEvent.seq)).where(m.AuditEvent.department_id == department_id)) or 0


def changed_facts(db, frozen, current, department_id, since):
    if not frozen.get('input_manifest'):
        return []  # Legacy evidence cannot establish which facts changed or who changed them.
    before, after = (x.get('input_manifest', {}).get('sources', {}) for x in (frozen, current))
    rows = []
    for key in sorted(before.keys() | after.keys()):
        old, new = before.get(key), after.get(key)
        if old and new and old['hash'] == new['hash']:
            continue
        ref = new or old
        rows.append({**{k: v for k, v in ref.items() if k not in {'hash', 'created_by'}},
            'change': 'added' if old is None else 'removed' if new is None else 'changed',
            'before': (old or {}).get('facts'), 'after': (new or {}).get('facts'),
            'actor': None, 'attribution_complete': False})
    if not rows:
        return []
    keys = {(r.get('source_kind', r['kind']), r.get('source_id', r['id'])) for r in rows}
    actors = {}
    if db.bind.dialect.name == 'sqlite':
        entries = func.json_each(m.AuditEvent.detail, '$.input_changes').table_valued(column('value', JSON))
        kind, ident = func.json_extract(entries.c.value, '$.kind'), func.json_extract(entries.c.value, '$.id')
    else:
        source = m.AuditEvent.detail['input_changes']
        source = case((func.json_typeof(source) == 'array', source), else_=literal([], JSON))
        entries = func.json_array_elements(source).table_valued(column('ref', JSON)).render_derived()
        kind, ident = entries.c.ref['kind'].as_string(), entries.c.ref['id'].as_string()
    ranked = select(kind.label('kind'), ident.label('id'), m.AuditEvent.actor_id,
        func.row_number().over(partition_by=(kind, ident), order_by=m.AuditEvent.seq.desc()).label('position')).select_from(
            m.AuditEvent).join(entries, true()).where(m.AuditEvent.department_id == department_id,
                m.AuditEvent.seq > since, ident.in_({key[1] for key in keys})).subquery()
    for kind, ident, actor in db.execute(select(ranked.c.kind, ranked.c.id, ranked.c.actor_id).where(ranked.c.position == 1)):
        if (kind, ident) in keys:
            actors[kind, ident] = actor
    ids = set(actors.values())
    names = dict(db.execute(select(m.Identity.id, m.Identity.display_name).where(m.Identity.id.in_(ids))).all()) if ids else {}
    people = {r['person_id'] for r in rows if r.get('person_id')}
    person_names = dict(db.execute(select(m.Person.id, m.Person.display_name).where(m.Person.id.in_(people))).all()) if people else {}
    for row in rows:
        actor = actors.get((row.get('source_kind', row['kind']), row.get('source_id', row['id'])))
        row['actor'] = {'id': actor, 'display_name': names.get(actor)} if actor else None
        row['attribution_complete'] = actor is not None
        if row.get('person_id'):
            row['person_name'] = person_names.get(row['person_id'])
    return rows
