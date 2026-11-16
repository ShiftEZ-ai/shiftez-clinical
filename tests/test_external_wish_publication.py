"""Frozen wish outcomes retain the official work used by the final check and later trimming."""
import copy
from datetime import date

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from apps.api.services.schedule import in_force_snapshot
from packages.domain.patterns import wish_outcomes
from packages.domain.validation import evaluate
from tests.test_staff_wishes import wish


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('kind,settings,own_day,before,after,work_days,before_points,after_points', [
    pytest.param('prefer_off', {}, 6, 'partly', 'declined', [4], (1, 1), (0, 2), id='dated'),
    pytest.param('max_days_in_row', {'limit': 1}, 5, 'declined', 'granted', [4, 5], (0, 2), (2, 0), id='pattern'),
])
def test_publication_and_replacement_keep_external_official_wish_work(
        database, request, kind, settings, own_day, before, after, work_days, before_points, after_points):
    h = request.getfixturevalue(database)
    person = h.seed['people'][0]
    saved = wish(h, '2026-10-04', last='2026-10-05', kind=kind, points=2, **settings)['wish']
    source = h.shift(h.workspace('External official source'), people=[person])['workspace']
    source_shifts = h.detail(source)['shifts']
    source_validation = evaluate(h.snapshot(source))
    # Synthetic official work in another department; its immutable evidence predates wish capture.
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed['organisation_id'], name='External official department')
        db.add(department)
        db.flush()
        workspace = db.get(m.Workspace, source['id'])
        workspace.department_id, workspace.status = department.id, 'published'
        db.add(m.Publication(department_id=department.id, workspace_id=workspace.id, sequence=1,
            starts_on=workspace.starts_on, ends_on=workspace.ends_on,
            published_by=h.seed['identities']['coordinator'], fingerprint='0' * 64,
            snapshot={'workspace': record(workspace), 'shifts': source_shifts, 'timezone': 'Asia/Riyadh'},
            validation=source_validation))
    start, end = f'2026-10-{own_day:02}T08:00:00+03:00', f'2026-10-{own_day:02}T12:00:00+03:00'
    candidate = h.shift(h.workspace('Checked official context'), start=start, end=end, people=[person])['workspace']
    assert wish_outcomes(h.snapshot(candidate))[saved['id']] == (before_points[0] / 2, 2)
    publication_id = h.publish(candidate)['publication_id']
    staff_snapshot = h.get('staff', '/api/publications/' + publication_id)['snapshot']
    assert 'wish_worked' not in staff_snapshot
    assert staff_snapshot['wishes'][0]['outcome'] == before
    with transaction(h.app.state.engine) as db:
        publication = db.get(m.Publication, publication_id)
        original = copy.deepcopy(publication.snapshot)
        [frozen] = original['wishes']
        assert frozen['outcome'] == before
        assert original['wish_worked'] == {person: {f'2026-10-{day:02}': [h.seed['type']] for day in work_days}}
        assert db.get(m.StaffWish, saved['id']).outcome == before
        ledger = {row.metric: row.value for row in db.scalars(select(m.WishLedger).where(
            m.WishLedger.publication_id == publication_id, m.WishLedger.person_id == person))}
        assert ledger == {'wishes_submitted': 2, 'wishes_granted': before_points[0], 'wishes_declined': before_points[1]}
    replacement = h.command('coordinator', 'workspace.create', {'name': 'Replace after external duty',
        'starts_on': '2026-10-05', 'ends_on': '2026-10-11', 'replaces_publication_id': publication_id})['workspace']
    replacement = h.shift(replacement, start=start, end=end, people=[person])['workspace']
    h.publish(replacement)
    with transaction(h.app.state.engine) as db:
        publication = db.get(m.Publication, publication_id)
        assert publication.effective_until == date(2026, 10, 5)
        assert publication.snapshot == original
        in_force = in_force_snapshot(db, publication)
        [trimmed] = in_force['wishes']
        assert trimmed['outcome'] == after
        assert in_force['wish_worked'] == {person: {'2026-10-04': [h.seed['type']]}}
        ledger = {row.metric: row.value for row in db.scalars(select(m.WishLedger).where(
            m.WishLedger.publication_id == publication_id, m.WishLedger.person_id == person))}
        assert ledger == {'wishes_submitted': 2, 'wishes_granted': after_points[0], 'wishes_declined': after_points[1]}
