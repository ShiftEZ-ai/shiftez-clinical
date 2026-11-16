"""Home uses draft-local places and the reader's pending acknowledgements."""
import copy
from datetime import date

from sqlalchemy import event, select

from apps.api import models as m
from apps.api.db import transaction
from packages.domain.rota_summary import rota_summary
from .test_announcement_receipts import notice


def test_home_uses_each_drafts_canonical_fix_list_without_separate_place_actions(h):
    typ = h.configure('assignment_types', {'name': 'Three places', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 3}]}})['record']['id']
    first = h.shift(h.workspace('Two staff'), typ=typ, people=h.seed['people'][:2])['workspace']
    second = h.shift(h.workspace('No staff'), typ=typ, people=[])['workspace']
    rows = h.get('coordinator', '/api/home')['attention']
    gaps = [row for row in rows if row['key'] == 'blocking']
    summaries = {draft['id']: h.get('coordinator', f"/api/workspaces/{draft['id']}/summary")
                 for draft in (first, second)}
    assert {row['rota_id']: row['count'] for row in gaps} == {
        rota_id: summary['fix_list']['summary']['must'] for rota_id, summary in summaries.items()}
    assert {rota_id: summary['places_open'] for rota_id, summary in summaries.items()} == {first['id']: 1, second['id']: 3}
    assert all(row['rotas'] == 1 for row in gaps)
    assert not [row for row in rows if row['key'] == 'open_places']
    summary = summaries[second['id']]
    assert summary['places_needed'] == summary['places_open'] == 3
    assert summary['sessions_with_gaps'] == 1
    assert summary['issues']['blocking'] > 0
    assert summary['issues']['blocking_without_open_places'] == 0
    listed = next(row for row in h.get('coordinator', '/api/workspaces')['workspaces'] if row['id'] == second['id'])
    assert listed['health']['open_places'] == summary['places_open'] == 3
    assert h.lifecycle(second, 'submit', status=422)['error']['code'] == 'VALIDATION_FAILED'


def test_historical_gap_places_are_a_read_only_minimum_and_safety_gaps_stay_blocking():
    validation = {'error_count': 0, 'coverage': [{'shift_id': 'legacy', 'status': 'gap'},
        {'shift_id': 'safety', 'status': 'gap', 'required': 2, 'available_eligible': 2}],
        'findings': [{'code': 'COVERAGE_GAP', 'severity': 'error', 'shift_id': 'legacy'},
                     {'code': 'CAPABILITY_GAP', 'severity': 'error', 'shift_id': 'safety'}]}
    original = copy.deepcopy(validation)
    summary = rota_summary(validation)
    assert (summary['places_needed'], summary['places_filled'], summary['places_open']) == (3, 2, 1)
    assert summary['sessions_with_gaps'] == 2
    assert summary['issues']['blocking'] == 2
    assert summary['issues']['blocking_without_open_places'] == 1
    assert validation == original


def test_rota_summary_coverage_scan_budget_is_linear():
    class CountedRows(list):
        visits = 0

        def __iter__(self):
            for row in super().__iter__():
                self.visits += 1
                yield row

    for size in (500, 2000):
        rows = CountedRows({'shift_id': str(index), 'status': 'gap'} for index in range(size))
        summary = rota_summary({'coverage': rows, 'findings': []})
        assert summary['places_open'] == size
        # Deterministic row budget remains valid on shared and coverage-instrumented CI runners.
        assert rows.visits <= size * 2


def test_home_acknowledgements_include_undelivered_and_read_but_not_acknowledged_notices(h):
    required = notice(h, {'roles': ['staff']}, importance='must_acknowledge', name='Please acknowledge')
    notice(h, {'roles': ['coordinator']}, importance='must_acknowledge', name='Other audience')
    notice(h, importance='normal', name='Read only')
    h.client('staff')
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all(m.DepartmentContent(department_id=h.seed['department_id'], name=f'Expired {index}',
            starts_on=date(2020, 1, 1), ends_on=date(2020, 2, 1), importance='must_acknowledge')
            for index in range(1000))
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(h.app.state.engine, 'before_cursor_execute', capture)
    try:
        home = h.get('staff', '/api/home')
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', capture)
    pending = next(row for row in home['attention'] if row['key'] == 'acknowledgements')
    assert pending['count'] == 1
    assert pending['sample'] == [{'content_id': required['id'], 'name': required['name']}]
    content_reads = [sql for sql in statements if 'FROM department_content' in sql]
    assert len(content_reads) <= 4
    assert not any(sql.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for sql in statements)
    h.command('staff', 'department_content.seen', {'ids': [required['id']]})
    assert next(row['count'] for row in h.get('staff', '/api/home')['attention']
                if row['key'] == 'acknowledgements') == 1
    with transaction(h.app.state.engine) as db:
        receipt = db.scalar(select(m.AnnouncementReceipt).where(m.AnnouncementReceipt.content_id == required['id'],
            m.AnnouncementReceipt.identity_id == h.seed['identities']['staff']))
        target, revision = receipt.id, receipt.revision
    h.command('staff', 'notification.acknowledge', {'reason': 'I have read the notice'}, target, revision)
    assert not any(row['key'] == 'acknowledgements' for row in h.get('staff', '/api/home')['attention'])
