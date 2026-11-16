"""Alpha acceptance: actual commands, scoped rules, rotation boundaries and privacy."""
import pytest
from packages.domain.time import aware
from packages.domain.validation import evaluate


def programme(h):
    p = h.configure("programmes", {"name": "Family medicine residency", "starts_on": "2026-01-01"})["record"]
    member = h.configure("programme_memberships", {"person_id": h.seed["people"][0], "programme_id": p["id"],
        "starts_on": "2026-10-01", "ends_on": "2026-11-01"}, role="coordinator")["record"]
    cohort = h.configure("cohorts", {"name": "Current residents", "programme_id": p["id"],
        "starts_on": "2026-01-01", "ends_on": "2027-01-01"})["record"]
    return p, member, cohort


def test_programme_eligibility_uses_reviewed_roster_and_preserves_existing_permissions(h):
    p = h.configure('programmes', {'name': 'Test learners', 'starts_on': '2026-01-01'})['record']
    people = h.seed['people'][:2]
    for person_id in people:
        h.configure('programme_memberships', {'person_id': person_id, 'programme_id': p['id'],
            'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}, role='coordinator')
    services = [h.configure('services', {'name': f'Test service {i}', 'starts_on': '2026-01-01'})['record']['id']
                for i in range(2)]
    payload = {'programme_id': p['id'], 'person_ids': people, 'service_ids': services,
               'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'reason': 'Reviewed test-only permissions'}
    h.command('coordinator', 'eligibility.programme', payload, status=403)
    first = h.command('credentialer', 'eligibility.programme', payload)
    assert (first['created_count'], first['already_current_count']) == (4, 0)
    repeat = h.command('credentialer', 'eligibility.programme', payload)
    assert (repeat['created_count'], repeat['already_current_count']) == (0, 4)
    assert len([r for r in h.get('credentialer', '/api/catalogues')['eligibilities']
                if r['person_id'] in people and r['service_id'] in services]) == 4
    third = h.seed['people'][2]
    h.configure('programme_memberships', {'person_id': third, 'programme_id': p['id'],
        'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}, role='coordinator')
    h.command('credentialer', 'eligibility.programme', payload, status=409)
    assert len([r for r in h.get('credentialer', '/api/catalogues')['eligibilities']
                if r['person_id'] == third and r['service_id'] in services]) == 0


def test_programme_capacity_is_shared_by_drafts_allocation_and_publication(h):
    from packages.domain.generation import generate
    p, _, _ = programme(h)
    h.configure('programme_memberships', {'person_id': h.seed['people'][1], 'programme_id': p['id'],
        'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}, role='coordinator')
    h.policy({'kind': 'maximum_participants', 'maximum': 1}, {'programme_id': p['id'],
        'service_id': h.seed['services'][0], 'weekdays': [6], 'start_time': '09:00', 'end_time': '10:00'})
    result = h.shift(h.workspace(), people=h.seed['people'][:2])
    limits = [f for f in result['validation']['findings'] if f['code'] == 'SCOPED_PARTICIPANT_LIMIT']
    assert len(limits) == 1 and limits[0]['detail'] == {'maximum': 1, 'scheduled': 2, 'rule_severity': 'mandatory', 'rule_strength': 'medium', 'rule_tier': 2}
    assert (aware(limits[0]['end']) - aware(limits[0]['start'])).total_seconds() == 3600
    h.lifecycle(result['workspace'], 'submit', status=422)
    data = h.snapshot(result['workspace'])
    data['availability'].append({'id': '00000000-0000-4000-8000-000000000054',
        'person_id': h.seed['people'][0], 'kind': 'recorded_leave',
        'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00'})
    assert 'SCOPED_PARTICIPANT_LIMIT' in {f['code'] for f in evaluate(data)['findings']}
    h.policy({'kind': 'minimum_cover', 'minimum': 2})
    w = h.shift(h.workspace(), people=[])['workspace']
    candidate = generate(h.snapshot(w))
    assert candidate['status'] == 'candidate_feasible'
    assert len(candidate['additions']) == 2
    assert sum(a['participant']['person_id'] in h.seed['people'][:2] for a in candidate['additions']) == 1


def alpha_journey(h):
    p, member, cohort = programme(h)
    rotation = {"person_id": h.seed["people"][0], "cohort_id": cohort["id"], "service_id": h.seed["services"][0],
        "starts_on": "2026-10-05", "ends_on": "2026-10-07", "department_available": False}
    h.configure("rotations", rotation, role="coordinator")
    w = h.workspace()
    w = h.shift(w)["workspace"]  # Sunday is outside the away rotation.
    h.policy({"kind": "max_duties", "maximum": 1, "window": "week"}, {
        "programme_id": p["id"], "category_id": h.seed["category"], "person_id": h.seed["people"][0],
        "service_id": h.seed["services"][0], "assignment_type_id": h.seed["type"], "weekdays": [6]})
    assert h.detail(w)["workspace"]["id"] == w["id"]
    assert evaluate(h.snapshot(w))["valid"]
    w = h.lifecycle(w, "submit")["workspace"]
    h.lifecycle(w, "approve", "coordinator", status=403)
    w = h.lifecycle(w, "approve", "approver")["workspace"]
    h.configure("programme_memberships", {"person_id": member["person_id"], "programme_id": p["id"],
        "starts_on": "2026-10-01", "ends_on": "2026-11-02"}, member["id"], member["revision"], role="coordinator")
    h.lifecycle(w, "publish", status=409)
    w = h.lifecycle(w, "reopen")["workspace"]
    h.publish(w)
    url = "/api/calendar?start=2026-10-04&end=2026-10-11"
    team = h.get("coordinator", url)
    own = h.get("staff", url)
    assert len(team["people"]) == 3
    assert {r["id"] for r in own["people"]} == {h.seed["people"][0]}
    assert {r["kind"] for r in own["events"]} == {"rotation", "duty"}
    assert all(r["person_id"] == h.seed["people"][0] for r in own["events"])
    assert "private_reason" not in str(own)
    h.get("staff", url + "&person_id=" + h.seed["people"][1], status=403)
    h.get("staff", url + "&workspace_id=" + w["id"], status=403)
    h.get("staff", "/api/calendar?start=2026-10-04&end=2027-10-11", status=422)


def test_alpha_workflow(h):
    alpha_journey(h)


def test_calendar_clinic_filters_and_personal_labels_use_issued_metadata(h):
    service = next(s for s in h.get('admin', '/api/catalogues')['services'] if s['id'] == h.seed['services'][0])
    h.configure('services', {'name': 'Issued virtual clinic', 'starts_on': service['starts_on'], 'mode': 'virtual'}, service['id'], service['revision'])
    h.publish(h.shift(h.workspace())['workspace'])
    changed = next(s for s in h.get('admin', '/api/catalogues')['services'] if s['id'] == service['id'])
    h.configure('services', {'name': 'Renamed current clinic', 'starts_on': service['starts_on'], 'mode': 'physical'}, changed['id'], changed['revision'])
    url = '/api/calendar?start=2026-10-04&end=2026-10-05'
    own = h.get('staff', url + '&mode=virtual')['events']
    assert h.get('staff', url)['sessions'] == []
    assert len(own) == 1
    assert own[0]['location_name']
    assert own[0]['services'] == [{'id': service['id'], 'name': 'Issued virtual clinic', 'mode': 'virtual'}]
    assert all(e['person_id'] == h.seed['people'][0] for e in own)
    assert h.get('staff', url + '&mode=physical')['events'] == []
    assert h.get('coordinator', url + '&service_id=' + h.seed['services'][1])['events'] == []
    h.get('staff', url + '&mode=invalid', status=422)


def test_calendar_shows_empty_draft_sessions_only_to_team(h):
    w = h.shift(h.workspace(), people=[])['workspace']
    data = h.get('coordinator', '/api/calendar?start=2026-10-04&end=2026-10-05&workspace_id=' + w['id'])
    assert len(data['sessions']) == 1 and data['sessions'][0]['assigned_people'] == 0
    assert data['events'] == []


@pytest.mark.postgres
def test_alpha_workflow_postgres(pg):
    alpha_journey(pg)


def test_rotation_boundaries_and_shared_staffing(h):
    _, _, cohort = programme(h)
    r = h.configure("rotations", {"person_id": h.seed["people"][0], "cohort_id": cohort["id"],
        "service_id": h.seed["services"][0], "starts_on": "2026-10-05", "ends_on": "2026-10-06",
        "department_available": False})["record"]
    w = h.workspace()
    w = h.shift(w, start="2026-10-04T23:00:00+03:00", end="2026-10-05T01:00:00+03:00")["workspace"]
    result = evaluate(h.snapshot(w))
    assert "EXTERNAL_ROTATION" in {f["code"] for f in result["findings"]}
    assert any(c["available_eligible"] == 0 for c in result["coverage"])
    h.configure("rotations", {k: r[k] for k in ("person_id", "cohort_id", "service_id", "starts_on", "ends_on")} | {
        "department_available": True}, r["id"], r["revision"])
    assert evaluate(h.snapshot(w))["valid"]


def test_combined_scope_does_not_match_sibling_person_or_day(h):
    p, _, _ = programme(h)
    h.policy({"kind": "restriction", "weekdays": [0,1,2,3,4,5,6]}, {
        "programme_id": p["id"], "weekdays": [6], "start_time": "09:00", "end_time": "10:00",
        "assignment_type_id": h.seed["type"], "service_id": h.seed["services"][0]})
    w = h.workspace(); w = h.shift(w, seed_restricted=True)["workspace"]
    result = evaluate(h.snapshot(w))
    assert "RESTRICTION" in {f["code"] for f in result["findings"]}
    assert any(c["available_eligible"] == 0 for c in result["coverage"])
    other = h.workspace(); other = h.shift(other, people=[h.seed["people"][1]])["workspace"]
    assert evaluate(h.snapshot(other))["valid"]
    monday = h.workspace(); monday = h.shift(monday, start="2026-10-05T08:00:00+03:00", end="2026-10-05T12:00:00+03:00")["workspace"]
    assert evaluate(h.snapshot(monday))["valid"]


def test_rank_cover_off_duty_and_programme_quota(h):
    p, _, _ = programme(h)
    h.policy({"kind": "minimum_cover", "minimum": 2}, {"category_id": h.seed["category"], "weekdays": [6]})
    h.policy({"kind": "block_quota", "starts_on": "2026-10-01", "ends_on": "2026-11-01",
        "metric": "duties", "comparison": "maximum", "amount": 0}, {"programme_id": p["id"]})
    w=h.workspace();w=h.shift(w, people=h.seed["people"][:2])["workspace"]
    result=evaluate(h.snapshot(w))
    assert result["quota_report"]["rows"][0]["actual"] == 1
    assert len(result["quota_report"]["rows"]) == 1
    assert "SCOPED_COVERAGE_GAP" not in {f["code"] for f in result["findings"]}
    h.command("coordinator", "availability.record", {"person_id": h.seed["people"][1], "kind": "off_duty",
        "start": "2026-10-04T00:00:00+03:00", "end": "2026-10-05T00:00:00+03:00", "private_reason": "Private off-duty reason"})
    assert "SCOPED_COVERAGE_GAP" in {f["code"] for f in evaluate(h.snapshot(w))["findings"]}


def test_overnight_scope_preserves_separate_restriction_windows(h):
    h.policy({"kind": "restriction", "weekdays": list(range(7))}, {
        "start_time": "20:00", "end_time": "06:00"})
    w = h.shift(h.workspace(), start="2026-10-04T05:00:00+03:00", end="2026-10-04T21:00:00+03:00", seed_restricted=True)["workspace"]
    findings = [f for f in evaluate(h.snapshot(w))["findings"] if f["code"] == "RESTRICTION"]
    assert len(findings) == 2
    assert sum((aware(f["end"]) - aware(f["start"])).total_seconds() for f in findings) == 2 * 3600


def test_rotation_types_are_shared_dated_configuration(h):
    _, _, cohort = programme(h)
    typ = h.configure('rotation_types', {'name': 'Procedure and urgent care', 'starts_on': '2026-10-01',
        'ends_on': '2026-11-01'}, role='coordinator')['record']
    value = {'person_id': h.seed['people'][0], 'cohort_id': cohort['id'],
        'rotation_type_id': typ['id'], 'starts_on': '2026-10-04', 'ends_on': '2026-10-18'}
    rotation = h.configure('rotations', value, role='coordinator')['record']
    assert rotation['rotation_type_id'] == typ['id']
    snapshot = h.snapshot(h.workspace())
    assert snapshot['rotation_types'][typ['id']]['name'] == 'Procedure and urgent care'
    assert next(r for r in snapshot['rotations'] if r['id'] == rotation['id'])['rotation_type_id'] == typ['id']
    h.command('coordinator', 'catalogue.create', {'catalogue': 'rotations', 'value': {**value, 'ends_on': '2026-11-02'},
        'reason': 'Synthetic out-of-window assignment'}, status=422)
    h.command('staff', 'catalogue.create', {'catalogue': 'rotation_types',
        'value': {'name': 'Unauthorised type', 'starts_on': '2026-10-01'}, 'reason': 'Permission boundary'}, status=403)


@pytest.mark.postgres
def test_rotation_types_postgres(pg):
    test_rotation_types_are_shared_dated_configuration(pg)
