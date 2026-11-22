from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.snapshots import snapshot_inputs
from apps.api.workspace_baselines import latest, live_safety_inputs, verify
from packages.domain.candidate_evidence import candidate_evidence
from packages.domain.time import now
from packages.domain.validation import evaluate, qualification_failures
from tests.clock_support import freeze_clock
from tests.test_schedule_score import data, shift
from tests.test_bulk_catalogue import rows


def _duty(sid, day, people=('a',)):
    value = shift(sid, 1, list(people))
    value['start'] = f'{day.isoformat()}T08:00:00+00:00'
    value['end'] = f'{day.isoformat()}T16:00:00+00:00'
    for participant in value['participants']:
        participant['start'], participant['end'] = value['start'], value['end']
    return value


def _dated_data(*duties):
    value = data(list(duties))
    value['workspace'] = {'department_id': 'd', 'starts_on': min(d['start'][:10] for d in duties),
                          'ends_on': max(d['end'][:10] for d in duties)}
    return value


def test_dated_inactivity_keeps_past_membership_and_blocks_future_candidate():
    inactive_from = now().date()
    past = _duty('past', inactive_from - timedelta(days=1))
    future = _duty('future', inactive_from)
    context = _dated_data(past, future)
    context['people']['a'].update(active=False, inactive_from=inactive_from.isoformat())
    definition = past['type_snapshot']['definition']

    assert qualification_failures(context, 'a', past, definition,
                                  datetime.fromisoformat(past['start']), datetime.fromisoformat(past['end'])) == []
    assert qualification_failures(context, 'a', future, definition,
                                  datetime.fromisoformat(future['start']), datetime.fromisoformat(future['end'])) == [
        {'code': 'PERSON_INACTIVE', 'person_id': 'a'}]
    assert candidate_evidence(context, 'a', past)['eligible_and_available']
    assert not candidate_evidence(context, 'a', future)['eligible_and_available']


def test_inactive_without_date_blocks_every_date():
    today = now().date()
    past = _duty('past', today - timedelta(days=1))
    future = _duty('future', today + timedelta(days=1))
    context = _dated_data(past, future)
    context['people']['a']['active'] = False
    definition = past['type_snapshot']['definition']

    for duty in (past, future):
        assert qualification_failures(context, 'a', duty, definition,
                                      datetime.fromisoformat(duty['start']), datetime.fromisoformat(duty['end'])) == [
            {'code': 'PERSON_INACTIVE', 'person_id': 'a'}]


def test_membership_end_still_binds_independently_of_person_inactive_date():
    today = now().date()
    duty = _duty('membership-end', today - timedelta(days=1))
    context = _dated_data(duty)
    context['people']['a'].update(active=False, inactive_from=today.isoformat())
    context['memberships'][0]['ends_on'] = (today - timedelta(days=1)).isoformat()
    failures = qualification_failures(context, 'a', duty, duty['type_snapshot']['definition'],
                                      datetime.fromisoformat(duty['start']), datetime.fromisoformat(duty['end']))
    assert any(failure['code'] == 'MEMBERSHIP_MISSING' for failure in failures)


def test_reactivated_person_is_eligible_for_future_duty():
    today = now().date()
    duty = _duty('reactivated', today + timedelta(days=1))
    context = _dated_data(duty)
    context['people']['a'].update(active=True, inactive_from=(today - timedelta(days=1)).isoformat())
    assert qualification_failures(context, 'a', duty, duty['type_snapshot']['definition'],
                                  datetime.fromisoformat(duty['start']), datetime.fromisoformat(duty['end'])) == []


def test_duty_crossing_into_inactive_date_is_blocked():
    context = _dated_data(_duty('overnight', date(2026, 10, 5)))
    duty = context['shifts'][0]
    duty['start'], duty['end'] = '2026-10-05T23:00:00+00:00', '2026-10-06T01:00:00+00:00'
    context['people']['a'].update(active=False, inactive_from='2026-10-06')
    failures = qualification_failures(context, 'a', duty, duty['type_snapshot']['definition'],
                                      datetime.fromisoformat(duty['start']), datetime.fromisoformat(duty['end']))
    assert failures == [{'code': 'PERSON_INACTIVE', 'person_id': 'a'}]


def test_inactive_from_is_an_input_manifest_field():
    from packages.domain.input_manifest import FIELDS
    assert 'inactive_from' in FIELDS['people']


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_snapshot_overlay_applies_dated_state_per_duty_date(h):
    draft = h.workspace('Dated person overlay', '2026-10-04', '2026-10-08')
    draft = h.shift(draft, '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00', people=[])['workspace']
    draft = h.shift(draft, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00', people=[])['workspace']
    person_id = h.seed['people'][0]
    with transaction(h.app.state.engine) as db:
        workspace = db.get(m.Workspace, draft['id'])
        frozen = verify(latest(db, workspace.id))
        current = snapshot_inputs(db, db.get(m.Organisation, h.seed['organisation_id']))
        current['people'][person_id] = {**current['people'][person_id], 'active': False, 'inactive_from': '2026-10-06'}
        overlay = live_safety_inputs(db, workspace, frozen, current=current)
    data = h.snapshot(draft)
    data['people'][person_id] = overlay['people'][person_id]
    duties = {row['start'][:10]: row for row in data['shifts']}
    assert data['people'][person_id]['inactive_from'] == '2026-10-06'
    assert candidate_evidence(data, person_id, duties['2026-10-04'])['eligible_and_available']
    assert not candidate_evidence(data, person_id, duties['2026-10-06'])['eligible_and_available']


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_greedy_and_cp_sat_candidate_paths_use_the_dated_domain_decision(h):
    from packages.domain import generation
    from packages.domain.cp_sat import generate

    person_id = h.seed['people'][0]
    h.policy({'kind': 'minimum_cover', 'minimum': 1}, {'person_id': person_id})
    draft = h.workspace('Dated solver paths', '2026-10-04', '2026-10-08')
    draft = h.shift(draft, '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00', people=[])['workspace']
    draft = h.shift(draft, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(draft)
    data['people'] = {person_id: {**data['people'][person_id], 'active': False, 'inactive_from': '2026-10-06'}}
    greedy = generation.generate(data, 5)
    additions = {(row['shift_id'], row['participant']['person_id']) for row in greedy['additions']}
    assert (data['shifts'][0]['id'], person_id) in additions
    assert (data['shifts'][1]['id'], person_id) not in additions
    result = generate(data, 5, workers=1, feasibility_only=True)
    evidence = {(row['shift_id'], row['person_id']): row for row in result['candidate_evidence']}
    duties = {row['start'][:10]: row for row in data['shifts']}
    assert evidence[duties['2026-10-04']['id'], person_id]['eligible_and_available']
    assert not evidence[duties['2026-10-06']['id'], person_id]['eligible_and_available']


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_reactivation_clears_live_inactive_date_but_preserves_history(h):
    person_id = h.seed['people'][0]
    past = '2026-09-20'
    person = next(p for p in h.get('admin', '/api/catalogues')['people'] if p['id'] == person_id)
    deactivated = h.configure('people', {'display_name': person['display_name'], 'active': False,
                                         'inactive_from': past}, person_id, person['revision'], role='admin')['record']
    reactivated = h.configure('people', {'display_name': person['display_name'], 'active': True},
                              person_id, deactivated['revision'], role='admin')['record']
    with transaction(h.app.state.engine) as db:
        person = db.get(m.Person, person_id)
        assert person.active is True and person.inactive_from is None
        versions = list(db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.kind == 'people', m.DefinitionHistory.object_id == person_id).order_by(
                m.DefinitionHistory.object_revision)))
    assert versions[-2].snapshot['inactive_from'] == past
    assert versions[-1].snapshot['inactive_from'] is None
    assert reactivated['inactive_from'] is None


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_reactivation_undo_restores_past_inactive_date_and_future_duty_block(h):
    person_id = h.seed['people'][0]
    past = '2026-09-20'
    person = next(p for p in h.get('admin', '/api/catalogues')['people'] if p['id'] == person_id)
    deactivated = h.configure('people', {'display_name': person['display_name'], 'active': False,
                                         'inactive_from': past}, person_id, person['revision'], role='admin')['record']
    reactivated = h.configure('people', {'display_name': person['display_name'], 'active': True},
                              person_id, deactivated['revision'], role='admin')
    restored = h.configure('people', reactivated['previous_value'], person_id,
                           reactivated['record']['revision'], role='admin')['record']
    assert restored['active'] is False and restored['inactive_from'] == past

    duty = _duty('after-reactivation-undo', date.fromisoformat(past) + timedelta(days=1))
    context = _dated_data(duty)
    context['people']['a'].update(active=restored['active'], inactive_from=restored['inactive_from'])
    assert qualification_failures(context, 'a', duty, duty['type_snapshot']['definition'],
                                  datetime.fromisoformat(duty['start']), datetime.fromisoformat(duty['end'])) == [
        {'code': 'PERSON_INACTIVE', 'person_id': 'a'}]


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_published_recheck_uses_dated_person_state(h):
    draft = h.shift(h.workspace('Published dated person', '2026-10-04', '2026-10-08'),
                    '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00')['workspace']
    published = h.publish(draft)
    person_id = h.seed['people'][0]
    with transaction(h.app.state.engine, write=True) as db:
        person = db.get(m.Person, person_id)
        person.active = False
        person.inactive_from = date(2026, 10, 6)
    data = h.snapshot(published['workspace'])
    duty = data['shifts'][0]
    assert candidate_evidence(data, person_id, duty)['eligible_and_available']
    assert not any(finding['code'] == 'PERSON_INACTIVE' for finding in evaluate(data)['findings'])


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_future_inactive_date_is_recorded_and_applied_per_duty(h):
    person = h.get('admin', '/api/catalogues')['people'][0]
    with transaction(h.app.state.engine) as db:
        organisation = db.get(m.Organisation, h.seed['organisation_id'])
        today = now().astimezone(ZoneInfo(organisation.timezone)).date().isoformat()
        future = (now().astimezone(ZoneInfo(organisation.timezone)).date() + timedelta(days=2)).isoformat()
    deactivated = h.configure('people', {'display_name': person['display_name'], 'active': False},
                               person['id'], person['revision'], role='admin')['record']
    assert deactivated['inactive_from'] == today
    reactivated = h.configure('people', {'display_name': person['display_name'], 'active': True},
                              person['id'], deactivated['revision'], role='admin')['record']
    assert reactivated['inactive_from'] is None
    saved = h.configure('people', {'display_name': person['display_name'], 'active': False,
                                  'inactive_from': future}, person['id'], reactivated['revision'], role='admin')['record']
    assert saved['active'] is False and saved['inactive_from'] == future
    draft = h.workspace('Future dated person', today, future)
    with transaction(h.app.state.engine) as db:
        frozen = verify(latest(db, draft['id']))
    assert frozen['people'][person['id']]['inactive_from'] == future
    duties = [_duty('before', date.fromisoformat(future) - timedelta(days=1)),
              _duty('boundary', date.fromisoformat(future))]
    context = _dated_data(*duties)
    context['people']['a'].update(active=saved['active'], inactive_from=saved['inactive_from'])
    assert candidate_evidence(context, 'a', duties[0])['eligible_and_available']
    assert not candidate_evidence(context, 'a', duties[1])['eligible_and_available']


@pytest.mark.parametrize('zone', ['UTC', 'Pacific/Auckland', 'America/New_York'])
def test_inactive_boundary_uses_local_midnight_and_exclusive_end(zone):
    from packages.domain.time import local_midnight
    from packages.domain.validation import person_active_for_interval

    context = _dated_data(_duty('boundary', date(2026, 10, 6)))
    context['organisation']['timezone'] = zone
    context['people']['a'].update(active=False, inactive_from='2026-10-06')
    boundary = local_midnight(date(2026, 10, 6), zone)
    assert person_active_for_interval(context, 'a', boundary - timedelta(hours=1), boundary)
    assert not person_active_for_interval(context, 'a', boundary - timedelta(hours=1), boundary + timedelta(microseconds=1))
    assert not person_active_for_interval(context, 'a', boundary, boundary + timedelta(hours=1))


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_active_person_writes_clear_inactive_date_and_deactivation_uses_local_day(h, monkeypatch):
    from apps.api import auth
    fixed = datetime.fromisoformat('2026-10-05T23:30:00+00:00')
    monkeypatch.setattr(auth, 'now', lambda: fixed)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Organisation, h.seed['organisation_id']).timezone = 'Pacific/Auckland'
    person = h.get('admin', '/api/catalogues')['people'][0]
    saved = h.configure('people', {'display_name': person['display_name'], 'active': True,
                                  'inactive_from': '2026-09-20'}, person['id'], person['revision'], role='admin')['record']
    assert saved['inactive_from'] is None
    inactive = h.configure('people', {'display_name': person['display_name'], 'active': False},
                           person['id'], saved['revision'], role='admin')['record']
    assert inactive['inactive_from'] == '2026-10-06'
    edited = h.configure('people', {'display_name': person['display_name'], 'active': False, 'title': 'Staff'},
                         person['id'], inactive['revision'], role='admin')['record']
    assert edited['inactive_from'] == '2026-10-06'


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('active', [True, False])
def test_person_creation_normalizes_inactive_date(h, active):
    from apps.api.auth import _local_today

    with transaction(h.app.state.engine) as db:
        expected = _local_today(db, h.seed['department_id']).isoformat()
    name = h.get('admin', '/api/catalogues')['people'][0]['display_name']
    person = h.configure('people', {'display_name': name, 'active': active,
                                   **({'inactive_from': '2026-09-20'} if active else {})}, role='admin')['record']
    assert person['inactive_from'] == (None if active else expected)



@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_bulk_people_deactivation_dates_use_local_day_and_clear_on_reactivation(h):
    from apps.api import models as m
    from apps.api.auth import _local_today
    from apps.api.db import transaction

    person = rows(h, 'people', h.seed['people'][:1])[0]
    item = {'id': person['id'], 'revision': person['revision']}
    with transaction(h.app.state.engine) as db:
        expected = _local_today(db, h.seed['department_id']).isoformat()
    h.command('admin', 'catalogue.bulk_update', {'catalogue': 'people', 'items': [item],
               'active': False, 'reason': 'Deactivate one person'})
    inactive = rows(h, 'people', [person['id']])[0]
    assert inactive['inactive_from'] == expected
    h.command('admin', 'catalogue.bulk_update', {'catalogue': 'people',
               'items': [{'id': inactive['id'], 'revision': inactive['revision']}],
               'active': True, 'reason': 'Reactivate one person'})
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Person, person['id']).inactive_from is None


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_bulk_active_person_clears_stale_inactive_date_before_deactivation(h):
    from apps.api.auth import _local_today
    from apps.api.db import transaction

    person = rows(h, 'people', h.seed['people'][:1])[0]
    past = '2026-09-20'
    h.command('admin', 'catalogue.bulk_update', {'catalogue': 'people',
               'items': [{'id': person['id'], 'revision': person['revision']}],
               'set': {'inactive_from': past}, 'reason': 'Clear stale date on active person'})
    active = rows(h, 'people', [person['id']])[0]
    assert active['active'] is True and active['inactive_from'] is None
    with transaction(h.app.state.engine) as db:
        expected = _local_today(db, h.seed['department_id']).isoformat()
    h.command('admin', 'catalogue.bulk_update', {'catalogue': 'people',
               'items': [{'id': active['id'], 'revision': active['revision']}],
               'active': False, 'reason': 'Deactivate after stale date'})
    inactive = rows(h, 'people', [person['id']])[0]
    assert inactive['inactive_from'] == expected and inactive['inactive_from'] != past


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_bulk_future_date_is_preserved_and_stale_active_date_is_ignored(h):
    person = rows(h, 'people', h.seed['people'][:1])[0]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, person['id']).inactive_from = date(2026, 9, 20)
    from apps.api.auth import _local_today
    with transaction(h.app.state.engine) as db:
        today = _local_today(db, h.seed['department_id'])
    reply = h.command('admin', 'catalogue.bulk_update', {'catalogue': 'people',
                  'items': [{'id': person['id'], 'revision': person['revision']}],
                  'active': False, 'reason': 'Deactivate with no explicit date'})
    assert reply['records'][0]['effective_active'] is False
    inactive = rows(h, 'people', [person['id']])[0]
    assert inactive['inactive_from'] == today.isoformat()
    future = (today + timedelta(days=10)).isoformat()
    h.command('admin', 'catalogue.bulk_update', {'catalogue': 'people',
              'items': [{'id': inactive['id'], 'revision': inactive['revision']}],
              'set': {'inactive_from': future}, 'reason': 'Schedule inactivity'})
    dated = rows(h, 'people', [person['id']])[0]
    assert dated['configured_active'] is False and dated['inactive_from'] == future
    assert dated['active'] is True
    reply = h.command('admin', 'catalogue.bulk_update', {'catalogue': 'people',
                  'items': [{'id': dated['id'], 'revision': dated['revision']}],
                  'set': {'short_code': 'DATE'}, 'reason': 'Keep the effective date'})
    saved = reply['records'][0]
    assert saved['active'] is False and saved['effective_active'] is True
    assert saved['short_code'] == 'DATE' and saved['revision'] > dated['revision']
    assert rows(h, 'people', [person['id']])[0]['inactive_from'] == future


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_undo_imported_person_sets_dated_inactive_state_and_history(h):
    from apps.api.auth import _local_today
    from apps.api.services.staff_import import COLUMNS
    from tests.test_staff_import import NEW, apply, csv_of, preview, row

    reviewed = preview(h, csv_of(row(display_name=NEW[COLUMNS.index('display_name')])))
    batch = apply(h, 'coordinator', reviewed['batch'])['batch']
    person_id = batch['applied']['applications'][0]['people_created'][0]['id']
    with transaction(h.app.state.engine) as db:
        expected = _local_today(db, h.seed['department_id']).isoformat()
    undone = h.command('coordinator', 'staff_import.undo', {'reason': 'Undo dated import'},
                      batch['id'], batch['revision'])
    person = next(p for p in h.get('admin', '/api/catalogues')['people'] if p['id'] == person_id)
    assert person['active'] is False and person['inactive_from'] == expected
    with transaction(h.app.state.engine) as db:
        history = db.scalar(select(m.DefinitionHistory).where(
            m.DefinitionHistory.kind == 'people', m.DefinitionHistory.object_id == person_id,
            m.DefinitionHistory.snapshot['inactive_from'].as_string() == expected))
    assert history is not None and undone['reverted'] == 1


def test_inactivity_migration_is_nullable_additive_and_has_one_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from apps.api.schema import supported_revision
    from migrations import safety

    path = safety.VERSIONS / '0150_person_inactive_from.py'
    assert safety.classification(path) == 'additive'
    assert not safety.findings(path)
    assert not safety.needs_confirmation(path)
    assert not safety.declaration_errors(path)
    config = Config()
    config.set_main_option('script_location', str(safety.VERSIONS.parent))
    graph = ScriptDirectory.from_config(config)
    head, = graph.get_heads()
    assert head == supported_revision()
    revision = '0150_person_inactive_from'
    assert graph.get_revision(revision).down_revision == '0148_bulk_analyze'
    assert revision in {node.revision for node in graph.iterate_revisions(head, 'base')}


def test_inactivity_migration_retains_existing_rows_without_backfill():
    import importlib
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    engine = sa.create_engine('sqlite://')
    try:
        with engine.begin() as db:
            db.execute(sa.text('CREATE TABLE people (id TEXT PRIMARY KEY, active BOOLEAN NOT NULL)'))
            db.execute(sa.text('INSERT INTO people VALUES (:id, :active)'),
                       [{'id': 'active', 'active': True}, {'id': 'inactive', 'active': False}])
            before = list(db.execute(sa.text('SELECT * FROM people ORDER BY id')))
            migration = importlib.import_module('migrations.versions.0150_person_inactive_from')
            with Operations.context(MigrationContext.configure(db)):
                migration.upgrade()
            assert list(db.execute(sa.text('SELECT id, active FROM people ORDER BY id'))) == before
            assert list(db.scalars(sa.text('SELECT inactive_from FROM people'))) == [None, None]
            column = next(c for c in sa.inspect(db).get_columns('people') if c['name'] == 'inactive_from')
            assert column['nullable'] and isinstance(column['type'], sa.Date)
    finally:
        engine.dispose()


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_undated_inactive_person_is_never_seated_and_published_finding_remains(h):
    from packages.domain import generation
    from packages.domain.cp_sat import generate

    published = h.publish(h.shift(h.workspace('Retained inactive finding', '2026-10-01', '2026-10-05'),
                          '2026-10-02T08:00:00+03:00', '2026-10-02T12:00:00+03:00')['workspace'])
    draft = h.shift(h.workspace('Undated inactive candidate', '2026-10-01', '2026-10-05'),
                    '2026-10-02T08:00:00+03:00', '2026-10-02T12:00:00+03:00', people=[])['workspace']
    person_id = h.seed['people'][0]
    with transaction(h.app.state.engine, write=True) as db:
        person = db.get(m.Person, person_id)
        person.active, person.inactive_from = False, None
    context = h.snapshot(draft)
    context['people'] = {person_id: context['people'][person_id]}
    assert not generation.generate(context, 5)['additions']
    result = generate(context, 5, workers=1, feasibility_only=True)
    assert not result['additions']
    assert len(result['candidate_evidence']) == 1
    assert all(not row['eligible_and_available'] for row in result['candidate_evidence'])
    retained = h.snapshot(published['workspace'])
    duty = retained['shifts'][0]
    assert any(row['code'] == 'PERSON_INACTIVE' for row in candidate_evidence(retained, person_id, duty)['exclusions'])
    assert any(row['code'] == 'NOT_ELIGIBLE' and row['person_id'] == person_id
               for row in evaluate(retained)['findings'])


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_inactive_date_edit_reaches_an_open_draft_without_refresh_or_baseline_drift(h):
    from apps.api.workspace_baselines import _diff

    draft = h.workspace('Live dated person change', '2026-10-04', '2026-10-08')
    for day in ('04', '06'):
        draft = h.shift(draft, f'2026-10-{day}T08:00:00+03:00',
                        f'2026-10-{day}T12:00:00+03:00', people=[])['workspace']
    before = h.snapshot(draft)
    person_id = h.seed['people'][0]
    person = before['people'][person_id]
    with transaction(h.app.state.engine) as db:
        baseline = latest(db, draft['id'])
        baseline_id, payload_hash = baseline.id, baseline.payload_hash
    saved = h.configure('people', {'display_name': person['display_name'], 'active': False, 'inactive_from': '2026-10-06'},
                        person_id, person['revision'], role='admin')['record']
    changed = h.snapshot(draft)
    assert changed['fingerprint'] != before['fingerprint']
    assert changed['people'][person_id]['inactive_from'] == '2026-10-06'
    duties = {row['start'][:10]: row for row in changed['shifts']}
    assert candidate_evidence(changed, person_id, duties['2026-10-04'])['eligible_and_available']
    assert not candidate_evidence(changed, person_id, duties['2026-10-06'])['eligible_and_available']
    h.configure('people', {'display_name': person['display_name'], 'active': False, 'inactive_from': '2026-10-04'},
                person_id, saved['revision'], role='admin')
    edited = h.snapshot(draft)
    assert edited['fingerprint'] != changed['fingerprint']
    assert edited['people'][person_id]['inactive_from'] == '2026-10-04'
    assert not candidate_evidence(edited, person_id, duties['2026-10-04'])['eligible_and_available']
    with transaction(h.app.state.engine) as db:
        workspace = db.get(m.Workspace, draft['id'])
        baseline = latest(db, workspace.id)
        assert (baseline.id, baseline.payload_hash) == (baseline_id, payload_hash)
        assert verify(baseline)['people'][person_id]['active'] is True
        current = snapshot_inputs(db, db.get(m.Organisation, h.seed['organisation_id']))
        assert not any(change['source'] == 'people' for change in _diff(baseline, workspace, current)[2])


@pytest.fixture
def dated_people(h):
    future, undated, _ = h.seed['people']
    with transaction(h.app.state.engine, write=True) as db:
        person = db.get(m.Person, future)
        person.active, person.inactive_from = False, date(2026, 10, 6)
        db.get(m.Person, undated).active = False
    return future, undated


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_people_reads_and_counts_change_at_department_midnight(h, dated_people, monkeypatch):
    from packages.domain.time import local_midnight

    future, undated = dated_people
    with transaction(h.app.state.engine, write=True) as db:
        zone = db.get(m.Organisation, h.seed['organisation_id']).timezone
        for row in db.scalars(select(m.Membership).where(m.Membership.person_id == future)):
            row.ends_on = date(2026, 10, 5)
    cutoff = local_midnight(date(2026, 10, 6), zone)
    workspace = h.workspace()
    summary_path = f'/api/workspaces/{workspace["id"]}/summary'
    path = '/api/catalogues?include=people'
    previous, summary_etag = None, None
    for instant, active in ((cutoff - timedelta(seconds=1), True), (cutoff, False)):
        freeze_clock(monkeypatch, instant)
        h.clients.clear()  # sign in again at the frozen time: the session clock moves with it
        client = h.client('coordinator')
        reply = client.get(path, headers={'If-None-Match': previous} if previous else {})
        assert reply.status_code == 200
        previous = reply.headers['etag']
        people = {p['id']: p for p in reply.json()['people']}
        assert people[future]['active'] is active
        assert people[future]['configured_active'] is False
        assert people[undated]['active'] is False
        assert (future in {p['id'] for p in h.get('staff', '/api/directory')['people']}) is active
        assert undated not in {p['id'] for p in h.get('staff', '/api/directory')['people']}
        counts = h.get('coordinator', '/api/people/summary')
        assert counts == {'active_people': 1 + active, 'people_without_current_rank': int(active)}
        assert h.get('coordinator', path + '&counts_only=true')['counts']['people'] == 1 + active
        home = h.get('coordinator', '/api/home')
        assert next((row['count'] for row in home['attention'] if row['key'] == 'no_rank'), 0) == int(active)
        summary = client.get(summary_path, headers={'If-None-Match': summary_etag} if summary_etag else {})
        assert summary.status_code == 200
        summary_etag = summary.headers['etag']
        assert summary.json()['people'] == counts
        selected = client.post('/api/bulk/rows', json={'selection': {'collection': 'people',
            'where': {'filters': {'status': ['active']}}}})
        assert selected.status_code == 200
        assert {row['id'] for row in selected.json()['rows']} == (
            {h.seed['people'][2], future} if active else {h.seed['people'][2]})


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_rule_reach_clips_the_people_window_at_deactivation(h, dated_people):
    from packages.domain.rule_map import rule_map
    from .test_rule_map import entry
    from .test_rule_templates import activate, propose

    future, undated = dated_people
    rule = {'kind': 'max_hours', 'hours': 48}
    scope = {'person_ids': [future, undated]}
    general = activate(h, propose(h, rule, scope, name='Dated reach'))
    later = activate(h, propose(h, rule, scope, name='Later reach', starts_on='2026-10-06'))
    for day, expected in (('2026-10-05', 1), ('2026-10-06', 0)):
        counted = h.get('coordinator', f'/api/policies/reach?day={day}')['reach']
        assert (counted[general['id']], counted[later['id']]) == (expected, 0)
    for first, ids in (('2026-10-05', {future}), ('2026-10-06', set())):
        detailed = h.get('coordinator', f'/api/policies/reach?starts_on={first}&ends_on=2026-10-07')
        assert detailed['reach'][general['id']] == len(ids)
        assert detailed['reach'][later['id']] == 0
        assert {p['person_id'] for line in detailed['details'].get(general['id'], {}).get('lines', [])
                for p in line['people']} == ids
    mapped = rule_map(h.snapshot(h.workspace()))
    assert entry(mapped, general['id'])['people'] == {future: [[0, 2]]}
    assert entry(mapped, later['id'])['people'] == {}


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_overtime_reads_each_day_before_deactivation(h, dated_people):
    from .test_overtime_volunteer import offer_rule

    future, _ = dated_people
    offer_rule(h, person=future)

    def eligibility(person, start, end):
        return h.get('coordinator', f'/api/requests/overtime-eligibility?starts_on={start}'
                     f'&ends_on={end}&person_id={person}')

    crossing = eligibility(future, '2026-10-05', '2026-10-07')
    assert crossing['eligible'] and {offer['starts_on'] for offer in crossing['offers']} == {'2026-10-05'}
    assert not eligibility(future, '2026-10-06', '2026-10-07')['eligible']
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, future).inactive_from = None
    assert not eligibility(future, '2026-10-05', '2026-10-07')['eligible']


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_person_score_uses_the_rota_window_for_freshness_and_opportunities(h, dated_people):
    from packages.contracts.schemas import RequirementSegment

    future, undated = dated_people
    with transaction(h.app.state.engine, write=True) as db:
        typ = db.get(m.AssignmentType, h.seed['type'])
        typ.definition = {**typ.definition, 'requirements': [RequirementSegment(minimum=2).model_dump()]}
        person = db.get(m.Person, future)
        person.active, person.inactive_from = True, None
    before = h.workspace(start='2026-10-04', end='2026-10-06')
    after = h.workspace(start='2026-10-06', end='2026-10-08')
    h.shift(before, people=[])
    h.shift(after, start='2026-10-06T08:00:00+03:00', end='2026-10-06T12:00:00+03:00', people=[])

    def score(person, workspace):
        path = f'/api/people/{person}/score?workspace_id={workspace["id"]}&open_limit=20'
        return h.get('coordinator', path)['draft']

    control = score(future, before)
    assert control['fresh'] and len(control['open_places']) == 1
    with transaction(h.app.state.engine, write=True) as db:
        person = db.get(m.Person, future)
        person.active, person.inactive_from = False, date(2026, 10, 6)
    past = score(future, before)
    assert past['fresh'] and past['open_places'] == control['open_places']
    for person, workspace in ((future, after), (undated, before), (undated, after)):
        result = score(person, workspace)
        assert not result['fresh'] and result['open_places'] == []


def _person_form_value(person, **changes):
    """What the person edit form sends: every field it shows, plus the dated deactivation when Active is off."""
    keys = ('display_name', 'colour', 'title', 'directory_section', 'work_phone', 'work_extension', 'staff_number',
            'pager', 'directory_order', 'short_code', 'tags', 'rota_name', 'rota_aliases')
    return {**{key: person[key] for key in keys if key in person}, 'active': person.get('configured_active', person['active']), **changes}


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_form_dated_deactivation_round_trips_and_undo_restores_each_step(h):
    from apps.api.auth import _local_today

    with transaction(h.app.state.engine) as db:
        today = _local_today(db, h.seed['department_id'])
    first, moved = (today + timedelta(days=30)).isoformat(), (today + timedelta(days=45)).isoformat()
    person = rows(h, 'people', h.seed['people'][:1])[0]
    reply = h.configure('people', _person_form_value(person, active=False, inactive_from=first),
                        person['id'], person['revision'], role='coordinator')
    saved = rows(h, 'people', [person['id']])[0]
    # Saved inactive from a later date: active in people reads until that date begins.
    assert saved['configured_active'] is False and saved['active'] is True and saved['inactive_from'] == first
    # Undo (the form's catalogue.update with the server's previous value) makes the person active again with no date.
    undone = h.configure('people', reply['previous_value'], person['id'], reply['record']['revision'], role='coordinator')['record']
    assert undone['active'] is True and undone['inactive_from'] is None

    dated = h.configure('people', _person_form_value(undone, active=False, inactive_from=first),
                        person['id'], undone['revision'], role='coordinator')['record']
    # A later edit without the date (the form leaves an untouched field out) keeps the saved date.
    kept = h.configure('people', _person_form_value(dated, title='Staff'), person['id'], dated['revision'], role='coordinator')['record']
    assert kept['active'] is False and kept['inactive_from'] == first
    reply = h.configure('people', _person_form_value(kept, inactive_from=moved), person['id'], kept['revision'], role='coordinator')
    assert reply['record']['inactive_from'] == moved
    back = h.configure('people', reply['previous_value'], person['id'], reply['record']['revision'], role='coordinator')['record']
    assert back['active'] is False and back['inactive_from'] == first


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('bad', ['2026-02-30', '30/10/2026', 'next month'])
def test_form_refuses_an_inactive_date_that_is_not_a_date(h, bad):
    person = rows(h, 'people', h.seed['people'][:1])[0]
    h.configure('people', _person_form_value(person, active=False, inactive_from=bad),
                person['id'], person['revision'], role='coordinator', status=422)
    assert rows(h, 'people', [person['id']])[0]['active'] is True


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_form_accepts_a_past_inactive_date(h):
    """The API keeps a past date as given (history before it still counts), so the form allows one too."""
    person = rows(h, 'people', h.seed['people'][:1])[0]
    saved = h.configure('people', _person_form_value(person, active=False, inactive_from='2026-09-20'),
                        person['id'], person['revision'], role='coordinator')['record']
    assert saved['active'] is False and saved['inactive_from'] == '2026-09-20'
