"""How leave shows on planning findings: the one place an absence's type, dates and note reach the browser.

The validator, solver and snapshots never carry the note (snapshots exclude it); they only say UNAVAILABLE. Responses
that list planning findings pass through `disclose_leave`, which adds `leave` to each UNAVAILABLE item as the
department's `leave_reason_display` allows:
    none    -> nothing is added (the reason stays private)
    type    -> {type, starts_on, ends_on}
    details -> {type, starts_on, ends_on, note}
and only for roles that read the department's planning findings (staff never get it). Other departments' absences
still block duties, but this department's setting cannot disclose their details. Findings, scores and fingerprints
are unchanged: this is presentation of data the reader is already allowed to plan with."""
from datetime import timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from sqlalchemy import select
from apps.api import models as m
from apps.api.db import current_request_parent
from packages.contracts.schemas import ABSENCE_CATEGORIES, ABSENCE_CATEGORY_LABELS, ABSENCE_REPORT_LABEL, TEAM_READER_ROLES
from packages.domain.time import aware
from . import leave_types
from .leave_types import KIND_LABELS

PLANNING_ROLES = TEAM_READER_ROLES
LEAVE_CODES = {'UNAVAILABLE', 'EXTERNAL_ROTATION', 'TIME_OVERLAP'}


def canonical_absence_category(value):
    """Return the contract spelling for a stored category, or None for legacy free text."""
    if not isinstance(value, str):
        return None
    folded = value.casefold()
    return next((category for category in ABSENCE_CATEGORIES if category.casefold() == folded), None)


def display(mode, name, start, end, note, zone):
    if mode == 'none':
        return None
    return {'type': name, 'starts_on': start.astimezone(zone).date().isoformat(),
            'ends_on': (end - timedelta(microseconds=1)).astimezone(zone).date().isoformat(),
            **({'note': note} if mode == 'details' and note else {})}


def with_leave(item, value):
    """Set a disclosed leave value, omitting the key when disclosure is disabled."""
    if value is None:
        item.pop('leave', None)
    else:
        item['leave'] = value
    return item


def sanitize_leave_details(body, mode, can_disclose):
    """Apply the leave disclosure shape to leave-bearing nodes and their details."""
    fields = {'type', 'starts_on', 'ends_on'} | ({'note'} if mode == 'details' else set())

    def walk(node, detail=False):
        if isinstance(node, list):
            for child in node:
                walk(child, detail)
        elif isinstance(node, dict):
            leave_node = detail or node.get('code') in LEAVE_CODES or 'availability_id' in node
            if leave_node and 'leave' in node:
                shown = node['leave']
                if not can_disclose or not isinstance(shown, dict):
                    with_leave(node, None)
                else:
                    with_leave(node, {key: shown[key] for key in fields if key in shown} or None)
            for key, child in node.items():
                walk(child, leave_node and key == 'detail')

    walk(body)
    return body


def disclose_leave(db, actor, body):
    """Adds `leave` to every UNAVAILABLE item in `body` (in place; returns it). An item's person and time come from
    itself or the nearest enclosing object (a Why row's person, an open place's session)."""
    department = current_request_parent(db, m.Department, actor.department_id)
    mode = department.leave_reason_display if department else 'none'
    sanitize_leave_details(body, mode, bool(actor.permits(*PLANNING_ROLES) and mode != 'none'))
    if not actor.permits(*PLANNING_ROLES):
        return body
    items = []

    def walk(node, person, start, end):
        if isinstance(node, list):
            for child in node:
                walk(child, person, start, end)
            return
        if not isinstance(node, dict):
            return
        person, start, end = node.get('person_id') or person, node.get('start') or start, node.get('end') or end
        if node.get('code') == 'UNAVAILABLE' and person and start and end:
            items.append((node, person, aware(start), aware(end)))
            if 'other_shift_id' in node:
                return  # Clash metadata and cancel commands contain no nested findings.
        for child in node.values():
            if isinstance(child, (dict, list)):
                walk(child, person, start, end)
    walk(body, None, None, None)
    if not items:
        return body
    people = {person for _, person, _, _ in items}
    lo, hi = min(a for *_, a, _ in items), max(b for *_, b in items)
    availability = list(db.scalars(select(m.Availability).where(m.Availability.department_id == actor.department_id,
                                                           m.Availability.person_id.in_(people), m.Availability.start < hi,
                                                           m.Availability.end > lo, m.Availability.retracted.is_(False),
                                                           m.Availability.counts_as_unavailable.is_(True))))
    report_ids = {row.request_id for row in availability if row.request_id}
    reports = {row.id: row for row in db.scalars(select(m.RequestRecord).where(
        m.RequestRecord.department_id == actor.department_id, m.RequestRecord.id.in_(report_ids),
        m.RequestRecord.kind == 'absence_report'))} if report_ids else {}
    records = [dict(id=r.id, person_id=r.person_id, start=aware(r.start), end=aware(r.end), leave_type_id=r.leave_type_id,
                    leave_type=r.leave_type, kind=r.kind, note=r.private_reason, request_id=r.request_id)
            for r in availability]
    rows = []
    for r in leave_types.names(db, actor.department_id, records):
        source = reports.get(r.get('request_id')) or SimpleNamespace(**r)
        rows.append((r['person_id'], r['start'], r['end'], r['leave_type_name'], r['note'], r['id'], source))
    zone = ZoneInfo(db.get(m.Organisation, actor.organisation_id).timezone)
    patterns = db.scalars(select(m.AvailabilityPattern).where(m.AvailabilityPattern.department_id == actor.department_id,
        m.AvailabilityPattern.status == 'active', m.AvailabilityPattern.person_id.in_(people)))
    from .availability_patterns import occurrences
    first, last = (lo.astimezone(zone) - timedelta(days=1)).date(), hi.astimezone(zone).date()
    rows += [(p.person_id, a, b, KIND_LABELS['weekly_unavailable'], p.note, f'pattern:{p.id}:{day.isoformat()}', p)
             for p in patterns for a, b, day in occurrences(p, first, last, zone, bounds=(lo, hi)) if a < hi and b > lo]
    by_person = {}
    for row in sorted(rows, key=lambda r: r[1]):
        by_person.setdefault(row[0], []).append(row)
    for node, person, a, b in items:
        match = next((r for r in by_person.get(person, ()) if r[1] < b and a < r[2]
                      and (not node.get('availability_id') or node['availability_id'] == r[5])), None)
        if match:
            _, start, end, kind, note, _, source = match
            if getattr(source, 'kind', None) == 'absence_report':
                wording = leave_wording(department, actor, source)
                shown = (display('details' if wording.get('note') else 'type',
                                 wording.get('label') or ABSENCE_CATEGORY_LABELS.get(wording['absence_reason'], wording['absence_reason']),
                                 start, end, wording.get('note'), zone)
                         if wording.get('absence_reason') else None)
                with_leave(node, shown)
            else:
                with_leave(node, display(mode, kind, start, end, note, zone))
    return body


def staff_overlap_labels(body):
    """Derive staff conflict labels only from public clinic metadata, never template provenance."""
    if isinstance(body, list):
        return [staff_overlap_labels(child) for child in body]
    if not isinstance(body, dict):
        return body
    result = {key: [] if key == 'matching_series' else staff_overlap_labels(value) for key, value in body.items()}
    for prefix in ('', 'other_'):
        if prefix + 'session_name' in result and result[prefix + 'session_name'] is not None:
            result[prefix + 'session_name'] = ', '.join(result.get(prefix + 'clinic_names', [])) or 'Session'
    if (result.get('detail') or {}).get('overlap') and 'message' in result:
        result['message'] = 'Scheduled sessions overlap; cancel one occurrence'
    return result


def overlap_labels(db, actor, body, *, disclose=True):
    """Label every time-away clash without mutating stored evidence or a narrower reader's source."""
    department = current_request_parent(db, m.Department, actor.department_id)
    mode = department.leave_reason_display if department else 'none'
    can_disclose = bool(actor.permits(*PLANNING_ROLES) and mode != 'none')
    flags = []

    def project(node):
        if isinstance(node, list):
            return [project(child) for child in node]
        if not isinstance(node, dict):
            return node
        if 'availability_id' in node and node.get('code') in {'TIME_OVERLAP', 'UNAVAILABLE', 'EXTERNAL_ROTATION'}:
            if node['code'] == 'TIME_OVERLAP':
                return {key: project(value) if isinstance(value, (dict, list)) else value
                        for key, value in node.items()}
            result = {key: project(value) if isinstance(value, (dict, list)) else value
                      for key, value in node.items() if key != 'leave'}
            result['label'] = 'Unavailable'
            flags.append(result)
            return result
        return {key: project(value) if isinstance(value, (dict, list)) else value
                for key, value in node.items()}

    result = project(body)
    sanitize_leave_details(result, mode, can_disclose)
    if flags and disclose:
        disclose_leave(db, actor, flags)
    for flag in flags:
        leave = flag.get('leave')
        if leave:
            flag['label'] = leave['type'] + (f": {leave['note']}" if leave.get('note') else '')
    return result


def leave_wording(department, actor, row, name=None, *, own=None):
    """Serialize one leave or absence reason for this reader.

    ``leave_reason_display`` is the one disclosure policy.  A person's own leave keeps its historical type-only
    wording; an own absence report exposes its explicit category and note.  Legacy absence reasons without a valid
    category stay generic at type level and are only retained at details level for the reader who may see details.
    """
    is_report = row.kind == 'absence_report' or getattr(row, 'absence_report', False)
    if is_report:
        own = row.person_id == actor.person_id if own is None and actor else bool(own)
        mode = 'details' if own else (
            department.leave_reason_display if department and actor and actor.permits(*PLANNING_ROLES) else 'none')
        if mode == 'none':
            return {'label': ABSENCE_REPORT_LABEL} if getattr(row, 'absence_report', False) else {}
        details = (row.details or {}) if row.kind == 'absence_report' else {
            'reporter_category': getattr(row, 'absence_report_category', None),
            'note': getattr(row, 'absence_report_note', None)}
        valid_category = canonical_absence_category(details.get('reporter_category'))
        reason = valid_category or 'other'
        private_reason = row.private_reason if row.kind == 'absence_report' else getattr(
            row, 'absence_report_reason', row.private_reason)
        if mode == 'details' and valid_category is None and private_reason:
            reason = private_reason
        result = {'absence_reason': reason}
        if getattr(row, 'absence_report', False):
            result['label'] = ABSENCE_CATEGORY_LABELS.get(reason, reason)
        if mode == 'details':
            if details.get('note'):
                result['note'] = details['note']
            if details.get('document_reference'):
                result['document_reference'] = details['document_reference']
        return result
    kind = name or row.leave_type or KIND_LABELS.get(row.kind, 'Leave')
    own = row.person_id == actor.person_id if own is None and actor else bool(own)
    mode = ('type' if own else
            department.leave_reason_display if department and actor and actor.permits(*PLANNING_ROLES) else 'none')
    return {'label': kind if mode != 'none' else 'On leave or away',
            **({'leave_type_id': getattr(row, 'leave_type_id', None) if mode != 'none' else None,
                'leave_type_name': kind if mode != 'none' else None} if name is not None else {}),
            **({'note': row.private_reason} if mode == 'details' and row.private_reason else {})}
