"""Scheduled metrics only. No attendance or completed-overtime inference."""

from .recurrence import inactive
from .supervision_teams import team_order
from .time import now as clock_now
from collections import Counter, defaultdict
import csv
import io
from uuid import NAMESPACE_URL, uuid5
from datetime import date, timedelta
from html import escape
from zoneinfo import ZoneInfo
from .time import aware, duration_hours, union_hours, local_midnight
from .participation import presence_intervals
from .date_sets import date_set_markers, prepare_date_sets, resolve_date_sets
from packages.contracts.schemas import TypeDefinition

METRIC_VERSION = "scheduled-v3"


def metric_version(snapshot):
    version = snapshot.get("metric_version", "scheduled-v1")
    if version not in {"scheduled-v1", "scheduled-v2", METRIC_VERSION}:
        raise ValueError("Unsupported issued scheduled metric version")
    return version


def person_label(snapshot, person_id):
    return snapshot.get('people', {}).get(person_id, {}).get('display_name', 'Unknown person')


def workload(snapshot: dict, person_id=None, start=None, end=None) -> dict:
    version = metric_version(snapshot)
    from .leadership import lead_turn_pairs
    shared_leads = (lead_turn_pairs(snapshot) if version != "scheduled-v1" and
                    snapshot.get('policies') and snapshot.get('organisation') else None)
    segmented = version != "scheduled-v1"
    rows = defaultdict(lambda: {"assignments": 0, "scheduled_hours": 0.0, "workload_credit": 0.0,
                                "training_credit": 0.0, "planned_overtime_assignments": 0,
                                "planned_overtime_hours": 0.0, "lead_turns": 0, "intervals": [], "overtime_intervals": [], "reserved_intervals": []})
    overtime_shift_ids = set()
    for shift in snapshot["shifts"]:
        if inactive(shift):
            continue
        definition = shift["type_snapshot"]["definition"]
        for p in shift["participants"]:
            pid = p["person_id"]
            if person_id and pid != person_id:
                continue
            reserved = (aware(p["start"]), aware(p["end"]))
            if start is not None and (reserved[1] <= start or reserved[0] >= end):
                continue
            row = rows[pid]
            intervals = presence_intervals(p) if segmented else [reserved]
            counted = start is None or start <= reserved[0] < end
            if start is not None:
                intervals = [(max(a, start), min(b, end)) for a, b in intervals if a < end and b > start]
                reserved = (max(reserved[0], start), min(reserved[1], end))
            hours = union_hours(intervals)
            row["assignments"] += int(counted)
            row["lead_turns"] += int(counted and bool(p.get("team_lead")) and
                                      (shared_leads is None or (shift['id'], pid) in shared_leads))
            row["intervals"].extend(intervals)
            row["reserved_intervals"].append(reserved)
            row["workload_credit"] += hours * definition.get(
                "workload_credit_per_hour", TypeDefinition.model_fields["workload_credit_per_hour"].default)
            row["training_credit"] += hours * definition.get(
                "training_credit_per_hour", TypeDefinition.model_fields["training_credit_per_hour"].default)
            if p.get("overtime", False):
                if counted:
                    overtime_shift_ids.add(shift["id"])
                row["planned_overtime_assignments"] += int(counted)
                row["overtime_intervals"].extend(intervals)
    if version == METRIC_VERSION:
        lo = start if start is not None else local_midnight(date.fromisoformat(snapshot['workspace']['starts_on']), snapshot['timezone'])
        hi = local_midnight(date.fromisoformat(snapshot.get('effective_until') or snapshot['workspace']['ends_on']), snapshot['timezone'])
        if end is not None:
            hi = min(hi, end)
        for pid, intervals in snapshot['overtime_offered_intervals'].items():
            if pid not in snapshot.get('people', {}) or (person_id and pid != person_id):
                continue
            offered = union_hours([(max(aware(a), lo), min(aware(b), hi)) for a, b in intervals
                                   if aware(a) < hi and aware(b) > lo])
            if offered or pid in rows:
                rows[pid]['overtime_offered_hours'] = round(offered, 4)
    output = []
    for pid, row in rows.items():
        row["scheduled_hours"] = round(union_hours(row.pop("intervals")), 4)
        row["planned_overtime_hours"] = round(union_hours(row.pop("overtime_intervals")), 4)
        reserved = union_hours(row.pop("reserved_intervals"))
        if segmented:
            row["reserved_hours"] = round(reserved, 4)
            row["off_cover_hours"] = round(max(0, reserved - row["scheduled_hours"]), 4)
        else:
            del row["lead_turns"]  # issued scheduled-v1 reports stay exactly as issued
        if version == METRIC_VERSION:
            row.setdefault('overtime_offered_hours', 0.0)
        row["workload_credit"], row["training_credit"] = round(row["workload_credit"], 4), round(row["training_credit"], 4)
        output.append({"person_id": pid, "display_name": person_label(snapshot, pid), **row})
    result = {"metric_version": version, "as_of": clock_now().isoformat(), "source_state": snapshot["workspace"]["status"],
            "timezone": snapshot["timezone"], "input_revision": snapshot["input_revision"],
            "availability_revision_at_publication": snapshot["availability_revision"],
            "overtime_marked_shifts": len(overtime_shift_ids), "rows": sorted(output, key=lambda r: r["display_name"]),
            "units": {"scheduled_hours": "elapsed hours, union per person", "workload_credit": "configured credits",
                      "training_credit": "configured scheduled educational credits, not completed training",
                      "lead_turns": "duties where the person is the named team lead",
                      **({"scheduled_hours": "elapsed planned presence hours, excluding explicit off-cover gaps",
                          "reserved_hours": "full duty envelopes; conflict/rest/reserved-hours rule basis",
                          "off_cover_hours": "reserved but not planned presence; no payroll or break-entitlement inference"}
                         if segmented else {})},
            "attendance_basis": "none — planned work only"}
    if "quota_report" in snapshot:
        from .quotas import project_report
        result["quotas"] = project_report(snapshot["quota_report"], person_id)
    return result


def workload_breakdown(snapshot, person_id=None, grouping='week'):
    if grouping not in {'week', 'month'}:
        raise ValueError('Choose week or month grouping')
    begin = date.fromisoformat(snapshot['workspace']['starts_on'])
    finish = date.fromisoformat(snapshot['workspace']['ends_on'])
    result = workload(snapshot, person_id)
    result['rows'] = []
    result['grouping'] = grouping
    result['counting_basis'] = 'Hours split at local period boundaries; assignment counts belong to their start period.'
    cursor = begin
    while cursor < finish:
        if grouping == 'month':
            boundary = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        else:
            week_start = snapshot.get('week_start', begin.weekday())
            boundary = cursor + timedelta(days=7 - (cursor.weekday() - week_start) % 7)
        stop = min(boundary, finish)
        period = workload(snapshot, person_id, local_midnight(cursor, snapshot['timezone']), local_midnight(stop, snapshot['timezone']))
        result['rows'].extend({**row, 'period_start': cursor.isoformat(), 'period_end': stop.isoformat()} for row in period['rows'])
        cursor = stop
    return result


def supervisor_label(snapshot, participant):
    supervisor = participant.get('supervisor_person_id')
    if not supervisor:
        return ''
    return snapshot.get('people', {}).get(supervisor, {}).get('display_name',
        'Assigned' if not snapshot.get('people') else 'not in this rota')


def participation_label(snapshot, participant, *, include_supervisor=True):
    parts = [participant.get('participation', 'staff')]
    if participant.get('team_lead'):
        parts.append('team lead')
    if include_supervisor:
        supervisor = supervisor_label(snapshot, participant)
        if supervisor:
            parts.append('supervisor: ' + supervisor)
    return '; '.join(parts)


def personal_calendar_description(snapshot, participant, prefix, *, date_label=None, note=None):
    """Build an ICS description without disclosing paired participants."""
    description = prefix + participation_label(snapshot, participant, include_supervisor=False) + '.'
    if note:
        description += ' ' + note
    if date_label:
        description += ' ' + date_label + '.'
    return description


def supervision_teams(snapshot, participants, roster=None):
    """Text and attendance rows for each recorded team, in Planner order."""
    groups = []
    for entry in team_order(participants, roster):
        if entry['depth'] and groups:
            groups[-1].append(entry)
        else:
            groups.append([entry])
    result = []
    for group in groups:
        head, *children = group
        p = head['p']
        names = {}
        for child in children:
            q = child['p']
            names.setdefault(q['person_id'], ' '.join(filter(None, (q.get('rank'), person_label(snapshot, q['person_id'])))))
        text = ''
        if names:
            text = 'Supervisor: ' + person_label(snapshot, p['person_id']) + ' — ' + ', '.join(names.values())
        elif p.get('supervisor_person_id') and p['supervisor_person_id'] != p['person_id']:
            text = 'Supervisor: ' + supervisor_label(snapshot, p) + ' — ' + ' '.join(filter(None, (p.get('rank'), person_label(snapshot, p['person_id']))))
        if head['missing']:
            text += ' (not on this session)'
        result.append((text, [entry['p'] for entry in group]))
    return result


def supervision_labels(snapshot, shift):
    groups = supervision_teams(snapshot, shift['participants'])
    by_person = {p['person_id']: text for text, participants in groups if text for p in participants}
    return {p.get('id', p['person_id']): text or by_person.get(p['person_id'], '')
            for text, participants in groups for p in participants}


def schedule_print(snapshot, sequence, current, layout='week', on=None, person_id=None, service_id=None, service_ids=None, score=None, *, stylesheet_url):
    """An escaped print projection of the authorised immutable publication."""
    if layout not in {'day', 'week', 'month'}:
        raise ValueError('Choose day, week or month')
    start = on or date.fromisoformat(snapshot['workspace']['starts_on'])
    if layout == 'month':
        start = start.replace(day=1)
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    elif layout == 'week':
        start -= timedelta(days=(start.weekday() - snapshot.get('week_start', start.weekday())) % 7)
        end = start + timedelta(days=7)
    else:
        end = start + timedelta(days=1)
    zone = snapshot['timezone']
    selected_services = set(service_ids or []) | ({service_id} if service_id else set())
    lo, hi = local_midnight(start, zone), local_midnight(end, zone)
    stamp = lambda value: aware(value).astimezone(ZoneInfo(zone)).strftime('%d %b %H:%M')
    def time_span(begin, finish):
        return '<span class="print-time">' + escape(stamp(begin) + '–' + stamp(finish)) + '</span>'
    rows = []
    for shift in sorted(snapshot['shifts'], key=lambda row: (row['start'], row['id'])):
        if inactive(shift) or (selected_services and not selected_services.intersection(shift['service_ids'])):
            continue
        people = [p for p in shift['participants'] if (not person_id or p['person_id'] == person_id)
                  and aware(p['start']) < hi and aware(p['end']) > lo]
        if person_id and not people:
            continue
        if not (aware(shift['start']) < hi and aware(shift['end']) > lo):
            continue
        groups = []
        selected = {id(p) for p in people}
        for team, participants in supervision_teams(snapshot, shift['participants']):
            participants = [p for p in participants if id(p) in selected]
            if not participants:
                continue
            attendance = '; '.join(escape(person_label(snapshot, p['person_id']) +
                ' (' + participation_label(snapshot, p) + ') · ') + time_span(p['start'], p['end']) for p in participants)
            groups.append(('<p>' + escape(team) + '</p>' if team else '') + attendance)
        names = '; '.join(groups)
        descriptions = [shift['type_snapshot']['name'], ', '.join(snapshot['services'][sid]['name'] for sid in shift['service_ids']),
                        snapshot['locations'][shift['location_id']]['name']]
        rows.append('<tr><td class="duty-time">' + time_span(shift['start'], shift['end']) + '</td>'
                    + ''.join('<td>' + escape(str(value)) + '</td>' for value in descriptions)
                    + '<td>' + (names or 'No people assigned') + '</td></tr>')
    title = person_label(snapshot, person_id) if person_id else ', '.join(snapshot['services'][sid]['name'] for sid in sorted(selected_services)) if selected_services else snapshot['workspace']['name']
    status = 'current version' if current else 'replaced by a newer version'
    day = lambda d: f'{d.day} {d:%b %Y}'
    period = day(start) if end - start == timedelta(days=1) else f'{day(start)} – {day(end - timedelta(days=1))}'
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>' + escape(title) + '</title><link rel="stylesheet" href="' + escape(stylesheet_url, quote=True) + '"></head><body>'
        '<nav><a href="/#published">Back to schedule</a><p>Use your browser’s Print command to print or save as PDF.</p></nav>'
        '<h1>' + escape(title) + '</h1><p><span class="print-period">' + escape(period) + '</span>'
        + escape(f' · version {sequence}, {status} · times in {zone}') + '</p>'
        '<p>Issued duties. Check the current rota for subsequent absences or amendments.</p>'
        + (f'<p>Schedule score at publication: {score:g} / 100</p>' if score is not None else '')
        +
        '<div class="print-table" role="region" aria-label="Published duties" tabindex="0">'
        '<table><thead><tr><th class="duty-time" scope="col">Duty time</th><th scope="col">Duty</th><th scope="col">Clinic / service</th><th scope="col">Location</th><th scope="col">People and assigned times</th></tr></thead><tbody>'
        + (''.join(rows) or '<tr><td colspan="5">No published duties match this selection.</td></tr>') + '</tbody></table></div></body></html>')


def safe_cell(value) -> str:
    import json
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        text = "'" + text
    return text


def report_csv(report: dict, publication_id: str, sequence: int) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, quoting=csv.QUOTE_ALL)
    writer.writerow(["publication_id", publication_id, "sequence", sequence, "metric_version", report["metric_version"],
                     "source_state", report["source_state"], "timezone", report["timezone"],
                     *(["schedule_score", report["schedule_score"]] if report.get("schedule_score") is not None else [])])
    columns = ["person_id", "person", "assignments", "scheduled_hours", "workload_credit", "training_credit",
               "planned_overtime_assignments", "planned_overtime_hours"]
    keys = ["person_id", "display_name", *columns[2:]]
    if report["metric_version"] == METRIC_VERSION:
        columns += ["overtime_offered_hours"]
        keys += ["overtime_offered_hours"]
    if report["metric_version"] != "scheduled-v1":
        columns += ["reserved_hours", "off_cover_hours", "lead_turns"]
        keys += ["reserved_hours", "off_cover_hours", "lead_turns"]
    if report.get('grouping'):
        columns = ['period_start', 'period_end_exclusive'] + columns
        keys = ['period_start', 'period_end'] + keys
    writer.writerow(columns)
    for r in report["rows"]:
        writer.writerow([safe_cell(r[k]) for k in keys])
    return output.getvalue()


def assignments_csv(snapshot, publication_id, sequence, person_id=None):
    version = metric_version(snapshot)
    segmented = version != "scheduled-v1"
    fields = {f["key"] for s in snapshot["shifts"] for f in s["type_snapshot"]["definition"]["fields"] if f["export"] and (not person_id or f["visibility"] == "staff")}
    columns = ["publication_id", "publication_sequence", "metric_version", "source_state", "timezone", "shift_id", "assignment_type", "location", "service", "person", "start", "end", "overtime"] + ["field_" + key for key in sorted(fields)]
    if segmented:
        columns += ["participation_id", "presence_segment", "reserved_start", "reserved_end"]
    columns += ['participation', 'team_lead', 'supervisor', 'team']
    out = io.StringIO(newline="")
    writer = csv.writer(out, quoting=csv.QUOTE_ALL)
    writer.writerow(columns)
    for shift in snapshot["shifts"]:
        if inactive(shift):
            continue
        teams = supervision_labels(snapshot, shift)
        allowed = {f["key"] for f in shift["type_snapshot"]["definition"]["fields"] if f["export"] and (not person_id or f["visibility"] == "staff")}
        for p in shift["participants"]:
            if person_id and p["person_id"] != person_id:
                continue
            intervals = presence_intervals(p) if segmented else [(aware(p["start"]), aware(p["end"]))]
            for index, (start, end) in enumerate(intervals, 1):
                row = [publication_id, sequence, version, snapshot["workspace"]["status"], snapshot["timezone"], shift["id"],
                       shift["type_snapshot"]["name"], snapshot["locations"][shift["location_id"]]["name"],
                       " | ".join(snapshot["services"][sid]["name"] for sid in shift["service_ids"]),
                       person_label(snapshot, p["person_id"]),
                       start.isoformat() if segmented else p["start"], end.isoformat() if segmented else p["end"], str(p["overtime"]).lower()]
                row += [shift["attributes"].get(key, "") if key in allowed else "" for key in sorted(fields)]
                if segmented:
                    row += [p["id"], index, p["start"], p["end"]]
                row += [p.get('participation', 'staff'), str(p.get('team_lead', False)).lower(), supervisor_label(snapshot, p), teams[p.get('id', p['person_id'])]]
                writer.writerow([safe_cell(x) for x in row])
    return out.getvalue()


def ics_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")


def fold_ics(line: str) -> str:
    """RFC5545 folding by UTF-8 octets without splitting multibyte characters."""
    lines, current = [], ""
    for char in line:
        if len((current + char).encode("utf-8")) > 75:
            lines.append(current)
            current = " " + char
        else:
            current += char
    lines.append(current)
    return "\r\n".join(lines)


CRLF = chr(13) + chr(10)


def event_calendar_lines(event, now_stamp, *, organiser=None, attendees=()):
    """The same canonical occurrence in subscriptions and email invitations."""
    stamp = lambda value: aware(value).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VEVENT", f"UID:event-{event['id']}@rota.invalid", f"SEQUENCE:{event['revision']}",
             "DTSTAMP:" + now_stamp, "DTSTART:" + stamp(event["start"]), "DTEND:" + stamp(event["end"]),
             "SUMMARY:" + ics_escape(event["heading"]), "LOCATION:" + ics_escape(event["place"]),
             "DESCRIPTION:" + ics_escape(event["description"]),
             "TRANSP:" + ("TRANSPARENT" if event["attendance"] == "optional" else "OPAQUE"),
             "STATUS:" + ("CANCELLED" if event["cancelled"] else "CONFIRMED")]
    if event["online_url"]:
        lines += ["URL:" + event["online_url"]]
    if event.get("rrule"):
        rule = event["rrule"]
        if "\r" in rule or "\n" in rule:
            raise ValueError("A recurrence rule must occupy one calendar property")
        lines += ["RRULE:" + rule]
    if event.get("exdates"):
        lines += ["EXDATE:" + ",".join(stamp(value) for value in event["exdates"])]
    if organiser:
        lines += ["ORGANIZER:mailto:" + ics_escape(organiser)]
    lines += ["ATTENDEE;RSVP=FALSE:mailto:" + ics_escape(address) for address in attendees]
    return lines + ["END:VEVENT"]


def event_calendar(events, *, method="REQUEST", organiser=None, attendees=()):
    """Email uses the feed's expanded occurrences and stable UIDs, without inventing a second recurrence."""
    if method not in {"REQUEST", "PUBLISH", "CANCEL"}:
        raise ValueError("Unsupported calendar method")
    if method in {"REQUEST", "CANCEL"} and len({event["id"] for event in events}) > 1:
        raise ValueError("A calendar invitation must contain only one UID")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Rota//Scheduled Rota v1//EN",
             "CALSCALE:GREGORIAN", "METHOD:" + method]
    now_stamp = clock_now().strftime("%Y%m%dT%H%M%SZ")
    for event in events:
        lines += event_calendar_lines(event, now_stamp, organiser=organiser, attendees=attendees)
    return CRLF.join(fold_ics(line) for line in lines + ["END:VCALENDAR"]) + CRLF


def date_set_label(snapshot, start):
    zone = snapshot.get('timezone') or snapshot.get('organisation', {}).get('timezone', 'UTC')
    markers = date_set_markers(snapshot, aware(start).astimezone(ZoneInfo(zone)).date())
    return '; '.join(f"{marker['set_name']}: {marker['name']}" for marker in markers)


def date_set_events(snapshot, sequence, publication_stamp):
    """All-day markers use the supplied sets within the publication's in-force date window."""
    if not snapshot.get('date_sets'):
        return []
    first = date.fromisoformat(snapshot['workspace']['starts_on'])
    until = date.fromisoformat(snapshot.get('effective_until') or snapshot['workspace']['ends_on'])
    result = []
    for entry in resolve_date_sets(snapshot, first, until - timedelta(days=1), errors={}):
        day = max(first, date.fromisoformat(entry['first_day']))
        last = min(until - timedelta(days=1), date.fromisoformat(entry['last_day']))
        while day <= last:
            key = f"{entry['set_id']}/{entry['entry_id']}/{entry['year']}/{day.isoformat()}"
            uid = uuid5(NAMESPACE_URL, 'rota.invalid/date-set/' + key)
            result.append((key, [
                'BEGIN:VEVENT', f'UID:{uid}@rota.invalid', f'SEQUENCE:{sequence}',
                'DTSTAMP:' + publication_stamp, 'LAST-MODIFIED:' + publication_stamp,
                'DTSTART;VALUE=DATE:' + day.strftime('%Y%m%d'),
                'DTEND;VALUE=DATE:' + (day + timedelta(days=1)).strftime('%Y%m%d'),
                'SUMMARY:' + ics_escape(entry['name']), 'CATEGORIES:' + ics_escape(entry['set_name']),
                'X-FMUC-DATE-SET-ID:' + entry['set_id'], 'X-FMUC-DATE-ENTRY-ID:' + entry['entry_id'],
                'DESCRIPTION:' + ics_escape(entry['set_name'] + ': ' + entry['name']),
                'TRANSP:TRANSPARENT', 'END:VEVENT']))
            day += timedelta(days=1)
    return result


def _reservation_times(participant):
    return aware(participant['start']), aware(participant['end'])


def _calendar_duties(shifts, person_id):
    duties = defaultdict(list)
    for shift in shifts:
        if inactive(shift):
            continue
        key = f"{shift['assignment_type_id']}/{shift['location_id']}/{aware(shift['start']).isoformat()}"
        for participant in shift["participants"]:
            if participant["person_id"] == person_id:
                duties[key].append((shift, participant))
    for key, matches in duties.items():
        # Without lineage, personal reservation times distinguish copies that share a session key.
        matches.sort(key=lambda duty: _reservation_times(duty[1]))
        for occurrence, (shift, participant) in enumerate(matches, 1):
            yield key, uuid5(NAMESPACE_URL, f"rota.invalid/{key}/{occurrence}"), shift, participant


def _replacement_duty_ids(previous_slots, duties, replace_from):
    current_slots = defaultdict(list)
    for slot, _, shift, participant in duties:
        if slot in previous_slots and aware(shift['start']) >= replace_from:
            current_slots[slot].append((shift['id'], _reservation_times(participant)))

    inherited = {}
    for slot, current in current_slots.items():
        prior = previous_slots[slot]
        if len(prior) == len(current) == 1:
            inherited[current[0][0]] = prior[0][1]
            continue
        # Ordinals can change when a replacement removes one of several reservations in a slot.
        prior_counts = Counter(reservation for reservation, _ in prior)
        current_counts = Counter(reservation for _, reservation in current)
        by_reservation = {reservation: uid for reservation, uid in prior if prior_counts[reservation] == 1}
        inherited.update({
            ident: by_reservation[reservation] for ident, reservation in current
            if current_counts[reservation] == 1 and reservation in by_reservation
        })
    return inherited


def publication_duty_ids(history, person_id):
    """Carry the first issued identifier for an unambiguously inherited duty."""
    previous, previous_slots = {}, {}
    ids = {}
    for shifts, use_lineage, replace_from in history:
        duties = list(_calendar_duties(shifts, person_id))
        lineages = {shift['id']: shift.get('lineage_id') or shift['id'] for _, _, shift, _ in duties}
        counts = Counter(lineages.values())
        replacement_ids = {}
        if replace_from is not None:
            # Only a duty actually removed by the replacement can lend its identifier.
            replacement_ids = _replacement_duty_ids(previous_slots, duties, replace_from)
            previous = {}

        if use_lineage:
            ids = {ident: previous[lineage] for ident, lineage in lineages.items()
                   if counts[lineage] == 1 and lineage in previous}
            for ident in lineages:
                if ident not in ids:
                    ids[ident] = replacement_ids.get(ident) or uuid5(NAMESPACE_URL, f"rota.invalid/duty/{ident}")
        else:
            # Anchor to what a legacy publication already emitted at rollout.
            ids = {shift['id']: legacy_uid for _, legacy_uid, shift, _ in duties}
        previous = {lineage: ids[ident] for ident, lineage in lineages.items() if counts[lineage] == 1}
        previous_slots = defaultdict(list)
        for slot, _, shift, participant in duties:
            previous_slots[slot].append((_reservation_times(participant), ids[shift['id']]))
    return ids


def calendar_feed(snapshots: list, person_id: str, name: str, *, events=()) -> str:
    """A subscribed calendar: one person's duties across the current published rotas and audience-visible department events."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Rota//Scheduled Rota v1//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             "X-WR-CALNAME:" + ics_escape(name), "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H"]
    stamp = lambda s: aware(s).strftime("%Y%m%dT%H%M%SZ")
    markers = {}
    for snapshot, sequence, issued_at in snapshots:
        snapshot = {**snapshot, '_date_set_cache': prepare_date_sets(snapshot)}
        publication_stamp = stamp(issued_at)
        markers.update(date_set_events(snapshot, sequence, publication_stamp))
        segmented = metric_version(snapshot) != "scheduled-v1"
        inherited = snapshot.get('_calendar_duty_ids', {})
        for _, default_id, shift, p in _calendar_duties(snapshot["shifts"], person_id):
            duty_id = inherited.get(shift.get('id'), default_id)
            intervals = presence_intervals(p) if segmented else [(aware(p["start"]), aware(p["end"]))]
            for index, (start, end) in enumerate(intervals, 1):
                # Legacy duties keep the same ordinal when amended into the current snapshot format.
                suffix = f"-presence-{index}"
                label = date_set_label(snapshot, start)
                description = personal_calendar_description(
                    snapshot, p, "Planned duty · role: ",
                    note="Your calendar app updates this on its own schedule; it may take up to a day.",
                    date_label=label)
                lines += ["BEGIN:VEVENT", f"UID:{duty_id}-{person_id}{suffix}@rota.invalid", f"SEQUENCE:{sequence}",
                          "DTSTAMP:" + publication_stamp, "LAST-MODIFIED:" + publication_stamp,
                          "DTSTART:" + stamp(start), "DTEND:" + stamp(end),
                          "SUMMARY:" + ics_escape(shift["type_snapshot"]["name"]),
                          "LOCATION:" + ics_escape(snapshot["locations"][shift["location_id"]]["name"]),
                          "DESCRIPTION:" + ics_escape(description)]
                if label:
                    lines.append('X-FMUC-DATE-SETS:' + ics_escape(label))
                lines.append('END:VEVENT')
    for _, event in sorted(markers.items()):
        lines += event
    for event in events:
        lines += event_calendar_lines(event, stamp(event["created_at"]))
    lines += ["END:VCALENDAR"]
    return CRLF.join(fold_ics(line) for line in lines) + CRLF


def calendar(snapshot: dict, publication_id: str, sequence: int, person_id: str, *, calendar_name=None) -> str:
    snapshot = {**snapshot, '_date_set_cache': prepare_date_sets(snapshot)}
    segmented = metric_version(snapshot) != "scheduled-v1"
    stamp = lambda s: aware(s).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Rota//Scheduled Rota v1//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             "X-WR-CALNAME:" + ics_escape(calendar_name or snapshot.get('department', {}).get('name') or
                                             snapshot.get('workspace', {}).get('department_name') or 'Published rota'),
             "X-PUBLICATION-ID:" + publication_id]
    now_stamp = clock_now().strftime("%Y%m%dT%H%M%SZ")
    for shift in snapshot["shifts"]:
        if inactive(shift):
            continue
        for p in shift["participants"]:
            if p["person_id"] != person_id:
                continue
            intervals = presence_intervals(p) if segmented else [(aware(p["start"]), aware(p["end"]))]
            for index, (start, end) in enumerate(intervals, 1):
                suffix = f"-presence-{index}" if segmented else ""
                label = date_set_label(snapshot, start)
                description = personal_calendar_description(
                    snapshot, p, "Planned duty, not attendance. Publication " + str(sequence) +
                    ". Static copy; consult the application for amendments and current availability. Role: ",
                    date_label=label)
                if segmented:
                    description += " Full duty reservation: " + p["start"] + " to " + p["end"] + ". Off-cover gaps do not mean availability for another duty."
                lines += ["BEGIN:VEVENT", f"UID:{publication_id}-{shift['id']}-{person_id}{suffix}@rota.invalid", f"SEQUENCE:{sequence}",
                          "DTSTAMP:" + now_stamp, "DTSTART:" + stamp(start), "DTEND:" + stamp(end),
                          "SUMMARY:" + ics_escape(shift["type_snapshot"]["name"]),
                          "LOCATION:" + ics_escape(snapshot["locations"][shift["location_id"]]["name"]),
                          "DESCRIPTION:" + ics_escape(description)]
                if label:
                    lines.append('X-FMUC-DATE-SETS:' + ics_escape(label))
                lines.append('END:VEVENT')
    for _, event in date_set_events(snapshot, sequence, now_stamp):
        lines += event
    lines += ["END:VCALENDAR"]
    return "\r\n".join(fold_ics(line) for line in lines) + "\r\n"
