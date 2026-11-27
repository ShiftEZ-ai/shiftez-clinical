"""A rota's status is another rota's input only inside that reader's wish-budget window (#1171)."""
import pytest

from apps.api import models as m
from apps.api.db import transaction
from tests.test_staff_wishes import wish
from tests.test_standing_wishes import configure, override, standing

DATABASES = ['h', pytest.param('pg', marks=pytest.mark.postgres)]


def input_revision(h):
    with transaction(h.app.state.engine) as db:
        return db.get(m.Organisation, h.seed['organisation_id']).input_revision


def month(h, name, start, end, day):
    return h.shift(h.workspace(name, start, end), start=f'{day}T08:00:00+03:00', end=f'{day}T12:00:00+03:00')['workspace']


def attention(h, role):
    return {item['key']: item for item in h.get(role, '/api/home')['attention']}


@pytest.mark.parametrize('database', DATABASES)
def test_later_month_submit_keeps_an_earlier_submitted_rota_current(database, request):
    h = request.getfixturevalue(database)
    first = month(h, 'Month A', '2026-10-01', '2026-11-01', '2026-10-05')
    later = month(h, 'Month B', '2026-11-01', '2026-12-01', '2026-11-09')
    first = h.lifecycle(first, 'submit')['workspace']
    before, revision = h.snapshot(first)['fingerprint'], input_revision(h)
    assert attention(h, 'approver')['reviews']['count'] == 1

    later = h.lifecycle(later, 'submit')['workspace']

    # Month A's wish-budget window ends where month B starts: nothing month A reads changed.
    assert input_revision(h) == revision
    detail = h.detail(first)
    assert h.snapshot(first)['fingerprint'] == before == detail['workspace']['submitted_fingerprint'] == detail['current_fingerprint']
    assert attention(h, 'approver')['reviews']['count'] == 2
    assert 'resubmit' not in attention(h, 'coordinator')
    h.lifecycle(later, 'approve', 'approver')
    assert input_revision(h) == revision
    assert 'resubmit' not in attention(h, 'coordinator')


@pytest.mark.parametrize('database', DATABASES)
def test_submit_inside_a_reader_window_still_marks_the_reader_out_of_date(database, request):
    """Budget 10 a month. Submitting the first half reserves 3 + 3 standing points and a 4-point one-off, so the
    amendment of the published second half no longer funds 19 Oct: a real input change of that draft."""
    h = request.getfixturevalue(database)
    configure(h, wish_period='month', wish_budget_points=10)
    other = h.seed['people'][1]
    wish(h, '2026-10-07', points=4)
    saved = standing(h, points=3)['wish']
    base = h.shift(h.workspace('Published second half', '2026-10-15', '2026-11-01'), people=[other],
                   start='2026-10-19T08:00:00+03:00', end='2026-10-19T12:00:00+03:00')['workspace']
    override(h, saved, workspace_id=base['id'], skip=True)
    draft = h.workspace('Amend second half', '2026-10-15', '2026-11-01', base=h.publish(base)['publication_id'])
    first = h.shift(h.workspace('First half', '2026-10-01', '2026-10-15'), people=[other],
                    start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')['workspace']
    before, revision = h.snapshot(draft), input_revision(h)
    assert [w['first_day'] for w in before['wishes'] if w.get('standing_wish_id')] == ['2026-10-19']

    h.lifecycle(first, 'submit')

    after = h.snapshot(draft)
    assert [w['first_day'] for w in after['wishes'] if w.get('standing_wish_id')] == []
    assert after['fingerprint'] != before['fingerprint']
    assert input_revision(h) == revision + 1


@pytest.mark.parametrize('database', DATABASES)
def test_a_frozen_longer_wish_period_still_reads_the_later_month(database, request):
    """Month A's baseline freezes quarter budgets, so its window reaches into month B after the department
    switches to monthly budgets."""
    h = request.getfixturevalue(database)
    configure(h, wish_period='quarter', wish_budget_points=10)
    first = month(h, 'Month A', '2026-10-01', '2026-11-01', '2026-10-05')
    later = month(h, 'Month B', '2026-11-01', '2026-12-01', '2026-11-09')
    configure(h, wish_period='month', wish_budget_points=10)
    h.lifecycle(h.detail(first)['workspace'], 'submit')
    revision = input_revision(h)

    h.lifecycle(h.detail(later)['workspace'], 'submit')

    assert input_revision(h) == revision + 1


@pytest.mark.parametrize('database', DATABASES)
def test_unrelated_draft_lifecycle_keeps_a_review_current(database, request):
    h = request.getfixturevalue(database)
    first = month(h, 'First month', '2026-10-01', '2026-11-01', '2026-10-05')
    first = h.lifecycle(first, 'submit')['workspace']
    before, revision = h.snapshot(first)['fingerprint'], input_revision(h)

    later = month(h, 'Later month', '2027-02-01', '2027-03-01', '2027-02-09')
    assert input_revision(h) == revision
    assert h.snapshot(first)['fingerprint'] == before
    assert 'resubmit' not in attention(h, 'coordinator')

    later = h.lifecycle(later, 'archive')['workspace']
    assert input_revision(h) == revision
    assert h.snapshot(first)['fingerprint'] == before
    assert 'resubmit' not in attention(h, 'coordinator')

    later = h.lifecycle(later, 'restore')['workspace']
    h.publish(later)
    assert input_revision(h) == revision
    assert h.snapshot(first)['fingerprint'] == before
    assert 'resubmit' not in attention(h, 'coordinator')


@pytest.mark.parametrize('database', DATABASES)
def test_a_publication_inside_the_snapshot_window_invalidates_its_reader(database, request):
    h = request.getfixturevalue(database)
    first = month(h, 'First month', '2026-10-01', '2026-11-01', '2026-10-05')
    first = h.lifecycle(first, 'submit')['workspace']
    later = month(h, 'Next month', '2026-11-01', '2026-12-01', '2026-11-09')
    before, revision = h.snapshot(first)['fingerprint'], input_revision(h)

    h.publish(later)

    assert input_revision(h) > revision
    assert h.snapshot(first)['fingerprint'] != before
    assert 'resubmit' in attention(h, 'coordinator')
