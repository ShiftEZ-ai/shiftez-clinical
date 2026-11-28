"""The Schedule read is a lossless write source and a bounded projection of full detail."""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import re
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest
from sqlalchemy import select

from apps.api import main, models as m
from apps.api.db import transaction
from apps.api.serialization import record
from apps.api.snapshots import shift_record
from apps.api.services import planning
from packages.contracts import schemas as s
from packages.domain.time import aware
from packages.domain.validation import check_contracts
from .governance_support import identity
from .test_backend_scale import scale_department
from .test_bulk import preview
from .test_bulk_workspace_sources import apply
from .test_fix_list_performance import backend


EDITABLE = ('assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')
PARTICIPATION = ('person_id', 'start', 'end', 'segments', 'participation', 'fixed', 'overtime', 'team_lead',
                 'supervisor_person_id')
IDENTITY = {'accept-encoding': 'identity'}
REASON = 'Exercise a Schedule edit'
BUDGET = {'draft': 1_900_000, 'checked': 2_500_000}


def test_schedule_detail_fields_do_not_enter_write_contracts():
    detail = s.SCHEDULE_DETAIL_FIELDS
    assert set(EDITABLE) | {'participants'} == s.ShiftInput.model_fields.keys()
    assert set(PARTICIPATION) == s.ParticipantInput.model_fields.keys()
    # Routing IDs come from workspace.id and shifts[].id, never the session's parent ID
    # or the participant's parent ID. Neither is writable record content.
    routing = {s.ShiftCreate: {'workspace_id'}, s.ShiftEdit: {'workspace_id', 'shift_id'},
               s.ShiftAction: {'workspace_id', 'shift_ids'}, s.ShiftCopy: {'workspace_id', 'shift_ids'}}
    session_fields = set(s.ShiftInput.model_fields)
    for model, identifiers in routing.items():
        session_fields.update(model.model_fields.keys() - identifiers)
    session_fields.update(key for key, value in s.BULK_COLLECTIONS['sessions']['columns'].items()
                          if not value.get('read_only'))
    assert not session_fields.intersection(detail['session'])
    assert not s.ParticipantInput.model_fields.keys() & set(detail['participant'])
    assert not set(detail['check'] + detail['coverage']) & (session_fields | set(s.ParticipantInput.model_fields))
    assert not s.ScheduleSession.model_fields.keys() & set(detail['session'])
    assert not s.ScheduleParticipant.model_fields.keys() & set(detail['participant'])


def test_the_client_constructs_whole_rota_urls_only_in_rotawhole():
    # A literal followed by a path segment addresses a different resource. A
    # query may choose compact or schedule, but the choice lives in api.ts.
    source = Path(__file__).resolve().parents[1] / 'apps/web/src'
    root = re.compile(r"""['"]/api/workspaces/['"][^\n]+|`/api/workspaces/\$\{[^}]+\}[^`]*`""")
    uses = [(path.name, match.group(0)) for path in source.glob('*.ts') for match in root.finditer(
        path.read_text(encoding='utf-8'))]
    whole = [(file, call) for file, call in uses if not re.search(
        r"""(?:\}/|['"]/api/workspaces/['"].*?\+\s*['"]/)[\w-]""", call)]
    assert len(whole) == 1 and whole[0][0] == 'api.ts' and "'?view='+view" in whole[0][1], whole
    assert (source / 'api.ts').read_text(encoding='utf-8').count("rotaPath(id,'schedule')") == 1


def schedule(h, path):
    response = h.client('coordinator').get(path, params={'view': 'schedule'}, headers=IDENTITY)
    assert response.status_code == 200
    return response


@pytest.fixture(params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def scale_rota(scale_harness, request):
    # The checked and edited scenarios change the data that determines the draft response size.
    h = scale_harness(request.param)
    w, _, _, _ = scale_department(h)
    return h, '/api/workspaces/' + w['id']


def test_schedule_retains_authoritative_check_currency(backend):
    h = backend
    w = h.shift(h.workspace())['workspace']
    path = '/api/workspaces/' + w['id']
    # Creating and editing a small draft can already store a complete check.
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, w['id']).validation = None
    assert schedule(h, path).json()['workspace']['validation_current'] is False
    h.lifecycle(w, 'validate')
    assert schedule(h, path).json()['workspace']['validation_current'] is True


def test_draft_schedule_byte_budget(scale_rota):
    h, path = scale_rota
    reply = schedule(h, path)
    assert len(reply.json()['shifts']) == 768 and len(reply.content) <= BUDGET['draft']


@pytest.fixture
def checked_rota(scale_rota):
    h, path = scale_rota
    wid = path.rsplit('/', 1)[-1]
    actor = h.seed['identities']['coordinator']
    with transaction(h.app.state.engine, write=True) as db:
        workspace = db.get(m.Workspace, wid)
        zone = ZoneInfo(db.get(m.Organisation, h.seed['organisation_id']).timezone)
        templates = {pid: row for row in db.scalars(select(m.RecurringSeries).where(
            m.RecurringSeries.name.like('Scale template %'))) for pid in row.definition['default_person_ids']}
        assignments = {}
        for p in db.scalars(select(m.Assignment).join(m.Shift).where(m.Shift.workspace_id == wid)):
            assignments.setdefault(p.shift_id, []).append(p)
        shifts = list(db.scalars(select(m.Shift).where(m.Shift.workspace_id == wid).order_by(m.Shift.start, m.Shift.id)))
        for shift in shifts:
            template = templates[assignments[shift.id][0].person_id]
            shift.series_id, shift.series_revision, shift.series_snapshot = template.id, template.revision, record(template)
            shift.occurrence_on = aware(shift.start).astimezone(zone).date()
        edit = shifts[0]
        job = m.Job(kind='auto_schedule', department_id=workspace.department_id, workspace_id=wid, requested_by=actor,
                    status='succeeded', input_fingerprint='0' * 64, input_snapshot={})
        db.add(job)
        db.flush()
        edit.lineage_id, edit.series_modified = m.uid(), True
        edit.template_skipped = [assignments[edit.id][0].person_id]
        for p in assignments[edit.id]:
            p.source, p.source_run_id, p.source_actor_id, p.source_at = 'auto_schedule', job.id, actor, aware(p.start)
            p.override_reason, p.revision = 'Retained admission', 2
        db.flush()
        planning.revalidate(db, workspace)
        return h, path, edit.id


def strip_schedule(full):
    result = deepcopy(full)
    fields = s.SCHEDULE_DETAIL_FIELDS
    for shift in result['shifts']:
        for key in fields['session']:
            shift.pop(key, None)
        for participant in shift['participants']:
            for key in fields['participant']:
                participant.pop(key, None)
    check = result['workspace']['validation']
    for key in fields['check']:
        check.pop(key, None)
    for row in check.get('coverage', []):
        for key in fields['coverage']:
            row.pop(key, None)
    return result


def test_checked_schedule_byte_budget_and_role_equivalence(checked_rota, monkeypatch):
    monkeypatch.setattr(main, "secrets", SimpleNamespace(token_hex=lambda size: "0" * (2 * size)))
    h, path, edit_id = checked_rota
    for role in s.ROLE_NAMES:
        if role not in h.seed['identities']:
            identity(h, role, [role])
    allowed = set()
    anonymous = TestClient(h.app, base_url=h.settings.public_origin)
    for role in (*s.ROLE_NAMES, None):
        client = h.client(role) if role else anonymous
        full = client.get(path, headers=IDENTITY)
        explicit_full = client.get(path, params={'view': 'full'}, headers=IDENTITY)
        value = client.get(path, params={'view': 'schedule'}, headers=IDENTITY)
        assert (explicit_full.status_code, explicit_full.content) == (full.status_code, full.content)
        assert value.status_code == full.status_code
        if full.status_code != 200:
            assert value.content == full.content
            assert client.get(path, params={'view': 'schedule'}, headers={'if-none-match': '*'}).content == full.content
            continue
        allowed.add(role)
        full_data = full.json()
        assert value.json() == strip_schedule(full_data)
        full_tag, schedule_tag = full.headers['etag'], value.headers['etag']
        compact_tag = client.get(path, params={'view': 'compact'}).headers['etag']
        assert len({full_tag, schedule_tag, compact_tag}) == 3
        for view, tag in [('full', full_tag), ('schedule', schedule_tag)]:
            other = schedule_tag if view == 'full' else full_tag
            assert client.get(path, params={'view': view}, headers={'if-none-match': other}).status_code == 200
            match = client.get(path, params={'view': view}, headers={'if-none-match': tag})
            assert match.status_code == 304 and match.content == b''
            assert match.headers['etag'] == tag and match.headers['cache-control'] == 'private, no-cache'
        if role == 'coordinator':
            assert len(full_data['shifts']) == 768 and len(value.content) <= BUDGET['checked']
            by_id = {row['id']: row for row in full_data['shifts']}
            assert all(by_id[edit_id][key] for key in set(s.SCHEDULE_DETAIL_FIELDS['session']) - {'closure_reason'})
            assert all(p[key] for p in by_id[edit_id]['participants'] for key in s.SCHEDULE_DETAIL_FIELDS['participant'])
            assert all(p['override_reason'] == 'Retained admission' for p in next(
                row for row in value.json()['shifts'] if row['id'] == edit_id)['participants'])
    assert allowed == {'admin', 'coordinator', 'approver', 'auditor', 'programme_lead'}
    missing = '/api/workspaces/' + m.uid()
    client = h.client('coordinator')
    full = client.get(missing, headers=IDENTITY)
    projected = client.get(missing, params={'view': 'schedule'}, headers={'if-none-match': '*'})
    assert full.status_code == projected.status_code == 404 and full.content == projected.content


def stored(h, wid):
    with transaction(h.app.state.engine) as db:
        S, A, L = m.Shift.__table__, m.Assignment.__table__, m.ShiftService.__table__
        sessions = S.select().where(S.c.workspace_id == wid)
        assignments = select(A).join(S).where(S.c.workspace_id == wid)
        services = select(L).join(S).where(S.c.workspace_id == wid)
        return tuple({tuple(row[col.name] for col in table.primary_key): dict(row)
                      for row in db.execute(query).mappings()}
                     for table, query in ((S, sessions), (A, assignments), (L, services)))


def test_schedule_edits_sheet_bulk_lock_copy_and_undo_redo_keep_every_stored_column(checked_rota):
    h, path, sid = checked_rota
    wid = path.rsplit('/', 1)[-1]
    before = stored(h, wid)

    def read():
        data = schedule(h, path).json()
        return data['workspace'], next(s for s in data['shifts'] if s['id'] == sid)

    def shift_update(location=None):
        w, row = read()
        value = {**{key: row[key] for key in EDITABLE}, 'workspace_id': w['id'], 'shift_id': row['id'],
                 'participants': [{key: p[key] for key in PARTICIPATION} for p in row['participants']]}
        if location is not None:
            value['location_id'] = location
        return h.command('coordinator', 'shift.update', value, revision=w['revision'])

    # Session panel (saveParticipants) and edit form/Staffing (ShiftEdit).
    shift_update()
    expected = deepcopy(before)
    expected[0][sid,]['revision'] += 1
    assert stored(h, wid) == expected
    shift_update(h.seed['locations'][1])
    expected[0][sid,].update(location_id=h.seed['locations'][1], revision=expected[0][sid,]['revision'] + 1)
    assert stored(h, wid) == expected

    # Sheet stages a specific row with its session revision; bulk resolves a collection selection itself.
    for sheet, loc in [(True, h.seed['locations'][0]), (False, h.seed['locations'][1])]:
        _, row = read()
        body = {'item_kind': 'shift.update', 'changes': [{'field': 'location_id', 'how': 'set', 'value': loc}],
                'reason': REASON}
        if sheet:
            body['items'] = [{'target_id': row['id'], 'expected_revision': row['revision']}]
        else:
            body['selection'] = {'collection': 'sessions', 'include_ids': [row['id']]}
        prepared = preview(h, 'coordinator', body)
        assert prepared['counts']['ready'] == 1, prepared['counts']
        apply(h, body, prepared['fingerprint'])
        expected[0][sid,].update(location_id=loc, revision=expected[0][sid,]['revision'] + 1)
        assert stored(h, wid) == expected

    unlocked = deepcopy(expected)
    w, row = read()
    h.command('coordinator', 'shift.action', {'workspace_id': w['id'], 'shift_ids': [row['id']], 'action': 'lock',
              'reason': REASON}, revision=w['revision'])
    expected[0][sid,].update(locked=True, revision=expected[0][sid,]['revision'] + 1)
    assert stored(h, wid) == expected
    for action, want in [('undo', unlocked), ('redo', expected)]:
        w, _ = read()
        h.command('coordinator', 'workspace.' + action, {'reason': REASON}, w['id'], w['revision'])
        assert stored(h, wid) == want

    w, row = read()
    h.command('coordinator', 'shift.copy', {'workspace_id': w['id'], 'shift_ids': [row['id']], 'offset_days': 1,
              'reason': REASON}, revision=w['revision'])
    after = stored(h, wid)
    [clone] = after[0].keys() - expected[0].keys()
    new_shift = after[0].pop(clone)
    # New identities, time offset and detached series are deliberate copy semantics.
    clock = new_shift['created_at']
    assert clock >= expected[0][sid,]['created_at']
    assert new_shift == {**expected[0][sid,], 'id': clone[0], 'revision': 1, 'created_at': clock,
                         'lineage_id': None, 'series_id': None, 'series_revision': None, 'series_snapshot': None,
                         'occurrence_on': None, 'series_modified': False,
                         'start': expected[0][sid,]['start'] + timedelta(days=1),
                         'end': expected[0][sid,]['end'] + timedelta(days=1)}
    originals = {p['person_id']: p for p in expected[1].values() if p['shift_id'] == sid}
    added = {key: after[1].pop(key) for key in after[1].keys() - expected[1].keys()}
    assert {p['person_id'] for p in added.values()} == originals.keys()
    for key, p in added.items():
        prior = originals[p['person_id']]
        assert p == {**prior, 'id': key[0], 'revision': 1, 'created_at': p['created_at'], 'shift_id': clone[0],
                     'source': 'manual', 'source_run_id': None, 'source_at': p['source_at'], 'override_reason': None,
                     'start': prior['start'] + timedelta(days=1), 'end': prior['end'] + timedelta(days=1)}
        assert p['source_at'] is not None and p['created_at'] >= clock
    [link] = after[2].keys() - expected[2].keys()
    assert after[2].pop(link) == {'shift_id': clone[0], 'service_id': row['service_ids'][0]}
    assert after == expected



def test_published_schedule_projects_frozen_closure_evidence(backend):
    h = backend
    made = h.shift(h.workspace('Historical closure fixture'))
    w = made['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        workspace = db.get(m.Workspace, w['id'])
        shift = db.get(m.Shift, made['shifts'][0]['id'])
        shift.series_snapshot = {'definition': {'close_when_all_usual_unavailable': True}}
        issued = {**shift_record(db, shift), 'series_state_frozen': True, 'closed': True,
                  'closure_reason': 'usual_assignees_unavailable', 'usual_assignee_unavailability': []}
        workspace.status = 'published'
        db.add(m.Publication(workspace_id=workspace.id, department_id=workspace.department_id, sequence=1,
                             starts_on=workspace.starts_on, ends_on=workspace.ends_on,
                             published_by=h.seed['identities']['coordinator'], fingerprint='0' * 64,
                             snapshot={'workspace': record(workspace), 'shifts': [issued]},
                             validation={'contracts': check_contracts()}))
    path = '/api/workspaces/' + w['id']
    full = h.detail(w)
    assert full['shifts'][0]['closure_reason'] == 'usual_assignees_unavailable'
    compact = schedule(h, path).json()
    assert compact == strip_schedule(full) and compact['shifts'][0]['closed']


BOTH = pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)


def rendered(value):
    return JSONResponse(value).body


@BOTH
def test_a_row_outside_the_contract_limits_is_still_projected_not_validated(h):
    made = h.shift(h.workspace('Retained out-of-contract session'))
    w, sid = made['workspace'], made['shifts'][0]['id']
    with transaction(h.app.state.engine, write=True) as db:
        shift = db.get(m.Shift, sid)
        shift.cancelled, shift.cancellation_reason = True, 'no'
        for p in db.scalars(select(m.Assignment).where(m.Assignment.shift_id == sid)):
            p.override_reason = 'no'
    path = '/api/workspaces/' + w['id']
    full = h.client('coordinator').get(path, headers=IDENTITY)
    value = schedule(h, path)
    assert full.status_code == 200
    row = next(row for row in value.json()['shifts'] if row['id'] == sid)
    with pytest.raises(ValidationError):
        s.ScheduleSession.model_validate(row)
    with pytest.raises(ValidationError):
        s.ScheduleParticipant.model_validate(row['participants'][0])
    assert value.content == rendered(strip_schedule(full.json()))


@BOTH
def test_double_booked_series_and_published_sessions_project_only_the_named_fields(h):
    from .test_h1_template_conflicts import assigned_template
    series = assigned_template(h)

    def rota(name, start, end):
        return h.command('coordinator', 'workspace.create', {'name': name, 'starts_on': start, 'ends_on': end,
                                                             'series_ids': [series['id']]})['workspace']

    published = rota('Synthetic published series', '2026-10-04', '2026-10-11')
    h.publish(published)
    draft = rota('Synthetic double-booked series', '2026-10-11', '2026-10-18')
    pid = h.seed['people'][0]
    leave = h.command('coordinator', 'request.create', {'person_id': pid, 'kind': 'leave',
        'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-12T00:00:00+03:00',
        'private_reason': 'Private synthetic leave note'})['record']
    h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Approve synthetic leave'},
              leave['id'], leave['revision'])
    for w in (published, draft):
        path = '/api/workspaces/' + w['id']
        full = h.client('coordinator').get(path, headers=IDENTITY).json()
        value = schedule(h, path).json()
        assert value == strip_schedule(full)
        assert [row['series_id'] for row in full['shifts']] == [series['id']]
        for row, kept in zip(full['shifts'], value['shifts']):
            assert row.keys() - kept.keys() == row.keys() & set(s.SCHEDULE_DETAIL_FIELDS['session'])
            assert row['double_booking_conflicts'] and kept['double_booking_conflicts'] == row['double_booking_conflicts']
            for p, q in zip(row['participants'], kept['participants']):
                assert p.keys() - q.keys() == p.keys() & set(s.SCHEDULE_DETAIL_FIELDS['participant'])
        check, kept = full['workspace']['validation'], value['workspace']['validation']
        assert check.keys() - kept.keys() == check.keys() & set(s.SCHEDULE_DETAIL_FIELDS['check'])
    assert h.detail(published)['workspace']['status'] == 'published'
