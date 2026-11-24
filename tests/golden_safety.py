"""Safety witnesses over the API-seeded rota and the production solver and validator."""
from copy import deepcopy

from packages.contracts.rule_catalogue import CATALOGUE_MAPPING
from packages.contracts.schemas import PolicyDefinition
from packages.domain.rule_catalogue_migration import lines_of
from packages.domain.time import aware
from packages.domain.validation import evaluate, finding_observations
from tests.golden_checks import disabled, feasible, place, shift, solve
from tests.scheduler_departments import _id


def resolve(value, ids):
    if isinstance(value, str) and value.startswith('$'):
        return ids[value[1:]]
    if isinstance(value, list):
        return [resolve(item, ids) for item in value]
    if isinstance(value, dict):
        return {key: resolve(item, ids) for key, item in value.items()}
    return value


def policy(data, rid):
    return next(row for row in data['policies'] if row['id'] == rid)


def witness(data, ids, case):
    trial = deepcopy(data)
    for key, participants in case.get('assignments', {}).items():
        place(trial, ids[key], resolve(participants, ids))
    return trial


def safety_errors(data, rid):
    return [row for row in finding_observations(evaluate(data))
            if row.get('rule_id') == rid and row['severity'] == 'error']


def matching_errors(data, rid, expected):
    errors = safety_errors(data, rid)
    clocks = {'start', 'end'} & expected.keys()
    matches = [row for row in errors if all(row[key] == value for key, value in expected.items()
                                          if key not in clocks and key != 'detail')
               and all(aware(row[key]) == aware(expected[key]) for key in clocks)
               and all(row['detail'].get(key) == value for key, value in expected.get('detail', {}).items())]
    assert matches, (expected, errors)
    return matches


def with_narrower_line(data, rid, narrower):
    trial = deepcopy(data)
    broad = policy(trial, rid)
    entry, broad_lines = lines_of(broad)
    narrow = deepcopy(broad)
    narrow['id'] = _id('golden-narrow-rule', rid)
    narrow['definition']['scope'].update(narrower['scope'])
    narrow['definition']['rule'].update(narrower['rule'])
    _, narrow_lines = lines_of(narrow)
    broad['kind'] = 'catalogue'
    broad['definition'] = PolicyDefinition.model_validate({'rule': {
        'kind': 'catalogue', 'id': CATALOGUE_MAPPING.get(entry, entry), 'catalogue_version': 2,
        'lines': broad_lines + narrow_lines,
    }}).model_dump(mode='json')
    return trial


def check_safety(data, ids, spec):
    expected = spec['expect']
    [rule_key] = expected['binding_rules']
    rid = ids[rule_key]
    safety = expected['safety']
    base = policy(data, rid)
    assert base['status'] == 'active' and base['definition']['severity'] == 'mandatory'

    boundary = witness(data, ids, expected['boundary'])
    assert evaluate(boundary)['valid'], evaluate(boundary)
    broken = witness(data, ids, expected['validator'])
    error = resolve(expected['validator']['finding'], ids)
    matching_errors(broken, rid, error)
    for check in safety['contrasts']:
        trial = witness(boundary if check.get('from_boundary') else broken, ids, check)
        policy(trial, rid)['definition']['rule'].update(resolve(check.get('rule', {}), ids))
        if 'finding' in check:
            matching_errors(trial, rid, resolve(check['finding'], ids))
        else:
            assert evaluate(trial)['valid'], (check, evaluate(trial))

    # Old snapshots may still contain an advisory value for a mandatory kind.
    historical = deepcopy(data)
    policy(historical, rid)['definition']['severity'] = 'advisory'
    with_lines = with_narrower_line(data, rid, resolve(safety['narrower'], ids))
    matching_errors({**broken, 'policies': historical['policies']}, rid, error)
    matching_errors({**broken, 'policies': with_lines['policies']}, rid,
                    {**error, 'detail': {**error.get('detail', {}), 'line': 0}})

    bound = set()
    for seed in expected['seed_robustness']['seeds']:
        for source in (data, historical):
            candidate = feasible(source, solve(source, seed))
            for session_key, people in safety.get('required_in_candidate', {}).items():
                assert set(resolve(people, ids)) <= {
                    p['person_id'] for p in shift(candidate, ids[session_key])['participants']}
        feasible(with_lines, solve(with_lines, seed))
        off_data = disabled(data, rid)
        off = feasible(off_data, solve(off_data, seed))
        off['policies'] = data['policies']
        hits = matching_errors(off, rid, resolve(expected['counterfactual'], ids))
        bound.update((row['shift_id'], row['person_id'], row['start'], row['end']) for row in hits)
    return {'assertions': {'boundary', 'validator', 'counterfactual', 'seed_robustness', 'safety'},
            'bindings': {rule_key: len(bound)}}
