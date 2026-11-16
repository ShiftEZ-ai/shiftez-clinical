"""Staff overtime offers: request lifecycle effects, using the scheduling resolver."""
from copy import deepcopy
from datetime import date, timedelta
from itertools import groupby
from zoneinfo import ZoneInfo

from sqlalchemy import select

from packages.contracts import schemas as s
from packages.domain.conditions import evaluate, scope_tree, with_tags
from packages.domain.generation import allocation_policy_matches
from packages.domain.rule_lines import resolved_overtime_offers
from packages.domain.time import aware, local_midnight, local_stamp
from packages.domain.validation import (ancestor_ids, location_type_allowed, location_validity_findings,
                                        qualified, scope_intervals, valid_dates)
from apps.api import models as m
from apps.api.auth import person_access, require_role
from apps.api.errors import require
from apps.api.serialization import record
from .common import history_many


def days(value):
    if value.dates:
        return sorted(value.dates)
    return [value.starts_on + timedelta(days=n) for n in range((value.ends_on - value.starts_on).days)
            if (value.starts_on + timedelta(days=n)).weekday() in value.weekdays]


def _value(row):
    return s.OvertimeAvailabilityInput.model_validate({
        **{k: v for k, v in row.overtime.items() if k in s.OvertimeAvailabilityInput.model_fields},
        "person_id": row.person_id, "kind": row.kind, "note": row.private_reason})


def _context(db, actor, person_id, starts_on, ends_on):
    from .policies import people_data
    data, _, _ = people_data(db, actor, starts_on)
    person = db.get(m.Person, person_id)
    if not (person is not None and person.active and any(
        r["person_id"] == person_id and r["starts_on"] < ends_on.isoformat()
        and (not r.get("ends_on") or starts_on.isoformat() < r["ends_on"]) for r in data["memberships"])):
        return data
    data["people"][person_id] = record(person)
    data["policies"] = [record(p) for p in db.scalars(select(m.Policy).where(
        m.Policy.department_id == actor.department_id)) if not p.definition.get("workspace_id")]
    for key, cls in (("services", m.Service), ("location_types", m.LocationType),
                     ("locations", m.Location), ("assignment_types", m.AssignmentType)):
        data[key] = {r.id: record(r) for r in db.scalars(select(cls).where(cls.department_id == actor.department_id))}
    data["types"] = data["assignment_types"]
    zone = data["organisation"]["timezone"]
    # shortcut: scan at most one year before the request; use a stored distinct-service-set table or SQL aggregation if needed.
    duties = (select(m.Shift.id, m.ShiftService.service_id)
            .join(m.Workspace, m.Workspace.id == m.Shift.workspace_id)
            .outerjoin(m.ShiftService, m.ShiftService.shift_id == m.Shift.id)
            .where(m.Workspace.department_id == actor.department_id, m.Shift.cancelled.is_(False),
                   m.Shift.start >= local_midnight(starts_on - timedelta(days=365), zone),
                   m.Shift.start < local_midnight(ends_on, zone))
            .order_by(m.Shift.id, m.ShiftService.service_id))
    service_sets = {(sid,) for sid in data["services"]}
    for _, rows in groupby(db.execute(duties.execution_options(yield_per=1000)), lambda row: row[0]):
        service_sets.add(tuple(sid for _, sid in rows if sid in data["services"]))
    data["_overtime_service_sets"] = sorted(service_sets)
    return with_tags(data)


def _clock(day, clock, zone):
    return local_stamp(day, clock, zone)


def _narrow_where(where, value):
    leaves = ([{"field": "service_ids", "op": "any", "values": [value.service_id]}] if value.service_id else [])
    leaves += ([{"field": "assignment_type_ids", "op": "any", "values": [value.assignment_type_id]}]
               if value.assignment_type_id else [])
    return {"match": "all", "conditions": ([where] if where else []) + leaves} if leaves else where


def _scope_choices(data, value, scope):
    """Narrow each dimension once; unknown fields preserve cross-dimension OR branches."""
    tree = scope_tree(scope)

    def matches(field, held):
        return not tree or evaluate(tree, lambda key, *_: held if key == field else None)

    def fields(tree):
        return {field for c in (tree or {}).get("conditions", [])
                for field in (fields(c) if "conditions" in c else {c["field"]})}

    selected_services = scope.get("service_ids") or ([scope["service_id"]] if scope.get("service_id") else [])
    services = [ids for ids in data["_overtime_service_sets"]
                if (not value.service_id or value.service_id in ids)
                and (not selected_services or set(ids).intersection(selected_services)) and matches("service_ids", set(ids))]
    types = [typ for typ in ([value.assignment_type_id] if value.assignment_type_id else data["types"])
             if (not scope.get("assignment_type_id") or typ == scope["assignment_type_id"])
             and matches("assignment_type_ids", {typ})]
    locations = [loc for loc in data["locations"]
                 if (not scope.get("location_id") or scope["location_id"] in ancestor_ids(data, loc))
                 and matches("location_ids", set(ancestor_ids(data, loc)))]
    return services, types, locations, bool(scope.get("location_id")) or "location_ids" in fields(tree)


def _selected_scope(data, value, policy, start, end, choices, allowed=None):
    """A selected clinic/type must describe a real department duty context, checked by the same resolver."""
    services, types, locations, location_scoped = choices
    if not services or not types or not locations:
        return False
    zone = data["organisation"]["timezone"]
    start, end = aware(start), aware(end)
    valid_locations = (loc for loc in locations if not any(location_validity_findings(data, loc, start, end)))
    if location_scoped or any(data["types"][typ]["definition"].get("allowed_location_type_ids") for typ in types):
        valid_locations = list(valid_locations)
    else:
        # Even an unrestricted line needs a real, date-valid department location as its witness.
        location = next(valid_locations, None)
        valid_locations = [] if location is None else [location]
    qualifications = data.setdefault("_overtime_qualifications", {})
    candidates = {"services": {sid for ids in services for sid in ids}, "assignment_types": set(types)} if allowed is not None else {}
    found = False
    for service_ids in services:
        if not all(valid_dates(data["services"][sid], start, end, zone) for sid in service_ids):
            continue
        for typ in types:
            if allowed is not None and found and set(service_ids) <= allowed["services"] and typ in allowed["assignment_types"]:
                continue
            if not valid_dates(data["types"][typ], start, end, zone):
                continue
            shift = {"id": "overtime-eligibility", "start": start, "end": end,
                     "service_ids": list(service_ids), "assignment_type_id": typ,
                     "participants": [], "attributes": {}, "type_snapshot": data["types"][typ]}
            # Clinical qualification also depends on the clinic and the exact dated interval, never the location.
            key = typ, service_ids, start, end
            if key not in qualifications:
                qualifications[key] = qualified(data, value.person_id, shift, data["types"][typ]["definition"], start, end)
            if not qualifications[key]:
                continue
            locations_for_type = valid_locations if location_scoped or data["types"][typ]["definition"].get(
                "allowed_location_type_ids") else valid_locations[:1]
            for location in locations_for_type:
                if not location_type_allowed(data["types"][typ]["definition"], data["locations"][location]):
                    continue
                shift["location_id"] = location
                if allocation_policy_matches(data["_overtime_resolved"], policy, shift, value.person_id):
                    if allowed is None:
                        return True
                    found = True
                    allowed["services"].update(service_ids)
                    allowed["assignment_types"].add(typ)
                    if all(ids <= allowed[key] for key, ids in candidates.items()):
                        return True
                    break
    return found


def offers(db, actor, value, *, validate=True, cache=None, allowed=None):
    person_access(db, actor, value.person_id)
    require(actor.permits("coordinator") or actor.person_id == value.person_id,
            "FORBIDDEN", "Staff may offer overtime only for themselves", 403)
    key = value.person_id, value.starts_on, value.ends_on
    if cache is None:
        data = _context(db, actor, *key)
    else:
        if key not in cache:
            cache[key] = _context(db, actor, *key)
        data = cache[key]
    if value.person_id not in data["people"]:
        require(not validate, "OVERTIME_NOT_ELIGIBLE", "Choose an active department member eligible for overtime", 400)
        return []
    for key, selected in (("services", value.service_id), ("assignment_types", value.assignment_type_id)):
        require(selected is None or selected in data[key], "OVERTIME_SCOPE_NOT_ELIGIBLE",
                "Choose a clinic or duty type within this person's overtime eligibility", 400)
    result = []
    zone = data["organisation"]["timezone"]
    requested_days = days(value)
    if not requested_days:
        return result
    first = requested_days[0]
    initial = resolved_overtime_offers(data, value.person_id,
        _clock(first, value.start_time, zone), _clock(first, value.end_time, zone))
    selections = data.setdefault("_overtime_scope_choices", {})
    selection = value.service_id, value.assignment_type_id
    if selection not in selections:
        selections[selection] = {id(p["definition"]["scope"]): _scope_choices(data, value, p["definition"]["scope"])
            for p in data["_overtime_resolved"]["policies"] if p["definition"]["rule"]["kind"] == "allocation_overtime"}
    choices = selections[selection]
    for day in requested_days:
        a, b = _clock(day, value.start_time, zone), _clock(day, value.end_time, zone)
        candidates = initial if day == first else resolved_overtime_offers(data, value.person_id, a, b)
        found = []
        for policy in candidates:
            lo = max(a, local_midnight(date.fromisoformat(str(policy["starts_on"])), zone)) if policy.get("starts_on") else a
            hi = min(b, local_midnight(date.fromisoformat(str(policy["ends_on"])), zone)) if policy.get("ends_on") else b
            for start, end in scope_intervals(data, policy["definition"]["scope"], value.person_id, lo, hi):
                if not _selected_scope(data, value, policy, start, end, choices[id(policy["definition"]["scope"])], allowed):
                    continue
                tz = ZoneInfo(zone)
                found.append((policy, start.astimezone(tz), end.astimezone(tz)))
        if validate:
            require(bool(found), "OVERTIME_SCOPE_NOT_ELIGIBLE" if candidates and (value.service_id or value.assignment_type_id)
                    else "OVERTIME_NOT_ELIGIBLE",
                    f"Overtime is not available on {day.day} {day:%b %Y} at the selected times. Choose another date or time.", 400)
        result.extend(found)
    return result


def eligibility(db, actor, starts_on, ends_on, person_id=None):
    require_role(actor, "staff", "coordinator")
    person_id = person_id or actor.person_id
    require(person_id is not None, "PERSON_REQUIRED", "Link your account to a person before offering overtime")
    value = s.OvertimeAvailabilityInput(person_id=person_id, starts_on=starts_on, ends_on=ends_on, weekdays=list(range(7)))
    allowed = {"services": set(), "assignment_types": set()}
    cache = {}
    matches = offers(db, actor, value, validate=False, cache=cache, allowed=allowed)
    data = cache[person_id, starts_on, ends_on]
    choices = {key: sorted(
        ({"id": id_, "name": data[key][id_]["name"]} for id_ in ids), key=lambda row: (row["name"], row["id"]))
        for key, ids in allowed.items()}
    return {"person_id": person_id, "eligible": bool(matches), "choices": choices, "offers": [
        {"policy_id": policy["id"], "line_id": policy.get("template_line", {}).get("id"),
         "starts_on": a.date().isoformat(), "ends_on": (a.date() + timedelta(days=1)).isoformat(),
         "start_time": a.strftime("%H:%M"), "end_time": "24:00" if b.date() > a.date() else b.strftime("%H:%M"),
         "where": policy["_overtime_line"].get("where"),
         "volunteer_required": policy["definition"]["rule"].get("volunteer_required", False),
         "required_policy_ids": policy["definition"]["rule"].get("required_policy_ids", [])}
        for policy, a, b in matches]}


def create(db, actor, value):
    offers(db, actor, value)
    zone = db.get(m.Organisation, actor.organisation_id).timezone
    return m.RequestRecord(department_id=actor.department_id, person_id=value.person_id, kind=value.kind,
        start=local_midnight(value.starts_on, zone), end=local_midnight(value.ends_on, zone),
        private_reason=value.note, counts_as_unavailable=False, created_by=actor.identity_id,
        overtime=value.model_dump(mode="json", exclude={"person_id", "kind", "note"}))


def approve(db, actor, row, reason, *, cache=None):
    value = _value(row)
    facts = []
    groups = {}
    for policy, start, end in offers(db, actor, value, cache=cache):
        source = {"policy_id": policy["id"], "line_id": policy.get("template_line", {}).get("id"),
                  "line_index": policy.get("template_line", {}).get("line")}
        clocks = start.strftime("%H:%M"), "24:00" if end.date() > start.date() else end.strftime("%H:%M")
        key = (*source.values(), *clocks)
        if key not in groups:
            groups[key] = (policy, source, clocks, set())
        groups[key][3].add(start.date())
    for policy, source, clocks, offered in groups.values():
        first, last = min(offered), max(offered) + timedelta(days=1)
        weekdays = sorted({day.weekday() for day in offered})
        line = deepcopy(policy["_overtime_line"])
        line.update(id=m.uid(), who=None, where=_narrow_where(line.get("where"), value),
                    starts_on=first.isoformat(), ends_on=last.isoformat(),
                    weekdays=weekdays, start_time=clocks[0], end_time=clocks[1],
                    numbers={"apply": {"level": policy["definition"].get("strength", "medium")}},
                    settings={"allocation_overtime": {k: v for k, v in policy["definition"]["rule"].items() if k != "kind"}},
                    source_bindings={})
        line["settings"]["allocation_overtime"]["volunteer_required"] = False
        data = {"catalogue_id": "overtime_offers", "catalogue_version": 1,
                "line": s.RuleLine.model_validate(line).model_dump(mode="json"), "offer_source": source}
        # Irregular dates must not grant the gaps that a range plus weekdays would include.
        if any(first + timedelta(days=n) not in offered
               for n in range((last - first).days) if (first.weekday() + n) % 7 in weekdays):
            data["offer_dates"] = sorted(day.isoformat() for day in offered)
        fact = m.SchedulingFact(department_id=actor.department_id, kind="overtime_offer", person_id=row.person_id,
            request_id=row.id, status="active", on=True, starts_on=first, ends_on=last,
            proposed_by=row.created_by, approved_by=actor.identity_id, approved_at=m.now(), reason=reason,
            data=data)
        db.add(fact)
        facts.append(fact)
    history_many(db, actor, [("scheduling_fact", fact, reason) for fact in facts])
    return len(facts)


def locked(db, actor, row):
    value = _value(row)
    offered = days(value)
    rotas = db.execute(select(m.Workspace.starts_on, m.Workspace.ends_on).where(
        m.Workspace.department_id == actor.department_id, m.Workspace.submitted_at.is_not(None),
        m.Workspace.starts_on < value.ends_on, m.Workspace.ends_on > value.starts_on)).all()
    if any(a <= day < b for a, b in rotas for day in offered):
        return True
    # Reopening clears submitted_at. The original submission receipt retains its exact submitted date range.
    result = m.CommandReceipt.result["workspace"]
    historic = db.execute(select(result["starts_on"].as_string(), result["ends_on"].as_string()).where(
        m.CommandReceipt.department_id == actor.department_id, m.CommandReceipt.command_kind == "workspace.submit",
        result["starts_on"].as_string() < value.ends_on.isoformat(),
        result["ends_on"].as_string() > value.starts_on.isoformat())).all()
    return any(a <= day.isoformat() < b for a, b in historic for day in offered)


def require_unlocked(db, actor, row):
    require(not locked(db, actor, row), "OVERTIME_ROTA_SUBMITTED",
            "This overtime offer cannot be withdrawn or undone after a rota covering its dates has been submitted", 409)


def _request_facts(db, actor, request_id):
    rows = dict.fromkeys([*db.scalars(select(m.SchedulingFact).where(m.SchedulingFact.request_id == request_id)),
                          *(fact for fact in [*db.new, *db.dirty] if isinstance(fact, m.SchedulingFact))])
    return [fact for fact in rows if fact not in db.deleted and fact.department_id == actor.department_id
            and fact.request_id == request_id]


def retire(db, actor, row, reason):
    facts = [fact for fact in _request_facts(db, actor, row.id) if fact.status == "active"]
    for fact in facts:
        fact.status = "retired"
        fact.revision += 1
    history_many(db, actor, [("scheduling_fact", fact, reason) for fact in facts])
    return [fact.id for fact in facts]


def restore(db, actor, row, reason):
    # Restore precisely the facts this withdrawal retired, never an earlier undone approval.
    facts = [fact for fact in _request_facts(db, actor, row.id) if fact.id in row.overtime.get("withdrawn_facts", [])]
    for fact in facts:
        require(fact.status == "retired", "STALE_REVISION", "The overtime offer changed after withdrawal", 409)
        fact.status = "active"
        fact.revision += 1
    history_many(db, actor, [("scheduling_fact", fact, reason) for fact in facts])
    return len(facts)
