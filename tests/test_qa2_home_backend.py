"""rc.13 Home audit batches and staff publication boundaries, on both supported databases."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import home
from .test_request_decide_many import decide, request_row, pattern_row
from .test_schedule_window import published_rota


@pytest.fixture(params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def database(request):
    return request.getfixturevalue(request.param)


def test_recent_changes_counts_full_batches_before_limit(database):
    h = database
    rows = [request_row(h) for _ in range(98)]
    decide(h, rows[:2])
    batch = decide(h, rows[2:])
    h.command('approver', 'request.undo', {'batch_id': batch['batch_id'], 'reason': 'Synthetic batch undo'})
    changes = h.get('auditor', '/api/home')['recent_changes']
    assert next(e['count'] for e in changes if e['action'] == 'request.undo') == 96
    assert sorted(e['count'] for e in changes if e['action'] == 'request.decide') == [2, 96]
    assert all('detail' not in e for e in changes)
    # Department scoping and excluding authentication happen before window counts too.
    with transaction(h.app.state.engine, write=True) as db:
        dept = m.Department(organisation_id=h.seed['organisation_id'], name='Other department')
        db.add(dept)
        db.flush()
        db.add(m.AuditEvent(department_id=dept.id, actor_id=h.seed['identities']['approver'],
                           action='request.undo', reason='Synthetic', detail={'batch_id': batch['batch_id']},
                           correlation_id=next(e['correlation_id'] for e in changes if e['action'] == 'request.undo')))
    assert next(e['count'] for e in h.get('auditor', '/api/home')['recent_changes'] if e['action'] == 'request.undo') == 96
    assert not h.get('staff', '/api/home')['recent_changes']


def recent_fixture(h):
    """Three approvals and the later Undo; adjacent same-action batches remain separate."""
    with transaction(h.app.state.engine, write=True) as db:
        for index, (action, size, batch) in enumerate((('request.decide', 7, 'a'), ('request.decide', 12, 'b'),
                                                     ('request.decide', 98, 'c'), ('request.undo', 98, 'c'))):
            db.add_all([m.AuditEvent(department_id=h.seed['department_id'], actor_id=h.seed['identities']['approver'],
                action=action, reason='Synthetic', detail={'batch_id': batch}, correlation_id=str(index),
                created_at=m.now() + timedelta(seconds=index)) for _ in range(size)])


def test_recent_changes_keep_distinct_batches_and_true_98_count(database):
    h = database
    recent_fixture(h)
    recent = h.get('auditor', '/api/home')['recent_changes']
    assert [(e['action'], e['count']) for e in recent[:4]] == [
        ('request.undo', 98), ('request.decide', 98), ('request.decide', 12), ('request.decide', 7)]
    # The service is the one API projection; never mutate append-only audit evidence.
    with transaction(h.app.state.engine) as db:
        assert len(list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == 'request.undo')))) == 98


def test_recent_changes_groups_mixed_request_and_weekly_batch(database):
    h = database
    batch = decide(h, [request_row(h), pattern_row(h)], role='admin')
    recent = h.get('auditor', '/api/home')['recent_changes']
    assert [(e['action'], e['count']) for e in recent if e['action'].endswith('.decide')] == [('request.decide', 2)]
    assert all(e['action'] != 'request.decide_many' for e in recent)
    h.command('admin', 'request.undo', {'batch_id': batch['batch_id'], 'reason': 'Synthetic mixed undo'})
    recent = h.get('auditor', '/api/home')['recent_changes']
    assert [(e['action'], e['count']) for e in recent if e['action'].endswith('.undo')] == [('request.undo', 2)]


def test_staff_publication_coverage_omits_rule_findings(database):
    h = database
    pid = published_rota(h)
    h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0],
        'start': '2026-10-04T10:00:00+03:00', 'end': '2026-10-04T11:00:00+03:00',
        'kind': 'actual_absence', 'private_reason': 'Synthetic private reason'})
    path = f'/api/publications/{pid}/coverage'
    operator = h.get('coordinator', path)['evaluation']
    assert operator['findings'] and operator['error_count'] > 0
    staff = h.get('staff', path)['evaluation']
    assert staff['findings'] == [] and staff['error_count'] == staff['warning_count'] == 0
    assert {r['shift_id'] for r in staff['coverage']} <= {
        s['id'] for s in h.get('staff', f'/api/publications/{pid}')['snapshot']['shifts']}
    assert h.get('staff', '/api/schedule/sessions?start=2026-10-01&end=2026-11-01')['findings'] == []
    # A staff read cannot strip the operator's cached evaluation.
    assert h.get('coordinator', path)['evaluation']['findings'] == operator['findings']
