"""One quota rule holds its minimum, maximum and an optional advisory target (docs/design/rule-simplification.md, §9).

The bounds keep their meaning (mandatory or advisory, per rule); the target is always advisory: scored, never a
hard constraint. A rule without a target reads exactly as before targets existed (equivalence below)."""
import copy

import pytest
from pydantic import ValidationError

from packages.contracts.schemas import BlockQuota, PolicyDefinition
from packages.domain.conditions import active_rules
from packages.domain.rule_groups import canonical_model
from packages.domain.score import schedule_score
from packages.domain.validation import evaluate


def rule(**changes):
    return {'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'metric': 'duties',
            'comparison': 'minimum', 'amount': 1, **changes}


def policy(r, severity='mandatory', per_person=(), listed_only=False, pid='Q'):
    return {'id': pid, 'status': 'active', 'active': True, 'name': 'Quota', 'kind': 'block_quota', 'starts_on': '2026-10-01',
            'ends_on': None, 'department_id': 'd',
            'definition': {'scope': {}, 'severity': severity, 'strength': 'high', 'exception_allowed': False, 'rule': r,
                           'per_person': list(per_person), 'listed_only': listed_only, 'overrides': []}}


def parts(policies):
    return [(c['definition']['rule']['comparison'], c['definition']['rule']['amount'], c['definition']['severity'],
             c['definition'].get('strength'), (c.get('template_line') or {}).get('person_id')) for c in active_rules(policies)]


# The contract ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('changes,message', [
    ({'comparison': 'target', 'target_amount': 2}, 'already a target'),
    ({'comparison': 'minimum', 'amount': 3, 'target_amount': 2}, 'within the minimum and maximum'),
    ({'comparison': 'maximum', 'amount': 3, 'target_amount': 4}, 'within the minimum and maximum'),
    ({'comparison': 'range', 'amount': 2, 'maximum_amount': 4, 'target_amount': 5}, 'within the minimum and maximum'),
    ({'target_strength': 'high'}, 'needs a target'),
    ({'amount': 1.5}, 'whole-number'),
])
def test_a_target_lies_within_the_rules_limits(changes, message):
    with pytest.raises(ValidationError, match=message):
        BlockQuota.model_validate(rule(**changes))


def test_valid_targets_on_every_bound():
    for changes in ({'comparison': 'minimum', 'amount': 2, 'target_amount': 4}, {'comparison': 'maximum', 'amount': 4, 'target_amount': 0},
                    {'comparison': 'range', 'amount': 2, 'maximum_amount': 4, 'target_amount': 3, 'target_strength': 'low'}):
        BlockQuota.model_validate(rule(**changes))


def test_linked_quota_sets_are_refused_clearly():
    with pytest.raises(ValidationError, match='linked quota set, which is no longer used'):
        PolicyDefinition.model_validate({'rule': rule(), 'authoring_set_id': '00000000-0000-4000-8000-000000000001'})


def test_a_line_sets_its_own_numbers_within_its_own_limits():
    base = {'rule': rule(comparison='range', amount=2, maximum_amount=4, target_amount=3), 'severity': 'mandatory'}
    PolicyDefinition.model_validate({**base, 'per_person': [{'person_id': '00000000-0000-4000-8000-000000000001', 'amount': 5,
                                                              'settings': {'maximum_amount': 8, 'target_amount': 6}}]})
    with pytest.raises(ValidationError, match='Line 1: The target must lie within'):
        PolicyDefinition.model_validate({**base, 'per_person': [{'person_id': '00000000-0000-4000-8000-000000000001',
                                                                  'settings': {'target_amount': 9}}]})
    # Only quota lines carry these numbers.
    with pytest.raises(ValidationError, match='cannot set its own'):
        PolicyDefinition.model_validate({'rule': {'kind': 'max_duties', 'maximum': 3}, 'per_person': [
            {'person_id': '00000000-0000-4000-8000-000000000001', 'settings': {'target_amount': 2}}]})


# How every check reads it (active_rules) -------------------------------------------------------------------------

def test_bounds_then_an_advisory_target_each_with_the_rules_id():
    assert parts([policy(rule(target_amount=3))]) == [('minimum', 1, 'mandatory', 'high', None), ('target', 3, 'advisory', 'high', None)]
    assert parts([policy(rule(comparison='range', amount=1, maximum_amount=4, target_amount=3, target_strength='low'))]) == [
        ('minimum', 1, 'mandatory', 'high', None), ('maximum', 4, 'mandatory', 'high', None), ('target', 3, 'advisory', 'low', None)]
    copies = active_rules([policy(rule(target_amount=3))])
    assert {c['id'] for c in copies} == {'Q'} and not copies[1]['definition']['exception_allowed']
    assert all('target_amount' not in c['definition']['rule'] and 'target_strength' not in c['definition']['rule'] for c in copies)
    # Idempotent: the copies are read again unchanged (checks call it on already-read rules).
    assert active_rules(copies) == copies


def test_lines_carry_their_own_numbers():
    lines = [{'person_id': 'A', 'amount': 2, 'settings': {'maximum_amount': 6, 'target_amount': 5}}, {'person_id': 'B'},
             {'person_id': 'C', 'settings': {'target_amount': 2}}]
    got = parts([policy(rule(comparison='range', amount=1, maximum_amount=4), per_person=lines, listed_only=True)])
    assert got == [('minimum', 2, 'mandatory', 'high', 'A'), ('minimum', 1, 'mandatory', 'high', 'B'), ('minimum', 1, 'mandatory', 'high', 'C'),
                   ('maximum', 6, 'mandatory', 'high', 'A'), ('maximum', 4, 'mandatory', 'high', 'B'), ('maximum', 4, 'mandatory', 'high', 'C'),
                   ('target', 5, 'advisory', 'high', 'A'), ('target', 2, 'advisory', 'high', 'C')]


def test_a_rule_without_a_target_reads_as_before():
    new = policy(BlockQuota.model_validate(rule(comparison='range', amount=1, maximum_amount=4)).model_dump(mode='json'))  # empty target fields
    old = policy({k: v for k, v in new['definition']['rule'].items() if k not in ('target_amount', 'target_strength')})  # saved before targets
    assert 'target_amount' in new['definition']['rule']
    assert active_rules([new]) == active_rules([old])
    plain = policy(rule())
    assert active_rules([plain])[0] is plain  # untouched: the same object


# Validator, score and CP-SAT --------------------------------------------------------------------------------------

def add_quota(h, **changes):
    return h.policy(rule(starts_on='2026-10-04', ends_on='2026-10-11', **changes), {'person_id': h.seed['people'][0]})


def test_a_missed_target_is_a_warning_and_scored_never_a_breach(h):
    q = add_quota(h, comparison='minimum', amount=1, target_amount=3)
    result = h.shift(h.workspace())['validation']
    rows = {r['comparison']: r for r in result['quota_report']['rows'] if r['policy_id'] == q['id']}
    assert rows['minimum']['status'] == 'met' and rows['minimum']['severity'] == 'mandatory'
    assert rows['target']['status'] == 'below_target' and rows['target']['severity'] == 'advisory' and rows['target']['amount'] == 3
    assert result['valid'] and [f['severity'] for f in result['findings'] if f['code'] == 'QUOTA_BELOW_TARGET'] == ['warning']


def test_score_counts_the_target_under_rules(h):
    add_quota(h, comparison='minimum', amount=0, target_amount=2)
    w = h.shift(h.workspace())['workspace']
    data = h.snapshot(w)
    score = schedule_score(data, evaluate(data))
    plain = without_target(data)
    for p in plain['policies']:
        p['definition']['rule'].pop('target_amount', None)
    unscored = schedule_score(plain, evaluate(plain))
    assert score['components']['rules']['value'] == 0.5
    assert score['components']['rules'] != unscored['components']['rules']
    # Adding Rules redistributes public lost points; the underlying fairness value/weight is unchanged.
    for field in ('value', 'weight'):
        assert score['components']['fairness'][field] == unscored['components']['fairness'][field]


def test_an_existing_range_gains_a_target_by_editing_through_approval(h):
    """The baseline's quotas (e.g. "Internship weekly clinical range", 6–7) gain a target as a new version."""
    old = add_quota(h, comparison='range', amount=1, maximum_amount=3)
    definition = copy.deepcopy(old['definition'])
    definition['rule'].update(target_amount=2, target_strength='high')
    definition['replaces_id'] = old['id']
    proposal = h.command('coordinator', 'policy.create', {'name': old['name'], 'owner': old['owner'], 'source': 'Add a target',
                                                          'starts_on': old['starts_on'], 'definition': definition})['record']
    h.command('approver', 'policy.activate', {'reason': 'Independent review'}, proposal['id'], proposal['revision'])
    data = h.snapshot(h.shift(h.workspace())['workspace'])
    live = [p for p in data['policies'] if p['id'] in (old['id'], proposal['id'])]
    assert [(p['id'], p['status']) for p in live if p['status'] == 'active'] == [(proposal['id'], 'active')]
    rows = [(r['comparison'], r['amount']) for r in evaluate(data)['quota_report']['rows'] if r['policy_id'] == proposal['id']]
    assert sorted(rows) == [('maximum', 3), ('minimum', 1), ('target', 2)]


def without_target(data):
    out = copy.deepcopy(data)
    for p in out['policies']:
        r = p['definition']['rule']
        if r['kind'] == 'block_quota':
            r.pop('target_amount', None), r.pop('target_strength', None)
    return out


def test_equivalence_without_targets_findings_quota_rows_score_and_model(h):
    """Rules saved with empty target fields check exactly as rules saved before targets existed."""
    add_quota(h, comparison='range', amount=1, maximum_amount=2)
    add_quota(h, comparison='maximum', amount=1)
    w = h.shift(h.shift(h.workspace())['workspace'], start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')['workspace']
    now = h.snapshot(w)
    assert any('target_amount' in p['definition']['rule'] for p in now['policies'] if p['definition']['rule']['kind'] == 'block_quota')
    before = without_target(now)
    a, b = evaluate(copy.deepcopy(before)), evaluate(copy.deepcopy(now))
    assert a['findings'] == b['findings'] and a['quota_report']['rows'] == b['quota_report']['rows']
    assert schedule_score(before, a)['score'] == schedule_score(now, b)['score']
    assert canonical_model(before) == canonical_model(now)


@pytest.mark.parametrize('feasibility_only', [False, True])
@pytest.mark.parametrize('minimum,status', [(1, 'candidate_feasible'), (2, 'requirements_conflict')])
def test_cp_sat_keeps_the_target_soft(h, feasibility_only, minimum, status):
    from packages.domain.cp_sat import generate

    q = add_quota(h, comparison='minimum', amount=minimum, target_amount=3)
    data = h.snapshot(h.shift(h.workspace(), people=[])['workspace'])
    # One session cannot meet the target of three or a mandatory minimum of two.
    for snapshot in (data, without_target(data)):
        result = generate(snapshot, 5, feasibility_only=feasibility_only)
        assert result['status'] == status, result
        if status == 'requirements_conflict':
            assert result['additions'] == result['removals'] == []
            continue
        assert result['validation']['valid']
        rows = {row['comparison']: row for row in result['validation']['quota_report']['rows'] if row['policy_id'] == q['id']}
        assert (rows['minimum']['actual'], rows['minimum']['amount'], rows['minimum']['status']) == (1, 1, 'met')
        if snapshot is data:
            assert (rows['target']['actual'], rows['target']['amount'], rows['target']['status']) == (1, 3, 'below_target')
            assert [f['severity'] for f in result['validation']['findings']
                    if f['rule_id'] == q['id'] and f['code'] == 'QUOTA_BELOW_TARGET'] == ['warning']
        else:
            assert 'target' not in rows
