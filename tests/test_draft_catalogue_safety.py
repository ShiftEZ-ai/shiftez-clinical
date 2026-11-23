"""Drafts enforce current catalogue safety together with captured limits."""
from copy import deepcopy

import pytest

from packages.domain import cp_sat, generation
from packages.domain.validation import effective_type_definition
from tests.test_generation_apply_scale import completed_proposal


@pytest.fixture(params=['h', pytest.param('pg', marks=pytest.mark.postgres)], ids=['sqlite', 'postgresql'])
def store(request):
    return request.getfixturevalue(request.param)


def catalogue(h, kind, ident):
    return next(row for row in h.get('admin', '/api/catalogues')[kind] if row['id'] == ident)


def staffing(h, minimum, maximum, *, target=None, supervision=None):
    old = catalogue(h, 'assignment_types', h.seed['type'])
    definition = deepcopy(old['definition'])
    definition['requirements'][0].update(minimum=minimum, maximum=maximum, target=target)
    definition['supervision'] = supervision
    return h.configure('assignment_types', {key: old[key] for key in ('name', 'description', 'starts_on')}
                       | {'definition': definition}, old['id'], old['revision'])['record']


def finding(report, code, **detail):
    return any(row['code'] == code and row['severity'] == 'error'
               and all(row['detail'].get(key) == value for key, value in detail.items())
               for row in report['findings'])


@pytest.mark.parametrize('captured, live', [(1, 2), (2, 1)], ids=['tightened', 'loosened'])
def test_minimum_cover_checks_and_schedules_to_the_stricter_value(store, captured, live):
    h = store
    supervision_scope = {'trainee_category_ids': [h.seed['trainee_category']],
                         'capability_id': h.seed['supervisor_capability']}
    staffing(h, captured, 3, target=2, supervision={**supervision_scope,
        'trainees_per_supervisor': captured, 'require_pairing': captured == 1})
    draft = h.shift(h.workspace())['workspace']
    frozen = h.snapshot(draft)['shifts'][0]['type_snapshot']
    staffing(h, live, 3, target=2, supervision={**supervision_scope,
        'trainees_per_supervisor': live, 'require_pairing': live == 1})
    checked = h.lifecycle(draft, 'validate')['workspace']
    assert finding(checked['validation'], 'COVERAGE_GAP', required=2, available=1)
    assert 'TYPE_VERSION_CHANGED' not in {f['code'] for f in checked['validation']['findings']}

    snapshot = h.snapshot(checked)
    assert snapshot['shifts'][0]['type_snapshot'] == frozen
    assert effective_type_definition(snapshot, snapshot['shifts'][0])['supervision'] == {
        **supervision_scope, 'trainees_per_supervisor': 1, 'require_pairing': True}
    for solve in (cp_sat.generate, generation.generate):
        result = solve(snapshot, time_budget_seconds=10, deterministic=True)
        assert result['status'] == 'candidate_feasible' and result['validation']['valid'], result
        assert len(result['additions']) == 1 and not result['removals']


@pytest.mark.parametrize('captured, live', [(3, 1), (1, 3)], ids=['tightened', 'loosened'])
def test_apply_keeps_proposal_that_exceeds_the_stricter_maximum(store, captured, live):
    h = store
    staffing(h, 1, captured)
    draft = h.shift(h.workspace())['workspace']
    shift = h.snapshot(draft)['shifts'][0]
    job = completed_proposal(h, draft, [{'shift_id': shift['id'], 'participant': {
        'person_id': h.seed['people'][1], 'start': shift['start'], 'end': shift['end']}}])
    staffing(h, 1, live)
    result = h.command('coordinator', 'generation.apply', {
        'workspace_id': draft['id'], 'job_id': job['id'], 'reason': 'Review proposed assignment'},
        revision=draft['revision'])
    assert result['proposal_retained'] and not result['reconciliation']['addition_indexes']
    conflict, = result['reconciliation']['conflicting_placements']
    assert conflict['code'] == 'MANDATORY_CONFLICT'
    assert finding(conflict, 'MAXIMUM_PARTICIPANTS', maximum=1, scheduled=2)
    assert h.snapshot(result['workspace'])['shifts'][0]['participants'] == shift['participants']


def change_place(h, place, **updates):
    fields = ('name', 'starts_on', 'location_type_id', 'capacity', 'parent_id')
    return h.configure('locations', {key: place[key] for key in fields} | updates,
                       place['id'], place['revision'])['record']


@pytest.mark.parametrize('late_change', [True, False], ids=['after-submit', 'retained-ceiling'])
def test_place_capacity_binds_existing_draft(store, late_change):
    h = store
    if late_change:
        # The shared parent is not a directly booked place.
        place = h.configure('locations', {'name': 'Shared resource', 'starts_on': '2020-01-01',
            'location_type_id': h.seed['location_type'], 'capacity': 2})['record']
        for ident in h.seed['locations']:
            change_place(h, catalogue(h, 'locations', ident), parent_id=place['id'])
    else:
        place = change_place(h, catalogue(h, 'locations', h.seed['locations'][0]), capacity=1)
    draft = h.shift(h.workspace())['workspace']
    if not late_change:
        change_place(h, place, capacity=2)
    draft = h.shift(draft, location=int(late_change), people=h.seed['people'][1:2],
                    status=200 if late_change else (200, 422))
    if late_change:
        submitted = h.lifecycle(draft['workspace'], 'submit')['workspace']
        change_place(h, place, capacity=1)
        refusal = h.lifecycle(submitted, 'approve', 'approver', status=409)['error']
        assert refusal['code'] == 'STALE_INPUTS'
        reopened = h.lifecycle(submitted, 'reopen')['workspace']
        checked = h.lifecycle(reopened, 'validate')['workspace']
        assert finding(checked['validation'], 'RESOURCE_CAPACITY', capacity=1, resource_id=place['id'])
        for solve in (cp_sat.generate, generation.generate):
            result = solve(h.snapshot(checked), time_budget_seconds=10, deterministic=True)
            assert result['status'] == 'invalid_inputs'
    else:
        checked = (h.lifecycle(draft['workspace'], 'validate')['workspace']['validation']
                   if 'workspace' in draft else draft['error']['details'])
        assert finding(checked, 'RESOURCE_CAPACITY', capacity=1, resource_id=place['id'])
