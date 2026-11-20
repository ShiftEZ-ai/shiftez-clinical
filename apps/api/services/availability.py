from packages.domain.time import now as clock_now
from sqlalchemy import and_, case, false, func, literal, or_, select, true
from sqlalchemy.orm import aliased, defer
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from packages.contracts import schemas as s
from packages.domain.time import aware
from packages.domain.validation import rotation_leave_findings
from apps.api import models as m
from apps.api.auth import require_role, person_access
from apps.api.errors import require
from apps.api.serialization import json_value, record
from apps.api.snapshots import inactive_series_shift_ids
from .common import scoped, expected, bump_inputs, history, history_version, outbox, supersede_outbox
from . import independence, scheduling_facts, leave_types, overtime_availability
from .leave_disclosure import leave_wording
from . import collection_filters
from apps.api.services.common import neutral_rota_name
from .conditional_reads import revision_etag

UPDATE_FIELDS = ('kind', 'start', 'end', 'leave_type_id')
UPDATE_SNAPSHOT_FIELDS = (*UPDATE_FIELDS, 'counts_as_unavailable', 'leave_type', 'manual_type_override', 'revision')
RECORDED_FACT_KINDS = frozenset({'actual_absence', 'external_commitment', 'off_duty'})


def redact_rotation_leave_findings(findings, *, rotation_ids, availability_ids, request_ids=()):
    """Shared-person conflicts retain their reason without exposing another department's record IDs."""
    result = []
    for finding in findings:
        if not finding['code'].startswith('ROTATION_LEAVE_'):
            result.append(finding)
            continue
        detail = dict(finding.get('detail') or {})
        if detail.get('rotation_id') is not None and detail['rotation_id'] not in rotation_ids:
            detail['rotation_id'] = detail['rotation_type_id'] = None
        for key, permitted in (('availability_id', availability_ids), ('request_id', request_ids)):
            if detail.get(key) is not None and detail[key] not in permitted:
                detail[key] = None
        result.append({**finding, 'detail': detail})
    return result


def rotation_leave_conflicts(db, actor, *, rotation=None, absence=None, rotation_context=None):
    """Project relational records once; the shared domain evaluator owns the rule itself."""
    cache = rotation_context if rotation_context is not None else {}
    if 'leave_timezone' not in cache:
        scope = db.execute(select(m.Organisation.timezone, m.Department.id).join(
            m.Department, m.Department.organisation_id == m.Organisation.id).where(
                m.Organisation.id == actor.organisation_id)).all()
        cache['leave_timezone'] = scope[0][0]
        cache['leave_department_ids'] = {row[1] for row in scope}
    zone = cache['leave_timezone']
    departments = cache['leave_department_ids']
    if rotation is not None:
        candidate = json_value(rotation if isinstance(rotation, dict) else rotation.model_dump())
        people = {pid for pid, _ in rotation_context['memberships']} if rotation_context is not None else {candidate['person_id']}
        if 'leave_records' not in cache:
            rows = list(db.scalars(select(m.Availability).where(m.Availability.department_id.in_(departments),
                m.Availability.person_id.in_(people), m.Availability.retracted.is_(False))))
            requests = list(db.scalars(select(m.RequestRecord).where(m.RequestRecord.department_id.in_(departments),
                m.RequestRecord.person_id.in_(people), m.RequestRecord.kind == 'leave', m.RequestRecord.status == 'submitted')))
            changed = [row for row in [*db.new, *db.dirty] if isinstance(row, (m.Availability, m.RequestRecord))]
            cache['leave_records'] = [record(row, exclude=('private_reason',)) for row in dict.fromkeys([*rows, *requests, *changed])
                if row not in db.deleted and row.department_id in departments and row.person_id in people
                and (isinstance(row, m.Availability) and not row.retracted
                     or isinstance(row, m.RequestRecord) and row.kind == 'leave' and row.status == 'submitted')]
        first, last = (date.fromisoformat(candidate[key]) for key in ('starts_on', 'ends_on'))
        wall_start, wall_end = (datetime.combine(day, time.min, ZoneInfo(zone)) for day in (first, last))
        records = [row for row in cache['leave_records'] if row['person_id'] == candidate['person_id']
                   and aware(row['start']) < wall_end and aware(row['end']) > wall_start]
        rotations = [candidate]
    else:
        candidate = json_value(absence if isinstance(absence, dict) else absence.model_dump())
        query = select(m.Rotation).where(m.Rotation.department_id.in_(departments),
            m.Rotation.person_id == candidate['person_id'])
        try:
            first = aware(candidate['start']).astimezone(ZoneInfo(zone)).date()
            last = aware(candidate['end']).astimezone(ZoneInfo(zone)).date()
            query = query.where(m.Rotation.ends_on > first)
            query = query.where(m.Rotation.starts_on <= last)
            rotations = [record(row) for row in db.scalars(query)]
        except (ValueError, OverflowError):
            require(False, 'INTERVAL_DATES', 'The interval exceeds the supported local date range')
        records = [candidate]
    ids = {row.get('rotation_type_id') for row in rotations} - {None}
    if 'leave_types' not in cache:
        cache['leave_types'] = {row.id: record(row) for row in cache.get('references', []) if isinstance(row, m.RotationType)}
    types = cache['leave_types']
    missing = ids - types.keys()
    if missing:
        types.update({row.id: record(row) for row in db.scalars(select(m.RotationType).where(
            m.RotationType.department_id.in_(departments), m.RotationType.id.in_(missing)))})
    require(ids <= types.keys(), 'NOT_FOUND', 'Record not found', 404)
    if rotation is not None:
        require(all(types[ident]['department_id'] == actor.department_id for ident in ids), 'NOT_FOUND', 'Record not found', 404)
    if not records or not rotations:
        return []
    try:
        findings = rotation_leave_findings({'organisation': {'timezone': zone}, 'rotations': rotations,
            'rotation_types': types, 'availability': records})
    except (ValueError, OverflowError):
        require(False, 'INTERVAL_DATES', 'The interval exceeds the supported local date range')
    own = lambda row: row.get('department_id', actor.department_id) == actor.department_id
    return redact_rotation_leave_findings(findings, rotation_ids={row.get('id') for row in rotations if own(row)},
        availability_ids={row.get('id') for row in records if own(row) and row['kind'] != 'leave'},
        request_ids={row.get('id') for row in records if own(row) and row['kind'] == 'leave'})


def require_rotation_leave(findings, *, record_kind=None):
    if record_kind in RECORDED_FACT_KINDS:
        return
    blocking = [finding for finding in findings if finding['severity'] == 'error']
    require(not blocking, 'ROTATION_LEAVE_NOT_ALLOWED',
            'Leave or time away is not allowed during this rotation.', 409, {'conflicts': blocking})


def _require_record_approval(db, row, values, findings):
    if values['kind'] in RECORDED_FACT_KINDS:
        return
    request = db.get(m.RequestRecord, row.request_id) if row.request_id else None
    require(not findings or request is not None and request.status == 'approved'
            and request.person_id == row.person_id and request.department_id == row.department_id
            and aware(request.start) == aware(values['start']) and aware(request.end) == aware(values['end'])
            and values['kind'] == (request.details or {}).get('availability_kind', 'approved_leave')
            and values['leave_type_id'] == request.leave_type_id,
            'ROTATION_LEAVE_APPROVAL_REQUIRED', 'Submit a leave request for approval before recording these dates or kind.',
            409, {'conflicts': findings})


def _route_rotation_leave(db, actor, value, values, findings):
    """Keep the requested record's kind for the existing independent request decision."""
    row = m.RequestRecord(department_id=actor.department_id, created_by=actor.identity_id,
        person_id=value.person_id, kind='leave', start=value.start, end=value.end,
        private_reason=value.private_reason, leave_type_id=values.get('leave_type_id'),
        counts_as_unavailable=values['counts_as_unavailable'],
        details={'availability_kind': value.kind, 'leave_type': getattr(value, 'leave_type', '')})
    db.add(row)
    db.flush()
    outbox(db, actor, 'request:' + row.id, 'request', {'request_id': row.id, 'person_id': row.person_id,
           'request_status': row.status, 'request_kind': row.kind})
    return {'record': leave_types.names(db, actor.department_id, [record(row, exclude=('private_reason',))])[0],
        'approval_required': True, 'conflicts': findings, 'warnings': [finding['message'] for finding in findings],
        'undo': {'kind': 'request.decide', 'target_id': row.id, 'expected_revision': row.revision,
                 'payload': {'decision': 'withdraw', 'reason': 'Withdraw the unapproved time-away request'}},
        'message': 'Submitted for approval because leave or time away during this rotation requires approval. No availability has changed.'}


def _update_snapshot(row):
    return {key: aware(getattr(row, key)) if key in {'start', 'end'} else getattr(row, key)
            for key in UPDATE_SNAPSHOT_FIELDS}


def _update_reply(db, actor, row, previous, reason, findings):
    operation_id = m.uid()
    return {'record': leave_types.names(db, actor.department_id, [record(row, exclude=('private_reason',))])[0],
            'conflicts': findings, 'warnings': [finding['message'] for finding in findings],
            'previous_value': json_value({key: previous[key] for key in UPDATE_FIELDS}),
            'previous_snapshot': json_value(previous), 'operation_id': operation_id,
            'undo': {'kind': 'availability.undo_update', 'target_id': row.id, 'expected_revision': row.revision,
                     'payload': {'receipt_id': db.info.get('receipt_id', operation_id), 'operation_id': operation_id, 'reason': reason}}}


def _availability_values(db, actor, value):
    require((value.end - value.start).days <= 366, "INTERVAL_TOO_LONG", "Availability entries are bounded to one year")
    require(value.kind in leave_types.LEAVE_KINDS or value.leave_type_id is None,
            'LEAVE_TYPE_NOT_APPLICABLE', 'Only leave or absence records use a leave type')
    typ = leave_types.resolve(db, actor, value.leave_type_id, getattr(value, 'leave_type', ''), value.kind in leave_types.LEAVE_KINDS)
    return {'leave_type_id': typ.id if typ else None,
            'counts_as_unavailable': typ.counts_as_unavailable if typ and value.kind in leave_types.LEAVE_KINDS else True}


def lead_protects(db, actor, kind, person_id, start, end):
    """A programme lead (not a coordinator) records or retracts only protected time, and only for trainees."""
    if actor.permits("coordinator"):
        return
    require_role(actor, "programme_lead")
    from .training import leads
    tz = ZoneInfo(db.get(m.Organisation, actor.organisation_id).timezone)
    try:
        first = aware(start).astimezone(tz).date()
        until = (aware(end) - timedelta(microseconds=1)).astimezone(tz).date() + timedelta(days=1)
    except (ValueError, OverflowError):
        require(False, "INTERVAL_DATES", "The interval exceeds the supported local date range")
    require(kind == "protected_time" and leads(db, actor, person_id, first, until),
            "FORBIDDEN", "Programme leads record protected time for trainees only", 403)


def record_availability(db, actor, command):
    require_role(actor, "coordinator", "programme_lead")
    value = s.AvailabilityInput.model_validate(command.payload)
    person_access(db, actor, value.person_id)
    lead_protects(db, actor, value.kind, value.person_id, value.start, value.end)
    independence.ensure(db, actor, "request.decide", value.person_id != actor.person_id,
                        "A different coordinator must record your own absence", value.private_reason)
    values = {**value.model_dump(), **_availability_values(db, actor, value)}
    findings = rotation_leave_conflicts(db, actor, absence=value)
    require_rotation_leave(findings, record_kind=value.kind)
    if findings and value.kind not in RECORDED_FACT_KINDS:
        return _route_rotation_leave(db, actor, value, values, findings)
    row = m.Availability(department_id=actor.department_id, recorded_by=actor.identity_id, **values)
    db.add(row)
    db.flush()
    for finding in findings:
        finding['detail']['availability_id'] = row.id
    bump_inputs(db, actor, availability=True)
    outbox(db, actor, "availability:" + row.id, "availability", {"person_id": row.person_id, "availability_id": row.id}, undoable=True)
    return {"record": leave_types.names(db, actor.department_id, [record(row, exclude=("private_reason",))])[0],
            'conflicts': findings, 'warnings': [finding['message'] for finding in findings],
            **({'undo': {'kind': 'availability.undo_create', 'target_id': row.id, 'expected_revision': row.revision,
                         'payload': {'receipt_id': db.info['receipt_id'], 'reason': 'Undo creation'}}}
               if db.info.get('receipt_id') else {}),
            "message": "Availability recorded even if it creates a shortage. Published duties have not been replaced."}


def update_availability(db, actor, command):
    require_role(actor, "coordinator")
    patch = s.AvailabilityUpdate.model_validate(command.payload)
    row = scoped(db, m.Availability, command.target_id, actor)
    expected(row, command.expected_revision)
    require(not row.retracted, "WORKFLOW_STATE", "Entry is already retracted", 409)
    person_access(db, actor, row.person_id)
    independence.ensure(db, actor, "request.decide", row.person_id != actor.person_id,
                        "A different coordinator must record your own absence", patch.reason, row.id)
    changes = patch.model_dump(include=set(UPDATE_FIELDS), exclude_unset=True)
    require(bool(changes), 'NOTHING_TO_CHANGE', 'Choose a change to apply')
    if changes.get('kind', row.kind) not in leave_types.LEAVE_KINDS and 'leave_type_id' not in changes:
        changes['leave_type_id'] = None
    previous = _update_snapshot(row)
    value = s.AvailabilityUpdate.model_validate({**{key: previous[key] for key in UPDATE_FIELDS}, **changes, 'reason': patch.reason})
    s.InstantInterval.model_validate({'start': value.start, 'end': value.end})
    require((value.end - value.start).days <= 366, "INTERVAL_TOO_LONG", "Availability entries are bounded to one year")
    type_changed = value.kind != row.kind or value.leave_type_id != row.leave_type_id
    values = {key: getattr(value, key) for key in UPDATE_FIELDS}
    # Date edits retain the recorded type's availability decision, even if that type was later archived or changed.
    if type_changed:
        values.update(_availability_values(db, actor, value))
    findings = rotation_leave_conflicts(db, actor, absence={**values, 'id': row.id, 'person_id': row.person_id})
    require_rotation_leave(findings, record_kind=value.kind)
    _require_record_approval(db, row, values, findings)
    if any(previous.get(key, getattr(row, key)) != val for key, val in values.items()):
        for key, val in values.items():
            setattr(row, key, val)
        if type_changed:
            row.manual_type_override = True
        row.revision += 1
        history(db, actor, 'availability.update', row, patch.reason)
        bump_inputs(db, actor, availability=True)
        outbox(db, actor, f'availability-updated:{row.id}:{row.revision}', 'availability',
               {'person_id': row.person_id, 'availability_id': row.id}, undoable=True)
    return _update_reply(db, actor, row, previous, patch.reason, findings)


def undo_update_availability(db, actor, command):
    require_role(actor, 'coordinator')
    value = s.AvailabilityUndoUpdate.model_validate(command.payload)
    row = scoped(db, m.Availability, command.target_id, actor)
    expected(row, command.expected_revision)
    receipt = scoped(db, m.CommandReceipt, value.receipt_id, actor)
    require(receipt.actor_id == actor.identity_id, 'NOT_FOUND', 'Change receipt not found', 404)
    cache = db.info.get('bulk', {}).setdefault('availability_operations', {})
    if receipt.id not in cache:
        replies = ([receipt.result] if receipt.command_kind in {'availability.update', 'availability.undo_update'} else
                   [reply for item in receipt.result.get('items', []) for reply in item.get('results', [])]
                   if receipt.command_kind == 'bulk.apply' else [])
        cache[receipt.id] = {reply['operation_id']: reply for reply in replies
                             if reply.get('operation_id') and reply.get('previous_snapshot')}
    source = cache[receipt.id].get(value.operation_id)
    require(source is not None and source.get('record', {}).get('id') == row.id,
            'NOT_FOUND', 'Recorded availability change not found', 404)
    previous = source['previous_snapshot']
    chain = db.info.get('bulk', {}).setdefault('availability_undo_revisions', {})
    require(row.revision == source['record']['revision'] or chain.get(row.id) == source['record']['revision'],
            'STALE_REVISION', f'{row.id}: Another session changed this record; reload and reconcile', 409)
    require(not row.retracted, 'WORKFLOW_STATE', 'Entry is already retracted', 409)
    person_access(db, actor, row.person_id)
    independence.ensure(db, actor, 'request.decide', row.person_id != actor.person_id,
                        'A different coordinator must record your own absence', value.reason, row.id)
    before = _update_snapshot(row)
    findings = rotation_leave_conflicts(db, actor, absence={**previous, 'id': row.id, 'person_id': row.person_id})
    require_rotation_leave(findings, record_kind=previous['kind'])
    _require_record_approval(db, row, previous, findings)
    for key in UPDATE_SNAPSHOT_FIELDS:
        if key != 'revision':
            setattr(row, key, aware(previous[key]) if key in {'start', 'end'} else previous[key])
    row.revision += 1
    chain[row.id] = previous['revision']
    history(db, actor, command.kind, row, value.reason)
    bump_inputs(db, actor, availability=True)
    outbox(db, actor, f'availability-updated:{row.id}:{row.revision}', 'availability',
           {'person_id': row.person_id, 'availability_id': row.id}, undoable=True,
           undo_of=f'availability-updated:{row.id}:{command.expected_revision}')
    return _update_reply(db, actor, row, before, value.reason, findings)


def retract(db, actor, command):
    require_role(actor, "coordinator", "programme_lead")
    s.Decision.model_validate(command.payload)
    row = scoped(db, m.Availability, command.target_id, actor)
    lead_protects(db, actor, row.kind, row.person_id, aware(row.start), aware(row.end))
    expected(row, command.expected_revision)
    require(not row.retracted, "WORKFLOW_STATE", "Entry is already retracted", 409)
    row.retracted = True
    row.revision += 1
    bump_inputs(db, actor, availability=True)
    cancelled = supersede_outbox(db, actor, ["availability:" + row.id],
        prefixes=[f"availability-updated:{row.id}:", f"availability-restored:{row.id}:"])
    sent = db.scalars(select(m.Outbox.event_key).where(m.Outbox.department_id == actor.department_id,
        m.Outbox.kind == 'availability', m.Outbox.payload['availability_id'].as_string() == row.id,
        m.Outbox.state == 'available_in_app'))
    last_sent, last_revision = '', 0
    for key in sent:
        suffix = key.rpartition(':')[2]
        revision = int(suffix) if suffix.isdigit() else 1
        if revision > last_revision:
            last_sent, last_revision = key, revision
    if not cancelled or last_sent and not last_sent.startswith('availability-retracted:'):
        outbox(db, actor, f"availability-retracted:{row.id}:{row.revision}", "availability",
               {"person_id": row.person_id, "availability_id": row.id}, undoable=True)
    return {"record": leave_types.names(db, actor.department_id, [record(row, exclude=("private_reason",))])[0],
            'undo': {'kind': 'availability.restore', 'target_id': row.id, 'expected_revision': row.revision,
                     'payload': {'reason': command.payload['reason']}}}


def visible_record_clause(cls=m.Availability):
    """Creation Undo retains the row and its audit evidence, outside operational lists and totals."""
    H = m.DefinitionHistory
    return ~select(H.id).where(H.department_id == cls.department_id, H.kind == 'availability.undo_create',
        H.object_id == cls.id, H.object_revision == cls.revision).correlate(cls).exists()


def undo_created(db, actor, command):
    value = s.AvailabilityUndoCreate.model_validate(command.payload)
    row = scoped(db, m.Availability, command.target_id, actor)
    expected(row, command.expected_revision)
    receipt = scoped(db, m.CommandReceipt, value.receipt_id, actor)
    require(receipt.actor_id == actor.identity_id and row.recorded_by == actor.identity_id,
            'NOT_FOUND', 'Creation receipt not found', 404)
    replies = ([receipt.result] if receipt.command_kind == 'availability.record' else
        [reply for item in receipt.result.get('items', []) for reply in item.get('results', [])]
        if receipt.command_kind == 'bulk.apply' else [])
    source = next((reply for reply in replies if reply.get('record', {}).get('id') == row.id
        and reply.get('undo', {}).get('kind') == 'availability.undo_create'), None)
    require(source is not None and source['record']['revision'] == row.revision,
            'CREATION_CHANGED', 'This record changed after creation; retract it instead', 409)
    result = retract(db, actor, command.model_copy(update={'payload': {'reason': value.reason}}))
    history(db, actor, 'availability.undo_create', row, value.reason)
    return {**{key: item for key, item in result.items() if key != 'undo'}, 'voided': True}


def restore_availability(db, actor, command):
    require_role(actor, 'coordinator', 'programme_lead')
    value = s.Decision.model_validate(command.payload)
    row = scoped(db, m.Availability, command.target_id, actor)
    lead_protects(db, actor, row.kind, row.person_id, aware(row.start), aware(row.end))
    expected(row, command.expected_revision)
    require(history_version(db, actor, 'availability.undo_create', row.id, row.revision) is None,
            'CREATION_UNDONE', 'This creation was undone; record a new entry', 409)
    require(row.retracted, 'WORKFLOW_STATE', 'Entry is not retracted', 409)
    person_access(db, actor, row.person_id)
    independence.ensure(db, actor, 'request.decide', row.person_id != actor.person_id,
                        'A different coordinator must record your own absence', value.reason, row.id)
    findings = rotation_leave_conflicts(db, actor, absence={**record(row), 'retracted': False})
    require_rotation_leave(findings, record_kind=row.kind)
    _require_record_approval(db, row, record(row), findings)
    row.retracted = False
    row.revision += 1
    history(db, actor, command.kind, row, value.reason)
    bump_inputs(db, actor, availability=True)
    outbox(db, actor, f'availability-restored:{row.id}:{row.revision}', 'availability',
           {'person_id': row.person_id, 'availability_id': row.id}, undoable=True,
           undo_of=f'availability-retracted:{row.id}:{command.expected_revision}')
    return {'record': leave_types.names(db, actor.department_id, [record(row, exclude=('private_reason',))])[0],
            'conflicts': findings, 'warnings': [finding['message'] for finding in findings],
            'undo': {'kind': 'availability.retract', 'target_id': row.id, 'expected_revision': row.revision,
                     'payload': {'reason': value.reason}}}


def require_request_person(actor, person_id):
    require(actor.permits("coordinator") or actor.person_id == person_id,
            "FORBIDDEN", "Staff may submit only their own requests", 403)


def create_request(db, actor, command):
    require_role(actor, "staff", "coordinator")
    if command.payload.get("kind") == "absence_report":
        from .published_changes import create_absence_report
        return create_absence_report(db, actor, command)
    if command.payload.get("kind") == "overtime_availability":
        value = s.OvertimeAvailabilityInput.model_validate(command.payload)
        row = overtime_availability.create(db, actor, value)
        db.add(row)
        db.flush()
        outbox(db, actor, "request:" + row.id, "request", {"request_id": row.id, "person_id": row.person_id,
               "request_status": row.status, "request_kind": row.kind})
        return {"record": record(row), "warnings": [], "message": "Overtime availability submitted for a coordinator's decision."}
    value = s.LeaveInput.model_validate(command.payload)
    person_access(db, actor, value.person_id)
    require_request_person(actor, value.person_id)
    require((value.end - value.start).days <= 366, "INTERVAL_TOO_LONG", "Requests are bounded to one year")
    require(value.kind == 'leave' or value.leave_type_id is None, 'LEAVE_TYPE_NOT_APPLICABLE', 'Only leave requests use a leave type')
    typ = leave_types.resolve(db, actor, value.leave_type_id, required=value.kind == 'leave')
    duplicate = db.scalar(select(m.RequestRecord.id).where(
        m.RequestRecord.department_id == actor.department_id, m.RequestRecord.person_id == value.person_id,
        m.RequestRecord.kind == value.kind, m.RequestRecord.leave_type_id == (typ.id if typ else None),
        m.RequestRecord.start == value.start, m.RequestRecord.end == value.end,
        m.RequestRecord.status.in_(("submitted", "approved"))).limit(1))
    require(duplicate is None, "REQUEST_DUPLICATE", "This exact request is already waiting or approved. Open the existing request.",
            409, {"request_id": duplicate})
    warnings = []
    findings = rotation_leave_conflicts(db, actor, absence=value) if value.kind == 'leave' else []
    require_rotation_leave(findings)
    warnings.extend(finding['message'] for finding in findings)
    if value.kind == 'leave':
        requests = select(m.RequestRecord.id).where(
            m.RequestRecord.department_id == actor.department_id, m.RequestRecord.person_id == value.person_id,
            m.RequestRecord.kind == 'leave', m.RequestRecord.status.in_(['submitted', 'approved']),
            m.RequestRecord.start < value.end, m.RequestRecord.end > value.start)
        absences = select(m.Availability.id).where(
            m.Availability.department_id == actor.department_id, m.Availability.person_id == value.person_id,
            m.Availability.kind.in_(leave_types.LEAVE_KINDS), m.Availability.retracted.is_(False),
            m.Availability.start < value.end, m.Availability.end > value.start)
        if db.scalar(requests.limit(1)) or db.scalar(absences.limit(1)):
            warnings.append('This overlaps existing waiting or approved leave, or a recorded absence.')
    values = {**value.model_dump(), 'leave_type_id': typ.id if typ else None,
              'counts_as_unavailable': typ.counts_as_unavailable if typ else True}
    row = m.RequestRecord(department_id=actor.department_id, created_by=actor.identity_id, **values)
    db.add(row)
    db.flush()
    outbox(db, actor, "request:" + row.id, "request", {"request_id": row.id, "person_id": row.person_id,
           "request_status": row.status, "request_kind": row.kind})
    return {"record": leave_types.names(db, actor.department_id, [record(row, exclude=("private_reason",))])[0],
            "warnings": warnings, 'conflicts': findings, "message": "Request submitted. No availability or published rota has changed."
            + (" Warning: " + " ".join(warnings) if warnings else "")}


def decision_scope(db, actor, cls, *, include_independence=True, approval_context=None):
    """Request/pattern decision authority, shared by commands and the batched list projection."""
    if cls is m.RequestRecord:
        if actor.permits("approver"):
            scope = cls.kind != "overtime_availability"
        elif actor.permits("programme_lead"):
            from .training import request_scope
            scope = request_scope(db, actor)
        else:
            scope = false()
        if actor.permits("coordinator"):
            scope = or_(scope, cls.kind == "overtime_availability")
        own = not_own(cls, actor)
    else:
        if not actor.permits("coordinator"):
            return false()
        own = (cls.created_by != actor.identity_id, *([cls.person_id != actor.person_id] if actor.person_id else []))
        scope = true()
    if include_independence and independence.permission_for(db, actor, "request.decide", context=approval_context) is None:
        scope = and_(scope, *own)
    return scope


def decision_allowed(db, actor, row, *, include_independence=True, approval_context=None):
    key = (type(row), row.id, include_independence)
    if approval_context is not None and key in approval_context['scopes']:
        return approval_context['scopes'][key]
    return bool(db.scalar(select(decision_scope(db, actor, type(row), include_independence=include_independence,
        approval_context=approval_context)).where(type(row).id == row.id)))


def may_decide(db, actor, row, *, approval_context=None):
    """Undo checks current role/programme authority without requiring a new independent decision."""
    return decision_allowed(db, actor, row, include_independence=False, approval_context=approval_context)


def owns_request(actor, row):
    """Current submitter or subject ownership, including after an identity is relinked."""
    return (row.created_by == actor.identity_id
            or actor.person_id is not None and row.person_id == actor.person_id)


def require_request_decision(db, actor, row, decision, *, approval_context=None):
    if decision == "withdraw":
        require(owns_request(actor, row),
                "FORBIDDEN", "Only the submitter or the person the request is for can withdraw it", 403)
    else:
        require(may_decide(db, actor, row, approval_context=approval_context), "FORBIDDEN", "An authorised coordinator, approver or programme lead must decide this request", 403)


def _restore_undo_absences(db, actor, request, conflicts):
    """Only a complete stored Undo restores its original request-owned absence records."""
    bulk = db.info.get("bulk", {})
    if not bulk.get("undo_source"):
        return None
    if "request_undo_absences" not in bulk:
        receipt = scoped(db, m.CommandReceipt, bulk["undo_source"], actor)
        restored = {}
        for item in receipt.result.get("items", []):
            for reply in item.get("results", []):
                if reply.get("authorization_command", {}).get("kind") != "request.undo":
                    continue
                for row in [reply["record"]] if reply.get("record") else reply.get("records", []):
                    if "retracted_availability" in reply and row.get("id"):
                        restored.setdefault(row["id"], []).append(reply["retracted_availability"])
                    elif "retracted_availability" in row:
                        restored.setdefault(row["id"], []).append(row["retracted_availability"])
        bulk["request_undo_absences"] = restored
    operations = bulk["request_undo_absences"].get(request.id)
    if not operations:
        return None
    refs = operations.pop()
    chain = bulk.setdefault("availability_undo_revisions", {})
    for ref in refs:
        absence = scoped(db, m.Availability, ref["id"], actor)
        require(absence.request_id == request.id and absence.person_id == request.person_id,
                "NOT_FOUND", "Recorded approval absence not found", 404)
        require(absence.revision == ref["revision"] or chain.get(absence.id) == ref["revision"],
                "STALE_REVISION", f"{absence.id}: Another session changed this record; reload and reconcile", 409)
        require(absence.retracted, "WORKFLOW_STATE", "Entry is not retracted", 409)
        findings = rotation_leave_conflicts(db, actor, absence={**record(absence), 'retracted': False})
        require_rotation_leave(findings, record_kind=absence.kind)
        _require_record_approval(db, absence, record(absence), findings)
        conflicts.extend(findings)
        absence.retracted = False
        absence.revision += 1
        chain[absence.id] = ref["revision"] - 1
    return len(refs)


def decide_request(db, actor, command, *, batch=False, overtime_cache=None, approval_context=None):
    value = s.RequestDecision.model_validate(command.payload)
    row = scoped(db, m.RequestRecord, command.target_id, actor)
    require_request_decision(db, actor, row, value.decision, approval_context=approval_context)
    expected(row, command.expected_revision)
    overtime = row.kind == "overtime_availability"
    require(row.status == "submitted" or overtime and row.status == "approved" and value.decision == "withdraw",
            "WORKFLOW_STATE", "Only a waiting request can be decided; an approved overtime offer may also be withdrawn", 409)
    inputs_changed = False
    conflicts = []
    affected = 1
    if value.decision == "withdraw":
        if overtime:
            overtime_availability.require_unlocked(db, actor, row)
            retired = overtime_availability.retire(db, actor, row, value.reason)
            affected += len(retired)
            row.overtime = {**row.overtime, "withdrawn_from": row.status, "withdrawn_facts": retired,
                            "previous_decision_reason": row.decision_reason,
                            "previous_decided_at": json_value(row.decided_at)}
            inputs_changed = bool(retired)
        row.status = "withdrawn"
        row.details = {**(row.details or {}), "withdrawn_by": actor.identity_id}
    else:
        require(decision_allowed(db, actor, row, approval_context=approval_context),
                "SELF_APPROVAL", "A different authorised person must decide this request", 403)
        from .training import change_people
        independence.ensure(db, actor, "request.decide",
                            independence.person_key(db, row.created_by) != independence.person_key(db, actor.identity_id)
                            and actor.person_id not in change_people(db, row),
                            "A different authorised person must decide this request", value.reason, row.id, context=approval_context)
        row.status = "approved" if value.decision == "approve" else "rejected"
        row.decided_by = actor.identity_id
        if value.decision == "approve" and overtime:
            affected += overtime_availability.approve(db, actor, row, value.reason, cache=overtime_cache)
            inputs_changed = True
        if value.decision == "approve" and row.kind == "rotation_change":
            from .training import apply_change
            affected += apply_change(db, actor, row, value.reason)
            inputs_changed = True
        if value.decision == "approve" and row.kind in {"leave", "absence_report"}:
            restored = _restore_undo_absences(db, actor, row, conflicts)
            if not restored:
                conflicts.extend(rotation_leave_conflicts(db, actor, absence=record(row)))
                require_rotation_leave(conflicts)
            if restored is None:
                absence = m.Availability(department_id=actor.department_id, person_id=row.person_id,
                                         start=row.start, end=row.end, kind=(row.details or {}).get('availability_kind', 'approved_leave'),
                                         leave_type=(row.details or {}).get('leave_type', ''),
                                         leave_type_id=row.leave_type_id, counts_as_unavailable=row.counts_as_unavailable,
                                         private_reason='' if row.kind == 'absence_report' else row.private_reason,
                                         recorded_by=actor.identity_id, request_id=row.id)
                db.add(absence)
            affected += 1 if restored is None else restored
            inputs_changed = restored is None or restored > 0
            if inputs_changed and not batch:
                bump_inputs(db, actor, availability=True)
    if overtime and inputs_changed and not batch:
        bump_inputs(db, actor, availability=True)
    row.decision_reason = value.reason
    row.decided_at = m.now()
    row.revision += 1
    # A decision undone and made again is a new event (the revision tells them apart).
    outbox(db, actor, f"request-decision:{row.id}" + (f":{row.revision}" if row.revision > 2 else ""), "request",
           {"request_id": row.id, "person_id": row.person_id, "request_status": row.status, "request_kind": row.kind,
            **({'replace_action': row.details['replace_action'],
                'publication_id': row.details['replace_action']['publication_id']}
               if row.kind == 'absence_report' and row.status == 'approved' else {})},
           undo_of="request:" + row.id if value.decision == "withdraw" else ())
    item = absence_report_record(row, actor, db)
    replace_action = (row.details or {}).get("replace_action") if row.kind == "absence_report" and row.status == "approved" else None
    if batch:
        return {"record": item, "inputs_changed": inputs_changed, "affected_count": affected,
                'conflicts': conflicts, 'warnings': [finding['message'] for finding in conflicts]}
    return {"record": leave_types.names(db, actor.department_id, [item])[0], "affected_count": affected,
            **({"replace_action": replace_action} if replace_action else {}),
            'conflicts': conflicts, 'warnings': [finding['message'] for finding in conflicts],
            "message": "Decision saved. Schedule changes and replacement cover require a reviewed rota amendment."}


def require_request_undo(db, actor, row):
    if row.status == "withdrawn":
        who = (row.details or {}).get("withdrawn_by", row.created_by)
        require(who == actor.identity_id, "FORBIDDEN", "Only the person who withdrew this request can undo it", 403)
        require(owns_request(actor, row) or may_decide(db, actor, row),
                "FORBIDDEN", "Your current ownership or decision authority no longer permits this withdrawal", 403)
    elif row.status == "submitted" and row.created_by == actor.identity_id:
        return
    else:
        require(may_decide(db, actor, row), "FORBIDDEN", "Your current role or programme no longer permits this decision", 403)
        if row.status in ("approved", "rejected"):
            require(row.decided_by == actor.identity_id, "FORBIDDEN", "Only the person who made this decision can undo it", 403)


def undo_decision(db, actor, command, *, batch=False):
    """Undo a request decision or withdrawal: it waits for a decision again. Only whoever made it may, only while the
    request is not linked to a rota (a rota may already act on it), and an approved leave's absence is retracted with it."""
    value = s.RequestUndo.model_validate(command.payload)
    row = scoped(db, m.RequestRecord, command.target_id, actor)
    require_request_undo(db, actor, row)
    require(row.status in ("approved", "rejected", "withdrawn"), "WORKFLOW_STATE", "This request is already waiting for a decision", 409)
    expected(row, command.expected_revision)
    was_approved = row.status == "approved"
    require(row.workspace_id is None, "REQUEST_UNDO_LINKED", "This request is linked to a rota; remove the link before undoing the decision", 409)
    if db.info.get("bulk") and row.status == "approved":
        db.flush()
    inputs_changed = False
    affected = 1
    retracted_availability = []
    overtime = row.kind == "overtime_availability"
    if overtime:
        overtime_availability.require_unlocked(db, actor, row)
        if row.status == "withdrawn" and row.overtime.get("withdrawn_from") == "approved":
            affected += overtime_availability.restore(db, actor, row, value.reason)
            row.status = "approved"
            row.decision_reason = row.overtime.get("previous_decision_reason")
            previous_at = row.overtime.get("previous_decided_at")
            if "previous_decided_at" not in row.overtime and row.decided_at is not None:
                E, R = m.AuditEvent, m.CommandReceipt
                events = list(db.execute(select(E, R).join(R, R.id == E.detail["receipt_id"].as_string()).where(
                    E.department_id == row.department_id, R.department_id == row.department_id, E.target_id == row.id,
                    E.action == "request.decide").order_by(E.created_at, E.seq)))
                groups, proven = {}, {}
                for event, receipt in events:
                    groups.setdefault(receipt.id, []).append((event, receipt))
                for group in groups.values():
                    result = group[0][1].result
                    replies = [result, *(reply for item in result.get("items", []) for reply in item.get("results", []))]
                    records = [item for reply in replies
                        if reply.get("authorization_command", {}).get("kind") in {None, "request.decide", "request.decide_many"}
                        for item in [reply.get("record"), *reply.get("records", [])]
                        if isinstance(item, dict) and item.get("id") == row.id and item.get("source", "request") == "request"]
                    # Tied or incomplete audit order cannot identify an operation within a multi-step receipt.
                    if len(records) == len(group) == len({aware(event.created_at) for event, _ in group}):
                        proven.update((event.id, state) for (event, _), state in zip(group, records))
                for event, _ in reversed(events):
                    state = proven.get(event.id, {})
                    if (aware(event.created_at) < aware(row.decided_at) and event.actor_id == row.decided_by
                            and event.reason == row.decision_reason and state.get("status") == "approved"
                            and state.get("revision", row.revision) <= row.revision):
                        previous_at = state.get("decided_at") or event.created_at
                        break
            row.decided_at = aware(previous_at) if previous_at is not None else None
            inputs_changed = True
        else:
            if row.status == "approved":
                retired = overtime_availability.retire(db, actor, row, value.reason)
                inputs_changed = bool(retired)
                affected += len(retired)
            row.status, row.decided_by, row.decision_reason = "submitted", None, None
        if inputs_changed and not batch:
            bump_inputs(db, actor, availability=True)
    elif row.status == "approved":
        if row.kind == "rotation_change":
            from .training import undo_change
            affected += undo_change(db, actor, row)
            inputs_changed = True
        for absence in db.scalars(select(m.Availability).where(m.Availability.request_id == row.id, m.Availability.retracted.is_(False))):
            absence.retracted = True
            absence.revision += 1
            retracted_availability.append({"id": absence.id, "revision": absence.revision})
            affected += 1
            inputs_changed = True
            if not batch:
                bump_inputs(db, actor, availability=True)
    if not overtime:
        row.status, row.decided_by, row.decision_reason = "submitted", None, None
    row.details = {key: value for key, value in (row.details or {}).items() if key != "withdrawn_by"}
    if row.status == "submitted":
        row.decided_at = None
    row.revision += 1
    outbox(db, actor, f"request-undo:{row.id}:{row.revision}", "request", {"request_id": row.id, "person_id": row.person_id,
           "request_status": row.status, "request_kind": row.kind},
           undo_of=f"request-decision:{row.id}" + (f":{command.expected_revision}" if command.expected_revision > 2 else ""))
    item = absence_report_record(row, actor, db)
    details = {"retracted_availability": retracted_availability} if row.kind in {"leave", "absence_report"} and was_approved else {}
    if batch:
        return {"record": item, "inputs_changed": inputs_changed, "affected_count": affected, **details}
    return {"record": leave_types.names(db, actor.department_id, [item])[0], "affected_count": affected, **details,
            "message": "Undone: the previous request state is restored." if overtime else "Undone: the request waits for a decision again."}


def link_request_rota(db, actor, command):
    require_role(actor, "coordinator")
    value = s.RequestRotaLink.model_validate(command.payload)
    row = scoped(db, m.RequestRecord, command.target_id, actor)
    expected(row, command.expected_revision)
    require(row.status == "approved", "WORKFLOW_STATE", "Approve the request before linking its rota", 409)
    if value.workspace_id:
        workspace = scoped(db, m.Workspace, value.workspace_id, actor)
        require(workspace.status != "superseded", "WORKFLOW_STATE", "Choose the current rota or its amendment", 409)
        zone = ZoneInfo(db.get(m.Organisation, actor.organisation_id).timezone)
        start = datetime.combine(workspace.starts_on, time.min, zone)
        end = datetime.combine(workspace.ends_on, time.min, zone)
        request_start = row.start.replace(tzinfo=timezone.utc) if row.start.tzinfo is None else row.start
        request_end = row.end.replace(tzinfo=timezone.utc) if row.end.tzinfo is None else row.end
        require(request_start < end and request_end > start, "REQUEST_ROTA_PERIOD", "The rota must overlap the requested period")
    row.workspace_id = value.workspace_id
    row.revision += 1
    # This is traceability, not approval or proof that the requested change was fulfilled.
    return {"record": leave_types.names(db, actor.department_id, [record(row, exclude=("private_reason",))])[0], "message": "Rota link saved. Review and publication remain separate."}


def list_requests(db, actor, status=None):
    require_role(actor, "staff", "coordinator", "approver", "programme_lead")
    query = select(m.RequestRecord, m.Workspace.status).outerjoin(m.Workspace, m.RequestRecord.workspace_id == m.Workspace.id).where(
        m.RequestRecord.department_id == actor.department_id)
    team = actor.permits("coordinator", "approver")
    if not team:
        from .training import change_person_scope, request_scope
        own = change_person_scope([actor.person_id]) if actor.person_id else false()
        query = query.where(or_(own, request_scope(db, actor)) if actor.permits("programme_lead") else own)
    if status:
        query = query.where(m.RequestRecord.status == status)
    result = []
    for row, rota_status in db.execute(query.order_by(_stage_order("request"), m.RequestRecord.created_at.desc(), m.RequestRecord.seq.desc()).limit(200)):
        item = absence_report_record(row, actor, db) if row.kind == "absence_report" else record(row)
        item["rota_status"] = rota_status
        # Staff may see progress on their request, never a colleague's draft or its identifier.
        if not team:
            item.pop("workspace_id", None)
        result.append(item)
    return {"requests": leave_types.names(db, actor.department_id, result)}


# ---- Requests page: one list over every kind of request (GET /api/requests/all) ----
# The one place a backend state becomes a Requests stage (the web client words each stage: terms.ts REQUEST_STAGES).
REQUEST_STAGE = {"submitted": "waiting", "approved": "approved", "rejected": "declined", "withdrawn": "withdrawn"}
PATTERN_STAGE = {"proposed": "waiting", "active": "approved", "declined": "declined", "ended": "withdrawn"}
SWAP_STAGE = {"offered": "reply", "counteroffered": "reply", "accepted": "accepted", "reviewing": "accepted",
              "published": "approved", "rejected": "declined", "withdrawn": "withdrawn",
              "expired": "expired", "stale": "expired", "access_revoked": "expired"}
WISH_STAGE = {"granted": "approved", "partly": "approved", "declined": "declined"}
LEAVE_KINDS = leave_types.LEAVE_KINDS
TYPES = ("leave", "weekly", "wish", "swap", "other")
STAGES = ("waiting", "reply", "accepted", "open", "approved", "declined", "withdrawn", "expired")


def requests_etag(db, actor, representation):
    """Revision-backed validator for the unified Requests projection.

    The list is assembled from several mutable tables.  A small aggregate stamp keeps
    conditional polling out of the page projection while still invalidating on edits,
    decisions, linked rota changes, audit history and an offer crossing its expiry.
    """
    from . import swaps
    department, organisation = db.execute(select(m.Department, m.Organisation).join(
        m.Organisation, m.Organisation.id == m.Department.organisation_id).where(
            m.Department.id == actor.department_id)).one()
    stamp = [department.revision, department.catalogue_revision, organisation.revision, organisation.input_revision,
             organisation.availability_revision]
    for cls in (m.RequestRecord, m.Availability, m.AvailabilityPattern, m.StaffWish,
                m.SchedulingFact, m.SwapRequest, m.Publication, m.Workspace):
        field = cls.department_id if hasattr(cls, "department_id") else None
        query = select(func.max(cls.revision), func.max(cls.created_at))
        if field is not None:
            query = query.where(field == actor.department_id)
        elif cls is m.Workspace:
            query = query.where(cls.department_id == actor.department_id)
        stamp.append(tuple(db.execute(query).one()))
    # Request history and displayed names are mutable evidence even when the source row
    # itself is unchanged.  These are deliberately broad and therefore only over-invalidate.
    stamp.append(tuple(db.execute(select(func.max(m.AuditEvent.revision), func.max(m.AuditEvent.created_at)).where(
        m.AuditEvent.department_id == actor.department_id)).one()))
    stamp.append(tuple(db.execute(select(func.max(m.Person.revision), func.max(m.Person.created_at)).where(
        m.Person.organisation_id == actor.organisation_id)).one()))
    now = clock_now()
    expiry_count, next_expiry = db.execute(select(
        func.count(m.SwapRequest.id).filter(m.SwapRequest.expires_at <= now),
        func.min(m.SwapRequest.expires_at).filter(m.SwapRequest.expires_at > now)
    ).where(m.SwapRequest.department_id == actor.department_id,
            m.SwapRequest.status.in_(tuple(swaps.OPEN_STATES)))).one()
    # The count advances whenever any open offer crosses its expiry; the next timestamp
    # catches the following transition without scanning or hashing every offer.
    stamp.append((expiry_count, json_value(next_expiry) if next_expiry is not None else None))
    # The Requests projection reads swap audience state from the durable offer
    # outbox payload; its revision changes when the worker freezes that payload.
    stamp.append(tuple(db.execute(select(func.count(), func.sum(m.Outbox.revision), func.max(m.Outbox.created_at))
        .where(m.Outbox.department_id == actor.department_id, m.Outbox.event_key.like('swap:%'))).one()))
    return revision_etag("requests", actor.department_id, actor.identity_id, actor.person_id,
                         sorted(actor.roles), representation, stamp)


def _recorded_details(db, actor):
    return actor.permits("coordinator", "approver") and db.get(m.Department, actor.department_id).leave_reason_display == "details"


def _stage_order(source):
    """The same stage order in SQL (before paging) and the merged list, across every request source."""
    if source == "scheduling_fact":
        stage = case(scheduling_facts.FACT_STAGES, value=m.SchedulingFact.status)
    elif source == "availability":
        stage = case((m.Availability.retracted.is_(True), "withdrawn"), else_="approved")
    elif source == "wish":
        stage = case((m.StaffWish.status == "withdrawn", "withdrawn"),
                     else_=case(WISH_STAGE, value=m.StaffWish.outcome, else_="open"))
    else:
        model, mapping = {"request": (m.RequestRecord, REQUEST_STAGE), "pattern": (m.AvailabilityPattern, PATTERN_STAGE),
                          "swap": (m.SwapRequest, SWAP_STAGE)}[source]
        stage = case(mapping, value=model.status, else_="expired")
        if source == "swap":
            from .swaps import OPEN_STATES
            stage = case((and_(model.status.in_(OPEN_STATES), model.expires_at <= m.now()), "expired"), else_=stage)
    return case({name: i for i, name in enumerate(STAGES)}, value=stage, else_=len(STAGES))


def _at(value):
    """Rows of every kind sort on one time line (dates as UTC midnight)."""
    return aware(value) if isinstance(value, datetime) else datetime.combine(value, time(), timezone.utc)


def _sources(db, actor, team, person_id, types, stages, q=None, person_ids=(), open_swap_ids=(), collection_filter=None,
             decided=False, personal=False):
    """Each kind of record behind the list: its query (scoped exactly like its own list endpoint), the date it sorts on,
    which rows wait for someone, and its key. Kinds a type or stage filter rules out are not read at all."""
    from . import swaps
    own = actor.person_id if personal or not team else None
    who = own if personal or not team else person_id
    want = lambda *keys: not types or bool(types & set(keys))
    stage_in = lambda mapping, column: true() if not stages else column.in_([k for k, v in mapping.items() if v in stages] or ["-"])
    # An unlinked staff account has no own rows; it must never fall through to the department-wide list.
    mine = lambda column: ([column == own] if personal or not team else []) + (
        [column == person_id] if person_id else []) + ([column.in_(person_ids)] if person_ids else [])
    term = (q or "").strip()
    search_options = collection_filter.search_options if collection_filter else None
    like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    named = select(m.Person.id).where(m.Person.display_name.ilike(like, escape="\\"))

    def matching(people, *columns):
        # Search never reads private reasons; disclosure settings cannot make them a row/count oracle.
        if search_options is not None:
            from .search import predicate
            from .bulk_selection import MODELS
            collection = next(key for key, model in MODELS.items() if model is people[0].class_)
            return [predicate(db, actor, collection, q, search_options, as_of=collection_filter.as_of,
                              columns=list(columns), people=people)]
        return [or_(*(person.in_(named) for person in people), *(column.ilike(like, escape="\\") for column in columns))] if term else []

    out = {}
    if want("other") and actor.permits(*scheduling_facts.READERS):
        F = m.SchedulingFact
        query = scheduling_facts.scoped_query(db, actor)
        query = query.add_columns(F.created_at.label("at")).where(stage_in(scheduling_facts.FACT_STAGES, F.status))
        if personal:
            query = query.where(F.person_id == own)
        if person_id:
            query = query.where(F.person_id == person_id)
        if person_ids:
            query = query.where(F.person_id.in_(person_ids))
        if search_options is not None:
            from .search import predicate
            query = query.where(predicate(db, actor, 'scheduling_facts', q, search_options,
                as_of=collection_filter.as_of, columns=[case(scheduling_facts.FACT_LABELS, value=F.kind)], people=(F.person_id,)))
        elif term:
            proposers = select(m.Identity.id).where(m.Identity.display_name.ilike(like, escape="\\"))
            calendars = select(m.ProgrammeCalendarEntry.id).where(m.ProgrammeCalendarEntry.name.ilike(like, escape="\\"))
            query = query.where(or_(F.person_id.in_(named), F.proposed_by.in_(proposers),
                                   F.calendar_entry_id.in_(calendars),
                                   case(scheduling_facts.FACT_LABELS, value=F.kind).ilike(like, escape="\\")))
        out["scheduling_fact"] = (query, F.created_at, F.status == "proposed", F.id)
    # Auditors read only this approval projection, never the private Requests of other people; programme leads also
    # read their trainees' requests (they decide them: may_decide).
    lead = actor.permits("programme_lead") and not team and not personal
    if not actor.permits("staff", "coordinator", "approver") and not lead:
        return _filter_sources(db, actor, out, collection_filter)
    type_name = lambda cls: select(m.LeaveType.name).where(m.LeaveType.id == cls.leave_type_id,
        m.LeaveType.department_id == actor.department_id).scalar_subquery()
    if want("leave", "other"):
        R = m.RequestRecord
        kind = true() if not types or {"leave", "other"} <= types else (R.kind == "leave") if "leave" in types else (R.kind != "leave")
        from .training import change_person_scope, request_scope
        scope = ([change_person_scope([own])] if personal or not team else [])
        if lead:
            scope = [or_(request_scope(db, actor), change_person_scope([own]) if own else false())]
        scope += ([change_person_scope([person_id])] if person_id else []) + (
            [change_person_scope(person_ids)] if person_ids else [])
        out["request"] = (select(R, R.start.label("at")).where(R.department_id == actor.department_id, kind,
                          stage_in(REQUEST_STAGE, R.status), *scope,
                          *matching((R.person_id, R.details["swap"]["person_id"].as_string()), type_name(R), func.replace(R.kind, "_", " "),
                                    case((R.kind == "leave", "leave"), else_="other"))), R.start, R.status == "submitted", R.id)
    if not actor.permits("staff", "coordinator", "approver"):
        return _filter_sources(db, actor, out, collection_filter)
    if want("leave", "weekly") and (not stages or stages & {"approved", "withdrawn"}):
        A = m.Availability
        kind = true() if not types or {"leave", "weekly"} <= types else A.kind.in_(LEAVE_KINDS) if "leave" in types else A.kind.not_in(LEAVE_KINDS)
        state = true() if not stages or {"approved", "withdrawn"} <= stages else A.retracted.is_("withdrawn" in stages)
        # Approved leave requests are listed as the request (which carries its record); these were recorded directly.
        out["availability"] = (select(A, A.start.label("at")).where(A.department_id == actor.department_id, A.request_id.is_(None),
                                visible_record_clause(A), kind, state, *mine(A.person_id),
                               *matching((A.person_id,), type_name(A), func.replace(A.kind, "_", " "),
                                         case((A.kind.in_(LEAVE_KINDS), "leave"), else_="weekly"))), A.start, false(), A.id)
    if want("weekly"):
        P = m.AvailabilityPattern
        out["pattern"] = (select(P, P.starts_on.label("at")).where(P.department_id == actor.department_id,
                          stage_in(PATTERN_STAGE, P.status), *mine(P.person_id),
                          *matching((P.person_id,), P.note, literal("weekly"))), P.starts_on, P.status == "proposed", P.id)
    if want("wish") and (team or own) and (not stages or stages & {"open", "approved", "declined", "withdrawn"}):
        W = m.StaffWish
        parts = []
        if not stages or "open" in stages:
            parts.append(and_(W.status == "open", W.outcome.is_(None)))
        if not stages or stages & {"approved", "declined"}:
            parts.append(and_(W.status == "open", stage_in(WISH_STAGE, W.outcome)))
        if not stages or "withdrawn" in stages:
            parts.append(W.status == "withdrawn")
        out["wish"] = (select(W, W.first_day.label("at")).where(W.department_id == actor.department_id, or_(*parts),
                       *mine(W.person_id), *matching((W.person_id,), W.note, func.replace(W.kind, "_", " "), literal("wish"))),
                       W.first_day, false(), W.id)
    reads_swaps = team or bool(own and "staff" in actor.roles and swaps.staff_identity(db, actor.identity_id, own, actor.department_id))
    if want("swap") and reads_swaps:
        S, first = m.SwapRequest, m.Assignment
        live = and_(S.status.in_(swaps.OPEN_STATES), S.expires_at > m.now())
        # "Expired" is decided per row (effective_status): its candidates are the open states.
        mapping = {**SWAP_STAGE, **({k: "expired" for k in swaps.OPEN_STATES} if stages and "expired" in stages else {})}
        participant = or_(S.proposer_person_id == who, S.recipient_person_id == who)
        public = and_(S.id.in_(open_swap_ids), swaps.unclaimed_open())
        both = [or_(participant, public) if personal or not team else participant] if who else []
        if person_id:
            both.append(or_(S.proposer_person_id == person_id, S.recipient_person_id == person_id))
        if person_ids:
            both.append(or_(S.proposer_person_id.in_(person_ids), S.recipient_person_id.in_(person_ids)))
        matches = matching((S.proposer_person_id, S.recipient_person_id), S.reason, literal("swap"))
        if term and not team:
            # Open offers disclose the proposer and offered duty, never the shared participant-only reason.
            matches = [or_(and_(participant, *matches), and_(public,
                *matching((S.proposer_person_id,), literal("swap"))))]
        later = aliased(m.SwapRequest)  # a swap an Undo reopened is listed once, as its newest row
        out["swap"] = (select(S, first.start.label("at")).join(first, first.id == S.first_assignment_id).where(
            S.department_id == actor.department_id, stage_in(mapping, S.status), *both,
            *matches,
            ~select(later.id).where(later.reopened_from_id == S.id).exists()), first.start, live, S.id)
    return _filter_sources(db, actor, out, collection_filter)


def _filter_sources(db, actor, sources, selected):
    if selected is None or selected.conditions is None:
        return sources
    from .condition_context import evaluation_context, required_tokens
    context = evaluation_context(db, actor, as_of=selected.as_of, table_key='requests',
                                 tokens=required_tokens(selected.conditions))
    fields = {'id', 'type', 'status', 'stage', 'source', 'person_id', 'person', 'start', 'end', 'proposed_at'}
    collection_filters.validate(selected.conditions, fields, context, table_key='requests')
    models = {'request': m.RequestRecord, 'availability': m.Availability, 'pattern': m.AvailabilityPattern,
              'wish': m.StaffWish, 'swap': m.SwapRequest, 'scheduling_fact': m.SchedulingFact}
    result = {}
    for source, (query, at, waiting, key) in sources.items():
        cls = models[source]
        people = ((cls.proposer_person_id, cls.recipient_person_id) if source == 'swap' else (cls.person_id,))
        if source == 'request':
            people += (cls.details["swap"]["person_id"].as_string(),)
        names = tuple(select(m.Person.display_name).where(m.Person.id == person).scalar_subquery() for person in people)
        stage = case({index: name for index, name in enumerate(STAGES)}, value=_stage_order(source), else_='expired')
        start = m.Assignment.start if source == 'swap' else getattr(cls, 'start', getattr(cls, 'starts_on', getattr(cls, 'first_day', at)))
        end = m.Assignment.end if source == 'swap' else getattr(cls, 'end', getattr(cls, 'ends_on', getattr(cls, 'last_day', at)))
        match = collection_filters.predicate(selected.conditions, {'id': key, 'type': _sort_value(db, source, at, 'type'),
            'status': stage, 'stage': stage, 'source': literal(source), 'person_id': people, 'person': (*people, *names),
            'start': start, 'end': end, 'proposed_at': getattr(cls, 'created_at', at)}, context, table_key='requests')
        result[source] = (query.where(match), at, waiting, key)
    return result


def _duty(db, assignment_id, publication_id):
    """A duty in a swap, read from its rows (not from the publication snapshot, which is megabytes on a big rota)."""
    a = db.get(m.Assignment, assignment_id) if assignment_id else None
    shift = db.get(m.Shift, a.shift_id) if a else None
    if not shift:
        return None
    location = db.get(m.Location, shift.location_id)
    return {"assignment_id": a.id, "person_id": a.person_id, "start": json_value(a.start), "end": json_value(a.end),
            "type": (shift.type_snapshot or {}).get("name", ""), "location": location.name if location else "",
            "publication_id": publication_id}


def _duty_cache(db, rows):
    """Load both offered duties for the page in one bounded join."""
    ids = {aid for row in rows for aid in (row.first_assignment_id, row.second_assignment_id) if aid}
    if not ids:
        return {}
    A, S, L = m.Assignment, m.Shift, m.Location
    return {aid: {"assignment_id": aid, "person_id": person_id, "start": json_value(start), "end": json_value(end),
                  "type": (snapshot or {}).get("name", ""), "location": location or ""}
            for aid, person_id, start, end, snapshot, location in db.execute(
                select(A.id, A.person_id, A.start, A.end, S.type_snapshot, L.name)
                .join(S, S.id == A.shift_id).outerjoin(L, L.id == S.location_id)
                .where(A.id.in_(ids)))}


def _rotation_names(db, rows):
    proposals = [(row.details or {}).get("proposed", {}) for row in rows if row.kind == "rotation_change"]
    return {kind: dict(db.execute(select(cls.id, cls.name).where(cls.id.in_(ids))).all()) if ids else {}
            for kind, cls, ids in (
                ("rotation_types", m.RotationType, {p.get("rotation_type_id") for p in proposals} - {None}),
                ("services", m.Service, {p.get("service_id") for p in proposals} - {None}))}


def _decidable(db, actor, rows):
    """Read decision eligibility once per source, including same-person sign-ins and current self-approval grants."""
    result = set()
    for source, cls, waiting in (("request", m.RequestRecord, "submitted"), ("pattern", m.AvailabilityPattern, "proposed")):
        ids = [row.id for kind, row in rows if kind == source]
        if ids:
            result.update(db.scalars(select(cls.id).where(cls.id.in_(ids), cls.department_id == actor.department_id,
                                                         cls.status == waiting, decision_scope(db, actor, cls))))
    return result


def absence_report_record(row, actor=None, db=None):
    item = record(row, exclude=('private_reason',))
    if row.kind == 'absence_report':
        details = {key: value for key, value in (row.details or {}).items()
                   if key not in {'reporter_category', 'note', 'document_reference'}}
        department = db.get(m.Department, actor.department_id) if actor and db else None
        wording = leave_wording(department, actor, row)
        item['details'] = {**details, **{key: wording[key] for key in ('absence_reason', 'note', 'document_reference')
                                         if key in wording}}
        item['absence_progress'] = ('waiting_for_approval' if row.status == 'submitted' else
            'finding_cover' if row.status == 'approved' else row.status)
    return item


def _project(db, actor, team, source, row, names, recorded_details=False, swap_cache=None, rotation_names=None,
             decidable=(), duty_cache=None, assignment_types=None):
    from . import swaps
    if source == "scheduling_fact":
        return scheduling_facts.approval_item(db, actor, row, names)
    if source == "request":
        # Staff see progress on their request, never the rota's identifier: _links reads the state, then drops the id.
        item = {**(absence_report_record(row, actor, db) if row.kind == "absence_report" else record(row)), "type": "leave" if row.kind == "leave" else "other", "stage": REQUEST_STAGE[row.status]}
        if row.kind == "absence_report":
            from .published_changes import own_absence_summary
            item.update(own_absence_summary(row, actor))
        if row.kind == "rotation_change":
            from .training import change_summary
            item["summary"] = change_summary(db, row, rotation_names)
    elif source == "availability":
        item = {**record(row, exclude=() if recorded_details else ("private_reason",)), "type": "leave" if row.kind in LEAVE_KINDS else "weekly",
                "stage": "withdrawn" if row.retracted else "approved", "created_by": row.recorded_by}
    elif source == "pattern":
        item = {**record(row), "type": "weekly", "stage": PATTERN_STAGE.get(row.status, "withdrawn")}
    elif source == "wish":
        from .wishes import wish_record
        item = {**wish_record(row, actor), "type": "wish", "stage": "withdrawn" if row.status == "withdrawn" else WISH_STAGE.get(row.outcome, "open"),
                "duty_type": assignment_types.get(row.assignment_type_id, "") if row.assignment_type_id else ""}
    elif swaps.public_offer(actor, row):
        item = {**swaps.projection(db, row, cache=swap_cache, actor=actor), "type": "swap", "stage": SWAP_STAGE[row.status],
                "person_id": row.proposer_person_id}
    else:
        state = swaps.effective_status(db, row)
        item = {**record(row, exclude=("expected_content_hash",)), "type": "swap", "stage": SWAP_STAGE.get(state, "expired"),
                "effective_status": state, "person_id": row.proposer_person_id,
                **(swap_cache["audience_states"] if swap_cache is not None else swaps.audience_states(db, [row])).get(
                    row.id, {"audience_pending": False, "audience_failed": False}),
                "proposer_name": names(row.proposer_person_id),
                "recipient_name": names(row.recipient_person_id) if row.recipient_person_id else None,
                "offered_assignment": ({**duty_cache[row.first_assignment_id], "publication_id": row.publication_id}
                                        if duty_cache is not None and row.first_assignment_id in duty_cache else _duty(db, row.first_assignment_id, row.publication_id)),
                "counteroffered_assignment": ({**duty_cache[row.second_assignment_id], "publication_id": row.publication_id}
                                               if duty_cache is not None and row.second_assignment_id in duty_cache else _duty(db, row.second_assignment_id, row.publication_id))}
        if not team:
            item.pop("workspace_id", None)
    item["source"] = source
    item["person_name"] = names(item["person_id"])
    if source in ("request", "pattern"):
        item.update(s.RequestDecisionAccess(id=row.id, revision=row.revision, source=source,
                                            can_decide=row.id in decidable).model_dump())
    return item


def _links(db, team, items):
    """Each request's linked rota state (everyone: staff follow their own request's progress, but the rota's identifier
    is dropped for them) and, for reviewers, each approved request's absence record (a coordinator may retract it),
    read once for the whole page."""
    requests = [i for i in items if i["source"] == "request"]
    if not requests:
        return
    rotas = {i["workspace_id"] for i in requests if i.get("workspace_id")}
    states = dict(db.execute(select(m.Workspace.id, m.Workspace.status).where(m.Workspace.id.in_(rotas))).all()) if rotas else {}
    for i in requests:
        i["rota_status"] = states.get(i.get("workspace_id"))
        if not team:
            i.pop("workspace_id", None)
    if not team:
        return
    approved = [i["id"] for i in requests if i["stage"] == "approved"]
    # The current record wins over one an undone approval retracted.
    absences = {r.request_id: r for r in db.scalars(select(m.Availability).where(m.Availability.request_id.in_(approved))
                .order_by(m.Availability.retracted.desc(), m.Availability.created_at, m.Availability.seq))} if approved else {}
    for i in requests:
        if absence := absences.get(i["id"]):
            i.update(record_id=absence.id, record_revision=absence.revision, record_retracted=absence.retracted)


def _settled(db, actor, items):
    """Open wishes on the days of a submitted (or later) rota can no longer change (wishes.locked, read once)."""
    from . import wishes
    open_wishes = [i for i in items if i["source"] == "wish" and i["stage"] == "open"]
    closed = db.execute(select(m.Workspace.starts_on, m.Workspace.ends_on, m.Workspace.status).where(
        m.Workspace.department_id == actor.department_id, m.Workspace.archived_at.is_(None),
        m.Workspace.status.in_(wishes.SETTLED))).all() if open_wishes else []
    for i in open_wishes:
        repeat = i.get('repeat')
        last = (repeat.get('ends_on') or date.max.isoformat()) if repeat else i['last_day']
        i["locked"] = any(a.isoformat() <= last and i["first_day"] < b.isoformat() and
                          (not repeat or status in ('submitted', 'approved')) for a, b, status in closed)


def impact_duties(department_id, start, end):
    """Active participant duties overlapping a half-open span, shared by Requests impact and Home."""
    A, S, W = m.Assignment, m.Shift, m.Workspace
    return select(A.id).join(S, S.id == A.shift_id).join(W, W.id == S.workspace_id).where(
        W.department_id == department_id, W.archived_at.is_(None), S.cancelled.is_(False),
        A.start < end, A.end > start)


def _impact(db, actor, items):
    """Future overlapping duties: published places retain `impact`; team reviewers also see current draft/review rotas."""
    from .availability_patterns import occurrences
    from .schedule import local_midnight
    want = [i for i in items if i["source"] in ("request", "availability", "pattern") and i["stage"] in ("waiting", "approved")]
    for i in want:
        i.update(impact=[], impact_count=0, schedule_impact={"count": 0, "published_count": 0, "draft_count": 0, "duties": []})
    want = [i for i in want if i.get('counts_as_unavailable', True)]
    if not want:
        return
    team = actor.permits("coordinator", "approver")
    current = or_(m.Publication.current.is_(True), m.Workspace.status.in_(('draft', 'submitted', 'approved'))) if team else m.Publication.current.is_(True)
    horizon = db.scalar(select(func.max(m.Workspace.ends_on)).outerjoin(m.Publication, m.Publication.workspace_id == m.Workspace.id).where(
        m.Workspace.department_id == actor.department_id, m.Workspace.archived_at.is_(None), current))
    if not horizon:
        return
    zone = db.get(m.Organisation, actor.organisation_id).timezone
    tz, now = ZoneInfo(zone), m.now()
    end_of_rotas = datetime.combine(horizon, time(), tz)
    spans = {}
    for i in want:
        if i["source"] == "pattern":
            row = db.get(m.AvailabilityPattern, i["id"])
            spans[i["id"]] = [(a, b) for a, b, _ in occurrences(
                row, now.astimezone(tz).date(), horizon, tz, bounds=(now, end_of_rotas)) if b > now]
        else:
            a, b = aware(i["start"]), aware(i["end"])
            spans[i["id"]] = [(max(a, now), b)] if b > now and a < end_of_rotas else []
    every = [s for v in spans.values() for s in v]
    if not every:
        return
    lo, hi = min(a for a, _ in every), max(b for _, b in every)
    duties_query = (impact_duties(actor.department_id, lo, hi).with_only_columns(
                             m.Assignment.person_id, m.Assignment.start, m.Assignment.end, m.Shift.type_snapshot["name"].as_string(),
                             m.Publication.id, m.Publication.effective_until, m.Assignment.id, m.Workspace.id, m.Workspace.name, m.Workspace.status)
                      .outerjoin(m.Publication, m.Publication.workspace_id == m.Workspace.id)
                       .where(current, m.Assignment.person_id.in_({i["person_id"] for i in want}))
                       .order_by(m.Assignment.start, m.Assignment.id))
    inactive_ids = inactive_series_shift_ids(db, duties_query.with_only_columns(m.Shift.id, maintain_column_froms=True))
    if inactive_ids:
        duties_query = duties_query.where(m.Shift.id.not_in(inactive_ids))
    rows = db.execute(duties_query).all()
    duties = [(p, aware(a), aware(b), name, pub, aid, wid, neutral_rota_name(rota), status) for p, a, b, name, pub, until, aid, wid, rota, status in rows
              if not until or aware(a) < local_midnight(until, zone)]
    duties_by_person = {}
    for duty in duties:
        duties_by_person.setdefault(duty[0], []).append(duty)
    for i in want:
        hits = [(a, b, name, pub, aid, wid, rota, status)
                for _, a, b, name, pub, aid, wid, rota, status in duties_by_person.get(i["person_id"], ())
                if any(x < b and a < y for x, y in spans[i["id"]])]
        published = [hit for hit in hits if hit[3]]
        i["impact"] = [{"day": a.astimezone(tz).date().isoformat(), "type": name or "Duty", "publication_id": pub}
                       for a, _, name, pub, *_ in published[:10]]
        i["impact_count"] = len(published)
        i["schedule_impact"] = {"count": len(hits), "published_count": len(published), "draft_count": len(hits) - len(published),
            "duties": [{"assignment_id": aid, "day": a.astimezone(tz).date().isoformat(), "start": json_value(a), "end": json_value(b),
                        "type": name or "Duty", "status": status, **({"publication_id": pub} if pub else {}),
                        **({"workspace_id": wid, "rota_name": rota} if team else {})}
                       for a, b, name, pub, aid, wid, rota, status in hits[:10]]}


def not_own(cls, actor):
    """Independent decisions (SSOT for the Requests dot and Home): a request or weekly availability this person neither
    made nor is about. Deciding one's own takes a self-approval permission, which these counts do not offer."""
    peer_scope = ()
    if cls is m.RequestRecord and actor.person_id:
        peer = cls.details["swap"]["person_id"].as_string()
        peer_scope = (or_(peer.is_(None), peer != actor.person_id),)
    return (cls.created_by != actor.identity_id, *peer_scope,
            *([cls.person_id != actor.person_id, cls.created_by.not_in(select(m.Identity.id).where(
                m.Identity.person_id == actor.person_id))] if actor.person_id else []))


def pending_request_decisions(db, actor):
    """Independent request decisions for both Home and Requests; self-approval stays an explicit command."""
    R = m.RequestRecord
    return select(R).where(R.department_id == actor.department_id, R.status == "submitted",
        decision_scope(db, actor, R, include_independence=False), *not_own(R, actor))


def _pending_queries(db, actor, open_swap_ids=None):
    """The source queries behind Requests' waiting-for-you count and filtered list."""
    from . import swaps
    F, R, P, S = m.SchedulingFact, m.RequestRecord, m.AvailabilityPattern, m.SwapRequest
    pending = [
        ("scheduling_fact", scheduling_facts.pending_query(db, actor, decidable=True), F.id),
        ("request", pending_request_decisions(db, actor), R.id),
    ]
    if actor.permits("coordinator"):
        pending.extend((
            ("pattern", select(P).where(P.department_id == actor.department_id, P.status == "proposed", *not_own(P, actor)), P.id),
            ("swap", select(S).where(S.department_id == actor.department_id, S.status == "accepted", S.expires_at > m.now()), S.id),
        ))
    if actor.person_id and "staff" in actor.roles:
        if open_swap_ids is None:
            open_swap_ids = swaps.visible_open_ids(db, actor)
        pending.append(("swap", select(S).where(S.department_id == actor.department_id, S.expires_at > m.now(), or_(
            and_(S.status == "offered", S.recipient_person_id == actor.person_id),
            and_(S.status == "counteroffered", S.proposer_person_id == actor.person_id),
            and_(S.id.in_(open_swap_ids), swaps.unclaimed_open()))), S.id))
    return pending


def _for_me_sources(db, actor, sources, open_swap_ids=None):
    """Restrict existing list sources to the exact rows used by ``_counts``."""
    by_source = {}
    for source, query, key in _pending_queries(db, actor, open_swap_ids):
        by_source.setdefault(source, []).append(query.with_only_columns(key, maintain_column_froms=True))
    result = {}
    for source, (query, at, waiting, key) in sources.items():
        pending = by_source.get(source)
        if pending:
            result[source] = (query.where(or_(*(key.in_(part) for part in pending))), at, waiting, key)
    return result


def _counts(db, actor, open_swap_ids=None):
    """What waits on this person to act (the Requests dot and Home read it)."""
    pending = [query.with_only_columns(key, maintain_column_froms=True)
               for _, query, key in _pending_queries(db, actor, open_swap_ids)]
    pending_union = pending[0].union_all(*pending[1:]).subquery("pending_requests")
    n = db.scalar(select(func.count()).select_from(pending_union)) or 0
    return {"to_decide": n}


def _stage_counts(db, actor, team, person_id, types, q=None, person_ids=(), open_swap_ids=(), collection_filter=None,
                  decided=False, personal=False, for_me=False):
    """How many requests are in each stage (the Requests views picker): one grouped count per kind, under the same
    scope, search and type filter as the list, whatever stage is shown."""
    from . import swaps
    out, now = dict.fromkeys(STAGES, 0), m.now()
    sources = _sources(db, actor, team, person_id, set(types), set(), q, person_ids, open_swap_ids, collection_filter,
                       decided, personal)
    if for_me:
        sources = _for_me_sources(db, actor, sources, open_swap_ids)
    projected = []
    for source, (query, *_) in (_decided_sources(sources) if decided else sources).items():
        if source == "request":
            stage = case(REQUEST_STAGE, value=m.RequestRecord.status, else_="expired")
        elif source == "availability":
            stage = case((m.Availability.retracted.is_(True), "withdrawn"), else_="approved")
        elif source == "pattern":
            stage = case(PATTERN_STAGE, value=m.AvailabilityPattern.status, else_="withdrawn")
        elif source == "wish":
            stage = case((m.StaffWish.status == "withdrawn", "withdrawn"),
                         else_=case(WISH_STAGE, value=m.StaffWish.outcome, else_="open"))
        elif source == "scheduling_fact":
            stage = case(scheduling_facts.FACT_STAGES, value=m.SchedulingFact.status, else_="withdrawn")
        else:
            stage = case((and_(m.SwapRequest.status.in_(swaps.OPEN_STATES), m.SwapRequest.expires_at <= now), "expired"),
                         else_=case(SWAP_STAGE, value=m.SwapRequest.status, else_="expired"))
        projected.append(query.with_only_columns(stage.label("stage"), maintain_column_froms=True))
    if projected:
        grouped = projected[0].union_all(*projected[1:]).subquery("request_stage_counts")
        for stage, n in db.execute(select(grouped.c.stage, func.count()).group_by(grouped.c.stage)):
            if stage in out:
                out[stage] += n
    return out


def _wish_points(db, actor):
    """Wishes are on (None: off): the budget, and for staff their points used in today's period and catch-up multiplier."""
    from . import wishes
    settings = wishes.scoring(db, actor.department_id)
    if not settings.wish_budget_points:
        return None
    out = {"budget_points": settings.wish_budget_points, "period": settings.wish_period}
    if not actor.person_id or "staff" not in actor.roles:
        return out
    organisation = db.get(m.Organisation, actor.organisation_id)
    start, end = wishes.period_of(clock_now().astimezone(ZoneInfo(organisation.timezone)).date(), settings, organisation.week_start)
    used = wishes.usage(db, actor.department_id, actor.person_id, start, end, settings, organisation.week_start)
    return {**out, "start": start.isoformat(), "end": end.isoformat(), "used": used[0]["used"] if used else 0,
            "multiplier": wishes.multipliers(db, actor.department_id, {actor.person_id}, settings)[actor.person_id]}


def _sort_value(db, source, at, sort_by):
    """Sortable persisted facts only; impact and formatted workflow details are page-only projections."""
    if sort_by == "decided":
        return _decision_time(source)
    if sort_by == "when":
        return m.SchedulingFact.starts_on if source == "scheduling_fact" else at
    if sort_by == "proposed_at":
        return at
    if sort_by == "type":
        if source == "request":
            return case((m.RequestRecord.kind == "leave", "leave"), else_="other")
        if source == "availability":
            return case((m.Availability.kind.in_(LEAVE_KINDS), "leave"), else_="weekly")
        return literal("weekly" if source == "pattern" else "other" if source == "scheduling_fact" else source)
    person = {"request": m.RequestRecord.person_id, "availability": m.Availability.person_id,
              "pattern": m.AvailabilityPattern.person_id, "wish": m.StaffWish.person_id,
              "swap": m.SwapRequest.proposer_person_id, "scheduling_fact": m.SchedulingFact.person_id}[source]
    name = select(collection_filters._unicode_lower(m.Person.display_name)).where(m.Person.id == person).scalar_subquery()
    # Return this SQL-normalized key with the row. Binary order agrees with Python's merge order, even for
    # non-ASCII names; the database's default locale and the browser's natural comparison need not agree.
    return func.coalesce(name, "").collate("C" if db.get_bind().dialect.name == "postgresql" else "BINARY")


def _decision_time(source):
    """The settled state has its event time; unknown legacy evidence stays unknown."""
    if source in {"request", "pattern"}:
        return (m.RequestRecord if source == "request" else m.AvailabilityPattern).decided_at
    empty = literal(None, type_=m.RequestRecord.decided_at.type)
    if source == "scheduling_fact":
        F, E = m.SchedulingFact, m.AuditEvent
        event = select(func.max(E.created_at)).where(E.department_id == F.department_id,
            E.target_id == F.id).correlate(F).scalar_subquery()
        return case((F.status != "proposed", event), else_=empty)
    if source == "swap":
        S, E = m.SwapRequest, m.SwapEvent
        event = select(func.max(E.created_at)).where(E.swap_id == S.id,
            E.action.in_(("published", "reject", "withdraw"))).correlate(S).scalar_subquery()
        undone = select(func.max(E.created_at)).where(E.swap_id == S.id, E.action == "undo").correlate(S).scalar_subquery()
        event = case((S.status == "withdrawn", func.coalesce(event, undone)), else_=event)
        return case((S.status.in_(("published", "rejected", "withdrawn")), event),
                    (and_(S.status.in_(("offered", "counteroffered", "accepted", "reviewing")), S.expires_at <= m.now()), S.expires_at),
                    else_=empty)
    cls = m.Availability if source == "availability" else m.StaffWish
    E = m.AuditEvent
    actions = ("availability.retract",) if source == "availability" else ("wish.withdraw",)
    event = select(func.max(E.created_at)).where(E.department_id == cls.department_id, E.target_id == cls.id,
        E.action.in_(actions)).correlate(cls).scalar_subquery()
    if source == "availability":
        return case((cls.retracted.is_(True), event), else_=cls.created_at)
    published = select(m.Publication.created_at).where(m.Publication.id == cls.publication_id).correlate(cls).scalar_subquery()
    return case((cls.status == "withdrawn", event), (cls.outcome.is_not(None), published), else_=empty)


def _self_approved(db, department_id, ids):
    """The requests whose latest decision was the person's own, under a self-approval permission (the audit's mark)."""
    latest = {}
    if ids:
        for target, detail in db.execute(select(m.AuditEvent.target_id, m.AuditEvent.detail).where(
                m.AuditEvent.department_id == department_id, m.AuditEvent.target_id.in_(ids),
                m.AuditEvent.action.like("%.decide")).order_by(m.AuditEvent.created_at, m.AuditEvent.seq)):
            latest[target] = bool((detail or {}).get("self_approved"))
    return {target for target, own in latest.items() if own}


def _decision_times(db, rows):
    """One limited read per source, rather than one event lookup for each page item."""
    result = {}
    for source in {source for source, _ in rows}:
        selected = [row for kind, row in rows if kind == source]
        if source in {"request", "pattern"}:
            result.update(((source, row.id), json_value(getattr(row, "decided_at", None))) for row in selected)
            continue
        cls = type(selected[0])
        result.update(((source, key), json_value(at)) for key, at in db.execute(
            select(cls.id, _decision_time(source)).where(cls.id.in_([row.id for row in selected]))))
    return result


def _decided_sources(sources):
    """Only explicit settled decisions; recording an absence is not a request decision."""
    result = {}
    for source, (query, at, stage, key) in sources.items():
        if source == 'availability':
            continue
        if source == 'scheduling_fact':
            query = query.where(m.SchedulingFact.status != "proposed")
        query = query.where(_decision_time(source).is_not(None))
        if source in {'request', 'pattern'}:
            query = query.where((m.RequestRecord if source == 'request' else m.AvailabilityPattern).decided_by.is_not(None))
        result[source] = query, at, stage, key
    return result


def list_all(db, actor, types=(), stages=(), person_id=None, limit=100, offset=0, item_id=None, q=None, person_ids=(), *,
             include_details=True, sort_by=None, sort_direction="asc", collection_filter=None, decided=False, personal=False,
             for_me=False):
    """Every kind of request in one list: stage first, soonest waiting/actionable then newest settled, paged. Each kind
    is read only as far as the page needs, so years of history cost no more than the page shown.
    With `item_id`: that one request and its history (the side panel).
    An explicit sort orders the complete authorized history before paging, overriding that default order.
    Home uses `personal=True` for own rows and visible open offers, independently of team decision authority, and
    `include_details=False` for the same page items without unused counts, impact or workflow enrichment."""
    from . import swaps
    require_role(actor, "staff", "coordinator", "approver", "programme_lead", "auditor")
    team = actor.permits("coordinator", "approver")
    recorded_details = _recorded_details(db, actor)
    people = {}

    def names(pid):
        if pid not in people:
            people[pid] = getattr(db.get(m.Person, pid), "display_name", "")
        return people[pid]
    open_swap_ids = swaps.visible_open_ids(db, actor)
    sources = _sources(db, actor, team, person_id, set(types), set(stages), q, person_ids, open_swap_ids, collection_filter,
                       decided, personal)
    if decided:
        sources = _decided_sources(sources)
    if item_id:
        # A durable fact link uses the same visibility boundary after it leaves the waiting queue.
        F = m.SchedulingFact
        sources["scheduling_fact"] = (scheduling_facts.scoped_query(db, actor).add_columns(F.created_at.label("at")),
                                      F.created_at, F.status == "proposed", F.id)
    if for_me:
        sources = _for_me_sources(db, actor, sources, open_swap_ids)
    aliases = {"start": "when", "decided_at": "decided"}
    sorting = [(aliases.get(key, key), direction) for key, direction in collection_filters.sort_keys(
        collection_filter, {"type", "person", "when", "proposed_at", "start", "decided", "decided_at"})]
    sorting = sorting or ([(sort_by, sort_direction)] if sort_by else [])
    # Swaps read their publications' flags (current, effective until), never the snapshot: load those flags first.
    held = list(db.scalars(select(m.Publication).options(defer(m.Publication.snapshot), defer(m.Publication.validation)).where(
        m.Publication.department_id == actor.department_id, m.Publication.current.is_(True)))) if "swap" in sources else []
    if item_id:
        for source, (query, _, _, key) in sources.items():
            lookup_id = swaps.latest_thread_id(db, actor, item_id) if source == "swap" else item_id
            row = db.execute(query.where(key == lookup_id)).first()
            if row:
                rotation_names = _rotation_names(db, [row[0]]) if source == "request" else None
                decidable = _decidable(db, actor, [(source, row[0])])
                assignment_types = {}
                if source == "wish" and row[0].assignment_type_id:
                    assignment_types[row[0].assignment_type_id] = getattr(
                        db.get(m.AssignmentType, row[0].assignment_type_id), "name", "")
                item = _project(db, actor, team, source, row[0], names, recorded_details,
                                rotation_names=rotation_names, decidable=decidable,
                                duty_cache=_duty_cache(db, [row[0]]) if source == "swap" else None,
                                assignment_types=assignment_types)
                item["decided_at"] = _decision_times(db, [(source, row[0])])[(source, row[0].id)]
                if source in ("request", "pattern") and row[0].id in _self_approved(
                        db, actor.department_id, [row[0].id] if getattr(row[0], "decided_by", None) else []):
                    item["self_approved"] = True
                if source in ('request', 'availability'):
                    leave_types.names(db, actor.department_id, [item])
                _impact(db, actor, [item])
                _settled(db, actor, [item])
                _links(db, team, [item])
                history = [] if source == "swap" and swaps.public_offer(actor, row[0]) else _history(db, actor, source, row[0], item)
                return {"item": item, "history": history,
                        **({"fact": record(row[0])} if source == "scheduling_fact" else {})}
        require(False, "NOT_FOUND", "Request not found", 404)
    total, default, ordered = 0, [], []
    total_queries = []
    for source, (query, at, _, key) in sources.items():
        if include_details:
            total_queries.append(query.with_only_columns(key.label("request_id"), maintain_column_froms=True))
        if sorting:
            values = [_sort_value(db, source, at, field) for field, _ in sorting]
            for row, _, *keys in db.execute(query.add_columns(*(value.label(f"sort_{index}") for index, value in enumerate(values)))
                    .order_by(*((value.desc() if direction == "desc" else value.asc()).nulls_last()
                                for value, (_, direction) in zip(values, sorting)), key).limit(offset + limit)):
                ordered.append((source, row, tuple(_at(value) if value is not None and field in {"when", "decided"} else value
                                                  for value, (field, _) in zip(keys, sorting))))
            continue
        stage = _stage_order(source)
        default.extend((source, row, _at(at), rank) for row, at, rank in db.execute(
            query.add_columns(stage.label("stage_order")).order_by(stage, case((stage < STAGES.index("approved"), at)),
                case((stage >= STAGES.index("approved"), at)).desc(), key).limit(offset + limit)))
    if total_queries:
        all_requests = total_queries[0].union_all(*total_queries[1:]).subquery("all_requests")
        total = db.scalar(select(func.count()).select_from(all_requests)) or 0
    if sorting:
        # Ties always use source then ID ascending, also for descending sorts. Each source's SQL limit uses
        # the same ID direction, so a boundary cannot drop a row that belongs on the merged page.
        ordered.sort(key=lambda x: (x[0], x[1].id))
        for index, (_, direction) in reversed(list(enumerate(sorting))):
            ordered.sort(key=lambda x: x[2][index] if x[2][index] is not None else datetime.min.replace(tzinfo=timezone.utc),
                         reverse=direction == "desc")
            ordered.sort(key=lambda x: x[2][index] is None)
    if not sorting:
        default.sort(key=lambda x: (x[3], x[2].timestamp() if x[3] < STAGES.index("approved") else -x[2].timestamp(), x[0], x[1].id))
        ordered = [(source, row, at) for source, row, at, _ in default]
    shown = ordered[offset:offset + limit]
    # Everyone named on the page, in one read.
    named = {pid for _, row, _ in shown for pid in (getattr(row, "person_id", None), getattr(row, "proposer_person_id", None),
                                                     getattr(row, "recipient_person_id", None)) if pid}
    people.update(db.execute(select(m.Person.id, m.Person.display_name).where(m.Person.id.in_(named))).all() if named else [])
    swap_rows = [row for source, row, _ in shown if source == "swap"]
    public_swaps = [row for row in swap_rows if swaps.public_offer(actor, row)]
    swap_cache = swaps.list_cache(db, actor, public_swaps) if public_swaps else {"audience_states": {}}
    swap_cache["audience_states"].update(swaps.audience_states(db, [row for row in swap_rows if not swaps.public_offer(actor, row)]))
    duty_cache = _duty_cache(db, swap_rows)
    type_ids = {row.assignment_type_id for source, row, _ in shown if source == "wish" and row.assignment_type_id}
    assignment_types = dict(db.execute(select(m.AssignmentType.id, m.AssignmentType.name).where(
        m.AssignmentType.id.in_(type_ids))).all()) if type_ids else {}
    rotation_names = _rotation_names(db, [row for source, row, _ in shown if source == "request"])
    decidable = _decidable(db, actor, [(source, row) for source, row, _ in shown])
    items = [_project(db, actor, team, source, row, names, recorded_details, swap_cache,
                      rotation_names, decidable, duty_cache, assignment_types) for source, row, _ in shown]
    decisions = _decision_times(db, [(source, row) for source, row, _ in shown])
    own = _self_approved(db, actor.department_id, [row.id for source, row, _ in shown if source in ("request", "pattern")
                                                   and getattr(row, "decided_by", None)])
    for item in items:
        item["decided_at"] = decisions[(item["source"], item["id"])]
        if item["id"] in own:  # only when true: every other projection of a request item stays as it was
            item["self_approved"] = True
    leave_types.names(db, actor.department_id, [i for i in items if i['source'] in ('request', 'availability')])
    if stages:  # a swap whose offer lapsed since it was listed as waiting
        items = [i for i in items if i["stage"] in stages]
    if not include_details:
        return {"items": items}
    _impact(db, actor, items)
    _settled(db, actor, items)
    _links(db, team, items)
    del held
    return {"items": items, "total": total, "limit": limit, "offset": offset, "has_more": offset + limit < total,
            "counts": {**_counts(db, actor, open_swap_ids),
                       "stages": _stage_counts(db, actor, team, person_id, types, q, person_ids, open_swap_ids,
                                               collection_filter, decided, personal, for_me)},
            "wishes": _wish_points(db, actor)}


def _history(db, actor, source, row, item):
    """Who did what to one request, oldest first: its creation, then each recorded decision with its reason."""
    who = {}

    def by(identity):
        if identity and identity not in who:
            who[identity] = getattr(db.get(m.Identity, identity), "display_name", "")
        return who.get(identity, "")
    if source == "swap":  # a swap records its own events, the offer first; a reopened swap continues the thread of the one before
        from . import swaps
        return [{"action": "swap." + e.action, "at": json_value(e.created_at), "actor": by(e.actor_id), "reason": e.reason}
                for e in swaps.events_for(db, actor, row, thread=True)]
    events = [{"action": "created", "at": json_value(row.created_at),
               "actor": by(item.get("created_by", item.get("proposed_by"))), "reason": ""}]
    targets = [row.id, *([item["record_id"]] if item.get("record_id") else [])]
    return events + [{"action": e.action, "at": json_value(e.created_at), "actor": by(e.actor_id), "reason": "" if e.reason == e.action else e.reason,
                      **({"self_approved": True} if (e.detail or {}).get("self_approved") else {})}
                     # A refused command changed nothing: it stays in the audit trail, not in what was done to the request.
                     for e in db.scalars(select(m.AuditEvent).where(m.AuditEvent.department_id == row.department_id, m.AuditEvent.target_id.in_(targets),
                                                                    m.AuditEvent.action != 'command.refused')
                                         .order_by(m.AuditEvent.created_at, m.AuditEvent.seq))]
