"""Undo restores the original preference under today's admission checks, without replacing its evidence links."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from packages.contracts.schemas import StaffWishInput
from tests.test_staff_wishes import wish
from tests.test_standing_wishes import configure, expanded, override, standing
from tests.test_wish_settings import save_settings


PATTERN_RESTORE_CASES = [
    pytest.param('max_days_in_row', {'limit': 2}, id='days-in-row'),
    pytest.param('min_days_off_together', {'limit': 3}, id='days-off-together'),
    pytest.param('max_weekends_in_row', {'limit': 2, 'weekend_start': 6, 'weekend_days': 3}, id='weekends-in-row'),
    pytest.param('whole_weekends', {'weekend_start': 2, 'weekend_days': 4}, id='whole-weekends'),
]


def pattern_preference(h, kind, settings):
    return wish(h, '2026-10-01', last='2026-12-31', kind=kind, points=3,
                note='Restore pattern ' + kind, **settings)['wish']


def assert_original_values(restored, saved):
    for key in ('id', *StaffWishInput.model_fields, 'created_by', 'created_at'):
        assert restored[key] == saved[key]


def restore(h, row, *, role='staff', status=200, revision=None, payload=None):
    return h.command(role, 'wish.restore', {'reason': 'Undo withdrawal'} if payload is None else payload,
                     row['id'], row['revision'] if revision is None else revision, status=status)


def withdraw(h, row, *, role='staff'):
    return h.command(role, 'wish.withdraw', {'reason': 'Plans changed'}, row['id'], row['revision'])['wish']


def stored_state(h, wish_id):
    with transaction(h.app.state.engine) as db:
        return {'wish': record(db.get(m.StaffWish, wish_id)),
                'overrides': [record(row) for row in db.scalars(select(m.WishOverride).where(
                    m.WishOverride.wish_id == wish_id).order_by(m.WishOverride.id))],
                'input_revision': db.get(m.Organisation, h.seed['organisation_id']).input_revision}


def standing_with_overrides(h):
    """The same real API fixture is used by API and browser Undo journeys."""
    workspace = h.workspace('Standing preference override', '2026-10-01', '2026-11-01')
    saved = standing(h, points=1, note='Every Monday with saved overrides')['wish']
    saved = override(h, saved, period_start='2026-10-01', points=3)['wish']
    saved = override(h, saved, workspace_id=workspace['id'], points=2, number=2)['wish']
    return workspace, saved


def occurrence_signature(h, workspace):
    return [(row['id'], row['first_day'], row['points'], row['standing_wish_id'])
            for row in expanded(h, workspace)]


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_restore_preserves_original_standing_identity_values_and_both_override_links(database, request):
    h = request.getfixturevalue(database)
    workspace, saved = standing_with_overrides(h)
    before = stored_state(h, saved['id'])
    occurrences = occurrence_signature(h, workspace)
    assert [(row[1], row[2]) for row in occurrences] == [('2026-10-05', 2), ('2026-10-12', 2)]
    assert len(before['overrides']) == 2
    assert {row['period_start'] for row in before['overrides']} == {None, '2026-10-01'}
    assert {row['workspace_id'] for row in before['overrides']} == {None, workspace['id']}
    withdrawn = withdraw(h, saved)
    assert expanded(h, workspace) == []
    restored = restore(h, withdrawn)['wish']
    assert restored['id'] == saved['id'] and restored['status'] == 'open'
    assert restored['revision'] == withdrawn['revision'] + 1
    for key in ('person_id', 'kind', 'first_day', 'last_day', 'assignment_type_id', 'points', 'repeat', 'note',
                'created_by', 'created_at'):
        assert restored[key] == saved[key]
    after = stored_state(h, saved['id'])
    assert after['overrides'] == before['overrides']
    assert occurrence_signature(h, workspace) == occurrences
    assert after['input_revision'] > before['input_revision']
    with transaction(h.app.state.engine) as db:
        assert list(db.scalars(select(m.StaffWish.id).where(m.StaffWish.person_id == saved['person_id']))) == [saved['id']]
        [audit] = db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == 'wish.restore',
                                                      m.AuditEvent.target_id == saved['id']))
        assert audit.actor_id == h.seed['identities']['staff'] and audit.reason == 'Undo withdrawal'
        assert audit.detail['payload_fields'] == ['reason']


@pytest.mark.parametrize('kind', ['prefer_off', 'prefer_on', 'prefer_shift_type', 'avoid_shift_type'])
def test_restore_preserves_each_dated_preference_and_duty_type(h, kind):
    typed = {'assignment_type_id': h.seed['type']} if kind.endswith('shift_type') else {}
    saved = wish(h, '2026-10-05', last='2026-10-06', kind=kind, points=3,
                 note='Preserve the original dated request', **typed)['wish']
    restored = restore(h, withdraw(h, saved))['wish']
    for key in ('id', 'person_id', 'kind', 'assignment_type_id', 'first_day', 'last_day', 'points', 'repeat', 'note'):
        assert restored[key] == saved[key]
    assert restored['status'] == 'open' and restored['revision'] == saved['revision'] + 2


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('field,code', [('allowed', 'WISH_KIND_DISABLED'), ('may_repeat', 'WISH_REPEAT_DISABLED')])
def test_restore_rechecks_current_kind_settings_without_replacing_saved_points(database, request, field, code):
    h = request.getfixturevalue(database)
    saved = standing(h, points=3, number=1)['wish']
    withdrawn = withdraw(h, saved)
    save_settings(h, {'prefer_off': {field: False, 'default_points': 8}})
    before = stored_state(h, saved['id'])
    assert restore(h, withdrawn, status=422)['error']['code'] == code
    assert stored_state(h, saved['id']) == before
    save_settings(h, {'prefer_off': {field: True, 'default_points': 8}})
    restored = restore(h, withdrawn)['wish']
    assert restored['id'] == saved['id'] and restored['points'] == 3 and restored['repeat'] == saved['repeat']


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('blocking,code', [
    ('budget', 'WISH_BUDGET'), ('dated_overlap', 'WISH_OVERLAP'), ('standing_overlap', 'WISH_OVERLAP'),
    ('submitted', 'WISH_LOCKED'), ('preferences_off', 'WISHES_OFF'), ('leave', 'WISH_ON_LEAVE'),
])
def test_refused_restore_rolls_back_original_preference_and_inputs(database, request, blocking, code):
    h = request.getfixturevalue(database)
    saved = (standing(h)['wish'] if blocking == 'standing_overlap' else
             wish(h, '2026-10-06', points=6 if blocking == 'budget' else 2)['wish'])
    withdrawn = withdraw(h, saved)
    if blocking == 'budget':
        wish(h, '2026-10-07', points=5)
    elif blocking == 'dated_overlap':
        wish(h, '2026-10-06', kind='prefer_on')
    elif blocking == 'standing_overlap':
        standing(h)
    elif blocking == 'submitted':
        h.lifecycle(h.shift(h.workspace())['workspace'], 'submit')
    elif blocking == 'preferences_off':
        configure(h, wish_budget_points=0)
    else:
        h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0], 'kind': 'recorded_leave',
            'start': '2026-10-06T00:00:00+03:00', 'end': '2026-10-07T00:00:00+03:00',
            'private_reason': 'Synthetic leave after withdrawal'})
    before = stored_state(h, saved['id'])
    assert restore(h, withdrawn, status=409)['error']['code'] == code
    assert stored_state(h, saved['id']) == before
    assert before['wish']['status'] == 'withdrawn'


@pytest.mark.parametrize('blocking,status,code', [
    ('owner', 403, 'FORBIDDEN'), ('role', 403, 'FORBIDDEN'), ('foreign_department', 404, 'NOT_FOUND'),
    ('stale', 409, 'STALE_REVISION'), ('revision_missing', 428, 'REVISION_REQUIRED'),
    ('open', 409, 'WORKFLOW_STATE'), ('payload', 422, 'SCHEMA_INVALID'),
])
def test_restore_requires_ownership_scope_current_revision_state_and_reason_only(h, blocking, status, code):
    other = blocking == 'owner'
    saved = wish(h, '2026-10-06', person=h.seed['people'][1] if other else None,
                 role='coordinator' if other else 'staff', points=2)['wish']
    row = saved if blocking == 'open' else withdraw(h, saved, role='coordinator' if other else 'staff')
    if blocking == 'foreign_department':
        with transaction(h.app.state.engine, write=True) as db:
            department = m.Department(organisation_id=h.seed['organisation_id'], name='Foreign restore scope')
            db.add(department)
            db.flush()
            db.get(m.StaffWish, saved['id']).department_id = department.id
    before = stored_state(h, row['id'])
    role = 'auditor' if blocking == 'role' else 'staff'
    payload = {'reason': 'Undo withdrawal', 'points': 1} if blocking == 'payload' else {'reason': 'Undo withdrawal'}
    revision = None if blocking == 'revision_missing' else saved['revision'] if blocking == 'stale' else row['revision']
    reply = h.command(role, 'wish.restore', payload, row['id'], revision, status=status)
    assert reply['error']['code'] == code
    assert stored_state(h, row['id']) == before
    if blocking == 'owner':
        assert restore(h, row, role='coordinator')['wish']['id'] == saved['id']


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('kind,settings', PATTERN_RESTORE_CASES)
def test_restore_retains_each_pattern_identity_shape_fields_and_period_charges(database, request, kind, settings):
    h = request.getfixturevalue(database)
    saved = pattern_preference(h, kind, settings)
    workspace = h.workspace('Restored multi-period pattern', '2026-10-01', '2027-01-01')
    endpoint = '/api/wishes?start=2026-10-01&end=2027-01-01'
    before = h.get('staff', endpoint)
    withdrawn = withdraw(h, saved)
    assert h.get('staff', endpoint)['wishes'] == []
    restored = restore(h, withdrawn)['wish']
    assert_original_values(restored, saved)
    assert restored['status'] == 'open' and restored['revision'] == withdrawn['revision'] + 1
    after = h.get('staff', endpoint)
    assert [row['id'] for row in after['wishes']] == [saved['id']]
    assert after['usage'] == before['usage']
    assert [row['one_off_points'] for row in after['usage']] == [3, 3, 3]
    [concrete] = expanded(h, workspace)
    for key in ('id', *(key for key in StaffWishInput.model_fields if key != 'note')):
        assert concrete[key] == saved[key]
    assert 'note' not in concrete
    assert concrete['weight'] == 0.3


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('kind,settings', PATTERN_RESTORE_CASES)
@pytest.mark.parametrize('blocking,code', [('later_period_budget', 'WISH_BUDGET'), ('same_pattern_overlap', 'WISH_OVERLAP')])
def test_pattern_restore_rechecks_each_period_budget_and_same_kind_overlap(database, request, kind, settings, blocking, code):
    h = request.getfixturevalue(database)
    saved = pattern_preference(h, kind, settings)
    withdrawn = withdraw(h, saved)
    if blocking == 'later_period_budget':
        wish(h, '2026-11-20', points=8)
    else:
        wish(h, '2026-10-15', last='2026-11-15', kind=kind, points=1, **settings)
    before = stored_state(h, saved['id'])
    refused = restore(h, withdrawn, status=409)['error']
    assert refused['code'] == code
    if blocking == 'later_period_budget':
        assert refused['details'] == {'used': 8, 'budget': 10, 'period_start': '2026-11-01'}
    assert stored_state(h, saved['id']) == before
