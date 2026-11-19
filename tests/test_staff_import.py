"""Staff spreadsheet import and bulk clinics/skills: parsing, matching, per-cell errors, the role split, idempotency,
audit and undo. Expected values are worked out by hand from the seeded demo department."""
import json

import pytest

from apps.api.services.staff_import import COLUMNS, parse, split, template_csv

HEAD = ",".join(COLUMNS)


def test_import_apply_handlers_use_shared_guard():
    import ast
    import inspect
    from apps.api import commands
    from apps.api.services import common

    handlers = {}
    for route in ast.walk(ast.parse(inspect.getsource(commands))):
        if not isinstance(route, ast.If):
            continue
        kinds = [node.value for node in ast.walk(route.test) if isinstance(node, ast.Constant)
                 and isinstance(node.value, str) and node.value.endswith('.apply')
                 and 'import' in node.value.rsplit('.', 1)[0]]
        if not kinds:
            continue
        result, = [node.value for node in route.body if isinstance(node, ast.Return)]
        handler = getattr(getattr(commands, result.func.value.id), result.func.attr)
        handlers.update(dict.fromkeys(kinds, handler))
    assert {'staff_import.apply', 'import.apply'} <= handlers.keys()
    for kind, handler in handlers.items():
        handler = inspect.unwrap(handler)
        calls = [node.func for node in ast.walk(ast.parse(inspect.getsource(handler))) if isinstance(node, ast.Call)]
        assert any(isinstance(call, ast.Name) and handler.__globals__.get(call.id) is common.require_import_unapplied
                   for call in calls), kind


def csv_of(*rows):
    return HEAD + "\r\n" + "\r\n".join(",".join(f'"{v}"' for v in r) for r in rows) + "\r\n"


def row(**values):
    return [values.get(c, "") for c in COLUMNS]


def setup_catalogue(h):
    clinic = h.configure("services", {"name": "Minor injuries", "starts_on": "2020-01-01"})["record"]["id"]
    skill = h.configure("capabilities", {"name": "Suturing", "starts_on": "2020-01-01"})["record"]["id"]
    programme = h.configure("programmes", {"name": "Fellowship", "starts_on": "2026-01-01", "ends_on": "2028-01-01"})["record"]["id"]
    return clinic, skill, programme


def catalogues(h):
    return h.get("admin", "/api/catalogues")


def preview(h, text, role="coordinator", name="Staff list"):
    return h.command(role, "staff_import.preview", {"source_name": name, "csv_text": text})


def apply(h, role, batch):
    reviewed = h.get(role, '/api/staff-imports/' + batch['id'])
    return h.command(role, "staff_import.apply", {"reason": "Staff list from HR"}, batch["id"], batch["revision"],
                     expected_fingerprint=reviewed['source_fingerprint'])


def import_business_state(h):
    from sqlalchemy import select
    from apps.api import models as m
    from apps.api.db import transaction
    from apps.api.serialization import record
    with transaction(h.app.state.engine) as db:
        return {cls.__tablename__: [record(r) for r in db.scalars(select(cls).order_by(cls.id))]
                for cls in (m.Person, m.Membership, m.Eligibility, m.CapabilityGrant, m.ProgrammeMembership,
                            m.Tag, m.TagMember, m.StaffImportBatch, m.DefinitionHistory, m.CommandReceipt)}


@pytest.mark.parametrize("later_edit", ["person", "rank", "programme"])
def test_applied_import_cannot_overwrite_later_edits(h, later_edit):
    from datetime import date
    from sqlalchemy import select
    from apps.api import models as m
    from apps.api.db import transaction

    setup_catalogue(h)
    sheet = csv_of(row(display_name="Alex Morgan", title="Imported title", rank="Demo learners", rank_from="2026-10-01",
                       programme="Fellowship", programme_from="2026-10-01", programme_last_day="2027-09-30"))
    batch = apply(h, "coordinator", preview(h, sheet)["batch"])["batch"]
    assert batch["status"] == "applied"
    with transaction(h.app.state.engine, write=True) as db:
        if later_edit == "person":
            changed = db.get(m.Person, h.seed["people"][0])
            changed.title = "Edited after import"
        else:
            cls = m.Membership if later_edit == "rank" else m.ProgrammeMembership
            changed = db.scalars(select(cls).where(cls.person_id == h.seed["people"][0],
                                                   cls.starts_on == date(2026, 10, 1))).one()
            changed.ends_on = date(2026, 10, 2)
        changed.revision += 1
    reviewed = h.get("coordinator", "/api/staff-imports/" + batch["id"])
    before = import_business_state(h)
    audit = h.get("auditor", "/api/audit")["events"]
    refused = h.command("coordinator", "staff_import.apply", {"reason": "Re-apply old sheet"}, batch["id"], batch["revision"],
                        expected_fingerprint=reviewed["source_fingerprint"], status=409)
    assert refused["error"]["code"] == "IMPORT_ALREADY_APPLIED"
    assert import_business_state(h) == before
    assert h.get("auditor", "/api/audit")["events"] == audit


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_staff_import_keeps_leavers_left_and_updates_active_people(request, database):
    h = request.getfixturevalue(database)
    _, _, programme = setup_catalogue(h)
    leaver_id, current_id, *_ = h.seed['people']
    people = {p['id']: p for p in catalogues(h)['people']}
    leaver, current = people[leaver_id], people[current_id]
    h.configure('programme_memberships', {
        'person_id': leaver_id, 'programme_id': programme,
        'starts_on': '2026-01-01', 'ends_on': '2026-04-01',
    })
    leaver = h.configure('people', {
        'display_name': leaver['display_name'], 'staff_number': 'LEFT-IMPORT', 'active': False,
    }, leaver_id, leaver['revision'])['record']
    before = import_business_state(h)

    credentials = dict(clinics='Minor injuries', skills='Suturing', skill_evidence='Synthetic evidence')
    result = preview(h, csv_of(
        row(display_name=leaver['display_name'], staff_number=leaver['staff_number'], title='Attempted update',
            tags='Imported label', rank='Demo learners', rank_from='2026-10-01',
            programme='Fellowship', programme_from='2026-10-01', programme_last_day='2027-09-30', **credentials),
        row(display_name=current['display_name'], title='Imported title', **credentials)))
    batch = result['batch']
    reviewed = h.get('coordinator', '/api/staff-imports/' + batch['id'])
    left, active = reviewed['rows']
    assert left['person']['id'] == leaver_id and left['person']['action'] == 'left'
    assert left['person']['left'] is True and left['person']['changes'] == {} and left['errors'] == {}
    assert left['rank'] is None and left['programme'] is None
    assert left['clinics']['items'] == left['skills']['items'] == []
    assert active['person']['action'] == 'update' and 'left' not in active['person']
    assert reviewed['summary']['people_pending'] == 1 and reviewed['summary']['credentials_pending'] == 2

    applied = apply(h, 'coordinator', batch)
    assert applied['applied']['updated_people'] == 1
    applied = apply(h, 'credentialer', applied['batch'])
    assert applied['applied']['eligibilities'] == applied['applied']['skills'] == 1
    assert applied['batch']['status'] == 'applied'
    after = import_business_state(h)
    people = {p['id']: p for p in after['people']}
    assert people[leaver_id] == next(p for p in before['people'] if p['id'] == leaver_id)
    assert people[current_id]['active'] is True and people[current_id]['title'] == 'Imported title'
    for kind in ('memberships', 'programme_memberships', 'eligibilities', 'capability_grants'):
        assert [r for r in after[kind] if r['person_id'] == leaver_id] == [
            r for r in before[kind] if r['person_id'] == leaver_id]
    assert after['tags'] == before['tags'] and after['tag_members'] == before['tag_members']
    # Auto-schedule draws only active people from the draft snapshot, so the leaver is never scheduled.
    people_in_draft = h.snapshot(h.workspace())['people']
    assert people_in_draft[leaver_id]['active'] is False and people_in_draft[current_id]['active'] is True


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_applied_import_with_stale_revision_is_refused_without_writes(request, database):
    h = request.getfixturevalue(database)
    reviewed = preview(h, csv_of(row(display_name='Applied from another review')))
    batch = reviewed['batch']
    applied = apply(h, 'coordinator', batch)['batch']
    assert applied['status'] == 'applied' and applied['revision'] != batch['revision']
    audit = h.get('auditor', '/api/audit')['events']
    before = import_business_state(h)
    refused = h.command('coordinator', 'staff_import.apply', {'reason': 'Stale review'}, batch['id'], batch['revision'],
                        expected_fingerprint=reviewed['source_fingerprint'], status=409)
    assert refused['error']['code'] == 'IMPORT_ALREADY_APPLIED'
    assert import_business_state(h) == before
    assert h.get('auditor', '/api/audit')['events'] == audit


@pytest.mark.parametrize("in_department", [True, False])
def test_application_display_names_are_scoped_and_not_stored(h, in_department):
    from apps.api import models as m
    from apps.api.db import transaction

    batch = apply(h, "coordinator", preview(h, csv_of(row(display_name="Imported person")))["batch"])["batch"]
    with transaction(h.app.state.engine, write=True) as db:
        if in_department:
            identity = db.get(m.Identity, h.seed["identities"]["coordinator"])
        else:
            identity = m.Identity(issuer="test", subject="foreign-import-actor", display_name="Foreign identity")
            db.add(identity)
            db.flush()
            stored = db.get(m.StaffImportBatch, batch["id"])
            stored.applied = {"applications": [{**entry, "by": identity.id} for entry in stored.applied["applications"]]}
        name = identity.display_name
    before = import_business_state(h)
    shown = h.get("coordinator", "/api/staff-imports/" + batch["id"])["batch"]
    entry, = shown["applied"]["applications"]
    assert entry["by_name"] == (name if in_department else None)
    assert import_business_state(h) == before
    assert "by_name" not in before["staff_import_batches"][0]["applied"]["applications"][0]


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('zero_change', [False, True])
@pytest.mark.parametrize('finisher', ['credentialer', 'admin'])
def test_finished_people_part_preserves_later_edits_and_leaves_credentials_open(request, database, zero_change, finisher):
    from datetime import date
    from apps.api import models as m
    from apps.api.db import transaction

    h = request.getfixturevalue(database)
    setup_catalogue(h)
    reviewed = preview(h, csv_of(row(display_name='Alex Morgan', title='' if zero_change else 'Imported title',
                                     clinics='Minor injuries', clinics_from='2026-10-01')))
    batch = apply(h, 'coordinator', reviewed['batch'])['batch']
    assert batch['status'] == 'partly_applied'
    assert h.get('coordinator', '/api/staff-imports/' + batch['id'])['batch']['finished_parts'] == ['people']
    entry, = batch['applied']['applications']
    assert bool(entry['people_updated']) is not zero_change
    with transaction(h.app.state.engine, write=True) as db:
        person = db.get(m.Person, h.seed['people'][0])
        person.title = 'Edited after people applied'
        person.revision += 1
    current = h.get('coordinator', '/api/staff-imports/' + batch['id'])
    audit = h.get('auditor', '/api/audit')['events']
    before = import_business_state(h)
    refused = h.command('coordinator', 'staff_import.apply', {'reason': 'Repeat people part'}, batch['id'], batch['revision'],
                        expected_fingerprint=current['source_fingerprint'], status=409)
    assert refused['error']['code'] == 'IMPORT_ALREADY_APPLIED'
    assert import_business_state(h) == before and h.get('auditor', '/api/audit')['events'] == audit
    if finisher == 'admin':
        with transaction(h.app.state.engine, write=True) as db:
            db.add(m.RoleBinding(identity_id=h.seed['identities']['admin'], department_id=h.seed['department_id'],
                                 role='credentialer', starts_on=date(2020, 1, 1)))
    done = apply(h, finisher, batch)
    assert done['applied']['eligibilities'] == 1 and done['applied']['updated_people'] == 0
    assert done['batch']['status'] == 'applied'
    assert next(p for p in catalogues(h)['people'] if p['id'] == h.seed['people'][0])['title'] == 'Edited after people applied'
    assert h.get('coordinator', '/api/staff-imports/' + batch['id'])['batch']['finished_parts'] == ['credentials', 'people']


def test_zero_change_credentials_part_is_finished_and_can_be_undone(h):
    batch = preview(h, csv_of(row(display_name='Alex Morgan', title='Awaiting people part')))['batch']
    applied = apply(h, 'credentialer', batch)
    assert not any(applied['applied'].values()) and applied['batch']['status'] == 'partly_applied'
    shown = h.get('credentialer', '/api/staff-imports/' + batch['id'])['batch']
    assert shown['finished_parts'] == ['credentials']
    entry, = shown['applied']['applications']
    assert entry['created'] == [] and entry['waiting_rows'] == []
    audit = h.get('auditor', '/api/audit')['events']
    before = import_business_state(h)
    refused = h.command('credentialer', 'staff_import.apply', {'reason': 'Repeat empty part'}, batch['id'],
                        applied['batch']['revision'], status=409)
    assert refused['error']['code'] == 'IMPORT_ALREADY_APPLIED'
    assert import_business_state(h) == before and h.get('auditor', '/api/audit')['events'] == audit
    undone = h.command('credentialer', 'staff_import.undo', {'reason': 'Reopen empty part'}, batch['id'], shown['revision'])
    assert undone['reverted'] == 0
    assert h.get('credentialer', '/api/staff-imports/' + batch['id'])['batch']['finished_parts'] == []
    assert apply(h, 'credentialer', undone['batch'])['batch']['status'] == 'partly_applied'


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_reapplying_an_undone_import_switches_its_own_people_back_on(request, database):
    h = request.getfixturevalue(database)
    batch = apply(h, "coordinator", preview(h, csv_of(row(display_name="Undo then return")))["batch"])["batch"]
    added = next(p for p in catalogues(h)["people"] if p["display_name"] == "Undo then return")
    assert added["active"] is True
    undone = h.command("coordinator", "staff_import.undo", {"reason": "Undo import"}, batch["id"], batch["revision"])["batch"]
    assert next(p for p in catalogues(h)["people"] if p["id"] == added["id"])["active"] is False
    # The person this batch added is not a leaver: reviewing the same batch offers to switch them back on.
    reviewed = h.get("coordinator", "/api/staff-imports/" + batch["id"])
    (planned,) = reviewed["rows"]
    assert planned["person"]["id"] == added["id"] and planned["person"]["action"] == "update"
    assert "left" not in planned["person"] and planned["person"]["changes"]["active"] == [False, True]
    assert reviewed["summary"]["people_pending"] == 1
    assert apply(h, "coordinator", undone)["batch"]["status"] == "applied"
    assert next(p for p in catalogues(h)["people"] if p["id"] == added["id"])["active"] is True


def test_undone_import_can_be_reapplied_with_current_revision_and_fingerprint(h):
    reviewed = preview(h, csv_of(row(display_name="Undo and re-apply")))
    batch = apply(h, "coordinator", reviewed["batch"])["batch"]
    undone = h.command("coordinator", "staff_import.undo", {"reason": "Undo import"}, batch["id"], batch["revision"])["batch"]
    assert undone["status"] == "undone"
    current = h.get("coordinator", "/api/staff-imports/" + batch["id"])
    before = import_business_state(h)
    stale_revision = h.command("coordinator", "staff_import.apply", {"reason": "Re-apply"}, batch["id"], batch["revision"],
                               expected_fingerprint=current["source_fingerprint"], status=409)
    assert stale_revision["error"]["code"] == "STALE_REVISION"
    stale_plan = h.command("coordinator", "staff_import.apply", {"reason": "Re-apply"}, batch["id"], undone["revision"],
                           expected_fingerprint=reviewed["source_fingerprint"], status=409)
    assert stale_plan["error"]["code"] == "STALE_PREVIEW"
    assert import_business_state(h) == before
    assert apply(h, "coordinator", undone)["batch"]["status"] == "applied"


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('drift', ['target', 'reference', 'rematch', 'alias', 'date', 'actor', 'missing', 'scope'])
def test_apply_requires_the_exact_reviewed_targets_references_date_and_actor(request, database, drift, monkeypatch):
    from datetime import date, timedelta
    from apps.api import models as m
    from apps.api.db import transaction
    from apps.api.services import staff_import
    h = request.getfixturevalue(database)
    setup_catalogue(h)
    on = date(2026, 10, 4)
    monkeypatch.setattr(staff_import, 'today', lambda db, dept: on)
    person = next(p for p in catalogues(h)['people'] if p['id'] == h.seed['people'][0])
    sheet = csv_of(row(display_name=person['display_name'], title='Reviewed title', clinics='Minor injuries'))
    reviewed = preview(h, sheet)
    assert reviewed['rows'][0]['errors'] == {}
    fingerprint = reviewed['source_fingerprint']
    if drift in {'target', 'reference'}:
        with transaction(h.app.state.engine, write=True) as db:
            if drift == 'target':
                record = db.get(m.Person, person['id'])
                record.title = 'Another operator changed this'
            else:
                from sqlalchemy import select
                record = db.scalar(select(m.Service).where(m.Service.name == 'Minor injuries',
                                                          m.Service.department_id == h.seed['department_id']))
                record.name = 'Renamed clinic after review'
            record.revision += 1
    elif drift == 'rematch':
        h.configure('people', {'display_name': person['display_name'], 'staff_number': 'another-person'})
    elif drift == 'alias':
        with transaction(h.app.state.engine, write=True) as db:
            db.add(m.SourceMapping(department_id=h.seed['department_id'], source_name='Synthetic retained source',
                source_key='alias:Another retained label', target_kind='people', target_id=person['id']))
    elif drift == 'scope':
        with transaction(h.app.state.engine, write=True) as db:
            foreign = m.Department(organisation_id=h.seed['organisation_id'], name='Foreign import scope (test)')
            db.add(foreign)
            db.flush()
            db.get(m.StaffImportBatch, reviewed['batch']['id']).department_id = foreign.id
    elif drift == 'date':
        on += timedelta(days=1)
    role = 'admin' if drift == 'actor' else 'coordinator'
    h.client(role)
    before = import_business_state(h)
    batch = reviewed['batch']
    refused = h.command(role, 'staff_import.apply', {'reason': 'Apply the reviewed sheet'}, batch['id'], batch['revision'],
        expected_fingerprint=None if drift == 'missing' else fingerprint, status=404 if drift == 'scope' else 409)
    assert refused['error']['code'] == ('NOT_FOUND' if drift == 'scope' else 'STALE_PREVIEW')
    assert import_business_state(h) == before
    if drift == 'scope':
        return
    fresh = h.get(role, '/api/staff-imports/' + batch['id'])
    if drift != 'missing':
        assert fresh['source_fingerprint'] != fingerprint
    if drift not in {'reference', 'rematch'}:
        applied = h.command(role, 'staff_import.apply', {'reason': 'New explicit review'}, batch['id'], batch['revision'],
            expected_fingerprint=fresh['source_fingerprint'])
        assert applied['applied']['updated_people'] == 1


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_reviewed_apply_replay_is_exact_and_a_failed_batch_has_zero_business_writes(request, database, monkeypatch):
    from apps.api.errors import Problem
    from apps.api.services import catalogue
    h = request.getfixturevalue(database)
    reviewed = preview(h, csv_of(row(display_name='Reviewed new A'), row(display_name='Reviewed new B')))
    batch = reviewed['batch']
    original = catalogue.write_catalogue
    calls = []
    def fail_second(*args, **kwargs):
        saved = original(*args, **kwargs)
        calls.append(saved)
        if len(calls) == 2:
            raise Problem(409, 'SYNTHETIC_BATCH_FAILURE', 'Failure after the second business write')
        return saved
    h.client('coordinator')
    before = import_business_state(h)
    monkeypatch.setattr(catalogue, 'write_catalogue', fail_second)
    result = h.command('coordinator', 'staff_import.apply', {'reason': 'Reviewed batch'}, batch['id'], batch['revision'],
        expected_fingerprint=reviewed['source_fingerprint'], status=409)
    assert result['error']['code'] == 'SYNTHETIC_BATCH_FAILURE' and len(calls) == 2
    assert import_business_state(h) == before
    monkeypatch.setattr(catalogue, 'write_catalogue', original)
    import uuid
    key = str(uuid.uuid4())
    arguments = dict(role='coordinator', kind='staff_import.apply', payload={'reason': 'Reviewed batch'},
        target=batch['id'], revision=batch['revision'], expected_fingerprint=reviewed['source_fingerprint'], key=key)
    first = h.command(**arguments)
    assert first['applied']['new_people'] == 2
    after = import_business_state(h)
    assert h.command(**arguments) == first
    assert import_business_state(h) == after
    h.command(**{**arguments, 'payload': {'reason': 'Changed retry'}, 'status': 409})
    assert import_business_state(h) == after


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_two_clients_cannot_apply_the_same_reviewed_batch_twice(request, database):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from fastapi.testclient import TestClient
    import uuid
    h = request.getfixturevalue(database)
    reviewed = preview(h, csv_of(row(display_name='One reviewed person')))
    source = h.client('coordinator')
    start = Barrier(2)
    def send(_):
        with TestClient(h.app, base_url=h.settings.public_origin) as client:
            client.cookies.update(source.cookies)
            client.headers.update({'origin': h.settings.public_origin, 'x-csrf-token': source.cookies['rota_csrf']})
            start.wait(timeout=10)
            return client.post('/api/commands', headers={'idempotency-key': str(uuid.uuid4())}, json={
                'kind': 'staff_import.apply', 'payload': {'reason': 'Reviewed in two sessions'},
                'target_id': reviewed['batch']['id'], 'expected_revision': reviewed['batch']['revision'],
                'expected_fingerprint': reviewed['source_fingerprint']})
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(send, range(2)))
    assert sorted(r.status_code for r in replies) == [200, 409]
    assert len([p for p in catalogues(h)['people'] if p['display_name'] == 'One reviewed person']) == 1
    saved = h.get('coordinator', '/api/staff-imports/' + reviewed['batch']['id'])
    assert len(saved['batch']['applied']['applications']) == 1


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('first,last,exclusive_end', [
    ('9999-12-30', '9999-12-31', None),
    ('9999-12-30', '9999-12-30', '9999-12-31'),
    ('2028-02-01', '2028-02-29', '2028-03-01'),
    ('2026-04-01', '2026-04-30', '2026-05-01'),
])
def test_programme_last_day_boundary_is_a_cell_error_and_preserves_the_batch(request, database, first, last, exclusive_end):
    """PCF-S05: an inclusive date needs a representable exclusive endpoint in every planner consumer."""
    h = request.getfixturevalue(database)
    h.configure('programmes', {'name': 'Date boundary programme', 'starts_on': '2020-01-01'})
    before = catalogues(h)
    sheet = csv_of(row(display_name='Alex Morgan', programme='Date boundary programme',
                       programme_from=first, programme_last_day=last))
    result = preview(h, sheet)
    entry, = result['rows']
    if exclusive_end is not None:
        assert entry['errors'] == {}
        assert entry['programme']['ends_on'] == exclusive_end
        return
    error = 'Use a last day before 9999-12-31'
    assert entry['errors']['programme_last_day'] == error and entry['programme'] is None
    reopened = h.get('coordinator', '/api/staff-imports/' + result['batch']['id'])
    assert reopened['rows'][0]['errors']['programme_last_day'] == error
    refused = h.command('coordinator', 'staff_import.apply', {'reason': 'Synthetic invalid date boundary'},
                        result['batch']['id'], result['batch']['revision'], status=422)
    assert refused['error']['code'] == 'IMPORT_ROW_ERRORS'
    assert refused['error']['details']['rows'][0]['errors']['programme_last_day'] == error
    after = catalogues(h)
    assert after['people'] == before['people']
    assert after['programme_memberships'] == before['programme_memberships']
    assert h.get('coordinator', '/api/staff-imports/' + result['batch']['id'])['batch']['status'] == 'preview'


ALEX = row(display_name="Alex Morgan", title="Consultant", rank="Demo learners", rank_from="2026-10-01",
           clinics="Continuity clinic; Minor injuries", skills="Demo supervision; Suturing", skill_evidence="Logbook reviewed 2026")


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('independent_change', [None, 'person', 'tag_membership'])
def test_import_undo_reconciles_existing_person_tags_and_preserves_independent_changes(request, database, independent_change):
    from sqlalchemy import select
    from apps.api import models as m
    from apps.api.db import transaction
    h = request.getfixturevalue(database)
    person_id = h.seed['people'][0]
    original = next(p for p in catalogues(h)['people'] if p['id'] == person_id)
    tag = h.configure('tags', {'name': 'Shared import label', 'labels': ['people']})['record']
    other = h.seed['people'][1]
    tag = h.command('admin', 'tag.members', {'add': [{'kind': 'people', 'id': other}],
        'reason': 'Independent existing label member'}, tag['id'], tag['revision'])['record']
    sheet = csv_of(row(display_name=original['display_name'], tags=';'.join([*original['tags'], tag['name']])))
    applied = apply(h, 'coordinator', preview(h, sheet)['batch'])['batch']
    data = catalogues(h)
    assert tag['name'] in next(p for p in data['people'] if p['id'] == person_id)['tags']
    assert any(r['tag_id'] == tag['id'] and r['member_id'] == person_id for r in data['tag_members'])
    if independent_change == 'person':
        from packages.contracts.schemas import PersonInput
        person = next(p for p in data['people'] if p['id'] == person_id)
        values = {k: person[k] for k in PersonInput.model_fields if k in person}
        h.configure('people', {**values, 'title': 'Independent operator edit'}, person_id, person['revision'])
    elif independent_change == 'tag_membership':
        current = next(t for t in data['tags'] if t['id'] == tag['id'])
        h.command('admin', 'tag.members', {'remove': [{'kind': 'people', 'id': person_id}],
            'reason': 'Independent membership removal'}, tag['id'], current['revision'])
    before_undo = catalogues(h)
    h.command('credentialer', 'staff_import.undo', {'reason': 'Wrong people sheet'}, applied['id'], applied['revision'], status=409)
    result = h.command('coordinator', 'staff_import.undo', {'reason': 'Undo imported person labels'}, applied['id'], applied['revision'])
    after = catalogues(h)
    person = next(p for p in after['people'] if p['id'] == person_id)
    if independent_change:
        assert result['kept'] == 1 and result['reverted'] == 0
        assert after['people'] == before_undo['people'] and after['tag_members'] == before_undo['tag_members']
    else:
        assert result['reverted'] == 1 and result['kept'] == 0
        assert person['tags'] == original['tags']
        assert not any(r['tag_id'] == tag['id'] and r['member_id'] == person_id for r in after['tag_members'])
        with transaction(h.app.state.engine) as db:
            assert db.scalar(select(m.DefinitionHistory.id).where(m.DefinitionHistory.kind == 'tag.member_remove',
                m.DefinitionHistory.snapshot['member_id'].as_string() == person_id))
    assert any(t['id'] == tag['id'] for t in after['tags'])
    assert any(r['tag_id'] == tag['id'] and r['member_id'] == other for r in after['tag_members'])


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_import_undo_tag_reconciliation_failure_rolls_back_person_membership_and_batch(request, database, monkeypatch):
    from apps.api.services import tags
    from apps.api.errors import Problem
    h = request.getfixturevalue(database)
    batch = apply(h, 'coordinator', preview(h, csv_of(row(display_name='Alex Morgan', tags='Rollback label')))['batch'])['batch']
    before = catalogues(h)
    saved = h.get('coordinator', '/api/staff-imports/' + batch['id'])
    original = tags.sync_person
    def fail_after_reconciliation(*args, **kwargs):
        original(*args, **kwargs)
        raise Problem(409, 'SYNTHETIC_TAG_FAILURE', 'Synthetic rollback after tag reconciliation')
    monkeypatch.setattr(tags, 'sync_person', fail_after_reconciliation)
    h.command('coordinator', 'staff_import.undo', {'reason': 'Verify transaction rollback'}, batch['id'], batch['revision'], status=409)
    assert catalogues(h) == before
    assert h.get('coordinator', '/api/staff-imports/' + batch['id']) == saved
NEW = row(display_name="Dr Nadia Example", short_code="nex", staff_number="9001", tags="Night team; Teaching",
          rank="Demo practitioners", rank_from="2026-10-01", clinics="Minor injuries", skills="Suturing",
          skill_evidence="Credentialing committee 2026-09", programme="Fellowship", programme_from="2026-10-01", programme_last_day="2027-09-30")


def test_template_and_parsing_are_forgiving_about_headers_but_not_unknown_columns():
    lines = template_csv().splitlines()
    assert lines[0] == HEAD and len(parse(template_csv())) == 1
    assert json.loads(parse(template_csv())[0][1]['rota_aliases']) == ['Alternate label', 'Label; with punctuation']
    rows = parse("Display Name,Short-Code\r\nAlex,ab\r\n,\r\n")
    assert rows == [(2, {"display_name": "Alex", "short_code": "ab"})]  # blank rows skipped, headers normalised
    assert split(" a ; b| a ;; ") == ["a", "b"]
    # Excel's "CSV" in semicolon locales, and tab-separated sheets (several clinics then go in quotes or use |).
    expected = [(2, {"display_name": "Alex", "clinics": "Minor injuries; Review"})]
    assert parse('﻿display_name;clinics\r\nAlex;"Minor injuries; Review"\r\n') == expected
    assert parse("display_name\tclinics\r\nAlex\tMinor injuries; Review\r\n") == expected


def test_preview_matches_rows_and_reports_errors_per_cell(h):
    setup_catalogue(h)
    bad = row(display_name="Taylor Noor", rank="Registrar", rank_from="01/10/2026", skills="Suturing", email="not-an-email")
    clash = row(display_name="Someone Else", short_code="NEX")
    result = preview(h, csv_of(ALEX, NEW, bad, clash))
    alex, new, taylor, other = result["rows"]
    assert alex["person"] == {"id": h.seed["people"][0], "by": "display_name", "action": "update", "changes": {"title": ["", "Consultant"]}}
    assert alex["rank"]["action"] == "add" and len(alex["rank"]["ends"]) == 1  # moving from the seeded rank
    assert [i["action"] for i in alex["clinics"]["items"]] == ["same", "add"]
    assert [i["action"] for i in alex["skills"]["items"]] == ["same", "add"]
    assert new["person"]["action"] == "new" and new["programme"]["ends_on"] == "2027-10-01" and not new["errors"]
    assert set(taylor["errors"]) == {"rank", "rank_from", "skill_evidence", "email"}
    assert "Same short code as row 3" in other["errors"]["short_code"]
    assert result["summary"] == {"new_people": 1, "updated_people": 1, "rank_changes": 2, "programme_memberships": 1,
                                 "eligibilities": 2, "skills": 2, "rows": 4, "error_rows": 2, "people_pending": 5, "credentials_pending": 4}
    # Rows with errors block applying the batch.
    h.command("coordinator", "staff_import.apply", {"reason": "Try"}, result["batch"]["id"], result["batch"]["revision"], status=422)
    h.command("coordinator", "staff_import.preview", {"source_name": "x", "csv_text": "name,colour\r\nA,red\r\n"}, status=422)


def test_apply_splits_by_role_is_idempotent_audited_and_undoable(h):
    clinic, skill, programme = setup_catalogue(h)
    text = csv_of(ALEX, NEW)
    batch = preview(h, text)["batch"]
    assert preview(h, text)["batch"]["id"] == batch["id"]  # the same sheet reopens its open preview
    h.command("staff", "staff_import.preview", {"source_name": "x", "csv_text": text}, status=403)
    h.command("approver", "staff_import.apply", {"reason": "Try"}, batch["id"], batch["revision"], status=403)

    first = apply(h, "coordinator", batch)
    assert first["applied"] == {"new_people": 1, "updated_people": 1, "rank_changes": 2, "programme_memberships": 1,
                                     "eligibilities": 0, "skills": 0, "waiting_for_people": 0, "own": 0}
    assert first["pending"]["credentials_pending"] == 4 and first["pending"]["people_pending"] == 0
    assert first["batch"]["status"] == "partly_applied" and "need a credentialer" in first["message"]
    data = catalogues(h)
    nadia = next(p for p in data["people"] if p["display_name"] == "Dr Nadia Example")
    assert nadia["short_code"] == "NEX" and nadia["staff_number"] == "9001" and nadia["tags"] == ["Night team", "Teaching"]
    alex_ranks = [r for r in data["memberships"] if r["person_id"] == h.seed["people"][0]]
    assert sorted((r["category_id"], r["ends_on"]) for r in alex_ranks) == sorted([(h.seed["category"], "2026-10-01"), (h.seed["trainee_category"], None)])
    assert any(r["person_id"] == nadia["id"] and r["programme_id"] == programme for r in data["programme_memberships"])

    # A partly applied batch stays open only for its pending credentials.
    refused = h.command("coordinator", "staff_import.apply", {"reason": "Repeat people"}, batch["id"],
                        first["batch"]["revision"], status=409)
    assert refused["error"]["code"] == "IMPORT_ALREADY_APPLIED"
    second = apply(h, "credentialer", first["batch"])
    assert second["applied"]["eligibilities"] == 2 and second["applied"]["skills"] == 2 and second["batch"]["status"] == "applied"
    data = catalogues(h)
    assert {r["person_id"] for r in data["eligibilities"] if r["service_id"] == clinic} == {h.seed["people"][0], nadia["id"]}
    grant = next(r for r in data["grants"] if r["person_id"] == nadia["id"] and r["capability_id"] == skill)
    assert grant["evidence"] == "Credentialing committee 2026-09"
    before = import_business_state(h)
    audit = h.get("auditor", "/api/audit")["events"]
    reviewed = h.get("coordinator", "/api/staff-imports/" + batch["id"])
    refused = h.command("coordinator", "staff_import.apply", {"reason": "Apply again"}, batch["id"],
                        second["batch"]["revision"], expected_fingerprint=reviewed["source_fingerprint"], status=409)
    assert refused["error"]["code"] == "IMPORT_ALREADY_APPLIED"
    assert import_business_state(h) == before
    assert h.get("auditor", "/api/audit")["events"] == audit
    # The identical sheet imported again finds everything current.
    rerun = preview(h, text)
    assert rerun["batch"]["id"] != batch["id"] and rerun["summary"]["people_pending"] == rerun["summary"]["credentials_pending"] == 0
    actions = [e["action"] for e in h.get("auditor", "/api/audit")["events"]]
    assert actions.count("staff_import.apply") == 2 and "staff_import.preview" in actions

    # Undo, part by part as each role may: the credentialer's clinics and skills, then the coordinator's people and ranks.
    latest = h.get("coordinator", "/api/staff-imports/" + batch["id"])["batch"]
    h.command("coordinator", "staff_import.undo", {"reason": "Wrong file"}, latest["id"], latest["revision"])
    latest = h.get("credentialer", "/api/staff-imports/" + batch["id"])["batch"]
    undone = h.command("credentialer", "staff_import.undo", {"reason": "Wrong file"}, latest["id"], latest["revision"])
    assert undone["reverted"] == 4 and undone["batch"]["status"] == "undone"
    data = catalogues(h)
    assert not any(r["service_id"] == clinic for r in data["eligibilities"]) and not any(r["capability_id"] == skill for r in data["grants"])
    alex = next(p for p in data["people"] if p["id"] == h.seed["people"][0])
    assert alex["title"] == "" and next(p for p in data["people"] if p["id"] == nadia["id"])["active"] is False
    assert [r["ends_on"] for r in data["memberships"] if r["person_id"] == alex["id"]] == [None]
    h.command("credentialer", "staff_import.undo", {"reason": "Again"}, undone["batch"]["id"], undone["batch"]["revision"], status=409)


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_credentialer_first_waits_for_new_people_and_never_self_attests(request, database):
    h = request.getfixturevalue(database)
    setup_catalogue(h)
    batch = preview(h, csv_of(ALEX, NEW), role="credentialer")["batch"]
    early = apply(h, "credentialer", batch)
    assert early["applied"]["eligibilities"] == 1 and early["applied"]["waiting_for_people"] == 1
    assert "need a coordinator" in early["message"]
    after = apply(h, "coordinator", early["batch"])
    assert after["pending"]["credentials_pending"] == 2
    done = apply(h, "credentialer", after["batch"])
    assert done["applied"]["eligibilities"] == 1 and done["applied"]["skills"] == 1 and done["batch"]["status"] == "applied"


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('ended_before_start', [False, True])
def test_credentials_followup_only_applies_waiting_rows_and_preserves_ended_credentials(request, database, ended_before_start):
    from datetime import timedelta
    from apps.api import models as m
    from apps.api.db import transaction
    from apps.api.serialization import record

    h = request.getfixturevalue(database)
    setup_catalogue(h)
    batch = preview(h, csv_of(ALEX, NEW), role='credentialer')['batch']
    first = apply(h, 'credentialer', batch)['batch']
    entry, = first['applied']['applications']
    assert entry['waiting_rows'] == [3]
    assert h.get('credentialer', '/api/staff-imports/' + batch['id'])['batch']['finished_parts'] == []
    with transaction(h.app.state.engine, write=True) as db:
        earlier = db.get(m.Eligibility, entry['created'][0]['id'])
        earlier.ends_on = earlier.starts_on + timedelta(days=1)
        if ended_before_start:
            earlier.ends_on = earlier.starts_on
            earlier.starts_on -= timedelta(days=1)
        earlier.revision += 1
        ended = record(earlier)
    after = apply(h, 'coordinator', first)['batch']
    reviewed = h.get('credentialer', '/api/staff-imports/' + batch['id'])
    assert reviewed['summary']['error_rows'] == 0
    done = apply(h, 'credentialer', after)
    assert done['applied']['eligibilities'] == 1 and done['applied']['skills'] == 1
    assert done['batch']['status'] == 'applied'
    eligibilities = catalogues(h)['eligibilities']
    assert next(r for r in eligibilities if r['id'] == ended['id']) == ended
    assert len([r for r in eligibilities if r['person_id'] == ended['person_id'] and r['service_id'] == ended['service_id']]) == 1


def test_bulk_clinics_and_skills_for_selected_people(h):
    clinic, skill, _ = setup_catalogue(h)
    people = h.seed["people"]
    payload = {"person_ids": people, "service_ids": [clinic, h.seed["services"][0]], "starts_on": "2026-10-01", "reason": "Onboarding"}
    h.command("coordinator", "eligibility.bulk_create", payload, status=403)
    made = h.command("credentialer", "eligibility.bulk_create", payload)
    assert len(made["created"]) == 3 and made["already_current"] == 3
    assert h.command("credentialer", "eligibility.bulk_create", payload)["created"] == []  # idempotent
    h.command("credentialer", "grant.bulk_create", {"person_ids": people, "capability_ids": [skill], "starts_on": "2026-10-01", "reason": "x" * 3}, status=422)
    grants = h.command("credentialer", "grant.bulk_create", {"person_ids": people, "capability_ids": [skill], "evidence": "Course certificate",
                                                              "starts_on": "2026-10-01", "reason": "Onboarding"})
    assert len(grants["created"]) == 3
    undo = h.command("credentialer", "eligibility.undo_create", {"items": made["created"], "reason": "Undo"})
    assert undo["removed"] == 3
    assert not any(r["service_id"] == clinic for r in catalogues(h)["eligibilities"])
    # Undo only removes one's own unchanged records.
    assert h.command("admin", "grant.undo_create", {"items": grants["created"], "reason": "Undo"})["kept"] == 3


def test_two_new_rows_with_the_same_name_and_nothing_else_are_refused(h):
    """Only a staff number or short code tells two new people with one name apart; without them the sheet would add one person twice."""
    rows = preview(h, csv_of(row(display_name="Dr Twin"), row(display_name="dr twin"), row(display_name="Dr Twin", staff_number="7001")))["rows"]
    assert rows[0]["errors"] == {} and rows[2]["errors"] == {}
    assert rows[1]["errors"]["display_name"].startswith("Same name as row 2")


@pytest.mark.parametrize("column", ["email", "work_email"])
def test_staff_import_saves_work_email_and_keeps_it_for_blank_cells(h, column):
    text = csv_of(row(display_name="Email recipient (test)", **{column: "recipient@example.test"}))
    initial = preview(h, text)
    assert initial["rows"][0]["person"]["changes"]["work_email"] == ["", "recipient@example.test"]
    apply(h, "coordinator", initial["batch"])
    person = next(p for p in catalogues(h)["people"] if p["display_name"] == "Email recipient (test)")
    assert person["work_email"] == "recipient@example.test"
    assert preview(h, text)["summary"]["people_pending"] == 0
    assert preview(h, csv_of(row(display_name=person["display_name"])))["rows"][0]["person"]["action"] == "same"

    changed = preview(h, csv_of(row(display_name=person["display_name"], **{column: "new@example.test"})))
    updated = apply(h, "coordinator", changed["batch"])
    assert next(p for p in catalogues(h)["people"] if p["id"] == person["id"])["work_email"] == "new@example.test"
    h.command("coordinator", "staff_import.undo", {"reason": "Wrong work email"}, updated["batch"]["id"], updated["batch"]["revision"])
    assert next(p for p in catalogues(h)["people"] if p["id"] == person["id"])["work_email"] == "recipient@example.test"


def test_staff_import_validates_work_email_with_shared_contract(h):
    result = preview(h, csv_of(
        row(display_name="Invalid email (test)", work_email="bad\"@example.test"),
        row(display_name="Long email (test)", work_email="a" * 242 + "@example.test"),
        row(display_name="Two emails (test)", email="one@example.test", work_email="two@example.test")))
    assert all("work_email" in entry["errors"] for entry in result["rows"])


def test_person_work_email_is_editable_clearable_and_preserved_when_omitted(h):
    person = h.configure("people", {"display_name": "Email recipient (test)", "work_email": "recipient@example.test"})["record"]
    assert next(p for p in catalogues(h)["people"] if p["id"] == person["id"])["work_email"] == "recipient@example.test"
    edited = h.configure("people", {"display_name": "Renamed recipient (test)"}, person["id"], person["revision"])["record"]
    assert edited["work_email"] == person["work_email"]
    h.configure("people", {"display_name": edited["display_name"], "work_email": "invalid"}, edited["id"], edited["revision"], status=422)
    cleared = h.configure("people", {"display_name": edited["display_name"], "work_email": ""}, edited["id"], edited["revision"])["record"]
    assert cleared["work_email"] == ""
