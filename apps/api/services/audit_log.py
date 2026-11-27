"""Bounded audit runs; raw history and export retain every immutable event."""
import base64
import re
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import Date, and_, case, cast, func, literal, or_, select, true

from apps.api import models as m
from apps.api.auth import can_read_rules
from apps.api.errors import require
from apps.api.serialization import json_value, record
from packages.contracts import schemas as s
from packages.domain.time import aware


def read_scope(actor):
    """Apply rule privacy before audit paging, folding, search and export counts."""
    if can_read_rules(actor):
        return true()
    e = m.AuditEvent
    kinds = func.coalesce(e.detail['item_kinds'].as_string(), '')
    return ~or_(*(or_(e.action.startswith(prefix + '.', autoescape=True),
                     kinds.like('%"' + prefix + '.%')) for prefix in ('policy', 'rule', 'exception')))


def _token(values):
    return base64.urlsafe_b64encode(json.dumps(values, separators=(',', ':')).encode()).decode().rstrip('=')


def _bounds(token, size):
    try:
        values = json.loads(base64.b64decode(token + '=' * (-len(token) % 4), altchars=b'-_', validate=True))
        if not isinstance(values, list) or len(values) != size or not all(isinstance(value, str) for value in values):
            raise ValueError
        for i in range(0, size, 2):
            values[i] = aware(datetime.fromisoformat(values[i]))
            UUID(values[i + 1])
        return values
    except (TypeError, ValueError, UnicodeError):
        require(False, 'INVALID_CURSOR', 'Reload the audit log to continue paging')


def _older(stamp, ident):
    seq = select(m.RecordInsertion.seq).where(m.RecordInsertion.table_name == 'audit_events',
        m.RecordInsertion.record_id == ident).scalar_subquery()
    return or_(m.AuditEvent.created_at < stamp,
        and_(m.AuditEvent.created_at == stamp, m.AuditEvent.seq < seq))


def expand(query, run_id):
    newest, newest_id, oldest, oldest_id = _bounds(run_id, 4)
    return query.where(~_older(oldest, oldest_id),
        or_(_older(newest, newest_id), m.AuditEvent.id == newest_id))


def absence_audit_mask(event):
    """Mask absence-report reasons in every audit read; the stored event is never changed."""
    if isinstance(event, type):
        fields = func.coalesce(event.detail['payload_fields'].as_string(), '')
        return or_(and_(event.action == 'request.create', or_(
                    event.reason == 'Absence reported', event.detail['absence_category'].as_string().is_not(None),
                    and_(fields.like('%"assignment_id"%'), fields.like('%"publication_id"%')))),
                   and_(event.action == 'request.decide', event.reason == 'Absence withdrawn'))
    if isinstance(event, dict):
        detail = event.get('detail') or {}
        fields = set(detail.get('payload_fields') or ())
        masked = ((event.get('action') == 'request.create' and
                   (event.get('reason') == 'Absence reported' or detail.get('absence_category') is not None or
                    {'assignment_id', 'publication_id'} <= fields)) or
                  (event.get('action') == 'request.decide' and event.get('reason') == 'Absence withdrawn'))
        return {**event, 'reason': ''} if masked else event
    return event


def masked_reason(event):
    return case((absence_audit_mask(event), literal('')), else_=event.reason)


def mask_balance_details(db, actor, events):
    """Apply request balance visibility to the audit projection without changing stored events."""
    from .availability import readable_request_balance_ids

    marked = []
    for event in events:
        detail = event.get('detail') or {}
        if 'leave_balance_overdrawn' in detail or 'balance_confirmation' in (detail.get('payload_fields') or ()):
            marked.append((event, event.get('target_id') if event['action'] == 'request.decide' else None))
    visible = readable_request_balance_ids(db, actor, {target for _, target in marked if target})
    for event, target in marked:
        if target not in visible:
            detail = dict(event['detail'])
            detail.pop('leave_balance_overdrawn', None)
            if 'balance_confirmation' in (detail.get('payload_fields') or ()):
                detail['payload_fields'] = [key for key in detail['payload_fields'] if key != 'balance_confirmation']
            event['detail'] = detail


def folded(db, query, zone, limit, cursor=None):
    """Find consecutive islands in SQL, then hydrate only one representative per returned run."""
    e = m.AuditEvent
    if db.bind.dialect.name == 'sqlite':
        # SQLite is the local harness backend; production uses PostgreSQL's native timezone conversion.
        db.connection().connection.driver_connection.create_function('audit_local_day', 1,
            lambda value: aware(datetime.fromisoformat(value)).astimezone(zone).date().isoformat(), deterministic=True)
        day = func.audit_local_day(e.created_at)
    else:
        day = cast(func.timezone(zone.key, e.created_at), Date)
    source = query.order_by(None).with_only_columns(e.id, e.seq, e.created_at, e.action, e.actor_id,
        masked_reason(e).label('reason'),
        func.coalesce(e.detail['catalogue'].as_string(), '').label('catalogue'), day.label('day'),
        case((e.detail['leave_allowance_changes'].as_string().is_not(None), e.id), else_='').label('allowance_event')).subquery()
    order = (source.c.created_at.desc(), source.c.seq.desc())
    signature = [source.c[key] for key in ('action', 'actor_id', 'reason', 'catalogue', 'day', 'allowance_event')]
    position = func.row_number().over(order_by=order)
    ranked = select(source, position.label('position'),
        (position - func.row_number().over(partition_by=signature, order_by=order)).label('island')).cte()
    keys = [ranked.c[column.key] for column in signature] + [ranked.c.island]
    groups = select(*keys, func.count().label('count'),
        func.min(ranked.c.position).label('newest'), func.max(ranked.c.position).label('oldest'),
        func.min(ranked.c.created_at).label('first_at'), func.max(ranked.c.created_at).label('last_at')).group_by(*keys).subquery()
    newest, oldest = ranked.alias('newest'), ranked.alias('oldest')
    page = (select(e, m.Identity.display_name, groups.c.count, groups.c.first_at, groups.c.last_at, oldest.c.id)
        .select_from(groups).join(newest, newest.c.position == groups.c.newest)
        .join(oldest, oldest.c.position == groups.c.oldest).join(e, e.id == newest.c.id)
        .outerjoin(m.Identity, m.Identity.id == e.actor_id)
        .order_by(e.created_at.desc(), e.seq.desc()))
    if cursor:
        page = page.where(_older(*_bounds(cursor, 2)))
    rows = list(db.execute(page.limit(limit + 1)))
    events = []
    for event, name, count, first, last, oldest_id in rows[:limit]:
        view = absence_audit_mask(record(event))
        events.append({**view, 'kind': event.action.split('.', 1)[0], 'actor_display_name': name,
            'count': count, 'first_at': aware(first).isoformat(), 'last_at': aware(last).isoformat(),
            'first_id': oldest_id, 'last_id': event.id,
            'run_id': _token([aware(last).isoformat(), event.id, aware(first).isoformat(), oldest_id])})
    return {'events': events, 'has_more': len(rows) > limit,
        'next_cursor': _token([events[-1]['last_at'], events[-1]['last_id']]) if len(rows) > limit else None,
        'changes': sum(event['count'] for event in events)}


def _subject(kind, value, names):
    def name(ident):
        return names.get(ident, ident or 'Everyone')

    def span(start='starts_on', end='ends_on'):
        return (f" from {value[start]}" if value.get(start) else '') + (f" until {value[end]}" if value.get(end) else '')

    if kind == 'role':
        return f"{s.ROLE_LABELS.get(value.get('role'), value.get('role', 'Role'))} to {name(value.get('identity_id'))}" + span()
    if kind == 'self_approval':
        who = name(value['identity_id']) if value.get('identity_id') else s.ROLE_LABELS.get(value.get('role'), value.get('role', 'Everyone'))
        return f"{who}: {', '.join(value.get('actions', []))}" + span()
    if kind == 'review_route':
        return value.get('name')
    if kind == 'review_delegation':
        return f"{name(value.get('delegator_id'))} to {name(value.get('delegate_id'))}" + span('starts_at', 'expires_at')
    if kind in RELATIONSHIP_ITEMS:
        item = value.get(RELATIONSHIP_ITEMS[kind])
        return f"{names.get(value.get('person_id'), 'Former staff member')}: {names.get(item, 'removed item')}" + span()
    return value.get('display_name') or names.get(value.get('identity_id'))


SUBJECT_MODELS = {'role': m.RoleBinding, 'self_approval': m.SelfApprovalPermission,
    'review_route': m.ReviewRoute, 'review_delegation': m.ReviewDelegation,
    'identity': m.Identity, 'account': m.Identity,
    'membership': m.Membership, 'eligibility': m.Eligibility, 'grant': m.CapabilityGrant,
    'programme_membership': m.ProgrammeMembership}
# A person's dated record names the person and its item. Only ids are stored with the event; names are read when the
# log is shown, so a person whose details were removed is never named again by their history.
RELATIONSHIP_ITEMS = {'membership': 'category_id', 'eligibility': 'service_id', 'grant': 'capability_id',
                      'programme_membership': 'programme_id'}
SUBJECT_FIELDS = ('person_id', 'category_id', 'service_id', 'capability_id', 'programme_id', 'starts_on', 'ends_on')


def _names(db, department_id, values):
    """Display names for every account, person and catalogue item these subjects mention, one query per table."""
    ids = {value[key] for value in values if value for key in
           ('identity_id', 'delegator_id', 'delegate_id', 'person_id', *RELATIONSHIP_ITEMS.values()) if value.get(key)}
    if not ids:
        return {}
    names = dict(db.execute(select(m.Identity.id, m.Identity.display_name).where(m.Identity.id.in_(ids))).all())
    for model, label in ((m.Person, m.Person.display_name), (m.Category, m.Category.name),
                         (m.Service, m.Service.name), (m.Capability, m.Capability.name), (m.Programme, m.Programme.name)):
        query = select(model.id, label).where(model.id.in_(ids))
        query = query.where(model.organisation_id == db.get(m.Department, department_id).organisation_id) \
            if model is m.Person else query.where(model.department_id == department_id)
        names.update(db.execute(query).all())
    return names


def _result_value(result):
    return next((result[key] for key in ('record', 'permission', 'route', 'delegation', 'identity')
        if isinstance(result.get(key), dict)), {'identity_id': result['identity_id']} if result.get('identity_id') else None)


def _stored(event):
    detail = event.get('detail') or {}
    return detail.get('subject') if event['kind'] in RELATIONSHIP_ITEMS else None


def subjects(db, department_id, events):
    """Resolve historical access targets in bounded batches, preferring the event-time snapshot."""
    pending = [event for event in events if not (event.get('detail') or {}).get('target_label') and not _stored(event)]
    receipt_ids = {(event.get('detail') or {}).get('receipt_id') for event in pending if event['kind'] in SUBJECT_MODELS}
    receipt_ids.discard(None)
    receipts = {ident: _result_value(result) for ident, result in db.execute(
        select(m.CommandReceipt.id, m.CommandReceipt.result).where(m.CommandReceipt.department_id == department_id,
            m.CommandReceipt.id.in_(receipt_ids)))} if receipt_ids else {}
    records = {}
    for kind, model in SUBJECT_MODELS.items():
        ids = {event['target_id'] for event in pending if event['kind'] == kind and event.get('target_id')
            and not receipts.get((event.get('detail') or {}).get('receipt_id'))}
        if not ids:
            continue
        query = select(model).where(model.id.in_(ids))
        if model is m.Identity:
            query = query.where(model.id.in_(select(m.RoleBinding.identity_id).where(m.RoleBinding.department_id == department_id)))
        else:
            query = query.where(model.department_id == department_id)
        for row in db.scalars(query):
            records[kind, row.id] = record(row, exclude=('password_hash',))
    values = {id(event): _stored(event) or receipts.get((event.get('detail') or {}).get('receipt_id'))
              or records.get((event['kind'], event.get('target_id'))) for event in events}
    names = _names(db, department_id, values.values())
    from packages.contracts.audit import audit_summary
    for event in events:
        value = values[id(event)]
        event['target_label'] = (event.get('detail') or {}).get('target_label') or (_subject(event['kind'], value, names) if value else None)
        event['summary'] = audit_summary(event['action'], event.get('count') or 1, event['target_label'])
    return events


def event_action(kind, result):
    approval = result.get("approval")
    return f"{kind}_pending" if kind in {"policy.retire", "policy.restore"} and approval and not approval["complete"] else kind


def subject_detail(db, kind, result, *, department_id=None):
    """Persist only an access-event description, never account credentials or private request text."""
    if kind == 'department.leave_balance_settings' and result.get('leave_allowance_changes'):
        return {'leave_allowance_changes': result['leave_allowance_changes']}
    if kind == 'department.create':
        return {'target_label': result['department']['name'],
                'first_admin_binding_id': result['first_admin_binding']['id'], 'created': result['created']}
    if kind.startswith('workspace.') and any(result.get(k) for k in ('review_items', 'gap_acknowledgements', 'rejected_review_items', 'review_item_decisions')):
        return {k: result[k] for k in ('review_items', 'gap_acknowledgements', 'rejected_review_items', 'review_item_decisions') if k in result}
    if kind in {"policy.retire", "policy.restore"} and result.get("approval"):
        return {"approval": result["approval"]}
    head = kind.split('.', 1)[0]
    if head not in SUBJECT_MODELS:
        return {}
    value = _result_value(result)
    if not value:
        return {}
    if kind == "account.invite":
        identity_id = value.get("id")
        query = select(m.RoleBinding.role).where(m.RoleBinding.identity_id == identity_id)
        if department_id:
            query = query.where(m.RoleBinding.department_id == department_id)
        roles = sorted(db.scalars(query)) if identity_id else []
        label = value.get("display_name") or value.get("username") or identity_id or "Account"
        return {"target_label": f"{label} · {', '.join(s.ROLE_LABELS.get(role, role) for role in roles)}" if roles else label,
                "roles": roles}
    if head in RELATIONSHIP_ITEMS:
        return {'subject': {key: json_value(value[key]) for key in SUBJECT_FIELDS if value.get(key) is not None}}
    ids = {value[key] for key in ('identity_id', 'delegator_id', 'delegate_id') if value.get(key)}
    names = dict(db.execute(select(m.Identity.id, m.Identity.display_name).where(m.Identity.id.in_(ids))).all()) if ids else {}
    return {'target_label': _subject(head, value, names)}


def summary_match(db, actor, q):
    """Search clauses for what an entry shows besides its reason and actor: its sentence and its subject."""
    from sqlalchemy import false
    from packages.contracts.audit import summary_actions
    e = m.AuditEvent
    like, escape = "%" + re.sub(r"([\\%_])", r"\\\1", q.strip()) + "%", "\\"
    actions = summary_actions(q)
    people = select(m.Person.id).where(m.Person.organisation_id == actor.organisation_id,
                                       m.Person.display_name.ilike(like, escape=escape))
    items = {key: select(model.id).where(model.department_id == actor.department_id, model.name.ilike(like, escape=escape))
             for key, model in (('category_id', m.Category), ('service_id', m.Service), ('capability_id', m.Capability))}
    return [e.action.in_(actions) if actions else false(),
            e.detail[('subject', 'person_id')].as_string().in_(people),
            *(e.detail[('subject', key)].as_string().in_(ids) for key, ids in items.items()),
            e.detail['target_label'].as_string().ilike(like, escape=escape)]
