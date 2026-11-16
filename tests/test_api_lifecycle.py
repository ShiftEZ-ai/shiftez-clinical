import copy
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading
from sqlalchemy import select, text, func
from sqlalchemy.exc import IntegrityError
import pytest
from apps.api import models as m
from apps.api.db import transaction
from apps.worker.main import process_one


def codes(result):
    return {f["code"] for f in result["validation"]["findings"]}


def test_real_shared_publication_and_precise_absence_overlay(h):
    w=h.workspace(); saved=h.shift(w); assert saved["validation"]["valid"]
    published=h.publish(saved["workspace"]); pid=published["publication_id"]
    staff=h.get("staff","/api/publications/"+pid)
    assert staff["publication"]["sequence"]==1
    assert len(staff["snapshot"]["shifts"])==1
    baseline=copy.deepcopy(staff["snapshot"])
    with transaction(h.app.state.engine) as db:
        issued_snapshot=copy.deepcopy(db.get(m.Publication,pid).snapshot)
    result=h.command("coordinator","availability.record",{"person_id":h.seed["people"][0],"start":"2026-10-04T10:00:00+03:00","end":"2026-10-04T11:00:00+03:00","kind":"actual_absence","private_reason":"Private detail must not leak"})
    assert "private_reason" not in result["record"]
    live=h.get("coordinator",f"/api/publications/{pid}/coverage")
    assert "Private detail" not in str(live)
    evaluation=live.get("evaluation",live.get("validation"))
    assert evaluation and not evaluation["valid"]
    gaps=[g for g in evaluation["coverage"] if g["status"]=="gap"]
    assert len(gaps)==1
    assert gaps[0]["start"]=="2026-10-04T07:00:00+00:00"
    assert gaps[0]["end"]=="2026-10-04T08:00:00+00:00"
    current=h.get("staff","/api/publications/"+pid)["snapshot"]
    flag,=current['shifts'][0]['double_booking_conflicts']
    assert (flag['code'],flag['label'],flag['start'],flag['end']) == (
        'UNAVAILABLE','Unavailable',gaps[0]['start'],gaps[0]['end'])
    assert 'leave' not in flag and 'Private detail' not in str(current)
    # The response has live flags; all issued fields and the stored snapshot stay frozen.
    current['shifts']=[{k:v for k,v in row.items() if k!='double_booking_conflicts'} for row in current['shifts']]
    assert current==baseline
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Publication,pid).snapshot==issued_snapshot
    report=h.get("staff","/api/reports/"+pid)
    assert report["rows"][0]["scheduled_hours"]==4
    assert report["attendance_basis"].startswith("none")


def test_repeated_command_is_one_result_and_conflicting_reuse_is_rejected(h):
    key=str(uuid.uuid4());payload={"name":"Retry case","starts_on":"2026-10-04","ends_on":"2026-10-11"}
    first=h.command("coordinator","workspace.create",payload,key=key)
    second=h.command("coordinator","workspace.create",payload,key=key)
    assert second==first
    assert len(h.get("coordinator","/api/workspaces")["workspaces"])==1
    rejected=h.command("coordinator","workspace.create",{**payload,"name":"Different"},key=key,status=409)
    assert rejected["error"]["code"]=="IDEMPOTENCY_CONFLICT"
    receipt=h.get("coordinator","/api/commands/"+key)
    assert receipt


def test_stale_edit_does_not_overwrite_saved_content(h):
    w=h.workspace();h.shift(w)
    failed=h.shift(w,people=[h.seed["people"][1]],status=409)
    assert failed["error"]["code"]=="STALE_REVISION"
    assert len(h.detail(w)["shifts"])==1


def test_alternative_drafts_are_not_official_conflicts(h):
    a=h.shift(h.workspace("Alternative A"))
    b=h.shift(h.workspace("Alternative B"))
    assert a["validation"]["valid"] and b["validation"]["valid"]


def test_cross_service_overlap_and_adjacent_boundary(h):
    first=h.shift(h.workspace())
    overlap=h.shift(first["workspace"],"2026-10-04T11:00:00+03:00","2026-10-04T14:00:00+03:00",service=1,location=1)
    assert "TIME_OVERLAP" in codes(overlap)
    w=h.workspace("Adjacent independent alternative")
    one=h.shift(w)
    adjacent=h.shift(one["workspace"],"2026-10-04T12:00:00+03:00","2026-10-04T15:00:00+03:00",service=1)
    assert "TIME_OVERLAP" not in codes(adjacent)


@pytest.mark.parametrize("action,role",[("publish","coordinator"),("approve","approver")])
def test_workflow_cannot_skip_submission(h,action,role):
    w=h.shift(h.workspace())["workspace"]
    assert h.lifecycle(w,action,role,status=409)["error"]["code"]=="WORKFLOW_STATE"


def test_changed_inputs_invalidate_approval(h):
    w=h.shift(h.workspace())["workspace"]
    w=h.lifecycle(w,"submit")["workspace"]
    w=h.lifecycle(w,"approve","approver")["workspace"]
    approved_fingerprint=h.snapshot(w)['fingerprint']
    h.configure("categories",{"name":"Unrelated future category","starts_on":"2027-01-01"})
    assert h.snapshot(w)['fingerprint']==approved_fingerprint
    h.command('coordinator','availability.record',{'person_id':h.seed['people'][0],
        'start':'2026-10-04T08:00:00+03:00','end':'2026-10-04T12:00:00+03:00',
        'kind':'actual_absence','private_reason':'Cannot attend the assigned duty'})
    assert h.lifecycle(w,"publish",status=409)["error"]["code"]=="STALE_APPROVAL"
    assert not h.get("coordinator","/api/publications")["publications"]


def test_publish_rejects_leave_approved_after_rota_approval(h):
    w = h.shift(h.workspace())["workspace"]
    w = h.lifecycle(w, "submit")["workspace"]
    w = h.lifecycle(w, "approve", "approver")["workspace"]
    request = h.command("staff", "request.create", {
        "person_id": h.seed["people"][0],
        "start": "2026-10-04T08:00:00+03:00",
        "end": "2026-10-04T12:00:00+03:00",
        "private_reason": "Approved leave must block publication",
    })["record"]
    h.command("approver", "request.decide", {
        "decision": "approve", "reason": "Approve leave before publication",
    }, target=request["id"], revision=request["revision"])
    failed = h.lifecycle(w, "publish", status=409)
    assert failed["error"]["code"] == "STALE_APPROVAL"
    assert not h.get("coordinator", "/api/publications")["publications"]


def test_self_approval_is_rejected_even_with_both_roles(h):
    h.command("admin","role.grant",{"identity_id":h.seed["identities"]["coordinator"],"role":"approver","starts_on":"2020-01-01","reason":"Independent access grant fixture"})
    w=h.shift(h.workspace())["workspace"];w=h.lifecycle(w,"submit")["workspace"]
    assert h.lifecycle(w,"approve",status=403)["error"]["code"]=="SELF_APPROVAL"


def test_revoked_approver_cannot_authorise_publication(h):
    w=h.shift(h.workspace())["workspace"];w=h.lifecycle(w,"submit")["workspace"];w=h.lifecycle(w,"approve","approver")["workspace"]
    bindings=h.get("admin","/api/access")["bindings"]
    binding=next(b for b in bindings if b["role"]=="approver")
    h.command("admin","role.revoke",{"reason":"Access ended"},binding["id"],binding["revision"])
    failed=h.lifecycle(w,"publish",status=409)
    assert failed["error"]["code"]=="APPROVER_REVOKED"
    h.get("approver","/api/me",status=403)


def single_command_winner(h, commands, conflict_code, held_admission_lock):
    client=h.client("coordinator")
    keys=[str(uuid.uuid4()) for _ in commands]
    def send(index):
        return client.post("/api/commands",headers={"idempotency-key":keys[index]},json=commands[index])
    if held_admission_lock:
        # Exercise real SQLite backpressure without waiting out the production request budget.
        winner=send(0)
        assert winner.status_code==200,winner.text
        engine=h.app.state.engine
        original=engine.get_execution_options()['rota_write_settings']
        engine.update_execution_options(rota_write_settings=replace(original,command_write_budget_ms=200))
        try:
            with engine.connect() as holder:
                holder.execute(text("BEGIN IMMEDIATE"))
                loser=send(1)
                assert loser.status_code==503,loser.text
        finally:
            engine.update_execution_options(rota_write_settings=original)
        responses=[winner,loser]
    else:
        barrier=threading.Barrier(2)
        def write(index):
            barrier.wait()
            return send(index)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses=list(pool.map(write,range(2)))
    assert sorted(response.status_code for response in responses) in ([200,409],[200,503]),[r.text for r in responses]
    index=next(i for i,response in enumerate(responses) if response.status_code!=200)
    loser=responses[index]
    assert h.get("coordinator","/api/commands/"+keys[index],status=404)["error"]["code"]=="RECEIPT_NOT_FOUND"
    if loser.status_code==503:
        assert loser.json()["error"]["code"]=="COMMAND_WRITE_TIMEOUT"
        # Both contenders have finished: retry the identical operation once, now against the winner's state.
        loser=send(index)
    assert loser.status_code==409,loser.text
    expected_codes={conflict_code} if isinstance(conflict_code,str) else conflict_code
    assert loser.json()["error"]["code"] in expected_codes,loser.text


@pytest.mark.parametrize("held_admission_lock",[False,True])
def test_concurrent_draft_writers_one_wins(h,held_admission_lock):
    w=h.workspace();h.client("coordinator")
    payload={"workspace_id":w["id"],"assignment_type_id":h.seed["type"],"location_id":h.seed["locations"][0],"service_ids":[h.seed["services"][0]],"start":"2026-10-04T08:00:00+03:00","end":"2026-10-04T12:00:00+03:00","participants":[]}
    command={"kind":"shift.create","payload":payload,"expected_revision":w["revision"]}
    single_command_winner(h,[command,command],"STALE_REVISION",held_admission_lock)
    assert len(h.detail(w)["shifts"])==1


def test_competing_publications_only_one_commits(h):
    competing_publications(h,False)


def test_competing_publications_after_admission_timeout(h):
    competing_publications(h,True)


def competing_publications(h,held_admission_lock):
    candidates=[]
    for i in range(2):
        w=h.shift(h.workspace("Competing "+str(i)))["workspace"]
        w=h.lifecycle(w,"submit")["workspace"]
        candidates.append(h.lifecycle(w,"approve","approver")["workspace"])
    commands=[{"kind":"workspace.publish","target_id":w["id"],"expected_revision":w["revision"],"payload":{"reason":"Competing publication test"}} for w in candidates]
    conflict_code = "STALE_APPROVAL" if held_admission_lock else {"STALE_APPROVAL", "STALE_INPUTS"}
    single_command_winner(h,commands,conflict_code,held_admission_lock)
    pubs=h.get("coordinator","/api/publications")["publications"]
    assert len(pubs)==1 and pubs[0]["current"]


def test_amendment_supersedes_preserves_history_and_renamed_type(h):
    one=h.publish(h.shift(h.workspace())["workspace"])
    original=h.get("coordinator","/api/publications/"+one["publication_id"])
    typ=h.get("admin","/api/catalogues")["assignment_types"][0]
    h.configure("assignment_types",{"name":"Renamed display only","starts_on":typ["starts_on"],"definition":typ["definition"]},typ["id"],typ["revision"])
    historical=h.get("coordinator","/api/publications/"+one["publication_id"])
    assert historical["snapshot"]==original["snapshot"]
    w=h.workspace("Reviewed amendment",base=one["publication_id"])
    current=h.detail(w)
    refreshed=h.command("coordinator","shift.action",{"workspace_id":w["id"],"shift_ids":[s["id"] for s in current["shifts"]],"action":"refresh_types","reason":"Use current type version"},revision=w["revision"])
    two=h.publish(refreshed["workspace"])
    pubs=h.get("coordinator","/api/publications")["publications"]
    assert sorted((p["sequence"],p["current"]) for p in pubs)==[(1,False),(2,True)]
    assert h.get("coordinator","/api/publications/"+one["publication_id"])["snapshot"]==original["snapshot"]


def test_refresh_types_preserves_recurring_series_flags_and_undo(h):
    from .test_series_reconciliation import edit, materialise, template

    series = template(h, days=(0, 1))
    w = materialise(h, series)
    original_rows = h.detail(w)["shifts"]
    w = edit(h, w, original_rows[1], attributes={})
    before = h.detail(w)
    originals = {row["id"]: row for row in before["shifts"]}
    assert [originals[row["id"]]["series_modified"] for row in original_rows] == [False, True]
    assert all(row["series_id"] == series["id"] for row in originals.values())

    typ = h.get("admin", "/api/catalogues")["assignment_types"][0]
    h.configure("assignment_types", {"name": "Refreshed recurring type", "starts_on": typ["starts_on"],
        "definition": typ["definition"]}, typ["id"], typ["revision"])
    refreshed = h.command("coordinator", "shift.action", {"workspace_id": w["id"],
        "shift_ids": list(originals), "action": "refresh_types",
        "reason": "Refresh the recurring duty type snapshot"}, revision=w["revision"])
    updated = {row["id"]: row for row in h.detail(w)["shifts"]}
    for shift_id, original in originals.items():
        assert updated[shift_id]["type_revision"] > original["type_revision"]
        assert updated[shift_id]["series_id"] == original["series_id"]
        assert updated[shift_id]["series_revision"] == original["series_revision"]
        assert updated[shift_id]["series_modified"] == original["series_modified"]
    assert refreshed["workspace"]["revision"] == w["revision"] + 1

    h.command("coordinator", "workspace.undo", {"reason": "Undo the type snapshot refresh"},
        w["id"], refreshed["workspace"]["revision"])
    restored = {row["id"]: row for row in h.detail(w)["shifts"]}
    for shift_id, original in originals.items():
        for key in ("series_id", "series_revision", "series_snapshot", "occurrence_on", "series_modified"):
            assert restored[shift_id][key] == original[key]


def test_notification_worker_deduplicates_and_acknowledgement_is_separate(h):
    h.publish(h.shift(h.workspace())["workspace"])
    for _ in range(3):process_one(h.app.state.engine)
    notes=h.get("staff","/api/notifications")["notifications"]
    assert len(notes)==1 and notes[0]["acknowledged_at"] is None
    acknowledged=h.command("staff","notification.ack",{"reason":"Read notification intentionally"},notes[0]["id"],notes[0]["revision"])
    assert acknowledged["notification"]["read_at"] is not None
    assert acknowledged["notification"]["acknowledged_at"] is not None
    ops=h.get("operator","/api/operations")
    assert ops["outbox"][0]["state"]=="available_in_app"
    assert ops["production_qualified"] is False


def test_pending_request_does_not_block_until_independent_approval(h):
    w=h.shift(h.workspace())["workspace"]
    req=h.command("staff","request.create",{"person_id":h.seed["people"][0],"start":"2026-10-04T09:00:00+03:00","end":"2026-10-04T10:00:00+03:00","private_reason":"Private family arrangement"})["record"]
    valid=h.lifecycle(w,"validate");assert valid["validation"]["valid"]
    h.command("approver","request.decide",{"decision":"approve","reason":"Request independently approved"},req["id"],req["revision"])
    invalid=h.lifecycle(valid["workspace"],"validate");assert "UNAVAILABLE" in codes(invalid)
    assert "Private family arrangement" not in str(h.get("coordinator","/api/availability"))


def test_published_assignments_and_audit_are_immutable_in_database(h):
    published=h.publish(h.shift(h.workspace())["workspace"])
    with transaction(h.app.state.engine) as db:
        aid=db.scalar(select(m.Assignment.id))
    with pytest.raises(IntegrityError), transaction(h.app.state.engine,write=True) as db:
        db.execute(text("UPDATE assignments SET overtime=true WHERE id=:id"),{"id":aid})
    with pytest.raises(IntegrityError), transaction(h.app.state.engine,write=True) as db:
        db.execute(text("DELETE FROM audit_events"))
    assert h.get("coordinator","/api/publications/"+published["publication_id"])["original_validation"]["valid"]


def test_publication_changes_list_the_days_an_amendment_changed(h):
    w = h.shift(h.workspace())["workspace"]
    w = h.shift(w, "2026-10-06T08:00:00+03:00", "2026-10-06T12:00:00+03:00")["workspace"]
    one = h.publish(w)
    assert h.get("coordinator", "/api/publications/" + one["publication_id"] + "/changes")["days"] == []
    amendment = h.workspace("Amend one day", base=one["publication_id"])
    shift = next(s for s in h.detail(amendment)["shifts"] if s["start"].startswith("2026-10-06"))
    changed = h.command("coordinator", "shift.update", {"workspace_id": amendment["id"], "shift_id": shift["id"], "assignment_type_id": shift["assignment_type_id"],
        "location_id": shift["location_id"], "service_ids": shift["service_ids"], "start": shift["start"], "end": shift["end"], "attributes": shift["attributes"],
        "participants": [{"person_id": h.seed["people"][1], "start": shift["start"], "end": shift["end"]}]}, revision=amendment["revision"])
    two = h.publish(changed["workspace"])
    result = h.get("coordinator", "/api/publications/" + two["publication_id"] + "/changes")
    assert result["supersedes_id"] == one["publication_id"]
    assert [(d["date"], d["changed"]) for d in result["days"]] == [("2026-10-06", 1)]
    h.get("staff", "/api/publications/" + two["publication_id"] + "/changes", status=403)


def test_workspace_list_carries_a_short_rota_health_summary(h):
    w = h.shift(h.workspace("Health summary"), people=[])["workspace"]
    h.lifecycle(h.detail(w)["workspace"], "validate")
    row = next(r for r in h.get("coordinator", "/api/workspaces")["workspaces"] if r["id"] == w["id"])
    assert row["health"]["checked"] and row["health"]["open_places"] == 1 and "validation" not in row
