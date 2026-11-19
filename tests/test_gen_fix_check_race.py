"""Background check writes cannot make a user's first edit fail preparation."""
import pytest
from sqlalchemy import event, select
from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import planning

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('update', [False, True])
def test_stored_background_check_retries_preparation_and_saves_once(pg, monkeypatch, update):
    w = pg.shift(pg.workspace())['workspace'] if update else pg.workspace()
    row = pg.detail(w)['shifts'][0] if update else None
    original, original_changed, fired, conflicts = planning.draft, planning.changed, [], []

    def store_check():
        fired.append(True)
        with transaction(pg.app.state.engine, write=True) as other:
            planning.revalidate(other, other.get(m.Workspace, w['id']))

    def concurrent_check(db, actor, workspace_id, revision):
        workspace = original(db, actor, workspace_id, revision)
        if not update and db.info.get('_planning_preparation') and not fired:
            store_check()
        return workspace

    def concurrent_edit_check(db, actor, workspace, *args, **kwargs):
        # D65's earlier calculation rolls back before changed(); race the actual write pass.
        if update and db.info.get('_planning_preparation') and not fired:
            store_check()
        return original_changed(db, actor, workspace, *args, **kwargs)

    def error(context):
        if getattr(context.original_exception, 'sqlstate', None) == '40001':
            conflicts.append(True)

    monkeypatch.setattr(planning, 'draft', concurrent_check)
    monkeypatch.setattr(planning, 'changed', concurrent_edit_check)
    event.listen(pg.app.state.engine, 'handle_error', error)
    try:
        if update:
            payload = {key: row[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')}
            payload.update(workspace_id=w['id'], shift_id=row['id'], participants=[
                {'person_id': row['participants'][0]['person_id'], 'start': row['start'],
                 'end': row['end'], 'team_lead': True}])
            saved = pg.command('coordinator', 'shift.update', payload, revision=w['revision'])
        else:
            saved = pg.shift(w)
    finally:
        event.remove(pg.app.state.engine, 'handle_error', error)
    assert fired and conflicts, 'Exercise a real PostgreSQL serialization conflict, not an injected error'
    assert saved['workspace']['revision'] == w['revision'] + 1
    detail = pg.detail(w)
    assert len(detail['shifts']) == 1
    if update:
        assert detail['shifts'][0]['participants'][0]['team_lead']
    with transaction(pg.app.state.engine) as db:
        changes = list(db.scalars(select(m.ChangeSet).where(m.ChangeSet.workspace_id == w['id'])))
        assert len(changes) == (2 if update else 1)


def test_preparation_retry_never_overwrites_a_real_concurrent_edit(pg, monkeypatch):
    w = pg.workspace()
    original, fired = planning.draft, []

    def concurrent_edit(db, actor, workspace_id, revision):
        workspace = original(db, actor, workspace_id, revision)
        if db.info.get('_planning_preparation') and not fired:
            fired.append(True)
            with transaction(pg.app.state.engine, write=True) as other:
                row = other.get(m.Workspace, w['id'])
                row.name = 'Another coordinator changed this'
                row.revision += 1
                row.content_revision += 1
        return workspace

    monkeypatch.setattr(planning, 'draft', concurrent_edit)
    refused = pg.shift(w, status=409)
    assert refused['error']['code'] == 'STALE_REVISION'
    detail = pg.detail(w)
    assert not detail['shifts']
    assert detail['workspace']['name'] == 'Another coordinator changed this'


def test_background_check_finishing_after_computation_does_not_invalidate_edit(pg, monkeypatch):
    w = pg.shift(pg.workspace())['workspace']
    row = pg.detail(w)['shifts'][0]
    original, checked = planning.evaluate, []

    def concurrent_check(data, **options):
        result = original(data, **options)
        if not checked:
            checked.append(True)
            with transaction(pg.app.state.engine, write=True) as db:
                planning.revalidate(db, db.get(m.Workspace, w['id']))
        return result

    monkeypatch.setattr(planning, 'evaluate', concurrent_check)
    payload = {key: row[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')}
    payload.update(workspace_id=w['id'], shift_id=row['id'], participants=[{
        'person_id': row['participants'][0]['person_id'], 'start': row['start'], 'end': row['end'], 'team_lead': True}])
    result = pg.command('coordinator', 'shift.update', payload, revision=w['revision'])
    assert checked and result['workspace']['revision'] == w['revision'] + 1
    assert pg.detail(w)['shifts'][0]['participants'][0]['team_lead']
