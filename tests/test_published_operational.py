"""Current operational absences are separate from immutable issued publication facts."""
import json
from datetime import timedelta

import pytest
from sqlalchemy import delete, event, select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import ParticipantInput, ROLE_NAMES
from packages.domain.time import pin_clock, now, aware
from .governance_support import identity


def issue(h, days=(4, 5, 6)):
    w = h.workspace('Issued duties')
    ids = []
    for day in days:
        result = h.shift(w, f'2026-10-{day:02}T08:00:00+03:00', f'2026-10-{day:02}T12:00:00+03:00')
        w = result['workspace']
        ids.append(result['shift_id'])
    pub = h.publish(w)['publication_id']
    return w, pub, ids


def absence(h, kind, start='2026-10-06T09:00:00+03:00', end='2026-10-06T10:00:00+03:00'):
    pid = h.seed['people'][0]
    if kind == 'pattern':
        row = h.command('staff', 'availability_pattern.propose', {'person_id': pid, 'weekdays': [1],
            'start_time': '09:00', 'end_time': '10:00', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
            'note': 'Private pattern explanation'})['pattern']
        h.command('coordinator', 'availability_pattern.decide', {'decision': 'approve', 'reason': 'Reviewed weekly absence'},
                  row['id'], row['revision'])
    elif kind == 'leave':
        row = h.command('staff', 'request.create', {'person_id': pid, 'start': start, 'end': end,
            'private_reason': 'Private leave explanation'})['record']
        h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Reviewed absence'}, row['id'], row['revision'])
    else:
        h.command('coordinator', 'availability.record', {'person_id': pid, 'kind': 'actual_absence',
            'start': start, 'end': end, 'private_reason': 'Private recorded explanation'})


def frozen(h, pub):
    with transaction(h.app.state.engine) as db:
        p = db.get(m.Publication, pub)
        shifts = list(db.scalars(select(m.Shift).where(m.Shift.workspace_id == p.workspace_id)))
        from apps.api.snapshots import shift_records
        from apps.api.serialization import record
        evidence = [record(row) for model in (m.Approval, m.ReviewDecision)
                    for row in db.scalars(select(model).where(model.workspace_id == p.workspace_id).order_by(model.id))]
        return json.dumps([p.snapshot, p.validation, p.fingerprint, shift_records(db, shifts),
                           record(db.get(m.Workspace, p.workspace_id)), evidence], sort_keys=True)


def attention(h, role='coordinator'):
    return [i for i in h.get(role, '/api/home')['attention'] if i['key'] == 'published_absence_conflicts']


def warning_action(h, pub, shift_id):
    """Every public consumer must carry the same canonical destination for the issued fact."""
    home = next(row['action'] for item in attention(h) for row in item['sample'] if row['shift_id'] == shift_id)
    for path in ('/api/publications/' + pub,
                 '/api/schedule/sessions?start=2026-10-04&end=2026-10-11',
                 '/api/schedule/days?start=2026-10-04&end=2026-10-11'):
        body = h.get('coordinator', path)
        summary = (body['operational'] if 'operational' in body else
                   next(row['operational'] for row in body['sources'] if row['id'] == pub))
        assert next(row['action'] for row in summary['warnings'] if row['shift_id'] == shift_id) == home
    return home


def update_duty(h, workspace, row, **changes):
    payload = {key: row[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')}
    payload.update(workspace_id=workspace['id'], shift_id=row['id'],
        participants=[{key: value for key, value in p.items() if key in ParticipantInput.model_fields}
                      for p in row['participants']], override_reason='Synthetic demand edit during known absence')
    return h.command('coordinator', 'shift.update', {**payload, **changes}, revision=workspace['revision'])['workspace']


@pytest.mark.parametrize('kind', ['leave', 'pattern', 'recorded'])
def test_publish_then_approved_absence_has_one_current_task_and_frozen_evidence(h, kind):
    w, pub, ids = issue(h)
    before = frozen(h, pub)
    assert attention(h) == []
    absence(h, kind)
    # Actual API reproduction: canonical grid already sees this overlap before Home is fixed.
    grid = h.get('coordinator', '/api/schedule/sessions?start=2026-10-04&end=2026-10-11')
    affected = [s for s in grid['shifts'] if any(c['code'] == 'UNAVAILABLE' for c in s.get('double_booking_conflicts', []))]
    assert [s['id'] for s in affected] == [ids[2]]
    assert frozen(h, pub) == before
    items = attention(h)
    assert len(items) == 1, {'home': items, 'grid_absent': [s['id'] for s in affected]}
    item = items[0]
    assert item['count'] == 1 and len(item['sample']) == 1
    fact = item['sample'][0]
    assert fact['publication_id'] == pub and fact['shift_id'] == ids[2]
    assert aware(fact['start']) == aware('2026-10-06T06:00:00Z') and aware(fact['end']) == aware('2026-10-06T07:00:00Z')
    assert fact['action'] == item['action']
    assert fact['action'] == {'key': 'amend_publication', 'publication_id': pub, 'workspace_id': w['id'],
        'amendment_workspace_id': None, 'amendment_shift_id': None, 'amendment_shift_state': 'not_created',
        'shift_id': ids[2], 'date': '2026-10-06'}
    current = grid['sources'][0]['operational']
    assert current['conflicting_assignments'] == current['affected_sessions'] == 1
    issued = h.get('coordinator', '/api/publications/' + pub)
    assert issued['operational']['conflicting_assignments'] == 1
    assert frozen(h, pub) == before


def test_duplicate_absences_and_open_amendment_are_one_task(h):
    w, pub, ids = issue(h)
    before = frozen(h, pub)
    absence(h, 'leave')
    absence(h, 'recorded')
    amendment = h.workspace('Repair issued duties', base=pub)
    item, = attention(h)
    assert item['count'] == 1 and len(item['sample']) == 1
    assert item['action']['amendment_workspace_id'] == amendment['id']
    clone = next(row for row in h.detail(amendment)['shifts'] if aware(row['start']).day == 6)
    action = warning_action(h, pub, ids[2])
    assert action['shift_id'] == ids[2] != clone['id']
    assert action['amendment_shift_state'] == 'matched' and action['amendment_shift_id'] == clone['id']
    assert not [i for i in h.get('coordinator', '/api/home')['attention'] if i['key'] == 'leave_conflicts']
    assert frozen(h, pub) == before


def test_schedule_source_warnings_reuse_the_visible_duty_overlap_evaluation(h, monkeypatch):
    from apps.api.services import schedule_view
    _, _, ids = issue(h)
    absence(h, 'recorded')
    original = schedule_view.session_overlap_flags
    evaluated = []

    def flags(*args, **kwargs):
        result = original(*args, **kwargs)
        evaluated.append(result)
        return result

    monkeypatch.setattr(schedule_view, 'session_overlap_flags', flags)
    client = h.client('coordinator')
    url = '/api/schedule/sessions?start=2026-10-04&end=2026-10-11'
    response = client.get(url)
    assert response.status_code == 200
    assert len(evaluated) == 1  # One canonical absence evaluation, shared by duties and their source warning.
    body = response.json()
    flagged_ids = {s['id'] for s in evaluated[0] if any(c['code'] == 'UNAVAILABLE'
                   for c in s.get('double_booking_conflicts', []))}
    warnings = body['sources'][0]['operational']['warnings']
    assert flagged_ids == {r['shift_id'] for r in warnings} == {ids[2]}
    evaluated.clear()
    assert client.get(url, headers={'if-none-match': response.headers['etag']}).status_code == 304
    assert len(evaluated) == 1


def test_all_roles_only_receive_authorized_warnings_and_actions(h):
    _, pub, _ = issue(h)
    absence(h, 'leave')
    for role in ROLE_NAMES:
        user = 'operational-' + role
        identity(h, user, [role], person=h.seed['people'][0] if role == 'staff' else None)
        items = attention(h, user)
        # SW3-C-01 / SW3-B-06: cover is the coordinators' job; staff and reviewers read the facts on Schedule and
        # the publication, and Home's Needs you lists only what they can act on.
        team = role in ('admin', 'coordinator')
        assert bool(items) == team, role
        if items:
            item, = items
            assert bool(item['action']) == (role in ('admin', 'coordinator'))
            assert 'Private' not in json.dumps(item)
            assert {r['person_id'] for r in item['sample']} == {h.seed['people'][0]}
    identity(h, 'other-staff', ['staff'], person=h.seed['people'][1])
    assert attention(h, 'other-staff') == []
    mine = h.get('staff', '/api/publications/' + pub)
    assert mine['original_validation'] == {'valid_at_publication': True}
    assert mine['operational']['conflicting_assignments'] == 1
    assert all(r['person_id'] == h.seed['people'][0] and r['action'] is None for r in mine['operational']['warnings'])


def test_only_effective_future_heads_count(h):
    w, pub, ids = issue(h)
    amendment = h.workspace('New issued head', base=pub)
    new = h.publish(amendment)['publication_id']
    absence(h, 'leave')
    assert {r['publication_id'] for i in attention(h) for r in i['sample']} == {new}
    assert h.get('coordinator', '/api/publications/' + pub)['operational']['conflicting_assignments'] == 0
    previous = now()
    try:
        pin_clock('2026-10-12T06:00:00+00:00')
        h.clients.pop('coordinator').close()  # The clock jump also expires the old test session.
        assert attention(h) == []
    finally:
        pin_clock(previous)


def test_partial_presence_and_endpoint_touching_are_exact(h):
    w = h.workspace()
    start, end = '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00'
    # Two people meet cover while the first has a planned off-cover interval.
    parts = [{'person_id': h.seed['people'][0], 'start': start, 'end': end,
              'segments': [{'start': start, 'end': '2026-10-06T09:00:00+03:00'},
                           {'start': '2026-10-06T11:00:00+03:00', 'end': end}]},
             {'person_id': h.seed['people'][1], 'start': start, 'end': end}]
    w = h.shift(w, start, end, people=parts)['workspace']
    pub = h.publish(w)['publication_id']
    absence(h, 'recorded', '2026-10-06T09:00:00+03:00', '2026-10-06T11:00:00+03:00')
    assert attention(h) == []
    absence(h, 'recorded', '2026-10-06T10:30:00+03:00', '2026-10-06T11:30:00+03:00')
    item, = attention(h)
    assert item['count'] == 1
    assert aware(item['sample'][0]['start']) == aware('2026-10-06T08:00:00Z')
    assert aware(item['sample'][0]['end']) == aware('2026-10-06T08:30:00Z')


def test_replacement_window_and_cancelled_duties_do_not_alert(h):
    from .test_schedule_overlaps import published_rota, replacement
    old = published_rota(h)
    new = h.publish(replacement(h, old))['publication_id']
    before = frozen(h, old)
    absence(h, 'recorded', '2026-10-09T08:00:00+03:00', '2026-10-09T12:00:00+03:00')
    assert attention(h) == []  # Old Oct 9 duty belongs to the replaced partition.
    assert frozen(h, old) == before
    w = h.workspace('Cancelled issued duty', '2026-10-18', '2026-10-25')
    created = h.shift(w, '2026-10-19T08:00:00+03:00', '2026-10-19T12:00:00+03:00')
    w = h.command('coordinator', 'shift.action', {'workspace_id': w['id'], 'shift_ids': [created['shift_id']],
        'action': 'cancel', 'reason': 'Cancelled synthetic clinic'}, revision=created['workspace']['revision'])['workspace']
    h.publish(w)
    absence(h, 'recorded', '2026-10-19T09:00:00+03:00', '2026-10-19T10:00:00+03:00')
    assert attention(h) == []


def test_home_pages_samples_and_source_tags_track_current_absence(h):
    from apps.api.auth import authenticate
    from apps.api.services import published_operational
    w, pub, ids = issue(h)
    client = h.client('coordinator')
    days = '/api/schedule/days?start=2026-10-04&end=2026-10-11'
    first = client.get(days)
    absence(h, 'recorded', '2026-10-04T09:00:00+03:00', '2026-10-06T10:00:00+03:00')
    second = client.get(days, headers={'if-none-match': first.headers['etag']})
    assert second.status_code == 200
    assert second.json()['sources'][0]['operational']['conflicting_assignments'] == 3
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, client.cookies['rota_session'])
        paged = published_operational.home_attention(db, actor, page_size=1)
        normal = published_operational.home_attention(db, actor)
    assert paged == normal and paged['count'] == 3 and len(paged['sample']) == 2
    assert client.get(days, headers={'if-none-match': second.headers['etag']}).status_code == 304


@pytest.mark.parametrize('initially_absent', [False, True])
def test_current_warning_respects_issued_closure_and_retraction(h, initially_absent):
    from .test_series_overlap_api import candidate, reviewed, absence as series_absence
    from .test_series_reconciliation import materialise
    if initially_absent:
        row = series_absence(h, day='2026-10-05')
    value = candidate(h, 'Issued closure truth')
    value['definition']['close_when_all_usual_unavailable'] = True
    series = reviewed(h, value)
    w = materialise(h, series)
    if initially_absent:
        shift_id = h.detail(w)['shifts'][0]['id']
        w = h.command('coordinator', 'shift.action', {'workspace_id': w['id'], 'shift_ids': [shift_id],
            'action': 'cancel', 'reason': 'Explicit coordinator closure'}, revision=w['revision'])['workspace']
    pub = h.publish(w)['publication_id']
    before = frozen(h, pub)
    if initially_absent:
        h.command('coordinator', 'availability.retract', {'reason': 'Recorded absence ended'}, row['id'], row['revision'])
    else:
        row = series_absence(h, day='2026-10-05')
    assert bool(attention(h)) is not initially_absent
    assert frozen(h, pub) == before
    if not initially_absent:
        h.command('coordinator', 'availability.retract', {'reason': 'Recorded absence ended'}, row['id'], row['revision'])
        assert attention(h) == []


def test_amendment_target_ignores_changed_roster_and_keeps_issued_approval(h):
    _, pub, ids = issue(h, (6,))
    before = frozen(h, pub)
    absence(h, 'recorded')
    amendment = h.workspace('Repair roster', base=pub)
    clone, = h.detail(amendment)['shifts']
    update_duty(h, amendment, clone, participants=[{'person_id': h.seed['people'][1],
        'start': clone['start'], 'end': clone['end']}])
    action = warning_action(h, pub, ids[0])
    assert action['amendment_shift_state'] == 'matched' and action['amendment_shift_id'] == clone['id']
    assert frozen(h, pub) == before


@pytest.mark.parametrize('side', ['source', 'destination', 'both'])
def test_duplicate_demands_are_ambiguous_beyond_home_page_and_person_filter(h, side):
    w = h.workspace('Duplicated issued demand')
    created = h.shift(w, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00')
    w, original = created['workspace'], created['shift_id']
    if side in ('source', 'both'):
        # The duplicate has no absent person, so Home never includes it in its warning sample.
        w = h.shift(w, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00',
                    people=[h.seed['people'][1]])['workspace']
    pub = h.publish(w)['publication_id']
    before = frozen(h, pub)
    absence(h, 'recorded')
    amendment = h.workspace('Ambiguous cloned demand', base=pub)
    if side == 'source':
        # One destination still cannot establish which original duplicate it represents.
        clone = next(row for row in h.detail(amendment)['shifts']
                     if row['participants'][0]['person_id'] == h.seed['people'][1])
        amendment = h.command('coordinator', 'shift.action', {'workspace_id': amendment['id'],
            'shift_ids': [clone['id']], 'action': 'remove', 'reason': 'Synthetic duplicate removal'},
            revision=amendment['revision'])['workspace']
    elif side == 'destination':
        amendment = h.shift(amendment, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00',
                            people=[])['workspace']
    action = warning_action(h, pub, original)
    assert action['amendment_shift_state'] == 'ambiguous' and action['amendment_shift_id'] is None
    from apps.api.auth import authenticate
    from apps.api.services.published_operational import home_attention
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, h.client('coordinator').cookies['rota_session'])
        assert home_attention(db, actor, page_size=1)['action'] == action
    assert frozen(h, pub) == before


@pytest.mark.parametrize('change', ['remove', 'cancel', 'retime', 'location', 'services', 'attributes', 'type_definition'])
def test_changed_or_missing_amendment_demand_has_no_invented_target_and_refreshes_tags(h, change):
    _, pub, ids = issue(h, (6,))
    before = frozen(h, pub)
    absence(h, 'recorded')
    amendment = h.workspace('Changed cloned demand', base=pub)
    clone, = h.detail(amendment)['shifts']
    client = h.client('coordinator')
    paths = ['/api/schedule/days?start=2026-10-04&end=2026-10-11',
             '/api/schedule/sessions?start=2026-10-04&end=2026-10-11']
    prior = [client.get(path) for path in paths]
    assert warning_action(h, pub, ids[0])['amendment_shift_state'] == 'matched'
    if change in ('remove', 'cancel'):
        h.command('coordinator', 'shift.action', {'workspace_id': amendment['id'], 'shift_ids': [clone['id']],
            'action': change, 'reason': 'Synthetic amendment change'}, revision=amendment['revision'])
    elif change == 'retime':
        start, end = (aware(clone[k]) + timedelta(hours=1) for k in ('start', 'end'))
        update_duty(h, amendment, clone, start=start.isoformat(), end=end.isoformat(), participants=[{
            'person_id': h.seed['people'][0], 'start': start.isoformat(), 'end': end.isoformat()}])
    elif change == 'location':
        update_duty(h, amendment, clone, location_id=h.seed['locations'][1])
    elif change == 'services':
        update_duty(h, amendment, clone, service_ids=[h.seed['services'][1]])
    else:
        # Canonical stored demand fixtures for fields needing separately governed catalogue setup.
        with transaction(h.app.state.engine, write=True) as db:
            row = db.get(m.Shift, clone['id'])
            if change == 'attributes':
                row.attributes = {'synthetic_demand': 'changed'}
            else:
                row.type_snapshot = {**row.type_snapshot, 'name': 'Changed demand definition'}
            # Raw fixture writes bypass command stamps; discard disposable read projections.
            db.execute(delete(m.WorkspaceReadProjection))
    action = warning_action(h, pub, ids[0])
    assert action['amendment_workspace_id'] == amendment['id'] and action['shift_id'] == ids[0]
    assert action['amendment_shift_state'] == 'changed_or_missing' and action['amendment_shift_id'] is None
    for path, old in zip(paths, prior):
        response = client.get(path, headers={'if-none-match': old.headers['etag']})
        assert response.status_code == 200 and response.headers['etag'] != old.headers['etag']
        old_op = next(row['operational'] for row in old.json()['sources'] if row['id'] == pub)
        new_op = next(row['operational'] for row in response.json()['sources'] if row['id'] == pub)
        assert old_op['fingerprint'] != new_op['fingerprint']
    assert frozen(h, pub) == before


@pytest.mark.parametrize('unusable', ['archived', 'published', 'superseded', 'other_department', 'other_base'])
def test_only_exact_current_live_amendment_is_an_action_destination(h, unusable):
    _, pub, ids = issue(h, (6,))
    absence(h, 'recorded')
    decoy = h.workspace('Unavailable amendment', base=pub)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, decoy['id'])
        if unusable == 'archived':
            row.archived_at = now()
        elif unusable in ('published', 'superseded'):
            row.status = unusable
        elif unusable == 'other_department':
            dept = m.Department(organisation_id=h.seed['organisation_id'], name='Other synthetic department')
            db.add(dept)
            db.flush()
            row.department_id = dept.id
        else:
            row.base_publication_id = None
    action = warning_action(h, pub, ids[0])
    assert action['amendment_workspace_id'] is None
    assert action['amendment_shift_id'] is None and action['amendment_shift_state'] == 'not_created'
    current = h.workspace('Actual current amendment', base=pub)
    clone, = h.detail(current)['shifts']
    action = warning_action(h, pub, ids[0])
    assert action['amendment_workspace_id'] == current['id']
    assert action['amendment_shift_id'] == clone['id'] and action['amendment_shift_state'] == 'matched'


def test_matching_is_scoped_per_publication_and_noncoordinators_do_not_read_amendment_demand(h, monkeypatch):
    from apps.api.services import published_operational
    _, pub, ids = issue(h, (6,))
    other = h.workspace('Next issued period', '2026-10-11', '2026-10-18')
    other = h.shift(other, '2026-10-13T08:00:00+03:00', '2026-10-13T12:00:00+03:00')['workspace']
    other_pub = h.publish(other)['publication_id']
    amendment = h.workspace('Matching current base', base=pub)
    h.workspace('Other publication amendment', '2026-10-11', '2026-10-18', base=other_pub)
    absence(h, 'recorded')
    clone, = h.detail(amendment)['shifts']
    assert warning_action(h, pub, ids[0])['amendment_shift_id'] == clone['id']
    # Staff and reviewers retain facts without action or amendment-session disclosure/work.
    def forbidden(*args, **kwargs):
        raise AssertionError('No amendment demand read for a viewer without coordinator permission')
    monkeypatch.setattr(published_operational, 'shift_records', forbidden)
    for role in ('staff', 'approver', 'auditor'):
        assert attention(h, role) == []  # SW3-B-06: nothing they can act on, so no Needs you item at all


@pytest.mark.parametrize('status', ['draft', 'submitted', 'approved'])
def test_current_amendment_selection_preserves_oldest_live_owner(h, status):
    _, pub, ids = issue(h, (6,))
    absence(h, 'recorded')
    chosen = h.workspace('Selected live amendment', base=pub)
    later = h.workspace('Later live amendment', base=pub)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, chosen['id']).status = status
        db.get(m.Workspace, chosen['id']).created_at = now()
        db.get(m.Workspace, later['id']).created_at = now() + timedelta(seconds=1)
    clone, = h.detail(chosen)['shifts']
    action = warning_action(h, pub, ids[0])
    assert action['amendment_workspace_id'] == chosen['id']
    assert action['amendment_shift_id'] == clone['id'] and action['amendment_shift_state'] == 'matched'


def test_amendment_matching_batches_sql_and_visits_each_relevant_demand_once(h, monkeypatch):
    from apps.api import snapshots
    from apps.api.services import published_operational
    _, pub, _ = issue(h, (4, 5, 6, 7, 8, 9, 10))
    amendment = h.workspace('Batch matching', base=pub)
    engine = h.app.state.engine
    with transaction(engine) as db:
        publication = db.get(m.Publication, pub)
        rows = [{**row, 'publication_id': pub} for row in snapshots.workspace_shifts(db, publication.workspace_id)]
        key = snapshots.session_key
        visited, sql = [], []
        def demand(row):
            visited.append(row['id'])
            return key(row)
        def statement(conn, cursor, text, parameters, *args):
            sql.append((text, parameters))
        monkeypatch.setattr(snapshots, 'session_key', demand)
        event.listen(engine, 'before_cursor_execute', statement)
        measurements = []
        try:
            for size in (1, len(rows)):
                visited.clear()
                sql.clear()
                result = published_operational.amendment_targets(db, {pub: publication}, {pub: amendment['id']}, rows[:size])
                assert len(result) == size and all(state == 'matched' for target, state in result.values())
                measurements.append({'issued_demands': size, 'demand_key_calls': len(visited), 'sql_statements': len(sql)})
                assert len(visited) == 2 * size
                assert not any('assignments' in text or 'publications' in text or 'snapshot' in text.replace('type_snapshot', '') for text, _ in sql)
                assert all(text.lstrip().upper().startswith('SELECT') for text, _ in sql)
        finally:
            event.remove(engine, 'before_cursor_execute', statement)
        assert measurements[0]['sql_statements'] == measurements[1]['sql_statements'] == 2
        print('Amendment deterministic work: ' + json.dumps(measurements))
        if engine.dialect.name == 'sqlite':
            plan = list(db.connection().exec_driver_sql('EXPLAIN QUERY PLAN ' + sql[0][0], sql[0][1]))
            assert not any('SCAN shifts' in row[-1] for row in plan)
            print('Amendment demand query plan: ' + json.dumps([row[-1] for row in plan]))


def test_shared_mapping_keeps_score_baseline_duplicate_and_inactive_semantics():
    from apps.api.snapshots import mapped_score_baseline
    old = {'id': 'issued', 'assignment_type_id': 'type', 'type_revision': 1, 'type_snapshot': {},
        'location_id': 'place', 'attributes': {}, 'cancelled': False, 'service_ids': [],
        'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00',
        'participants': [{'person_id': 'person', 'team_lead': True, 'overtime': False}]}
    clone = {**old, 'id': 'clone', 'participants': []}
    duplicate = {**old, 'id': 'duplicate'}
    details = {}
    assert mapped_score_baseline([clone], [old, duplicate], details) is None
    assert details == {'state': 'unavailable', 'matched_sessions': 0, 'unmatched_sessions': 0, 'ambiguous_sessions': 2}
    assert mapped_score_baseline([], [old, duplicate], details) is None
    assert details == {'state': 'unavailable', 'matched_sessions': 0, 'unmatched_sessions': 2, 'ambiguous_sessions': 0}
    assert mapped_score_baseline([clone], [old, {**duplicate, 'closed': True}], details) == {('clone', 'person'): (True, False)}
    assert details == {'state': 'available', 'matched_sessions': 1, 'unmatched_sessions': 0, 'ambiguous_sessions': 0}
    assert mapped_score_baseline([{**clone, 'cancelled': True}], [old], details) is None
    assert details['unmatched_sessions'] == 1 and details['ambiguous_sessions'] == 0
    assert mapped_score_baseline([clone], None, details) is None
