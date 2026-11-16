"""Rank labels travel with visible duty rows; contact permission remains unchanged."""
from datetime import date
import json

import pytest
from sqlalchemy import event, select

from apps.api import models as m
from apps.api.auth import effective_roles
from apps.api.db import transaction
from apps.api.services.people import duty_ranks
from packages.contracts.schemas import ROLE_NAMES, PERMISSIONS
from .governance_support import identity
from .test_home_team_payload import supervision_type


@pytest.mark.postgres
@pytest.mark.parametrize('role', ROLE_NAMES)
def test_every_duty_reader_receives_dated_rank_without_contacts(pg, role):
    h = pg
    me, supervisor, unrelated = h.seed['people']
    identity(h, 'rank_' + role, [role], me)
    # Synthetic dated records exercise a mid-rota promotion and name fallback.
    with transaction(h.app.state.engine, write=True) as db:
        base = db.get(m.Category, h.seed['category'])
        base.short_code = 'R1'
        promoted = m.Category(department_id=h.seed['department_id'], name='Synthetic senior',
                              short_code='', starts_on=date(2026, 10, 5))
        db.add(promoted)
        db.flush()
        membership = db.scalar(select(m.Membership).where(m.Membership.person_id == me,
                                                        m.Membership.category_id == base.id))
        membership.ends_on = date(2026, 10, 5)
        db.add(m.Membership(department_id=h.seed['department_id'], person_id=me,
                            category_id=promoted.id, starts_on=date(2026, 10, 5)))
        db.get(m.Person, supervisor).work_email = 'hidden-team@example.test'
    typ = supervision_type(h)
    w = h.workspace()
    for day in ('04', '05'):
        start, end = f'2026-10-{day}T08:00:00+03:00', f'2026-10-{day}T12:00:00+03:00'
        w = h.shift(w, start=start, end=end, typ=typ, people=[
            {'person_id': me, 'start': start, 'end': end, 'participation': 'trainee', 'supervisor_person_id': supervisor},
            {'person_id': supervisor, 'start': start, 'end': end},
            {'person_id': unrelated, 'start': start, 'end': end}])['workspace']
    pub = h.publish(w)['publication_id']
    for path in ('/api/schedule/sessions?start=2026-10-04&end=2026-10-06', f'/api/publications/{pub}'):
        response = h.client('rank_' + role).get(path)
        allowed = bool(effective_roles([role]) & ({'staff', *PERMISSIONS['schedule.read_team']} if 'schedule/sessions' in path
                          else {'staff', *PERMISSIONS['publications.read_team']}))
        assert response.status_code == (200 if allowed else 403)
        if not allowed:
            continue
        data = response.json().get('snapshot', response.json())
        mine = [p['rank'] for sh in data['shifts'] for p in sh['participants'] if p['person_id'] == me]
        assert mine == ['R1', 'Synthetic senior']
        assert all(p['rank'] == 'R1' for sh in data['shifts'] for p in sh['participants'] if p['person_id'] == supervisor)
        if role == 'staff':
            assert unrelated not in data['people']
        assert 'hidden-team@example.test' not in json.dumps(data)
    if effective_roles([role]) & {'coordinator', 'approver', 'programme_lead', 'auditor'}:
        for path in (f'/api/workspaces/{w["id"]}',
                     f'/api/workspaces/{w["id"]}/parts?part=shifts&start=2026-10-04&end=2026-10-06'):
            data = h.get('rank_' + role, path)
            shifts = data.get('shifts', data.get('items'))
            assert [p['rank'] for sh in shifts for p in sh['participants'] if p['person_id'] == me] == ['R1', 'Synthetic senior']
    # Issued labels survive a later catalogue rename; the publication is never rewritten.
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Category, h.seed['category']).short_code = 'NEW'
    snapshot = h.get('staff', f'/api/publications/{pub}')['snapshot']
    assert snapshot['shifts'][0]['participants'][0]['rank'] == 'R1'


@pytest.mark.postgres
def test_rank_projection_batches_reads_and_skips_already_frozen_labels(pg):
    start = '2026-10-04T08:00:00+03:00'
    rows = [{'id': str(i), 'start': start, 'participants': [{'person_id': pid} for pid in pg.seed['people']]}
            for i in range(150)]
    statements = []
    def executed(conn, cursor, statement, parameters, context, many):
        statements.append(statement)
    with transaction(pg.app.state.engine) as db:
        event.listen(pg.app.state.engine, 'before_cursor_execute', executed)
        try:
            result = duty_ranks(db, pg.seed['department_id'], rows, 'Asia/Riyadh')
            assert all(p['rank'] for sh in result for p in sh['participants'])
            assert duty_ranks(db, pg.seed['department_id'], result, 'Asia/Riyadh') == result
        finally:
            event.remove(pg.app.state.engine, 'before_cursor_execute', executed)
    assert len(statements) <= 2, 'Membership and category reads must be batched, never per duty/person'
