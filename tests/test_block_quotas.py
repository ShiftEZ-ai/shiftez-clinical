"""Constructed expectations for fixed-block commitments; no institutional defaults."""
import copy
import csv
import io
import uuid
from datetime import date
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import BlockQuota, PolicyInput
from packages.domain.validation import evaluate
from packages.domain.quotas import project_report, quota_csv
from .governance_support import identity
from .test_participation_segments import with_relief, at


def rule(**changes):
    return {"kind": "block_quota", "starts_on": "2026-10-01", "ends_on": "2026-11-01",
            "metric": "duties", "comparison": "maximum", "amount": 3, **changes}


def quota(h, *, scope=None, severity="mandatory", exception=False, **changes):
    return h.policy(rule(**changes), scope if scope is not None else {"person_id": h.seed["people"][0]}, severity, exception)


def row(result, person=None):
    rows = result["quota_report"]["rows"]
    return next(r for r in rows if r["person_id"] == person) if person else rows[0]


def codes(result):
    return {f["code"] for f in result["findings"] if f["severity"] == "error"}


def test_rota_target_prorates_canonical_external_rotations_and_exports_calculation(h):
    from .test_alpha_calendar import programme
    p, _, cohort = programme(h)
    h.configure('rotations', {'person_id': h.seed['people'][0], 'cohort_id': cohort['id'],
        'service_id': h.seed['services'][0], 'department_available': False,
        'starts_on': '2026-10-01', 'ends_on': '2026-10-18'}, role='coordinator')
    quota(h, scope={'programme_id': p['id']}, starts_on='2026-01-01', ends_on='2027-01-01',
        comparison='target', severity='advisory', amount=16, prorate_department_days=True, reference_days=28)
    w = h.workspace(start='2026-10-01', end='2026-11-01')
    w = h.shift(w, start='2026-10-18T08:00:00+03:00', end='2026-10-18T12:00:00+03:00')['workspace']
    data = h.snapshot(w)
    result = evaluate(data)
    target = row(result)
    assert (target['starts_on'], target['ends_on'], target['amount'], target['actual']) == ('2026-10-01', '2026-11-01', 9, 1)
    assert target['target_calculation']['department_days'] == 365
    assert target['target_calculation']['default_amount'] == 16
    assert target['target_calculation']['calculated_amount'] == 209
    assert (target['proration']['working_days'], target['proration']['full_working_days']) == (10, 261)
    duplicate = copy.deepcopy(data)
    duplicate['rotations'] += copy.deepcopy(duplicate['rotations'])
    assert row(evaluate(duplicate))['amount'] == 9  # Overlapping evidence never subtracts twice.
    internal = copy.deepcopy(data)
    internal['rotations'][0]['department_available'] = True
    assert row(evaluate(internal))['amount'] == 17  # Annual rate baseline 209, then 21 of 261 working days, rounded up.
    pub = h.publish(w)
    report = h.get('staff', '/api/reports/'+pub['publication_id'])['quotas']
    assert report['rows'][0]['target_calculation'] == target['target_calculation']
    exported = list(csv.DictReader(io.StringIO(quota_csv(report, pub['publication_id'], 1))))
    assert exported[0]['department_days'] == '365' and exported[0]['amount'] == '9'


@pytest.mark.postgres
def test_prorated_target_postgres(pg):
    test_rota_target_prorates_canonical_external_rotations_and_exports_calculation(pg)


def test_rota_target_override_undo_review_publication_and_privacy(h):
    policy = quota(h, scope={}, comparison='target', severity='advisory', amount=16, prorate_department_days=True)
    w = h.shift(h.workspace())['workspace']
    payload = {'policy_id': policy['id'], 'person_id': h.seed['people'][1], 'amount': 6, 'reason': 'Private departmental adjustment'}
    h.command('staff', 'workspace.target_override', payload, w['id'], w['revision'], status=403)
    h.command('coordinator', 'workspace.target_override', {**payload, 'amount': -0.01}, w['id'], w['revision'], status=422)
    updated = h.command('coordinator', 'workspace.target_override', payload, w['id'], w['revision'])
    assert row(updated['validation'], h.seed['people'][1])['amount'] == 6
    assert row(updated['validation'], h.seed['people'][1])['target_calculation']['calculated_amount'] == 18
    w = updated['workspace']
    undone = h.command('coordinator', 'workspace.undo', {'reason': 'Restore calculated target'}, w['id'], w['revision'])
    assert row(undone['validation'], h.seed['people'][1])['amount'] == 5
    w = undone['workspace']
    w = h.command('coordinator', 'workspace.target_override', payload, w['id'], w['revision'])['workspace']
    submitted = h.lifecycle(w, 'submit')
    item, = submitted['review_items']
    assert item['kind'] == 'target_override'
    approved = h.command('approver', 'workspace.approve', {
        'reason': 'Independently approve the rota-only target',
        'decisions': [{'item_id': item['id'], 'kind': 'adjustment', 'accept': True}]},
        w['id'], submitted['workspace']['revision'])
    published = h.lifecycle(approved['workspace'], 'publish')
    personal = h.get('staff', '/api/publications/'+published['publication_id'])['snapshot']
    assert 'target_overrides' not in personal['workspace']
    assert 'Private departmental adjustment' not in str(personal)
    amended = h.workspace(base=published['publication_id'])
    assert amended['target_overrides'][0]['amount'] == 6
    result = h.command('coordinator', 'workspace.target_override', {**payload, 'amount': None}, amended['id'], amended['revision'])
    assert result['workspace']['target_overrides'] == []
    # Untouched duties retain issued checks; the editable draft uses its calculated target.
    assert row(result['validation'], h.seed['people'][1])['amount'] == 6
    assert row(evaluate(h.snapshot(result['workspace'])), h.seed['people'][1])['amount'] == 5
    w = result['workspace']
    w = h.command('coordinator', 'workspace.target_override', payload, w['id'], w['revision'])['workspace']
    h.command('approver', 'policy.retire', {'reason': 'Retire old target default'}, policy['id'], policy['revision'])
    result = h.command('coordinator', 'workspace.target_override', {**payload, 'amount': None}, w['id'], w['revision'])
    assert result['workspace']['target_overrides'] == []


def test_rolling_minimum_checks_intermediate_windows_not_disjoint_blocks(h):
    quota(h, starts_on='2026-10-01', ends_on='2026-11-26', window_days=28, comparison='minimum', amount=1)
    w = h.workspace(start='2026-10-01', end='2026-11-26')
    for day in ('2026-10-01', '2026-11-25'):
        w = h.shift(w, start=day+'T08:00:00+03:00', end=day+'T12:00:00+03:00')['workspace']
    result = evaluate(h.snapshot(w))
    rows = result['quota_report']['rows']
    assert len(rows) == 29
    assert sum(r['status'] == 'met' for r in rows) == 2
    assert sum(r['status'] == 'below_minimum' for r in rows) == 27
    assert {r['actual'] for r in rows if r['starts_on'] == '2026-10-02'} == {0}
    h.lifecycle(w, 'submit', status=422)


def test_rolling_quota_loads_cross_period_publications_and_clips_effective_boundaries(h):
    w = h.workspace(start='2026-10-01', end='2026-10-02')
    w = h.shift(w, start='2026-10-01T08:00:00+03:00', end='2026-10-01T12:00:00+03:00')['workspace']
    h.publish(w)
    quota(h, starts_on='2026-10-01', ends_on='2026-11-26', window_days=28, amount=1)
    w = h.workspace(start='2026-10-28', end='2026-10-29')
    w = h.shift(w, start='2026-10-28T08:00:00+03:00', end='2026-10-28T12:00:00+03:00')['workspace']
    data = h.snapshot(w)
    result = evaluate(data)
    rows = result['quota_report']['rows']
    assert len(rows) == 28 and min(r['starts_on'] for r in rows) == '2026-10-01'
    assert next(r for r in rows if r['starts_on'] == '2026-10-01')['actual'] == 2
    assert 'QUOTA_ABOVE_MAXIMUM' in codes(result)
    data['quota_context']['start'] = '2026-10-28T00:00:00+03:00'
    assert 'QUOTA_CONTEXT_MISSING' in codes(evaluate(data))


def test_allocation_respects_each_rolling_minimum_and_maximum(h):
    from datetime import timedelta
    from packages.domain.generation import generate
    for comparison in ('minimum', 'maximum'):
        quota(h, starts_on='2026-10-04', ends_on='2026-10-11', window_days=3, comparison=comparison, amount=1)
    typ = h.configure('assignment_types', {'name': 'Optional rolling allocation duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': []}})['record']
    w = h.workspace()
    for offset in range(7):
        day = (date(2026, 10, 4) + timedelta(days=offset)).isoformat()
        w = h.shift(w, start=day+'T08:00:00+03:00', end=day+'T12:00:00+03:00', people=[], typ=typ['id'])['workspace']
    result = generate(h.snapshot(w))
    assert result['status'] == 'candidate_feasible'
    assert len(result['additions']) == 3
    assert all(r['actual'] == 1 for r in result['validation']['quota_report']['rows'])


@pytest.mark.postgres
def test_rolling_quota_postgres(pg):
    test_rolling_quota_loads_cross_period_publications_and_clips_effective_boundaries(pg)


@pytest.mark.parametrize("changes", [
    {"ends_on": "2026-10-01"}, {"ends_on": "2028-10-01"}, {"amount": -1}, {"amount": 1.1},
    {"amount": float("inf")}, {"metric": "paid_hours"}, {"comparison": "approximately"},
    {"overtime": "guess"}, {"participation": ["attendee"]}, {"participation": ["staff", "staff"]},
    {"assignment_type_ids": ["not-an-id"]}, {"extra": True},
    {'window_days': 0}, {'window_days': 32}, {'window_days': 2.5},
    {'prorate_department_days': True}, {'reference_days': 0},
])
def test_invalid_quota_contracts_are_refused(changes):
    with pytest.raises(ValidationError): BlockQuota.model_validate(rule(**changes))


def test_target_cannot_be_silently_promoted_to_mandatory_or_excepted():
    base = {"name": "Synthetic target", "owner": "Test owner", "source": "Constructed expectations", "starts_on": "2026-10-01",
            "definition": {"rule": rule(comparison="target"), "severity": "advisory"}}
    assert PolicyInput.model_validate(base).definition.rule.comparison == "target"
    for change in ({"severity": "mandatory"}, {"exception_allowed": True}):
        value = copy.deepcopy(base); value["definition"].update(change)
        with pytest.raises(ValidationError): PolicyInput.model_validate(value)
    for change in ({"starts_on": "2026-10-02"}, {"ends_on": "2026-10-31"}, {"active": False}):
        with pytest.raises(ValidationError): PolicyInput.model_validate({**base, **change})


@pytest.mark.parametrize("comparison,amount,severity,expected,valid", [
    ("maximum", 1, "mandatory", "met", True), ("maximum", 0, "mandatory", "above_maximum", False),
    ("minimum", 2, "mandatory", "below_minimum", False), ("minimum", 0, "mandatory", "met", True),
    ("target", 0, "advisory", "above_target", True), ("target", 2, "advisory", "below_target", True),
    ("maximum", 0, "advisory", "above_maximum", True),
])
def test_comparison_meaning_and_classification_have_real_consumers(h, comparison, amount, severity, expected, valid):
    quota(h, comparison=comparison, amount=amount, severity=severity)
    result = h.shift(h.workspace(start='2026-10-01', end='2026-11-01'))["validation"]
    assert result["valid"] is valid
    assert row(result)["actual"] == 1 and row(result)["status"] == expected
    if expected != "met":
        assert any(f["code"] == "QUOTA_" + expected.upper() for f in result["findings"])


def test_minimum_counts_zero_assignments_and_does_not_depend_on_a_focal_duty(h):
    quota(h, comparison="minimum", amount=1)
    w = h.workspace()
    result = h.lifecycle(w, "validate")["validation"]
    assert row(result)["actual"] == 0 and "QUOTA_BELOW_MINIMUM" in codes(result)


def test_department_quota_overlap_checks_every_rule_and_retains_bound(h):
    from packages.domain.quotas import MAX_POLICIES
    quota(h, amount=1)
    workspace = h.shift(h.workspace())["workspace"]
    data = h.snapshot(workspace)
    template = next(p for p in data["policies"] if p["definition"]["rule"]["kind"] == "block_quota")
    data["policies"] = [p for p in data["policies"] if p is not template]
    for index in range(121):
        policy = copy.deepcopy(template)
        policy["id"] = str(uuid.uuid4())
        if index == 120:
            policy["definition"]["rule"]["amount"] = 0
        data["policies"].append(policy)
    result = evaluate(data)
    assert result["quota_report"]["complete"]
    assert len(result["quota_report"]["rows"]) == 121
    assert "QUOTA_ABOVE_MAXIMUM" in codes(result)
    for _ in range(MAX_POLICIES + 1 - 121):
        policy = copy.deepcopy(template)
        policy["id"] = str(uuid.uuid4())
        data["policies"].append(policy)
    result = evaluate(data)
    assert not result["quota_report"]["complete"]
    assert "QUOTA_SCOPE_LIMIT" in codes(result)


def test_deployment_quota_limit_is_validated_snapshotted_and_invalidates_review(h, monkeypatch):
    from apps.api.settings import Settings
    quota(h)
    quota(h, amount=2)
    workspace = h.workspace()
    before = h.snapshot(workspace)
    monkeypatch.setenv("ROTA_MAX_QUOTA_POLICIES", "1")
    limited = h.snapshot(workspace)
    assert limited["fingerprint"] != before["fingerprint"]
    assert limited["quota_policy_limit"] == 1
    assert "QUOTA_SCOPE_LIMIT" in codes(evaluate(limited))
    monkeypatch.setenv("ROTA_MAX_QUOTA_POLICIES", "2")
    assert evaluate(h.snapshot(workspace))["quota_report"]["complete"]
    # Captured worker inputs retain their limit despite later environment changes.
    assert "QUOTA_SCOPE_LIMIT" in codes(evaluate(limited))
    for invalid in ("0", "-1", "2001", "unlimited", "1.5"):
        monkeypatch.setenv("ROTA_MAX_QUOTA_POLICIES", invalid)
        with pytest.raises(RuntimeError, match="ROTA_MAX_QUOTA_POLICIES"):
            Settings().check()


def test_full_block_includes_official_work_more_than_32_days_away(h):
    quota(h, ends_on="2027-03-01", amount=1)
    first = h.publish(h.shift(h.workspace())["workspace"])
    w = h.workspace(start="2027-02-07", end="2027-02-14")
    result = h.shift(w, start=at("08:00", "2027-02-07"), end=at("12:00", "2027-02-07"))
    assert row(result["validation"])["actual"] == 2
    assert "QUOTA_ABOVE_MAXIMUM" in codes(result["validation"])
    assert first["publication_id"] in {p["publication_id"] for p in result["validation"]["quota_report"]["source_publications"]}
    h.lifecycle(result["workspace"], "submit", status=422)


def test_alternative_drafts_and_cancelled_work_do_not_inflate_a_quota(h):
    quota(h, amount=1)
    alternative = h.shift(h.workspace("Alternative, not official"))["workspace"]
    result = h.shift(h.workspace("Actual candidate"))
    assert row(result["validation"])["actual"] == 1
    data = h.snapshot(result["workspace"])
    data["shifts"][0]["cancelled"] = True
    assert row(evaluate(data))["actual"] == 0
    assert alternative["id"] != result["workspace"]["id"]


def test_amendment_replaces_its_base_instead_of_counting_it_twice(h):
    quota(h, amount=1)
    pub = h.publish(h.shift(h.workspace())["workspace"])
    amendment = h.workspace("Amendment", base=pub["publication_id"])
    result = h.lifecycle(amendment, "validate")["validation"]
    assert row(result)["actual"] == 1 and result["valid"]
    next_pub = h.publish(h.detail(amendment)["workspace"])
    assert h.get("coordinator", "/api/reports/" + next_pub["publication_id"])["quotas"]["rows"][0]["actual"] == 1


@pytest.mark.parametrize("metric,expected", [("duties", 1), ("session_units", 2), ("scheduled_hours", 3.5), ("reserved_hours", 4), ("workload_credit", 7), ("training_credit", 1.75)])
def test_presence_gaps_dont_multiply_duties_and_credits_use_issued_type_rates(h, metric, expected):
    with transaction(h.app.state.engine) as db:
        definition = copy.deepcopy(db.get(m.AssignmentType, h.seed["type"]).definition)
    definition.update(workload_credit_per_hour=2, training_credit_per_hour=.5, session_units=2)
    typ = h.configure("assignment_types", {"name": "Explicit weighted duty", "starts_on": "2020-01-01", "definition": definition})["record"]
    quota(h, metric=metric, amount=100)
    result = h.shift(h.workspace(), people=with_relief(h), typ=typ["id"])
    assert result["validation"]["valid"]
    assert row(result["validation"])["actual"] == expected
    if metric == 'session_units':
        issued = h.publish(result['workspace'])
        report = h.get('staff', '/api/reports/' + issued['publication_id'])['quotas']
        assert report['rows'][0]['actual'] == 2
        csv_result = h.client('staff').get('/api/reports/' + issued['publication_id'] + '?format=quotas')
        assert next(csv.DictReader(io.StringIO(csv_result.text)))['actual'] == '2.0'


def test_legacy_quota_report_projection_preserves_version_and_values():
    legacy = {'metric_version': 'block-quota-v1', 'rows': [{'person_id': 'one', 'actual': 3}],
              'source_publications': ['private-other-publication']}
    projected = project_report(legacy, 'one')
    assert projected == {'metric_version': 'block-quota-v1', 'rows': [{'person_id': 'one', 'actual': 3}]}
    assert legacy['source_publications'] == ['private-other-publication']


def test_legacy_proportional_units_remain_readable_at_quota_boundary(h):
    # New definitions cannot select proportional counting. Old snapshot reports
    # remain reproducible without creating new partial assignments.
    quota(h, metric='session_units', amount=100, starts_on='2026-10-04', ends_on='2026-10-05')
    quota(h, metric='session_units', amount=100, starts_on='2026-10-05', ends_on='2026-10-06')
    result = h.shift(h.workspace(), start=at('22:00'), end=at('02:00', '2026-10-05'))
    snapshot = h.snapshot(result['workspace'])
    snapshot['shifts'][0]['type_snapshot']['definition'].update(session_units=2, session_unit_basis='proportional')
    assert [r['actual'] for r in evaluate(snapshot)['quota_report']['rows']] == [1, 1]


def test_session_weight_change_invalidates_draft_and_preserves_issued_totals(h):
    with transaction(h.app.state.engine) as db:
        definition = copy.deepcopy(db.get(m.AssignmentType, h.seed['type']).definition)
    definition['session_units'] = 2
    value = {'name': 'Weighted clinic', 'starts_on': '2020-01-01', 'definition': definition}
    typ = h.configure('assignment_types', value)['record']
    quota(h, metric='session_units', amount=100)
    issued = h.publish(h.shift(h.workspace(), typ=typ['id'])['workspace'])
    report_path = '/api/reports/' + issued['publication_id']
    before = h.get('coordinator', report_path)['quotas']
    draft = h.shift(h.workspace('Next clinic', start='2026-10-11', end='2026-10-18'),
                    start=at('08:00', '2026-10-11'), end=at('12:00', '2026-10-11'), typ=typ['id'])['workspace']
    old = h.snapshot(draft)
    definition['session_units'] = 3
    h.configure('assignment_types', value, typ['id'], typ['revision'])
    current = h.snapshot(draft)
    assert current['fingerprint'] != old['fingerprint']
    assert not evaluate(current)['valid']  # Existing duty needs explicit type refresh.
    assert h.get('coordinator', report_path)['quotas'] == before
    assert before['rows'][0]['actual'] == 2
    detail = h.detail(draft)
    refreshed = h.command('coordinator', 'shift.action', {'workspace_id': draft['id'],
        'shift_ids': [s['id'] for s in detail['shifts']], 'action': 'refresh_types',
        'reason': 'Apply reviewed session weight'}, revision=detail['workspace']['revision'])
    assert refreshed['validation']['valid']
    assert row(refreshed['validation'])['actual'] == 5  # Issued 2 plus refreshed draft 3.


@pytest.mark.parametrize("overtime,actual", [("include", 2), ("exclude", 1), ("only", 1)])
def test_overtime_selector_has_explicit_counting_meaning(h, overtime, actual):
    quota(h, overtime=overtime)
    w = h.shift(h.workspace())["workspace"]
    data = {"person_id": h.seed["people"][0], "start": at("08:00", "2026-10-05"), "end": at("12:00", "2026-10-05"), "overtime": True}
    result = h.shift(w, start=data["start"], end=data["end"], people=[data])["validation"]
    assert row(result)["actual"] == actual


def test_type_participation_service_and_location_filters(h):
    quota(h, assignment_type_ids=[h.seed["type"]], participation=["supervisor"], scope={
        "person_id": h.seed["people"][0], "service_id": h.seed["services"][0], "location_id": h.seed["locations"][0]})
    w = h.shift(h.workspace())["workspace"]
    data = h.snapshot(w)
    assert row(evaluate(data))["actual"] == 0
    p = data["shifts"][0]["participants"][0]; p["participation"] = "supervisor"
    assert row(evaluate(data))["actual"] == 1
    data["shifts"][0]["location_id"] = h.seed["locations"][1]
    assert row(evaluate(data))["actual"] == 0
    data["shifts"][0]["location_id"] = h.seed["locations"][0]
    data["shifts"][0]["service_ids"] = [h.seed["services"][1]]
    assert row(evaluate(data))["actual"] == 0
    data["policies"][0]["definition"]["rule"]["assignment_type_ids"] = [str(uuid.uuid4())]
    assert row(evaluate(data))["actual"] == 0


def test_shared_services_count_one_duty(h):
    quota(h)
    w = h.shift(h.workspace())["workspace"]
    data = h.snapshot(w)
    data["shifts"][0]["service_ids"] = h.seed["services"]
    assert row(evaluate(data))["actual"] == 1


def test_hours_are_clipped_but_duties_belong_to_start_date(h):
    quota(h, metric="scheduled_hours", amount=2)
    quota(h, metric="duties", amount=1)
    w = h.workspace(start="2026-10-31", end="2026-11-02")
    result = h.shift(w, start=at("22:00", "2026-10-31"), end=at("02:00", "2026-11-01"))["validation"]
    values = {r["metric"]: r["actual"] for r in result["quota_report"]["rows"]}
    assert values == {"scheduled_hours": 2, "duties": 1}
    assert result["valid"]


def test_category_scope_uses_matching_effective_membership_not_other_people(h):
    with transaction(h.app.state.engine) as db:
        category = db.scalar(select(m.Membership.category_id).where(m.Membership.person_id == h.seed["people"][0]))
    quota(h, scope={"category_id": category})
    w = h.shift(h.workspace())["workspace"]
    data = h.snapshot(w)
    subjects = {r["person_id"] for r in evaluate(data)["quota_report"]["rows"]}
    assert h.seed["people"][0] in subjects
    for membership in data["memberships"]:
        if membership["person_id"] == h.seed["people"][0]: membership["ends_on"] = "2026-10-04"
    assert row(evaluate(data), h.seed["people"][0])["actual"] == 0


def test_quota_exception_requires_whole_block_and_independent_person(h):
    policy = quota(h, amount=0, exception=True)
    payload = {"policy_id": policy["id"], "person_id": h.seed["people"][0], "starts_on": "2026-10-01", "ends_on": "2026-10-03", "reason": "Explicit constructed exception"}
    assert h.command("coordinator", "exception.create", payload, status=422)["error"]["code"] == "QUOTA_EXCEPTION_WINDOW"
    payload["ends_on"] = "2026-11-01"
    exc = h.command("coordinator", "exception.create", payload)["record"]
    h.command("approver", "exception.approve", {"reason": "Independent full-block decision"}, exc["id"], exc["revision"])
    result = h.shift(h.workspace())["validation"]
    assert result["valid"] and row(result)["status"] == "excepted"
    assert row(result)["exception_id"] == exc["id"]


def test_missing_partial_oversized_or_corrupt_context_cannot_certify_compliance(h):
    quota(h)
    w = h.shift(h.workspace())["workspace"]
    data = h.snapshot(w)
    for context, code in [({}, "QUOTA_CONTEXT_MISSING"), ({"complete": True}, "QUOTA_CONTEXT_MISSING"),
                          ({"complete": True, "start": "not-a-date", "end": "not-a-date"}, "QUOTA_CONTEXT_MISSING"), ({"complete": True, "limit_exceeded": True}, "QUOTA_SCOPE_LIMIT"),
                          ({"complete": True, "start": "2026-10-03T00:00:00Z", "end": "2026-10-07T00:00:00Z"}, "QUOTA_CONTEXT_MISSING")]:
        changed = copy.deepcopy(data); changed["quota_context"] = context
        result = evaluate(changed)
        assert code in codes(result) and not result["quota_report"]["complete"]
    data["shifts"][0]["participants"][0]["segments"] = {"invalid": True}
    assert "QUOTA_PRESENCE_INVALID" in codes(evaluate(data))


def test_policy_scope_bounds_fail_closed(h, monkeypatch):
    import packages.domain.quotas as q
    quota(h)
    w = h.shift(h.workspace())["workspace"]
    monkeypatch.setattr(q, "MAX_POLICIES", 0)
    assert "QUOTA_SCOPE_LIMIT" in codes(evaluate(h.snapshot(w)))


def test_foreign_or_unknown_type_filter_is_rejected_without_writing_policy(h):
    before = h.get("coordinator", "/api/catalogues")["policies"]
    response = h.command("coordinator", "policy.create", {"name": "Invalid type", "owner": "Test", "source": "Constructed invalid reference", "starts_on": "2020-01-01", "definition": {"rule": rule(assignment_type_ids=[str(uuid.uuid4())])}}, status=404)
    assert response["error"]["code"] == "NOT_FOUND"
    assert h.get("coordinator", "/api/catalogues")["policies"] == before


def test_issued_quota_report_is_immutable_and_staff_never_receive_other_people(h):
    policy = quota(h, scope={}, comparison="target", amount=2, severity="advisory")
    pub = h.publish(h.shift(h.workspace())["workspace"])
    path = "/api/reports/" + pub["publication_id"]
    full = h.get("coordinator", path)["quotas"]
    assert len(full["rows"]) == 3
    assert {r["actual"] for r in full["rows"]} == {0, 1}
    personal = h.get("staff", path)["quotas"]
    assert len(personal["rows"]) == 1 and personal["rows"][0]["person_id"] == h.seed["people"][0]
    assert "source_publications" not in personal and "empty_policy_ids" not in personal
    snap = h.get("staff", "/api/publications/" + pub["publication_id"])["snapshot"]["quota_report"]
    assert snap == personal
    live = h.get("staff", "/api/publications/" + pub["publication_id"] + "/coverage")["evaluation"]
    assert len(live["quota_report"]["rows"]) == 1
    assert live["findings"] == []  # Rule issues belong to operators, even for the staff member's own quota.
    operator = h.get("coordinator", "/api/publications/" + pub["publication_id"] + "/coverage")["evaluation"]
    assert any(f["code"] == "QUOTA_BELOW_TARGET" for f in operator["findings"])
    csv_response = h.client("staff").get(path + "?format=quotas")
    assert csv_response.status_code == 200
    records = list(csv.DictReader(io.StringIO(csv_response.text)))
    assert len(records) == 1 and records[0]["actual"] == "1"
    assert h.seed["people"][1] not in csv_response.text and h.seed["people"][2] not in csv_response.text
    h.command("approver", "policy.retire", {"reason": "Independent retirement"}, policy["id"], policy["revision"])
    assert h.get("coordinator", path)["quotas"] == full
    # The published rota keeps the rules it was approved under; retiring one later does not rewrite it.
    assert "quota_report" in h.get("coordinator", "/api/publications/" + pub["publication_id"] + "/coverage")["evaluation"]


def test_legacy_publications_do_not_receive_retroactive_quota_reports(h):
    pub = h.publish(h.shift(h.workspace())["workspace"])
    quota(h, scope={}, comparison="target", amount=2, severity="advisory")
    path = "/api/reports/" + pub["publication_id"]
    assert "quotas" not in h.get("coordinator", path)
    assert h.client("coordinator").get(path + "?format=quotas").status_code == 404


def test_new_policy_applies_to_drafts_while_published_rotas_keep_their_rules(h):
    first = h.publish(h.shift(h.workspace())["workspace"])
    w = h.workspace(start="2027-02-07", end="2027-02-14")
    h.publish(h.shift(w, start=at("08:00", "2027-02-07"), end=at("12:00", "2027-02-07"))["workspace"])
    draft = h.shift(h.workspace(start="2027-02-14", end="2027-02-21"), start=at("08:00", "2027-02-14"), end=at("12:00", "2027-02-14"))["workspace"]
    policy = h.command("coordinator", "policy.create", {"name": "Long-block limit", "owner": "Test", "source": "Explicit test input", "starts_on": "2020-01-01",
        "definition": {"scope": {"person_id": h.seed["people"][0]}, "rule": rule(ends_on="2027-03-01", amount=1)}})["record"]
    impact = h.get("approver", "/api/policies/" + policy["id"] + "/impact")["impact"]
    assert [i["workspace_id"] for i in impact] == [draft["id"]]
    assert any(f["code"] == "QUOTA_ABOVE_MAXIMUM" for f in impact[0]["findings"])
    h.command("approver", "policy.activate", {"reason": "Independent policy review"}, policy["id"], policy["revision"])
    live = h.get("coordinator", "/api/publications/" + first["publication_id"] + "/coverage")["evaluation"]
    assert not any(f["rule_id"] == policy["id"] for f in live["findings"])


def test_known_same_person_cannot_use_an_alternate_account_to_approve_policy(h):
    person = h.seed["people"][1]
    identity(h, "quota_author", ["coordinator"], person)
    identity(h, "quota_alias", ["approver"], person)
    policy = h.command("quota_author", "policy.create", {"name": "Independence test", "owner": "Test", "source": "Synthetic identity test", "starts_on": "2020-01-01", "definition": {"rule": rule()}})["record"]
    result = h.command("quota_alias", "policy.activate", {"reason": "Same person different account"}, policy["id"], policy["revision"], status=403)
    assert result["error"]["code"] == "SELF_APPROVAL"


def test_known_same_person_cannot_approve_own_exception_with_an_alias(h):
    policy = quota(h, exception=True)
    person = h.seed["people"][0]
    identity(h, "exception_author", ["coordinator"], person)
    identity(h, "exception_alias", ["approver"], person)
    exc = h.command("exception_author", "exception.create", {"policy_id": policy["id"], "person_id": h.seed["people"][0], "starts_on": "2026-10-01", "ends_on": "2026-11-01", "reason": "Synthetic full-block request"})["record"]
    result = h.command("exception_alias", "exception.approve", {"reason": "Same person alternate account"}, exc["id"], exc["revision"], status=403)
    assert result["error"]["code"] == "SELF_APPROVAL"


def test_exception_catalogue_approval_is_actor_scoped_without_polluting_cached_records(h):
    policy = quota(h, exception=True)
    person = h.seed['people'][0]
    identity(h, 'exception_author', ['coordinator', 'approver'], person)
    identity(h, 'exception_alias', ['approver'], person)
    identity(h, 'exception_beneficiary', ['approver'], h.seed['people'][0])
    identity(h, 'exception_independent', ['approver'])
    exc = h.command('exception_author', 'exception.create', {'policy_id': policy['id'], 'person_id': h.seed['people'][0],
        'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'reason': 'Synthetic full-block request'})['record']
    independent = h.client('exception_independent')
    first = independent.get('/api/catalogues')
    assert first.status_code == 200
    assert next(r for r in first.json()['exceptions'] if r['id'] == exc['id'])['can_approve'] is True
    known = ','.join(first.json()['_versions'].values())
    alias = h.client('exception_alias')
    for who in ['exception_author', 'exception_alias', 'exception_beneficiary', 'coordinator']:
        rows = h.get(who, '/api/catalogues')['exceptions']
        assert next(r for r in rows if r['id'] == exc['id'])['can_approve'] is False
    # The same cached rows serve two approvers, but their incremental response and ETag reflect their own action.
    changed = alias.get('/api/catalogues', params={'known': known})
    assert changed.status_code == 200
    assert set(changed.json()) == {'exceptions', '_versions'}
    assert changed.json()['exceptions'][0]['can_approve'] is False
    assert changed.json()['_versions']['exceptions'] != first.json()['_versions']['exceptions']
    denied = alias.get('/api/catalogues', headers={'If-None-Match': first.headers['etag']})
    assert denied.status_code == 200
    assert alias.get('/api/catalogues', headers={'If-None-Match': denied.headers['etag']}).status_code == 304
    assert independent.get('/api/catalogues', headers={'If-None-Match': first.headers['etag']}).status_code == 304
    approved = h.command('exception_independent', 'exception.approve', {'reason': 'Independent full-block decision'}, exc['id'], exc['revision'])['record']
    assert approved['status'] == 'approved'
    assert next(r for r in h.get('exception_independent', '/api/catalogues')['exceptions'] if r['id'] == exc['id'])['can_approve'] is False


@pytest.mark.parametrize('subject', ['identity', 'role'])
def test_exception_catalogue_approval_tracks_explicit_waiver_grant_and_revoke(h, subject):
    policy = quota(h, exception=True)
    person = h.seed['people'][0]
    identity(h, 'exception_author', ['coordinator'], person)
    alias_id = identity(h, 'exception_alias', ['approver'], person)
    exc = h.command('exception_author', 'exception.create', {'policy_id': policy['id'], 'person_id': h.seed['people'][0],
        'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'reason': 'Synthetic full-block request'})['record']
    client = h.client('exception_alias')
    before = client.get('/api/catalogues')
    assert before.json()['exceptions'][0]['can_approve'] is False
    permission_input = {'actions': ['exception.approve'], 'starts_on': '2026-01-01', 'ends_on': '2027-01-01',
        'reason': 'Explicit synthetic exception permission', **({'identity_id': alias_id} if subject == 'identity' else {'role': 'approver'})}
    permission = h.command('admin', 'self_approval.grant', permission_input)['permission']
    allowed = client.get('/api/catalogues', params={'known': ','.join(before.json()['_versions'].values())})
    assert set(allowed.json()) == {'exceptions', '_versions'}
    assert allowed.json()['exceptions'][0]['can_approve'] is True
    assert allowed.json()['_versions']['exceptions'] != before.json()['_versions']['exceptions']
    assert client.get('/api/catalogues', headers={'If-None-Match': before.headers['etag']}).status_code == 200
    refusal = h.command('exception_alias', 'exception.approve', {'reason': 'Routine update'}, exc['id'], exc['revision'], status=422)
    assert refusal['error']['code'] == 'SELF_APPROVAL_REASON'
    h.command('admin', 'self_approval.revoke', {'reason': 'Independent reviewer available'}, permission['id'], permission['revision'])
    revoked = client.get('/api/catalogues', params={'known': ','.join(allowed.json()['_versions'].values())})
    assert revoked.json()['exceptions'][0]['can_approve'] is False
    assert revoked.json()['_versions']['exceptions'] == before.json()['_versions']['exceptions']
    refusal = h.command('exception_alias', 'exception.approve', {'reason': 'Alias is still the proposer'}, exc['id'], exc['revision'], status=403)
    assert refusal['error']['code'] == 'SELF_APPROVAL'
    permitted = h.command('admin', 'self_approval.grant', permission_input)['permission']
    approved = h.command('exception_alias', 'exception.approve', {'reason': 'Explicitly permitted review with a real reason'}, exc['id'], exc['revision'])['record']
    assert approved['status'] == 'approved'
    event = next(e for e in h.get('auditor', '/api/audit')['events'] if e['action'] == 'exception.approve')
    assert event['detail']['self_approved'][0] == {'action': 'exception.approve', 'permission_id': permitted['id']}


@pytest.mark.parametrize('relinked', ['exception_author', 'exception_alias'])
def test_exception_catalogue_approval_rechecks_identity_linkage_with_unchanged_exception(h, relinked):
    policy = quota(h, exception=True)
    author_person = h.seed['people'][1]
    reviewer_person = h.configure('people', {'display_name': 'Independent exception reviewer'})['record']['id']
    identity(h, 'exception_author', ['coordinator'], author_person)
    identity(h, 'exception_alias', ['approver'], reviewer_person)
    exc = h.command('exception_author', 'exception.create', {'policy_id': policy['id'], 'person_id': h.seed['people'][0],
        'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'reason': 'Synthetic full-block request'})['record']
    client = h.client('exception_alias')
    before = client.get('/api/catalogues')
    assert before.json()['exceptions'][0]['can_approve'] is True
    with transaction(h.app.state.engine, write=True) as db:
        linked = db.get(m.Identity, h.seed['identities'][relinked])
        linked.person_id = reviewer_person if relinked == 'exception_author' else h.seed['people'][0]
        linked.revision += 1
    after = client.get('/api/catalogues', params={'known': ','.join(before.json()['_versions'].values())})
    if relinked == 'exception_author':
        # The enterer's person does not own a personal exception.
        assert set(after.json()) == {'_versions'}
        assert after.json()['_versions']['exceptions'] == before.json()['_versions']['exceptions']
        changed = client.get('/api/catalogues', headers={'If-None-Match': before.headers['etag']})
        assert changed.status_code == 200
        assert client.get('/api/catalogues', headers={'If-None-Match': changed.headers['etag']}).status_code == 304
        current = next(row for row in h.get('exception_alias', '/api/catalogues')['exceptions'] if row['id'] == exc['id'])
        assert current['can_approve'] is True and current['revision'] == exc['revision']
        approved = h.command('exception_alias', 'exception.approve', {'reason': 'Independent subject decision'},
                             exc['id'], exc['revision'])['record']
        assert approved['status'] == 'approved'
        event = next(e for e in h.get('auditor', '/api/audit')['events'] if e['action'] == 'exception.approve')
        assert event['detail']['entered_on_behalf_by'] == [h.seed['identities']['exception_author']]
        assert 'self_approved' not in event['detail']
        return
    assert set(after.json()) == {'exceptions', '_versions'}
    assert after.json()['exceptions'][0]['can_approve'] is False
    assert after.json()['exceptions'][0]['revision'] == exc['revision']
    changed = client.get('/api/catalogues', headers={'If-None-Match': before.headers['etag']})
    assert changed.status_code == 200
    assert client.get('/api/catalogues', headers={'If-None-Match': changed.headers['etag']}).status_code == 304
    refusal = h.command('exception_alias', 'exception.approve', {'reason': 'Linked accounts are the same person'}, exc['id'], exc['revision'], status=403)
    assert refusal['error']['code'] == 'SELF_APPROVAL'


def test_csv_escapes_formula_text_and_unknown_report_version_is_rejected(h):
    quota(h)
    report = h.shift(h.workspace())["validation"]["quota_report"]
    report["rows"][0]["policy_name"] = "=HYPERLINK(\"evil\")"
    exported = list(csv.DictReader(io.StringIO(quota_csv(report, "pub", 1))))
    assert exported[0]["policy_name"].startswith("'=HYPERLINK")
    with pytest.raises(ValueError): project_report({**report, "metric_version": "guessed-v99"})


def test_staffing_replacement_options_use_same_maximum_quota(h):
    quota(h, amount=0, scope={"person_id": h.seed["people"][1]})
    w = h.shift(h.workspace())["workspace"]
    shift = h.detail(w)["shifts"][0]
    result = h.client("coordinator").post("/api/planner/staffing-suggestions", json={
        "workspace_id": w["id"], "expected_revision": w["revision"], "shift_id": shift["id"],
        "replace_assignment_id": shift["participants"][0]["id"], "search": "Jordan"})
    assert result.status_code == 200
    option = result.json()["candidates"][0]
    assert option["person_blockers"] == 1 and not option["whole_candidate_valid"]
    assert option["findings"][0]["code"] == "QUOTA_ABOVE_MAXIMUM"
    assert h.detail(w)["shifts"][0]["participants"][0]["person_id"] == h.seed["people"][0]


def test_other_department_and_external_duties_are_not_counted_as_this_department_commitments(h):
    quota(h, metric="scheduled_hours", amount=4)
    w = h.shift(h.workspace())["workspace"]
    data = h.snapshot(w)
    external = copy.deepcopy(data["shifts"][0]); external["id"] = str(uuid.uuid4())
    typ = copy.deepcopy(data["types"][external["assignment_type_id"]]); typ["id"] = str(uuid.uuid4()); typ["department_id"] = str(uuid.uuid4())
    external["assignment_type_id"] = typ["id"]; data["types"][typ["id"]] = typ
    data["shifts"].append(external)
    data["availability"].append({"person_id": h.seed["people"][0], "kind": "external_commitment", "start": at("16:00"), "end": at("20:00"), "id": str(uuid.uuid4())})
    assert row(evaluate(data))["actual"] == 4


def test_quota_local_midnight_dst_ambiguity_is_not_guessed(h):
    # Use a historical repeated midnight rather than a forecast transition.
    quota(h, starts_on="2025-10-01", ends_on="2025-11-02")
    w = h.workspace(start="2025-10-01", end="2025-11-02")
    w = h.shift(w, start="2025-10-04T08:00:00-04:00", end="2025-10-04T12:00:00-04:00")["workspace"]
    data = h.snapshot(w); data["organisation"]["timezone"] = "America/Havana"
    assert "QUOTA_TIME_AMBIGUOUS" in codes(evaluate(data))


def test_quota_subject_report_and_participation_bounds_are_explicit(h, monkeypatch):
    import packages.domain.quotas as q
    quota(h, scope={})
    data = h.snapshot(h.shift(h.workspace())["workspace"])
    for constant in ("MAX_SUBJECTS", "MAX_ROWS", "MAX_PARTICIPANTS", "MAX_SHIFTS"):
        with monkeypatch.context() as patch:
            patch.setattr(q, constant, 0)
            result = evaluate(data)
            assert "QUOTA_SCOPE_LIMIT" in codes(result) and not result["quota_report"]["complete"]


def test_quota_recovery_preserves_issued_values_and_revokes_restored_sessions(h, tmp_path):
    from scripts.backup import backup, restore
    from .conftest import Harness
    quota(h, scope={}, comparison="target", amount=2, severity="advisory")
    pub = h.publish(h.shift(h.workspace())["workspace"])
    before = h.get("coordinator", "/api/reports/" + pub["publication_id"])["quotas"]
    snapshot = tmp_path / "quota.backup"; backup(h.settings.database_url, snapshot)
    target = "sqlite:///" + str(tmp_path / "restored.db")
    restore(snapshot, target, trusted_source=True)
    recovered = Harness(target, h.seed)
    try:
        with transaction(recovered.app.state.engine) as db:
            assert all(session.revoked for session in db.scalars(select(m.Session)))
        assert recovered.get("coordinator", "/api/reports/" + pub["publication_id"])["quotas"] == before
    finally:
        for client in recovered.clients.values(): client.close()
        recovered.app.state.engine.dispose()


def test_incomplete_policy_impact_cannot_be_activated_as_no_findings(h, monkeypatch):
    import packages.domain.quotas as q
    h.shift(h.workspace())
    proposal = h.command("coordinator", "policy.create", {
        "name": "Explicit incomplete impact", "owner": "Test", "source": "Constructed bounds", "starts_on": "2020-01-01",
        "definition": {"rule": rule(), "scope": {"person_id": h.seed["people"][0]}}})["record"]
    monkeypatch.setattr(q, "MAX_POLICIES", 0)
    response = h.command("approver", "policy.activate", {"reason": "Cannot certify unknown impact"}, proposal["id"], proposal["revision"], status=409)
    assert response["error"]["code"] == "QUOTA_IMPACT_UNRESOLVED"
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, proposal["id"]).status == "proposed"


def test_generation_uses_quota_blockers_without_claiming_quota_optimisation(h):
    from packages.domain.generation import generate, MANIFEST
    quota(h, amount=0)
    w = h.shift(h.workspace(), people=[])["workspace"]
    result = generate(h.snapshot(w))
    assert "block_quota" in MANIFEST["supported_rule_kinds"]
    assert result["status"] == "candidate_feasible"
    assert all(a["participant"]["person_id"] != h.seed["people"][0] for a in result["additions"])
    assert "QUOTA_ABOVE_MAXIMUM" not in codes(result["validation"])


def test_generator_fills_an_unmet_minimum_without_bypassing_validation(h):
    from packages.domain.generation import generate
    quota(h, comparison="minimum", amount=1)
    w = h.shift(h.workspace(), people=[])["workspace"]
    result = generate(h.snapshot(w))
    assert result["status"] == "candidate_feasible"
    assert "QUOTA_BELOW_MINIMUM" not in codes(result["validation"])
    assert any(a['participant']['person_id'] == h.seed['people'][0] for a in result['additions'])


def test_generator_does_not_fill_cover_by_violating_every_person_quota(h):
    from packages.domain.generation import generate
    quota(h, amount=0, scope={})
    w = h.shift(h.workspace(), people=[])["workspace"]
    result = generate(h.snapshot(w))
    assert result["status"] == "candidate_with_gaps" and result["additions"] == []


@pytest.mark.parametrize('ceiling,feasible', [(2, True), (1, False)])
def test_generator_uses_session_weights_for_minimum_and_ceiling(h, ceiling, feasible):
    from packages.domain.generation import generate
    with transaction(h.app.state.engine) as db:
        definition = copy.deepcopy(db.get(m.AssignmentType, h.seed['type']).definition)
    definition['session_units'] = 2
    typ = h.configure('assignment_types', {'name': 'Two-unit clinic', 'starts_on': '2020-01-01',
                                         'definition': definition})['record']
    quota(h, metric='session_units', comparison='minimum', amount=2)
    quota(h, metric='session_units', amount=ceiling, scope={})
    workspace = h.shift(h.workspace(), people=[], typ=typ['id'])['workspace']
    result = generate(h.snapshot(workspace))
    assert 'QUOTA_ABOVE_MAXIMUM' not in codes(result['validation'])
    if feasible:
        assert result['status'] == 'candidate_feasible'
        assert len(result['additions']) == 1
        assert result['additions'][0]['participant']['person_id'] == h.seed['people'][0]
        assert 'QUOTA_BELOW_MINIMUM' not in codes(result['validation'])
    else:
        assert result['status'] == 'candidate_with_gaps'
        assert result['additions'] == []
        assert 'QUOTA_BELOW_MINIMUM' in codes(result['validation'])


def test_mixed_rotation_requirements_use_only_matching_placement_days(h, nested=False):
    from .test_alpha_calendar import programme
    _, _, cohort = programme(h)
    person = h.seed['people'][0]
    ucc = h.configure('rotation_types', {'name': 'UCC placement', 'starts_on': '2026-01-01'})['record']
    mixed = h.configure('rotation_types', {'name': 'Procedure-UCC placement', 'starts_on': '2026-01-01'})['record']
    for typ, first, last in ((ucc, '2026-10-01', '2026-10-15'), (mixed, '2026-10-15', '2026-10-29')):
        h.configure('rotations', {'person_id': person, 'cohort_id': cohort['id'], 'service_id': h.seed['services'][0],
            'rotation_type_id': typ['id'], 'starts_on': first, 'ends_on': last}, role='coordinator')
    policies = []
    for typ, service, amount in ((ucc, 0, 16), (mixed, 0, 8), (mixed, 1, 6)):
        selected = {'conditions': {'match': 'all', 'conditions': [{'match': 'all', 'conditions': [
            {'field': 'rotation_type_ids', 'op': 'any', 'values': [typ['id']]}]}]}} if nested else {'rotation_type_id': typ['id']}
        policies.append(quota(h, scope={**selected, 'service_ids': [h.seed['services'][service]]},
            comparison='target', severity='advisory', amount=amount, prorate_department_days=True, reference_days=28))
    w = h.workspace(start='2026-10-01', end='2026-10-29')
    for day, service in (('2026-10-02', 0), ('2026-10-15', 0), ('2026-10-16', 1)):
        w = h.shift(w, start=day+'T08:00:00+03:00', end=day+'T12:00:00+03:00', service=service)['workspace']
    w = h.shift(w, start='2026-10-14T20:00:00+03:00', end='2026-10-15T04:00:00+03:00')['workspace']
    result = evaluate(h.snapshot(w))
    results = {r['policy_id']:r for r in result['quota_report']['rows']}
    assert len(results) == 3
    assert [results[p['id']]['amount'] for p in policies] == [9, 5, 4]
    assert [results[p['id']]['actual'] for p in policies] == [2, 1, 1]  # Overnight duty belongs to its starting rotation.
    assert all(r['person_id'] == person and r['target_calculation']['department_days'] == 31 for r in results.values())
    assert [results[p['id']]['target_calculation']['calculated_amount'] for p in policies] == [18, 9, 7]
    assert all((r['proration']['working_days'], r['proration']['full_working_days']) == (10, 21) for r in results.values())
    published = h.publish(w)
    personal = h.get('staff', '/api/reports/'+published['publication_id'])['quotas']['rows']
    assert sorted(r['amount'] for r in personal) == [4, 5, 9]


@pytest.mark.postgres
def test_mixed_rotation_requirements_postgres(pg):
    test_mixed_rotation_requirements_use_only_matching_placement_days(pg)


@pytest.mark.parametrize('fixture_name', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_mixed_rotation_requirements_nested_selectors(request, fixture_name):
    test_mixed_rotation_requirements_use_only_matching_placement_days(request.getfixturevalue(fixture_name), nested=True)


@pytest.mark.parametrize('fixture_name', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_daily_programme_targets_respect_education_weekends_and_membership(request, fixture_name):
    h = request.getfixturevalue(fixture_name)
    p = h.configure('programmes', {'name': 'Daily pediatric fixture', 'starts_on': '2026-01-01'})['record']
    h.configure('programme_memberships', {'person_id': h.seed['people'][0], 'programme_id': p['id'],
        'starts_on': '2026-10-05', 'ends_on': '2026-10-09'}, role='coordinator')
    for weekdays, amount in [([6, 0, 1, 3], 2), ([2], 1)]:
        quota(h, scope={'programme_id': p['id'], 'service_ids': [h.seed['services'][0]], 'weekdays': weekdays},
            starts_on='2026-10-01', ends_on='2026-11-01', window_days=1,
            comparison='target', severity='advisory', amount=amount)
    h.policy({'kind': 'restriction'}, {'programme_id': p['id'], 'weekdays': [2],
        'start_time': '13:00', 'end_time': '17:00'})
    w = h.workspace()
    for day, hour in [('2026-10-05', '08'), ('2026-10-05', '13'), ('2026-10-07', '08')]:
        end_hour = str(int(hour) + 4).zfill(2)
        w = h.shift(w, start=day+'T'+hour+':00:00+03:00', end=day+'T'+end_hour+':00:00+03:00',
                    seed_restricted=True)['workspace']
    result = evaluate(h.snapshot(w))
    rows = {r['starts_on']: (r['amount'], r['actual']) for r in result['quota_report']['rows']}
    assert rows == {'2026-10-05': (2, 2), '2026-10-06': (2, 0), '2026-10-07': (1, 1), '2026-10-08': (2, 0)}
    assert result['valid']  # Targets show gaps without weakening the separate safety rules.
    published = h.publish(w)
    report = h.get('staff', '/api/reports/'+published['publication_id'])['quotas']
    assert {r['starts_on']: (r['amount'], r['actual']) for r in report['rows']} == rows
    amended = h.workspace(base=published['publication_id'])
    bad = h.shift(amended, start='2026-10-07T13:00:00+03:00', end='2026-10-07T17:00:00+03:00', seed_restricted=True)['workspace']
    assert not bad['validation']['valid']
    h.lifecycle(bad, 'submit', status=422)
