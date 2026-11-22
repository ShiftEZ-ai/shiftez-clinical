"""The organisation input stamp is derived from canonical ORM input writes."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import MetaData, Table, delete, insert, select, update

from apps.api import models as m, snapshots
from apps.api import db as database
from apps.api.db import snapshot_input_listener_classes, transaction
from apps.api.errors import Problem
from packages.contracts.schemas import GENERATION_RUNTIME_OPTIONS, GenerationOptions
from tests.test_fix_list_performance import backend
from .test_events import event, event_type, repeat


def test_snapshot_input_registry_is_covered_by_flush_listener():
    specs = snapshots.snapshot_input_specs()
    assert {cls for _name, cls, *_ in specs} == set(snapshot_input_listener_classes())
    assert {category for _name, _cls, category, *_ in specs} == {'general', 'availability'}
    assert len(specs) == len({cls for _name, cls, *_ in specs})
    for _name, cls, _category, _scope, _shape, exclusions in specs:
        for exclusion in exclusions:
            column, _, key = exclusion.partition('.')
            assert column in cls.__table__.columns
            if key:
                assert cls is m.Department and column == 'generation_defaults'
                assert key in GENERATION_RUNTIME_OPTIONS & GenerationOptions.model_fields.keys()


def test_general_input_moves_once_per_transaction_and_non_input_does_not(backend):
    h = backend
    with transaction(h.app.state.engine, write=True) as db:
        org = db.get(m.Organisation, h.seed['organisation_id'])
        before = (org.input_revision, org.availability_revision)
        location = db.get(m.Location, h.seed['locations'][0])
        location.capacity += 1
        db.flush()
        location.name = location.name + ' (stamp)'
        db.flush()
        assert (org.input_revision, org.availability_revision) == (before[0] + 1, before[1])

    with transaction(h.app.state.engine, write=True) as db:
        org = db.get(m.Organisation, h.seed['organisation_id'])
        before = (org.input_revision, org.availability_revision)
        identity = db.get(m.Identity, h.seed['identities']['coordinator'])
        identity.display_name = identity.display_name + ' (metadata)'
        db.flush()
        assert (org.input_revision, org.availability_revision) == before


def test_availability_input_moves_both_revisions_once_without_explicit_bump(backend):
    h = backend
    with transaction(h.app.state.engine, write=True) as db:
        org = db.get(m.Organisation, h.seed['organisation_id'])
        before = (org.input_revision, org.availability_revision)
        db.add(m.Availability(
            person_id=h.seed['people'][0], department_id=h.seed['department_id'],
            start=datetime(2026, 10, 4, 8, tzinfo=timezone.utc),
            end=datetime(2026, 10, 4, 9, tzinfo=timezone.utc), kind='actual_absence',
            leave_type='', counts_as_unavailable=True, private_reason='',
            recorded_by=h.seed['identities']['coordinator'], retracted=False,
        ))
        db.flush()
        db.add(m.Availability(
            person_id=h.seed['people'][0], department_id=h.seed['department_id'],
            start=datetime(2026, 10, 5, 8, tzinfo=timezone.utc),
            end=datetime(2026, 10, 5, 9, tzinfo=timezone.utc), kind='actual_absence',
            leave_type='', counts_as_unavailable=True, private_reason='',
            recorded_by=h.seed['identities']['coordinator'], retracted=False,
        ))
        db.flush()
        assert (org.input_revision, org.availability_revision) == (before[0] + 1, before[1] + 1)


def test_unresolved_input_is_an_error_in_the_guard_path(backend):
    h = backend
    with pytest.raises(ValueError, match='Unresolved snapshot input Location id='):
        with transaction(h.app.state.engine, write=True) as db:
            db.add(m.Location(department_id=m.uid(), name='Unresolvable synthetic input'))
            db.flush()


@pytest.mark.parametrize('known', [False, True])
def test_runtime_unresolved_input_stamps_touched_or_all_organisations(backend, monkeypatch, caplog, known):
    h = backend
    with transaction(h.app.state.engine, write=True) as db:
        first = db.get(m.Organisation, h.seed['organisation_id'])
        other = m.Organisation(name='Second synthetic organisation')
        db.add(other)
        db.flush()
        other_id = other.id
    with transaction(h.app.state.engine, write=True) as db:
        if known:
            db.get(m.Organisation, h.seed['organisation_id'])
            # Keep a strong reference: the ORM identity map uses weak references.
            touched = db.get(m.Department, h.seed['department_id'])
        monkeypatch.setenv('ROTA_ENV', 'development')
        row = m.Availability(id=m.uid(), department_id=m.uid())
        db.add(row)
        with db.no_autoflush:
            database.stamp_snapshot_inputs(db, None, None)
            ids = db.info['_snapshot_input_revisions']['availability']
            assert ids == ({h.seed['organisation_id']} if known else {h.seed['organisation_id'], other_id})
            assert any(record.levelname == 'ERROR' and type(row).__name__ in record.message and row.id in record.message
                       for record in caplog.records)
            assert db.get(m.Organisation, h.seed['organisation_id']).availability_revision > 1
            # The listener must fail closed before an invalid FK reaches the database.
            db.expunge(row)



@pytest.mark.parametrize('nested', ['commit', 'rollback'])
def test_savepoints_preserve_once_per_outer_transaction(backend, nested):
    h = backend
    with transaction(h.app.state.engine, write=True) as db:
        org = db.get(m.Organisation, h.seed['organisation_id'])
        before = org.input_revision
        location = db.get(m.Location, h.seed['locations'][0])
        location.capacity += 1
        db.flush()
        savepoint = db.begin_nested()
        location.capacity += 1
        db.flush()
        getattr(savepoint, nested)()
        location.capacity += 1
        db.flush()
        assert org.input_revision == before + 1

    with transaction(h.app.state.engine, write=True) as db:
        org = db.get(m.Organisation, h.seed['organisation_id'])
        assert org.input_revision == before + 1
        savepoint = db.begin_nested()
        db.get(m.Location, h.seed['locations'][0]).capacity += 1
        db.flush()
        savepoint.rollback()
        db.get(m.Location, h.seed['locations'][0]).capacity += 1
        db.flush()
        assert org.input_revision == before + 2


def test_live_safety_cache_reloads_after_each_flush_in_one_transaction(backend):
    from apps.api import workspace_baselines
    h = backend
    workspace = h.workspace()
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, workspace['id'])
        org = db.get(m.Organisation, h.seed['organisation_id'])
        person = db.get(m.Person, h.seed['people'][0])
        workspace_baselines._live_safety_snapshot(db, row)
        before = org.input_revision
        for label in ('First changed name', 'Second changed name'):
            person.display_name = label
            db.flush()
            assert org.input_revision == before + 1
            live = workspace_baselines._live_safety_snapshot(db, row)
            assert live['people'][person.id]['display_name'] == label


@pytest.mark.parametrize('writer', ['flush', 'insert', 'update', 'delete', 'core_session', 'bulk_pk'])
def test_all_session_write_paths_stamp_and_clear_both_memos(backend, writer):
    from apps.api.services import planning_preparation
    h = backend
    with transaction(h.app.state.engine, write=True) as db:
        org = db.get(m.Organisation, h.seed['organisation_id'])
        plan = {'seed': 'stamp-memo', 'at': m.now(), 'checks': []}
        planning_preparation.bind(db, plan)
        original = snapshots.prepared_snapshot_inputs(db, org)
        plan['snapshots']['old'] = b'old prepared domain inputs'
        before = org.input_revision
        location_id = h.seed['locations'][0]
        location = db.get(m.Location, location_id)
        db.info['_live_safety_sources'] = {(org.id, before, org.availability_revision): {'old': True}}
        if writer == 'flush':
            location.capacity += 1
            db.flush()
        elif writer in {'update', 'core_session', 'bulk_pk'}:
            target = m.Location.__table__ if writer == 'core_session' else m.Location
            if writer == 'bulk_pk':
                db.execute(update(target), [{'id': location_id, 'capacity': location.capacity + 1}])
            else:
                db.execute(update(target).where(target.c.id == location_id if writer == 'core_session' else m.Location.id == location_id)
                           .values(capacity=location.capacity + 1))
        else:
            db.execute(insert(m.Location), [{'id': m.uid(), 'department_id': h.seed['department_id'], 'name': 'New location',
                                            'location_type_id': location.location_type_id, 'starts_on': location.starts_on}])
            if writer == 'delete':
                db.execute(delete(m.Location).where(m.Location.name == 'New location'))
        assert org.input_revision == before + 1
        assert not db.info['_live_safety_sources']
        assert not plan['snapshots']
        assert org.id not in plan['inputs'] and org.id not in plan['input_tokens']
        snapshots.prepared_snapshot_inputs(db, org)
        db.info['_live_safety_sources'][(org.id, org.input_revision, org.availability_revision)] = {'old': True}
        location.name += ' changed again'
        db.flush()
        assert org.input_revision == before + 1
        assert not db.info['_live_safety_sources'] and org.id not in plan['inputs']
        fresh = snapshots.prepared_snapshot_inputs(db, org)
        assert fresh['locations'][location_id]['name'] == location.name
        assert fresh != original


@pytest.mark.parametrize('writer', ['update', 'update_all', 'update_from', 'delete'])
def test_reflected_session_dml_stamps_and_refreshes_target_rows(backend, writer):
    h = backend
    with transaction(h.app.state.engine, write=True) as db:
        target = m.Person(organisation_id=h.seed['organisation_id'], display_name='Synthetic stamp target')
        source = m.Person(organisation_id=h.seed['organisation_id'], display_name='Synthetic stamp source')
        db.add_all([target, source])
        db.flush()
        target_id, source_id = target.id, source.id
    with transaction(h.app.state.engine, write=True) as db:
        org = db.get(m.Organisation, h.seed['organisation_id'])
        before = (org.input_revision, org.availability_revision)
        target = db.get(m.Person, target_id)
        table = Table('people', MetaData(), autoload_with=db.connection())
        if writer == 'delete':
            statement = delete(table).where(table.c.id == target_id)
        elif writer == 'update_all':
            statement = update(table).values(active=False)
        elif writer == 'update_from':
            source_table = table.alias('stamp_source')
            statement = update(table).where(table.c.id == target_id, source_table.c.id == source_id,
                source_table.c.organisation_id == table.c.organisation_id).values(display_name=source_table.c.display_name)
        else:
            statement = update(table).where(table.c.id == target_id).values(active=False)
        db.execute(statement)
        assert (org.input_revision, org.availability_revision) == (before[0] + 1, before[1])
        if writer == 'delete':
            assert db.get(m.Person, target_id) is None
        elif writer == 'update_from':
            assert target.display_name == 'Synthetic stamp source'
        else:
            assert target.active is False
        source = db.get(m.Person, source_id)
        assert source.active is (writer != 'update_all')
        assert source.display_name == 'Synthetic stamp source'


def test_only_declared_exclusions_suppress_modified_rows(backend):
    h = backend
    with transaction(h.app.state.engine, write=True) as db:
        org = db.get(m.Organisation, h.seed['organisation_id'])
        department = db.get(m.Department, h.seed['department_id'])
        before = org.input_revision
        defaults = dict(department.generation_defaults or {})
        defaults['search_minutes'] = defaults.get('search_minutes', 1) + 1
        assert 'search_minutes' in GENERATION_RUNTIME_OPTIONS
        department.generation_defaults = defaults
        department.revision += 1
        db.flush()
        assert org.input_revision == before
        # Switching from the selected calendar to the weekend fallback changes scheduling currency.
        assert department.operating_calendar_id is not None
        department.operating_calendar_id = None
        db.flush()
        assert org.input_revision == before + 1


@pytest.mark.parametrize('selected_calendar', [True, False])
@pytest.mark.parametrize('writer', ['orm', 'statement', 'core', 'bulk'])
def test_weekend_shading_stamps_only_without_an_operating_calendar(backend, selected_calendar, writer):
    h = backend
    if not selected_calendar:
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Department, h.seed['department_id']).operating_calendar_id = None
    with transaction(h.app.state.engine, write=True) as db:
        org = db.get(m.Organisation, h.seed['organisation_id'])
        department = db.get(m.Department, h.seed['department_id'])
        before = (org.input_revision, org.availability_revision)
        if writer == 'orm':
            department.weekend_days = [5, 6]
        elif writer == 'bulk':
            db.execute(update(m.Department), [{'id': department.id, 'weekend_days': [5, 6]}])
        else:
            target = m.Department.__table__ if writer == 'core' else m.Department
            key = target.c.id if writer == 'core' else target.id
            db.execute(update(target).where(key == department.id).values(weekend_days=[5, 6]))
        db.flush()
        assert department.weekend_days == [5, 6]
        assert (org.input_revision, org.availability_revision) == (before[0] + (not selected_calendar), before[1])


def test_one_write_changing_weekends_and_calendar_stamps_inputs(backend):
    h = backend
    with transaction(h.app.state.engine, write=True) as db:
        org = db.get(m.Organisation, h.seed['organisation_id'])
        department = db.get(m.Department, h.seed['department_id'])
        assert department.operating_calendar_id is not None and department.weekend_days != [5, 6]
        before = (org.input_revision, org.availability_revision)
        db.execute(update(m.Department).where(m.Department.id == department.id).values(
            weekend_days=[5, 6], operating_calendar_id=None))
        db.flush()
        assert department.weekend_days == [5, 6] and department.operating_calendar_id is None
        assert (org.input_revision, org.availability_revision) == (before[0] + 1, before[1])


@pytest.mark.parametrize('core', [False, True])
def test_bulk_input_reads_cannot_reuse_unsynchronised_identity_state(backend, core):
    h = backend
    with transaction(h.app.state.engine, write=True) as db:
        org = db.get(m.Organisation, h.seed['organisation_id'])
        original = snapshots.snapshot_inputs(db, org)
        target = m.Organisation.__table__ if core else m.Organisation
        key = target.c.id if core else target.id
        db.execute(update(target).where(key == org.id).values(timezone='UTC'),
                   execution_options={'synchronize_session': False})
        assert org.timezone == 'UTC' and original['organisation']['timezone'] != org.timezone
        assert snapshots.snapshot_inputs(db, org)['organisation']['timezone'] == 'UTC'


@pytest.mark.parametrize('writer', ['flush', 'deferred', 'bulk', 'runtime_expression'])
def test_ownership_moves_stamp_both_organisations(backend, monkeypatch, writer):
    h = backend
    other_id, department_id = m.uid(), m.uid()
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Organisation(id=other_id, name='Other synthetic organisation'))
        db.add(m.Department(id=department_id, organisation_id=h.seed['organisation_id'], name='Movable department'))
    with transaction(h.app.state.engine, write=True) as db:
        old, new = [db.get(m.Organisation, ident) for ident in (h.seed['organisation_id'], other_id)]
        before = old.input_revision, new.input_revision
        if writer == 'bulk':
            db.execute(update(m.Department).where(m.Department.id == department_id).values(organisation_id=other_id))
        elif writer == 'runtime_expression':
            monkeypatch.setenv('ROTA_ENV', 'development')
            db.execute(update(m.Department).where(m.Department.id == department_id).values(
                organisation_id=select(m.Organisation.id).where(m.Organisation.id == other_id).scalar_subquery()))
        else:
            from sqlalchemy.orm import load_only
            options = [load_only(m.Department.id)] if writer == 'deferred' else []
            db.get(m.Department, department_id, options=options).organisation_id = other_id
        db.flush()
        assert (old.input_revision, new.input_revision) == (before[0] + 1, before[1] + 1)


def test_rollback_discards_changed_prepared_inputs_but_keeps_unchanged_ones(backend):
    from apps.api.services import planning_preparation
    h = backend
    with transaction(h.app.state.engine, write=True) as db:
        org = db.get(m.Organisation, h.seed['organisation_id'])
        plan = {'seed': 'rollback-inputs', 'at': m.now(), 'checks': []}
        planning_preparation.bind(db, plan)
        before = snapshots.prepared_snapshot_inputs(db, org)
        db.rollback()
        assert org.id in plan['inputs']
        location = db.get(m.Location, h.seed['locations'][0])
        location.capacity += 1
        db.flush()
        changed = snapshots.prepared_snapshot_inputs(db, org)
        assert changed['locations'][location.id]['capacity'] == before['locations'][location.id]['capacity'] + 1
        db.rollback()
        assert org.id not in plan['inputs']
        assert snapshots.prepared_snapshot_inputs(db, org) == before


@pytest.mark.parametrize('bulk', [False, True])
def test_session_dml_uses_the_same_runtime_key_exclusions(backend, bulk):
    from sqlalchemy import bindparam
    h = backend
    with transaction(h.app.state.engine, write=True) as db:
        org = db.get(m.Organisation, h.seed['organisation_id'])
        department = db.get(m.Department, h.seed['department_id'])
        before = org.input_revision
        defaults = {**(department.generation_defaults or {}), 'search_minutes': 3}
        if bulk:
            db.execute(update(m.Department), [{'id': department.id, 'generation_defaults': defaults}])
        else:
            db.execute(update(m.Department).where(m.Department.id == department.id).values(
                generation_defaults=bindparam('new_defaults')), {'new_defaults': defaults})
        db.flush()
        assert org.input_revision == before
        assert department.generation_defaults == defaults


def test_workspace_lifecycle_and_redate_stamp_once(h):
    rota = h.shift(h.workspace("Workspace input stamp"))['workspace']

    def input_revision():
        with transaction(h.app.state.engine) as db:
            return db.get(m.Organisation, h.seed['organisation_id']).input_revision

    # 17 Nov oracle (#1171): a lone rota's status is read by no other rota, so its submit and reopen do not advance.
    before = input_revision()
    rota = h.lifecycle(rota, 'submit')['workspace']
    assert input_revision() == before
    rota = h.lifecycle(rota, 'reopen')['workspace']
    assert input_revision() == before
    # Its own reopen is caught by the erased check, not by the input revision.
    assert h.detail(rota)['workspace']['validation_current'] is False

    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, rota['id'])
        org = db.get(m.Organisation, h.seed['organisation_id'])
        before = org.input_revision
        row.starts_on += timedelta(days=1)
        row.ends_on += timedelta(days=1)
        db.flush()
        assert org.input_revision == before + 1


def test_archiving_published_rota_refuses_old_activation_receipt(h):
    from apps.worker.main import process_one
    from .test_contextual_feasibility import activate

    published = h.publish(h.shift(h.workspace("Published input stamp"))["workspace"])["workspace"]
    target = h.shift(h.workspace("Target input stamp"))["workspace"]
    policy = h.command("coordinator", "policy.create", {
        "name": "Archive invalidation rule", "owner": "FM coordinator", "source": "Stamp regression",
        "starts_on": "2026-01-01", "definition": {"rule": {"kind": "restriction"}},
    })["record"]
    job = h.command("approver", "job.create", {
        "workspace_id": target["id"], "kind": "feasibility",
        "rule_changes": [{"policy_id": policy["id"], "expected_revision": policy["revision"]}],
    }, revision=target["revision"])["job"]
    assert process_one(h.app.state.engine)
    archived = h.command("coordinator", "workspace.archive", {"reason": "Stamp regression"},
                         published["id"], published["revision"])["workspace"]
    assert archived["archived_at"]
    with pytest.raises(Problem) as error:
        activate(h, policy, job["id"], acknowledge=True)
    assert error.value.code == "RULE_CHECK_REQUIRED"


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_archiving_before_receipt_keeps_unchanged_receipt_accepted(request, backend):
    from apps.worker.main import process_one
    from .test_contextual_feasibility import activate
    h = request.getfixturevalue(backend)

    published = h.publish(h.shift(h.workspace("Archived context"))["workspace"])["workspace"]
    archived = h.command("coordinator", "workspace.archive", {"reason": "Archive before review"},
                         published["id"], published["revision"])["workspace"]
    assert archived["archived_at"]
    target = h.shift(h.workspace("Unchanged receipt target"))["workspace"]
    policy = h.command("coordinator", "policy.create", {
        "name": "Unchanged archive receipt rule", "owner": "FM coordinator", "source": "Stamp regression",
        "starts_on": "2026-01-01", "definition": {"rule": {"kind": "restriction"}},
    })["record"]
    job = h.command("approver", "job.create", {
        "workspace_id": target["id"], "kind": "feasibility",
        "rule_changes": [{"policy_id": policy["id"], "expected_revision": policy["revision"]}],
    }, revision=target["revision"])["job"]
    assert process_one(h.app.state.engine)
    decision = activate(h, policy, job["id"], acknowledge=True)
    assert decision["records"][0]["status"] == "active"


def test_currency_hint_ignores_midnight_without_date_dependent_blocking_facts(h, monkeypatch):
    workspace = h.workspace("Stable time-bucket currency")
    with transaction(h.app.state.engine) as db:
        before = snapshots.currency_hint(db, db.get(m.Workspace, workspace["id"]))
    moment = m.now()
    monkeypatch.setattr(m, "clock_now", lambda: moment + timedelta(days=1))
    with transaction(h.app.state.engine) as db:
        after = snapshots.currency_hint(db, db.get(m.Workspace, workspace["id"]))
    assert before == after and "event_local_date" not in before


def test_currency_hint_moves_when_recurring_blocking_facts_depend_on_today(h, monkeypatch):
    event(h, event_type(h, blocks_duties=True, default_audience={"everyone": True}), repeat=repeat())
    workspace = h.workspace("Date-dependent event currency", start="2026-10-03", end="2026-10-11")
    with transaction(h.app.state.engine) as db:
        before = snapshots.currency_hint(db, db.get(m.Workspace, workspace["id"]))
    moment = m.now()
    monkeypatch.setattr(m, "clock_now", lambda: moment + timedelta(days=1))
    with transaction(h.app.state.engine) as db:
        after = snapshots.currency_hint(db, db.get(m.Workspace, workspace["id"]))
    assert before["event_local_date"] != after["event_local_date"]


def test_currency_hint_without_a_timezone_is_unresolved_and_cannot_match(h):
    workspace = h.workspace("Missing timezone currency")
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, workspace["id"])
        organisation = db.get(m.Organisation, h.seed["organisation_id"])
        hint = snapshots.hint_with_event_local_date(db, row, {
            "input_revision": organisation.input_revision,
            "availability_revision": organisation.availability_revision,
            "content_revision": row.content_revision,
        }, {"input_revision": organisation.input_revision,
            "availability_revision": organisation.availability_revision})
    assert hint["event_local_date"] == snapshots.HINT_DATE_UNRESOLVED
    assert not snapshots.currency_match(hint, hint)


def test_currency_hint_scopes_recurring_blocking_events_to_the_organisation(h, monkeypatch):
    from .governance_support import identity

    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed["organisation_id"], name="Currency scope department")
        db.add(department)
        db.flush()
        second_department_id = department.id
    identity(h, "currency_scope_coordinator", ["admin", "coordinator"], department=second_department_id)
    typ = h.configure("event_types", {
        "name": "Currency scope event type", "starts_on": "2020-01-01",
        "blocks_duties": True, "default_audience": {"everyone": True},
    }, role="currency_scope_coordinator")["record"]
    h.configure("events", {
        "event_type_id": typ["id"], "heading": "Currency scope event",
        "description": "Synthetic recurring blocker", "start": "2026-10-04T09:00:00+03:00",
        "end": "2026-10-04T10:00:00+03:00", "format": "in_person", "place": "Room B",
        "audience": {"everyone": True}, "repeat": repeat(),
    }, role="currency_scope_coordinator")
    workspace = h.workspace("Organisation-wide event currency", start="2026-10-03", end="2026-10-11")
    moment = m.now()
    monkeypatch.setattr(m, "clock_now", lambda: moment)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, workspace["id"])
        before = snapshots.currency_hint(db, row)
    monkeypatch.setattr(m, "clock_now", lambda: moment + timedelta(days=1))
    with transaction(h.app.state.engine) as db:
        after = snapshots.currency_hint(db, db.get(m.Workspace, workspace["id"]))
    assert before["event_local_date"] != after["event_local_date"]


def test_currency_hint_uses_the_shared_snapshot_window(h, monkeypatch):
    workspace = h.workspace("Shared snapshot window currency")
    calls = []
    original = snapshots.snapshot_window

    def observed(row, timezone):
        value = original(row, timezone)
        calls.append(value)
        return value

    monkeypatch.setattr(snapshots, "snapshot_window", observed)
    with transaction(h.app.state.engine) as db:
        snapshots.currency_hint(db, db.get(m.Workspace, workspace["id"]))
    assert calls
