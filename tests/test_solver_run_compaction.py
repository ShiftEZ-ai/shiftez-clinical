"""Parallel regular/overtime alternatives retain the canonical hard rules."""
import copy
from itertools import product

import pytest
from ortools.sat.python import cp_model

from packages.domain.cp_sat import generate, runs
from packages.domain.generation import citation_finding_key
from packages.domain.validation import evaluate, finding_observations
from tests.scheduler_departments import department


@pytest.mark.parametrize('domain', [[0], [1], [0, 1], [-1, 1], [1, 3]])
def test_run_compaction_keeps_only_fixed_present_rows_separate(domain):
    model = cp_model.CpModel()
    choices = [model.new_int_var_from_domain(cp_model.Domain.from_values(domain), str(i)) for i in range(2)]
    rows = [({'id': str(i)}, {}, 0, 1, choice) for i, choice in enumerate(choices)]
    compacted = runs(rows, lambda row: 'same')
    assert len(compacted) == (2 if domain == [1] else 1)
    assert [choice.domain.flattened_intervals() for choice in choices] == [
        cp_model.Domain.from_values(domain).flattened_intervals()] * 2
    assert [row[0]['id'] for row in rows] == ['0', '1']


@pytest.mark.parametrize('kind', ['min_rest', 'travel_buffer', 'forbidden_transition'])
def test_interleaved_overtime_clinics_match_fixed_schedule_validation(h, kind):
    person = h.seed['people'][0]
    typ = h.configure('assignment_types', {'name': 'Optional parallel clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 0, 'maximum': 1}]}})['record']['id']
    rules = {
        'min_rest': {'kind': 'min_rest', 'hours': 4},
        'travel_buffer': {'kind': 'travel_buffer', 'from_location_id': h.seed['locations'][0],
                         'to_location_id': h.seed['locations'][1], 'minutes': 90},
        'forbidden_transition': {'kind': 'forbidden_transition', 'from_type_ids': [typ],
                                 'to_type_ids': [typ], 'overtime_only': True},
    }
    h.policy(rules[kind])
    h.policy({'kind': 'allocation_overtime'}, severity='advisory')
    workspace = h.workspace()
    slots = [('2026-10-04', '08:00', '10:00', 0), ('2026-10-04', '11:00', '12:00', 1),
             ('2026-10-05', '16:00', '17:00', 1)]
    for day, start, end, location in slots:
        for _ in range(2):
            workspace = h.shift(workspace, typ=typ, start=f'{day}T{start}:00+03:00',
                end=f'{day}T{end}:00+03:00', location=location, people=[])['workspace']
    data = h.snapshot(workspace)
    data['people'] = {person: data['people'][person]}
    captured = []
    result = generate(data, 1, deterministic=True, feasibility_only=True, _on_model=captured.append)
    assert result['status'] == 'model_ready', result
    model, = captured
    choices = {s['id']: next(i for i, v in enumerate(model.proto.variables) if v.name == s['id'] + ':' + person)
               for s in data['shifts']}
    # Test every selection with both regular and overtime classifications. The
    # independent validator supplies the expected answer, including overlaps.
    for overtime, selected in product((False, True), product((False, True), repeat=len(data['shifts']))):
        pinned = model.clone()
        witness = copy.deepcopy(data)
        for shift, chosen in zip(witness['shifts'], selected):
            pinned.add(pinned.get_bool_var_from_proto_index(choices[shift['id']]) == int(chosen))
            if chosen:
                shift['participants'] = [{'id': shift['id'] + ':witness', 'shift_id': shift['id'],
                    'person_id': person, 'start': shift['start'], 'end': shift['end'], 'participation': 'staff',
                    'overtime': overtime, 'revision': 1}]
        for i, variable in enumerate(pinned.proto.variables):
            if variable.name == ('regular' if overtime else 'overtime'):
                pinned.add(pinned.get_bool_var_from_proto_index(i) == 0)
        solver = cp_model.CpSolver()
        solver.parameters.num_workers = 1
        solver.parameters.max_time_in_seconds = 1
        status = solver.solve(pinned)
        expected = evaluate(witness)['valid']
        assert status in (cp_model.OPTIMAL, cp_model.INFEASIBLE)
        assert (status == cp_model.OPTIMAL) is expected, (kind, overtime, selected)


def test_parallel_overtime_clinics_keep_linear_model_growth(h):
    data, _ = department(h, 31, people=40, days=7, clinics=4)
    data['policies'].append(h.policy({'kind': 'allocation_overtime'}, severity='advisory'))
    sizes = []
    for clinics in (1, 4):
        sample = copy.deepcopy(data)
        counts, shifts = {}, []
        for shift in sample['shifts']:
            key = shift['start'], shift['assignment_type_id']
            counts[key] = counts.get(key, 0) + 1
            if counts[key] <= clinics:
                shifts.append(shift)
        sample['shifts'] = shifts
        sample['focus_ids'] = [s['id'] for s in shifts]
        captured = []
        result = generate(sample, 1, deterministic=True, feasibility_only=True, _on_model=captured.append)
        assert result['status'] == 'model_ready', result
        model, = captured
        sizes.append({'places': len(shifts) * len(sample['people']),
                      'variables': len(model.proto.variables), 'constraints': len(model.proto.constraints)})
    growth = sizes[1]['places'] / sizes[0]['places']
    assert sizes[1]['variables'] < 1.3 * growth * sizes[0]['variables'], sizes
    assert sizes[1]['constraints'] < 1.3 * growth * sizes[0]['constraints'], sizes


def _fixed_overlap(h, kind='min_rest', severity='advisory'):
    person = h.seed['people'][0]
    optional = h.configure('assignment_types', {'name': 'Optional interleaved alternative', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 0, 'maximum': 1}]}})['record']['id']
    rule = {'kind': 'min_rest', 'hours': 1}
    if kind == 'travel_buffer':
        rule = {'kind': kind, 'minutes': 90, 'from_location_id': h.seed['locations'][0],
                'to_location_id': h.seed['locations'][1]}
    elif kind == 'forbidden_transition':
        rule = {'kind': kind, 'from_type_ids': [optional], 'to_type_ids': [h.seed['type']], 'overtime_only': True}
    if severity == 'advisory' and kind != 'travel_buffer':
        pair_policy = h.stored_advisory_safety(rule)
    else:
        pair_policy = h.policy(rule, severity=severity)
    overtime_policy = h.policy({'kind': 'allocation_overtime'}, severity='advisory')
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T10:00:00+03:00'
    fixed = {'person_id': person, 'start': start, 'end': end, 'fixed': True,
             'overtime': kind == 'forbidden_transition'}
    workspace = h.workspace()
    workspace = h.shift(workspace, start=start, end=end, typ=optional, people=[fixed])['workspace']
    workspace = h.shift(workspace, start=start, end=end, typ=optional, people=[],
                        location=1)['workspace']
    workspace = h.shift(workspace, start=start, end=end, typ=optional, people=[fixed])['workspace']
    later = '2026-10-05T08:00:00+03:00' if kind == 'forbidden_transition' else '2026-10-04T10:30:00+03:00'
    last = '2026-10-05T12:00:00+03:00' if kind == 'forbidden_transition' else '2026-10-04T12:00:00+03:00'
    workspace = h.shift(workspace, start=later, end=last, people=[], location=1)['workspace']
    data = h.snapshot(workspace)
    # The database may return rows in any order; make the relevant A/B/C order explicit.
    rows = sorted(data['shifts'], key=lambda s: (s['start'], len(s['participants']) == 0, s['id']))
    fixed_rows = [s for s in rows if s['participants']]
    alternative = next(s for s in rows if not s['participants'] and s['assignment_type_id'] == optional)
    target = next(s for s in rows if s['assignment_type_id'] == h.seed['type'])
    data['shifts'] = [fixed_rows[0], alternative, fixed_rows[1], target]
    for n, shift in enumerate(data['shifts'], 1):
        shift['id'] = f'00000000-0000-4000-8000-{n:012d}'
        for p in shift['participants']:
            p['shift_id'] = shift['id']
    data['focus_ids'] = [s['id'] for s in data['shifts']]
    data['people'] = {person: data['people'][person]}
    data['policies'] = [pair_policy, overtime_policy]
    data['generation_options'] = {'mode': 'rebalance', 'shortlist_width': 0, 'balance_measure': 'duties'}
    return data, target['id']


@pytest.mark.parametrize('kind', ['min_rest', 'travel_buffer', 'forbidden_transition'])
@pytest.mark.parametrize('severity', ['advisory', 'mandatory'])
def test_interleaved_fixed_focus_duties_keep_pair_rule_severity(h, kind, severity):
    data, target = _fixed_overlap(h, kind, severity)
    frozen = copy.deepcopy(data)
    before = [f for f in finding_observations(evaluate(data)) if f['severity'] == 'error']
    assert {'TIME_OVERLAP', 'COVERAGE_GAP'} <= {f['code'] for f in before}
    retained_errors = {citation_finding_key(f) for f in before if f['code'] != 'COVERAGE_GAP'}

    result = generate(data, 5, workers=1)

    assert data == frozen
    assert result['removals'] == []
    assert not result.get('optimality_proven')
    # Safety kinds are always mandatory. Travel buffer remains advisory when configured that way.
    if severity == 'mandatory' or kind != 'travel_buffer':
        assert result['status'] == 'requirements_conflict' and not result['additions'], result
        return
    assert result['status'] == 'candidate_with_gaps', result
    assert {a['shift_id'] for a in result['additions']} == {target}, result
    after_errors = {citation_finding_key(f) for f in finding_observations(result['validation']) if f['severity'] == 'error'}
    assert after_errors == retained_errors
    assert any(f['severity'] == 'warning' and f['code'] == {
        'min_rest': 'MIN_REST', 'travel_buffer': 'TRAVEL_BUFFER',
        'forbidden_transition': 'FORBIDDEN_TRANSITION',
    }[kind] for f in finding_observations(result['validation']))
