"""Publication refuses mandatory safety rules activated after approval."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('urgent', [False, True])
@pytest.mark.parametrize('kind,code', [('min_rest', 'MIN_REST'), ('forbidden_transition', 'FORBIDDEN_TRANSITION')])
def test_late_mandatory_safety_rule_refuses_publication(request, database, urgent, kind, code):
    h = request.getfixturevalue(database)
    if urgent:
        route = h.command('admin', 'review_route.create', {
            'name': 'Urgent safety review', 'starts_on': '2026-01-01',
            'definition': {'steps': [{'key': 'routine', 'label': 'Routine review'}],
                           'urgent_steps': [{'key': 'urgent', 'label': 'Urgent review'}]},
            'reason': 'Urgent safety regression'})['route']
        h.command('approver', 'review_route.activate', {'reason': 'Activate urgent safety regression'},
                  target=route['id'], revision=route['revision'])
        publication = h.publish(h.shift(h.workspace())['workspace'])['publication_id']
    typ = h.configure('assignment_types', {'name': 'Synthetic next-day duty', 'starts_on': '2020-01-01',
                                          'definition': {}})['record']
    workspace = (h.command('coordinator', 'workspace.create', {
        'name': 'Urgent safety amendment', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
        'base_publication_id': publication, 'urgency': 'urgent', 'urgency_reason': 'Urgent safety regression',
    })['workspace'] if urgent else h.shift(h.workspace())['workspace'])
    workspace = h.shift(workspace, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00',
                        typ=typ['id'])['workspace']
    workspace = h.lifecycle(workspace, 'submit')['workspace']
    workspace = h.lifecycle(workspace, 'approve', 'approver')['workspace']
    if kind == 'min_rest':
        rule = h.command('coordinator', 'rule.propose', {'catalogue_id': 'rest_between_duties', 'lines': [{
            'id': m.uid(), 'who': {'conditions': [{'field': 'person_ids', 'values': [h.seed['people'][0]]}]},
            'where': {'conditions': [{'field': 'assignment_type_ids', 'values': [typ['id']]}]},
            'numbers': {'min': {'value': 24}}}], 'reason': 'Late mandatory rest catalogue line'})['record']
    else:
        rule = h.command('coordinator', 'policy.create', {
            'name': 'Synthetic late next-day safety rule', 'owner': 'Synthetic owner', 'source': 'Regression',
            'starts_on': '2020-01-01', 'definition': {
                'rule': {'kind': kind, 'from_type_ids': [h.seed['type']], 'to_type_ids': [typ['id']]},
                'scope': {'person_id': h.seed['people'][0]}, 'severity': 'mandatory',
                'exception_allowed': False}})['record']
    h.command('approver', 'policy.activate', {'reason': 'Activate after the candidate was checked'},
              rule['id'], rule['revision'])
    refused = h.lifecycle(workspace, 'publish', 'coordinator', status=422)
    assert refused['error']['code'] == 'VALIDATION_FAILED'
    assert refused['error']['details']['stale_approval']['code'] == 'STALE_APPROVAL'
    assert code in {finding['code'] for finding in refused['error']['details']['findings']}
    assert h.detail(workspace)['workspace']['status'] == workspace['status']
    assert len(h.get('coordinator', '/api/publications')['publications']) == int(urgent)
    with transaction(h.app.state.engine) as db:
        events = list(db.scalars(select(m.AuditEvent).where(
            m.AuditEvent.target_id == workspace['id'], m.AuditEvent.action == 'command.refused')))
        assert any(event.detail['command_kind'] == 'workspace.publish'
                   and event.detail['error_code'] == 'VALIDATION_FAILED' for event in events)
        assert any(event.detail.get('stale_approval') is True for event in events)
    checked = h.lifecycle(workspace, 'validate')['validation']
    assert code in {finding['code'] for finding in checked['findings']}
    request.node.user_properties.append(('refusal_code', 'VALIDATION_FAILED'))


@pytest.mark.wall_benchmark
def test_publish_department_scale_observation(h, monkeypatch, record_property):
    from apps.api import snapshots
    from tests.test_backend_scale import measure, post, scale_department

    workspace, data, assignments, _ = scale_department(h)
    workspace = h.lifecycle(workspace, 'submit')['workspace']
    workspace = h.lifecycle(workspace, 'approve', 'approver')['workspace']
    acquisitions = []
    original = snapshots._domain_snapshot

    def acquired(db, *args, **kwargs):
        acquisitions.append(bool(db.connection().info.get('rota_write')))
        return original(db, *args, **kwargs)

    monkeypatch.setattr(snapshots, '_domain_snapshot', acquired)
    result, metrics = measure(h, 'publish', lambda: post(h, 'workspace.publish',
        {'reason': 'Department-scale publication measurement'}, workspace['id'], workspace['revision']))
    assert result['publication_id']
    for name, value in {
        'people': len(data['people']), 'sessions': len(data['focus_ids']), 'assignments': len(assignments),
        'publish_wall_seconds': metrics['wall_seconds'], 'publish_cpu_seconds': metrics['cpu_seconds'],
        'publish_save_budget_seconds': 5, 'publish_within_save_budget': metrics['wall_seconds'] <= 5,
        'publish_writer_snapshots': acquisitions.count(True),
    }.items():
        record_property(name, value)
