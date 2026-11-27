"""Batched recurrence conflict inputs and existing audited cancellation commands."""
from datetime import date, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from sqlalchemy import and_, or_, select
from apps.api import models as m
from apps.api.auth import require_role
from apps.api.errors import Problem, require
from apps.api.serialization import json_value, record
from apps.api.snapshots import snapshot_hash, shift_records, frozen_series_states
from packages.contracts import schemas as s
from packages.domain.overlap import evaluate_overlaps, merged_windows, overlap_dates, overlap_envelope, per_session_overlaps
from packages.domain.recurrence import expand, MAX_PREVIEW_DAYS, stored_series_definition
from packages.domain.time import aware, local_midnight
from .common import scoped, expected
from .leave_disclosure import overlap_labels
from .schedule import LIVE, current_publications, in_force_clause


def cancel_actions(db, actor, conflicts):
    """Offer one occurrence command only where this actor can execute it."""
    if not conflicts:
        return []
    shift_ids = {row.get(prefix + 'shift_id') for row in conflicts for prefix in ('', 'other_')} - {None}
    series_ids = {row.get(prefix + 'series_id') for row in conflicts for prefix in ('', 'other_')} - {None}
    dates = {date.fromisoformat(str(row[prefix + 'occurrence_on'])) for row in conflicts for prefix in ('', 'other_')
             if row.get(prefix + 'occurrence_on')}
    virtual = any(str(sid).startswith('series:') for sid in shift_ids)
    selection = m.Shift.id.in_(shift_ids)
    if virtual and dates and series_ids:
        selection = or_(selection, and_(m.Shift.series_id.in_(series_ids), m.Shift.occurrence_on.in_(dates)))
    shifts = {row.id: row for row in db.execute(select(m.Shift.id, m.Shift.workspace_id, m.Shift.series_id,
        m.Shift.occurrence_on, m.Shift.start, m.Shift.locked, m.Shift.cancelled).where(selection))} if shift_ids else {}
    workspace_ids = {row.workspace_id for row in shifts.values()}
    workspaces = {row.id: row for row in db.execute(select(m.Workspace.id, m.Workspace.department_id,
        m.Workspace.status, m.Workspace.archived_at, m.Workspace.base_publication_id).where(
            m.Workspace.id.in_(workspace_ids)))} if workspace_ids else {}
    series = {row.id: row for row in db.execute(select(m.RecurringSeries.id, m.RecurringSeries.revision).where(
        m.RecurringSeries.department_id == actor.department_id, m.RecurringSeries.id.in_(series_ids)))} if series_ids else {}
    current = dict(db.execute(select(m.Publication.workspace_id, m.Publication.effective_until).where(
        m.Publication.current.is_(True), m.Publication.workspace_id.in_(workspace_ids))).all()) if virtual and workspace_ids else {}
    zone = db.get(m.Organisation, actor.organisation_id).timezone if current else None
    by_occurrence, cancellable = {}, {}
    coordinator, admin = actor.permits('coordinator'), actor.permits('admin')
    for shift in shifts.values():
        workspace = workspaces[shift.workspace_id]
        issued = workspace.id in current and (current[workspace.id] is None
                 or aware(shift.start) < local_midnight(current[workspace.id], zone))
        if shift.series_id is not None and shift.occurrence_on is not None and (
                (workspace.status in LIVE and workspace.archived_at is None) or issued):
            by_occurrence.setdefault((shift.series_id, str(shift.occurrence_on)), []).append(shift.id)
        if (coordinator and workspace.department_id == actor.department_id
                and workspace.status == 'draft' and workspace.archived_at is None and not shift.locked and not shift.cancelled):
            cancellable[shift.id] = workspace.id
    result = []
    for conflict in conflicts:
        actions = []
        # A published-change draft has its own approval path; ordinary draft
        # duties still need the direct, audited cancellation action.
        published_change = any(workspaces[shift.workspace_id].base_publication_id
                               for prefix in ('', 'other_')
                               if (shift := shifts.get(conflict.get(prefix + 'shift_id'))) is not None)
        if conflict.get("code") in {"UNAVAILABLE", "EXTERNAL_ROTATION"} and published_change:
            result.append({**conflict, "cancel_actions": []})
            continue
        for prefix, side, label in (('', 'this', 'Cancel this occurrence'), ('other_', 'other', 'Cancel the other occurrence')):
            shift_id = conflict.get(prefix + 'shift_id')
            series_id, day = conflict.get(prefix + 'series_id'), conflict.get(prefix + 'occurrence_on')
            saved = [shift_id] if shift_id in shifts else []
            if not saved and series_id is not None and day is not None:
                saved = by_occurrence.get((series_id, str(day)), [])
            for shift_id in saved:
                workspace_id = cancellable.get(shift_id)
                if workspace_id is not None:
                    actions.append({'side': side, 'label': label, 'kind': 'shift.action', 'target_id': None,
                        'payload': {'workspace_id': workspace_id,
                        'shift_ids': [shift_id], 'action': 'cancel', 'reason': 'Resolve double-booked occurrence'}})
            if saved:
                continue
            template = series.get(series_id)
            if template is not None and day and admin:
                actions.append({'side': side, 'label': label, 'kind': 'series.occurrence.cancel', 'target_id': template.id,
                    'expected_revision': template.revision, 'payload': {'occurrence_on': day,
                    'reason': 'Resolve double-booked occurrence'}})
        result.append({**conflict, 'cancel_actions': actions})
    return result


def finding_actions(db, actor, validation):
    if not validation:
        return validation
    findings = validation.get('findings', [])
    conflicts = []

    def collect(row):
        detail = row.get('detail') or {}
        if detail.get('overlap'):
            conflicts.append(detail['overlap'])
        for child in detail.get('derived_findings', []):
            collect(child)

    for row in findings:
        collect(row)
    decorated = iter(cancel_actions(db, actor, conflicts))

    def project(row):
        detail = row.get('detail') or {}
        updates = {'overlap': next(decorated)} if detail.get('overlap') else {}
        if detail.get('derived_findings'):
            updates['derived_findings'] = [project(child) for child in detail['derived_findings']]
        return {**row, 'detail': {**detail, **updates}} if updates else row

    return {**validation, 'findings': [project(row) for row in findings]}


def session_actions(db, actor, shifts, *, disclose=True):
    conflicts = [conflict for shift in shifts for conflict in shift.get('double_booking_conflicts', [])]
    decorated = iter(cancel_actions(db, actor, overlap_labels(db, actor, conflicts, disclose=disclose)))
    return [{**shift, 'double_booking_conflicts': [next(decorated) for _ in shift['double_booking_conflicts']]}
            if shift.get('double_booking_conflicts') else shift for shift in shifts]


def preview(db, actor, value, series_id=None, *, disclose=True):
    checked = preview_many(db, actor, [s.SeriesPreviewItem(value=value, series_id=series_id)], disclose=disclose)
    return {key: part for key, part in checked['items'][0].items() if key not in {'index', 'series_id'}} | {
        'conflict_token': checked['conflict_token']}


def preview_many(db, actor, items, *, disclose=True):
    """All occurrences in the existing bounded preview; saved instances override templates."""
    from .planning import series_occurrences
    org = db.get(m.Organisation, actor.organisation_id)
    department_ids = list(db.scalars(select(m.Department.id).where(m.Department.organisation_id == org.id)))
    data = {'organisation': record(org), 'department_ids': department_ids}
    # Overlap and calendar checks do not read rules, fairness or approval history.
    for name, cls in {'services': m.Service, 'locations': m.Location, 'types': m.AssignmentType,
                      'resource_pools': m.ResourcePool, 'operating_calendars': m.OperatingCalendar}.items():
        data[name] = {row.id: record(row) for row in db.scalars(select(cls).where(cls.department_id.in_(department_ids)))}
    data['people'] = {pid: {'id': pid, 'display_name': name} for pid, name in db.execute(
        select(m.Person.id, m.Person.display_name).where(m.Person.organisation_id == org.id))}
    data['rotations'] = [record(row) for row in db.scalars(select(m.Rotation).where(m.Rotation.department_id.in_(department_ids)))]
    from .date_sets import data as date_set_data
    data['date_sets'] = date_set_data(db, department_ids)
    series_ids = {item.series_id for item in items if item.series_id is not None}
    ends = [item.value.ends_on or item.value.starts_on + timedelta(days=MAX_PREVIEW_DAYS) for item in items]
    proposed = [SimpleNamespace(id=item.series_id, **item.value.model_dump()) for item in items]

    def timing(template, definition, ranges):
        for start, stop in merged_windows((start, stop) for start, stop in ranges if start < stop):
            while start < stop:
                boundary = min(stop, start + timedelta(days=MAX_PREVIEW_DAYS))
                try:
                    yield from series_occurrences(data, template, definition, start, boundary)
                except Problem as error:
                    if error.code != 'DATE_SET_RANGE':
                        raise
                    # Template saves defer unresolved calendar ranges to rota materialisation.
                start = boundary

    proposed_timing = [list(timing(template, item.value.definition, [(item.value.starts_on, stop)]))
                       if template.active else [] for template, item, stop in zip(proposed, items, ends)]
    windows = merged_windows((aware(row['start']), aware(row['end'])) for rows in proposed_timing for row in rows)
    envelopes = merged_windows(overlap_envelope(start, stop) for start, stop in windows)

    def bounded_queries(query, clauses):
        clauses = list(clauses)
        for offset in range(0, len(clauses), 200):
            yield query.where(or_(*clauses[offset:offset + 200]))

    # Before reading a template's zone, cover its possible local dates (UTC offsets are strictly less than a day).
    template_windows = merged_windows((start.date() - timedelta(days=1), stop.date() + timedelta(days=2))
                                     for start, stop in envelopes)
    templates = {row.id: row for query in bounded_queries(select(m.RecurringSeries).where(
        m.RecurringSeries.department_id.in_(department_ids), m.RecurringSeries.active.is_(True),
        m.RecurringSeries.id.not_in(series_ids)), (and_(m.RecurringSeries.starts_on < stop,
        or_(m.RecurringSeries.ends_on.is_(None), m.RecurringSeries.ends_on > start))
        for start, stop in template_windows)) for row in db.scalars(query)}
    templates = sorted(templates.values(), key=lambda row: row.id)
    zones = {org.timezone, *(stored_series_definition(template.definition).timezone for template in [*templates, *proposed])}
    occurrence_windows = merged_windows((start.astimezone(ZoneInfo(zone)).date(),
        stop.astimezone(ZoneInfo(zone)).date() + timedelta(days=1)) for start, stop in envelopes for zone in zones)

    S = m.Shift
    shift_windows = [and_(S.start < stop, S.end > start) for start, stop in windows]
    lineage_windows = [and_(S.series_id.is_not(None), S.occurrence_on >= start,
        S.occurrence_on < stop) for start, stop in occurrence_windows]
    relevant = {workspace_id for query in bounded_queries(select(S.workspace_id).join(m.Workspace).where(
        m.Workspace.department_id.in_(department_ids)).distinct(), [*shift_windows, *lineage_windows])
        for workspace_id in db.scalars(query)}
    pubs = current_publications(db, department_ids, light=True, workspace_ids=relevant)
    live = and_(m.Workspace.status.in_(LIVE), m.Workspace.archived_at.is_(None))

    def saved_rows(query, clauses):
        # Both range and publication predicates stay bounded; a long duty may meet several range chunks.
        rows = {}
        for selected in bounded_queries(query, clauses):
            rows.update((row['id'], row) for row in db.execute(selected.where(live)).mappings())
            for offset in range(0, len(pubs), 200):
                published = or_(*(in_force_clause(pub, org.timezone) for pub in pubs[offset:offset + 200]))
                rows.update((row['id'], row) for row in db.execute(selected.where(~live, published)).mappings())
        return list(rows.values())

    query = select(S.id, S.workspace_id, S.series_id, S.occurrence_on, S.assignment_type_id,
        S.start, S.end, S.cancelled, S.series_snapshot, S.type_snapshot).join(
        m.Workspace, m.Workspace.id == S.workspace_id).where(
        m.Workspace.department_id.in_(data['department_ids'])).order_by(m.Shift.start, m.Shift.id)
    # Overlap needs the issued duty name, not a copy of its entire definition or custom fields.
    stored = shift_records(db, ({**row, 'type_snapshot': {'name': row['type_snapshot'].get('name')}
        if row['type_snapshot'] else row['type_snapshot']} for row in saved_rows(query, shift_windows)))
    stored.sort(key=lambda row: (aware(row['start']), row['id']))
    published_ids = {pub.workspace_id for pub in pubs}
    frozen_ids = {row['workspace_id'] for row in stored if row['workspace_id'] in published_ids
        and any((row.get('series_snapshot') or {}).get('definition', {}).get(key)
                for key in ('usual_assignees_fixed', 'close_when_all_usual_unavailable'))}
    if frozen_ids:
        evidence = dict(db.execute(select(m.Publication.workspace_id, m.Publication.snapshot['shifts']).where(
            m.Publication.current.is_(True), m.Publication.workspace_id.in_(frozen_ids))).all())
        by_workspace = {}
        for row in stored:
            by_workspace.setdefault(row['workspace_id'], []).append(row)
        frozen = {row['id']: row for workspace_id in frozen_ids for row in frozen_series_states(
            by_workspace[workspace_id], SimpleNamespace(snapshot={'shifts': evidence.get(workspace_id) or []}))}
        stored = [frozen.get(row['id'], row) for row in stored]
    # Saved occurrence identity survives manual moves outside the preview's timestamp window.
    lineage = [json_value(dict(row)) for row in saved_rows(select(S.id, S.series_id, S.occurrence_on, S.cancelled).join(
        m.Workspace, m.Workspace.id == S.workspace_id).where(
        m.Workspace.department_id.in_(data['department_ids'])), lineage_windows)]
    materialised = {(row['series_id'], row['occurrence_on']) for row in lineage}
    own_occurrences = {}
    for row in lineage:
        if row.get('series_id') in series_ids:
            own_occurrences.setdefault((row['series_id'], row.get('occurrence_on')), []).append(row)
    proposed_indexes = {id(template): index for index, template in enumerate(proposed)}
    virtual = []
    focuses, occurrences = [[] for _ in items], [[] for _ in items]
    for template in [*templates, *proposed]:
        index = proposed_indexes.get(id(template))
        value = items[index].value if index is not None else None
        definition = value.definition if value is not None else stored_series_definition(template.definition)
        zone = ZoneInfo(definition.timezone)
        if not template.active:
            continue
        ranges = [(max(start.astimezone(zone).date(), template.starts_on),
                   min(stop.astimezone(zone).date() + timedelta(days=1),
                       template.ends_on or stop.astimezone(zone).date() + timedelta(days=1)))
                  for start, stop in envelopes]
        for occurrence in timing(template, definition, ranges) if value is None else proposed_timing[index]:
            if value is None and not any(aware(occurrence['start']) < stop and aware(occurrence['end']) > start
                                         for start, stop in windows):
                continue
            day = occurrence['start'].astimezone(ZoneInfo(definition.timezone)).date().isoformat()
            if value is None and (template.id, day) in materialised:
                continue
            saved = own_occurrences.get((template.id, day), []) if value is not None else []
            if saved and all(row.get('cancelled') for row in saved):
                continue
            sid = f'series:{template.id or ("preview" if len(items) == 1 else "preview-" + str(index))}:{day}'
            row = {'id': sid, 'series_id': template.id, 'occurrence_on': day, 'workspace_id': None,
                'assignment_type_id': template.assignment_type_id, 'location_id': template.location_id,
                'service_ids': definition.service_ids, 'attributes': definition.attributes,
                'type_snapshot': data['types'][template.assignment_type_id],
                'series_snapshot': {'name': template.name, 'definition': definition.model_dump(mode='json')},
                **json_value(occurrence), 'participants': [
                    {'person_id': pid, **json_value(occurrence)} for pid in definition.default_person_ids]}
            virtual.append(row)
            if value is not None:
                focuses[index].append(sid)
                occurrences[index].append(occurrence)
    # Closure needs every evaluated duty's full envelope, including peers starting before a DST change.
    availability_windows = merged_windows((aware(row['start']), aware(row['end'])) for row in [*stored, *virtual])
    availability = {row.id: record(row, exclude=('private_reason',)) for query in bounded_queries(
        select(m.Availability).where(m.Availability.person_id.in_(data['people']),
        m.Availability.retracted.is_(False), m.Availability.counts_as_unavailable.is_(True)),
        (and_(m.Availability.start < stop, m.Availability.end > start) for start, stop in availability_windows))
        for row in db.scalars(query)}
    data['availability'] = list(availability.values())
    from .availability_patterns import expand as expand_availability
    data['availability'] += list({row['id']: row for start, stop in availability_windows for row in expand_availability(
        db, data['department_ids'], list(data['people']), start, stop, org.timezone)}.values())
    conflicts, grouped = [], []
    for item, ids in zip(items, focuses):
        own = set(ids)
        # Saving a template leaves peers' saved duties intact, including manual changes and cancellations.
        context = {**data, 'shifts': [row for row in stored
            if item.series_id is None or row.get('series_id') != item.series_id] + [row for row in virtual
            if row['id'] in own or (row.get('series_id'), row.get('occurrence_on')) not in materialised]}
        found = evaluate_overlaps(context, focus_ids=own, include_fixed=True)
        conflicts.extend(found)
        by_session = per_session_overlaps(context, found, focus_ids=own)
        grouped.append([conflict for sid in ids for conflict in by_session.get(sid, [])])
    conflicts.sort(key=lambda row: (row['date'], row['person_id'], row['shift_id'], row['other_shift_id'] or '', row['availability_id'] or ''))
    token = snapshot_hash({'department_id': actor.department_id,
        'items': [item.model_dump(mode='json') for item in items], 'conflicts': conflicts})
    for rows in grouped:
        rows.sort(key=lambda row: (row['date'], row['person_id'], row['shift_id'], row['other_shift_id'] or '', row['availability_id'] or ''))
    decorated = iter(overlap_labels(db, actor, cancel_actions(db, actor,
        [row for rows in grouped for row in rows]), disclose=disclose))
    return {'items': [{'index': index, 'series_id': item.series_id, 'occurrences': occurrences[index],
        'timezone': item.value.definition.timezone, 'conflicts': [next(decorated) for _ in grouped[index]]}
        for index, item in enumerate(items)], 'conflict_token': token}


def validate_pending(db, actor, items, *, ignore_invalid=False):
    from .catalogue import validate_references
    ids = [item.series_id for item in items if item.series_id is not None]
    require(len(ids) == len(set(ids)), 'SERIES_PENDING_DUPLICATE', 'Send each pending series once')
    people = {pid for item in items for pid in item.value.definition.default_person_ids}
    context = {'people': set(db.scalars(select(m.Person.id).where(
        m.Person.organisation_id == actor.organisation_id, m.Person.id.in_(people)))) if people else set()}
    # Keep referenced rows alive so scoped db.get calls reuse the identity map throughout validation.
    context['rows'] = [row for cls, keys in (
        (m.RecurringSeries, ids), (m.AssignmentType, {item.value.assignment_type_id for item in items}),
        (m.Location, {item.value.location_id for item in items}),
        (m.Service, {sid for item in items for sid in item.value.definition.service_ids})) if keys
        for row in db.scalars(select(cls).where(cls.department_id == actor.department_id, cls.id.in_(keys)))]
    valid = []
    for item in items:
        try:
            if item.series_id:
                saved = scoped(db, m.RecurringSeries, item.series_id, actor)
                expected(saved, item.expected_revision)
                if 'usual_assignees_fixed' not in item.value.definition.model_fields_set:
                    definition = item.value.definition.model_copy(update={
                        'usual_assignees_fixed': saved.definition.get('usual_assignees_fixed', False)})
                    item = item.model_copy(update={'value': item.value.model_copy(update={'definition': definition})})
            validate_references(db, actor, 'recurring_series', item.value, item.series_id, series_context=context)
        except (Problem, ValueError):
            if not ignore_invalid:
                raise
        else:
            valid.append(item)
    return valid


def write_item(db, actor, command):
    value = command.payload['value']
    if command.target_id and isinstance(value.get('definition'), dict) and 'usual_assignees_fixed' not in value['definition']:
        row = scoped(db, m.RecurringSeries, command.target_id, actor)
        value = {**value, 'definition': {'usual_assignees_fixed':
            row.definition.get('usual_assignees_fixed', False), **value['definition']}}
    return s.SeriesPreviewItem(value=value, series_id=command.target_id, expected_revision=command.expected_revision)


def prepare_writes(db, actor, items, acknowledgement, *, preview_only=False, legacy_acknowledgement=None, ignore_invalid=False):
    """Freeze one validated pending set before any of its writes can change its peers."""
    if not items:
        return None
    require_role(actor, 'admin')
    items = validate_pending(db, actor, items, ignore_invalid=ignore_invalid)
    if not items:
        return None
    # Existing one-row bulk callers may carry the ordinary single-preview token.
    evaluated = [items[0].model_copy(update={'expected_revision': None})] if (
        len(items) == 1 and acknowledgement is None and legacy_acknowledgement is not None) else items
    checked = s.SeriesOverlapBatchPreview.model_validate(preview_many(db, actor, evaluated, disclose=False)).model_dump(mode='json')
    acknowledgement = acknowledgement or legacy_acknowledgement
    if not (preview_only or not any(item['conflicts'] for item in checked['items'])
            or acknowledgement == checked['conflict_token']):
        details = s.SeriesOverlapBatchPreview.model_validate(overlap_labels(db, actor, checked)).model_dump(mode='json')
        raise Problem(409, 'SERIES_OVERLAP_ACKNOWLEDGEMENT',
                      'Review and acknowledge every conflicting occurrence before saving these recurrences', details)
    return {'pending': items, 'checked': checked, 'acknowledgement': acknowledgement, 'preview_only': preview_only}


def pending_write(context, value, series_id, revision):
    if context:
        used = context.setdefault('created_items', set())
        for index, (item, checked) in enumerate(zip(context['pending'], context['checked']['items'])):
            if series_id is None and index in used:
                continue
            if (item.series_id == series_id and item.expected_revision == revision
                    and item.value == value):
                if series_id is None:
                    used.add(index)
                return {key: part for key, part in checked.items() if key not in {'index', 'series_id'}} | {
                    'conflict_token': context['checked']['conflict_token']}
    return None


def cancel_occurrence(db, actor, command):
    """An unmaterialised cancellation is the existing excluded-date edit, with its normal Undo."""
    from .catalogue import write_catalogue
    require_role(actor, 'admin')
    value = s.SeriesOccurrenceCancel.model_validate(command.payload)
    row = scoped(db, m.RecurringSeries, command.target_id, actor)
    expected(row, command.expected_revision)
    definition = stored_series_definition(row.definition)
    occurrences = (expand(definition, value.occurrence_on, value.occurrence_on + timedelta(days=1))
        if row.active and value.occurrence_on >= row.starts_on
        and (row.ends_on is None or value.occurrence_on < row.ends_on) else [])
    require(occurrences,
            'NO_OCCURRENCE', 'This date is not an active occurrence of this series', 409)
    org = db.get(m.Organisation, actor.organisation_id)
    pubs = current_publications(db, [actor.department_id],
        *overlap_dates(occurrences[0]['start'], occurrences[-1]['end'], org.timezone), light=True)
    published = or_(*(in_force_clause(pub, org.timezone) for pub in pubs)) if pubs else m.Workspace.id == ''
    saved = db.scalar(select(m.Shift.id).join(m.Workspace).where(m.Shift.series_id == row.id,
        m.Shift.occurrence_on == value.occurrence_on,
        or_(and_(m.Workspace.status.in_(LIVE), m.Workspace.archived_at.is_(None)), published)).limit(1))
    require(saved is None, 'OCCURRENCE_MATERIALISED', 'Cancel the saved occurrence on its draft rota; published sessions require an amendment', 409)
    updated = s.SeriesInput.model_validate({**{key: record(row)[key] for key in s.SeriesInput.model_fields},
                                          'definition': definition})
    original = preview(db, actor, updated, row.id)
    updated.definition.excluded_dates.append(value.occurrence_on)
    payload = {'catalogue': 'recurring_series', 'value': updated.model_dump(mode='json'), 'reason': value.reason}
    result = write_catalogue(db, actor, command.model_copy(update={'payload': payload}), update=True,
                             resolved_occurrence=value.occurrence_on)
    return {**result, 'cancelled_occurrence_on': value.occurrence_on.isoformat(),
        'undo_command': {'kind': 'catalogue.update', 'target_id': row.id, 'expected_revision': row.revision,
            'payload': {'catalogue': 'recurring_series', 'value': result['previous_value'],
                        'reason': 'Undo occurrence cancellation', 'overlap_acknowledgement': original['conflict_token']}}}
