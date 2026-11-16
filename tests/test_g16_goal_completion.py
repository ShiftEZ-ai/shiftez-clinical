"""maintenance probe for converted Fair-share target facts whose goal is incomplete."""
import copy
import json
from datetime import date

import pytest

from apps.api import models as m
from apps.api.auth import Actor
from apps.api.db import transaction
from apps.api.snapshots import domain_snapshot
from apps.api.services import fairness as fairness_service
from packages.domain.conditions import active_rules
from packages.domain.cp_sat import generate
from packages.domain.rule_catalogue_migration import after_snapshot, plan_v2
from packages.domain.rule_lines import bound_policies
from packages.domain.solver_rules import definition_issues
from packages.domain.validation import evaluate
from .test_rule_catalogue_v2_domain import catalogue, line, who
from .test_rule_catalogue_v2_proof import uid


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def _coordinator(h, db):
    identity = db.get(m.Identity, h.seed["identities"]["coordinator"])
    department = db.get(m.Department, h.seed["department_id"])
    return Actor(identity.id, department.id, department.organisation_id, identity.display_name,
                 frozenset({"coordinator"}), identity.person_id, "g16-test")


def _published_complete_goal(h):
    goal = h.policy({'kind': 'balance_goal', 'metric': 'duties', 'period': 'rota', 'aim': 'equal_share'},
                    severity='advisory')
    workspace = h.workspace()
    workspace = h.shift(workspace, people=[h.seed['people'][0]])['workspace']
    published = h.publish(workspace)
    return goal, published['publication_id']


def _converted_retired_target(h):
    workspace = h.workspace(start="2026-10-01", end="2026-11-01")
    h.shift(workspace, start="2026-10-01T08:00:00+03:00", end="2026-10-01T12:00:00+03:00")
    data = h.snapshot(workspace)
    person = h.seed["people"][0]
    source = catalogue("fair_share", [
        line(uid(200), {"apply": {"value": None, "level": "medium"}},
             settings={"balance_goal": {"metric": "hours", "aim": "equal_share"}}),
        line(uid(201), {"target": {"value": 8, "level": "medium"}}, who(person),
             settings={"balance_goal": {"metric": "hours", "aim": "target"}}),
    ])
    source["department_id"] = data["workspace"]["department_id"]
    proposal = plan_v2([source], department_id=source["department_id"])
    assert not proposal["blocked"], proposal["blocked"]
    converted = after_snapshot({**data, "policies": [source], "source_bindings": [], "scheduling_facts": []}, proposal)
    destination = converted["policies"][0]
    destination.update(status="retired", active=False)
    fact = converted["scheduling_facts"][0]
    fact["data"]["line"]["settings"]["balance_goal"].pop("metric")
    return converted


def test_probe_retired_converted_personal_target_is_excluded_without_inventing_a_metric(h):
    data = _converted_retired_target(h)
    original = copy.deepcopy(data)
    read_quality = {}
    policy_id = data["source_bindings"][0]["source_policy_id"]
    assert active_rules(data["policies"], context=data, read_quality=read_quality) == []
    assert read_quality["_fair_share_goal_read_quality"] == {
        "count": 1, "policy_ids": [policy_id], "message": "1 rules stored incomplete"}
    assert data == original
    result = evaluate(data)
    assert any(finding["code"] == "FAIR_SHARE_GOAL_INCOMPLETE" and finding["severity"] == "warning"
               and finding["rule_id"] == policy_id
               for finding in result["findings"])
    assert data == original


def test_conversion_stores_a_complete_personal_fair_share_goal():
    person = uid(100)
    source = catalogue("fair_share", [
        line(uid(210), {"target": {"value": 8, "level": "medium"}}, who(person),
             settings={"balance_goal": {"aim": "target"}}),
    ], settings={"balance_goal": {"metric": "hours", "aim": "target"}})
    source["department_id"] = uid(10)
    proposal = plan_v2([source], department_id=source["department_id"])
    assert not proposal["blocked"], proposal["blocked"]
    fact = next(row for row in proposal["scheduling_facts"] if row["kind"] == "target")
    settings = fact["data"]["line"]["settings"]["balance_goal"]
    assert settings["metric"] == "hours" and settings["period"] == "rolling"
    assert settings["share_by"] == "available_days"


def test_read_time_completion_uses_the_parent_goal_metric_without_mutating_storage():
    source = catalogue("fair_share", [
        line(uid(220), {"apply": {"value": None, "level": "medium"}},
             settings={"balance_goal": {"aim": "equal_share"}}),
    ], settings={"balance_goal": {"metric": "hours"}})
    source["department_id"] = uid(10)
    context = {"policies": [source], "source_bindings": [], "scheduling_facts": []}
    original = copy.deepcopy(context)
    read_quality = {}
    [resolved] = bound_policies([source], context, read_quality=read_quality)
    assert resolved["definition"]["rule"]["lines"][0]["settings"]["balance_goal"]["metric"] == "hours"
    assert read_quality["_fair_share_goal_findings"] == []
    assert read_quality["_fair_share_goal_read_quality"] == {"count": 0, "policy_ids": [],
                                                               "message": "0 rules stored incomplete"}
    assert context == original


def test_fair_share_reads_never_write_caller_data(h):
    goal = h.policy({'kind': 'balance_goal', 'metric': 'duties', 'aim': 'equal_share'}, severity='advisory')
    goal['definition']['rule'].pop('metric')
    snapshot = h.snapshot(h.workspace())
    caller = dict(snapshot)
    caller['policies'] = [*snapshot['policies'], goal]

    bound_policies(caller['policies'], context=caller)
    assert not any(key.startswith('_fair_share_') for key in caller)
    active_rules(caller['policies'], context=caller)
    assert not any(key.startswith('_fair_share_') for key in caller)
    evaluate(caller)
    assert not any(key.startswith('_fair_share_') for key in caller)


def test_domain_snapshot_retains_fair_share_findings_only_for_omitted_goals(h):
    goal = h.policy({'kind': 'balance_goal', 'metric': 'duties', 'aim': 'equal_share'}, severity='advisory')
    goal_id = goal['id']
    workspace = h.workspace()

    snapshot = h.snapshot(workspace)
    invalid = copy.deepcopy(next(policy for policy in snapshot['policies'] if policy['id'] == goal_id))
    invalid['definition']['rule'].pop('metric')
    snapshot['policies'] = [invalid if policy['id'] == goal_id else policy for policy in snapshot['policies']]
    snapshot.read_quality.clear()
    materialized = bound_policies(snapshot['policies'], context=snapshot)
    assert snapshot.read_quality['_fair_share_goal_findings'][0]['rule_id'] == goal_id
    snapshot['policies'] = materialized
    active_rules(snapshot, context=snapshot)
    assert snapshot.read_quality['_fair_share_goal_findings'][0]['rule_id'] == goal_id

    deleted = h.snapshot(workspace)
    catalogue_goal = catalogue('fair_share', [
        line(uid(260), {'apply': {'value': None, 'level': 'medium'}},
             settings={'balance_goal': {'metric': 'duties', 'aim': 'equal_share'}}),
        line(uid(261), {'apply': {'value': None, 'level': 'medium'}},
             settings={'balance_goal': {'aim': 'equal_share'}}),
    ])
    deleted['policies'].append(catalogue_goal)
    deleted.read_quality.clear()
    materialized = bound_policies(deleted['policies'], context=deleted)
    assert deleted.read_quality['_fair_share_goal_findings']
    deleted['policies'] = [policy for policy in materialized if policy['id'] != catalogue_goal['id']]
    active_rules(deleted, context=deleted)
    assert deleted.read_quality['_fair_share_goal_findings'] == []

    fixed = h.snapshot(workspace)
    invalid = copy.deepcopy(next(policy for policy in fixed['policies'] if policy['id'] == goal_id))
    invalid['definition']['rule'].pop('metric')
    fixed['policies'] = [invalid if policy['id'] == goal_id else policy for policy in fixed['policies']]
    fixed.read_quality.clear()
    materialized = bound_policies(fixed['policies'], context=fixed)
    assert fixed.read_quality['_fair_share_goal_findings']
    fixed_goal = next(policy for policy in h.snapshot(workspace)['policies'] if policy['id'] == goal_id)
    fixed['policies'] = [*materialized, fixed_goal]
    active_rules(fixed, context=fixed)
    assert fixed.read_quality['_fair_share_goal_findings'] == []


@pytest.mark.parametrize('catalogue_goal', [False, True])
@pytest.mark.parametrize('corrupt,problem', [
    ({'share_by': 'invalid'}, 'share_by'),
    ({'share_by': 'offered'}, 'overtime'),
    ({'amount': -1}, 'amount'),
    ({'period': 'rolling', 'period_days': None, 'period_months': None}, 'rolling'),
], ids=['share-choice', 'share-metric', 'target', 'window'])
def test_corrupt_stored_goal_is_reported_without_breaking_snapshot_evaluation_or_home(h, catalogue_goal, corrupt, problem):
    goal = h.policy({'kind': 'balance_goal', 'metric': 'duties', 'aim': 'target', 'amount': 4,
                     'period': 'rota'}, severity='advisory')
    workspace = h.shift(h.workspace(), people=[])['workspace']
    data = h.snapshot(workspace)
    if catalogue_goal:
        goal = next(row for row in data['policies'] if row['definition']['rule'].get('id') == 'fair_share')
    policy = next(row for row in data['policies'] if row['id'] == goal['id'])
    if catalogue_goal:
        template = catalogue('fair_share', [line(uid(230), {'target': {'value': 4, 'level': 'medium'}},
            settings={'balance_goal': {'metric': 'duties', 'aim': 'target', 'period': 'rota'}})])
        policy.update(kind='catalogue', definition=template['definition'])
        target = policy['definition']['rule']['lines'][0]
        if 'amount' in corrupt:
            target['numbers']['target']['value'] = corrupt['amount']
        else:
            target['settings']['balance_goal'].update(corrupt)
    else:
        policy['definition']['rule'].update(corrupt)
    assert definition_issues(policy['definition'])  # The authoring gate rejects the legacy corruption.
    original = copy.deepcopy(data)
    read_quality = {}
    assert active_rules([policy], read_quality=read_quality) == []
    assert read_quality['_fair_share_goal_findings'][0]['rule_id'] == goal['id']
    resolved = active_rules(data, read_quality=read_quality)
    assert goal['id'] not in {row['id'] for row in resolved}
    assert resolved  # The healthy built-in workload goal still reads normally.
    assert read_quality['_fair_share_goal_read_quality']['policy_ids'] == [goal['id']]
    assert read_quality['_fair_share_goal_read_quality']['count'] == 1
    finding = next(row for row in evaluate(data)['findings'] if row['rule_id'] == goal['id'])
    assert finding['code'] == 'FAIR_SHARE_GOAL_INVALID' and finding['severity'] == 'warning'
    assert problem in finding['message'] and goal['id'] in finding['message']
    assert data == original
    with transaction(h.app.state.engine, write=True) as db:
        stored = db.get(m.Policy, goal['id'])
        stored.kind, stored.definition = policy['kind'], copy.deepcopy(policy['definition'])
        stored.revision += 1
    with transaction(h.app.state.engine) as db:
        snapshot = domain_snapshot(db, db.get(m.Workspace, workspace['id']), current_scheduling=True)
    assert goal['id'] not in {row['id'] for row in snapshot['policies']}
    assert any(row['code'] == 'FAIR_SHARE_GOAL_INVALID' and row['rule_id'] == goal['id']
               for row in evaluate(snapshot)['findings'])
    entry = h.get('coordinator', '/api/rules/catalogue/fair_share/detail?day=2026-10-04', status=200)['entry']
    assert any(row['code'] == 'FAIR_SHARE_GOAL_INVALID' and row['rule_id'] == goal['id']
               and problem in row['message'] for row in entry['findings'])
    assert 'attention' in h.get('coordinator', '/api/home', status=200)


@pytest.mark.parametrize('weight', [1e-15, 0.03125])
@pytest.mark.parametrize('catalogue_goal', [False, True])
def test_reading_unsupported_goal_precision_never_weakens_authoring_validation(weight, catalogue_goal):
    policy = catalogue('fair_share', [line(uid(240), {'apply': {'value': None, 'level': 'medium'}},
        settings={'balance_goal': {'metric': 'duties', 'aim': 'equal_share', 'weight': 1}})])
    if not catalogue_goal:
        policy.update(kind='balance_goal')
        policy['definition']['severity'] = 'advisory'
        policy['definition']['rule'] = {'kind': 'balance_goal', 'metric': 'duties', 'aim': 'equal_share',
                                         'period': 'rota', 'weight': 1}
    assert definition_issues(policy['definition']) == []
    settings = (policy['definition']['rule']['lines'][0]['settings']['balance_goal'] if catalogue_goal
                else policy['definition']['rule'])
    settings['weight'] = weight
    context = {'policies': [policy]}
    original = copy.deepcopy(context)
    [resolved] = active_rules(context)
    assert resolved['definition']['rule']['weight'] == weight
    assert context == original
    assert any('decimal places' in issue['message'] for issue in definition_issues(policy['definition']))


def test_corrupt_catalogue_goal_weight_still_reaches_defensive_arithmetic(h, monkeypatch):
    from packages.domain import cp_sat

    goal = h.policy({'kind': 'balance_goal', 'metric': 'duties', 'aim': 'equal_share'}, severity='advisory')
    workspace = h.workspace()
    for day in (4, 5, 6):
        workspace = h.shift(workspace, start=f'2026-10-{day:02}T08:00:00+03:00',
                            end=f'2026-10-{day:02}T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(workspace)
    policy = next(row for row in data['policies'] if row['id'] == goal['id'])
    template = catalogue('fair_share', [line(uid(250), {'apply': {'value': None, 'level': 'medium'}},
        settings={'balance_goal': {'metric': 'duties', 'aim': 'equal_share', 'weight': 1}})])
    policy.update(kind='catalogue', definition=template['definition'])
    policy['definition']['rule']['lines'][0]['settings']['balance_goal']['weight'] = 1e-15
    assert generate(data, 5)['status'] == 'unsupported_rules'
    monkeypatch.setattr(cp_sat, 'compatibility_issues', lambda snapshot: [])
    result = generate(data, 5)
    assert result['status'] == 'invalid_inputs', result
    assert result['issues'][0]['code'] == 'SCORE_PRECISION_UNSUPPORTED'
    assert result['additions'] == result['removals'] == []


def test_complete_published_fairness_read_is_identical_without_completion_and_keeps_policy_identity(h, monkeypatch):
    goal, publication_id = _published_complete_goal(h)
    with transaction(h.app.state.engine) as db:
        actor = _coordinator(h, db)
        with monkeypatch.context() as patches:
            with_completion = fairness_service._goals_uncached(
                db, actor, starts_on=date(2026, 10, 1), ends_on=date(2026, 11, 1))
            patches.setattr(
                'packages.domain.rule_lines._complete_fair_share_goals',
                lambda policies, read_quality, context=None: list(policies))
            without_completion = fairness_service._goals_uncached(
                db, actor, starts_on=date(2026, 10, 1), ends_on=date(2026, 11, 1))
        policy = next(row for row in with_completion['goals'] if row['policy_id'] == goal['id'])
        assert policy['score'] is not None and policy['trend']
        snapshot = domain_snapshot(db, db.get(m.Workspace, db.get(m.Publication, publication_id).workspace_id),
                                   current_scheduling=True)
        complete = next(row for row in snapshot['policies'] if row['id'] == goal['id'])
        assert bound_policies([complete], snapshot)[0] is complete
    assert _bytes(with_completion) == _bytes(without_completion)


def test_incomplete_policy_in_published_snapshot_keeps_issued_fairness_without_rescoring(h, monkeypatch):
    goal, publication_id = _published_complete_goal(h)
    with transaction(h.app.state.engine) as db:
        publication = db.get(m.Publication, publication_id)
        issued_validation = copy.deepcopy(publication.validation)
        snapshot = copy.deepcopy(publication.snapshot)
        incomplete = copy.deepcopy(next(row for row in snapshot.get('policies', [])
                                         if row['id'] == goal['id'])) if snapshot.get('policies') else None
        if incomplete is None:
            incomplete = {
                'id': goal['id'], 'definition': {'rule': {'kind': 'balance_goal', 'aim': 'equal_share'}},
            }
        else:
            incomplete['definition']['rule'].pop('metric', None)
        snapshot.setdefault('policies', []).append(incomplete)

    def no_rescore(*_args, **_kwargs):
        raise AssertionError('published snapshot read must not rescore fairness')

    from apps.api import main
    original_in_force_snapshot = main.in_force_snapshot

    def published_snapshot_with_incomplete_row(db, row):
        return snapshot if row.id == publication_id else original_in_force_snapshot(db, row)

    monkeypatch.setattr(main, 'in_force_snapshot', published_snapshot_with_incomplete_row)
    monkeypatch.setattr(main, 'evaluate', no_rescore)
    monkeypatch.setattr('packages.domain.validation.evaluate', no_rescore)
    monkeypatch.setattr('packages.domain.rule_lines.bound_policies', no_rescore)
    body = h.get('coordinator', f'/api/publications/{publication_id}')
    assert _bytes(body['original_validation']) == _bytes(issued_validation)
    published_row = body['snapshot']['policies'][-1]
    assert published_row['definition']['rule']['kind'] == 'balance_goal'
    assert published_row['definition']['rule'].get('metric') is None
