"""Clinic-specific quota subjects stay bounded before a department rota has assignments."""
import copy
import gc
import json
import time
import uuid

import pytest

from packages.contracts.schemas import PolicyDefinition, TypeDefinition
from packages.domain import conditions
from packages.domain.validation import evaluate
from tests.scheduler_departments import department
from tests.test_performance_budget import python_budget
from tests.test_quota_skill_subjects import _quota
from tests.test_rule_skill_precedence import _line, _who


@pytest.fixture(scope="module")
def department_quota_preparation(scale_harness):
    h = scale_harness()
    with pytest.MonkeyPatch.context() as monkeypatch:
        _, template, _, primary_skill = _quota(h)
        data, _ = department(h, 41, people=150, days=28, rules='none', leave_rate=0, clinics=9)
        dept = data['workspace']['department_id']
        services = list(data['services'])
        for n in range(len(services), 9):
            sid = str(uuid.uuid5(uuid.NAMESPACE_URL, f'quota-scale-service-{n}'))
            data['services'][sid] = {**data['services'][services[0]], 'id': sid}
            services.append(sid)
        skills = [primary_skill]
        for n in range(1, 3):
            skill = str(uuid.uuid5(uuid.NAMESPACE_URL, f'quota-scale-skill-{n}'))
            data['capabilities'][skill] = {**data['capabilities'][primary_skill], 'id': skill}
            skills.append(skill)
        # Each skill is held at three clinics, so shared duties must use the complete skill intersection.
        data['grants'] = [
            {'id': str(uuid.uuid5(uuid.NAMESPACE_URL, f'quota-scale-grant-{pid}-{k}-{s}')),
             'person_id': pid, 'department_id': dept, 'capability_id': skill,
             'service_id': sid, 'starts_on': '2020-01-01', 'ends_on': None, 'revoked': False}
            for pid in data['people'] for k, skill in enumerate(skills)
            for s, sid in enumerate(services) if s % 3 == k
        ]
        for typ in data['types'].values():
            typ['definition'] = TypeDefinition.model_validate({**typ['definition'], 'allow_shared_services': True,
                'shared_duty_reason': 'Synthetic reviewed common duty'}).model_dump(mode='json')
        for n, shift in enumerate(data['shifts']):
            shift['service_ids'] = [services[n % 9], services[(n + 1) % 9]] if n % 4 == 0 else [services[n % 9]]
            shift['type_snapshot'] = copy.deepcopy(data['types'][shift['assignment_type_id']])
        data['quota_context'] = {'complete': True, 'start': '2026-10-04T00:00:00+03:00',
                                 'end': '2026-11-01T00:00:00+03:00', 'publications': []}
        data['policies'] = []
        for n in range(6):
            policy = copy.deepcopy(template)
            policy['id'] = str(uuid.uuid5(uuid.NAMESPACE_URL, f'quota-scale-policy-{n}'))
            lines = policy['definition']['rule']['lines']
            lines[0]['numbers']['max']['value'] = 30
            lines[1]['numbers']['max'].update(value=40, level='high')
            lines[1]['who'] = _who('capability_ids', skills[n % 3])
            settings = {'block_quota': {'metric': 'duties', 'starts_on': '2026-10-04', 'ends_on': '2026-11-01'}}
            for line in lines:
                line['settings'] = copy.deepcopy(settings)
            for pid in data['people']:
                own = _line(35, who=_who('person_ids', pid), level=None)
                own['settings'] = copy.deepcopy(settings)
                lines.append(own)
            policy['definition'] = PolicyDefinition.model_validate(policy['definition']).model_dump(mode='json')
            data['policies'].append(policy)

        before, original_contexts = copy.deepcopy(data), conditions._scope_duty_contexts
        contexts = 0

        def counted(*args, **kwargs):
            nonlocal contexts
            for context in original_contexts(*args, **kwargs):
                contexts += context[2] is not None
                yield context

        monkeypatch.setattr(conditions, '_scope_duty_contexts', counted)
        gc.collect()
        started = time.process_time()
        checked = evaluate(data, quota_only=True)
        elapsed = time.process_time() - started
        report = checked['quota_report']
        measurement = {'people': len(data['people']), 'sessions': len(data['shifts']), 'clinics': len(services),
                       'skills': len(skills), 'grants': len(data['grants']), 'quota_sources': len(data['policies']),
                       'catalogue_lines': sum(len(p['definition']['rule']['lines']) for p in data['policies']),
                       'report_rows': len(report['rows']), 'virtual_contexts': contexts, 'cpu_seconds': elapsed}
        print('clinic-scoped quota preparation', measurement)
        return measurement, report, data, before, checked, contexts


def test_clinic_scoped_quota_preparation_at_department_scale(department_quota_preparation, request):
    measurement, report, data, before, checked, contexts = department_quota_preparation
    request.node.user_properties.append(('quota_subject_performance', json.dumps(measurement)))
    assert (measurement['people'], measurement['sessions'], measurement['catalogue_lines']) == (150, 532, 912)
    assert report['complete'] and data == before, checked
    assert len(report['rows']) == 900
    assert {(r['policy_id'], r['person_id']) for r in report['rows']} == {
        (policy['id'], pid) for policy in data['policies'] for pid in data['people']}
    # Personal amounts inherit the mandatory default available outside their clinic-specific skill grants.
    assert all(r['actual'] == 0 and r['amount'] == 35 and r['severity'] == 'mandatory' for r in report['rows'])
    # Measured 10,200 contexts / 1.24 CPU seconds. Bound repeated work independently of runner speed;
    # the pure-Python CPU ceiling leaves >3x native headroom and uses the existing tracing allowance.
    assert 0 < contexts < 20_000, measurement


@pytest.mark.cpu_budget
def test_clinic_scoped_quota_preparation_at_department_scale_cpu_budget(department_quota_preparation):
    measurement, *_ = department_quota_preparation
    assert measurement['cpu_seconds'] < python_budget(4), measurement
