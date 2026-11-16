"""The frontend and backend share a bounded timeline and drag payload."""
from uuid import uuid4
from importlib import import_module

import pytest
from pydantic import ValidationError

from packages.contracts.schemas import RotationTypeInput, TimelineRotationInput


def test_rotation_type_leave_policy_defaults_and_rejects_unknown_values():
    assert RotationTypeInput(name='Synthetic rotation', starts_on='2026-01-01').leave_policy == 'not_allowed'
    for policy in ('not_allowed', 'needs_approval', 'allowed'):
        assert RotationTypeInput(name='Synthetic rotation', starts_on='2026-01-01', leave_policy=policy).leave_policy == policy
    with pytest.raises(ValidationError):
        RotationTypeInput(name='Synthetic rotation', starts_on='2026-01-01', leave_policy='sometimes')


def test_rotation_leave_migration_extends_the_previous_single_head():
    migration = import_module('migrations.versions.0112_timeline_leave_policy')
    assert migration.revision == '0112_timeline_leave_policy'
    assert migration.down_revision == '0111_autoschedule_resume'


@pytest.mark.parametrize('action', ['move', 'resize', 'copy'])
def test_drag_requires_source_revision_and_ordered_dates(action):
    value = {'action': action, 'person_id': str(uuid4()), 'starts_on': '2026-10-04',
             'ends_on': '2026-10-06', 'reason': 'Synthetic timeline change'}
    with pytest.raises(ValidationError):
        TimelineRotationInput(**value)
    value.update(rotation_id=str(uuid4()), expected_revision=1)
    assert TimelineRotationInput(**value).action == action
    with pytest.raises(ValidationError):
        TimelineRotationInput(**{**value, 'ends_on': '2026-10-04'})


@pytest.mark.parametrize('action', ['create', 'template'])
def test_new_rotation_requires_definition_and_cohort(action):
    value = {'action': action, 'person_id': str(uuid4()), 'starts_on': '2026-10-04',
             'ends_on': '2026-10-06', 'reason': 'Synthetic timeline change'}
    with pytest.raises(ValidationError):
        TimelineRotationInput(**value)
    value.update(cohort_id=str(uuid4()))
    value['template_id' if action == 'template' else 'rotation_type_id'] = str(uuid4())
    if action == 'template':
        value.pop('ends_on')
    assert TimelineRotationInput(**value).action == action


@pytest.mark.parametrize('extra', [
    {'department_available': None}, {'template_id': str(uuid4())},
    {'rotation_id': str(uuid4()), 'expected_revision': 1},
])
def test_create_rejects_null_flags_and_ambiguous_source_inputs(extra):
    with pytest.raises(ValidationError):
        TimelineRotationInput(action='create', person_id=str(uuid4()), cohort_id=str(uuid4()),
            rotation_type_id=str(uuid4()), starts_on='2026-10-01', ends_on='2026-10-08',
            reason='Synthetic invalid gesture', **extra)


@pytest.mark.parametrize('extra', [{'ends_on': '2026-11-01'}, {'department_available': False},
                                  {'service_id': str(uuid4())}, {'mentor_id': None}])
def test_template_rejects_overrides_that_would_be_ignored(extra):
    with pytest.raises(ValidationError):
        TimelineRotationInput(action='template', person_id=str(uuid4()), cohort_id=str(uuid4()),
            template_id=str(uuid4()), starts_on='2026-10-01', reason='Synthetic template placement', **extra)
