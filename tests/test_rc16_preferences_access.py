"""Standing-first allocation, default-point admission, historical reads and date-set read authority."""
import pytest
from sqlalchemy import event, select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from .test_date_sets_api import date_set, value
from .test_staff_wishes import department_settings
from .test_standing_wishes import configure, expanded, standing
from .test_wish_settings import payload, save_settings


def test_default_points_must_fit_saved_budget_and_budget_cuts_validate_existing_defaults(h):
    before = h.get('admin', '/api/me')
    refused = h.command('admin', 'department.settings', {**department_settings(h),
        'wish_settings': {'types': {'prefer_off': {'default_points': 11}}}}, h.seed['department_id'],
        before['organisation']['revision'], status=422)['error']
    assert refused['code'] == 'WISH_DEFAULT_POINTS'
    assert '(11)' in refused['message'] and '(10)' in refused['message']
    assert h.get('admin', '/api/me')['department'] == before['department']

    save_settings(h, {'prefer_off': {'default_points': 8}})
    current = h.get('admin', '/api/me')
    refused = h.command('admin', 'department.settings', {**department_settings(h),
        'generation_defaults': {'scoring': {'wish_budget_points': 7}}}, h.seed['department_id'],
        current['organisation']['revision'], status=422)['error']
    assert refused['code'] == 'WISH_DEFAULT_POINTS'
    assert h.get('admin', '/api/me')['department'] == current['department']
    # A zero budget disables preferences while retaining defaults for the next time they are enabled.
    configure(h, wish_budget_points=0)
    assert h.get('staff', '/api/wishes')['budget_points'] == 0
    save_settings(h, {'prefer_off': {'default_points': 8}},
                  generation_defaults={'scoring': {'wish_budget_points': 8}})
    assert h.command('staff', 'wish.create', payload(h))['wish']['points'] == 8


@pytest.mark.parametrize('repeat', [None, {'weekdays': [0], 'starts_on': '2026-10-01'}])
def test_create_reports_an_invalid_legacy_default_before_budget_allocation(h, repeat):
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).wish_settings = {
            'types': {'prefer_off': {'default_points': 11}}}
    refused = h.command('staff', 'wish.create', payload(h, repeat=repeat), status=422)['error']
    assert refused['code'] == 'WISH_DEFAULT_POINTS'
    assert 'Default points' in refused['message'] and 'budget (10)' in refused['message']
    assert h.get('staff', '/api/wishes')['wishes'] == []
    assert h.command('staff', 'wish.create', payload(h, points=2, repeat=repeat))['wish']['points'] == 2


def test_one_off_creation_and_update_require_budget_after_standing(h):
    one_off = h.command('staff', 'wish.create', payload(h, day='2026-10-06', points=2))['wish']
    standing(h, points=2)
    workspace = h.workspace(start='2026-10-01', end='2026-11-01')
    for kind, target, revision, day in [('wish.create', None, None, '2026-10-07'),
                                         ('wish.update', one_off['id'], one_off['revision'], '2026-10-06')]:
        refused = h.command('staff', kind, payload(h, day=day, points=3), target, revision, status=409)['error']
        assert refused['code'] == 'WISH_BUDGET'
        assert refused['details']['used'] == (10 if kind == 'wish.create' else 8)
    concrete = expanded(h, workspace)
    assert len(concrete) == 5
    assert next(w for w in concrete if w['id'] == one_off['id'])['points'] == 2
    assert len(h.get('staff', '/api/wishes')['wishes']) == 2


def test_budget_cut_reports_existing_live_one_off_as_unfunded_without_changing_it(h):
    saved = h.command('staff', 'wish.create', payload(h, day='2026-10-06', points=3))['wish']
    workspace = h.workspace(start='2026-10-01', end='2026-11-01')
    configure(h, wish_budget_points=2)
    assert expanded(h, workspace) == []
    listed = h.get('staff', '/api/wishes?start=2026-10-01&end=2026-11-01')
    assert listed['usage'] == [{'start': '2026-10-01', 'end': '2026-11-01', 'used': 0,
        'standing_points': 0, 'one_off_points': 0, 'unfunded_standing_points': 0, 'unfunded_one_off_points': 3}]
    assert listed['wishes'] == [{**saved, 'locked': False}]


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('status', ['submitted', 'approved'])
def test_settled_one_off_reserves_budget_without_standing_preferences(request, database, status):
    h = request.getfixturevalue(database)
    saved = h.command('staff', 'wish.create', payload(h, day='2026-10-04', last_day='2026-10-17', points=3))['wish']
    settled = h.shift(h.workspace(), people=[h.seed['people'][1]])['workspace']
    settled = h.lifecycle(settled, 'submit')['workspace']
    if status == 'approved':
        settled = h.lifecycle(settled, 'approve', role='approver')['workspace']
    frozen = h.detail(settled)['workspace']['validation']
    configure(h, wish_budget_points=2)
    adjacent = h.workspace('Adjacent open rota', '2026-10-11', '2026-10-18')
    [current] = expanded(h, adjacent)
    assert current['id'] == saved['id'] and current['points'] == 3 and current['weight'] == 1
    [usage] = h.get('staff', '/api/wishes?start=2026-10-11&end=2026-10-18')['usage']
    assert usage['used'] == usage['one_off_points'] == 3 and usage['unfunded_one_off_points'] == 0
    assert h.detail(settled)['workspace']['validation'] == frozen
    if status == 'approved':
        refused = h.lifecycle(settled, 'reopen', role='approver', status=409)
        assert refused['error']['code'] == 'WORKFLOW_STATE'
        assert [wish['id'] for wish in expanded(h, adjacent)] == [saved['id']]
    else:
        h.lifecycle(settled, 'reopen')
        assert expanded(h, adjacent) == []


@pytest.mark.parametrize('legacy', [False, True])
def test_fairness_reads_live_wish_rows_only_for_legacy_publications(h, monkeypatch, legacy):
    from apps.api.services import planning

    wish = h.command('staff', 'wish.create', payload(h, day='2026-10-06', points=2))['wish']
    workspace = h.shift(h.workspace(), start='2026-10-06T08:00:00+03:00',
                        end='2026-10-06T12:00:00+03:00')['workspace']
    publication_snapshot = planning.publication_snapshot
    def snapshot(*args, **kwargs):
        result = publication_snapshot(*args, **kwargs)
        return {key: value for key, value in result.items() if key != 'wishes'} if legacy else result
    monkeypatch.setattr(planning, 'publication_snapshot', snapshot)
    h.publish(workspace)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.StaffWish, wish['id']).points = 8
    reads = []
    def count(conn, cursor, statement, parameters, context, executemany):
        if 'from staff_wishes' in ' '.join(statement.lower().split()):
            reads.append(statement)
    event.listen(h.app.state.engine, 'before_cursor_execute', count)
    try:
        detail = h.get('coordinator', f'/api/fairness/sessions?person_id={wish["person_id"]}'
            '&metric=wishes_declined&starts_on=2026-10-01&ends_on=2026-11-01')
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', count)
    assert len(reads) == int(legacy)
    [publication] = detail['publications']
    [historical] = publication['wishes']
    assert historical['id'] == wish['id'] and historical['points'] == (8 if legacy else 2)


def test_coordinator_can_read_inactive_date_sets_and_history_but_cannot_edit(h):
    inactive = date_set(h, active=False)
    assert s.PERMISSIONS['date_sets.read_all'] == ('admin', 'coordinator')
    assert h.get('coordinator', '/api/date-sets')['date_sets'] == []
    [listed] = h.get('coordinator', '/api/date-sets?include_inactive=true')['date_sets']
    assert s.DateSetRecord.model_validate(listed) == s.DateSetRecord.model_validate(inactive)
    resolved = h.get('coordinator', '/api/date-sets/resolved?start=2026-10-04&end=2026-10-05&include_inactive=true')
    assert [entry['set_id'] for entry in resolved] == [inactive['id']]
    assert len(h.get('coordinator', f'/api/catalogues/date_sets/{inactive["id"]}/history')['versions']) == 1
    h.configure('date_sets', value(inactive, active=True), inactive['id'], inactive['revision'], role='coordinator', status=403)
    h.command('coordinator', 'date_set.entry.delete', {'entry_id': inactive['entries'][0]['id'],
        'reason': 'Read authority is not edit authority'}, inactive['id'], inactive['revision'], status=403)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.DateSet, inactive['id']).active is False
        assert len(list(db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.object_id == inactive['id'])))) == 1
