"""Regression coverage for scheduling input readers."""
import json

from apps.api import models as m, snapshots, workspace_baselines
from apps.api.db import transaction
from apps.api.services import planning_groups
from packages.contracts import schemas as s
from packages.domain.input_manifest import manifest
from tests.test_fix_list_performance import actor, backend
from tests.test_planning_groups_drafts import integrated, settings, tree


REASON = "Check scheduling input currency"


def currency(h, workspace):
    with transaction(h.app.state.engine) as db:
        draft = db.get(m.Workspace, workspace["id"])
        organisation = db.get(m.Organisation, db.get(m.Department, draft.department_id).organisation_id)
        return {
            "fingerprint": snapshots.fingerprint(db, draft),
            "input_revision": organisation.input_revision,
            "availability_revision": organisation.availability_revision,
            "content_revision": draft.content_revision,
        }


def comparison(record_property, before, after, **evidence):
    record_property("oracle", json.dumps({"before": before, "after": after, **evidence}))
    return (before["fingerprint"] != after["fingerprint"],
            any(after[key] > before[key] for key in before if key.endswith("_revision")))


def generation_token(db, draft, data):
    baseline = workspace_baselines.latest(db, draft.id)
    token = workspace_baselines.generation_manifest(baseline, data)['generation']['build_digest']
    assert token == manifest(data)['token']
    return token


def test_group_rollback_changes_generation_population_outside_manifest(backend, record_property):
    h = backend
    workspace, shift, version = integrated(h)

    def generation_inputs(version_id):
        with transaction(h.app.state.engine) as db:
            draft = db.get(m.Workspace, workspace["id"])
            data = snapshots.domain_snapshot(db, draft)
            request = s.JobInput(workspace_id=draft.id, kind="generation", planning_group_version_id=version_id)
            planning_groups.guard_generation(db, actor(h), draft, data, request)
            return data["planning_population"][shift["id"]], generation_token(db, draft, data)

    original_people, original_token = generation_inputs(version["id"])
    changed = h.command("admin", "planning_group.update", {
        "definition": {**version["definition"], "membership": tree(h.seed["people"][1])},
        "reason": REASON,
    }, version["group_id"], version["revision"])["group"]
    alternate_people, alternate_token = generation_inputs(changed["id"])
    before = currency(h, workspace)
    restored = h.command("admin", "planning_group.rollback", {
        "version_id": version["id"], "reason": REASON,
    }, version["group_id"], changed["revision"])["group"]
    restored_people, restored_token = generation_inputs(restored["id"])
    assert alternate_people == [h.seed["people"][1]] and restored_people == original_people == [h.seed["people"][0]]
    assert alternate_token not in {original_token, restored_token}
    assert comparison(record_property, before, currency(h, workspace), population_changed=True) == (False, True)


def test_planning_setting_rollback_changes_generation_lookahead_outside_manifest(backend, record_property):
    h = backend
    workspace, _, version = integrated(h)
    previous = settings(h, group_lookahead="none")
    changed = settings(h, group_lookahead="coupled")

    def lookahead():
        with transaction(h.app.state.engine) as db:
            draft = db.get(m.Workspace, workspace["id"])
            request = s.JobInput(workspace_id=draft.id, kind="generation", planning_group_version_id=version["id"])
            data = snapshots.domain_snapshot(db, draft)
            planning_groups.guard_generation(db, actor(h), draft, data, request)
            return data["lookahead"], generation_token(db, draft, data)

    before, first = currency(h, workspace), lookahead()
    h.command("admin", "planning.settings.rollback", {"version_id": previous["id"], "reason": REASON},
              revision=changed["revision"])
    second = lookahead()
    assert (first[0], second[0]) == ("coupled", "none")
    assert first[1] != second[1]
    assert comparison(record_property, before, currency(h, workspace), lookahead=[first, second]) == (False, False)
