"""Staff spreadsheet import: people, rank, clinics, skills and programme membership, one person per row.

Preview matches current records by staff number, short code, name, then configured or retained import aliases, and
reports errors per cell. Apply writes through the same checked commands as single edits, split by permission:
a coordinator (or admin) applies people, ranks and programme membership; a credentialer applies clinics and skills.
Whatever the actor may not do stays pending on the batch for someone who may. Every step skips what is already
current; a completed batch cannot be applied again. Uploading the sheet again reviews current changes.
Undo reverts what an apply created, where nobody has changed it since.
"""
import csv
import hashlib
import io
import json
import re
import unicodedata
from datetime import date, timedelta
from types import SimpleNamespace
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified
from packages.contracts import schemas as s
from packages.domain.people import person_aliases
from apps.api import models as m
from apps.api.auth import require_role
from apps.api.errors import require
from apps.api.serialization import record
from apps.api.snapshots import snapshot_hash
from .common import scoped, expected, history, range_overlaps, bump_inputs, batch_input_invalidations, require_import_unapplied
from .independence import today
from .people import imported_aliases
from . import catalogue

COLUMNS = ("display_name", "short_code", "staff_number", "pager", "title", "tags", "email", "work_email", "rank", "rank_from",
           "clinics", "clinics_from", "skills", "skill_evidence", "skills_from", "programme", "programme_from", "programme_last_day",
           "rota_name", "rota_aliases")
EXAMPLE = ("Dr Keiko Keller", "SMP", "000000", "0000", "Consultant", "Night team; Teaching", "keiko.example@hospital.example",
           "", "Consultant", "2026-10-01", "Continuity clinic; Review clinic", "2026-10-01", "Supervision",
           "Credentialing committee minutes 2026-09", "2026-10-01", "", "", "", "", '["Alternate label", "Label; with punctuation"]')
PEOPLE_ROLES = ("admin", "coordinator")
MAX_ROWS = 2000
SPLIT = re.compile(r"\s*[;|]\s*")
PERSON_FIELDS = ("display_name", "title", "staff_number", "pager", "short_code", "rota_name", "work_email")
CREATED = {"memberships": m.Membership, "programme_memberships": m.ProgrammeMembership,
           "eligibilities": m.Eligibility, "grants": m.CapabilityGrant}


def template_csv():
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(COLUMNS)
    writer.writerow(EXAMPLE)
    return out.getvalue()


def column(name):
    return re.sub(r"[\s\-]+", "_", (name or "").strip().lower())


def parse(text):
    """(row_number, {column: value}) for every non-blank row. Headers are forgiving ("Display name"), unknown ones are not."""
    require("\x00" not in text, "INVALID_FILE", "NUL bytes are not allowed")
    text = text.lstrip("﻿")
    # Excel in many locales saves "CSV" with semicolons (or tabs): the header row says which.
    header = text.split("\n", 1)[0]
    delimiter = next((d for d in ",;\t" if d in header), ",")
    lines = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))
    require(bool(lines), "EMPTY_IMPORT", "The file is empty")
    headers = [column(h) for h in lines[0]]
    named = [h for h in headers if h]
    unknown = [h for h in named if h not in COLUMNS]
    require(not unknown, "UNKNOWN_COLUMN", "Unknown columns: " + ", ".join(unknown) + ". Download the template for the column names.",
            422, {"columns": list(COLUMNS)})
    require(len(named) == len(set(named)), "IMPORT_HEADERS", "Each column may appear only once")
    require("display_name" in named, "IMPORT_HEADERS", "The display_name column is required", 422, {"columns": list(COLUMNS)})
    rows = []
    for number, cells in enumerate(lines[1:], 2):
        if not any(c.strip() for c in cells):
            continue
        require(all(not c.strip() for c in cells[len(headers):]), "MALFORMED_ROW", f"Row {number} has more cells than there are columns")
        require(len(rows) < MAX_ROWS, "IMPORT_TOO_LARGE", f"Staff imports are limited to {MAX_ROWS} rows")
        rows.append((number, {h: (cells[i].strip() if i < len(cells) else "") for i, h in enumerate(headers) if h}))
    require(bool(rows), "EMPTY_IMPORT", "The file contains no data rows")
    return rows


def split(value):
    return list(dict.fromkeys(v for v in SPLIT.split(value.strip()) if v)) if value.strip() else []


def by_name(db, actor, cls):
    found = {}
    for row in db.scalars(select(cls).where(cls.department_id == actor.department_id)):
        found.setdefault(row.name.casefold(), []).append(row)
    return found


def lookup(index, name, noun, errors, col):
    rows = index.get(name.casefold(), [])
    active = [r for r in rows if r.active] or rows
    if len(active) == 1:
        return active[0]
    errors[col] = f"More than one {noun} is called {name}" if active else f"No {noun} called {name}. Add it in Configuration first, or fix the spelling."
    return None


def day(value, col, errors, default=None):
    if not value:
        return default
    try:
        return date.fromisoformat(value)
    except ValueError:
        errors[col] = "Use a date like 2026-10-01"
        return None


def covering(rows, starts_on):
    return any(r.starts_on <= starts_on and r.ends_on is None for r in rows)


def match_key(value):
    """The same forgiving key for pasted names, aliases and spreadsheet identifiers."""
    return " ".join("".join(c for c in unicodedata.normalize("NFKD", str(value or ""))
                            if not unicodedata.combining(c)).casefold().split())


def people_index(db, actor, people):
    index = {"staff_number": {}, "short_code": {}, "display_name": {}, "aliases": {}}
    imported = imported_aliases(db, actor)
    for person in people:
        for key in ("staff_number", "short_code", "display_name"):
            value = match_key(getattr(person, key))
            if value:
                index[key].setdefault(value, []).append(person)
        for value in {match_key(alias) for alias in person_aliases(record(person), imported.get(person.id, ()))}:
            if value:
                index["aliases"].setdefault(value, []).append(person)
    return index


def match_person(index, value):
    """Staff number wins over short code, then exact name, then aliases; ambiguity never picks a person."""
    fields = value if isinstance(value, dict) else {key: value for key in index}
    for key in ("staff_number", "short_code", "display_name"):
        hits = index[key].get(match_key(fields.get(key)), [])
        if hits:
            return hits, key
    names = (fields.get("display_name"), fields.get("rota_name"), *(fields.get("rota_aliases") or []))
    hits = {person.id: person for name in names for person in index["aliases"].get(match_key(name), []) if name}
    return (list(hits.values()), "aliases") if hits else ([], None)


def match_people(db, actor, lines, people=None):
    people = list(db.scalars(select(m.Person).where(m.Person.organisation_id == actor.organisation_id))) if people is None else people
    index = people_index(db, actor, people)
    matches, unmatched, ambiguous = [], [], []
    for line in lines:
        hits, by = match_person(index, line)
        if len(hits) == 1:
            matches.append({"line": line, "id": hits[0].id, "by": by})
        elif hits:
            ambiguous.append({"line": line, "candidates": [
                {"id": p.id, "display_name": p.display_name, "short_code": p.short_code, "staff_number": p.staff_number}
                for p in hits]})
        else:
            unmatched.append(line)
    return {"matches": matches, "unmatched": unmatched, "ambiguous": ambiguous}


def plan(db, actor, source, known=None, *, on=None):
    """The current plan for each source row: what applying would change now. Read-only."""
    known = known or {}
    on = on or today(db, actor.department_id)
    people = list(db.scalars(select(m.Person).where(m.Person.organisation_id == actor.organisation_id)))
    by_id = {p.id: p for p in people}
    index = people_index(db, actor, people)
    ranks, clinics, skills, programmes = (by_name(db, actor, cls) for cls in (m.Category, m.Service, m.Capability, m.Programme))
    dept = actor.department_id
    records = {cls: {} for cls in (m.Membership, m.Eligibility, m.CapabilityGrant, m.ProgrammeMembership)}
    for cls, bucket in records.items():
        for r in db.scalars(select(cls).where(cls.department_id == dept)):
            if cls is m.CapabilityGrant and (r.revoked or r.service_id):
                continue
            bucket.setdefault(r.person_id, []).append(r)
    seen = {"staff_number": {}, "short_code": {}, "person": {}, "name": {}}
    out = []
    for number, data in source:
        errors = {}
        get = lambda k: data.get(k, "")  # noqa: E731
        short = get("short_code").upper()
        try:
            aliases = json.loads(get("rota_aliases") or "[]")
        except (ValueError, TypeError):
            aliases = None
        if not isinstance(aliases, list) or any(not isinstance(alias, str) for alias in aliases):
            errors["rota_aliases"] = "Use a JSON array of text aliases"
            aliases = []
        aliases = list(dict.fromkeys(alias.strip() for alias in aliases))
        # Identifiers and the exact display name take precedence; ambiguous alias matches require an identifier.
        person, by = by_id.get(known.get(number) or ""), "import" if known.get(number) in by_id else None
        if person is None:
            hits, by = match_person(index, {**data, "rota_aliases": aliases})
            if len(hits) > 1:
                if by == "aliases":
                    errors["display_name"] = "More than one person matches these rota names or aliases; give a staff number or short code"
                else:
                    errors[by] = f"{len(hits)} people already have this {by.replace('_', ' ')}; give a staff number to pick one"
            elif hits:
                person = hits[0]
        if person is not None and not errors:
            for key, val in (("staff_number", get("staff_number")), ("short_code", short)):
                other = [p for p in index[key].get(match_key(val), []) if p.id != person.id] if val else []
                if other:
                    errors[key] = f"Already used by {other[0].display_name}, but this row matches {person.display_name}"
            if by in {"display_name", "aliases"} and get("staff_number") and person.staff_number and match_key(person.staff_number) != match_key(get("staff_number")):
                errors["staff_number"] = f"{person.display_name} has staff number {person.staff_number}; check this is the same person"
        for key, val in (("staff_number", get("staff_number")), ("short_code", short)):
            normalized = match_key(val)
            if normalized and normalized in seen[key]:
                errors[key] = f"Same {key.replace('_', ' ')} as row {seen[key][normalized]}"
            elif normalized:
                seen[key][normalized] = number
        if person is not None:
            if person.id in seen["person"]:
                errors.setdefault("display_name", f"Same person as row {seen['person'][person.id]}")
            seen["person"][person.id] = number
        elif not get("staff_number") and not short and get("display_name"):
            # Two new rows told apart only by name would add the same person twice.
            name = match_key(get("display_name"))
            if name in seen["name"]:
                errors.setdefault("display_name", f"Same name as row {seen['name'][name]}; give each a staff number or short code, or remove one")
            seen["name"].setdefault(name, number)
        # Person fields: blank cells keep what is there; tags are added, never removed.
        tags = split(get("tags"))
        current = record(person) if person else {}
        value = {k: current[k] for k in s.PersonInput.model_fields if k in current}
        changes = {}
        for key in PERSON_FIELDS:
            new = short if key == "short_code" else get(key)
            if key == "work_email":
                new = new or get("email")
            if new and new != value.get(key):
                changes[key] = [value.get(key, ""), new]
                value[key] = new
        added_aliases = [alias for alias in aliases if alias not in (value.get("rota_aliases") or [])]
        if added_aliases:
            changes["rota_aliases"] = [value.get("rota_aliases") or [], (value.get("rota_aliases") or []) + added_aliases]
            value["rota_aliases"] = changes["rota_aliases"][1]
        added = [t for t in tags if t not in (value.get("tags") or [])]
        if added:
            changes["tags"] = [value.get("tags") or [], (value.get("tags") or []) + added]
            value["tags"] = changes["tags"][1]
        if person is not None and not person.active:
            changes["active"] = [False, True]
            value["active"] = True
        try:
            s.PersonInput.model_validate(value)
        except ValidationError as exc:
            for problem in exc.errors():
                key = str(problem["loc"][0]) if problem["loc"] else "display_name"
                if key == "work_email":
                    key = "work_email" if get("work_email") else "email"
                errors.setdefault(key, problem["msg"].removeprefix("Value error, "))
        if get("email") and get("work_email") and get("email") != get("work_email"):
            errors["work_email"] = "Use one work email address; email and work_email differ"
        action = "new" if person is None else "update" if changes else "same"
        held = lambda cls: records[cls].get(person.id, []) if person else []  # noqa: E731
        # Rank from a date; other current ranks end that day (a move), like "Give a rank" on People.
        rank = None
        if get("rank"):
            category = lookup(ranks, get("rank"), "rank", errors, "rank")
            starts = day(get("rank_from"), "rank_from", errors, on)
            if category and starts:
                mine = held(m.Membership)
                same = [r for r in mine if r.category_id == category.id and range_overlaps(r.starts_on, r.ends_on, starts, None)]
                if covering(same, starts):
                    rank = {"category_id": category.id, "starts_on": starts.isoformat(), "action": "same", "ends": []}
                elif same:
                    errors["rank"] = f"Already has {category.name} for part of this time; change it on People"
                elif any(r.category_id != category.id and r.starts_on >= starts for r in mine):
                    errors["rank_from"] = "Another rank starts on or after this day; change it on People"
                else:
                    ends = sorted({r.category_id for r in mine if r.starts_on < starts and (r.ends_on is None or r.ends_on > starts)})
                    rank = {"category_id": category.id, "starts_on": starts.isoformat(), "action": "add", "ends": ends}
        elif get("rank_from"):
            errors["rank"] = "Give the rank for this start date"
        programme = None
        if get("programme"):
            found = lookup(programmes, get("programme"), "programme", errors, "programme")
            starts = day(get("programme_from"), "programme_from", errors)
            last = day(get("programme_last_day"), "programme_last_day", errors)
            if last == date.max:
                errors["programme_last_day"] = "Use a last day before 9999-12-31"
                last = None
            if starts is None and "programme_from" not in errors:
                errors["programme_from"] = "Programme membership needs a first day"
            if last is None and "programme_last_day" not in errors:
                errors["programme_last_day"] = "Programme membership needs a last day"
            if found and starts and last:
                ends = last + timedelta(days=1)
                if ends <= starts:
                    errors["programme_last_day"] = "The last day must be on or after the first day"
                elif found.starts_on > starts or (found.ends_on is not None and ends > found.ends_on):
                    errors["programme_from"] = f"Must lie within the programme dates of {found.name}"
                else:
                    mine = [r for r in held(m.ProgrammeMembership) if r.programme_id == found.id and range_overlaps(r.starts_on, r.ends_on, starts, ends)]
                    if any(r.starts_on <= starts and r.ends_on >= ends for r in mine):
                        programme = {"programme_id": found.id, "starts_on": starts.isoformat(), "ends_on": ends.isoformat(), "action": "same"}
                    elif mine:
                        errors["programme"] = "Overlaps an existing membership of this programme; change it on Programmes"
                    else:
                        programme = {"programme_id": found.id, "starts_on": starts.isoformat(), "ends_on": ends.isoformat(), "action": "add"}
        # Clinics and skills: one open-ended record each from the given day, skipped when already current.
        granted = {}
        for col, index_, cls, key, noun in (("clinics", clinics, m.Eligibility, "service_id", "clinic"),
                                            ("skills", skills, m.CapabilityGrant, "capability_id", "skill")):
            starts = day(get(col + "_from"), col + "_from", errors, on)
            items = []
            for name in split(get(col)):
                found = lookup(index_, name, noun, errors, col)
                if not found or not starts:
                    continue
                mine = [r for r in held(cls) if getattr(r, key) == found.id and range_overlaps(r.starts_on, r.ends_on, starts, None)]
                if covering(mine, starts):
                    items.append({"id": found.id, "action": "same"})
                elif mine:
                    errors[col] = f"{found.name} overlaps an existing dated record; change it on People"
                else:
                    items.append({"id": found.id, "action": "add"})
            granted[col] = {"starts_on": starts.isoformat() if starts else None, "items": items}
        evidence = get("skill_evidence")
        if any(i["action"] == "add" for i in granted["skills"]["items"]) and len(evidence) < 3:
            errors["skill_evidence"] = "Skills need verification evidence (at least 3 characters)"
        if len(evidence) > 500:
            errors["skill_evidence"] = "Evidence is limited to 500 characters"
        out.append({"row_number": number, "data": data, "errors": errors,
                    "person": {"id": person.id if person else None, "by": by, "action": action, "changes": changes},
                    "rank": rank, "programme": programme, "clinics": granted["clinics"], "skills": granted["skills"]})
    return out


def summary(rows):
    adds = lambda part: sum(1 for r in rows if not r["errors"] for i in r[part]["items"] if i["action"] == "add")  # noqa: E731
    ok = [r for r in rows if not r["errors"]]
    people = {"new_people": sum(r["person"]["action"] == "new" for r in ok),
              "updated_people": sum(r["person"]["action"] == "update" for r in ok),
              "rank_changes": sum(bool(r["rank"] and r["rank"]["action"] == "add") for r in ok),
              "programme_memberships": sum(bool(r["programme"] and r["programme"]["action"] == "add") for r in ok)}
    credentials = {"eligibilities": adds("clinics"), "skills": adds("skills")}
    return {**people, **credentials, "rows": len(rows), "error_rows": len(rows) - len(ok),
            "people_pending": sum(people.values()), "credentials_pending": sum(credentials.values())}


def credential_waiting_rows(batch):
    applications = [a for a in batch.applied.get("applications", []) if a["part"] == "credentials" and not a.get("undone")]
    # Missing legacy evidence closes the part rather than guessing which rows are safe to replay.
    return set(applications[-1].get("waiting_rows", [])) if applications else None


def finished_parts(batch):
    parts = {a["part"] for a in batch.applied.get("applications", []) if not a.get("undone") and a["part"] == "people"}
    if credential_waiting_rows(batch) == set():
        parts.add("credentials")
    return sorted(parts)


def reviewed_plan(db, actor, batch):
    """Bind the displayed result and its actual matching/reference inputs, not only the CSV batch."""
    on = today(db, actor.department_id)
    known = {r["row_number"]: r.get("person_id") for r in batch.rows}
    rows = plan(db, actor, [(r["row_number"], r["data"]) for r in batch.rows], known, on=on)
    waiting = credential_waiting_rows(batch)
    people_finished = "people" in finished_parts(batch)
    for row in rows:
        if waiting is not None and row["row_number"] not in waiting:
            row["errors"] = {} if people_finished else {
                key: error for key, error in row["errors"].items()
                if key not in {"clinics", "clinics_from", "skills", "skills_from", "skill_evidence"}}
    references = {}
    for cls in (m.Category, m.Service, m.Capability, m.Programme, m.Membership, m.Eligibility,
                m.CapabilityGrant, m.ProgrammeMembership, m.Tag, m.TagMember):
        references[cls.__tablename__] = [record(r) for r in db.scalars(
            select(cls).where(cls.department_id == actor.department_id).order_by(cls.id))]
    descriptor = {"contract": "staff-import-reviewed-plan-v1", "actor_id": actor.identity_id,
        "department_id": actor.department_id, "organisation_id": actor.organisation_id,
        "people_part": actor.permits(*PEOPLE_ROLES), "credential_part": actor.permits("credentialer"),
        "effective_on": on.isoformat(), "batch": {"id": batch.id, "revision": batch.revision,
            "source_hash": batch.source_hash, "rows": batch.rows}, "rows": rows,
        "matching_aliases": [record(r) for r in db.scalars(select(m.SourceMapping).where(
            m.SourceMapping.department_id == actor.department_id, m.SourceMapping.target_kind == 'people',
            m.SourceMapping.source_key.startswith('alias:')).order_by(m.SourceMapping.id))],
        "people": [record(p) for p in db.scalars(select(m.Person).where(
            m.Person.organisation_id == actor.organisation_id).order_by(m.Person.id))],
        "references": references}
    return rows, snapshot_hash(descriptor)


def view(db, actor, batch):
    """The live plan and its precondition: each actor reviews their own current Apply inputs."""
    rows, fingerprint = reviewed_plan(db, actor, batch)
    from .contacts import LEGACY_FIELDS, visible_contacts
    ids = {row['person']['id'] for row in rows if row['person']['id']}
    people = [record(person) for person in db.scalars(select(m.Person).where(m.Person.id.in_(ids)))]
    # A synthetic empty subject resolves type defaults for newly imported people; it carries no stored values.
    people.append({'id': ''})
    visible = visible_contacts(db, actor, people, include_empty=True)
    rows = [{**row, 'data': dict(row['data']), 'person': {**row['person'], 'changes': dict(row['person']['changes'])}}
            for row in rows]
    for row in rows:
        allowed = {contact['legacy_field'] for contact in visible.get(row['person']['id'] or '', [])}
        for field in LEGACY_FIELDS - allowed:
            row['person']['changes'].pop(field, None)
            row['data'].pop(field, None)
            if field == 'work_email':
                row['data'].pop('email', None)
    applications = batch.applied.get("applications", [])
    names = dict(db.execute(select(m.Identity.id, m.Identity.display_name).join(
        m.RoleBinding, m.RoleBinding.identity_id == m.Identity.id).where(
        m.RoleBinding.department_id == actor.department_id,
        m.Identity.id.in_({entry["by"] for entry in applications})).distinct()).all()) if applications else {}
    shown = record(batch, exclude=("rows",))
    shown["finished_parts"] = finished_parts(batch)
    shown["applied"] = {**batch.applied, "applications": [
        {**entry, "by_name": names.get(entry["by"])} for entry in applications]}
    return {"batch": shown, "rows": rows, "summary": summary(rows),
            "columns": list(COLUMNS), "source_fingerprint": fingerprint}


def preview(db, actor, command):
    require_role(actor, *PEOPLE_ROLES, "credentialer")
    value = s.StaffImportPreview.model_validate(command.payload)
    source = parse(value.csv_text)
    digest = hashlib.sha256(value.csv_text.encode("utf-8")).hexdigest()
    # The same sheet again reopens its open preview instead of starting a second one.
    batch = db.scalar(select(m.StaffImportBatch).where(m.StaffImportBatch.department_id == actor.department_id,
                      m.StaffImportBatch.source_hash == digest, m.StaffImportBatch.status == "preview").limit(1))
    if batch is None:
        batch = m.StaffImportBatch(department_id=actor.department_id, source_name=value.source_name, source_hash=digest,
                                   rows=[{"row_number": n, "data": d} for n, d in source], applied={"applications": []},
                                   status="preview", created_by=actor.identity_id)
        db.add(batch)
        db.flush()
    return {**view(db, actor, batch), "message": "Preview only. Nothing changes until you apply it."}


@batch_input_invalidations
def apply(db, actor, command):
    value = s.Decision.model_validate(command.payload)
    batch = scoped(db, m.StaffImportBatch, command.target_id, actor)
    people_part, credential_part = actor.permits(*PEOPLE_ROLES), actor.permits("credentialer")
    require(people_part or credential_part, "FORBIDDEN", "Your current role does not permit this action", 403)
    finished = finished_parts(batch)
    people_part = people_part and "people" not in finished
    credential_part = credential_part and "credentials" not in finished
    require_import_unapplied(batch, has_unapplied_part=people_part or credential_part)
    expected(batch, command.expected_revision)
    known = {r["row_number"]: r.get("person_id") for r in batch.rows}
    rows, fingerprint = reviewed_plan(db, actor, batch)
    bad = [r for r in rows if r["errors"]]
    if not bad or command.expected_fingerprint is not None:
        require(command.expected_fingerprint == fingerprint, "STALE_PREVIEW",
                "This import plan was not reviewed or its inputs changed. Reload and review it before applying.",
                409, {"batch_id": batch.id})
    require(not bad, "IMPORT_ROW_ERRORS", f"{len(bad)} rows have errors. Fix them in the sheet and upload it again.", 422,
            {"rows": [{"row_number": r["row_number"], "errors": r["errors"]} for r in bad[:50]]})
    reason = value.reason
    done = {"new_people": 0, "updated_people": 0, "rank_changes": 0, "programme_memberships": 0, "eligibilities": 0, "skills": 0,
            "waiting_for_people": 0, "own": 0}
    applications = []
    if people_part:
        entry = {"part": "people", "by": actor.identity_id, "at": m.now().isoformat(), "people_created": [], "people_updated": [],
                 "created": [], "ended": []}
        for row in rows:
            plan_person = row["person"]
            fields = {k: row["data"].get(k, "") for k in PERSON_FIELDS}
            fields["work_email"] = fields["work_email"] or row["data"].get("email", "")
            fields["short_code"] = fields["short_code"].upper()
            if plan_person["action"] == "new":
                person_value = {k: v for k, v in fields.items() if v}
                person_value["rota_aliases"] = plan_person["changes"].get("rota_aliases", [[], []])[1]
                person_value["tags"] = split(row["data"].get("tags", ""))
                saved = catalogue.write_catalogue(db, actor, SimpleNamespace(
                    payload={"catalogue": "people", "value": person_value, "reason": reason}, target_id=None, expected_revision=None))["record"]
                plan_person["id"] = saved["id"]
                known[row["row_number"]] = saved["id"]
                entry["people_created"].append({"id": saved["id"], "revision": saved["revision"]})
                done["new_people"] += 1
            elif plan_person["action"] == "update":
                person = db.get(m.Person, plan_person["id"])
                current = record(person)
                person_value = {k: current[k] for k in s.PersonInput.model_fields if k in current}
                person_value.update({k: new for k, (_, new) in plan_person["changes"].items()})
                saved = catalogue.write_catalogue(db, actor, SimpleNamespace(
                    payload={"catalogue": "people", "value": person_value, "reason": reason},
                    target_id=person.id, expected_revision=person.revision), update=True)
                entry["people_updated"].append({"id": person.id, "revision": saved["record"]["revision"], "previous": saved["previous_value"]})
                done["updated_people"] += 1
            person_id = plan_person["id"]
            if row["rank"] and row["rank"]["action"] == "add":
                result = catalogue.bulk_membership(db, actor, SimpleNamespace(payload={
                    "person_ids": [person_id], "category_id": row["rank"]["category_id"], "starts_on": row["rank"]["starts_on"],
                    "ends_on": None, "end_other_ranks": True, "reason": reason}))
                entry["created"] += [{"kind": "memberships", "id": i, "revision": 1} for i in result["created_ids"]]
                entry["ended"] += result["ended"]
                done["rank_changes"] += len(result["created_ids"])
            if row["programme"] and row["programme"]["action"] == "add":
                saved = catalogue.write_catalogue(db, actor, SimpleNamespace(payload={"catalogue": "programme_memberships", "reason": reason,
                    "value": {"person_id": person_id, **{k: row["programme"][k] for k in ("programme_id", "starts_on", "ends_on")}}},
                    target_id=None, expected_revision=None))["record"]
                entry["created"].append({"kind": "programme_memberships", "id": saved["id"], "revision": saved["revision"]})
                done["programme_memberships"] += 1
        applications.append(entry)
    if credential_part:
        waiting = credential_waiting_rows(batch)
        entry = {"part": "credentials", "by": actor.identity_id, "at": m.now().isoformat(), "created": [], "waiting_rows": []}
        for row in rows:
            if waiting is not None and row["row_number"] not in waiting:
                continue
            adds = [(col, i["id"]) for col in ("clinics", "skills") for i in row[col]["items"] if i["action"] == "add"]
            if not adds:
                continue
            person_id = row["person"]["id"]
            if person_id is None:
                done["waiting_for_people"] += 1  # a coordinator adds the person first
                entry["waiting_rows"].append(row["row_number"])
                continue
            if person_id == actor.person_id:
                done["own"] += 1  # never self-attested: another credentialer does their own
                entry["waiting_rows"].append(row["row_number"])
                continue
            for col, item in adds:
                clinic = col == "clinics"
                payload = {"person_id": person_id, "service_id" if clinic else "capability_id": item,
                           "starts_on": row[col]["starts_on"], "ends_on": None}
                if not clinic:
                    payload["evidence"] = row["data"].get("skill_evidence", "")
                saved = catalogue.relationship(db, actor, SimpleNamespace(kind="eligibility.create" if clinic else "grant.create", payload=payload))["record"]
                entry["created"].append({"kind": "eligibilities" if clinic else "grants", "id": saved["id"], "revision": saved["revision"]})
                done["eligibilities" if clinic else "skills"] += 1
        applications.append(entry)
    batch.rows = [{**r, "person_id": known.get(r["row_number"]) or r.get("person_id")} for r in batch.rows]
    batch.applied = {"applications": [*batch.applied.get("applications", []), *applications]}
    db.flush()
    left = summary(plan(db, actor, [(r["row_number"], r["data"]) for r in batch.rows], known))
    finished = finished_parts(batch)
    batch.status = "applied" if ("people" in finished or not left["people_pending"]) and \
        ("credentials" in finished or not left["credentials_pending"]) else "partly_applied"
    batch.revision += 1
    db.flush()
    parts = []
    if done["new_people"] or done["updated_people"]:
        parts.append(f"{done['new_people']} new and {done['updated_people']} updated people")
    for key, noun in (("rank_changes", "rank changes"), ("programme_memberships", "programme memberships"),
                      ("eligibilities", "clinic permissions"), ("skills", "skills")):
        if done[key]:
            parts.append(f"{done[key]} {noun}")
    waiting = []
    if left["people_pending"]:
        waiting.append(f"{left['people_pending']} people changes need a coordinator")
    if left["credentials_pending"]:
        waiting.append(f"{left['credentials_pending']} clinic and skill changes need a credentialer")
    message = ("Applied " + ", ".join(parts) if parts else "Nothing new to apply; everything is already current") + "." \
        + (" " + "; ".join(waiting) + "." if waiting else "")
    return {"batch": record(batch, exclude=("rows",)), "applied": done, "pending": left, "message": message}


def undo(db, actor, command):
    """Revert what applies of this batch created, part by part as the actor may, where nothing changed since."""
    value = s.Decision.model_validate(command.payload)
    batch = scoped(db, m.StaffImportBatch, command.target_id, actor)
    expected(batch, command.expected_revision)
    may = {"people": actor.permits(*PEOPLE_ROLES), "credentials": actor.permits("credentialer")}
    entries = [a for a in batch.applied.get("applications", []) if not a.get("undone") and may[a["part"]]]
    require(bool(entries), "NOTHING_TO_UNDO", "Nothing from this import is left for you to undo", 409)
    removed = kept = 0
    for entry in reversed(entries):
        for kind, cls in CREATED.items():
            items = [i for i in entry.get("created", []) if i["kind"] == kind]
            if items:
                r, k = catalogue.remove_created(db, actor, cls, "staff_import.undo", items, value.reason, own=False)
                removed, kept = removed + r, kept + k
        for item in entry.get("ended", []):
            row = db.get(m.Membership, item["id"])
            if row is not None and row.revision == item["revision"]:
                row.ends_on = date.fromisoformat(item["ends_on"]) if item["ends_on"] else None
                row.revision += 1
                history(db, actor, "staff_import.undo", row, value.reason)
                removed += 1
            else:
                kept += 1
        for item in entry.get("people_updated", []):
            row = db.get(m.Person, item["id"])
            if row is not None and row.revision == item["revision"]:
                catalogue.write_catalogue(db, actor, SimpleNamespace(
                    payload={"catalogue": "people", "value": item["previous"], "reason": value.reason},
                    target_id=row.id, expected_revision=row.revision), update=True)
                removed += 1
            else:
                kept += 1
        # People are never deleted (sign-ins and history may point at them): a person this import added is switched off.
        for item in entry.get("people_created", []):
            row = db.get(m.Person, item["id"])
            if row is not None and row.revision == item["revision"] and row.active:
                row.inactive_from = catalogue.person_inactive_from_transition(db, actor, row, False, None)
                row.active = False
                row.revision += 1
                history(db, actor, "people", row, value.reason)
                removed += 1
            else:
                kept += 1
        entry["undone"] = True
    flag_modified(batch, "applied")  # the entries above were marked in place
    batch.status = "undone" if all(a.get("undone") for a in batch.applied["applications"]) else "partly_applied"
    batch.revision += 1
    bump_inputs(db, actor)
    return {"batch": record(batch, exclude=("rows",)), "reverted": removed, "kept": kept,
            "message": f"{removed} changes undone" + (f"; {kept} changed since and kept" if kept else "")
                       + ". People this import added are switched off, not deleted."}
