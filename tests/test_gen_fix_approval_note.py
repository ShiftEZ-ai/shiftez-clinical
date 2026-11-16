"""Optional independent approval notes retain only the person's words."""
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from .test_request_decide_many import request_row


@pytest.mark.parametrize('note', [None, '', 'OK', 'Cover arranged'])
@pytest.mark.parametrize('bulk', [False, True])
@pytest.mark.postgres
def test_independent_approval_keeps_exact_note(pg, note, bulk):
    row = request_row(pg)
    payload = {'decision': 'approve', **({'reason': note} if note is not None else {})}
    kind = 'request.decide_many' if bulk else 'request.decide'
    if bulk:
        payload['items'] = [{'id': row['id'], 'expected_revision': row['revision']}]
    pg.command('approver', kind, payload, None if bulk else row['id'], None if bulk else row['revision'])
    with transaction(pg.app.state.engine) as db:
        assert db.get(m.RequestRecord, row['id']).decision_reason == (note or '')
        audit = db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == kind))
        assert audit.reason == (note or '')


@pytest.mark.parametrize('schema,decision', [(s.RequestDecision, 'reject'), (s.RequestDecision, 'withdraw'),
                                            (s.PatternDecision, 'decline')])
def test_refusal_still_requires_reason(schema, decision):
    with pytest.raises(ValidationError):
        schema.model_validate({'decision': decision})
    assert schema.model_validate({'decision': 'approve'}).note is None


@pytest.mark.postgres
def test_sick_call_approval_without_note_stores_no_reason(pg):
    from .test_be16_published import issued
    publication, shifts = issued(pg)
    row = pg.command('staff', 'request.create', {'kind': 'absence_report', 'publication_id': publication,
        'assignment_id': shifts[0]['participants'][0]['id'], 'reason': 'Synthetic sick call'})['record']
    pg.command('approver', 'request.decide', {'decision': 'approve'}, row['id'], row['revision'])
    with transaction(pg.app.state.engine) as db:
        assert db.get(m.RequestRecord, row['id']).decision_reason == ''
        assert db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == 'request.decide',
            m.AuditEvent.target_id == row['id'])).reason == ''
