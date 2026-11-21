"""Bulk proposal admission at department scale, with CPU budgets independent of runner contention."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import gc
from time import perf_counter, process_time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from apps.api import models as m
from apps.api import rate_limit
from apps.api.db import transaction
from packages.contracts.schemas import PolicyDefinition
from .legacy_lead import historical_type
from .test_backend_scale import measure
from .test_performance_budget import python_budget
from .test_rule_catalogue_api import approve, propose


def department(h, *, people_count=150):
    workspace = h.workspace('Synthetic bulk proposal fixture', start='2026-10-01', end='2026-11-01')
    with transaction(h.app.state.engine, write=True) as db:
        dept = db.get(m.Department, h.seed['department_id'])
        people = list(h.seed['people'])
        added = [m.Person(id=str(uuid4()), organisation_id=dept.organisation_id,
                          display_name=f'Synthetic proposal person {n} (test)') for n in range(people_count - len(people))]
        db.add_all(added)
        db.flush()
        db.add_all([m.Membership(department_id=dept.id, person_id=p.id, category_id=h.seed['category'],
                                starts_on=date(2020, 1, 1)) for p in added])
        people.extend(p.id for p in added)
        history = PolicyDefinition.model_validate({'rule': {'kind': 'max_duties', 'maximum': 7}}).model_dump(mode='json')
        db.add_all([m.Policy(department_id=dept.id, name=f'Synthetic history {n}', kind='max_duties',
            definition=history, status='retired', active=False, starts_on=date(2020, 1, 1),
            owner='Synthetic owner', source='Synthetic scale regression', created_by=h.seed['identities']['coordinator'])
            for n in range(1000)])
        typ = db.get(m.AssignmentType, h.seed['type'])
        first = datetime(2026, 10, 1, 5, tzinfo=timezone.utc)
        db.add_all([m.Shift(workspace_id=workspace['id'], assignment_type_id=typ.id, type_revision=typ.revision,
            type_snapshot=typ.definition, location_id=h.seed['locations'][n % len(h.seed['locations'])],
            start=first + timedelta(days=n % 31), end=first + timedelta(days=n % 31, hours=4)) for n in range(500)])
    h.client('coordinator')
    return people


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_legacy_lead_conversion_preview_select_budget(scale_harness, backend):
    h = scale_harness(backend)
    people = department(h, people_count=163)
    duty = historical_type(h, {'name': 'Historical lead duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1}], 'lead': {'minimum': 1, 'maximum': 1}}})
    client = h.client('coordinator')
    response, metrics = measure(h, 'Team leads conversion preview', lambda: client.post(
        '/api/rules/catalogue/team_leads/check', json={'legacy_effective_on': '2026-10-04'}))
    assert response.status_code == 200, response.text
    preview = response.json()
    assert not preview['issues'] and not preview['duplicates']
    assert [source['type_id'] for source in preview['legacy_lead_conversion']['sources']] == [duty['id']]
    reached = {person['person_id'] for line in preview['reach_details']['lines'] for person in line['people']}
    assert len(reached) == 163 and reached == set(people)
    assert 0 < metrics['sql']['SELECT'] <= 22, metrics['sql']


def measured(label, operation, ceiling):
    gc.collect()  # Exclude prior fixtures' garbage; request-time collections remain inside the CPU budget.
    cpu, wall = process_time(), perf_counter()
    result = operation()
    cpu, wall = process_time() - cpu, perf_counter() - wall
    print(f'{label}: {cpu:.3f} CPU s / {wall:.3f} wall s')
    return result, (label, cpu, ceiling)


@pytest.fixture(scope='module', params=[
    pytest.param((backend, count, catalogue_id, number), marks=pytest.mark.postgres if backend == 'pg' else ())
    for backend in ('h', 'pg') for count, catalogue_id, number in (
        (12, 'duties_per_week', 'max'), (12, 'session_units_per_week', 'max'),
        (150, 'duties_per_week', 'max'), (150, 'session_units_per_week', 'max'),
        (150, 'duties_per_week', 'target'))
])
def line_proposal_measurement(request, scale_harness):
    backend, count, catalogue_id, number = request.param
    h = scale_harness(backend)
    # Keep one limiter window; setup under load can otherwise expire and prune its initial credit.
    moment = m.now()
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(rate_limit, 'm', SimpleNamespace(now=lambda: moment, RateLimit=m.RateLimit, Session=m.Session))
        people = department(h)
        amount = {'value': 32 / 7, 'level': 'medium'} if number == 'target' else {'value': 5}
        lines = [{'id': str(uuid4()), 'who': {'conditions': [{'field': 'person_ids', 'values': [person]}]},
                  'numbers': {number: amount}, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'} for person in people[:count]]
        body = {'lines': lines}
        label = f'{backend} {catalogue_id} {count} {number} lines'
        with transaction(h.app.state.engine) as db:
            credits = db.scalar(select(func.sum(m.RateLimit.hits)))
        catalogue_response, catalogue_metric = measured(label + ' catalogue preview', lambda: h.client('coordinator').post(
            f'/api/rules/catalogue/{catalogue_id}/check', json=body), 0.8)
        definition = {'rule': {'kind': 'catalogue', 'id': catalogue_id, **body}}
        policy_response, policy_metric = measured(label + ' policy preview', lambda: h.client('coordinator').post(
            '/api/policies/check', json={'definition': definition}), 0.8)
        with transaction(h.app.state.engine) as db:
            preview_credits = db.scalar(select(func.sum(m.RateLimit.hits)))
        saved, proposal_metric = measured(label + ' propose', lambda: propose(h, catalogue_id, lines), 2)
        saved = saved['record']
        with transaction(h.app.state.engine) as db:
            stored = db.scalar(select(m.Policy).where(m.Policy.id == saved['id'])).definition
            final_credits = db.scalar(select(func.sum(m.RateLimit.hits)))
    return (count, number, catalogue_response, policy_response, credits, preview_credits, saved, stored, final_credits,
            [catalogue_metric, policy_metric, proposal_metric])


def test_line_preview_and_proposal_cpu_budget(line_proposal_measurement):
    count, number, catalogue_response, policy_response, credits, preview_credits, saved, stored, final_credits, _ = line_proposal_measurement
    assert catalogue_response.status_code == 200, catalogue_response.text
    checked = catalogue_response.json()
    assert not checked['issues'] and not checked['duplicates'] and not checked['overlaps']
    assert len(checked['reach_details']['lines']) == count
    assert policy_response.status_code == 200, policy_response.text
    assert policy_response.json()['compatible'] and len(policy_response.json()['reach_details']['lines']) == count
    assert preview_credits == credits
    assert len(saved['definition']['rule']['lines']) == count and saved['status'] == 'proposed'
    assert all(line['numbers'][number]['value'] == (4.57 if number == 'target' else 5)
               for line in saved['definition']['rule']['lines'])
    assert stored == saved['definition']
    assert final_credits == credits + 1


@pytest.mark.cpu_budget
def test_line_preview_and_proposal_cpu_budget_timing(line_proposal_measurement):
    *_, metrics = line_proposal_measurement
    for label, cpu, ceiling in metrics:
        assert cpu <= python_budget(ceiling), (label, cpu, python_budget(ceiling))


@pytest.fixture(scope='module', params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def legacy_proposal_measurement(request, scale_harness):
    backend = request.param
    h = scale_harness(backend)
    people = department(h)
    definition = {'rule': {'kind': 'block_quota', 'metric': 'session_units', 'comparison': 'target', 'amount': 7,
        'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'window_alignment': 'calendar_week'},
        'severity': 'advisory', 'listed_only': True,
        'per_person': [{'person_id': person, 'amount': 7} for person in people]}
    response, preview_metric = measured(backend + ' legacy 150 lines policy preview', lambda: h.client('coordinator').post(
        '/api/policies/check', json={'definition': definition}), 0.8)
    saved, proposal_metric = measured(backend + ' legacy 150 lines propose', lambda: h.command('coordinator', 'policy.create', {
        'name': 'Synthetic weekly units', 'owner': 'Synthetic owner', 'source': 'Synthetic bulk regression',
        'starts_on': '2026-10-01', 'definition': definition}), 2)
    return response, saved['record'], [preview_metric, proposal_metric]


def test_legacy_150_person_preview_and_proposal_cpu_budget(legacy_proposal_measurement):
    response, saved, _ = legacy_proposal_measurement
    assert response.status_code == 200 and response.json()['compatible'], response.text
    assert len(response.json()['reach_details']['lines']) == 150
    assert len(saved['definition']['per_person']) == 150 and saved['status'] == 'proposed'


@pytest.mark.cpu_budget
def test_legacy_150_person_preview_and_proposal_cpu_budget_timing(legacy_proposal_measurement):
    *_, metrics = legacy_proposal_measurement
    for label, cpu, ceiling in metrics:
        assert cpu <= python_budget(ceiling), (label, cpu, python_budget(ceiling))


@pytest.mark.parametrize('catalogue_id', ['not_after', 'companion_cover'])
@pytest.mark.parametrize('settings_on', ['rule', 'line'])
def test_date_only_renewal_preserves_complete_required_settings(h, catalogue_id, settings_on):
    settings = ({'forbidden_transition': {'from_type_ids': [h.seed['type']], 'to_type_ids': [h.seed['type']]}}
                if catalogue_id == 'not_after' else {'minimum_cover': {'when_person_ids': [h.seed['people'][0]]}})
    line = {'id': str(uuid4()), 'numbers': {'apply': {}} if catalogue_id == 'not_after' else {'min': {'value': 2}},
            'starts_on': '2026-10-01', 'ends_on': '2026-10-10'}
    rule_settings = settings if settings_on == 'rule' else {}
    if settings_on == 'line':
        line['settings'] = settings
    live = approve(h, propose(h, catalogue_id, [line], settings=rule_settings)['record'])
    renewed = deepcopy(live['definition']['rule']['lines'])
    renewed[0]['ends_on'] = '2026-11-01'
    result = propose(h, catalogue_id, renewed, settings=rule_settings,
                     policy_id=live['id'], revision=live['revision'])['record']
    assert result['definition']['rule']['settings'] == live['definition']['rule']['settings']
    assert result['definition']['rule']['lines'][0]['settings'] == live['definition']['rule']['lines'][0]['settings']
    assert result['definition']['rule']['lines'][0]['ends_on'] == '2026-11-01'
