"""Why reuses unchanged personal checks without changing any explanation or score."""
from datetime import date

import pytest
from sqlalchemy import insert

from apps.api import models as m
from apps.api.db import transaction
from tests.test_rule_map import rule

from apps.api.services import staffing
from packages.domain import validation
from tests.test_backend_scale import measure, scale_department
from tests.test_planner_why import why
from tests.test_performance_budget import python_budget


@pytest.mark.postgres
def test_department_why_reuses_exact_trials(pg, monkeypatch, record_property):
    workspace, data, _, _ = scale_department(pg)
    people = list(data['people'])
    policies = [rule(data, f'Synthetic Why rule {n}', {'kind': 'max_hours', 'hours': 168},
                     {'person_ids': [people[n % len(people)]]}) for n in range(997)]
    with transaction(pg.app.state.engine, write=True) as db:
        db.execute(insert(m.Policy), [{**{k: v for k, v in p.items() if k in m.Policy.__table__.columns},
            'starts_on': date(2020, 1, 1), 'created_at': m.now(),
            'status': 'active' if n < 163 else 'retired', 'active': n < 163,
            'approved_by': pg.seed['identities']['approver'], 'owner': 'Synthetic owner',
            'source': 'Synthetic scale fixture', 'created_by': pg.seed['identities']['coordinator']}
            for n, p in enumerate(policies)])
        db.get(m.Organisation, pg.seed['organisation_id']).input_revision += 1
    shift = data['shifts'][0]
    pg.client('coordinator')
    calls = []
    original_actual = validation.workload_actual
    def actual(*args, **kwargs):
        calls.append(True)
        return original_actual(*args, **kwargs)
    monkeypatch.setattr(validation, 'workload_actual', actual)
    result, measured = measure(pg, 'why-reused', lambda: why(pg, workspace, [shift['id']]))
    reused_work = len(calls)
    calls.clear()
    original = validation.evaluate
    def uncached(*args, **kwargs):
        kwargs.pop('trial_cache', None)
        source, *rest = args
        source = {key: value for key, value in source.items() if key != '_participation_cache'}
        return original(source, *rest, **kwargs)
    with monkeypatch.context() as reference:
        reference.setattr(staffing, '_WHY_RESULT_CACHE', {})
        reference.setattr(validation, 'evaluate', uncached)
        reference.setattr(staffing, 'evaluate', uncached)
        expected, baseline = measure(pg, 'why-reference', lambda: why(pg, workspace, [shift['id']]))
    result.pop('evaluated_at')
    expected.pop('evaluated_at')
    record_property('why_trial_work', reused_work)
    record_property('why_reference_work', len(calls))
    record_property('why_selects', measured['sql'].get('SELECT', 0))
    assert result == expected
    assert len(result['sessions'][0]['rows']) == 163
    assert reused_work < len(calls) * .8, (reused_work, len(calls))
    assert measured['sql'].get('SELECT', 0) <= 35, measured
    print('why work', {'before': len(calls), 'after': reused_work, 'before_seconds': baseline,
                       'after_seconds': measured})
    return locals()


@pytest.mark.postgres
@pytest.mark.cpu_budget
def test_department_why_cpu_budget(pg, monkeypatch, record_property):
    state = test_department_why_reuses_exact_trials(pg, monkeypatch, record_property)
    assert state['measured']['cpu_seconds'] < python_budget(3.5), state['measured']


@pytest.mark.postgres
@pytest.mark.wall_benchmark
def test_department_why_wall_budget(pg, monkeypatch, record_property):
    state = test_department_why_reuses_exact_trials(pg, monkeypatch, record_property)
    assert state['measured']['wall_seconds'] < python_budget(12), state['measured']
