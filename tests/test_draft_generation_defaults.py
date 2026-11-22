"""Draft readers agree on captured scoring after department settings move."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from apps.api import models as m
from apps.api.auth import authenticate
from apps.api.db import transaction
from apps.api.services import policies, read_projections, rota_summary
from apps.api.services.jobs import baseline_generation_context
from apps.api.snapshots import domain_snapshot
from packages.domain import fairness
from packages.domain.cp_sat import generate
from packages.domain.score import schedule_score, settings_of
from packages.domain.validation import evaluate
from tests.clock_support import freeze_clock
from tests.test_fill_options import options
from tests.test_p2_groups import apply, edit, fork, preview
from tests.test_planner_why import why
from tests.test_planning_groups_drafts import REASON, group
from tests.test_rule_catalogue_api import approve, propose
from tests.test_rule_catalogue_guard import line


CAPTURED = {"search_minutes": 2, "scoring": {"desired_cover_factor": 0.8}}
CURRENT = {"search_minutes": 3, "scoring": {"desired_cover_factor": 0.1}}

pytestmark = pytest.mark.parametrize(
    "h", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)], indirect=True)


@pytest.fixture
def divergent_defaults(h):
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed["department_id"]).generation_defaults = CAPTURED
    duty_type = h.configure("assignment_types", {"name": "Cover with a target", "starts_on": "2020-01-01",
        "definition": {"requirements": [{"minimum": 1, "target": 2, "maximum": 2}]}})["record"]["id"]
    created = h.shift(h.workspace(), typ=duty_type, people=h.seed["people"][:1])
    workspace = created["workspace"]
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed["department_id"])
        department.generation_defaults = CURRENT
        department.revision += 1
    source = h.snapshot(workspace)
    assert source["department"]["generation_defaults"] == CAPTURED
    checked = evaluate(source)
    expected = schedule_score(source, checked, CAPTURED)["score"]
    assert expected != schedule_score(source, checked, CURRENT)["score"]
    return workspace, created["shifts"][0], source, expected


@pytest.fixture
def draft_list_clock(monkeypatch):
    freeze_clock(monkeypatch, datetime(2026, 10, 4, tzinfo=timezone.utc))


@pytest.mark.usefixtures("draft_list_clock")
def test_draft_validation(h, divergent_defaults):
    workspace, _, _, expected = divergent_defaults
    start, end = workspace["starts_on"], workspace["ends_on"]
    h.lifecycle(workspace, "validate")
    for path in ("/api/workspaces", f"/api/home/rotas?start={start}&end={end}"):
        rows = h.get("coordinator", path)["workspaces"]
        listed = next(row for row in rows if row["id"] == workspace["id"])
        assert listed["health"]["fresh"] is True, path
    detail = h.detail(workspace)["workspace"]
    assert detail["validation"]["score"]["score"] == expected
    assert detail["validation_current"] is True


def test_draft_summary(h, divergent_defaults):
    workspace, _, _, expected = divergent_defaults
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, workspace["id"]).validation = None
    with transaction(h.app.state.engine) as db:
        _, validation, _, _ = rota_summary.summary_check(db, db.get(m.Workspace, workspace["id"]))
    assert validation["score"]["score"] == expected


def test_draft_score_review(h, divergent_defaults):
    workspace, _, _, expected = divergent_defaults
    result = h.get("coordinator", f"/api/workspaces/{workspace['id']}/score-review")
    assert result["score"]["score"] == expected


@pytest.fixture
def proposed_fill(h, divergent_defaults):
    workspace, shift, source, _ = divergent_defaults
    with transaction(h.app.state.engine) as db:
        defaults, _ = baseline_generation_context(db, db.get(m.Workspace, workspace["id"]))
    run_options = defaults.model_dump(mode="json")
    result = generate({**source, "generation_options": run_options, "generation_defaults": run_options},
                      time_budget_seconds=5)
    [addition] = result["additions"]
    assert addition["shift_id"] == shift["id"]
    return addition["participant"]["person_id"], result["summary"]


def test_draft_why(h, divergent_defaults, proposed_fill, record_property):
    workspace, shift, _, expected = divergent_defaults
    person, summary = proposed_fill
    assert summary["score_before"]["score"] == expected
    [session] = why(h, workspace, [shift["id"]], person_id=person)["sessions"]
    [row] = session["rows"]
    assert row["effect"]["score_without"] == summary["score_before"]["score"]
    assert row["effect"]["score_with"] == summary["score_saved"]["score"]
    record_property("score_before", row["effect"]["score_without"])
    record_property("score_after", row["effect"]["score_with"])


def test_draft_fill(h, divergent_defaults, proposed_fill, record_property):
    from apps.api.services.issues import issue_key
    workspace, shift, source, expected = divergent_defaults
    finding = next(row for row in evaluate(source)["findings"] if row["code"] == "COVERAGE_TARGET")
    fills = options(h, workspace, shift["id"], finding_key=issue_key(finding))
    pid, summary = proposed_fill
    candidate = next(row for row in fills["candidates"] if row["person_id"] == pid)
    assert candidate["status"] == "ok" and candidate["fills"]
    assert candidate["resolves"] is None
    [session] = why(h, workspace, [shift["id"]], person_id=pid)["sessions"]
    [row] = session["rows"]
    assert row["effect"]["score_without"] == expected
    trial = edit(h, workspace, shift, [*shift["participants"], {**fills["slot"], "person_id": pid}])
    stored = h.detail(trial["workspace"])["workspace"]["validation"]
    assert stored["score"]["score"] == row["effect"]["score_with"] == summary["score_saved"]["score"]
    assert not any(f["code"] == "COVERAGE_TARGET" for f in stored["findings"])
    record_property("score_before", row["effect"]["score_without"])
    record_property("score_after", stored["score"]["score"])


def test_draft_person_score(h, divergent_defaults, proposed_fill, record_property):
    workspace, shift, _, expected = divergent_defaults
    with transaction(h.app.state.engine, write=True) as db:
        # A stored check can predate captured scoring. Trial scores still use the draft defaults.
        row = db.get(m.Workspace, workspace["id"])
        check = deepcopy(row.validation)
        check["score"]["settings"] = settings_of(CURRENT)
        row.validation = check
    pid, summary = proposed_fill
    result = h.get("coordinator", f"/api/people/{pid}/score?workspace_id={workspace['id']}&open_limit=20")["draft"]
    assert result["fresh"] and result["opportunity_score"] == expected
    [place] = result["open_places"]
    [session] = why(h, workspace, [shift["id"]], person_id=pid)["sessions"]
    [row] = session["rows"]
    assert row["effect"]["score_with"] == summary["score_saved"]["score"]
    assert place["score_delta"] == round(row["effect"]["score_with"] - expected, 1) > 0
    record_property("score_before", result["opportunity_score"])
    record_property("score_delta", place["score_delta"])


def test_draft_merge(h, divergent_defaults):
    workspace, _, _, _ = divergent_defaults
    group(h)
    workspace = h.command("admin", "workspace.designate", {"reason": REASON},
                          workspace["id"], workspace["revision"])["workspace"]
    source = fork(h, workspace)
    [shift] = h.detail(source)["shifts"]
    source = edit(h, source, shift, [{**shift["participants"][0], "team_lead": True}])["workspace"]
    merged = apply(h, workspace, preview(h, source, workspace))["workspace"]
    data = h.snapshot(merged)
    score = schedule_score(data, evaluate(data), CAPTURED)
    assert h.detail(merged)["workspace"]["validation"]["score"] == score


def test_draft_grouping(h, divergent_defaults):
    workspace, _, _, expected = divergent_defaults
    token = h.client("coordinator").cookies["rota_session"]
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, token)
        [result] = policies.grouping_proof(db, actor, [])["rotas"]
    assert result["workspace_id"] == workspace["id"]
    assert result["score_before"] == result["score_after"] == expected


def test_draft_read_stamp(h, divergent_defaults):
    workspace, _, _, _ = divergent_defaults
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, workspace["id"])
        before = read_projections.input_stamp(db, row)
        # Hold revision inputs fixed to isolate the options used by the stamp.
        db.get(m.Department, h.seed["department_id"]).generation_defaults = {"search_minutes": 4}
        assert read_projections.input_stamp(db, row) == before
    refreshed = h.refresh_draft_baseline(workspace)
    assert h.snapshot(refreshed)["department"]["generation_defaults"] == {"search_minutes": 4}
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, workspace["id"])
        assert read_projections.input_stamp(db, row) != before


def test_generation_defaults_api(h, divergent_defaults):
    workspace, _, _, _ = divergent_defaults
    path = f"/api/jobs?workspace_id={workspace['id']}"
    client = h.client("coordinator")
    draft = client.get(path)
    assert draft.status_code == 200 and draft.json()["generation_defaults"] == CAPTURED
    assert h.get("admin", "/api/me")["department"]["generation_defaults"] == CURRENT
    submitted = h.lifecycle(workspace, "submit")["workspace"]
    assert submitted["status"] == "submitted"
    read = client.get(path, headers={"If-None-Match": draft.headers["etag"]})
    assert read.status_code == 200 and read.json()["generation_defaults"] == CAPTURED
    data = h.snapshot(submitted)
    assert data["department"]["generation_defaults"] == read.json()["generation_defaults"]
    score = h.get("coordinator", f"/api/workspaces/{workspace['id']}/score-review")["score"]
    assert score["score"] == schedule_score(data, evaluate(data), CAPTURED)["score"]


def goal_scores(reports, goal_ids):
    return {row["policy_id"]: (row["unit"], row["score"]) for row in reports if row["policy_id"] in goal_ids}


@pytest.fixture
def fairness_defaults(h):
    captured = {"balance_measure": "duties", "scoring": {"fairness_worst_off_emphasis": 0.0,
        "strengths": {"low": 1, "medium": 3, "high": 10, "critical": 30}}}
    current = {"balance_measure": "reserved_hours", "scoring": {"fairness_worst_off_emphasis": 0.8,
        "strengths": {"low": 2, "medium": 3, "high": 5, "critical": 30}}}
    first, second, _ = h.seed["people"]
    workload = h.policy({"kind": "balance_goal", "metric": "workload", "aim": "workload_spread",
                         "period": "rota", "share_by": "equal"}, severity="advisory")["id"]
    lines = [line(who={"conditions": [{"field": "person_ids", "values": [person]}]},
                  numbers={"target": {"value": amount, "level": strength}})
             for person, amount, strength in ((first, 2, "high"), (second, 4, "low"))]
    share = approve(h, propose(h, "fair_share", lines, settings={"balance_goal": {
        "metric": "duties", "aim": "target", "period": "rota", "comparison": "pooled"}})["record"])["id"]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed["department_id"]).generation_defaults = captured
    workspace = h.workspace()
    for day, person in ((4, first), (5, first), (6, second)):
        workspace = h.shift(workspace, f"2026-10-{day:02}T08:00:00+03:00",
                            f"2026-10-{day:02}T12:00:00+03:00", people=[person])["workspace"]
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed["department_id"])
        department.generation_defaults = current
        department.revision += 1

    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, workspace["id"])
        options, _ = baseline_generation_context(db, row)
        solver_input = domain_snapshot(db, row)
        live = domain_snapshot(db, row, current_scheduling=True)
    assert solver_input["department"]["generation_defaults"] == captured
    assert live["generation_options"] == current
    run_options = options.model_dump(mode="json")
    solver_input.update(generation_options=run_options, generation_defaults=run_options)
    result = generate(solver_input, time_budget_seconds=5)
    assert result["summary"]["score_before"]["settings"] == settings_of(captured)

    ids = {workload, share}
    reports = fairness.goal_reports(solver_input)
    expected = goal_scores(reports, ids)
    live_scores = goal_scores(fairness.goal_reports(live), ids)
    assert expected[workload][0] == "duties" and live_scores[workload][0] == "h"
    assert expected[share][1] != live_scores[share][1]
    for key in ("fairness_worst_off_emphasis", "strengths"):
        scoring = {**run_options["scoring"], key: current["scoring"][key]}
        trial = {**solver_input, "generation_options": {**run_options, "scoring": scoring},
                 "fairness_strengths": scoring["strengths"]}
        assert goal_scores(fairness.goal_reports(trial), ids)[share][1] != expected[share][1]
    return workspace, second, ids, reports, expected, live_scores


def test_draft_fairness_dashboard(h, fairness_defaults):
    workspace, _, ids, _, expected, _ = fairness_defaults
    h.lifecycle(workspace, "validate")
    response = h.get("coordinator", f"/api/fairness/goals?workspace_id={workspace['id']}")
    assert goal_scores(response["goals"], ids) == expected
    assert response["schedule"]["fresh"] is True


def test_draft_person_standing(h, fairness_defaults):
    workspace, person, ids, reports, expected, _ = fairness_defaults
    response = h.get("coordinator", f"/api/people/{person}/fairness?workspace_id={workspace['id']}")
    assert goal_scores(response["goals"], ids) == expected
    for report in reports:
        if report["policy_id"] not in ids:
            continue
        expected_row = next(row for row in report["people"] if row["person_id"] == person)
        [actual] = [row for goal in response["goals"] if goal["policy_id"] == report["policy_id"]
                    for row in goal["people"]]
        assert {key: actual[key] for key in expected_row} == expected_row


def test_published_fairness_uses_current_defaults(h, fairness_defaults):
    workspace, person, ids, _, _, current = fairness_defaults
    h.publish(workspace)
    for path in ("/api/fairness/goals", f"/api/people/{person}/fairness"):
        response = h.get("coordinator", f"{path}?workspace_id={workspace['id']}")
        assert response["workspace"]["status"] == "published"
        assert goal_scores(response["goals"], ids) == current


def test_draft_defaults_without_score_sources(h):
    created = h.command("coordinator", "workspace.create", {"name": "Display baseline",
        "starts_on": "2026-10-04", "ends_on": "2026-10-11",
        "baseline": {"mode": "custom", "sources": ["display"]}})
    assert {"score", "auto"} <= set(created["baseline"]["omitted_sources"])
    workspace = created["workspace"]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed["department_id"]).generation_defaults = CURRENT
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, workspace["id"])
        assert domain_snapshot(db, row, current_goals=True)["generation_options"] == CURRENT
    assert h.snapshot(workspace)["department"]["generation_defaults"] == CURRENT
    assert h.get("coordinator", f"/api/jobs?workspace_id={workspace['id']}")["generation_defaults"] == CURRENT


def test_review_keeps_captured_score_and_fairness(h, fairness_defaults):
    workspace, person, ids, _, expected_goals, _ = fairness_defaults
    wid = workspace["id"]
    review_path = f"/api/workspaces/{wid}/score-review"
    goal_paths = ("/api/fairness/goals", f"/api/people/{person}/fairness")
    draft_score = h.get("coordinator", review_path)["score"]
    stored_score = h.detail(workspace)["workspace"]["validation"]["score"]
    assert stored_score["score"] == draft_score["score"]
    assert stored_score["settings"] == draft_score["settings"]

    stages = (("validate", "coordinator", "draft"), ("submit", "coordinator", "submitted"),
              ("approve", "approver", "approved"))
    for action, role, status in stages:
        workspace = h.lifecycle(workspace, action, role)["workspace"]
        assert workspace["status"] == status
        if action == "submit":
            me = h.get("admin", "/api/me")
            department, organisation = me["department"], me["organisation"]
            h.command("admin", "department.settings", {
                "department_name": department["name"], "department_revision": department["revision"],
                "organisation_name": organisation["name"], "timezone": organisation["timezone"],
                "week_start": organisation["week_start"], "reason": "Update scoring for later rotas",
                "generation_defaults": {"scoring": {"fairness_worst_off_emphasis": 1,
                    "weights": {"coverage": 0, "rules": 0, "fairness": 1, "preferences": 0, "stability": 0}}},
            }, department["id"], organisation["revision"])
        assert h.get("coordinator", review_path)["score"] == draft_score
        validation = h.detail(workspace)["workspace"]["validation"]
        assert validation["score"]["score"] == stored_score["score"]
        assert validation["score"]["components"]["fairness"] == stored_score["components"]["fairness"]
        for path in goal_paths:
            response = h.get("coordinator", f"{path}?workspace_id={wid}")
            assert response["workspace"]["status"] == status
            assert goal_scores(response["goals"], ids) == expected_goals

    current = h.get("admin", "/api/me")["department"]["generation_defaults"]
    assert settings_of(current) != draft_score["settings"]
    published = h.lifecycle(workspace, "publish")["workspace"]
    assert published["status"] == "published"
    review = h.get("coordinator", review_path)["score"]
    assert review["settings"] == settings_of(current)
    assert review["score"] != draft_score["score"]
