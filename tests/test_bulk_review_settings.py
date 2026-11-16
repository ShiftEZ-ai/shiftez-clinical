"""Bulk limits use the same effective values and validation at every settings boundary."""
from dataclasses import replace

import pytest
from pydantic import ValidationError

from apps.api import models as m
from apps.api.db import transaction
from apps.api.main import create_app
from apps.api.settings import Settings
from packages.contracts import schemas as s


@pytest.mark.parametrize('overrides', [{}, {'bulk_staged_days': 28}])
def test_department_settings_undo_restores_effective_deployment_bulk_limits(h, overrides):
    deployment = {'bulk_max_items': 350, 'bulk_direct_max': 25, 'bulk_staged_days': 21,
                  'rate_bulk_preview_per_minute': 60}
    h.settings = replace(h.settings, **deployment)
    h.app = create_app(h.settings, engine=h.app.state.engine)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).bulk_settings = overrides
    before = h.get('admin', '/api/me')
    department, organisation = before['department'], before['organisation']
    effective = {**deployment, **overrides}
    assert h.get('admin', '/api/bulk/settings')['settings'] == effective
    saved = h.command('admin', 'department.settings', {
        'department_name': department['name'], 'department_revision': department['revision'],
        'organisation_name': organisation['name'], 'timezone': organisation['timezone'],
        'week_start': organisation['week_start'], 'reason': 'Adjust inherited bulk limits',
        'bulk_settings': {'bulk_max_items': 100, 'bulk_direct_max': 10, 'bulk_staged_days': 7,
                          'rate_bulk_preview_per_minute': 5}},
        department['id'], organisation['revision'])
    assert saved['previous']['bulk_settings'] == effective
    h.command('admin', 'department.settings', {**saved['previous'], 'reason': 'Undo bulk settings'},
              department['id'], saved['organisation']['revision'])
    assert h.get('admin', '/api/bulk/settings')['settings'] == effective


@pytest.mark.parametrize('values', [
    {'bulk_max_items': 5000, 'bulk_direct_max': 3000},
    {'bulk_max_items': 0}, {'bulk_max_items': 5001},
    {'bulk_direct_max': -1}, {'bulk_direct_max': 2001},
    {'bulk_max_items': 20, 'bulk_direct_max': 21},
    {'bulk_staged_days': 0}, {'bulk_staged_days': 366},
    {'rate_bulk_preview_per_minute': -1}, {'rate_bulk_preview_per_minute': 10001},
    {'bulk_max_items': True}, {'bulk_direct_max': 1.5},
])
def test_startup_rejects_bulk_limits_with_the_shared_schema(values):
    with pytest.raises(ValidationError) as invalid:
        s.BulkSettings.model_validate(values)
    with pytest.raises(RuntimeError) as startup:
        create_app(replace(Settings(environment='test'), **values))
    assert str(startup.value) == 'Invalid bulk limits: ' + invalid.value.errors()[0]['msg']


@pytest.mark.parametrize('values', [
    {'bulk_max_items': 1, 'bulk_direct_max': 0, 'bulk_staged_days': 1,
     'rate_bulk_preview_per_minute': 0},
    {'bulk_max_items': 5000, 'bulk_direct_max': 2000, 'bulk_staged_days': 365,
     'rate_bulk_preview_per_minute': 10000},
])
def test_startup_accepts_shared_bulk_limit_boundaries(values):
    replace(Settings(environment='test'), **values).check()
