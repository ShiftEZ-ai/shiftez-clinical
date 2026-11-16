"""Extra cover is optional headcount, filled automatically only as permitted overtime."""
import copy

import pytest
from pydantic import ValidationError

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import job_outcome, job_result_metadata
from packages.contracts.schemas import TypeDefinition
from packages.domain.cp_sat import generate as cp_sat_generate
from packages.domain.generation import allocation_settings, generate as greedy_generate
from packages.domain.rota_summary import rota_summary
from tests.legacy_lead import historical_type
from packages.domain.validation import evaluate, finding_observations


@pytest.fixture(params=[greedy_generate, cp_sat_generate], ids=['greedy', 'cp_sat'])
def generate(request):
    return request.param


def extra_type(h, unfilled='open', **definition):
    value = {
        'name': 'Synthetic extra-cover duty', 'starts_on': '2020-01-01',
        'definition': {'extra_cover': True, 'extra_cover_unfilled': unfilled,
                       'requirements': [{'minimum': 1, 'maximum': 1}], **definition}}
    return historical_type(h, value) if definition.get('lead') else h.configure('assignment_types', value)['record']



def test_contract_defaults_and_supported_unfilled_choices():
    definition = TypeDefinition.model_validate({})
    assert definition.extra_cover is False
    assert definition.extra_cover_unfilled == 'open'
    for choice in ('open', 'gap'):
        assert TypeDefinition(extra_cover=True, extra_cover_unfilled=choice).extra_cover_unfilled == choice
    with pytest.raises(ValidationError):
        TypeDefinition(extra_cover_unfilled='ignore')


def test_duty_type_settings_round_trip_through_catalogue_and_session(h):
    duty_type = extra_type(h)
    changed = h.configure('assignment_types', {
        'name': duty_type['name'], 'starts_on': duty_type['starts_on'],
        'definition': {**duty_type['definition'], 'extra_cover_unfilled': 'gap'},
    }, target=duty_type['id'], revision=duty_type['revision'])['record']
    saved = next(t for t in h.get('admin', '/api/catalogues')['assignment_types']
                 if t['id'] == changed['id'])
    assert saved['definition']['extra_cover'] is True
    assert saved['definition']['extra_cover_unfilled'] == 'gap'
    workspace = h.shift(h.workspace(), typ=saved['id'], people=[])['workspace']
    snapshot = h.detail(workspace)['shifts'][0]['type_snapshot']['definition']
    assert snapshot['extra_cover'] is True and snapshot['extra_cover_unfilled'] == 'gap'


@pytest.mark.parametrize('unfilled', ['open', 'gap'])
def test_unfilled_extra_cover_uses_normal_validation_and_publication_lifecycle(h, unfilled):
    duty_type = extra_type(h, unfilled)
    result = h.shift(h.workspace(), typ=duty_type['id'], people=[])
    workspace, validation = result['workspace'], result['validation']
    code = 'OPEN_EXTRA_COVER' if unfilled == 'open' else 'COVERAGE_GAP'
    finding, = [f for f in validation['findings'] if f['code'] == code]
    assert finding['severity'] == ('warning' if unfilled == 'open' else 'error')
    assert finding['detail']['required'] == 1 and finding['detail']['available'] == 0
    assert validation['valid'] is (unfilled == 'open')
    summary = h.get('coordinator', f"/api/workspaces/{workspace['id']}/summary")
    assert summary['places_open'] == 1
    assert summary['extra_cover_places_open'] == int(unfilled == 'open')
    assert summary['sessions_with_open_extra_cover'] == int(unfilled == 'open')
    assert summary['sessions_with_gaps'] == int(unfilled == 'gap')
    assert summary['sessions'][0]['extra_cover_places_open'] == int(unfilled == 'open')
    listed = next(w for w in h.get('coordinator', '/api/workspaces')['workspaces']
                  if w['id'] == workspace['id'])
    assert listed['health']['summary']['extra_cover_places_open'] == int(unfilled == 'open')
    if unfilled == 'gap':
        assert h.lifecycle(workspace, 'submit', status=422)['error']['code'] == 'VALIDATION_FAILED'
    else:
        published = h.publish(workspace)
        publication = h.get('coordinator', '/api/publications/' + published['publication_id'])
        assert publication['snapshot']['shifts'][0]['participants'] == []
        assert publication['snapshot']['shifts'][0]['type_snapshot']['definition']['extra_cover'] is True


@pytest.mark.parametrize('overtime', [False, True])
def test_manual_regular_placement_stays_possible_with_a_plain_advisory(h, overtime):
    duty_type = extra_type(h)
    person = h.seed['people'][0]
    h.policy({'kind': 'allocation_overtime'}, {'person_id': person}, severity='advisory')
    result = h.shift(h.workspace(), typ=duty_type['id'], people=[{
        'person_id': person, 'start': '2026-10-04T08:00:00+03:00',
        'end': '2026-10-04T12:00:00+03:00', 'overtime': overtime,
    }])
    findings = [f for f in result['validation']['findings'] if f['code'] == 'EXTRA_COVER_NOT_OVERTIME']
    assert len(findings) == int(not overtime)
    if findings:
        assert findings[0]['severity'] == 'warning'
        assert findings[0]['person_id'] == person and 'overtime' in findings[0]['message'].lower()
    assert result['validation']['valid']
    assert h.detail(result['workspace'])['shifts'][0]['participants'][0]['overtime'] is overtime
    assert h.publish(result['workspace'])['publication_id']


def test_optional_headcount_respects_requirement_intervals(h):
    duty_type = extra_type(h, requirements=[{'minimum': 1, 'to_minute': 120},
                                          {'minimum': 2, 'from_minute': 120}])
    result = h.shift(h.workspace(), typ=duty_type['id'], people=[{
        'person_id': h.seed['people'][0], 'start': '2026-10-04T08:00:00+03:00',
        'end': '2026-10-04T12:00:00+03:00', 'overtime': True,
    }])
    finding, = [f for f in result['validation']['findings'] if f['code'] == 'OPEN_EXTRA_COVER']
    assert finding['start'] == '2026-10-04T07:00:00+00:00'
    assert finding['end'] == '2026-10-04T09:00:00+00:00'
    assert finding['detail']['required'] == 2 and finding['detail']['available'] == 1
    summary = rota_summary(result['validation'])
    assert summary['places_needed'] == 2 and summary['places_filled'] == 1
    assert summary['extra_cover_places_open'] == 1 and summary['sessions_with_gaps'] == 0


@pytest.mark.parametrize('mode', ['fill_gaps', 'rebalance'])
def test_both_engines_fill_only_whole_duty_overtime_eligible_people(h, generate, mode):
    duty_type = extra_type(h)
    regular, eligible, partial = h.seed['people'][:3]
    h.policy({'kind': 'allocation_overtime'}, {'person_id': eligible,
        'assignment_type_id': duty_type['id']}, severity='advisory')
    h.policy({'kind': 'allocation_overtime'}, {'person_id': regular,
        'service_id': h.seed['services'][1]}, severity='advisory')
    h.policy({'kind': 'allocation_overtime'}, {'person_id': partial,
        'end_time': '10:00'}, severity='advisory')
    workspace = h.shift(h.workspace(), typ=duty_type['id'], people=[])['workspace']
    data = h.snapshot(workspace)
    data['generation_options'] = {'mode': mode}
    shift = data['shifts'][0]
    assert allocation_settings(data, shift, eligible)[1]
    assert not allocation_settings(data, shift, regular)[1]
    assert not allocation_settings(data, shift, partial)[1]
    before = copy.deepcopy(data)
    result = generate(data, 5)
    assert data == before
    assert result['status'] == 'candidate_feasible', result
    participant, = [a['participant'] for a in result['additions']]
    assert participant['person_id'] == eligible and participant['overtime'] is True
    assert result['validation']['valid']
    assert result['summary']['open_extra_cover_places_count'] == 0
    if generate is cp_sat_generate:
        assert result['optimality_proven']


@pytest.mark.parametrize('unfilled', ['open', 'gap'])
def test_no_overtime_eligible_people_never_get_regular_placements(h, generate, unfilled):
    duty_type = extra_type(h, unfilled)
    workspace = h.shift(h.workspace(), typ=duty_type['id'], people=[])['workspace']
    result = generate(h.snapshot(workspace), 5)
    assert result['additions'] == []
    if unfilled == 'open':
        assert result['status'] == 'candidate_feasible', result
        assert result['validation']['valid']
        assert {f['code'] for f in result['validation']['findings']} == {'OPEN_EXTRA_COVER'}
        assert result['summary']['open_places_count'] == 0
        assert result['summary']['open_places'] == []
        assert result['summary']['open_extra_cover_places_count'] == 1
        assert result['summary']['open_extra_cover_session_count'] == 1
        assert len(result['summary']['open_extra_cover']) == 1
        assert result['summary']['open_extra_cover'][0]['shift_id'] == h.snapshot(workspace)['shifts'][0]['id']
        assert job_outcome('succeeded', result) == 'complete'
        metadata = job_result_metadata(result)
        assert metadata['summary']['open_extra_cover_places_count'] == 1
        assert metadata['summary']['open_extra_cover_session_count'] == 1
        assert job_outcome('succeeded', metadata) == 'complete'
    else:
        assert result['status'] == ('requirements_conflict' if generate is cp_sat_generate else 'candidate_with_gaps')
        assert not result.get('optimality_proven', False)


@pytest.mark.parametrize('safeguard', ['cap', 'rest', 'qualification'])
def test_extra_cover_overtime_does_not_bypass_safety_or_qualifications(h, generate, safeguard):
    duty_type = extra_type(h)
    person = h.seed['people'][0]
    h.policy({'kind': 'allocation_overtime'}, {'person_id': person}, severity='advisory')
    workspace = h.workspace()
    if safeguard == 'cap':
        h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
            'metric': 'duties', 'comparison': 'maximum', 'amount': 0, 'overtime': 'only'},
            {'person_id': person})
    elif safeguard == 'rest':
        h.policy({'kind': 'min_rest', 'hours': 24})
        workspace = h.shift(workspace, start='2026-10-04T00:00:00+03:00',
            end='2026-10-04T04:00:00+03:00', people=[person])['workspace']
    workspace = h.shift(workspace, typ=duty_type['id'], people=[])['workspace']
    data = h.snapshot(workspace)
    data['people'] = {person: data['people'][person]}
    if safeguard == 'qualification':
        data['eligibilities'] = []
    result = generate(data, 5)
    assert result['status'] == 'candidate_feasible', result
    assert result['additions'] == [] and result['validation']['valid']
    assert result['summary']['open_extra_cover_places_count'] == 1


def type_floor_definition(h, floor):
    if floor == 'distinct_capabilities':
        return {'requirements': [{'minimum': 1, 'distinct_capability_seats': True, 'capabilities': [
            {'capability_id': h.seed['capability'], 'minimum': 1},
            {'capability_id': h.seed['supervisor_capability'], 'minimum': 1}]}]}
    skill = h.configure('capabilities', {'name': 'Synthetic ungranted skill', 'starts_on': '2020-01-01'})['record']['id']
    if floor == 'lead':
        return {'lead': {'minimum': 1, 'capability_ids': [skill]}}
    return {'requirements': [{'minimum': 1, 'capabilities': [{'capability_id': skill, 'minimum': 1}]}]}


@pytest.mark.parametrize('floor', ['capability', 'distinct_capabilities', 'lead'])
@pytest.mark.parametrize('unfilled', ['open', 'gap'])
def test_empty_extra_cover_type_floors_follow_unfilled_choice(h, floor, unfilled):
    duty_type = extra_type(h, unfilled, **type_floor_definition(h, floor))
    result = h.shift(h.workspace(), typ=duty_type['id'], people=[])
    workspace, check = result['workspace'], result['validation']
    assert check['valid'] is (unfilled == 'open')
    assert {f['severity'] for f in finding_observations(check)} == ({'warning'} if unfilled == 'open' else {'error'})
    assert any(f['code'] == {'capability': 'CAPABILITY_GAP', 'distinct_capabilities': 'DISTINCT_PERSON_GAP',
                            'lead': 'LEAD_GAP'}[floor] for f in finding_observations(check))
    if unfilled == 'open':
        assert {f['code'] for f in check['findings']} == {'OPEN_EXTRA_COVER'}
        assert h.publish(workspace)['publication_id']
    else:
        assert h.lifecycle(workspace, 'submit', status=422)['error']['code'] == 'VALIDATION_FAILED'


@pytest.mark.parametrize('floor', ['capability', 'distinct_capabilities', 'lead'])
@pytest.mark.parametrize('unfilled,volunteered', [('open', False), ('open', True), ('gap', False)])
def test_empty_extra_cover_type_floors_do_not_block_ordinary_proposals_when_open(h, generate, floor, unfilled, volunteered):
    duty_type = extra_type(h, unfilled, **type_floor_definition(h, floor))
    if volunteered:
        h.policy({'kind': 'allocation_overtime'}, {'person_id': h.seed['people'][0],
            'assignment_type_id': duty_type['id']}, severity='advisory')
    workspace = h.shift(h.workspace(), people=[])['workspace']
    workspace = h.shift(workspace, typ=duty_type['id'], people=[])['workspace']
    data = h.snapshot(workspace)
    ordinary = next(s for s in data['shifts'] if s['assignment_type_id'] == h.seed['type'])
    result = generate(data, 5)
    if unfilled == 'open':
        assert result['status'] == 'candidate_feasible', result
        assert result['validation']['valid']
        assert {a['shift_id'] for a in result['additions']} == {ordinary['id']}
        assert result['summary']['open_extra_cover_places_count'] == 1
        assert result['summary']['open_places_count'] == 0
    else:
        assert result['status'] == ('requirements_conflict' if generate is cp_sat_generate else 'candidate_with_gaps')


@pytest.mark.parametrize('floor', ['capability', 'distinct_capabilities', 'lead'])
def test_staffed_open_extra_cover_type_floors_stay_mandatory(h, generate, floor):
    duty_type = extra_type(h, **type_floor_definition(h, floor))
    person = h.seed['people'][0]
    h.policy({'kind': 'allocation_overtime'}, {'person_id': person}, severity='advisory')
    result = h.shift(h.workspace(), typ=duty_type['id'], people=[{
        'person_id': person, 'start': '2026-10-04T08:00:00+03:00',
        'end': '2026-10-04T12:00:00+03:00', 'overtime': True,
    }])
    workspace, check = result['workspace'], result['validation']
    assert not check['valid']
    assert any(f['code'] == {'capability': 'CAPABILITY_GAP', 'distinct_capabilities': 'DISTINCT_PERSON_GAP',
                            'lead': 'LEAD_GAP'}[floor] and f['severity'] == 'error' for f in finding_observations(check))
    assert h.lifecycle(workspace, 'submit', status=422)['error']['code'] == 'VALIDATION_FAILED'
    data = h.snapshot(workspace)
    data['people'] = {person: data['people'][person]}
    generated = generate(data, 5)
    assert generated['status'] == ('requirements_conflict' if generate is cp_sat_generate else 'candidate_with_gaps')


@pytest.mark.parametrize('floor', ['capability', 'distinct_capabilities', 'lead'])
def test_empty_open_extra_cover_folds_type_floors_without_a_headcount_gap(h, floor):
    definition = type_floor_definition(h, floor)
    if floor == 'lead':
        definition['requirements'] = []
    else:
        definition['requirements'][0]['minimum'] = 0
    duty_type = extra_type(h, **definition)
    result = h.shift(h.workspace(), typ=duty_type['id'], people=[])
    assert result['validation']['valid']
    assert {f['code'] for f in result['validation']['findings']} == {'OPEN_EXTRA_COVER'}
    summary = rota_summary(result['validation'])
    assert summary['places_open'] == summary['extra_cover_places_open'] == 0
    assert h.publish(result['workspace'])['publication_id']


def test_engines_fill_open_extra_cover_when_the_volunteer_meets_skill_and_lead_floors(h, generate):
    duty_type = extra_type(h, requirements=[{'minimum': 1, 'maximum': 1, 'capabilities': [
        {'capability_id': h.seed['capability'], 'minimum': 1}]}],
        lead={'minimum': 1, 'capability_ids': [h.seed['supervisor_capability']]})
    person = h.seed['people'][0]
    h.policy({'kind': 'allocation_overtime'}, {'person_id': person}, severity='advisory')
    workspace = h.shift(h.workspace(), typ=duty_type['id'], people=[])['workspace']
    result = generate(h.snapshot(workspace), 5)
    assert result['status'] == 'candidate_feasible', result
    participant, = [a['participant'] for a in result['additions']]
    assert participant['person_id'] == person and participant['overtime'] and participant['team_lead']
    assert result['validation']['valid']


@pytest.mark.parametrize('floor', ['capability', 'lead'])
def test_staffed_open_extra_cover_keeps_type_floors_in_unoccupied_intervals(h, generate, floor):
    definition = {'requirements': [{'minimum': 0}]}
    if floor == 'lead':
        definition['lead'] = {'minimum': 1, 'capability_ids': [h.seed['capability']]}
    else:
        definition['requirements'][0].update(from_minute=60, to_minute=180,
            capabilities=[{'capability_id': h.seed['capability'], 'minimum': 1}])
    duty_type = extra_type(h, **definition)
    person = h.seed['people'][0]
    h.policy({'kind': 'allocation_overtime'}, {'person_id': person}, severity='advisory')
    result = h.shift(h.workspace(), typ=duty_type['id'], people=[{
        'person_id': person, 'start': '2026-10-04T08:00:00+03:00',
        'end': '2026-10-04T12:00:00+03:00', 'overtime': True, 'team_lead': floor == 'lead',
        'segments': [{'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T09:00:00+03:00'},
                     {'start': '2026-10-04T11:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00'}],
    }])
    assert not result['validation']['valid']
    assert any(f['code'] == ('LEAD_GAP' if floor == 'lead' else 'CAPABILITY_GAP')
               and f['severity'] == 'error' and f['start'] == '2026-10-04T06:00:00+00:00'
               for f in finding_observations(result['validation']))
    data = h.snapshot(result['workspace'])
    data['people'] = {person: data['people'][person]}
    generated = generate(data, 5)
    assert generated['status'] == ('requirements_conflict' if generate is cp_sat_generate else 'candidate_with_gaps')


def test_open_extra_cover_never_relaxes_minimum_cover_policies(h, generate):
    duty_type = extra_type(h)
    h.policy({'kind': 'minimum_cover', 'minimum': 1}, {'assignment_type_id': duty_type['id']})
    workspace = h.shift(h.workspace(), typ=duty_type['id'], people=[])['workspace']
    check = evaluate(h.snapshot(workspace))
    assert not check['valid']
    assert any(f['severity'] == 'error' for f in finding_observations(check))
    assert any(f['code'] == 'OPEN_EXTRA_COVER' for f in finding_observations(check))
    assert h.lifecycle(workspace, 'submit', status=422)['error']['code'] == 'VALIDATION_FAILED'
    result = generate(h.snapshot(workspace), 5)
    assert result['status'] == ('requirements_conflict' if generate is cp_sat_generate else 'candidate_with_gaps')
    assert not result.get('optimality_proven', False)


def test_open_extra_cover_never_relaxes_supervision(h, generate):
    duty_type = extra_type(h, requirements=[{'minimum': 0}], supervision={
        'trainee_category_ids': [h.seed['trainee_category']],
        'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1})
    person = h.seed['people'][0]
    h.policy({'kind': 'allocation_overtime'}, {'person_id': person}, severity='advisory')
    result = h.shift(h.workspace(), typ=duty_type['id'], people=[{
        'person_id': person, 'start': '2026-10-04T08:00:00+03:00',
        'end': '2026-10-04T12:00:00+03:00', 'overtime': True, 'participation': 'trainee',
    }])
    workspace = result['workspace']
    assert not result['validation']['valid']
    assert any(f['code'] == 'SUPERVISION_GAP' and f['severity'] == 'error'
               for f in finding_observations(result['validation']))
    assert h.lifecycle(workspace, 'submit', status=422)['error']['code'] == 'VALIDATION_FAILED'
    data = h.snapshot(workspace)
    data['people'] = {person: data['people'][person]}
    generated = generate(data, 5)
    assert generated['status'] == ('requirements_conflict' if generate is cp_sat_generate else 'candidate_with_gaps')


def test_default_false_keeps_existing_validation_and_solver_choices(h, generate):
    h.policy({'kind': 'allocation_overtime'}, severity='advisory')
    workspace = h.shift(h.workspace(), people=[])['workspace']
    explicit = h.snapshot(workspace)
    for duty_type in [*explicit['types'].values(), *(s['type_snapshot'] for s in explicit['shifts'])]:
        duty_type['definition'].update(extra_cover=False, extra_cover_unfilled='open')
    legacy = copy.deepcopy(explicit)
    for duty_type in [*legacy['types'].values(), *(s['type_snapshot'] for s in legacy['shifts'])]:
        duty_type['definition'].pop('extra_cover', None)
        duty_type['definition'].pop('extra_cover_unfilled', None)
    assert evaluate(legacy) == evaluate(explicit)
    options = {'workers': 1, 'deterministic': True} if generate is cp_sat_generate else {}
    old, new = generate(legacy, 5, **options), generate(explicit, 5, **options)
    for field in ('status', 'additions', 'removals', 'validation', 'summary'):
        assert old[field] == new[field]
    assert old['status'] == 'candidate_feasible'
    assert all(not addition['participant']['overtime'] for addition in old['additions'])


def test_job_history_preserves_optional_extra_cover_counts_and_complete_outcome(h):
    duty_type = extra_type(h)
    workspace = h.shift(h.workspace(), typ=duty_type['id'], people=[])['workspace']
    source = h.snapshot(workspace)
    result = {
        'status': 'candidate_feasible',
        'validation': {'valid': True, 'error_count': 0, 'warning_count': 1},
        'summary': {'required_places': 1, 'filled_places': 0, 'open_places_count': 0,
                    'open_extra_cover_places_count': 1, 'open_extra_cover_session_count': 1},
    }
    with transaction(h.app.state.engine, write=True) as db:
        job = m.Job(department_id=h.seed['department_id'], workspace_id=workspace['id'],
            requested_by=h.seed['identities']['coordinator'], kind='generation', status='succeeded',
            input_fingerprint=source['fingerprint'], input_snapshot=source, result=result)
        db.add(job)
        db.flush()
        job_id = job.id
    listed, = h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])['jobs']
    assert listed['id'] == job_id and listed['outcome'] == 'complete'
    assert listed['result']['summary']['open_places_count'] == 0
    assert listed['result']['summary']['open_extra_cover_places_count'] == 1
    assert listed['result']['summary']['open_extra_cover_session_count'] == 1
    assert listed['result']['validation']['valid'] is True


@pytest.mark.postgres
def test_job_history_preserves_optional_extra_cover_counts_postgres(pg):
    test_job_history_preserves_optional_extra_cover_counts_and_complete_outcome(pg)
