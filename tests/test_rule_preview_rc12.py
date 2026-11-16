"""Preview optimization retains complete manifests and notices concurrent source changes."""
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record, json_value
from apps.api.snapshots import snapshot_hash
from apps.api.services import rule_catalogue_v2
from apps.api.auth import authenticate


def actor(db, h):
    return authenticate(db, h.client('coordinator').cookies['rota_session'])


def test_preview_manifest_preserves_every_original_record_field(h):
    h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    h.workspace()
    with transaction(h.app.state.engine) as db:
        current = actor(db, h)
        expected = {cls.__tablename__: snapshot_hash({'rows': [{column.name: json_value(getattr(row, column.name))
            for column in cls.__table__.columns} for row in db.scalars(
            select(cls).where(cls.department_id == current.department_id).order_by(cls.id))]})
            for cls in (m.Policy, m.PolicyException, m.Workspace, m.Publication, m.SchedulingFact, m.RuleSourceBinding, m.PolicySeedLine)}
        assert rule_catalogue_v2._inputs(db, current)['records'] == expected


def test_preview_rejects_concurrent_edit_even_with_cached_source_identity(h, monkeypatch):
    source = h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    h.workspace()

    def proof(db, current, candidate, rotas):
        assert db.get(m.Policy, source['id']).revision == source['revision']
        with transaction(h.app.state.engine, write=True) as other:
            other.get(m.Policy, source['id']).revision += 1
        return {'identical': True}

    monkeypatch.setattr(rule_catalogue_v2, '_proof', proof)
    reply = h.get('coordinator', '/api/rules/catalogue/migration?version=2&proof=true', status=409)
    assert reply['error']['code'] == 'STALE_INPUTS'


def tiny_conversion(h):
    source = h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    workspace = h.workspace('Synthetic freshness regression')
    h.shift(workspace)
    return source, workspace


def test_preview_reloads_cached_organisation_before_compiling_current_proof(h):
    _, workspace = tiny_conversion(h)
    with transaction(h.app.state.engine) as db:
        current = actor(db, h)
        cached = db.get(m.Organisation, current.organisation_id)
        week_start = (cached.week_start + 1) % 7
        with transaction(h.app.state.engine, write=True) as other:
            organisation = other.get(m.Organisation, current.organisation_id)
            organisation.week_start = week_start
            organisation.input_revision += 1
        preview = rule_catalogue_v2.plan(db, current, proof=True)
        with transaction(h.app.state.engine) as fresh:
            reference = rule_catalogue_v2.plan(fresh, current, proof=True)
        assert cached.week_start == week_start
        assert preview['manifest'] == reference['manifest']
        assert preview['plan_hash'] == reference['plan_hash']
        assert preview['proof']['identical']
        assert preview['proof']['rotas'][0]['workspace_id'] == workspace['id']
        assert preview['proof']['rotas'][0]['input_hashes'] == reference['proof']['rotas'][0]['input_hashes']


def test_preview_reloads_cached_workspace_adjustments_before_blockers(h):
    source, workspace = tiny_conversion(h)
    with transaction(h.app.state.engine) as db:
        current = actor(db, h)
        cached = db.get(m.Workspace, workspace['id'])
        assert cached.rule_adjustments == []
        with transaction(h.app.state.engine, write=True) as other:
            other.get(m.Organisation, current.organisation_id).input_revision += 1
            changed = other.get(m.Workspace, workspace['id'])
            changed.rule_adjustments = [{'policy_id': source['id'], 'off': True, 'reason': 'Synthetic allowed adjustment'}]
            changed.content_revision += 1
        preview = rule_catalogue_v2.plan(db, current, proof=True)
        with transaction(h.app.state.engine) as fresh:
            reference = rule_catalogue_v2.plan(fresh, current, proof=True)
        assert preview['manifest'] == reference['manifest']
        assert preview['blocked'] == reference['blocked']
        assert any(row['rule_id'] == source['id'] and row['reason'] == 'a rota not archived changes it'
                   for row in preview['blocked'])
        assert preview['proof'] is None


def test_preview_reloads_cached_department_scoring_before_compiling_current_proof(h):
    tiny_conversion(h)
    with transaction(h.app.state.engine) as db:
        current = actor(db, h)
        cached = db.get(m.Department, current.department_id)
        assert cached.generation_defaults == {}
        with transaction(h.app.state.engine, write=True) as other:
            other.get(m.Department, current.department_id).generation_defaults = {'scoring': {'lead_balance': 'off'}}
            other.get(m.Organisation, current.organisation_id).input_revision += 1
        preview = rule_catalogue_v2.plan(db, current, proof=True)
        with transaction(h.app.state.engine) as fresh:
            reference = rule_catalogue_v2.plan(fresh, current, proof=True)
        assert cached.generation_defaults == {'scoring': {'lead_balance': 'off'}}
        assert preview['manifest'] == reference['manifest']
        assert preview['plan_hash'] == reference['plan_hash']
        assert preview['proof']['identical']
        assert preview['proof']['rotas'][0]['input_hashes'] == reference['proof']['rotas'][0]['input_hashes']


def test_preview_preserves_pending_local_input_writes_when_reloading(h):
    tiny_conversion(h)
    with transaction(h.app.state.engine, write=True) as db:
        current = actor(db, h)
        organisation = db.get(m.Organisation, current.organisation_id)
        week_start = (organisation.week_start + 1) % 7
        organisation.week_start = week_start
        organisation.input_revision += 1
        department = db.get(m.Department, current.department_id)
        department.generation_defaults = {'scoring': {'lead_balance': 'off'}}
        preview = rule_catalogue_v2.plan(db, current, proof=True)
        assert preview['proof']['identical']
        assert organisation.week_start == week_start
        assert department.generation_defaults == {'scoring': {'lead_balance': 'off'}}
    with transaction(h.app.state.engine) as fresh:
        organisation = fresh.get(m.Organisation, current.organisation_id)
        department = fresh.get(m.Department, current.department_id)
        reference = rule_catalogue_v2.plan(fresh, current, proof=True)
        assert organisation.week_start == week_start
        assert department.generation_defaults == {'scoring': {'lead_balance': 'off'}}
        assert preview['manifest'] == reference['manifest']
        assert preview['proof']['rotas'][0]['input_hashes'] == reference['proof']['rotas'][0]['input_hashes']
