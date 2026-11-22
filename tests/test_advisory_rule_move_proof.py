"""Catalogue moves preserve realistic advisory constraints despite helper creation order."""
import copy

import pytest
from ortools.sat.python import cp_model

from packages.domain.rule_catalogue_migration import SAFETY_REVIEW_REASON, id_map, plan
from packages.domain.rule_groups import canonical_model, expanded, same_checks, same_model
from packages.domain.validation import evaluate
from tests.scheduler_departments import department
from tests.test_rule_simplification import rule
from tests.test_rule_model_proof import deficit_model, proof_of_model  # noqa: F401


@pytest.mark.parametrize('kind', ['min_rest', 'forbidden_transition', 'minimum_cover'])
def test_advisory_department_rules_move_without_false_model_refusals(h, kind):
    data, info = department(h, 73, people=12, days=4, rules='none', leave_rate=0)
    people = h.seed['people'][:2]
    if kind == 'min_rest':
        bodies = [{'kind': kind, 'hours': hours} for hours in (11, 16)]
        scopes = [{'person_id': person} for person in people]
    elif kind == 'forbidden_transition':
        bodies = [{'kind': kind, 'from_type_ids': [info['types']['night']], 'to_type_ids': [info['types'][to]]}
                  for to in ('am', 'pm')]
        scopes = [{'person_id': person} for person in people]
    else:
        bodies = [{'kind': kind, 'minimum': minimum} for minimum in (1, 2)]
        scopes = [{'assignment_type_id': info['types'][typ]} for typ in ('am', 'pm')]
    rules = [{**rule(name, body, scope, severity='advisory', strength='high'),
              'department_id': data['workspace']['department_id']}
             for name, body, scope in zip(('Z first in force', 'A second in force'), bodies, scopes)]
    # Safety kinds are always mandatory. Conversion waits for approval of a mandatory version.
    proposals, report = plan(rules, {}, data['organisation']['week_start'], data['workspace']['department_id'])
    assert not proposals and {row['rule_id'] for row in report} == {row['id'] for row in rules}
    assert all(SAFETY_REVIEW_REASON in row['reason'] for row in report)
    for policy in rules:
        policy['definition'].update(severity='mandatory', strength='medium')
    data['policies'] = rules
    proposals, report = plan(rules, {}, data['organisation']['week_start'], data['workspace']['department_id'])
    assert report == [] and sum(len(p['rule_ids']) for p in proposals) == len(rules)
    moved = id_map(proposals)
    after = {**data, 'policies': [p['row'] for p in proposals]}
    assert expanded(rules, moved) == expanded(after['policies'])
    checks = same_checks(evaluate(data), evaluate(after), moved)
    assert checks['quota_equal'] and not checks['findings_only_before'] and not checks['findings_only_after'], checks
    before_model, after_model = canonical_model(data), canonical_model(after)
    assert before_model[0] == after_model[0] == 'model_ready'
    assert same_model(before_model, after_model, moved), kind
    # A genuine change in the same safety-kind slot must still fail the structural proof.
    changed = copy.deepcopy(after)
    changed['policies'][0]['definition']['rule']['lines'][0]['starts_on'] = '2027-01-01'
    assert not same_model(before_model, canonical_model(changed), moved)


@pytest.mark.parametrize('kind', ['min_rest', 'forbidden_transition', 'minimum_cover'])
def test_catalogue_migration_api_confirms_advisory_move_without_changing_the_rota(h, kind):
    early = h.seed['type']
    late = h.configure('assignment_types', {'name': 'Synthetic late duty', 'starts_on': '2020-01-01',
                                          'definition': {'requirements': [{'minimum': 1, 'maximum': 3}]}})['record']['id']
    if kind == 'min_rest':
        bodies = [{'kind': kind, 'hours': hours} for hours in (11, 16)]
        scopes = [{'person_id': person} for person in h.seed['people'][:2]]
    elif kind == 'forbidden_transition':
        bodies = [{'kind': kind, 'from_type_ids': [late], 'to_type_ids': [to]} for to in (early, late)]
        scopes = [{'person_id': person} for person in h.seed['people'][:2]]
    else:
        bodies = [{'kind': kind, 'minimum': minimum} for minimum in (1, 2)]
        scopes = [{'assignment_type_id': typ} for typ in (early, late)]
    policies = [h.stored_advisory_safety(body, scope) for body, scope in zip(bodies, scopes)]
    w = h.workspace('Advisory catalogue proof')
    for start, end, typ in (('2026-10-04T20:00:00+03:00', '2026-10-05T08:00:00+03:00', late),
                            ('2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', early),
                            ('2026-10-05T13:00:00+03:00', '2026-10-05T17:00:00+03:00', late)):
        w = h.shift(w, start, end, people=[], typ=typ)['workspace']
    before = h.detail(w)
    preview = h.get('coordinator', '/api/rules/catalogue/migration?proof=true')
    # Safety kinds are always mandatory. Keep the sources until a mandatory version is approved.
    assert preview['moving'] == 0
    assert {row['rule_id'] for row in preview['report']} == {row['id'] for row in policies}
    assert all(SAFETY_REVIEW_REASON in row['reason'] for row in preview['report'])
    assert h.detail(w) == before


def pair_preference_model(kind, **options):
    model = deficit_model(**options)
    for variable in model.proto.variables:
        if variable.name == 'preference_deficit':
            variable.name = kind
    return model


@pytest.mark.parametrize('kind', ['rest_preference', 'transition_preference'])
def test_advisory_pair_leaves_reorder_without_losing_their_whole_constraint(proof_of_model, kind):
    before = proof_of_model(pair_preference_model(kind))
    assert same_model(before, proof_of_model(pair_preference_model(kind, reverse=True)), {})
    for change in ('trigger', 'coefficient', 'bound', 'domain', 'multiplicity'):
        assert not same_model(before, proof_of_model(pair_preference_model(kind, change=change)), {}), change


@pytest.mark.parametrize('kind', ['rest_preference', 'transition_preference'])
@pytest.mark.parametrize('attachment', ['linear', 'enforcement', 'negative_enforcement', 'literal', 'objective'])
def test_advisory_pair_extra_attachments_keep_distinct_identity(proof_of_model, kind, attachment):
    before = proof_of_model(pair_preference_model(kind, attachment=attachment))
    assert same_model(before, proof_of_model(pair_preference_model(kind, attachment=attachment)), {})
    assert not same_model(before, proof_of_model(pair_preference_model(kind, attachment=attachment, use_other=True)), {})


def exact_pair_model(kind, *, reverse=False, change=None, attachment=None, use_other=False):
    model = cp_model.CpModel()
    x, y, z, result = [model.new_bool_var(name) for name in ('input_x', 'input_y', 'input_z', 'result')]
    entries = [('first', x + y), ('second', x + z)]
    if change == 'multiplicity':
        entries.append(('third', x + y))
    flags = {}
    for key, expression in reversed(entries) if reverse else entries:
        flags[key] = flag = model.new_bool_var(kind)
        model.add(expression <= 1 + flag)
        constraint = model.add(expression >= (1 if key == 'first' and change == 'reverse_bound' else 2))
        constraint.only_enforce_if(~flag if key == 'first' and change == 'enforcement' else flag)
        if key == 'first' and change == 'provenance':
            constraint.with_name('different-rule-source')
    model.add(x == 1)
    model.add(y == 0)
    model.add(z == 1)
    model.add(result == 0)
    flag = flags['second' if use_other else 'first']
    model.minimize(0)
    if attachment == 'linear':
        model.add(result == flag)
    elif attachment == 'enforcement':
        model.add(result == 1).only_enforce_if(flag)
    elif attachment == 'negative_enforcement':
        model.add(result == 1).only_enforce_if(~flag)
    elif attachment == 'literal':
        model.add_bool_or([flag, result])
    elif attachment == 'objective':
        model.minimize(3 * flag)
    return model


@pytest.mark.parametrize('kind', ['rest_preference', 'transition_preference'])
def test_exact_advisory_pair_truth_reorders_without_losing_constraints(proof_of_model, kind):
    before = proof_of_model(exact_pair_model(kind))
    after = proof_of_model(exact_pair_model(kind, reverse=True))
    assert same_model(before, after, {})
    assert len(before[1]) == len(after[1]) == 8
    assert len(before[2]) == len(after[2]) == 6
    for change in ('reverse_bound', 'enforcement', 'provenance', 'multiplicity'):
        assert not same_model(before, proof_of_model(exact_pair_model(kind, reverse=True, change=change)), {}), change


@pytest.mark.parametrize('kind', ['rest_preference', 'transition_preference'])
@pytest.mark.parametrize('attachment', ['linear', 'enforcement', 'negative_enforcement', 'literal', 'objective'])
def test_exact_advisory_pair_extra_attachments_keep_distinct_identity(proof_of_model, kind, attachment):
    before = proof_of_model(exact_pair_model(kind, attachment=attachment))
    assert same_model(before, proof_of_model(exact_pair_model(kind, attachment=attachment)), {})
    assert not same_model(before, proof_of_model(exact_pair_model(kind, attachment=attachment, use_other=True)), {})
