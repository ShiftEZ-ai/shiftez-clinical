"""Focused proof for immutable draft input baselines and refresh decisions."""
from copy import deepcopy
from datetime import date
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DatabaseError

from .governance_support import retire_rule
from apps.api import models as m
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.snapshots import currency_hint, domain_snapshot, fingerprint, fingerprints, snapshot_hash
from apps.api.snapshots import snapshot_inputs
from apps.api.workspace_baselines import _diff, _strictest_policy, live_safety_inputs, latest, projection, verify, verify_generation_snapshot
from packages.contracts.rule_catalogue import RULE_CATALOGUE, _V2_ENTRIES
from packages.contracts.schemas import GenerationOptions, NEVER_OVERRIDDEN, PolicyDefinition, SAFETY_STRICTNESS, safety_merge_policy


def test_workspace_create_captures_baseline_and_freezes_evaluation(h):
    created = h.workspace("Frozen baseline")
    workspace = created
    baseline = created["baseline"]
    assert created["snapshot_hash"] == baseline["snapshot_hash"]
    assert baseline["scope"] == "department"
    assert set(baseline["included_sources"]) == {
        "people", "duties", "calendars", "rules", "score", "auto", "receipts", "display"
    }

    with transaction(h.app.state.engine, write=True) as db:
        row = db.scalar(select(m.WorkspaceBaseline).where(m.WorkspaceBaseline.workspace_id == workspace["id"]))
        assert row.payload_hash == baseline["snapshot_hash"]
        old_policy = db.scalar(select(m.Policy).where(m.Policy.status == "active"))
        old_definition = deepcopy(old_policy.definition)
        new_definition = {**old_definition, "scope": {"person_ids": [h.seed["people"][0]]}}
        old_policy.definition = new_definition
        old_policy.revision += 1
        db.get(m.Organisation, h.seed["organisation_id"]).input_revision += 1
        baseline_id = row.id
        frozen = next(item for item in verify(row)["policies"] if item["id"] == old_policy.id)
        assert frozen["definition"] == old_definition

    # A draft check evaluates the current revision against its immutable capture.
    with transaction(h.app.state.engine) as db:
        snapshot = domain_snapshot(db, db.get(m.Workspace, workspace["id"]), mandatory_rules=True)
    assert [item["definition"] for item in snapshot["policies"] if item["id"] == old_policy.id] == [new_definition]

    with pytest.raises(DatabaseError, match="immutable workspace baseline"):
        with h.app.state.engine.begin() as db:
            db.execute(text("UPDATE workspace_baselines SET payload_hash = :value WHERE id = :id"),
                       {"value": "0" * 64, "id": baseline_id})


def test_draft_snapshot_overlays_live_safety_rule_after_session_capture(h):
    person = h.seed["people"][0]
    draft = h.shift(h.workspace("Live safety overlay"), people=[])["workspace"]
    restriction = h.policy({"kind": "restriction"}, {"person_id": person})
    snapshot = h.snapshot(draft)
    assert any(policy["id"] == restriction["id"] and
               policy["definition"]["rule"]["kind"] == "restriction"
               for policy in snapshot["policies"])


def test_draft_snapshot_overlays_live_catalogue_safety_rule_after_capture(h):
    import uuid
    draft = h.workspace("Live catalogue safety")
    rule = {"kind": "catalogue", "catalogue_version": 2, "id": "people_per_session", "settings": {},
            "lines": [{"id": str(uuid.uuid4()), "numbers": {"min": {"value": 2, "level": "mandatory"}}}]}
    policy = h.command("coordinator", "rule.propose", {"catalogue_id": "people_per_session",
        "catalogue_version": 2, "lines": rule["lines"], "reason": "Catalogue safety fixture"})["record"]
    policy = h.command("approver", "policy.activate", {"reason": "Independent catalogue review"},
                       policy["id"], policy["revision"])["record"]
    snapshot = h.snapshot(draft)
    assert policy["id"] in {row["id"] for row in snapshot["policies"]}


@pytest.mark.parametrize(("blocked", "same_id"), [
    pytest.param(False, False, id="current-workload-allows"),
    pytest.param(True, False, id="captured-safety-blocks"),
    pytest.param(True, True, id="captured-safety-blocks-after-in-place-revision"),
])
def test_review_uses_the_current_workload_number_and_keeps_captured_safety(h, monkeypatch, blocked, same_id):
    from packages.domain.conditions import active_rules
    from packages.domain.time import aware
    from packages.domain.validation import evaluate, finding_observations
    from tests.clock_support import freeze_clock
    from .test_rule_catalogue_api import approve, line, person, propose
    from .test_rule_catalogue_v2_api import proposed

    freeze_clock(monkeypatch, aware('2026-10-04T00:00:00+03:00'))
    conversion = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent conversion review'},
              conversion['id'], conversion['revision'])
    rota = h.workspace('Captured mixed workload')
    for day in ('05', '06', '07'):
        rota = h.shift(rota, start=f'2026-10-{day}T08:00:00+03:00', end=f'2026-10-{day}T12:00:00+03:00')['workspace']
    cap = line(numbers={'max': {'value': 1}}, settings={'max_duties': {'window': 'week'}})
    restricted = h.seed['people'][0 if blocked else 1]
    restriction = line(who=person(restricted), numbers={'apply': {}}, weekdays=[1], settings={'restriction': {}})
    catalogue = 'workload_limit'
    old = approve(h, propose(h, catalogue, [cap, restriction], catalogue_version=2)['record'])
    rota = h.refresh_draft_baseline(h.detail(rota)['workspace'])
    revised = {**cap, 'numbers': {'max': {'value': 3}}}
    live_restriction = {**restriction, 'weekdays': [4]}
    if same_id:
        # Model a lifecycle revision that updates an active policy without replacing its row.
        with transaction(h.app.state.engine, write=True) as db:
            policy = db.get(m.Policy, old['id'])
            definition = deepcopy(policy.definition)
            lines = {item['id']: item for item in definition['rule']['lines']}
            lines[cap['id']]['numbers']['max']['value'] = revised['numbers']['max']['value']
            lines[restriction['id']]['weekdays'] = live_restriction['weekdays']
            policy.definition = definition
            policy.revision += 1
            db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
            new = {'id': policy.id}
    else:
        new = approve(h, propose(h, catalogue, [revised, live_restriction], catalogue_version=2,
                                policy_id=old['id'], revision=old['revision'])['record'])

    with transaction(h.app.state.engine) as db:
        workspace = db.get(m.Workspace, rota['id'])
        review = domain_snapshot(db, workspace, current_mandatory=True)
    rules = active_rules(review)
    assert [(rule['id'], rule['definition']['rule']['maximum']) for rule in rules if rule['kind'] == 'max_duties'] == [(new['id'], 3)]
    kept = {tuple(rule['definition']['scope']['weekdays']) for rule in rules if rule['kind'] == 'restriction'}
    assert kept == {(1,), (4,)}
    findings = list(finding_observations(evaluate(review)))
    assert any(row['code'] == 'RESTRICTION' and row['severity'] == 'error' for row in findings) is blocked
    assert not any(row['code'] == 'MAX_DUTIES' for row in findings)

    submitted = h.lifecycle(h.detail(rota)['workspace'], 'submit', status=422 if blocked else 200)
    if blocked:
        assert submitted['error']['code'] == 'VALIDATION_FAILED'
        assert any(row['code'] == 'RESTRICTION' and row['person_id'] == restricted
                   for row in submitted['error']['details']['findings'])
    else:
        assert submitted['workspace']['status'] == 'submitted'
        approved = h.lifecycle(submitted['workspace'], 'approve', role='approver')
        assert approved['workspace']['status'] == 'approved'


def test_review_keeps_stricter_cover_minimum_and_maximum_from_either_revision(h):
    from packages.domain.conditions import active_rules
    from .test_rule_catalogue_api import approve, line, propose

    catalogue = 'people_per_session'
    original = line(numbers={'min': {'value': 1}, 'max': {'value': 2},
                             'target': {'value': 2, 'level': 'medium'}})
    old = approve(h, propose(h, catalogue, [original])['record'])
    rota = h.shift(h.workspace('Mixed cover numbers'))['workspace']
    current = {**original, 'numbers': {'min': {'value': 2}, 'max': {'value': 4},
                                      'target': {'value': 3, 'level': 'medium'}}}
    approve(h, propose(h, catalogue, [current], policy_id=old['id'], revision=old['revision'])['record'])

    with transaction(h.app.state.engine) as db:
        review = domain_snapshot(db, db.get(m.Workspace, rota['id']), current_mandatory=True)
    rules = [p['definition'] for p in active_rules(review)]
    assert max(d['rule']['minimum'] for d in rules if d['rule']['kind'] == 'minimum_cover'
               and d['severity'] == 'mandatory') == 2
    assert min(d['rule']['maximum'] for d in rules if d['rule']['kind'] == 'maximum_participants') == 2
    assert [d['rule']['minimum'] for d in rules if d['rule']['kind'] == 'minimum_cover'
            and d['severity'] == 'advisory'] == [3]


def test_review_replaces_captured_seed_counts_with_current_counts_in_a_mixed_rule(h):
    from packages.domain.conditions import active_rules
    from .test_rule_catalogue_api import approve, line, propose
    from .test_rule_catalogue_v2_api import proposed

    conversion = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent conversion review'},
              conversion['id'], conversion['revision'])
    restricted = line(numbers={'apply': {}}, weekdays=[1], settings={'restriction': {}})
    old = approve(h, propose(h, 'workload_limit', [restricted], catalogue_version=2)['record'])
    rota = h.workspace('Captured seed counts')
    new = approve(h, propose(h, 'workload_limit', [{**restricted, 'weekdays': [4]}], catalogue_version=2,
                            policy_id=old['id'], revision=old['revision'])['record'])

    def seed(policy, limit):
        maximum = line(numbers={'max': {'value': limit, 'level': 'critical'}},
                       settings={'block_quota': {'metric': 'lead_turns', 'starts_on': '2000-01-01',
                                                 'repeat': True, 'window_alignment': 'calendar_month'}})
        return {'id': maximum['id'], 'policy_id': policy['id'], 'policy_revision': policy['revision'],
                'line': maximum}

    with transaction(h.app.state.engine) as db:
        workspace = db.get(m.Workspace, rota['id'])
        organisation = db.get(m.Organisation, h.seed['organisation_id'])
        current = snapshot_inputs(db, organisation)
        capture = {**current, 'policies': [old], 'policy_seed_lines': [seed(old, 2)]}
        current = {**current, 'policies': [new], 'policy_seed_lines': [seed(new, 4)]}
        review = live_safety_inputs(db, workspace, capture, current=current,
                                    include_mandatory=True, current_mandatory=True)
    copies = active_rules(review)
    assert [p['definition']['rule']['amount'] for p in copies
            if p['definition']['rule'].get('metric') == 'lead_turns'] == [4]
    assert {tuple(p['definition']['scope']['weekdays']) for p in copies
            if p['definition']['rule']['kind'] == 'restriction'} == {(1,), (4,)}


def test_catalogue_safety_dedup_keeps_a_captured_rule_with_longer_dates(h):
    from apps.api.workspace_baselines import _catalogue_destination_at_least_as_strict
    from .test_rule_catalogue_api import approve, line, propose

    original = line(numbers={'min': {'value': 11}})
    saved = approve(h, propose(h, 'rest_between_duties', [original])['record'])
    earlier = {**saved, 'starts_on': '2026-10-01', 'ends_on': '2026-10-08'}
    assert _catalogue_destination_at_least_as_strict(earlier, dict(earlier))
    assert not _catalogue_destination_at_least_as_strict(earlier, {**earlier, 'starts_on': '2026-10-02'})
    assert not _catalogue_destination_at_least_as_strict(earlier, {**earlier, 'ends_on': '2026-10-07'})


def test_safety_catalogue_conversion_binds_once_before_and_after_draft_capture(h):
    from packages.domain.conditions import active_rules
    from .test_rule_catalogue_guard import world
    from .test_rule_templates import activate

    draft_before, old, _ = world(h)
    source_id = old["cover"]["id"]
    result = h.command("coordinator", "rule.migrate_to_catalogue", {"reason": "Safety conversion overlay"})
    replacement = next(record for record in result["records"] if source_id in record["definition"]["merged_ids"])
    for record in result["records"]:
        activate(h, record)

    # maintenance freeze matrix rows 425d832da/6ef713812: a SAFETY conversion must bind the
    # live catalogue destination once, while the older frozen source is removed.
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, draft_before["id"])
        organisation = db.get(m.Organisation, h.seed["organisation_id"])
        frozen = verify(latest(db, row.id))
        current = snapshot_inputs(db, organisation)
        effective_before = live_safety_inputs(db, row, frozen, current=current)
    before_policies = [policy for policy in effective_before["policies"]
                       if policy["id"] in {source_id, replacement["id"]}]
    assert [policy["id"] for policy in before_policies] == [replacement["id"]]
    assert len([rule for rule in active_rules(effective_before) if rule["id"] == replacement["id"]]) == 1

    draft_after = h.workspace("Safety conversion after", start="2026-10-04", end="2026-10-11")
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, draft_after["id"])
        organisation = db.get(m.Organisation, h.seed["organisation_id"])
        frozen = verify(latest(db, row.id))
        current = snapshot_inputs(db, organisation)
        effective_after = live_safety_inputs(db, row, frozen, current=current)
    after_policies = [policy for policy in effective_after["policies"]
                      if policy["id"] in {source_id, replacement["id"]}]
    assert [policy["id"] for policy in after_policies] == [replacement["id"]]
    assert len([rule for rule in active_rules(effective_after) if rule["id"] == replacement["id"]]) == 1


def _catalogue_destination(source, *, hours=12, catalogue_id="rest_between_duties", safety=True):
    line = {"id": str(uuid.uuid4()), "numbers": {
        "min" if safety else "apply": {"value": hours if safety else None, "level": "mandatory" if safety else "medium"}}}
    if not safety:
        line["settings"] = {"balance_goal": {"metric": "duties"}}
    definition = PolicyDefinition.model_validate({
        "scope": {}, "severity": "mandatory", "merged_ids": [source["id"]],
        "rule": {"kind": "catalogue", "id": catalogue_id, "settings": {}, "lines": [line]},
    }).model_dump(mode="json")
    return {**source, "id": str(uuid.uuid4()), "kind": "catalogue", "revision": 1,
            "definition": definition}


def test_looser_live_catalogue_destination_keeps_frozen_safety_source(h):
    source = h.policy({"kind": "min_rest", "hours": 12})
    draft = h.workspace("Frozen rest before conversion")
    result = h.command("coordinator", "rule.migrate_to_catalogue", {"reason": "Amend rest conversion"})
    destination = next(record for record in result["records"] if source["id"] in record["definition"]["merged_ids"])
    assert destination.get("kind") == "catalogue"
    # Amend the reviewed proposal before the independent approver activates it.
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Policy, destination["id"])
        definition = deepcopy(row.definition)
        definition["rule"]["lines"][0]["numbers"]["min"]["value"] = 11
        row.definition = definition
    h.command("approver", "policy.activate", {"reason": "Independent review of amended conversion"},
              destination["id"], destination["revision"])
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, draft["id"])
        organisation = db.get(m.Organisation, h.seed["organisation_id"])
        frozen = verify(latest(db, row.id))
        current = snapshot_inputs(db, organisation)
        effective = live_safety_inputs(db, row, frozen, current=current)
    selected = [policy for policy in effective["policies"] if policy["id"] in {source["id"], destination["id"]}]
    assert {policy["id"] for policy in selected} == {source["id"], destination["id"]}
    assert any(policy["id"] == source["id"] and policy["definition"]["rule"]["hours"] == 12 for policy in selected)


def test_non_safety_catalogue_destination_keeps_frozen_safety_source(h):
    source = h.policy({"kind": "min_rest", "hours": 12})
    draft = h.workspace("Non-safety conversion destination")
    destination = _catalogue_destination(source, catalogue_id="fair_share", safety=False)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, draft["id"])
        organisation = db.get(m.Organisation, h.seed["organisation_id"])
        frozen = verify(latest(db, row.id))
        current = snapshot_inputs(db, organisation)
        current["policies"] = [*current["policies"], destination]
        effective = live_safety_inputs(db, row, frozen, current=current)
    assert source["id"] in {policy["id"] for policy in effective["policies"]}


def test_missing_catalogue_destination_keeps_frozen_safety_source(h):
    source = h.policy({"kind": "min_rest", "hours": 12})
    draft = h.workspace("Missing conversion destination")
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, draft["id"])
        organisation = db.get(m.Organisation, h.seed["organisation_id"])
        frozen = verify(latest(db, row.id))
        effective = live_safety_inputs(db, row, frozen, current=snapshot_inputs(db, organisation))
    assert source["id"] in {policy["id"] for policy in effective["policies"]}


def test_draft_fingerprint_tracks_live_bound_inputs(h):
    draft = h.shift(h.workspace("Live fingerprint"), people=[])["workspace"]
    with transaction(h.app.state.engine) as db:
        before = fingerprint(db, db.get(m.Workspace, draft["id"]))
    h.policy({"kind": "restriction"}, {"person_id": h.seed["people"][0]})
    with transaction(h.app.state.engine) as db:
        after_rule = fingerprint(db, db.get(m.Workspace, draft["id"]))
    assert after_rule != before
    h.command("coordinator", "availability.record", {"person_id": h.seed["people"][0], "kind": "recorded_leave",
        "start": "2026-10-04T00:00:00+03:00", "end": "2026-10-05T00:00:00+03:00", "private_reason": "test leave"})
    with transaction(h.app.state.engine) as db:
        after_leave = fingerprint(db, db.get(m.Workspace, draft["id"]))
    assert after_leave != after_rule
    h.policy({"kind": "max_hours", "hours": 80, "window": "week"})
    with transaction(h.app.state.engine) as db:
        assert fingerprint(db, db.get(m.Workspace, draft["id"])) != after_leave


@pytest.mark.parametrize("with_sessions", [False, True], ids=["empty", "with-sessions"])
def test_single_and_batch_fingerprints_share_the_live_overlay(h, with_sessions):
    """Every read path hashes the same effective frozen-plus-live draft inputs."""
    person = h.seed["people"][0]
    draft = h.workspace("Fingerprint invariant")
    if with_sessions:
        draft = h.shift(draft, people=[person])["workspace"]
    unrelated = h.workspace("Unrelated fingerprint draft")

    def values(workspace):
        with transaction(h.app.state.engine) as db:
            row = db.get(m.Workspace, workspace["id"])
            single = fingerprint(db, row)
            batch = fingerprints(db, [row])[row.id]
            stored = (row.validation or {}).get("fingerprint")
            return single, batch, stored

    before = values(draft)
    assert before[0] == before[1]

    safety = h.policy({"kind": "restriction"}, {"person_id": person})
    non_safety = h.policy({"kind": "max_hours", "hours": 80, "window": "week"}, exception=True)
    exception = h.command("coordinator", "exception.create", {
        "policy_id": non_safety["id"], "person_id": person,
        "starts_on": "2026-10-04", "ends_on": "2026-10-11", "reason": "Fingerprint invariant",
    })["record"]
    approved = h.command("approver", "exception.approve", {"reason": "Independent invariant review"},
                         exception["id"], exception["revision"])["record"]
    assert approved["status"] == "approved"
    h.command("coordinator", "availability.record", {
        "person_id": person, "kind": "recorded_leave", "start": "2026-10-04T00:00:00+03:00",
        "end": "2026-10-05T00:00:00+03:00", "private_reason": "Fingerprint invariant",
    })
    checked = h.lifecycle(draft, "validate")["workspace"]
    after = values(checked)
    assert after[0] == after[1] == after[2]
    assert after[0] != before[0]
    assert safety["status"] == "active"

    coordinator = h.detail(checked)["current_fingerprint"]
    approver = h.get("approver", f"/api/workspaces/{checked['id']}")["current_fingerprint"]
    assert coordinator == approver == after[0]

    h.shift(unrelated, people=[h.seed["people"][1]])
    assert values(checked) == after


def test_draft_snapshot_with_unassigned_person_is_byte_identical_from_prepared_inputs(h):
    assigned, unassigned = h.seed["people"][:2]
    draft = h.shift(h.workspace("Prepared people snapshot"), people=[assigned])["workspace"]
    with transaction(h.app.state.engine) as db:
        organisation = db.get(m.Organisation, h.seed["organisation_id"])
        workspace = db.get(m.Workspace, draft["id"])
        shared = snapshot_inputs(db, organisation)
        prepared = domain_snapshot(db, workspace, shared_inputs=shared)
        fresh = domain_snapshot(db, workspace)

    assert prepared == fresh
    assert snapshot_hash(prepared) == snapshot_hash(fresh)
    assert unassigned in prepared["people"]


def test_safety_merge_contract_declares_every_kind_and_catalogue_slot():
    for kind in NEVER_OVERRIDDEN:
        assert safety_merge_policy(kind, None) in {"max", "min", "keep_both"}
    for kind, fields in SAFETY_STRICTNESS.items():
        for field in fields:
            assert safety_merge_policy(kind, field) in {"max", "min"}
    entries = [*RULE_CATALOGUE, *_V2_ENTRIES.values()]
    for entry in entries:
        for slots in entry["numbers"].values():
            for slot in slots:
                if slot["kind"] in NEVER_OVERRIDDEN:
                    assert safety_merge_policy(slot["kind"], slot.get("field")) in {"max", "min", "keep_both"}


def test_strictest_reader_keeps_differently_scoped_safety_obligations():
    frozen = {"id": "same", "revision": 1, "status": "active", "active": True,
              "definition": {"scope": {"person_id": "a"}, "rule": {"kind": "min_rest", "hours": 11}}}
    live = {"id": "same", "revision": 2, "status": "active", "active": True,
            "definition": {"scope": {"person_id": "b"}, "rule": {"kind": "min_rest", "hours": 12}}}
    merged = _strictest_policy(frozen, live, "min_rest")
    assert len(merged) == 2 and merged[0]["id"] != merged[1]["id"]


def test_retired_live_safety_revision_is_not_added_to_draft_overlay(h):
    draft = h.workspace("Retired live rule")
    policy = h.policy({"kind": "restriction"})
    retire_rule(h, policy, "Synthetic retirement")
    snapshot = h.snapshot(draft)
    assert policy["id"] not in {row["id"] for row in snapshot["policies"]}


def test_resource_pool_is_live_for_drafts_and_stale_but_published_evidence_stays_frozen(h):
    from .test_resources_calendars import pool, typ

    resource = pool(h, capacity=2)
    duty = typ(h, resource, resources=[{"resource_pool_id": resource["id"], "units": 2}])
    draft = h.shift(h.workspace("Live resource draft"), typ=duty["id"])["workspace"]
    before = h.detail(draft)
    assert before["workspace"]["validation_current"]

    published = h.publish(h.shift(h.workspace("Frozen resource publication"), typ=duty["id"])["workspace"])
    publication_id = published["publication_id"]
    issued = h.get("coordinator", "/api/publications/" + publication_id)
    issued_snapshot = deepcopy(issued["snapshot"])
    issued_validation = deepcopy(issued["original_validation"])
    current = h.get("admin", "/api/catalogues")["resource_pools"]
    changed = next(row for row in current if row["id"] == resource["id"])
    h.configure("resource_pools", {"name": changed["name"], "starts_on": changed["starts_on"], "capacity": 1},
                changed["id"], changed["revision"])

    after = h.detail(draft)
    assert not after["workspace"]["validation_current"]
    unchanged = h.get("coordinator", "/api/publications/" + publication_id)
    # 730ead030 / 6c34ffc9a preserve issued evidence; current-read behavior is covered by
    # test_resources_calendars::test_resource_recheck_after_approval_and_frozen_history and
    # test_postgres::test_postgres_pooled_capacity_calendar_and_frozen_history.
    assert unchanged["snapshot"] == issued_snapshot
    assert unchanged["snapshot"]["resource_pools"][resource["id"]]["capacity"] == 2
    assert unchanged["original_validation"] == issued_validation
    current = h.get("coordinator", "/api/publications/" + publication_id + "/coverage")
    finding_codes = {finding["code"] for finding in current["evaluation"]["findings"]}
    assert finding_codes & {"RESOURCE_INACTIVE", "POOLED_RESOURCE_CAPACITY"}

    with transaction(h.app.state.engine) as db:
        organisation = db.get(m.Organisation, h.seed["organisation_id"])
        current_inputs = snapshot_inputs(db, organisation)
        draft_row = db.get(m.Workspace, draft["id"])
        published_row = db.get(m.Workspace, published["workspace"]["id"])
        draft_inputs = verify(latest(db, draft_row.id))
        published_inputs = verify(latest(db, published_row.id))
        assert live_safety_inputs(db, draft_row, draft_inputs, current=current_inputs)["resource_pools"][resource["id"]]["capacity"] == 1
        assert live_safety_inputs(db, published_row, published_inputs, current=current_inputs)["resource_pools"][resource["id"]]["capacity"] == 1


def test_qa_2027_legacy_draft_capture_unblocks_refresh_and_generation(h):
    with transaction(h.app.state.engine, write=True) as db:
        actor_id = h.seed["identities"]["coordinator"]
        workspace = m.Workspace(id=m.uid(), created_at=m.now(), department_id=h.seed["department_id"],
                                created_by=actor_id, last_edited_by=actor_id, name="QA 2027 baseline",
                                starts_on=date(2027, 10, 3), ends_on=date(2027, 10, 10),
                                status="draft", revision=1, content_revision=1)
        db.add(workspace)
        db.flush()
        created = {"id": workspace.id, "revision": workspace.revision}

    captured = h.command("coordinator", "workspace.baseline.capture",
                         {"reason": "Capture QA 2027 draft baseline"},
                         created["id"], created["revision"])
    assert captured["baseline"]["snapshot_hash"] == captured["snapshot_hash"]
    assert captured["baseline"]["scope"] == "department"
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.AuditEvent.id).where(
            m.AuditEvent.action == "workspace.baseline.capture",
            m.AuditEvent.target_id == created["id"]))

    with transaction(h.app.state.engine, write=True) as db:
        legacy = m.Workspace(id=m.uid(), created_at=m.now(), department_id=h.seed["department_id"],
                             created_by=h.seed["identities"]["coordinator"], last_edited_by=h.seed["identities"]["coordinator"],
                             name="QA 2027 refresh baseline", starts_on=date(2027, 10, 3), ends_on=date(2027, 10, 10),
                             status="draft", revision=1, content_revision=1)
        db.add(legacy)
        db.flush()
        legacy_id, legacy_revision = legacy.id, legacy.revision
    preview = h.command("coordinator", "workspace.baseline.refresh.preview", {},
                        legacy_id, legacy_revision)
    assert preview["baseline"]["snapshot_hash"]
    job = h.command("coordinator", "job.create",
                    {"workspace_id": legacy_id, "kind": "generation"},
                    revision=legacy_revision)["job"]
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job["id"])
        assert row.input_snapshot["baseline"]["hash"] == preview["baseline"]["snapshot_hash"]


def test_capture_missing_changes_currency_hint_baseline_identity(h):
    with transaction(h.app.state.engine, write=True) as db:
        actor_id = h.seed["identities"]["coordinator"]
        workspace = m.Workspace(id=m.uid(), created_at=m.now(), department_id=h.seed["department_id"],
                                created_by=actor_id, last_edited_by=actor_id, name="Missing baseline hint",
                                starts_on=date(2027, 10, 3), ends_on=date(2027, 10, 10),
                                status="draft", revision=1, content_revision=1)
        db.add(workspace)
        db.flush()
        workspace_id, revision = workspace.id, workspace.revision
        before = currency_hint(db, workspace)
    assert before["baseline"] == [None, None]

    captured = h.command("coordinator", "workspace.baseline.capture", {"reason": "Capture missing hint"},
                         workspace_id, revision)
    with transaction(h.app.state.engine) as db:
        after = currency_hint(db, db.get(m.Workspace, workspace_id))
    assert after["baseline"] == [captured["baseline"]["id"], captured["baseline"]["snapshot_hash"]]
    assert after != before


def test_refresh_baseline_refuses_old_activation_receipt(h):
    from apps.worker.main import process_one
    from .test_contextual_feasibility import activate

    draft = h.shift(h.workspace("Refresh receipt"))["workspace"]
    policy = h.command("coordinator", "policy.create", {
        "name": "Refresh receipt rule", "owner": "FM coordinator", "source": "Baseline stamp regression",
        "starts_on": "2026-01-01", "definition": {"rule": {"kind": "restriction"}},
    })["record"]
    job = h.command("approver", "job.create", {
        "workspace_id": draft["id"], "kind": "feasibility",
        "rule_changes": [{"policy_id": policy["id"], "expected_revision": policy["revision"]}],
    }, revision=draft["revision"])["job"]
    assert process_one(h.app.state.engine)
    with transaction(h.app.state.engine) as db:
        before_hint = currency_hint(db, db.get(m.Workspace, draft["id"]))

    with transaction(h.app.state.engine, write=True) as db:
        active = db.scalar(select(m.Policy).where(m.Policy.status == "active"))
        active.definition = {**active.definition, "scope": {"person_ids": ["refresh-change"]}}
        active.revision += 1
    preview = h.command("coordinator", "workspace.baseline.refresh.preview", {}, draft["id"], draft["revision"])
    applied = h.command("coordinator", "workspace.baseline.refresh.apply", {
        "expected_baseline_hash": preview["baseline"]["snapshot_hash"],
        "decisions": {source: "use_department" for source in preview["conflicts"]},
        "reason": "Refresh the draft baseline after reviewed rules",
    }, draft["id"], draft["revision"])
    with transaction(h.app.state.engine) as db:
        after_hint = currency_hint(db, db.get(m.Workspace, draft["id"]))
    assert applied["snapshot_hash"] != before_hint["baseline"][1]
    assert applied["workspace"]["content_revision"] == draft["content_revision"]
    assert before_hint["baseline"] != after_hint["baseline"]
    with pytest.raises(Problem) as error:
        activate(h, policy, job["id"], acknowledge=True)
    assert error.value.code == "RULE_CHECK_REQUIRED"


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_refresh_preview_keeps_unchanged_receipt_accepted(request, backend):
    from apps.worker.main import process_one
    from .test_contextual_feasibility import activate
    h = request.getfixturevalue(backend)

    draft = h.shift(h.workspace("Refresh preview receipt"))["workspace"]
    policy = h.command("coordinator", "policy.create", {
        "name": "Refresh preview receipt rule", "owner": "FM coordinator", "source": "Baseline stamp regression",
        "starts_on": "2026-01-01", "definition": {"rule": {"kind": "restriction"}},
    })["record"]
    job = h.command("approver", "job.create", {
        "workspace_id": draft["id"], "kind": "feasibility",
        "rule_changes": [{"policy_id": policy["id"], "expected_revision": policy["revision"]}],
    }, revision=draft["revision"])["job"]
    assert process_one(h.app.state.engine)
    preview = h.command("coordinator", "workspace.baseline.refresh.preview", {}, draft["id"], draft["revision"])
    with transaction(h.app.state.engine) as db:
        current = currency_hint(db, db.get(m.Workspace, draft["id"]))
    assert current["baseline"][1] == preview["baseline"]["snapshot_hash"]
    decision = activate(h, policy, job["id"], acknowledge=True)
    assert decision["records"][0]["status"] == "active"


def test_custom_baseline_expands_dependencies_and_reports_omissions(h):
    result = h.command("coordinator", "workspace.create", {
        "name": "Custom baseline", "starts_on": "2026-10-04", "ends_on": "2026-10-11",
        "baseline": {"mode": "custom", "sources": ["score"]},
    })
    baseline = result["baseline"]
    assert baseline["scope"] == "custom"
    assert set(("score", "rules", "people", "receipts")) <= set(baseline["included_sources"])
    assert set(("auto", "display")) <= set(baseline["omitted_sources"])
    assert baseline["dependency_closure"]["score"] == ["rules", "people"]


def test_baseline_scope_and_refresh_contracts_fail_closed_and_permissions_are_explicit(h):
    h.command("coordinator", "workspace.create", {
        "name": "Invalid baseline", "starts_on": "2026-10-04", "ends_on": "2026-10-11",
        "baseline": {"mode": "custom", "sources": ["unknown"]},
    }, status=422)
    created = h.workspace("Refresh permissions")
    h.command("approver", "workspace.baseline.refresh.preview", {}, created["id"], created["revision"], status=403)
    h.command("staff", "workspace.baseline.refresh.preview", {}, created["id"], created["revision"], status=403)
    h.command("coordinator", "workspace.baseline.refresh.apply", {
        "decisions": {"rules": "invalid"}, "reason": "Reject malformed decision"
    }, created["id"], created["revision"], status=422)


def test_generation_reference_fails_closed_when_baseline_hash_is_tampered(h):
    created = h.workspace("Manifest integrity")
    with transaction(h.app.state.engine) as db:
        workspace = db.get(m.Workspace, created["id"])
        baseline = db.scalar(select(m.WorkspaceBaseline).where(m.WorkspaceBaseline.workspace_id == workspace.id))
        reference = projection(baseline)
        reference["hash"] = "0" * 64
        with pytest.raises(Problem) as rejected:
            verify_generation_snapshot(db, workspace, {"baseline": reference})
        assert rejected.value.code == "BASELINE_INVALID"


def test_baseline_migration_is_additive_and_forward_only():
    import importlib
    migration = importlib.import_module("migrations.versions.0142_workspace_baselines")
    assert migration.SAFETY_CLASS == "additive"
    with pytest.raises(RuntimeError, match="Forward-only"):
        migration.downgrade()


def test_refresh_preview_apply_and_conflict_decision(h):
    created = h.workspace("Refresh baseline")
    with transaction(h.app.state.engine, write=True) as db:
        policy = db.scalar(select(m.Policy).where(m.Policy.status == "active"))
        policy.definition = {**policy.definition, "scope": {"person_ids": ["department-change"]}}
        policy.revision += 1
        db.get(m.Organisation, h.seed["organisation_id"]).input_revision += 1

    preview = h.command("coordinator", "workspace.baseline.refresh.preview", {}, created["id"], created["revision"])
    read_preview = h.get("coordinator", f"/api/workspaces/{created['id']}/baseline/refresh-preview")
    assert read_preview["changes"] == preview["changes"] and read_preview["conflicts"] == preview["conflicts"]
    assert "rules" in preview["conflicts"] or any(item["source"] == "rules" for item in preview["changes"])
    # A department-only change is accepted without a conflict choice.
    applied = h.command("coordinator", "workspace.baseline.refresh.apply", {
        "expected_baseline_hash": created["snapshot_hash"],
        "decisions": {source: "use_department" for source in preview["conflicts"]},
        "reason": "Refresh the draft baseline after reviewed department changes",
    }, created["id"], created["revision"])
    assert applied["applied"] is True
    assert applied["snapshot_hash"] != created["snapshot_hash"]
    assert applied["workspace"]["revision"] == created["revision"] + 1


@pytest.mark.parametrize("h", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)], indirect=True)
def test_baseline_freezes_settings_but_keeps_department_row_live(h):
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed["department_id"])
        department.generation_defaults = {"scoring": {"wish_debt_cap": 2, "wish_budget_points": 10}}
        department.display_settings = {"autoschedule_animation": {"mode": "off"}}
    h.command("staff", "wish.create", {"person_id": h.seed["people"][0], "kind": "prefer_off",
        "first_day": "2026-10-05", "last_day": "2026-10-05", "points": 1})
    draft = h.workspace("Frozen settings, live calendar")
    before = h.snapshot(draft)
    frozen = before["generation_options"]
    assert before["wishes"]
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed["department_id"])
        department.generation_defaults = {"scoring": {"wish_debt_cap": 7, "wish_budget_points": 0}}
        department.weekend_days = [0, 1]
        department.name = "Live department name"
        calendar = m.OperatingCalendar(department_id=department.id, name="Live operating calendar",
                                       starts_on=date(2026, 1, 1), definition={})
        db.add(calendar)
        db.flush()
        department.operating_calendar_id = calendar.id
        department.revision += 1
        calendar_id = calendar.id
    snapshot = h.snapshot(draft)
    assert snapshot["generation_options"] == frozen
    assert snapshot["wishes"] == before["wishes"]
    assert snapshot["fairness_strengths"] == before["fairness_strengths"]
    assert snapshot["department"]["weekend_days"] == [0, 1]
    assert snapshot["department"]["operating_calendar_id"] == calendar_id
    assert snapshot["department"]["name"] == "Live department name"
    # Captured generation defaults are exposed alongside the captured display settings.
    assert snapshot["department"]["generation_defaults"] == frozen
    assert snapshot["department"]["generation_defaults"]["scoring"]["wish_debt_cap"] == 2
    with transaction(h.app.state.engine) as db:
        from apps.api.serialization import record
        from apps.api.services.jobs import baseline_generation_context
        workspace = db.get(m.Workspace, draft["id"])
        assert snapshot["department"] == {**record(db.get(m.Department, workspace.department_id)),
            "generation_defaults": before["department"]["generation_defaults"],
            "display_settings": before["department"]["display_settings"]}
        defaults, _ = baseline_generation_context(db, workspace)
        assert defaults.scoring.wish_debt_cap == frozen["scoring"]["wish_debt_cap"]
        assert domain_snapshot(db, workspace, operational=True)["generation_options"] == frozen
        current = domain_snapshot(db, workspace, current_scheduling=True)
        assert current["generation_options"]["scoring"]["wish_debt_cap"] == 7
        assert current["department"]["generation_defaults"] == current["generation_options"]
        goals = domain_snapshot(db, workspace, current_goals=True)
        assert goals["department"]["generation_defaults"] == goals["generation_options"] == frozen
        assert goals["fairness_strengths"] == snapshot["fairness_strengths"]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, draft["id"]).status = "submitted"
    assert h.snapshot(draft)["department"]["generation_defaults"] == frozen


@pytest.mark.parametrize("h", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)], indirect=True)
def test_legacy_draft_settings_follow_live_department(h):
    with transaction(h.app.state.engine, write=True) as db:
        actor_id = db.scalar(select(m.Identity.id).limit(1))
        workspace = m.Workspace(created_by=actor_id, last_edited_by=actor_id, department_id=h.seed["department_id"],
            name="Legacy settings control",
            starts_on=date(2026, 10, 4), ends_on=date(2026, 10, 11), status="draft")
        db.add(workspace)
        db.flush()
        draft = {"id": workspace.id}
        db.get(m.Department, h.seed["department_id"]).generation_defaults = {"scoring": {"wish_debt_cap": 7}}
    options = h.snapshot(draft)["generation_options"]
    assert options["scoring"]["wish_debt_cap"] == 7
    assert h.get("coordinator", f"/api/jobs?workspace_id={draft['id']}")["generation_defaults"] == options
    with transaction(h.app.state.engine) as db:
        assert domain_snapshot(db, db.get(m.Workspace, draft["id"]), current_goals=True)["generation_options"] == options


@pytest.mark.parametrize("h", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)], indirect=True)
def test_live_department_changes_do_not_drift_frozen_settings(h):
    draft = h.workspace("Live calendar drift")
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed["department_id"])
        department.weekend_days = [0, 1]
        department.revision += 1
        workspace = db.get(m.Workspace, draft["id"])
        baseline = latest(db, workspace.id)
        current = snapshot_inputs(db, db.get(m.Organisation, h.seed["organisation_id"]))
        _, _, changes = _diff(baseline, workspace, current)
        assert not {item["source"] for item in changes} & {"score", "auto", "display"}


def test_rules_change_says_whether_a_mandatory_rule_is_in_it(h):
    # Issue 1030: a rules change on advisory rules only is not called mandatory; one with a mandatory rule is.
    advisory = h.policy({"kind": "max_hours", "hours": 80, "window": "week"}, severity="advisory")
    mandatory = h.policy({"kind": "max_hours", "hours": 70, "window": "week"})
    draft = h.workspace("Advisory conflict label")

    def change(policy_id):
        with transaction(h.app.state.engine, write=True) as db:
            row = db.get(m.Policy, policy_id)
            row.definition = {**row.definition, "scope": {"person_ids": ["label-change"]}}
            row.revision += 1
            db.flush()
            workspace = db.get(m.Workspace, draft["id"])
            current = snapshot_inputs(db, db.get(m.Organisation, h.seed["organisation_id"]))
            _, _, changes = _diff(latest(db, workspace.id), workspace, current)
        return next(item for item in changes if item["source"] == "rules")

    assert change(advisory["id"])["mandatory"] is False
    assert change(mandatory["id"])["mandatory"] is True


def test_rules_change_label_reads_catalogue_strengths_safety_and_other_rule_inputs():
    from apps.api.workspace_baselines import _changes_mandatory_rule

    def catalogue(level, catalogue_id="fair_share"):
        line = {"id": "line", "numbers": {"apply": {"value": None, "level": level}}}
        return {"id": "rule", "definition": {"severity": "mandatory", "rule": {"kind": "catalogue", "id": catalogue_id,
                                                                               "settings": {}, "lines": [line]}}}

    def legacy(kind, severity):
        return {"id": "rule", "definition": {"severity": severity, "rule": {"kind": kind}}}

    def changed(before, after, **extra):
        return _changes_mandatory_rule({"policies": [before]}, {"policies": [after], **extra}, {"policies": [before]})

    medium = catalogue("medium")
    assert changed(medium, {**medium, "revision": 2}) is False
    assert changed(medium, catalogue("mandatory")) is True
    assert changed(medium, catalogue(None)) is True  # an inherited strength is not known to be advisory
    advisory = legacy("max_hours", "advisory")
    assert changed(advisory, {**advisory, "revision": 2}) is False
    assert changed(advisory, legacy("max_hours", "mandatory")) is True
    assert changed(advisory, legacy("restriction", "advisory")) is True  # a safety kind binds whatever it stores
    # Any other frozen rules input changing is not known to be advisory; a live one (exceptions) is never drift.
    assert changed(advisory, advisory, type_lead_ownership={"type": "lead"}) is True
    assert changed(advisory, advisory, exceptions=[{"id": "exception"}]) is False


def test_department_frozen_fields_and_live_complement_are_exhaustive(monkeypatch):
    from types import SimpleNamespace
    from apps.api import workspace_baselines as baselines
    frozen = set().union(*baselines.DEPARTMENT_SETTING_FIELDS.values())
    fields = set(m.Department.__mapper__.column_attrs.keys())
    assert frozen <= fields

    def assert_partition(columns):
        row = {field: "live:" + field for field in columns}
        captured = {field: "captured:" + field for field in columns}
        owned = baselines.frozen_department_settings({"departments": {"department": captured}}, "department")
        live = baselines.live_department_settings(row)
        assert set(owned) == frozen
        assert not set(owned) & set(live)
        assert set(owned) | set(live) == columns
        assert {**owned, **live} == {field: ("captured:" if field in frozen else "live:") + field
                                   for field in columns}

    assert_partition(fields)
    future_columns = fields | {"future_department_setting"}
    monkeypatch.setattr(baselines, "m", SimpleNamespace(Department=SimpleNamespace(
        __mapper__=SimpleNamespace(column_attrs=dict.fromkeys(future_columns)))))
    assert_partition(future_columns)


@pytest.mark.parametrize("field,groups", [
    ("generation_defaults", {"score", "auto"}), ("display_settings", {"display"}),
])
def test_department_setting_drift_is_limited_to_its_groups(field, groups):
    from apps.api.workspace_baselines import DEPARTMENT_SETTING_FIELDS, _drift_group_hash
    before = {"departments": {"department": {"generation_defaults": {}, "display_settings": {}}}}
    after = deepcopy(before)
    after["departments"]["department"][field] = {"changed": True}
    assert {source for source in DEPARTMENT_SETTING_FIELDS
            if _drift_group_hash(before, source) != _drift_group_hash(after, source)} == groups


@pytest.mark.parametrize("h", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)], indirect=True)
def test_refresh_keeps_only_the_chosen_department_setting_group(h):
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed["department_id"])
        department.display_settings = {"autoschedule_animation": {"mode": "off"}}
    draft = h.workspace("Independent setting refresh")
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed["department_id"])
        department.generation_defaults = {"scoring": {"wish_debt_cap": 7}}
        department.display_settings = {"autoschedule_animation": {"mode": "fly"}}
        department.weekend_days = [0, 1]
        department.revision += 1
    refreshed = h.command("coordinator", "workspace.baseline.refresh.apply", {
        "expected_baseline_hash": draft["snapshot_hash"], "decisions": {"display": "keep_draft"},
        "reason": "Keep captured animation and accept changed score settings",
    }, draft["id"], draft["revision"])
    snapshot = h.snapshot(refreshed["workspace"])
    assert snapshot["generation_options"]["scoring"]["wish_debt_cap"] == 7
    assert snapshot["departments"][h.seed["department_id"]]["display_settings"]["autoschedule_animation"]["mode"] == "off"
    assert snapshot["department"]["weekend_days"] == [0, 1]


@pytest.mark.parametrize("h", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)], indirect=True)
def test_deferred_generation_keeps_frozen_display_and_score_settings(h):
    from apps.worker.main import claim_job, prepare_deferred_generation
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed["department_id"])
        department.generation_defaults = {"scoring": {"wish_debt_cap": 2}}
        department.display_settings = {"autoschedule_animation": {"mode": "off"}}
    draft = h.shift(h.workspace("Frozen run settings"), people=[])["workspace"]
    job = h.command("coordinator", "job.create", {"workspace_id": draft["id"], "kind": "generation"},
                    revision=draft["revision"])["job"]
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed["department_id"])
        department.generation_defaults = {"scoring": {"wish_debt_cap": 7}}
        department.display_settings = {"autoschedule_animation": {"mode": "fly"}}
        department.weekend_days = [0, 1]
        department.revision += 1
        claim = claim_job(db)
    prepare_deferred_generation(h.app.state.engine, claim, h.settings)
    with transaction(h.app.state.engine) as db:
        snapshot = db.get(m.Job, job["id"]).input_snapshot
        assert snapshot["autoschedule_animation"]["mode"] == "off"
        assert snapshot["generation_options"]["scoring"]["wish_debt_cap"] == 2
        assert snapshot["generation_defaults"]["scoring"]["wish_debt_cap"] == 2
        assert GenerationOptions.model_validate(snapshot["department"]["generation_defaults"]).model_dump(mode="json") == snapshot["generation_options"]
        assert snapshot["department"]["weekend_days"] == [0, 1]
