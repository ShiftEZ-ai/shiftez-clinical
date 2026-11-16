import copy
import uuid
import pytest
from sqlalchemy import select
from apps.api import models as m
from apps.api.db import transaction
from apps.api.snapshots import fingerprint
from packages.contracts import schemas as s


def test_extension_settings_preserve_inputs_checkpoint_other_display_and_support_undo(h):
    workspace = h.workspace()
    me = h.get('admin', '/api/me')
    org, dept = me['organisation'], me['department']
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, dept['id'])
        department.display_settings = {'names_default': 'short', 'autoschedule_animation': {'mode': 'roll'}}
        before = copy.deepcopy(department.display_settings)
        current = fingerprint(db, db.get(m.Workspace, workspace['id']))
        job = m.Job(department_id=dept['id'], kind='generation', workspace_id=workspace['id'],
            requested_by=me['identity_id'], status='succeeded', input_fingerprint=current, input_snapshot={},
            resume_state={'retained': True}, resume_checkpoint={'hint': 'retained'})
        db.add(job)
        db.flush()
        job_id = job.id
    payload = {'department_name': dept['name'], 'department_revision': dept['revision'],
        'organisation_name': org['name'], 'timezone': org['timezone'], 'week_start': org['week_start'],
        'generation_controls': {'extension_minutes': 12}, 'reason': 'Set Add time amount'}
    h.command('staff', 'department.settings', payload, dept['id'], org['revision'], status=403)
    h.command('admin', 'department.settings', payload, str(uuid.uuid4()), org['revision'], status=404)
    saved = h.command('admin', 'department.settings', payload, dept['id'], org['revision'])
    assert saved['department']['display_settings'] == {**before, 'generation_controls': {'extension_minutes': 12}}
    assert saved['previous']['generation_controls'] == s.GENERATION_CONTROL_DEFAULTS
    assert saved['organisation'] == org
    h.command('admin', 'department.settings', payload, dept['id'], org['revision'], status=409)
    for controls in ('omit', None):
        older = {k: v for k, v in payload.items() if k != 'generation_controls'}
        older['department_revision'] = saved['department']['revision']
        if controls is None:
            older['generation_controls'] = None
        kept = h.command('admin', 'department.settings', older, dept['id'], org['revision'])
        assert kept['department'] == saved['department']
    # The other shared display command must also retain the run controls.
    displayed = h.command('admin', 'department.display', {'reason': 'Change display only', 'names_default': 'code'},
                          dept['id'], saved['department']['revision'])
    assert displayed['department']['display_settings']['generation_controls'] == {'extension_minutes': 12}
    with transaction(h.app.state.engine) as db:
        assert fingerprint(db, db.get(m.Workspace, workspace['id'])) == current
        stored = db.get(m.Job, job_id)
        assert stored.resume_state == {'retained': True} and stored.resume_checkpoint == {'hint': 'retained'}
        history = db.scalar(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == 'department_settings'))
        assert history.snapshot['display_settings']['generation_controls'] == {'extension_minutes': 12}
    undo_payload = {**saved['previous'], 'department_revision': displayed['department']['revision'],
                    'reason': 'Undo Add time setting'}
    undone = h.command('admin', 'department.settings', undo_payload, dept['id'], org['revision'])
    assert undone['department']['display_settings']['generation_controls'] == s.GENERATION_CONTROL_DEFAULTS


@pytest.mark.parametrize('minutes', [0, s.AUTO_SCHEDULE_LIMITS['maximum_minutes'] + 1, True, 1.5, '5'])
def test_extension_minutes_share_canonical_job_time_validation(minutes):
    with pytest.raises(ValueError):
        s.GenerationControls(extension_minutes=minutes)
    with pytest.raises(ValueError):
        s.JobTimeInput(minutes=minutes, reason='Add time')


def test_extension_minutes_accept_canonical_endpoints():
    for minutes in (s.AUTO_SCHEDULE_LIMITS['minimum_minutes'], s.AUTO_SCHEDULE_LIMITS['maximum_minutes']):
        assert s.GenerationControls(extension_minutes=minutes).extension_minutes == minutes
