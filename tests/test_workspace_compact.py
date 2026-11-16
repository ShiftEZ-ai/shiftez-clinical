"""Compact first reads retain authority and defer large view-specific data."""
import json
from copy import deepcopy

from sqlalchemy import delete, event

from apps.api import models as m
from apps.api.db import retry_write, transaction
from apps.api.services import planning
from apps.worker.main import claim_job, finish_job
from packages.contracts import schemas as s
from packages.domain.time import aware
from tests.test_backend_scale import scale_department
from tests.test_fix_list_performance import backend


def test_compact_matches_review_and_stored_summary_and_revalidates_without_validation(backend):
    h = backend
    w = h.lifecycle(h.shift(h.workspace())['workspace'], 'submit')['workspace']
    path = '/api/workspaces/' + w['id']
    client = h.client('approver')
    full = client.get(path)
    assert full.status_code == 200
    first = client.get(path, params={'view': 'compact'})
    assert first.status_code == 200, first.text
    value = first.json()
    s.WorkspaceCompactDetail.model_validate(value)
    for key in ('review_progress', 'reviewer_involved', 'can_change_fixed', 'can_redo', 'swap_id', 'approvals', 'review_items'):
        assert value[key] == full.json()[key], key
    assert 'shifts' not in value and 'wishes' not in value and 'change_diff' not in value
    assert 'findings' not in value['workspace']['validation']
    assert 'review_snapshot' not in value['workspace']
    listed = next(row for row in h.get('approver', '/api/workspaces')['workspaces'] if row['id'] == w['id'])
    assert value['health'] == listed['health']
    assert value['summary'] == listed['health']['summary']
    statements = []
    def capture(_conn, _cursor, sql, _parameters, _context, _many):
        statements.append(sql)
    event.listen(h.app.state.engine, 'before_cursor_execute', capture)
    try:
        repeat = client.get(path, params={'view': 'compact'}, headers={'if-none-match': first.headers['etag']})
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', capture)
    assert repeat.status_code == 304 and repeat.content == b''
    expensive = [sql for sql in statements if 'workspaces.validation' in sql or sql.startswith('SELECT shifts.')]
    assert not expensive, expensive
    assert repeat.headers['cache-control'] == 'private, no-cache'
    assert client.get(path + '/review', params={'view': 'compact'}).json() == value
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
    changed = client.get(path, params={'view': 'compact'}, headers={'if-none-match': first.headers['etag']})
    assert changed.status_code == 200 and changed.headers['etag'] != first.headers['etag']
    assert client.get(path, headers={'if-none-match': full.headers['etag']}).status_code == 200


def test_workspace_reads_revalidate_after_a_worker_check_finishes_without_a_workspace_write(backend):
    h = backend
    w = h.shift(h.workspace('Check read invalidation'), people=[])['workspace']
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'validation'},
                    revision=w['revision'])['job']
    claim = retry_write(h.app.state.engine, claim_job)
    assert claim and claim['id'] == job['id']
    with transaction(h.app.state.engine) as db:
        result = planning.revalidate(db, db.get(m.Workspace, w['id']), store=False, include_internal=True)
    client = h.client('coordinator')
    paths = [f"/api/workspaces/{w['id']}", f"/api/workspaces/{w['id']}?view=compact"]
    before = [client.get(path) for path in paths]
    assert all(response.status_code == 200 for response in before)
    assert retry_write(h.app.state.engine, lambda db: finish_job(db, claim, result, 'CHECK_FAILED'))
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, job['id']).status == 'failed'
    after = [client.get(path, headers={'If-None-Match': response.headers['etag']})
             for path, response in zip(paths, before)]
    assert all(response.status_code == 200 for response in after)
    assert all(response.headers['etag'] != before[index].headers['etag'] for index, response in enumerate(after))


def test_compact_legacy_projection_and_unchecked_workspace(backend):
    h = backend
    w = h.workspace()
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, w['id']).validation = None
        db.flush()
        db.execute(delete(m.WorkspaceReadProjection).where(m.WorkspaceReadProjection.workspace_id == w['id']))
    value = h.get('coordinator', f"/api/workspaces/{w['id']}?view=compact")
    assert value['summary'] is None and value['health'] == {'checked': False, 'fresh': False}
    assert value['workspace']['validation'] == {}


def test_windowed_parts_page_overlap_findings_and_keep_full_detail(backend):
    h = backend
    w = h.workspace()
    for start, end in [('2026-10-04T23:00:00+03:00', '2026-10-05T03:00:00+03:00'),
                       ('2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00'),
                       ('2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00')]:
        w = h.shift(w, start, end, people=[])['workspace']
    full = h.detail(w)
    path = f"/api/workspaces/{w['id']}/parts"
    client = h.client('coordinator')
    query = {'part': 'shifts', 'start': '2026-10-05', 'end': '2026-10-06', 'limit': 1}
    first = client.get(path, params=query)
    assert first.status_code == 200, first.text
    page = first.json()
    s.WorkspacePartPage.model_validate(page)
    assert page['total'] == 2 and page['has_more']
    second = client.get(path, params={**query, 'offset': 1}).json()
    assert not second['has_more']
    assert [row['id'] for row in page['items'] + second['items']] == [row['id'] for row in full['shifts'][:2]]
    assert client.get(path, params=query, headers={'if-none-match': first.headers['etag']}).status_code == 304
    findings = client.get(path, params={**query, 'part': 'validation', 'limit': 500})
    assert findings.status_code == 200, findings.text
    assert findings.json()['items']
    assert all(row in full['workspace']['validation']['findings'] for row in findings.json()['items'])
    outside_id = full['shifts'][-1]['id']
    assert all(row.get('shift_id') != outside_id for row in findings.json()['items'])
    assert client.get(path, params={**query, 'start': query['end']}).status_code == 422
    assert client.get(path, params={**query, 'limit': h.settings.workspace_read_page_size + 1}).status_code == 422
    assert client.get(path, params={**query, 'part': 'unsupported'}).status_code == 422
    original = client.get(f"/api/workspaces/{w['id']}")
    assert original.json() == full
    assert client.get(f"/api/workspaces/{w['id']}", headers={'if-none-match': original.headers['etag']}).status_code == 304


def test_windowed_shift_keeps_conflicts_with_overnight_peers_outside_the_window(backend):
    h = backend
    w = h.shift(h.workspace(), '2026-10-04T23:00:00+03:00', '2026-10-05T03:00:00+03:00')['workspace']
    w = h.shift(w, '2026-10-05T01:00:00+03:00', '2026-10-05T04:00:00+03:00')['workspace']
    full = h.detail(w)['shifts']
    assert full[0]['double_booking_conflicts']
    path = f"/api/workspaces/{w['id']}/parts?part=shifts&start=2026-10-04&end=2026-10-05&limit=1"
    page = h.get('coordinator', path)
    assert page['total'] == 1 and not page['has_more']
    assert [row['id'] for row in page['items']] == [full[0]['id']]
    assert page['items'][0]['double_booking_conflicts'] == full[0]['double_booking_conflicts']


def test_window_coverage_matches_full_intervals_and_pages_independently_of_findings(backend):
    h = backend
    w = h.shift(h.workspace(), '2026-10-04T23:00:00+03:00', '2026-10-05T03:00:00+03:00', people=[])['workspace']
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[])['workspace']
    w = h.shift(w, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00', people=[])['workspace']
    w = h.lifecycle(w, 'validate')['workspace']
    # Split an overnight session's stored coverage at local midnight; keep a legacy undated row as well.
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, w['id'])
        check = deepcopy(row.validation)
        night = min(check['coverage'], key=lambda c: c['start'])
        midnight = '2026-10-05T00:00:00+03:00'
        check['coverage'] = [c for c in check['coverage'] if c is not night] + [
            {**night, 'end': midnight}, {**night, 'start': midnight},
            {key: value for key, value in night.items() if key not in {'start', 'end'}}]
        row.validation = check
    full = h.detail(w)
    lo, hi = aware('2026-10-05T00:00:00+03:00'), aware('2026-10-06T00:00:00+03:00')
    ids = {row['id'] for row in full['shifts'] if aware(row['start']) < hi and aware(row['end']) > lo}
    expected = [row for row in full['workspace']['validation']['coverage'] if row['shift_id'] in ids
        and (not row.get('start') or not row.get('end') or (aware(row['start']) < hi and aware(row['end']) > lo))]
    assert len(expected) == 3 and any(row['status'] == 'gap' for row in expected)
    path = f"/api/workspaces/{w['id']}/parts"
    query = {'part': 'validation', 'start': '2026-10-05', 'end': '2026-10-06', 'limit': 1}
    client = h.client('coordinator')
    collected = []
    for offset in range(len(expected)):
        response = client.get(path, params={**query, 'offset': offset})
        assert response.status_code == 200, response.text
        page = s.WorkspacePartPage.model_validate(response.json())
        assert page.coverage_total == len(expected) and len(page.coverage) == 1
        assert page.coverage_has_more == (offset + 1 < len(expected))
        collected.extend(page.coverage)
    assert collected == expected
    first = client.get(path, params=query)
    assert client.get(path, params=query, headers={'if-none-match': first.headers['etag']}).status_code == 304
    # A worker check can change coverage without changing the workspace revision or the findings page.
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, w['id'])
        check = deepcopy(row.validation)
        check['coverage'] = [{**c, 'required': c['required'] + 1} if c == expected[0] else c for c in check['coverage']]
        row.validation = check
    changed = client.get(path, params=query, headers={'if-none-match': first.headers['etag']})
    assert changed.status_code == 200 and changed.headers['etag'] != first.headers['etag']
    assert changed.json()['revision'] == first.json()['revision']
    assert changed.json()['items'] == first.json()['items']
    assert changed.json()['coverage'][0]['required'] == expected[0]['required'] + 1
    empty = client.get(path, params={**query, 'start': '2026-10-08', 'end': '2026-10-09'}).json()
    assert empty['coverage'] == [] and empty['coverage_total'] == 0 and not empty['coverage_has_more']
    shifts = client.get(path, params={**query, 'part': 'shifts'}).json()
    assert shifts['coverage'] == [] and shifts['coverage_total'] == 0 and not shifts['coverage_has_more']


def test_covered_window_with_no_findings_still_exposes_all_coverage_pages(backend):
    h = backend
    w = h.shift(h.workspace())['workspace']
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00')['workspace']
    w = h.lifecycle(w, 'validate')['workspace']
    full = h.detail(w)['workspace']['validation']
    assert full['findings'] == [] and len(full['coverage']) == 2
    path = f"/api/workspaces/{w['id']}/parts?part=validation&start=2026-10-04&end=2026-10-06&limit=1"
    first = h.get('coordinator', path)
    assert first['items'] == [] and first['has_more'] is False and first['coverage_has_more'] is True
    second = h.get('coordinator', path + '&offset=1')
    assert second['coverage_has_more'] is False
    assert first['coverage'] + second['coverage'] == full['coverage']


def test_compact_and_parts_enforce_current_access_even_with_etag(backend):
    h = backend
    w = h.command('coordinator', 'workspace.create', {'name': 'Private compact rota', 'starts_on': '2026-10-04',
        'ends_on': '2026-10-11', 'visibility': 'private', 'purpose': 'sandbox'})['workspace']
    paths = [f"/api/workspaces/{w['id']}?view=compact",
             f"/api/workspaces/{w['id']}/parts?part=shifts&start=2026-10-04&end=2026-10-05",
             f"/api/workspaces/{w['id']}/parts?part=validation&start=2026-10-04&end=2026-10-05"]
    for path in paths:
        response = h.client('coordinator').get(path)
        assert response.status_code == 200, response.text
        headers = {'if-none-match': response.headers['etag']}
        assert h.client('approver').get(path, headers=headers).status_code == 404
        assert h.client('staff').get(path, headers=headers).status_code == 403
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed['identities']['coordinator']).disabled = True
    assert h.client('coordinator').get(paths[0], headers=headers).status_code == 401


def test_department_scale_compact_byte_budget(backend):
    h = backend
    w, data, *_ = scale_department(h)
    assert len(data['people']) == 163 and len(data['focus_ids']) == 768
    w = h.lifecycle(w, 'validate')['workspace']
    client = h.client('coordinator')
    path = f"/api/workspaces/{w['id']}"
    full = client.get(path)
    compact = client.get(path, params={'view': 'compact'})
    assert full.status_code == compact.status_code == 200
    print('Workspace bytes:', json.dumps({'backend': h.app.state.engine.dialect.name,
        'full': len(full.content), 'compact': len(compact.content)}))
    assert len(full.content) > 1_000_000
    # Measured 3,474 bytes on this 163-person / 768-session fixture; allow ample review metadata headroom.
    assert len(compact.content) < 16_384
    assert len(compact.content) < len(full.content) / 10
    repeat = client.get(path, params={'view': 'compact'}, headers={'if-none-match': compact.headers['etag']})
    assert repeat.status_code == 304 and len(repeat.content) == 0
    query = {'part': 'validation', 'start': '2026-10-04', 'end': '2026-10-11'}
    window = client.get(path + '/parts', params=query)
    assert window.status_code == 200
    expected = [row for row in full.json()['workspace']['validation']['coverage']
        if aware(row['start']) < aware('2026-10-11T00:00:00+03:00')
        and aware(row['end']) > aware('2026-10-04T00:00:00+03:00')]
    assert len(expected) == 192 and window.json()['coverage'] == expected
    assert window.json()['coverage_total'] == 192 and not window.json()['coverage_has_more']
    print('Window validation bytes:', json.dumps({'backend': h.app.state.engine.dialect.name,
        'with_coverage': len(window.content), 'coverage_rows': len(expected)}))
    # Measured 178,450 bytes with 192 coverage rows; retain headroom for review/diagnostic metadata.
    assert len(window.content) < 256 * 1024
    assert client.get(path + '/parts', params=query, headers={'if-none-match': window.headers['etag']}).status_code == 304
