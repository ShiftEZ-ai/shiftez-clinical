from copy import deepcopy

import pytest

from packages.contracts.rule_catalogue import RULE_CATALOGUE_V2, entry_kinds
from packages.contracts.schemas import CatalogueRule, NEVER_OVERRIDDEN
from packages.domain.conditions import active_rules
from packages.domain.cp_sat import generate
from packages.domain.fill_priority import allocation_tier
from packages.domain.generation import allocation_settings
from packages.domain.patterns import day_of
from packages.domain.validation import evaluate, finding_observations
from scripts.golden_department import day, load_department, seed_department
from scripts.golden_scenarios import command_key
from tests.golden_checks import place
from tests.scheduler_departments import apply


COUNTS = {'micro': (20, 82), 'department': (160, 829), 'stress': (480, 2301)}
DIMENSIONS = {'person_ids', 'category_ids', 'capability_ids', 'programme_ids', 'rotation_type_ids',
              'cohort_ids', 'tags', 'service_ids', 'location_ids', 'assignment_type_ids', 'weekdays', 'time'}
WITNESS = [
    ('clinic-0-early', 0, ['p000', 'p001', 'p002', 'p003']),
    ('clinic-0-teaching', 1, ['p001', 'p002', 'p005', 'p006']),
    ('clinic-0-teaching', 2, ['p008']),
    ('daily-night', 0, ['p010']),
    ('daily-night', 1, ['p010']),
    ('daily-night', 3, ['p004']),
    ('clinic-0-early', 2, ['p010']),
    ('clinic-0-early', 3, ['p010']),
    ('daily-response-am', 5, ['p011']),
    ('daily-response-am', 7, ['p011']),
    ('daily-response-am', 12, ['p011']),
    ('daily-response-pm', 5, ['p012']),
    ('clinic-0-late', 0, ['p013']),
    ('clinic-0-early', 1, ['p013']),
    ('clinic-0-early', 4, ['p014']),
    ('clinic-0-mid', 4, ['p014']),
    ('language-cover', 9, ['p014']),
    *[('clinic-0-mid', d, ['p005', 'p007']) for d in (0, 1, 2, 3)],
    ('clinic-0-late', 3, ['p007']),
]


def proposals(spec):
    return {row['key']: row['payload'] for row in spec['commands'] if row['kind'] == 'rule.propose'}


def test_department_pack_covers_catalogue_and_dimensions():
    catalogue = {entry['id']: entry for entry in RULE_CATALOGUE_V2}
    for size, (people, sessions) in COUNTS.items():
        spec = load_department(size)
        assert load_department(size) == spec
        rules = proposals(spec)
        assert rules.keys() == spec['expect']['bindings'].keys() == catalogue.keys()
        assert (len(spec['people']), spec['session_count']) == (people, sessions)
        keys = [row['key'] for row in spec['commands']]
        assert len(keys) == len(set(keys))
        dimensions, line_ids, kinds = set(), set(), set()
        for key, body in rules.items():
            # Command references are resolved by the public seed path. Use the same UUID shape for contract validation.
            def resolve(value):
                if isinstance(value, str) and value.startswith('$'):
                    return command_key(spec, value[1:])
                if isinstance(value, dict):
                    return {key: resolve(part) for key, part in value.items()}
                if isinstance(value, list):
                    return list(map(resolve, value))
                return value

            rule = CatalogueRule.model_validate({
                'kind': 'catalogue', 'id': key, 'catalogue_version': body['catalogue_version'],
                'lines': resolve(body['lines']),
            })
            for line in rule.lines:
                assert line.id not in line_ids
                line_ids.add(line.id)
                for select in (line.who, line.where):
                    if select is not None:
                        dimensions.update(select.fields())
                if len(line.weekdays) < 7:
                    dimensions.add('weekdays')
                if (line.start_time, line.end_time) != ('00:00', '24:00'):
                    dimensions.add('time')
            kinds.update(entry_kinds(catalogue[key]))
        assert DIMENSIONS <= dimensions
        assert NEVER_OVERRIDDEN <= kinds
        if size == 'department':
            assert 200 <= len(line_ids) <= 400
        if size == 'stress':
            assert 850 <= len(line_ids) <= 1100
            assert sum(row['kind'] == 'workspace.publish' for row in spec['commands']) == 12
            assert spec['historical_sessions'] == 353


def sessions_by_occurrence(data, ids, spec):
    series = {ids[key]: key for key in spec['series']}
    zone = data['organisation']['timezone']
    return {(series[row['series_id']], day_of(row['start'], zone).isoformat()): row for row in data['shifts']
            if row['series_id'] in series}


def check_outcomes(h, size, record_property):
    zone = h.get('coordinator', '/api/me')['organisation']['timezone']
    spec = load_department(size, zone)
    receipt = seed_department(spec, h.command, h.get, h.committed)
    ids = receipt['ids']
    data = h.snapshot(receipt['workspace'])
    sessions = sessions_by_occurrence(data, ids, spec)
    assert len(data['focus_ids']) == len(sessions) == spec['session_count']
    historical_sessions = 0
    for month in range(1, spec['history_months'] + 1):
        published = h.detail(receipt['records'][f'history-{month}-publish'])
        assert published['workspace']['publication_id']
        assert all(len(shift['participants']) == 1 for shift in published['shifts'])
        historical_sessions += len(published['shifts'])
    assert historical_sessions == spec['historical_sessions']
    assert set(ids[person] for person in spec['people']) <= set(data['people'])
    by_id = {ids[key]: key for key in spec['expect']['bindings']}
    data['generation_options'] = receipt['generation_options']
    effective = active_rules(data['policies'], context=data)
    assert {rule['id'] for rule in effective} >= by_id.keys()
    safety = {(rule['definition']['rule']['kind'], rule['definition']['severity'])
              for rule in effective if rule['id'] in by_id
              and rule['definition']['rule']['kind'] in NEVER_OVERRIDDEN}
    assert safety == {(kind, 'mandatory') for kind in NEVER_OVERRIDDEN} | {('minimum_cover', 'advisory')}

    broken = deepcopy(data)
    examples = sessions_by_occurrence(broken, ids, spec)
    for series, offset, people in WITNESS:
        place(broken, examples[series, day(offset)]['id'], [ids[person] for person in people])
    training = examples['clinic-0-teaching', day(1)]
    for p in training['participants']:
        if p['person_id'] in {ids['p005'], ids['p006']}:
            p.update(participation='trainee', supervisor_person_id=ids['p001'])
        else:
            p['participation'] = 'supervisor'
    validation = evaluate(broken)
    assert {row['required'] for row in validation['coverage'] if row['shift_id'] == training['id']} == {1}
    findings = list(finding_observations(validation))
    bindings = {(by_id[f['rule_id']], f['code']) for f in findings if f.get('rule_id') in by_id}
    missing = {(key, code) for key, codes in spec['expect']['bindings'].items() for code in codes} - bindings
    assert not missing, ('missing binding findings', sorted(missing), sorted(bindings))
    limits = {f['person_id'] for f in findings if f['code'] == 'MAX_DUTIES'}
    assert ids['p007'] in limits and ids['p005'] not in limits
    assert any(f['code'] == 'BALANCE_GOAL' and f['rule_id'] == ids['fair_share']
               and (f['detail'] or {}).get('planned', 0) > 0 for f in findings)

    # These two catalogue entries affect allocation objectives, so their witness is a priority delta.
    expansion = {**broken, 'policies': active_rules(broken['policies'], context=broken)}
    night = examples['daily-night', day(3)]
    pid = ids['p004']
    for key, measure in [('allocation_order', allocation_tier),
                         ('preferences', lambda data, shift, person: allocation_settings(data, shift, person)[0])]:
        without = {**expansion, 'policies': [p for p in expansion['policies'] if p['id'] != ids[key]]}
        assert measure(expansion, night, pid) > measure(without, night, pid), key

    result = generate(data, spec['expect']['search_seconds'], workers=1)
    record_property('golden_solver_status', result['solver_status'])
    record_property('golden_solver_timings', str(result.get('phase_timings')))
    assert result['solver_status'] in spec['expect']['statuses'], (size, result['status'], result.get('phase_timings'))
    assert result['status'] == 'candidate_feasible', result['status']
    after = apply(data, result)
    arrival = day(spec['people']['p015']['first_day'])
    departure = day(spec['people']['p016']['last_day'])
    for (_, duty_day), shift in sessions_by_occurrence(after, ids, spec).items():
        assigned = {p['person_id'] for p in shift['participants']}
        assert duty_day >= arrival or ids['p015'] not in assigned
        assert duty_day < departure or ids['p016'] not in assigned
    checked = evaluate(after)
    assert checked['error_count'] == 0, [(f['code'], f.get('rule_id')) for f in checked['findings'] if f['severity'] == 'error']
    shortages = {f['shift_id']: f for f in finding_observations(checked)
                 if f['code'] == 'SCOPED_COVERAGE_GAP' and f['rule_id'] == ids['people_per_session']}
    expected = {sessions[key, day(value['day'])]['id']: value['count'] for key, value in spec['expect']['shortages'].items()}
    assert shortages.keys() == expected.keys(), [(key, d, shortages[row['id']]['detail'])
        for (key, d), row in sessions.items() if row['id'] in shortages]
    for sid, count in expected.items():
        finding = shortages[sid]
        assert finding['severity'] == 'warning'
        assert finding['detail']['required'] - finding['detail']['available'] == count

    if size == 'micro':
        replay = seed_department(spec, h.command, h.get, h.committed)
        assert replay['ids'] == ids
        assert replay['input_sha256'] == receipt['input_sha256']
        assert replay['workspace']['revision'] == receipt['workspace']['revision']


def test_micro_department(cp_sat_engine, record_property):
    check_outcomes(cp_sat_engine, 'micro', record_property)


@pytest.mark.cpu_budget
@pytest.mark.parametrize('size', ['department', 'stress'])
def test_scale_department(pg, size, record_property):
    check_outcomes(pg, size, record_property)
