"""Literal text and explicit record references; never clinical applicability or graph traversal."""
import json
from sqlalchemy import Boolean, String, false, literal, or_, select, true
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement
from apps.api import models as m
from apps.api.errors import Problem, require
from packages.contracts import schemas as s
from packages.domain.conditions import absolute_values
from .collection_filters import _unicode_lower


class search_lower(FunctionElement):
    type = String()
    inherit_cache = True


@compiles(search_lower)
def _lower(element, compiler, **kw):
    return compiler.process(_unicode_lower(*element.clauses), **kw)


@compiles(search_lower, 'sqlite')
def _sqlite_lower(element, compiler, **kw):
    return 'fmuc_search_lower(' + compiler.process(next(iter(element.clauses)), **kw) + ')'


class contains_literal(FunctionElement):
    type = Boolean()
    inherit_cache = True


@compiles(contains_literal)
def _contains(element, compiler, **kw):
    column, text = (compiler.process(arg, **kw) for arg in element.clauses)
    return f'(strpos({column}, {text}) > 0)'


@compiles(contains_literal, 'sqlite')
def _sqlite_contains(element, compiler, **kw):
    column, text = (compiler.process(arg, **kw) for arg in element.clauses)
    return f'(instr({column}, {text}) > 0)'


def text_predicate(columns, q, options):
    options = options or s.SearchOptions()
    term = (q or '').strip()
    if not term:
        return true()
    if not options.case_sensitive:
        term = term.lower()
    clauses = []
    for column in columns:
        value = column if options.case_sensitive else search_lower(column)
        clauses.append(value == term if options.match == 'exact' else contains_literal(value, literal(term)))
    return or_(*clauses) if clauses else false()


def text_matches(values, q, options):
    """Local collections use the same per-field literal semantics as SQL (no compact/fuzzy normalization)."""
    term = (q or '').strip()
    if not term:
        return True
    term = term if options.case_sensitive else term.lower()
    values = [str(value) if options.case_sensitive else str(value).lower() for value in values if value is not None]
    return any(value == term if options.match == 'exact' else term in value for value in values)


def reference_values(document, field):
    """Only typed condition operands and declared reference keys count; arbitrary strings never do."""
    if isinstance(document, list):
        return set().union(*(reference_values(value, field) for value in document))
    if not isinstance(document, dict):
        return set()
    own = set()
    if document.get('field') == field:
        own.update(value for value in absolute_values(document) or [] if isinstance(value, str))
    direct = document.get(field)
    if isinstance(direct, list):
        own.update(value for value in direct if isinstance(value, str))
    elif isinstance(direct, str):
        own.add(direct)
    return own | set().union(*(reference_values(value, field) for value in document.values() if isinstance(value, (dict, list))))


class json_reference(FunctionElement):
    type = Boolean()
    inherit_cache = False

    def __init__(self, document, field, values):
        self.field = field
        super().__init__(document, literal(json.dumps(sorted(values))))


@compiles(json_reference, 'sqlite')
def _sqlite_reference(element, compiler, **kw):
    document, values = (compiler.process(arg, **kw) for arg in element.clauses)
    field = compiler.process(literal(element.field), **kw)
    return f'fmuc_search_reference({document}, {field}, {values})'


@compiles(json_reference, 'postgresql')
def _postgres_reference(element, compiler, **kw):
    document, values = (compiler.process(arg, **kw) for arg in element.clauses)
    # Field names come only from the canonical registry; ids remain bound JSON data.
    field = element.field
    assert field.replace('_', '').isalnum()
    paths = ([f'lax $[*] ? (@ == $ids[*])'] if field == 'tags' else []) + [f'lax $.**.{field} ? (@ == $ids[*])', f'lax $.**.{field}[*] ? (@ == $ids[*])',
             f'lax $.** ? (@.field == "{field}").values[*] ? (@ == $ids[*])',
             f'lax $.** ? (@.field == "{field}").value ? (@.type() == "string" && @ == $ids[*])',
             f'lax $.** ? (@.field == "{field}").value ? (@.kind == "absolute").value[*] ? (@ == $ids[*])']
    if field == 'tags':
        paths = ['lax $[*]', 'lax $.**.tags[*]',
                 'lax $.** ? (@.field == "tags").values[*]',
                 'lax $.** ? (@.field == "tags").value ? (@.kind == "absolute").value[*]']
        return '(' + ' OR '.join("EXISTS (SELECT 1 FROM jsonb_path_query(CAST(" + document +
            ' AS jsonb), CAST(' + compiler.process(literal(path), **kw) + " AS jsonpath)) AS search_ref(value) "
            "WHERE lower((search_ref.value #>> '{}') COLLATE unicode) IN (SELECT lower(value COLLATE unicode) "
            "FROM jsonb_array_elements_text(CAST(" + values + ' AS jsonb)) AS expected(value)))' for path in paths) + ')'
    return '(' + ' OR '.join('jsonb_path_exists(CAST(' + document + ' AS jsonb), CAST(' +
        compiler.process(literal(path), **kw) + " AS jsonpath), jsonb_build_object('ids', CAST(" + values + ' AS jsonb)))'
        for path in paths) + ')'


def _reference_overlap(document, field, values):
    actual = set(document) if field == 'tags' and isinstance(document, list) else reference_values(document, field)
    if field == 'tags':
        actual, values = {value.lower() for value in actual}, {value.lower() for value in values}
    return bool(actual & set(values))


def register_sqlite(connection):
    connection.create_function('fmuc_search_lower', 1, lambda value: value.lower() if value is not None else None, deterministic=True)
    connection.create_function('fmuc_search_reference', 3, lambda doc, field, values:
        _reference_overlap(json.loads(doc or '{}'), field, json.loads(values)), deterministic=True)


def _models():
    from .bulk_selection import MODELS
    return {**MODELS, 'workspaces': m.Workspace, 'audit': m.AuditEvent, 'operations': m.Outbox,
            'notifications': m.Notification, 'timeline': m.Person}


def _text_fields(cls):
    from .bulk_selection import PRIVATE
    return tuple(column.key for column in cls.__table__.columns
        if isinstance(column.type, String) and not column.foreign_keys and column.key not in {
            *PRIVATE, 'id', 'department_id', 'organisation_id', 'subject', 'subject_normalized', 'issuer',
            'username_normalized', 'email_normalized', 'source_key', 'correlation_id', 'operation_key', 'token_hash'})


def _direct(cls):
    targets = {model.__table__.name: kind for kind, model in _models().items() if kind in s.SEARCH_ENTITY_KINDS}
    return {column.key: targets[fk.column.table.name] for column in cls.__table__.columns for fk in column.foreign_keys
            if fk.column.table.name in targets}


def _json_fields(cls):
    fields = {field['key']: field['kind'] for field in s.CONDITION_FIELD_DEFINITIONS if field['kind'] in s.SEARCH_ENTITY_KINDS}
    fields['tags'] = 'tags'
    if cls in {m.Policy, m.RecurringSeries, m.DepartmentContent, m.Event}:
        return {**fields, 'default_person_ids': 'people'}
    return {}


def registry():
    result = {}
    for collection, cls in _models().items():
        kinds = set(_direct(cls).values()) | set(_json_fields(cls).values())
        if collection in s.TAG_KINDS or collection == 'policy_lines':
            kinds.add('tags')
        if cls is m.Tag:
            kinds.update(set(s.TAG_KINDS) & set(s.SEARCH_ENTITY_KINDS))
        if cls is m.Shift:
            kinds.update(('people', 'services'))
        if cls is m.Person:
            kinds.update(('programmes', 'categories', 'services', 'capabilities', 'tags'))
        if cls is m.Programme:
            kinds.add('people')
        if any(fk.column.table.name == m.Identity.__tablename__ for column in cls.__table__.columns for fk in column.foreign_keys):
            kinds.add('people')
        if collection == 'issues':
            kinds = set(_json_fields(m.Policy).values())
        text_fields = list(_text_fields(cls))
        if collection == 'policy_lines':
            text_fields = ['name', 'note', 'catalogue_id']
        elif collection == 'issues':
            text_fields = ['message', 'code']
        result[collection] = {'text_fields': text_fields, 'reference_kinds': sorted(kinds),
            'scopes': ['all', 'self', *(['related'] if kinds else [])]}
    return result


def _active(cls, day):
    clauses = []
    if hasattr(cls, 'starts_on'):
        clauses += [cls.starts_on <= day, or_(cls.ends_on.is_(None), cls.ends_on > day)]
    if hasattr(cls, 'revoked'):
        clauses.append(cls.revoked.is_(False))
    return clauses


def _entities(db, actor, kind):
    from .bulk_selection import _base_query
    try:
        return _base_query(db, actor, kind)
    except Problem as error:
        if error.status != 403:
            raise
        if kind == 'people' and actor.person_id:
            return select(m.Person).where(m.Person.organisation_id == actor.organisation_id, m.Person.id == actor.person_id)
        return None


def matching_entities(db, actor, kinds, q, options):
    """At most the existing collection candidate ceiling, batched by kind, never per record."""
    from .bulk_selection import MAX_CANDIDATES
    selected = options.reference
    if not (q or '').strip() and not selected:
        return {}
    if selected:
        require(selected.kind in kinds, 'SEARCH_REFERENCE', 'This collection does not support that reference kind', 422)
        kinds = (selected.kind,)
    found = {}
    for kind in kinds:
        cls = _models().get(kind)
        query = _entities(db, actor, kind)
        if cls is None or query is None:
            continue
        query = query.where(cls.id == selected.id) if selected else query.where(text_predicate(
            [getattr(cls, key) for key in _text_fields(cls)], q, options))
        rows = list(db.scalars(query.order_by(cls.id).limit(MAX_CANDIDATES + 1)))
        require(len(rows) <= MAX_CANDIDATES, 'SEARCH_LIMIT', 'Narrow the related-record search', 422)
        found[kind] = {row.id for row in rows}
        if kind == 'tags':
            found['_tag_names'] = {row.name for row in rows}
    return found


def _related(cls, collection, actor, found, day):
    clauses = []
    for field, kind in _direct(cls).items():
        clauses.append(getattr(cls, field).in_(found.get(kind, set())))
    for column in cls.__table__.columns:
        if any(fk.column.table.name == 'identities' for fk in column.foreign_keys) and found.get('people'):
            clauses.append(getattr(cls, column.key).in_(select(m.Identity.id).where(
                m.Identity.person_id.in_(found['people']))))
    if collection in s.TAG_KINDS or collection == 'policy_lines':
        kind = 'policies' if collection == 'policy_lines' else collection
        clauses.append(cls.id.in_(select(m.TagMember.member_id).where(m.TagMember.department_id == actor.department_id,
            m.TagMember.kind == kind, m.TagMember.tag_id.in_(found.get('tags', set())))))
        if hasattr(cls, 'tags'):
            clauses.append(json_reference(cls.tags, 'tags', found.get('_tag_names', set()) | found.get('tags', set())))
    if cls is m.Tag:
        for kind in set(s.TAG_KINDS) & found.keys():
            clauses.append(cls.id.in_(select(m.TagMember.tag_id).where(m.TagMember.department_id == actor.department_id,
                m.TagMember.kind == kind, m.TagMember.member_id.in_(found[kind]))))
    if cls is m.Person:
        for model, field, kind in ((m.ProgrammeMembership, 'programme_id', 'programmes'), (m.Membership, 'category_id', 'categories'),
                (m.Eligibility, 'service_id', 'services'), (m.CapabilityGrant, 'capability_id', 'capabilities')):
            clauses.append(cls.id.in_(select(model.person_id).where(model.department_id == actor.department_id,
                getattr(model, field).in_(found.get(kind, set())), *_active(model, day))))
    if cls is m.Programme:
        clauses.append(cls.id.in_(select(m.ProgrammeMembership.programme_id).where(
            m.ProgrammeMembership.department_id == actor.department_id, m.ProgrammeMembership.person_id.in_(found.get('people', set())),
            *_active(m.ProgrammeMembership, day))))
    if cls is m.Shift:
        clauses.append(cls.id.in_(select(m.Assignment.shift_id).where(m.Assignment.person_id.in_(found.get('people', set())))))
        clauses.append(cls.id.in_(select(m.ShiftService.shift_id).where(m.ShiftService.service_id.in_(found.get('services', set())))))
    if hasattr(cls, 'definition'):
        for field, kind in _json_fields(cls).items():
            values = found.get(kind, set()) | (found.get('_tag_names', set()) if kind == 'tags' else set())
            if values:
                clauses.append(json_reference(cls.definition, field, values))
    return or_(*clauses) if clauses else false()


def predicate(db, actor, collection, q, options, *, as_of=None, columns=None, people=(), found=None):
    if isinstance(options, dict):
        options = s.SearchOptions.model_validate(options)
    options = options or s.SearchOptions()
    if not (q or '').strip() and not options.reference:
        return true()
    cls = _models()[collection]
    meta = registry()[collection]
    require(options.scope in meta['scopes'], 'SEARCH_SCOPE', 'This collection does not support that search scope', 422)
    from .independence import today
    day = as_of or today(db, actor.department_id)
    kinds = set(meta['reference_kinds']) | ({collection} if collection in s.SEARCH_ENTITY_KINDS else set())
    found = matching_entities(db, actor, kinds, q, options) if found is None and (options.scope != 'self' or options.reference) else (found or {})
    own = cls.id.in_(found.get(collection, set())) if options.reference else text_predicate(
        columns if columns is not None else [getattr(cls, key) for key in meta['text_fields']], q, options)
    related = _related(cls, collection, actor, found, day)
    if people:
        # These columns belong to the caller's already-authorized source (including public open offers).
        # Match only the public person name here; private directory fields never become a count oracle.
        named = select(m.Person.id).where(m.Person.organisation_id == actor.organisation_id)
        if options.reference:
            named = named.where(m.Person.id == options.reference.id) if options.reference.kind == 'people' else named.where(false())
        else:
            named = named.where(text_predicate([m.Person.display_name], q, options))
        related = or_(related, *(column.in_(named) for column in people))
    return own if options.scope == 'self' else related if options.scope == 'related' else or_(own, related)


def suggestions(db, actor, collection, kind, q, limit):
    from .bulk_selection import _base_query
    require(collection in registry(), 'SEARCH_COLLECTION', 'Choose a supported collection', 422)
    # Validate source access before returning any directory metadata.
    source = {'timeline': 'people', 'notifications': 'inbox', 'audit': 'history', 'operations': 'outbox'}.get(collection, collection)
    if source == 'workspaces':
        from apps.api.auth import require_role
        from .planner_tools import PLANNER_ROLES
        require_role(actor, *PLANNER_ROLES)
    else:
        _base_query(db, actor, source)
    require(kind in registry()[collection]['reference_kinds'] or kind == collection,
        'SEARCH_REFERENCE', 'Choose an available reference kind', 422)
    cls = _models()[kind]
    query = _entities(db, actor, kind)
    if query is None or not q.strip():
        return {'items': [], 'has_more': False}
    label = getattr(cls, 'display_name', None)
    if label is None:
        label = cls.name
    query = query.where(text_predicate([label], q, s.SearchOptions())).order_by(search_lower(label), cls.id)
    rows = list(db.execute(query.with_only_columns(cls.id, label).limit(limit + 1)))
    return {'items': [{'kind': kind, 'id': ident, 'label': name} for ident, name in rows[:limit]], 'has_more': len(rows) > limit}


def projected_matches(row, collection, q, options, found):
    """Projected lines/findings are filtered before the shared pager, never by inferred rule subjects."""
    if not (q or '').strip() and not options.reference:
        return True
    own = text_matches([row.get(key) for key in registry()[collection]['text_fields']], q, options)
    document = {key: row.get(key) for key in ('who', 'where')} if collection == 'policy_lines' else row
    fields = _json_fields(m.Policy) if collection == 'issues' else _json_fields(_models()[collection])
    related = row.get('policy_id') in found.get('_tagged_policies', set()) or any(_reference_overlap(document, field, found.get(kind, set()) |
        (found.get('_tag_names', set()) if kind == 'tags' else set())) for field, kind in fields.items())
    if options.reference:
        own = collection == options.reference.kind and row['id'] == options.reference.id
    return own if options.scope == 'self' else related if options.scope == 'related' else own or related
