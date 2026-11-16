"""Animation defaults use the existing department command, history and Undo payload."""
from copy import deepcopy

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import AutoscheduleAnimationSettings
from .test_staff_wishes import department_settings


def save(h, animation, *, role='admin', status=200):
    me = h.get('admin', '/api/me')
    return h.command(role, 'department.settings', {**department_settings(h), 'autoschedule_animation': animation},
                     me['department']['id'], me['organisation']['revision'], status=status)


def test_animation_defaults_save_reload_audit_and_undo_without_invalidating_rotas(h):
    before = h.get('admin', '/api/me')
    defaults = AutoscheduleAnimationSettings().model_dump(mode='json')
    assert before['department']['autoschedule_animation'] == defaults == {
        'mode': 'hybrid', 'big_move': {'min_row_distance': 4, 'swaps': True, 'fills_from_open': True,
                                     'cross_session': True},
        'big_style': 'fly', 'small_style': 'roll', 'max_concurrent_flights': 16,
        'max_updates_per_second': 3, 'step_change': 'roll', 'speed': 1.0}
    changed = {'mode': 'off', 'big_move': {'min_row_distance': 40, 'swaps': False,
                                         'fills_from_open': False, 'cross_session': False},
               'big_style': 'fade', 'small_style': 'retype', 'max_concurrent_flights': 32,
               'max_updates_per_second': 8, 'step_change': 'retype', 'speed': 2.0}
    saved = save(h, changed)
    assert saved['department']['autoschedule_animation'] == changed
    assert saved['department']['revision'] == before['department']['revision'] + 1
    assert saved['organisation']['input_revision'] == before['organisation']['input_revision']
    assert h.get('coordinator', '/api/me')['department']['autoschedule_animation'] == changed
    assert saved['previous']['autoschedule_animation'] == defaults
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.Department, h.seed['department_id'])
        assert stored.display_settings['autoschedule_animation'] == changed
        history = db.scalar(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == 'department_settings',
            m.DefinitionHistory.object_id == stored.id, m.DefinitionHistory.object_revision == stored.revision))
        assert history.snapshot['autoschedule_animation'] == changed
        assert db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.action == 'department.settings',
            m.AuditEvent.reason == 'Constructed settings change'))
    undone = h.command('admin', 'department.settings', {**saved['previous'], 'reason': 'Undo animation settings'},
                       h.seed['department_id'], saved['organisation']['revision'])
    assert undone['department']['autoschedule_animation'] == defaults
    assert h.get('admin', '/api/me')['department']['autoschedule_animation'] == defaults
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.action == 'department.settings',
            m.AuditEvent.reason == 'Undo animation settings'))


@pytest.mark.parametrize('invalid', [
    {'mode': 'sparkle'}, {'mode': None}, {'big_style': 'retype'}, {'small_style': 'fly'}, {'step_change': 'fade'},
    {'big_move': {'min_row_distance': 0}}, {'big_move': {'min_row_distance': 41}},
    {'big_move': {'min_row_distance': 1.5}}, {'big_move': {'min_row_distance': True}},
    {'big_move': {'min_row_distance': '4'}}, {'big_move': {'swaps': 'true'}},
    {'big_move': {'fills_from_open': 1}}, {'big_move': {'cross_session': None}},
    {'max_concurrent_flights': 3}, {'max_concurrent_flights': 33}, {'max_concurrent_flights': 4.5},
    {'max_updates_per_second': 0}, {'max_updates_per_second': 9}, {'max_updates_per_second': True},
    {'max_updates_per_second': '3'}, {'speed': 0.49}, {'speed': 2.01}, {'speed': True}, {'speed': '1'},
    {'reduced_motion': False}, {'big_move': {'unknown': True}},
])
def test_animation_invalid_settings_are_rejected_atomically(h, invalid):
    before = h.get('admin', '/api/me')['department']
    assert save(h, invalid, status=422)['error']['code'] == 'SCHEMA_INVALID'
    assert h.get('admin', '/api/me')['department'] == before


@pytest.mark.parametrize('mode', ['hybrid', 'fly', 'roll', 'fade', 'retype', 'off'])
def test_animation_contract_accepts_supported_modes_and_lower_bounds(mode):
    value = AutoscheduleAnimationSettings.model_validate({'mode': mode, 'big_style': 'roll', 'small_style': 'fade',
        'big_move': {'min_row_distance': 1}, 'max_concurrent_flights': 4,
        'max_updates_per_second': 1, 'speed': 0.5})
    assert value.mode == mode and value.speed == 0.5


@pytest.mark.parametrize('role', ['staff', 'coordinator'])
def test_animation_settings_changes_require_admin(h, role):
    before = h.get('admin', '/api/me')['department']
    assert save(h, {'mode': 'off'}, role=role, status=403)['error']['code'] == 'FORBIDDEN'
    assert h.get('admin', '/api/me')['department'] == before


def test_animation_and_other_display_settings_preserve_each_other_and_stale_updates_are_refused(h):
    saved = save(h, {'mode': 'roll', 'max_updates_per_second': 7})
    animation = deepcopy(saved['department']['autoschedule_animation'])
    display = h.command('admin', 'department.display', {'names_default': 'short', 'reason': 'Name display'},
                        h.seed['department_id'], saved['department']['revision'])
    assert display['department']['autoschedule_animation'] == animation
    assert 'autoschedule_animation' not in display['previous']
    stale = h.command('admin', 'department.settings', {**department_settings(h),
        'department_revision': saved['department']['revision'], 'autoschedule_animation': {'mode': 'off'}},
        h.seed['department_id'], saved['organisation']['revision'], status=409)
    assert stale['error']['code'] == 'STALE_REVISION'
    me = h.get('admin', '/api/me')
    omitted = h.command('admin', 'department.settings', department_settings(h),
                        h.seed['department_id'], me['organisation']['revision'])
    assert omitted['department']['autoschedule_animation'] == animation
    assert omitted['department']['display_settings']['names_default'] == 'short'
    reset = save(h, {})
    assert reset['department']['autoschedule_animation'] == AutoscheduleAnimationSettings().model_dump(mode='json')
    assert reset['department']['display_settings']['names_default'] == 'short'
