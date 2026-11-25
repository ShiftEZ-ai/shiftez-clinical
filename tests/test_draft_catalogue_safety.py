"""Drafts enforce current catalogue safety together with captured limits."""
from copy import deepcopy

import pytest

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import GenerationOptions
from packages.domain import cp_sat, generation, solver_resume, validation
from packages.domain.solver_control import SolverControl
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


@pytest.mark.parametrize('replacement', [{'assignment_type_id': 'replacement'}, {'type_revision': 2}],
                         ids=['duty-type', 'revision'])
def test_prepared_draft_staffing_only_applies_to_the_session_version(replacement):
    captured = {'requirements': [{'role': 'clinician', 'minimum': 1}]}
    current = {'requirements': [{'role': 'clinician', 'minimum': 2}]}
    shift = {'id': 'session', 'workspace_id': 'draft', 'assignment_type_id': 'original', 'type_revision': 1,
             'type_snapshot': {'definition': captured}, 'start': '2026-10-04T08:00:00+00:00',
             'end': '2026-10-04T12:00:00+00:00'}
    data = {'workspace': {'id': 'draft'}, 'organisation': {'timezone': 'UTC'},
            'types': {'original': {'revision': 2, 'definition': current}}}
    data['_draft_type_safety'] = validation.prepare_draft_type_safety(data, [shift])

    assert validation.effective_type_definition(data, shift)['requirements'] == current['requirements']
    replaced = {**shift, **replacement}
    assert validation.effective_type_definition(data, replaced)['requirements'] == captured['requirements']


@pytest.mark.parametrize('captured, live', [(1, 2), (2, 1)], ids=['tightened', 'loosened'])
def test_minimum_cover_checks_and_schedules_to_the_stricter_value(store, captured, live, monkeypatch):
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

    def merge_during_search(*args, **kwargs):
        pytest.fail('Draft staffing should be resolved before either scheduler reads it')

    monkeypatch.setattr(validation, 'effective_issued_definition', merge_during_search)
    assert validation.effective_type_definition(snapshot, snapshot['shifts'][0])['supervision'] == {
        **supervision_scope, 'trainees_per_supervisor': 1, 'require_pairing': True}
    for solve in (cp_sat.generate, generation.generate):
        result = solve(snapshot, time_budget_seconds=10, deterministic=True)
        assert result['status'] == 'candidate_feasible' and result['validation']['valid'], result
        assert len(result['additions']) == 1 and not result['removals']


def test_resumed_cp_sat_validation_uses_stricter_draft_staffing(h):
    staffing(h, 1, 3)
    draft = h.shift(h.workspace())['workspace']
    staffing(h, 2, 3)
    snapshot = h.snapshot(draft)
    options = GenerationOptions.model_validate(snapshot['generation_options'])
    prepared = cp_sat._prepare(snapshot, 5, 1, SolverControl(), options, False, False)
    assert 'model' in prepared, prepared
    key = solver_resume.resume_key(snapshot, options, False, False)
    checkpoint = solver_resume.compress_state(solver_resume.prepared_state(key, prepared))
    restored = solver_resume.restore(solver_resume.decompress_state(checkpoint), key)

    checked = validation.evaluate(restored['data'])
    assert finding(checked, 'COVERAGE_GAP', required=2, available=1)
    assert 'TYPE_VERSION_CHANGED' not in {row['code'] for row in checked['findings']}


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


def test_place_capacity_from_parent_added_to_an_existing_draft(store):
    h = store
    draft = h.shift(h.workspace())['workspace']
    draft = h.shift(draft, location=1, people=h.seed['people'][1:2])['workspace']
    parent = h.configure('locations', {'name': 'Added common place', 'starts_on': '2020-01-01',
        'location_type_id': h.seed['location_type'], 'capacity': 1})['record']
    for ident in h.seed['locations']:
        change_place(h, catalogue(h, 'locations', ident), parent_id=parent['id'])

    checked = h.lifecycle(draft, 'validate')['workspace']
    assert finding(checked['validation'], 'RESOURCE_CAPACITY', capacity=1, resource_id=parent['id'])
    snapshot = h.snapshot(checked)
    assert snapshot['locations'][parent['id']]['capacity'] == 1
    for solve in (cp_sat.generate, generation.generate):
        result = solve(snapshot, time_budget_seconds=10, deterministic=True)
        assert result['status'] == 'invalid_inputs'


def capacity_parent(h):
    return h.configure('locations', {'name': 'Shared place', 'starts_on': '2020-01-01',
        'location_type_id': h.seed['location_type'], 'capacity': 1})['record']


def issue_overlapping_duty(h, location_id):
    workspace = h.command('coordinator', 'workspace.create', {
        'name': 'Issued rota fixture', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
        'purpose': 'integrated',
    })['workspace']
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    workspace = h.command('coordinator', 'shift.create', {
        'workspace_id': workspace['id'], 'start': start, 'end': end,
        'assignment_type_id': h.seed['type'], 'location_id': location_id,
        'service_ids': [h.seed['services'][0]],
        'participants': [{'person_id': h.seed['people'][1], 'start': start, 'end': end}],
    }, revision=workspace['revision'])['workspace']
    return h.publish(workspace)['publication_id']


def persisted_check(h, draft):
    job = h.command('coordinator', 'job.create', {'workspace_id': draft['id'], 'kind': 'feasibility'},
                    revision=draft['revision'])['job']
    with transaction(h.app.state.engine) as db:
        return db.get(m.Job, job['id']).input_snapshot['baseline']


def parent_findings(report, parent_id):
    return [row for row in report['findings']
            if row['code'] == 'RESOURCE_CAPACITY' and row['detail']['resource_id'] == parent_id]


def shared_capacity_findings(data, own, issued, parent_id):
    start, end = validation.aware(own['start']), validation.aware(own['end'])
    consuming = validation.resource_sessions(data, data['shifts'], parent_id, start, end)
    assert {row['id'] for row in consuming} == {own['id'], issued['id']}
    assert len(consuming) == 2
    report = validation.evaluate(data)
    assert finding(report, 'RESOURCE_CAPACITY',
                   scheduled_sessions=2, published_sessions=1, capacity=1, resource_id=parent_id)
    return parent_findings(report, parent_id)


def test_shared_capacity_counts_issued_duty_at_place_added_after_capture(store):
    from packages.domain.validation import capacity_ancestor_ids

    h = store
    parent = capacity_parent(h)
    own_place = change_place(h, catalogue(h, 'locations', h.seed['locations'][0]), parent_id=parent['id'])
    draft = h.shift(h.workspace())['workspace']
    capture = h.snapshot(draft)
    late_place = h.configure('locations', {'name': 'Added child place', 'starts_on': '2020-01-01',
        'location_type_id': h.seed['location_type'], 'parent_id': parent['id'], 'capacity': 2})['record']
    assert late_place['id'] not in capture['locations']
    issued_id = issue_overlapping_duty(h, late_place['id'])
    issued_evidence = h.get('coordinator', '/api/publications/' + issued_id)['snapshot']
    issued, = issued_evidence['shifts']
    own, = capture['shifts']
    change_place(h, own_place, parent_id=None)

    checked = h.lifecycle(draft, 'validate')['workspace']
    live = h.snapshot(checked)
    saved = persisted_check(h, checked)
    for data in (live, saved):
        assert data['baseline']['snapshot_hash'] == capture['baseline']['snapshot_hash']
        assert late_place['id'] not in data['locations']
        sources = data['input_manifest']['sources']
        assert all(sources['shifts:' + duty['id']]['facts']['location_id'] == duty['location_id']
                   for duty in (own, issued))
        assert sources['capacity_locations:' + late_place['id']]['facts'] == {
            'id': late_place['id'], 'department_id': late_place['department_id'], 'capacity': 2,
            'capacity_ancestors': [parent['id']],
        }
        assert tuple(capacity_ancestor_ids(data, own_place['id'])) == (own_place['id'], parent['id'])
        assert tuple(capacity_ancestor_ids(data, late_place['id'])) == (late_place['id'], parent['id'])
        assert not validation.session_matches(data, {'location_id': parent['id']}, issued)
        assert validation.session_matches(data, {'service_id': issued['service_ids'][0]}, issued)
        assert shared_capacity_findings(data, own, issued, parent['id']) == parent_findings(checked['validation'], parent['id'])
    for solve in (cp_sat.generate, generation.generate):
        assert solve(saved, time_budget_seconds=10, deterministic=True)['status'] == 'invalid_inputs'
    assert h.get('coordinator', '/api/publications/' + issued_id)['snapshot'] == issued_evidence


def test_shared_capacity_refuses_draft_duty_reparented_after_capture(store):
    h = store
    parent = capacity_parent(h)
    change_place(h, catalogue(h, 'locations', h.seed['locations'][1]), parent_id=parent['id'])
    draft = h.shift(h.workspace())['workspace']
    capture = h.snapshot(draft)
    own, = capture['shifts']
    assert capture['locations'][own['location_id']]['parent_id'] is None
    issued_id = issue_overlapping_duty(h, h.seed['locations'][1])
    issued_evidence = h.get('coordinator', '/api/publications/' + issued_id)['snapshot']
    issued, = issued_evidence['shifts']
    change_place(h, catalogue(h, 'locations', own['location_id']), parent_id=parent['id'])

    checked = h.lifecycle(draft, 'validate')['workspace']
    assert finding(checked['validation'], 'RESOURCE_CAPACITY',
                   scheduled_sessions=2, published_sessions=1, capacity=1, resource_id=parent['id'])
    refusal = h.lifecycle(checked, 'submit', status=422)['error']
    assert refusal['code'] == 'VALIDATION_FAILED'
    assert parent_findings(refusal['details'], parent['id']) == parent_findings(checked['validation'], parent['id'])
    saved = persisted_check(h, checked)
    assert saved['baseline']['snapshot_hash'] == capture['baseline']['snapshot_hash']
    assert shared_capacity_findings(saved, own, issued, parent['id']) == parent_findings(checked['validation'], parent['id'])
    assert h.get('coordinator', '/api/publications/' + issued_id)['snapshot'] == issued_evidence
