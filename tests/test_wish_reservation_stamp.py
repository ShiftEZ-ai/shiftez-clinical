"""A settled rota's stored wish reservation is a scheduling input of every other rota that reads it."""
import pytest
from sqlalchemy import update

from apps.api import models as m
from apps.api.db import transaction
from tests.test_staff_wishes import wish
from tests.test_standing_wishes import configure, override, standing

DATABASES = ['h', pytest.param('pg', marks=pytest.mark.postgres)]
PATHS = ['orm', 'session_dml']


def stored(h, workspace):
    with transaction(h.app.state.engine) as db:
        return db.get(m.Workspace, workspace['id']).validation


def input_revision(h):
    with transaction(h.app.state.engine) as db:
        return db.get(m.Organisation, h.seed['organisation_id']).input_revision


def save_check(h, workspace, validation, path):
    with transaction(h.app.state.engine, write=True) as db:
        if path == 'orm':
            db.get(m.Workspace, workspace['id']).validation = validation
        else:
            db.execute(update(m.Workspace).where(m.Workspace.id == workspace['id']).values(validation=validation))


def standing_days(snapshot):
    return sorted(w['first_day'] for w in snapshot['wishes'] if w.get('standing_wish_id'))


def scene(h, budget=10):
    """Budget 10 a month. The settled first half reserves 3 + 3 standing points and a 4-point one-off.
    The amendment skips its base publication's reservation, so without the settled reservation it funds 19 Oct."""
    configure(h, wish_period='month', wish_budget_points=budget)
    other = h.seed['people'][1]
    wish(h, '2026-10-07', points=4)
    saved = standing(h, points=3)['wish']
    base = h.shift(h.workspace('Published second half', '2026-10-15', '2026-11-01'), people=[other],
                   start='2026-10-19T08:00:00+03:00', end='2026-10-19T12:00:00+03:00')['workspace']
    override(h, saved, workspace_id=base['id'], skip=True)
    publication_id = h.publish(base)['publication_id']
    draft = h.workspace('Amend second half', '2026-10-15', '2026-11-01', base=publication_id)
    settled = h.shift(h.workspace('Settled first half', '2026-10-01', '2026-10-15'), people=[other],
                      start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')['workspace']
    settled = h.lifecycle(settled, 'submit')['workspace']
    return settled, draft


@pytest.mark.parametrize('path', PATHS)
@pytest.mark.parametrize('database', DATABASES)
def test_changed_settled_reservation_advances_the_input_revision(database, path, request):
    h = request.getfixturevalue(database)
    settled, draft = scene(h)
    checked = stored(h, settled)
    assert sorted(w['first_day'] for w in checked['wishes']) == ['2026-10-05', '2026-10-07', '2026-10-12']
    assert standing_days(h.snapshot(draft)) == []
    # An older check without a recorded reservation: the reader expands the settled days live.
    save_check(h, settled, {key: value for key, value in checked.items() if key != 'wishes'}, path)
    before = h.snapshot(draft)
    assert standing_days(before) == ['2026-10-19']

    if path == 'orm':
        h.lifecycle(h.detail(settled)['workspace'], 'validate')
        assert sorted(w['id'] for w in stored(h, settled)['wishes']) == sorted(w['id'] for w in checked['wishes'])
    else:
        save_check(h, settled, checked, path)
    after = h.snapshot(draft)

    assert standing_days(after) == []
    assert after['fingerprint'] != before['fingerprint']
    assert after['organisation']['input_revision'] == before['organisation']['input_revision'] + 1


@pytest.mark.parametrize('path', PATHS)
@pytest.mark.parametrize('database', DATABASES)
def test_unchanged_settled_reservation_keeps_the_input_revision(database, path, request):
    h = request.getfixturevalue(database)
    settled, draft = scene(h)
    checked = stored(h, settled)
    before, revision = h.snapshot(draft), input_revision(h)

    if path == 'orm':
        h.lifecycle(h.detail(settled)['workspace'], 'validate')
        rechecked = stored(h, settled)
        assert rechecked['evaluated_at'] != checked['evaluated_at']
        assert sorted(w['id'] for w in rechecked['wishes']) == sorted(w['id'] for w in checked['wishes'])
    else:
        # Same reservation in another order, with other check fields changed.
        save_check(h, settled, {**checked, 'evaluated_at': 'later', 'wishes': checked['wishes'][::-1]}, path)

    assert input_revision(h) == revision
    assert h.snapshot(draft)['fingerprint'] == before['fingerprint']


@pytest.mark.parametrize('path', PATHS)
@pytest.mark.parametrize('database', DATABASES)
def test_draft_check_save_keeps_the_input_revision(database, path, request):
    h = request.getfixturevalue(database)
    settled, draft = scene(h)
    neighbour = h.shift(h.workspace('Open first half', '2026-10-01', '2026-10-15'), people=[h.seed['people'][1]],
                        start='2026-10-06T08:00:00+03:00', end='2026-10-06T12:00:00+03:00')['workspace']
    before, revision = h.snapshot(draft), input_revision(h)
    checked = stored(h, neighbour)

    if path == 'orm':
        h.lifecycle(h.detail(neighbour)['workspace'], 'validate')
        assert stored(h, neighbour) != checked
    # Even a draft check carrying a wishes list is never read as a reservation.
    save_check(h, neighbour, {**(stored(h, neighbour) or {}), 'wishes': stored(h, settled)['wishes']}, path)

    assert input_revision(h) == revision
    assert h.snapshot(draft)['fingerprint'] == before['fingerprint']


@pytest.mark.parametrize('database', DATABASES)
def test_budgets_off_recheck_keeps_the_input_revision(database, request):
    h = request.getfixturevalue(database)
    configure(h, wish_period='month', wish_budget_points=10)
    standing(h, points=3)
    configure(h, wish_period='month', wish_budget_points=0)
    settled = h.shift(h.workspace('Settled first half', '2026-10-01', '2026-10-15'), people=[h.seed['people'][1]],
                      start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')['workspace']
    settled = h.lifecycle(settled, 'submit')['workspace']
    draft = h.workspace('Open second half', '2026-10-15', '2026-11-01')
    assert stored(h, settled)['wishes'] == []
    before, revision = h.snapshot(draft), input_revision(h)

    h.lifecycle(h.detail(settled)['workspace'], 'validate')

    assert stored(h, settled)['wishes'] == []
    assert input_revision(h) == revision
    after = h.snapshot(draft)
    assert after['wishes'] == [] and after['fingerprint'] == before['fingerprint']
