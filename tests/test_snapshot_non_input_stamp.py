"""Non-input exclusions must agree with the full, uncached input fingerprint."""
import pytest
from sqlalchemy import update

from apps.api import models as m, snapshots
from apps.api.db import transaction
from tests.governance_support import developer
from tests.test_fix_list_performance import backend


def input_state(h, workspace):
    # A fresh session rebuilds the full fingerprint without consulting stamp-based read projections.
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, workspace['id'])
        return snapshots.fingerprint(db, row), snapshots.currency_hint(db, row)


@pytest.mark.parametrize('enabled', [True, False])
@pytest.mark.parametrize('writer', ['orm', 'statement', 'core', 'bulk', 'settings', 'settings_review'])
def test_score_check_toggle_matches_full_fingerprint_oracle(backend, enabled, writer):
    h = backend
    actor = developer(h) if writer.startswith('settings') else None
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).score_check_enabled = not enabled
    workspace = h.shift(h.workspace('Non-input setting oracle'))['workspace']
    if writer == 'settings_review':
        workspace = h.lifecycle(workspace, 'submit')['workspace']
    before_fingerprint, before_hint = input_state(h, workspace)

    if writer.startswith('settings'):
        me = h.get('admin', '/api/me')
        org, department = me['organisation'], me['department']
        saved = h.command(actor, 'department.settings', {
            'department_name': department['name'], 'department_revision': department['revision'],
            'organisation_name': org['name'], 'timezone': org['timezone'], 'week_start': org['week_start'],
            'score_check_enabled': enabled, 'reason': 'Synthetic non-input toggle',
        }, department['id'], org['revision'])
        assert saved['department']['score_check_enabled'] is enabled
        assert saved['previous']['score_check_enabled'] is not enabled
    else:
        with transaction(h.app.state.engine, write=True) as db:
            department = db.get(m.Department, h.seed['department_id'])
            if writer == 'orm':
                department.score_check_enabled = enabled
                department.revision += 1
            elif writer == 'bulk':
                db.execute(update(m.Department), [{'id': department.id, 'score_check_enabled': enabled,
                                                  'revision': department.revision + 1}])
            else:
                target = m.Department.__table__ if writer == 'core' else m.Department
                key = target.c.id if writer == 'core' else target.id
                db.execute(update(target).where(key == department.id).values(
                    score_check_enabled=enabled, revision=department.revision + 1))
            db.flush()
            db.refresh(department)
            assert department.score_check_enabled is enabled

    after_fingerprint, after_hint = input_state(h, workspace)
    assert after_fingerprint == before_fingerprint
    assert (after_hint == before_hint) == (after_fingerprint == before_fingerprint)
    if writer == 'settings_review':
        assert h.detail(workspace)['current_fingerprint'] == before_fingerprint

    # A real safety change must still move both the full fingerprint and its stamp.
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, h.seed['people'][0]).active = False
    blocking_fingerprint, blocking_hint = input_state(h, workspace)
    assert blocking_fingerprint != after_fingerprint
    assert blocking_hint['input_revision'] == after_hint['input_revision'] + 1
    assert (blocking_hint == after_hint) == (blocking_fingerprint == after_fingerprint)


@pytest.mark.parametrize('writer', ['orm', 'statement', 'core', 'bulk'])
def test_score_check_exclusion_keeps_unproved_department_changes_stamped(backend, writer):
    h = backend
    with transaction(h.app.state.engine, write=True) as db:
        org = db.get(m.Organisation, h.seed['organisation_id'])
        department = db.get(m.Department, h.seed['department_id'])
        before = org.input_revision, org.availability_revision
        changes = {'score_check_enabled': not department.score_check_enabled,
                   'event_horizon_days': department.event_horizon_days + 1}
        if writer == 'orm':
            for key, value in changes.items():
                setattr(department, key, value)
        elif writer == 'bulk':
            db.execute(update(m.Department), [{'id': department.id, **changes}])
        else:
            target = m.Department.__table__ if writer == 'core' else m.Department
            key = target.c.id if writer == 'core' else target.id
            db.execute(update(target).where(key == department.id).values(**changes))
        db.flush()
        assert (org.input_revision, org.availability_revision) == (before[0] + 1, before[1])
