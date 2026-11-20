"""Every advertised bulk action must preserve its ordinary handler's persisted result."""
from copy import deepcopy
from datetime import date, datetime
from itertools import count
import json
import uuid

import pytest
from sqlalchemy import literal, select

from apps.api import models as m
from apps.api.auth import authenticate, require_role
from apps.api.commands import command_roles, dispatch
from apps.api.db import transaction
from apps.api.serialization import json_value
from packages.contracts import schemas as s


REASON = "Independent single and bulk equivalence"
REASONLESS = {"membership.create", "eligibility.create", "grant.create", "availability.record", "request.create",
              "availability_pattern.propose", "wish.create", "wish.update", "shift.create", "shift.update"}


def command(kind, payload=None, row=None):
    return s.Command(kind=kind, payload=payload or {} if kind in REASONLESS else {**(payload or {}), "reason": REASON},
                     target_id=row and row["id"], expected_revision=row and row["revision"])


def case(h, kind):
    """Build real, valid prerequisites; the tested command itself runs in both branches below."""
    def run(role, name, payload=None, row=None, key="record"):
        return h.command(role, name, command(name, payload).payload,
                         row and row["id"], row and row["revision"])[key]

    person = h.seed["people"][0]
    if kind == "job.cancel":
        workspace = h.workspace("Bulk equivalence job")
        with transaction(h.app.state.engine, write=True) as db:
            row = m.Job(department_id=h.seed['department_id'], workspace_id=workspace['id'],
                requested_by=h.seed['identities']['coordinator'], kind='feasibility', status='queued',
                input_fingerprint='frozen', input_snapshot={})
            db.add(row)
            db.flush()
            target = {"id": row.id, "revision": row.revision}
        return "coordinator", command(kind, row=target)
    if kind == "person.anonymise":
        row = run("admin", "catalogue.create", {"catalogue": "people", "value": {
            "display_name": "Synthetic former colleague", "active": False, "staff_number": "EQ-ANON",
            "short_code": "EQANON", "work_phone": "Synthetic phone", "tags": ["Synthetic former team"]}})
        return "admin", command(kind, row=row)
    if kind.startswith("catalogue."):
        payload = {"catalogue": "services", "value": {"name": "Synthetic equivalence clinic", "starts_on": "2026-01-01"}}
        if kind == "catalogue.create":
            return "admin", command(kind, payload)
        row = run("admin", "catalogue.create", payload)
        if kind == "catalogue.undo_create":
            return "admin", command(kind, {"catalogue": "services"}, row)
        if kind == "catalogue.bulk_update":
            return "admin", command(kind, {"catalogue": "services", "items": [{"id": row["id"], "revision": 1}],
                                             "set": {"description": "Changed in both paths"}})
        return "admin", command(kind, {**payload, "value": {**payload["value"], "description": "Changed in both paths"}}, row)
    if kind == "tag.members":
        row = run("admin", "catalogue.create", {"catalogue": "tags", "value": {"name": "Equivalence team", "labels": ["people"]}})
        return "coordinator", command(kind, {"add": [{"kind": "people", "id": person}], "remove": []}, row)
    if kind in {"programme_membership.withdraw", "programme_membership.restore"}:
        from .test_future_record_withdrawal import _programme

        row = _programme(h, "2028-02-01")
        if kind == "programme_membership.restore":
            row = run("coordinator", "programme_membership.withdraw", row=row)
        return "coordinator", command(kind, row=row)
    if kind in {"rotation.remove", "rotation.restore"}:
        from .test_rotation_withdrawal import future_value, remove

        value = future_value(h)
        value.pop("mentor_id")
        row = h.configure("rotations", value, role="coordinator")["record"]
        payload = (remove(h, [row])["undo"]["payload"] if kind == "rotation.restore"
                   else {"items": [{"id": row["id"], "revision": row["revision"]}]})
        return "coordinator", command(kind, payload)
    if kind.startswith(("membership.", "eligibility.", "grant.")):
        family, action = kind.split(".")
        role = "coordinator" if family == "membership" else "credentialer"
        payload = {"person_id": person, "starts_on": "2027-01-01", "ends_on": "2027-06-01"}
        ref = {"membership": ("categories", "category_id", {"name": "Synthetic equivalence rank", "short_code": "EQ"}),
               "eligibility": ("services", "service_id", {"name": "Synthetic equivalence service"}),
               "grant": ("capabilities", "capability_id", {"name": "Synthetic equivalence skill"})}[family]
        catalogue, key, value = ref
        ref_row = run("admin", "catalogue.create", {"catalogue": catalogue, "value": {**value, "starts_on": "2026-01-01"}})
        payload[key] = ref_row["id"]
        if family == "grant":
            payload["evidence"] = "Synthetic reviewed competence evidence"
        if action == "create":
            return role, command(kind, payload)
        if action == "bulk_create":
            bulk = {"person_ids": [person], "starts_on": payload["starts_on"], "ends_on": payload["ends_on"]}
            bulk[key if family == "membership" else key.replace("_id", "_ids")] = ref_row["id"] if family == "membership" else [ref_row["id"]]
            if family == "grant":
                bulk["evidence"] = payload["evidence"]
            return role, command(kind, bulk)
        if action == "programme":
            programme = run("admin", "catalogue.create", {"catalogue": "programmes", "value": {"name": "Equivalence programme", "starts_on": "2026-01-01"}})
            run("admin", "catalogue.create", {"catalogue": "programme_memberships", "value": {
                "person_id": person, "programme_id": programme["id"], "starts_on": "2027-01-01", "ends_on": "2027-06-01"}})
            return role, command(kind, {"programme_id": programme["id"], "person_ids": [person], "service_ids": [ref_row["id"]],
                                       "starts_on": "2027-01-01", "ends_on": "2027-06-01"})
        row = run(role, family + ".create", payload)
        if action == "undo_create":
            return role, command(kind, {"items": [{"id": row["id"], "revision": row["revision"]}]}, row)
        if action == "restore":
            row = run(role, "grant.revoke", {"ends_on": "2027-05-01"}, row)
            return role, command(kind, {"starts_on": "2027-01-01", "ends_on": "2027-06-01"}, row)
        dates = {"ends_on": "2027-07-01" if action in {"extend", "update_dates"} else "2027-05-01"}
        if action == "update_dates":
            dates["starts_on"] = "2027-01-01"
        return role, command(kind, dates, row)
    if kind.startswith("availability."):
        payload = {"person_id": person, "kind": "recorded_leave", "start": "2026-11-01T00:00:00+03:00",
                   "end": "2026-11-02T00:00:00+03:00", "private_reason": "Synthetic confidential reason"}
        if kind == "availability.record":
            return "coordinator", command(kind, payload)
        created = h.command("coordinator", "availability.record", command("availability.record", payload).payload)
        row = created["record"]
        if kind == "availability.undo_create":
            return "coordinator", command(kind, created["undo"]["payload"], row)
        if kind == "availability.undo_update":
            result = h.command("coordinator", "availability.update", {"kind": "off_duty", "reason": REASON}, row["id"], row["revision"])
            return "coordinator", command(kind, result["undo"]["payload"], result["record"])
        if kind == "availability.restore":
            row = run("coordinator", "availability.retract", row=row)
        return "coordinator", command(kind, {"kind": "off_duty"} if kind.endswith("update") else {}, row)
    if kind.startswith("request."):
        row = run("staff", "request.create", {"person_id": person, "kind": "leave", "start": "2026-11-01T00:00:00+03:00",
                                              "end": "2026-11-02T00:00:00+03:00", "private_reason": "Synthetic confidential request"})
        if kind == "request.undo":
            row = run("approver", "request.decide", {"decision": "approve"}, row)
        payload = {"decision": "approve"}
        if kind == "request.decide_many":
            payload["items"] = [{"source": "request", "id": row["id"], "expected_revision": row["revision"]}]
            return "approver", command(kind, payload)
        return "approver", command(kind, payload if kind.endswith("decide") else {}, row)
    if kind.startswith("availability_pattern."):
        payload = {"person_id": person, "weekdays": [1], "start_time": "08:00", "end_time": "10:00", "starts_on": "2026-11-01"}
        if kind.endswith("propose"):
            return "coordinator", command(kind, payload)
        row = run("staff", "availability_pattern.propose", payload, key="pattern")
        if kind.endswith(("end", "undo", "restore")):
            row = run("coordinator", "availability_pattern.decide", {"decision": "approve"}, row, key="pattern")
        if kind.endswith("restore"):
            row = run("coordinator", "availability_pattern.end", row=row, key="pattern")
        return "coordinator", command(kind, {"decision": "approve"} if kind.endswith("decide") else {}, row)
    if kind.startswith(("rule.", "policy.", "exception.")):
        payload = {"catalogue_id": "duties_per_week", "lines": [{"id": str(uuid.uuid4()), "numbers": {"max": {"value": 5}}}]}
        if kind == "rule.propose":
            return "coordinator", command(kind, payload)
        row = run("coordinator", "rule.propose", payload)
        if kind == "policy.activate_many":
            return "approver", command(kind, {"items": [{"policy_id": row["id"], "expected_revision": row["revision"]}]})
        if kind == "policy.restore_proposal":
            row = run("coordinator", "policy.decline", row=row)
        elif kind not in {"policy.activate", "policy.decline"}:
            row = run("approver", "policy.activate", row=row)
        if kind == "policy.restore":
            row = run("approver", "policy.retire", row=row)
        if kind.startswith("policy."):
            return "coordinator" if kind in {"policy.decline", "policy.restore_proposal"} else "approver", command(kind, row=row)
        # Person exceptions require an explicitly exception-permitting, non-safety rule.
        with transaction(h.app.state.engine, write=True) as db:
            policy = db.get(m.Policy, row["id"])
            policy.definition = {**policy.definition, "exception_allowed": True}
        payload = {"policy_id": row["id"], "person_id": person, "starts_on": "2026-11-01", "ends_on": "2026-11-08"}
        if kind in {"exception.create", "exception.create_many"}:
            return "coordinator", command(kind, {"items": [payload]} if kind.endswith("many") else payload)
        exception = run("coordinator", "exception.create", payload)
        if kind != "exception.approve":
            exception = run("approver", "exception.approve", row=exception)
        if kind == "exception.undo":
            exception = run("approver", "exception.end", row=exception)
        return "approver", command(kind, row=exception)
    if kind.startswith(("role.", "identity.")):
        identity = h.seed["identities"]["auditor"]
        if kind.startswith("identity."):
            with transaction(h.app.state.engine) as db:
                row = {"id": identity, "revision": db.get(m.Identity, identity).revision}
            if kind == "identity.enable":
                row = run("admin", "identity.disable", row=row)
            return "admin", command(kind, row=row)
        payload = {"identity_id": identity, "role": "coordinator", "starts_on": "2026-01-01"}
        if kind == "role.grant":
            return "admin", command(kind, payload)
        row = run("admin", "role.grant", payload)
        if kind == "role.restore":
            row = run("admin", "role.revoke", row=row)
        return "admin", command(kind, row=row)
    if kind.startswith("wish."):
        payload = {"person_id": person, "first_day": "2026-11-01", "last_day": "2026-11-01", "kind": "prefer_off", "points": 1}
        if kind == "wish.create":
            return "staff", command(kind, payload)
        row = run("staff", "wish.create", payload, key="wish")
        if kind == "wish.restore":
            row = run("staff", "wish.withdraw", row=row, key="wish")
        return "staff", command(kind, {**payload, "note": "A revised wish"} if kind == "wish.update" else {}, row)
    if kind.startswith(("person.workload.", "person.overtime.", "calendar.protection.")):
        family, action = kind.rsplit(".", 1)
        if family == "person.workload":
            payload = {"kind": "workload", "person_id": person, "catalogue_id": "workload_limit", "line": {
                "id": str(uuid.uuid4()), "measure": "duties", "period": {"kind": "calendar_week"},
                "numbers": {"max": {"value": 5, "level": "mandatory"}}}}
        else:
            overtime = family == "person.overtime"
            payload = {"kind": "overtime_offer" if overtime else "protected_time", "person_id": person,
                       "catalogue_id": "overtime_offers" if overtime else "protected_time", "line": {
                           "id": str(uuid.uuid4()), "weekdays": [1], "start_time": "09:00", "end_time": "11:00",
                           "numbers": {"apply": {"value": None, "level": "medium" if overtime else "mandatory"}}}}
        if action == "propose":
            return "coordinator", command(kind, payload)
        row = run("coordinator", family + ".propose", payload, key="fact")
        if action == "restore_proposal":
            row = run("approver", family + ".decline", row=row, key="fact")
        if action in {"retire", "undo"}:
            row = run("approver", family + ".activate", row=row, key="fact")
        return "approver", command(kind, row=row)
    if kind.startswith("shift.") or kind == "issues.resolve":
        if kind == "issues.resolve":
            h.policy({"kind": "restriction"}, {"person_id": person}, severity="advisory")
        w = h.workspace()
        payload = {"workspace_id": w["id"], "assignment_type_id": h.seed["type"], "location_id": h.seed["locations"][0],
                   "service_ids": [h.seed["services"][0]], "start": "2026-10-04T08:00:00+03:00", "end": "2026-10-04T12:00:00+03:00",
                   "participants": [{"person_id": person, "start": "2026-10-04T08:00:00+03:00", "end": "2026-10-04T12:00:00+03:00"}]}
        if kind == "shift.create":
            return "coordinator", command(kind, payload, w)
        result = h.command("coordinator", "shift.create", payload, revision=w["revision"])
        w, shift = result["workspace"], result["shifts"][0]
        if kind == "shift.undo_batch":
            return "coordinator", command(kind, row=w)
        if kind == "shift.update":
            return "coordinator", command(kind, {**payload, "shift_id": shift["id"],
                "participants": [{**payload["participants"][0], "fixed": True}]}).model_copy(update={"expected_revision": w["revision"]})
        if kind == "shift.copy":
            return "coordinator", command(kind, {"workspace_id": w["id"], "shift_ids": [shift["id"]], "offset_days": 1}, w)
        if kind == "shift.action":
            return "coordinator", command(kind, {"workspace_id": w["id"], "shift_ids": [shift["id"]], "action": "lock"}, w)
        from apps.api.services.issues import issue_key
        finding = next(f for f in h.detail(w)["workspace"]["validation"]["findings"] if f.get("shift_id"))
        return "coordinator", command(kind, {"workspace_id": w["id"], "action": "remove", "keys": [issue_key(finding)]}, w)
    pytest.fail("No real equivalence fixture for advertised bulk command " + kind)


def persisted(db):
    """Compare all domain tables and definition history, including ancillary records the reply omits."""
    # Ordinals include excluded transport/audit writes and are not domain content.
    # workspace_read_projections is a derived read cache rebuilt from the domain rows compared here.
    ignored = {"audit_events", "command_receipts", "rate_limits", "record_insertions", "workspace_read_projections"}
    result = {}
    for table in m.Base.metadata.tables.values():
        if table.name in ignored:
            continue
        values = [json_value(dict(row)) for row in db.execute(select(table)).mappings()]
        # The outer command and batch own different transport/audit identifiers; the domain content must agree.
        for value in values:
            if table.name == "definition_history":
                value.pop("batch_id", None)
                value.pop("id", None)
            if table.name == "workspaces" and isinstance(value.get("validation"), dict):
                # The content digest hashes generated row ids, which differ by the batch's own ids; the rows it
                # digests are compared canonically, and the other manifest components still must agree.
                check = value["validation"] = dict(value["validation"])
                check.pop("fingerprint", None)
                if isinstance(check.get("input_manifest"), dict):
                    manifest = check["input_manifest"] = {**check["input_manifest"]}
                    manifest.pop("token", None)
                    manifest["components"] = {k: v for k, v in (manifest.get("components") or {}).items() if k != "content"}
        result[table.name] = sorted(values, key=lambda value: json.dumps(value, sort_keys=True))
    return result


def canonical(state, baseline):
    """Generated primary keys may interleave with deferred history/audit inserts differently."""
    existing = {row["id"] for rows in baseline.values() for row in rows if "id" in row}
    generated = {}
    for table, rows in state.items():
        new = [row for row in rows if row.get("id") and row["id"] not in existing]
        new.sort(key=lambda row: json.dumps({k: v for k, v in row.items() if k != "id"}, sort_keys=True))
        for index, row in enumerate(new):
            generated[row["id"]] = f"<{table}:{index}>"

    def replace(value):
        if isinstance(value, str):
            for key, replacement in generated.items():
                value = value.replace(key, replacement)
            return value
        if isinstance(value, dict):
            return {replace(k): replace(v) for k, v in value.items()}
        if isinstance(value, list):
            return [replace(v) for v in value]
        return value

    return {table: sorted(replace(rows), key=lambda row: json.dumps(row, sort_keys=True)) for table, rows in state.items()}


def effective_tables(state, kind):
    """Undo retains lifecycle evidence; compare the effective data independently of that evidence."""
    family = kind.split(".")[0]
    tables = {"catalogue": ["services"], "tag": ["tag_members", "people"], "membership": ["memberships"],
              "programme_membership": ["programme_memberships"], "rotation": ["rotations"],
              "eligibility": ["eligibilities"], "grant": ["capability_grants"], "availability": ["availability"],
              "availability_pattern": ["availability_patterns"], "wish": ["staff_wishes"], "role": ["role_bindings"],
              "identity": ["identities"], "rule": ["policies"], "policy": ["policies"], "exception": ["policy_exceptions"],
              "person": ["scheduling_facts"], "calendar": ["scheduling_facts"],
              "shift": ["workspaces", "shifts", "assignments", "shift_services"],
              "issues": ["workspaces", "shifts", "assignments", "shift_services"]}.get(family, [])
    result = {}
    for table in tables:
        rows = []
        for source in state[table]:
            if (table == "availability" and source["retracted"] or table == "role_bindings" and source["revoked"]
                    or table == "staff_wishes" and source["status"] != "open"
                    or table in {"policies", "scheduling_facts", "availability_patterns", "policy_exceptions"}
                    and source["status"] != ("approved" if table == "policy_exceptions" else "active")):
                continue
            row = {k: v for k, v in source.items() if k not in {"revision", "created_at"}}
            if table == "availability_patterns":
                # Reapproval after Undo is a new decision; its date does not change effective availability.
                row.pop("decided_at", None)
            if table == "workspaces":
                row = {k: row[k] for k in ("id", "name", "starts_on", "ends_on", "status", "target_overrides",
                                           "rule_adjustments", "accepted_issues", "replaces_publication_id")}
            rows.append(row)
        result[table] = sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))
    return result


def freeze_clock(monkeypatch):
    from packages.domain import time as clock
    fixed = clock.now() - clock._offset

    class FrozenDatetime(datetime):
        # Shared clock caches outlive this patch; SQLite must receive ordinary datetimes.
        fromisoformat = staticmethod(datetime.fromisoformat)
        combine = staticmethod(datetime.combine)

        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr(clock, "datetime", FrozenDatetime)


def test_equivalence_clock_preserves_database_bindable_cached_timestamps(h, monkeypatch):
    from packages.domain import time as clock
    with monkeypatch.context() as patched:
        freeze_clock(patched)
        stamps = [clock.aware("2035-02-03T05:17:00+00:00"),
                  clock.local_midnight(date(2035, 2, 3), "UTC"),
                  clock.local_stamp(date(2035, 2, 3), "05:17", "UTC")]
    assert clock.aware("2035-02-03T05:17:00+00:00") is stamps[0]
    with h.app.state.engine.connect() as db:
        for stamp in stamps:
            assert clock.aware(db.scalar(select(literal(stamp)))) == stamp


@pytest.mark.parametrize("kind", s.BULK_ITEM_KINDS)
def test_every_bulk_kind_preserves_single_records_and_history(h, monkeypatch, kind):
    role, single = case(h, kind)
    client = h.client(role)
    freeze_clock(monkeypatch)
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies["rota_session"], client.cookies["rota_csrf"], write=True, settings=h.settings)
        db.info["receipt_id"] = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        db.info["correlation_id"] = "single-bulk-equivalence"
        before = persisted(db)
        # Use repeatable IDs in both executions, so histories, foreign keys and validation fingerprints are comparable.
        with monkeypatch.context() as patched:
            ids = count(1)
            patched.setattr(uuid, "uuid4", lambda: uuid.UUID(int=next(ids)))
            with db.begin_nested() as branch:
                require_role(actor, *command_roles(single))
                dispatch(db, actor, single, h.settings)
                db.flush()
                raw = persisted(db)
                assert raw != before, "The single command must actually change persisted state"
                ordinary = canonical(raw, before)
                branch.rollback()
        with monkeypatch.context() as patched:
            ids = count(1)
            patched.setattr(uuid, "uuid4", lambda: uuid.UUID(int=next(ids)))
            payload = deepcopy(single.payload)
            target, revision = single.target_id, single.expected_revision
            if kind in {"rotation.remove", "rotation.restore"}:
                [ref] = payload.pop("items")
                target, revision = ref["id"], ref["revision"]
            item = {"item_kind": kind, "target_id": target, "expected_revision": revision, "payload": payload}
            bulk = command("bulk.apply", {"items": [item]})
            result = dispatch(db, actor, bulk, h.settings)
            db.flush()
            batched = canonical(persisted(db), before)
        for table in ordinary:
            assert batched[table] == ordinary[table], (table,
                [row for row in batched[table] if row not in ordinary[table]],
                [row for row in ordinary[table] if row not in batched[table]])
        if kind == "person.anonymise":
            assert result["undo"] is None and result["undoable"] is False
            assert result["undo_partial"] is False
            assert result["reversible_count"] == 0 and result["irreversible_count"] == 1
        if result.get("undo") and result["undo"]["payload"]["items"]:
            # The normal command boundary stores this receipt; availability reverses bind its recorded operation.
            db.add(m.CommandReceipt(id=db.info["receipt_id"], department_id=actor.department_id,
                                    actor_id=actor.identity_id, command_kind="bulk.apply", required_roles=list(actor.roles),
                                    key="equivalence-original", payload_hash="0" * 64, result=json_value(result)))
            db.flush()
            db.info["receipt_id"] = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
            undo = s.Command.model_validate(result["undo"])
            undone = dispatch(db, actor, undo, h.settings)
            db.flush()
            assert undone["count"] > 0, "The server-generated Undo must execute its reverse commands"
            assert effective_tables(persisted(db), kind) == effective_tables(before, kind)
