from dataclasses import replace

import pytest

from apps.api import models as m
from apps.api.auth import authenticate
from apps.api.db import transaction
from apps.api.errors import Problem, problem_payload
from apps.api.services.jobs import create_job
from apps.worker import main as worker
from packages.contracts.schemas import Command, JobErrorDetail


def test_problem_payload_cap_drops_person_id_from_public_detail():
    refusal = Problem(422, 'SOLVER_RULE_INCOMPATIBLE', 'Resolve rule settings before generating', [{
        'person_id': 'private-person', 'rule_id': 'rule-1', 'code': 'UNSUPPORTED_SOLVER_RULE',
        'message': 'Automatic scheduling does not support this rule.',
    }])

    payload = problem_payload(refusal, cap=50)

    assert payload['total'] == 1
    assert payload['details'] == [{
        'rule_id': 'rule-1', 'code': 'UNSUPPORTED_SOLVER_RULE',
        'message': 'Automatic scheduling does not support this rule.',
    }]


def queued_generation(h):
    workspace = h.shift(h.workspace('Deferred refusal details'), people=[])['workspace']
    client = h.client('coordinator')
    settings = replace(h.settings, generation_engine='cp_sat')
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
        queued = create_job(db, actor, Command(kind='job.create', expected_revision=workspace['revision'],
            payload={'workspace_id': workspace['id'], 'kind': 'generation'}), settings)['job']
    return workspace, queued


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_deferred_preparation_refusal_is_available_on_detail_and_status_but_not_list(h, monkeypatch):
    workspace, queued = queued_generation(h)
    issues = [{
        'rule_id': 'rule-' + str(index), 'rule_revision': 1,
        'field': f'definition.rule.lines.{index}.numbers',
        'code': 'UNSUPPORTED_SOLVER_RULE', 'message': 'Automatic scheduling does not support this rule.',
    } for index in range(55)]
    refusal = Problem(422, 'SOLVER_RULE_INCOMPATIBLE', 'Resolve rule settings before generating', issues)

    def refuse(*_args, **_kwargs):
        raise refusal

    monkeypatch.setattr(worker, 'prepare_deferred_generation', refuse)
    processed = worker.process_one(h.app.state.engine, scheduler=lambda *_args, **_kwargs: {})
    assert processed

    synchronous = problem_payload(refusal)
    detail = h.get('coordinator', '/api/jobs/' + queued['id'])['job']
    status = h.get('coordinator', '/api/jobs/' + queued['id'] + '/status')['job']
    listed = h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])['jobs']
    stored = detail['error_detail']
    expected = problem_payload(refusal, cap=50)
    assert detail['status'] == status['status'] == 'failed'
    assert detail['error_code'] == status['error_code'] == 'SOLVER_RULE_INCOMPATIBLE'
    assert stored['code'] == expected['code'] and stored['message'] == expected['message']
    assert stored['details'] == expected['details'] and stored['total'] == expected['total']
    assert stored['details'] == synchronous['details'][:50]
    JobErrorDetail.model_validate(stored)
    assert status['error_detail'] == stored
    assert 'error_detail' not in listed[0]
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, queued['id'])
        assert row.result == {'status': 'refused', 'error': stored}
