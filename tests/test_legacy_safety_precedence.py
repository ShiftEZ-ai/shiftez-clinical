"""Supported legacy rule projections retain every matching protected safety obligation."""
import copy
from datetime import datetime, timedelta

import pytest

from packages.contracts import schemas as s
from packages.contracts.schemas import PolicyDefinition
from packages.domain.conditions import active_rules
from packages.domain.cp_sat import generate as cp_generate
from packages.domain.generation import generate as greedy_generate
from packages.domain.rule_catalogue_migration import after_snapshot, plan_v2
from packages.domain.rule_migration_proof import strict_expanded
from packages.domain.validation import evaluate
from tests.test_autoscheduler_audit_safety import _apply, _assignment
from tests.test_rule_templates import activate, propose


SAFETY_RULES = {
    'restriction': {},
    'min_rest': {'hours': 12},
    'forbidden_transition': {'from_type_ids': ['00000000-0000-4000-8000-000000000001'],
                             'to_type_ids': ['00000000-0000-4000-8000-000000000001']},
    'supervision_limit': {'mode': 'capacity', 'maximum': 2},
    'minimum_cover': {'minimum': 2},
    'maximum_participants': {'maximum': 2},
    'team_lead_requirement': {'minimum': 1, 'maximum': 1},
    'spread_evenly': {'holder': 'supervisors', 'within': 'session', 'mode': 'levelled', 'maximum_gap': 1},
}
SAFETY_KINDS = sorted(s.NEVER_OVERRIDDEN)


def test_historical_safety_fixture_set_matches_contract():
    assert set(SAFETY_RULES) == set(SAFETY_KINDS)


@pytest.mark.parametrize('rule', [
    {'kind': kind, **SAFETY_RULES[kind]} for kind in SAFETY_KINDS
], ids=lambda rule: rule['kind'])
def test_historical_overrides_preserve_every_protected_safety_kind(rule):
    protected = {'id': 'protected', 'status': 'active', 'definition':
                 PolicyDefinition.model_validate({'rule': rule}).model_dump(mode='json')}
    legacy_override = {'id': 'legacy', 'status': 'active', 'definition': {
        'scope': {}, 'rule': {'kind': 'max_hours', 'hours': 48}, 'overrides': ['protected']}}
    resolved = active_rules([protected, legacy_override])
    assert next(p for p in resolved if p['id'] == protected['id']) == protected
    assert active_rules(resolved) == resolved


@pytest.mark.parametrize('converted', [False, True], ids=['legacy', 'catalogue-v2'])
@pytest.mark.parametrize('hours,gap', [(1, 8), (16, 14)], ids=['weaker-person', 'stricter-person'])
def test_legacy_person_rest_line_preserves_global_and_stricter_obligations(h, converted, hours, gap):
    person = h.seed['people'][0]
    saved = activate(h, propose(h, {'kind': 'min_rest', 'hours': 12},
        per_person=[{'person_id': person, 'amount': hours}]))
    workspace = h.shift(h.workspace(), start='2026-10-04T08:00:00+03:00',
                        end='2026-10-04T20:00:00+03:00', people=[])['workspace']
    start = datetime.fromisoformat('2026-10-04T20:00:00+03:00') + timedelta(hours=gap)
    workspace = h.shift(workspace, start=start.isoformat(), end=(start + timedelta(hours=4)).isoformat(),
                        people=[])['workspace']
    data = h.snapshot(workspace)
    data['people'] = {person: data['people'][person]}
    data['generation_options'] = {'mode': 'fill_gaps', 'shortlist_width': 0}
    if converted:
        from sqlalchemy import select
        from apps.api import models as m
        from apps.api.db import transaction
        from apps.api.services.scoring_defaults import seeded_rows
        from scripts.system_command import MAINTENANCE_ISSUER, MAINTENANCE_SUBJECT
        with transaction(h.app.state.engine) as db:
            policies = seeded_rows(db, data['policies'])
            default_actor_id = db.scalar(select(m.Identity.id).where(m.Identity.issuer == MAINTENANCE_ISSUER,
                m.Identity.subject == MAINTENANCE_SUBJECT, m.Identity.disabled.is_(True)))
        assert default_actor_id
        proposal = plan_v2(policies, department_id=data['workspace']['department_id'],
                           default_actor_id=default_actor_id)
        assert not proposal['blocked']
        converted_data = after_snapshot(data, proposal)
        assert strict_expanded(data, converted_data)['equal']
        data = converted_data
    assigned = copy.deepcopy(data)
    for shift in assigned['shifts']:
        shift['participants'] = [_assignment(shift, person)]
    findings = [f for f in evaluate(assigned)['findings'] if f['code'] == 'MIN_REST']
    assert any(f['rule_id'] == saved['id'] and f['severity'] == 'error'
               and f['detail']['required_hours'] == max(12, hours) for f in findings), findings
    for generate in (cp_generate, greedy_generate):
        result = generate(data, time_budget_seconds=3)
        candidate = _apply(data, result)
        assert sum(len(s['participants']) for s in candidate['shifts']) < 2, result
        assert result['status'] != 'candidate_feasible', result


def test_historical_override_cannot_remove_a_protected_rest_rule(h):
    from apps.api import models as m
    from apps.api.db import transaction

    person = h.seed['people'][0]
    baseline = h.policy({'kind': 'min_rest', 'hours': 12})
    replacement = h.policy({'kind': 'min_rest', 'hours': 1}, scope={'person_id': person})
    # Legacy records remain readable even if their override would now be refused on admission.
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Policy, replacement['id'])
        row.definition = {**row.definition, 'overrides': [baseline['id']]}
    workspace = h.shift(h.workspace(), start='2026-10-04T08:00:00+03:00',
                        end='2026-10-04T20:00:00+03:00', people=[person])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T04:00:00+03:00',
                        end='2026-10-05T08:00:00+03:00', people=[person])['workspace']
    data = h.snapshot(workspace)
    copies = active_rules(data['policies'], context=data)
    protected = next(p for p in copies if p['id'] == baseline['id'])
    assert 'overridden_by' not in str(protected['definition']['scope'])
    assert any(f['code'] == 'MIN_REST' and f['rule_id'] == baseline['id'] and f['severity'] == 'error'
               for f in evaluate(data)['findings'])
    data['people'] = {person: data['people'][person]}
    data['generation_options'] = {'mode': 'rebalance', 'shortlist_width': 0}
    for generate in (cp_generate, greedy_generate):
        result = generate(data, time_budget_seconds=3)
        candidate = _apply(data, result)
        assert result['status'] != 'candidate_feasible', result
        if result['additions'] or result['removals']:
            assert sum(len(s['participants']) for s in candidate['shifts']) < 2, result
