"""Rules applied to one session: the validator's own scope test plus the stored rota check."""


def test_shift_rules_lists_applicable_rules_with_check_status(h):
    rule = h.policy({"kind": "min_rest", "hours": 12})
    person = h.seed["people"][0]
    w = h.workspace("Rules applied", "2026-10-04", "2026-10-11")
    w = h.shift(w, "2026-10-04T08:00:00+03:00", "2026-10-04T20:00:00+03:00", people=[person])["workspace"]
    w = h.shift(w, "2026-10-04T22:00:00+03:00", "2026-10-05T06:00:00+03:00", people=[person], location=1)["workspace"]

    w = h.lifecycle(h.detail(w)["workspace"], "validate")["workspace"]
    statuses = []
    for shift in h.detail(w)["shifts"]:
        value = h.get("coordinator", f"/api/workspaces/{w['id']}/shifts/{shift['id']}/rules")
        assert value["checked"] is True and value["fresh"] is True and value["active_rules"] >= 1
        rows = value["people"][0]["rules"]
        assert rule["id"] in [r["id"] for r in rows]
        statuses.append(next(r for r in rows if r["id"] == rule["id"])["status"])
    # Two sessions two hours apart break a 12-hour rest rule; the check names it on at least one side.
    assert "broken" in statuses
    assert all(s in {"broken", "passes"} for s in statuses)


def test_shift_rules_requires_planning_role_and_known_session(h):
    w = h.workspace("Rules applied access", "2026-10-04", "2026-10-11")
    w = h.shift(w)["workspace"]
    sid = h.detail(w)["shifts"][0]["id"]
    h.get("staff", f"/api/workspaces/{w['id']}/shifts/{sid}/rules", status=403)
    h.get("coordinator", f"/api/workspaces/{w['id']}/shifts/00000000-0000-4000-8000-000000000000/rules", status=404)


def test_person_short_code_and_tags_round_trip_and_unique(h):
    people = h.get("admin", "/api/catalogues")["people"]
    first, second = people[0], people[1]
    base = {k: first[k] for k in ("display_name", "active", "title", "work_phone", "work_extension", "staff_number", "directory_section", "directory_order") if k in first}  # legacy phone fields are absent without contacts
    saved = h.configure("people", {**base, "short_code": "MKT", "tags": ["Part-time", "Locum"]}, first["id"], first["revision"])["record"]
    assert saved["short_code"] == "MKT" and saved["tags"] == ["Part-time", "Locum"]
    other = {k: second[k] for k in base}
    h.configure("people", {**other, "short_code": "MKT"}, second["id"], second["revision"], status=409)
    h.configure("people", {**other, "short_code": "mk t"}, second["id"], second["revision"], status=422)
    h.configure("people", {**other, "tags": ["A", "a"]}, second["id"], second["revision"], status=422)


def test_duty_short_code_round_trip_snapshot_and_department_uniqueness(h):
    current = next(t for t in h.get("admin", "/api/catalogues")["assignment_types"] if t["id"] == h.seed["type"])
    value = {key: current[key] for key in ("name", "description", "starts_on", "ends_on", "active", "definition")}
    saved = h.configure("assignment_types", {**value, "short_code": "SYN"})["record"]
    assert saved["short_code"] == "SYN"
    h.configure("assignment_types", {**value, "name": "Synthetic duplicate duty code", "short_code": "SYN"}, status=422)
    preserved = h.configure("assignment_types", value, saved["id"], saved["revision"])["record"]
    assert preserved["short_code"] == "SYN"
    workspace = h.shift(h.workspace("Configured duty code"), typ=saved["id"])["workspace"]
    shift = h.detail(workspace)["shifts"][0]
    assert shift["type_snapshot"]["short_code"] == "SYN"


def test_duty_short_code_duplicates_follow_department_setting(h):
    department = h.get("admin", "/api/me")["department"]
    settings = {**department["display_settings"], "avoid_duplicate_codes": False,
                "reason": "Allow duplicate duty codes for this synthetic department"}
    h.command("admin", "department.display", settings, department["id"], department["revision"])
    base = {"name": "Synthetic duplicate setting duty", "short_code": "DUP", "starts_on": "2020-01-01",
            "definition": {}}
    first = h.configure("assignment_types", base)["record"]
    second = h.configure("assignment_types", {**base, "name": "Synthetic duplicate setting duty two"})["record"]
    assert first["short_code"] == second["short_code"] == "DUP"


def test_shift_history_lists_who_changed_the_session(h):
    people = h.seed["people"]
    w = h.shift(h.workspace("History", "2026-10-04", "2026-10-11"), people=[people[0]])["workspace"]
    shift = h.detail(w)["shifts"][0]
    changed = h.command("coordinator", "shift.update", {"workspace_id": w["id"], "shift_id": shift["id"], "assignment_type_id": shift["assignment_type_id"],
        "location_id": shift["location_id"], "service_ids": shift["service_ids"], "start": shift["start"], "end": shift["end"], "attributes": shift["attributes"],
        "participants": [{"person_id": people[1], "start": shift["start"], "end": shift["end"]}]}, revision=w["revision"])["workspace"]
    history = h.get("coordinator", f"/api/workspaces/{changed['id']}/shifts/{shift['id']}/history")["entries"]
    assert len(history) == 2 and history[-1]["changes"] == ["Session created"]
    assert any(c.startswith("Added ") for c in history[0]["changes"]) and any(c.startswith("Removed ") for c in history[0]["changes"])
    h.get("staff", f"/api/workspaces/{changed['id']}/shifts/{shift['id']}/history", status=403)



def test_department_display_defaults_are_admin_only_and_visible_to_everyone(h):
    me = h.get("admin", "/api/me")
    dept = me["department"]
    value = {"names_default": "code", "ignore_al_prefix": True, "avoid_duplicate_codes": True,
             "tags": [{"label": "PT", "name": "Part-time"}, {"label": "LOC", "name": "Locum"}], "reason": "Department display agreement"}
    saved = h.command("admin", "department.display", value, dept["id"], dept["revision"])["department"]
    assert saved["display_settings"]["names_default"] == "code" and len(saved["display_settings"]["tags"]) == 2
    assert h.get("staff", "/api/me")["department"]["display_settings"]["names_default"] == "code"
    h.command("coordinator", "department.display", value, dept["id"], saved["revision"], status=403)
    h.command("admin", "department.display", {**value, "tags": [{"label": "PT", "name": "A"}, {"label": "PT", "name": "B"}]}, dept["id"], saved["revision"], status=422)
    # The rule catalogue flag is off by default and survives saving the other settings without it.
    assert saved["display_settings"]["rule_catalogue"] is False
    saved = h.command("admin", "department.display", {**value, "rule_catalogue": True}, dept["id"], saved["revision"])["department"]
    saved = h.command("admin", "department.display", {**value, "names_default": "short"}, dept["id"], saved["revision"])["department"]
    assert saved["display_settings"]["rule_catalogue"] is True and saved["display_settings"]["names_default"] == "short"
