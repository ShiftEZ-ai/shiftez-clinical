"""Reply latency boundaries: admission now, canonical complete evidence in the durable worker."""
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from collections import Counter
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from apps.api import models as m, snapshots
from apps.api.db import transaction, retry_write
from apps.api.services import planning, rota_summary
from apps.worker.main import process_one, claim_job, finish_job
from packages.contracts import schemas as s
from packages.domain import validation, score
from tests.clock_support import freeze_clock
from tests.test_be18_command_latency import edit, worker_up
from tests.test_fix_list_performance import backend
from tests.test_p2_groups import fork
from tests.test_planning_groups_drafts import integrated, edit as group_edit, REASON


def test_edit_delta_is_revision_bound_and_scoped_to_touched_people_and_sessions():
    s1, s2, p1, p2, a1 = (str(uuid4()) for _ in range(5))
    before = {
        'workspace': {'revision': 7},
        'shifts': [{'id': s1, 'participants': [{'id': a1, 'person_id': p1}]}],
    }
    changed = [{'id': s1, 'participants': [{'id': a1, 'person_id': p2}]}]
    previous = {'findings': [{'code': 'UNAVAILABLE', 'shift_id': s1, 'person_id': p1, 'start': 'a'},
                             {'code': 'WEEKLY_LIMIT', 'shift_id': s2, 'person_id': p1, 'start': 'b'}],
                'coverage': [{'shift_id': s1, 'status': 'gap', 'required': 2, 'available_eligible': 1}]}
    current = {'findings': [{'code': 'UNAVAILABLE', 'shift_id': s1, 'person_id': p2, 'start': 'a'},
                            {'code': 'WEEKLY_LIMIT', 'shift_id': s2, 'person_id': p1, 'start': 'b'}],
               'coverage': [{'shift_id': s1, 'status': 'covered', 'required': 2, 'available_eligible': 2}]}
    delta = planning.edit_delta(before, SimpleNamespace(revision=8), changed, [], previous, current,
                                current_fingerprint='current', current_input_revision=3,
                                current_availability_revision=4)
    assert delta['base_revision'] == 7 and delta['revision'] == 8
    assert delta['shifts'] == changed and delta['removed_shift_ids'] == []
    assert delta['findings']['shift_ids'] == [s1] and set(delta['findings']['person_ids']) == {p1, p2}
    assert {(row['shift_id'], row['person_id']) for row in delta['findings']['items']} == {(s1, p2), (s2, p1)}
    assert delta['coverage']['shift_ids'] == [s1] and set(delta['coverage']['person_ids']) == {p1, p2}
    assert delta['coverage']['items'][0]['places_open'] == 0
    assert delta['summary_counts'] == {'sessions': 1, 'places_needed': 2, 'places_filled': 2,
                                       'places_open': 0, 'open_sessions': 0}
    typed = s.DraftEditDelta.model_validate(delta)
    assert typed.current_fingerprint == 'current' and typed.stale is False


def test_edit_delta_over_limit_falls_back_to_authoritative_read():
    s1 = str(uuid4())
    before = {'workspace': {'revision': 4}, 'shifts': [{'id': s1, 'participants': []}]}
    validation = {'findings': [], 'coverage': [
        {'shift_id': s1, 'status': 'covered', 'required': 1, 'available_eligible': 1}
        for _ in range(s.EDIT_DELTA_LIMITS['coverage'] + 1)]}
    assert planning.edit_delta(before, SimpleNamespace(revision=5),
                               [{'id': s1, 'participants': []}], [], {}, validation,
                               current_fingerprint='current') is None


def test_bulk_edit_deltas_chain_revisions_and_reject_gaps():
    from apps.api.services.bulk import _merge_edit_deltas

    workspace_id = str(uuid4())
    first_shift, second_shift = str(uuid4()), str(uuid4())
    def child(base, revision, shift_id):
        return {'workspace': {'id': workspace_id}, 'delta': {
            'base_revision': base, 'revision': revision, 'stale': False,
            'shifts': [{'id': shift_id, 'participants': []}],
            'removed_shift_ids': [], 'assignments': [], 'removed_assignment_ids': [],
            'findings': {'shift_ids': [shift_id], 'person_ids': [], 'items': []},
            'coverage': {'shift_ids': [shift_id], 'person_ids': [f'p-{shift_id}'], 'items': []},
            'summary_counts': {'sessions': revision}, 'current_fingerprint': 'f' * 64}}

    rows = [{'results': [child(7, 8, first_shift), child(8, 9, second_shift)]}]
    merged = _merge_edit_deltas(rows)
    assert merged is not None and merged['base_revision'] == 7 and merged['revision'] == 9
    assert {row['id'] for row in merged['shifts']} == {first_shift, second_shift}
    assert set(merged['coverage']['person_ids']) == {f'p-{first_shift}', f'p-{second_shift}'}
    assert _merge_edit_deltas([{'results': [child(7, 8, first_shift), child(9, 10, second_shift)]}]) is None


def test_published_policy_versions_keep_as_of_order_lineage_and_bounded_hydration(backend, monkeypatch):
    h = backend
    first = datetime(2026, 1, 1, tzinfo=timezone.utc)
    a, b, c, missing = [str(uuid4()) for _ in range(4)]
    versions = []
    with transaction(h.app.state.engine, write=True) as db:
        identity = db.scalar(select(m.Identity.id))
        for ident, revision, day, status, approved in [
                *[(a, revision, revision, 'active', True) for revision in range(1, 13)],
                (a, 13, 13, 'retired', True), (a, 14, 14, 'proposed', False),
                (b, 1, 1, 'active', True), (b, 2, 2, 'retired', False), (b, 3, 16, 'active', True),
                (c, 1, 5, 'active', True), (c, 2, 5, 'active', True)]:
            snapshot = {'id': ident, 'revision': revision, 'status': status,
                        'approved_by': identity if approved else None, 'definition': {'rule': {'kind': 'catalogue'}}}
            versions.append(m.DefinitionHistory(department_id=h.seed['department_id'], kind='policy', object_id=ident,
                object_revision=revision, snapshot=snapshot, actor_id=identity, reason=REASON,
                created_at=first + timedelta(days=day)))
        db.add_all(versions)
    policies = [{'id': ident} for ident in (c, a, b, missing)]
    with transaction(h.app.state.engine) as db:
        execute, hydrated = db.execute, []
        def counted(*args, **kw):
            rows = execute(*args, **kw).all()
            hydrated.append(len(rows))
            return rows
        monkeypatch.setattr(db, 'execute', counted)
        for day, lineage, expected in [(12, False, [(c, 2), (a, 12)]),
                (13, False, [(c, 2)]), (13, True, [(c, 2), (a, 13)]), (15, True, [(c, 2)])]:
            actual = snapshots.policies_as_of(db, policies, first + timedelta(days=day), keep_catalogue_lineage=lineage)
            assert [(row['id'], row['revision']) for row in actual] == expected
        # Nineteen immutable versions return only three current JSON rows per acquisition.
        assert hydrated == [3, 3, 3, 3]


def test_supervisor_precheck_uses_canonical_dated_trainee_classification(backend, monkeypatch):
    h = backend
    typ = h.configure('assignment_types', {'name': 'Dated pairing probe', 'starts_on': '2020-01-01',
        'definition': {'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
            'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1, 'require_pairing': True}}})['record']
    w = h.shift(h.workspace(), typ=typ['id'], people=[h.seed['people'][1]])['workspace']
    def required(*a, **kw):
        raise RuntimeError('full pairing context required')
    with transaction(h.app.state.engine) as db:
        row = db.scalar(select(m.Shift).where(m.Shift.workspace_id == w['id']))
        shared = snapshots.snapshot_inputs(db, db.get(m.Organisation, h.seed['organisation_id']))
        monkeypatch.setattr(planning, 'domain_snapshot', required)
        planning.name_attached_supervisors(db, db.get(m.Workspace, w['id']), row, shared_inputs=shared)
        shared['memberships'].append({'person_id': h.seed['people'][1], 'category_id': h.seed['trainee_category'],
                                     'starts_on': '2026-10-04', 'ends_on': '2026-10-05'})
        with pytest.raises(RuntimeError, match='full pairing context required'):
            planning.name_attached_supervisors(db, db.get(m.Workspace, w['id']), row, shared_inputs=shared)


def test_full_period_amendment_ledger_equals_fresh_rebuild_with_prior_and_later_history(backend):
    from apps.api.services.fairness import rebuild
    from tests.test_fairness import goal, ledger
    h = backend
    goal(h, metric='duties', period='month')
    prior = h.shift(h.workspace(start='2026-10-01', end='2026-10-04'),
                    start='2026-10-02T08:00:00+03:00', end='2026-10-02T12:00:00+03:00')['workspace']
    h.publish(prior)
    w = h.shift(h.workspace(), people=h.seed['people'][:2])['workspace']
    base = h.publish(w)
    later = h.shift(h.workspace(start='2026-10-11', end='2026-10-18'),
                    start='2026-10-12T08:00:00+03:00', end='2026-10-12T12:00:00+03:00')['workspace']
    later = h.publish(later)
    amendment = h.workspace(base=base['publication_id'])
    shift = h.detail(amendment)['shifts'][0]
    amendment = edit(h, amendment, shift, [p for p in shift['participants'] if p['person_id'] == h.seed['people'][1]])['workspace']
    final = h.publish(amendment)
    ids = [final['publication_id'], later['publication_id']]
    expected = {ident: Counter(ledger(h, publication_id=ident)) for ident in ids}
    with transaction(h.app.state.engine, write=True) as db:
        for ident in ids:
            rebuild(db, db.get(m.Publication, ident))
    assert {ident: Counter(ledger(h, publication_id=ident)) for ident in ids} == expected


@pytest.mark.parametrize('add', [False, True])
def test_reply_never_evaluates_the_rota_and_worker_stores_complete_current_evidence(backend, monkeypatch, record_property, add):
    h = backend
    freeze_clock(monkeypatch)
    w = h.shift(h.workspace(), people=[] if add else h.seed['people'][:2])['workspace']
    shift = h.detail(w)['shifts'][0]
    worker_up(h)
    client = h.client('coordinator')
    payload = {key: shift[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')}
    payload.update(workspace_id=w['id'], shift_id=shift['id'], participants=[{
        'person_id': h.seed['people'][0 if add else 1], 'start': shift['start'], 'end': shift['end']}])
    statements, acquired, scopes = [], [], []
    def counted(_c, _cu, statement, _p, _ctx, _many):
        statements.append(statement.split()[0])
    def forbidden(*a, **kw):
        pytest.fail('Edit reply waited for complete inputs, validation, scoring or diagnostics')
    canonical = validation.evaluate
    def scoped(data, **kw):
        scope = data.get('_partial')
        assert scope and not scope.get('complete')
        assert scope['shifts'] == {shift['id']} and scope['people'] <= set(h.seed['people'][:2])
        assert kw.get('explain_findings') is False
        scopes.append(scope)
        return canonical(data, **kw)
    with monkeypatch.context() as patch:
        from apps.api.services import fairness, wishes
        from packages.domain import finding_causes
        patch.setattr(planning, 'revalidate', forbidden)
        patch.setattr(validation, 'evaluate', scoped)
        patch.setattr(score, 'schedule_score', forbidden)
        patch.setattr(fairness, 'prepare_history_window', forbidden)
        patch.setattr(fairness, 'history', forbidden)
        patch.setattr(wishes, 'snapshot_wishes', forbidden)
        patch.setattr(snapshots, 'publication_score_baseline', forbidden)
        patch.setattr(finding_causes, 'FindingCauses', forbidden)
        for name in ('_domain_snapshot', '_snapshot_inputs'):
            original = getattr(snapshots, name)
            def acquisition(*a, _name=name, _original=original, **kw):
                if _name == '_domain_snapshot':
                    assert kw.get('include_fairness') is False
                    assert kw.get('include_objectives') is False
                acquired.append(_name)
                return _original(*a, **kw)
            patch.setattr(snapshots, name, acquisition)
        event.listen(h.app.state.engine, 'before_cursor_execute', counted)
        try:
            response = client.post('/api/commands', headers={'idempotency-key': str(uuid4())}, json={
                'kind': 'shift.update', 'payload': payload, 'expected_revision': w['revision']})
            assert response.status_code == 200, response.text
            result = response.json()
        finally:
            event.remove(h.app.state.engine, 'before_cursor_execute', counted)
    record_property('reply_selects', statements.count('SELECT'))
    record_property('reply_statements', len(statements))
    print({'reply_selects': statements.count('SELECT'), 'reply_statements': len(statements)})
    # Both additions and removals need D65's scoped safety check; neither needs a complete evaluation.
    assert statements.count('SELECT') <= (130 if add else 80) and len(statements) <= (160 if add else 100)
    assert acquired.count('_domain_snapshot') == acquired.count('_snapshot_inputs') == 1
    assert len(scopes) == 2  # One before and one after, reused by the reply.
    assert result['validation']['scope'] == 'command_changes'
    assert result['health']['score'] is None and result['fix_list']['summary']['checking']
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, w['id'])
        assert 'score' not in row.validation and '_session_parts' not in row.validation
        queued = db.scalar(select(m.Job).where(m.Job.workspace_id == w['id']))
        assert queued.status == 'queued' and queued.input_snapshot['validation_basis'] == 'current'
        assert 'shifts' not in queued.input_snapshot and 'policies' not in queued.input_snapshot
        job_id = queued.id
        expected = planning.revalidate(db, row, store=False, include_internal=True)
    assert process_one(h.app.state.engine)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, w['id'])
        assert row.validation == expected
        assert not row.validation.get('scope') and row.validation['score']
        assert row.revision == result['workspace']['revision']
        assert db.get(m.Job, job_id).status == 'succeeded'


def test_current_check_uses_current_inputs_but_cannot_store_after_a_later_input_change(h):
    w = h.shift(h.workspace(), people=h.seed['people'][:2])['workspace']
    shift = h.detail(w)['shifts'][0]
    worker_up(h)
    w = edit(h, w, shift, shift['participants'][1:])['workspace']
    h.policy({'kind': 'minimum_cover', 'minimum': 2})
    claim = retry_write(h.app.state.engine, claim_job)
    with transaction(h.app.state.engine) as db:
        result = planning.revalidate(db, db.get(m.Workspace, w['id']), store=False, include_internal=True)
    h.policy({'kind': 'min_rest', 'hours': 24})
    assert retry_write(h.app.state.engine, lambda db: finish_job(db, claim, result, None))
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, claim['id']).error_code == 'STALE_INPUTS'
        assert db.get(m.Workspace, w['id']).validation['scope'] == 'command_changes'


def test_validation_status_read_exposes_the_compact_complete_check_delta(h):
    w = h.shift(h.workspace('Compact validation status'), people=h.seed['people'][:2])['workspace']
    worker_up(h)
    shift = h.detail(w)['shifts'][0]
    w = edit(h, w, shift, shift['participants'][1:])['workspace']
    job = h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs'][0]
    assert job['kind'] == 'validation'
    assert process_one(h.app.state.engine)
    status = h.get('coordinator', '/api/jobs/' + job['id'] + '/status')['job']
    assert status['status'] == 'succeeded' and status['delta']['kind'] == 'complete_check'
    assert status['delta']['workspace_id'] == w['id'] and not status['delta']['partial']
    assert isinstance(status['delta']['validation']['findings'], list)
    # Status keeps the polling projection; the raw job result remains behind detail/full=true.
    assert 'input_snapshot' not in status and 'read_metadata' not in status


def test_supervised_edit_pairs_without_objective_inputs_or_diagnostic_explanations(backend, monkeypatch):
    from tests.test_attachments import setup, people
    from apps.api.services import fairness, wishes
    from packages.domain import finding_causes
    h = backend
    trainee, first, mentor, typ = setup(h)
    participants = people(trainee, first, mentor)
    w = h.shift(h.workspace(), typ=typ, people=participants[1:])['workspace']
    shift = h.detail(w)['shifts'][0]
    worker_up(h)
    canonical = validation.evaluate
    scopes = []
    def scoped(data, **kwargs):
        scope = data.get('_partial')
        assert scope and not scope.get('complete') and scope['shifts'] == {shift['id']}
        assert kwargs.get('explain_findings') is False
        scopes.append(scope)
        return canonical(data, **kwargs)
    def forbidden(*args, **kwargs):
        pytest.fail('Supervised edit acquired complete inputs or diagnostic explanations')
    monkeypatch.setattr(planning, 'revalidate', forbidden)
    monkeypatch.setattr(planning, 'evaluate', scoped)
    monkeypatch.setattr(validation, 'evaluate', scoped)
    monkeypatch.setattr(fairness, 'prepare_history_window', forbidden)
    monkeypatch.setattr(fairness, 'history', forbidden)
    monkeypatch.setattr(wishes, 'snapshot_wishes', forbidden)
    monkeypatch.setattr(snapshots, 'publication_score_baseline', forbidden)
    monkeypatch.setattr(finding_causes, 'FindingCauses', forbidden)
    monkeypatch.setattr(score, 'schedule_score', forbidden)
    # Harness.command follows edits with a full detail GET; this gate measures the edit reply itself.
    payload = {key: shift[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')}
    payload.update(workspace_id=w['id'], shift_id=shift['id'], participants=participants)
    response = h.client('coordinator').post('/api/commands', headers={'idempotency-key': str(uuid4())},
        json={'kind': 'shift.update', 'payload': payload, 'expected_revision': w['revision']})
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(scopes) >= 3  # Pairing trial plus the before/after admission.
    row = next(row for row in result['shifts'] if row['id'] == shift['id'])
    assert next(p for p in row['participants'] if p['person_id'] == trainee)['supervisor_person_id'] == mentor
    assert result['validation']['scope'] == 'command_changes'
    assert result['health']['score'] is None and result['fix_list']['summary']['checking']


def test_cancelled_check_is_stale_scoreless_and_no_longer_checking(h):
    w = h.shift(h.workspace(), people=h.seed['people'][:2])['workspace']
    shift = h.detail(w)['shifts'][0]
    worker_up(h)
    w = edit(h, w, shift, shift['participants'][1:])['workspace']
    job = h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs'][0]
    h.command('coordinator', 'job.cancel', {'reason': REASON}, job['id'], job['revision'])
    fix = h.get('coordinator', '/api/workspaces/' + w['id'] + '/fix-list')
    assert not fix['summary']['checking'] and fix['summary']['score'] is None
    with transaction(h.app.state.engine) as db:
        check = db.get(m.Workspace, w['id']).validation
        assert check['scope'] == 'command_changes' and 'score' not in check and not check.get('checking')


def test_never_checked_edit_has_no_invented_check_time_or_delta_and_reads_do_not_evaluate(h, monkeypatch):
    w = h.shift(h.workspace(), people=h.seed['people'][:2])['workspace']
    shift = h.detail(w)['shifts'][0]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, w['id']).validation = {}
    worker_up(h)
    def forbidden(*a, **kw):
        pytest.fail('Pending evidence triggered synchronous complete evaluation')
    monkeypatch.setattr(planning, 'revalidate', forbidden)
    monkeypatch.setattr(rota_summary, 'evaluate', forbidden)
    result = edit(h, w, shift, shift['participants'][1:])
    assert result['fix_list_delta']['cleared'] == [] and result['fix_list_delta']['new'] == []
    assert result['fix_list_delta']['changed'] == []
    summary = h.get('coordinator', '/api/workspaces/' + w['id'] + '/summary')
    assert summary['checked_at'] is None and summary['fix_list']['summary']['checking']
    assert summary['fix_list']['summary']['score'] is None


def test_edit_reply_health_and_delta_share_accepted_fix_count(backend):
    h = backend
    person = h.seed['people'][1]
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
              'metric': 'duties', 'comparison': 'minimum', 'amount': 3}, {'person_id': person})
    w = h.shift(h.workspace('Accepted edit count'), people=[person])['workspace']
    shift = h.detail(w)['shifts'][0]
    cause = next(c for c in h.get('coordinator', f"/api/workspaces/{w['id']}/fix-list")['causes']
                 if c['acknowledgeable'])
    accepted = [{**{key: cause[key] for key in ('evidence_hash',)}, 'cause_id': cause['id'], 'key': key,
                 'reason': 'Reviewed this shortfall', 'by': h.seed['identities']['coordinator'],
                 'at': '2026-10-04T08:00:00+03:00', 'decisions': []}
                for key in cause['finding_keys']]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, w['id']).accepted_issues = accepted
    worker_up(h)

    result = edit(h, w, shift, [])
    assert result['health']['summary']['fix_list']['summary']['must'] == result['fix_list_delta']['summary']['must']


def merge_setup(h):
    w, shift, _ = integrated(h, people=[])
    source = fork(h, w)
    local = h.detail(source)['shifts'][0]
    source = group_edit(h, source, local, [{'person_id': h.seed['people'][0],
        'start': local['start'], 'end': local['end']}])['workspace']
    # Fork creation queued an earlier validation job; settle that distinct workload first.
    assert process_one(h.app.state.engine)
    return w, source


def queued_preview(h, w, source):
    return h.command('coordinator', 'workspace.merge.preview', {'source_workspace_id': source['id'], 'reason': REASON},
        w['id'], w['revision'])['job']


def test_merge_requests_queue_without_snapshot_and_worker_keeps_final_authority(backend, monkeypatch):
    h = backend
    w, source = merge_setup(h)
    def forbidden(*a, **kw):
        pytest.fail('Merge request performed heavy work before replying')
    with monkeypatch.context() as patch:
        patch.setattr(snapshots, '_domain_snapshot', forbidden)
        patch.setattr(validation, 'evaluate', forbidden)
        job = queued_preview(h, w, source)
    assert job['status'] == 'queued' and job['kind'] == 'merge_preview'
    assert process_one(h.app.state.engine)
    completed = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert completed['status'] == 'succeeded'
    proposal = completed['result']['merge']
    with transaction(h.app.state.engine) as db:
        audits = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == 'workspace.merge.preview')))
        assert len(audits) == 2 and all(row.detail.get('private') and row.reason == 'Private draft operation' for row in audits)
    with monkeypatch.context() as patch:
        patch.setattr(snapshots, '_domain_snapshot', forbidden)
        patch.setattr(validation, 'evaluate', forbidden)
        job = h.command('coordinator', 'workspace.merge.apply', {'merge_id': proposal['id'], 'reason': REASON},
            w['id'], w['revision'])['job']
    assert job['kind'] == 'merge_apply' and job['status'] == 'queued'
    with transaction(h.app.state.engine) as db:
        audit = db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == 'workspace.merge.apply'))
        assert audit.detail.get('private') and audit.reason == 'Private draft operation'
    assert not h.get('admin', '/api/workspaces/' + w['id'])['shifts'][0]['participants']
    assert process_one(h.app.state.engine)
    completed = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert completed['status'] == 'succeeded'
    assert completed['result']['workspace']['revision'] == w['revision'] + 1
    assert 'findings' not in completed['result']['validation'] and '_session_parts' not in str(completed['result'])
    with transaction(h.app.state.engine) as db:
        audit = db.scalar(select(m.AuditEvent).where(m.AuditEvent.detail['job_id'].as_string() == job['id']))
        assert isinstance(audit.detail['input_changes'], list) and not audit.detail.get('private')
    assert h.detail(w)['shifts'][0]['participants'][0]['person_id'] == h.seed['people'][0]


@pytest.mark.parametrize('amend', [False, True])
def test_publish_checks_fresh_inputs_inside_writer_with_bounded_acquisitions(backend, monkeypatch, record_property, caplog, amend):
    h = backend
    w = h.shift(h.workspace())['workspace']
    if amend:
        issued = h.publish(w)
        w = h.workspace(base=issued['publication_id'])
        shift = h.detail(w)['shifts'][0]
        people = [{**p, 'overtime': not p['overtime']} for p in shift['participants']]
        w = edit(h, w, shift, people)['workspace']
    w = h.lifecycle(w, 'submit')['workspace']
    w = h.lifecycle(w, 'approve', 'approver')['workspace']
    acquisitions, statements, checks = [], [], []
    for name in ('_domain_snapshot', '_snapshot_inputs'):
        original = getattr(snapshots, name)
        def acquire(db, *a, _name=name, _original=original, **kw):
            acquisitions.append((_name, bool(db.connection().info.get('rota_write'))))
            return _original(db, *a, **kw)
        monkeypatch.setattr(snapshots, name, acquire)
    revalidate = planning.revalidate
    def checked(db, *a, **kw):
        assert db.connection().info.get('rota_write')
        assert not kw.get('reuse') and kw.get('acquired_data') is None
        assert kw.get('shared_inputs') is None
        checks.append(True)
        return revalidate(db, *a, **kw)
    monkeypatch.setattr(planning, 'revalidate', checked)
    evaluated, evaluate = [], planning.evaluate
    def fresh(data, **kw):
        assert checks == [True]
        evaluated.append(data['fingerprint'])
        return evaluate(data, **kw)
    monkeypatch.setattr(planning, 'evaluate', fresh)
    def counted(_c, _cu, statement, _p, _ctx, _many):
        statements.append(statement.split()[0])
    event.listen(h.app.state.engine, 'before_cursor_execute', counted)
    try:
        result = h.client('coordinator').post('/api/commands', headers={'idempotency-key': str(uuid4())}, json={
            'kind': 'workspace.publish', 'target_id': w['id'], 'expected_revision': w['revision'], 'payload': {'reason': REASON}})
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', counted)
    assert result.status_code == 200, result.text
    assert checks == [True]
    assert len(evaluated) == 1
    assert not any(row.name == 'rota.api' and row.getMessage() == 'unhandled_error' for row in caplog.records)
    # Publish acquires inputs only after flush inside the writer.
    assert acquisitions.count(('_snapshot_inputs', False)) == 0
    assert acquisitions.count(('_snapshot_inputs', True)) == 1
    assert acquisitions.count(('_domain_snapshot', False)) == 0
    assert acquisitions.count(('_domain_snapshot', True)) == 1
    with transaction(h.app.state.engine) as db:
        published = db.get(m.Publication, result.json()['publication_id'])
        assert published.fingerprint == published.validation['fingerprint'] == evaluated[0]
    record_property('publish_input_acquisitions', len(acquisitions) // 2)
    record_property('publish_selects', statements.count('SELECT'))
    record_property('publish_statements', len(statements))
    # Native measured 174/228 SELECTs before eliminating the second amendment preparation round.
    assert statements.count('SELECT') <= 240 and len(statements) <= 320
    print({'publication_amendment': amend, 'selects': statements.count('SELECT'), 'statements': len(statements)})


def test_publish_rejects_inputs_changed_after_read_basis(backend, monkeypatch):
    h = backend
    w = h.lifecycle(h.shift(h.workspace())['workspace'], 'submit')['workspace']
    w = h.lifecycle(w, 'approve', 'approver')['workspace']
    from apps.api import commands
    original, changed_inputs = commands.expected, []
    def changed(row, revision):
        result = original(row, revision)
        if row.id == w['id'] and not changed_inputs:
            with transaction(h.app.state.engine, write=True) as writer:
                # The captured ceiling still binds when capacity rises. Lower it to exercise the publish race.
                writer.get(m.Location, h.seed['locations'][0]).capacity -= 1
            changed_inputs.append(True)
        return result
    monkeypatch.setattr(commands, 'expected', changed)
    result = h.lifecycle(w, 'publish', status=409)
    # Scheduling-only drift retains the actual approval refusal of the previous publish path.
    assert result['error']['code'] == 'STALE_APPROVAL'
    assert changed_inputs == [True]
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Workspace, w['id']).status == 'approved'
        assert db.scalar(select(m.Publication.id).where(m.Publication.workspace_id == w['id'])) is None


@pytest.mark.parametrize('rule', [
    {'kind': 'restriction'}, {'kind': 'max_duties', 'maximum': 0, 'window': 'week'},
    {'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
     'metric': 'duties', 'comparison': 'maximum', 'amount': 0}])
def test_live_worker_keeps_synchronous_restriction_and_zero_maximum_refusals(h, rule):
    w = h.shift(h.workspace(), people=[])['workspace']
    shift = h.detail(w)['shifts'][0]
    pid = h.seed['people'][0]
    h.policy(rule, {'person_id': pid})
    worker_up(h)
    result = edit(h, w, shift, [{'person_id': pid, 'start': shift['start'], 'end': shift['end']}], status=422)
    kind = rule['kind']
    assert result['error']['code'] == ('EDIT_INVALID' if kind == 'restriction' else 'PERSON_UNAVAILABLE')
    if kind != 'restriction':
        code = 'MAX_DUTIES' if kind == 'max_duties' else 'QUOTA_ABOVE_MAXIMUM'
        assert any(finding['code'] == code for finding in result['error']['details']['findings'])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Workspace, w['id']).revision == w['revision']
        assert not db.scalar(select(m.Job.id).where(m.Job.workspace_id == w['id']))


def test_merge_job_reads_and_controls_are_requester_only(h):
    w, source = merge_setup(h)
    job = queued_preview(h, w, source)
    for suffix in ('', '/status'):
        h.get('admin', '/api/jobs/' + job['id'] + suffix, status=404)
    assert job['id'] not in {j['id'] for j in h.get('admin', '/api/jobs?workspace_id=' + w['id'])['jobs']}
    h.command('admin', 'job.cancel', {'reason': REASON}, job['id'], job['revision'], status=404)


def test_merge_final_input_race_rolls_back_and_records_failed_job(h, monkeypatch):
    from apps.worker import workspace_merge
    w, source = merge_setup(h)
    job = queued_preview(h, w, source)
    original = workspace_merge.run
    def changed_after_preparation(*a, **kw):
        plan = original(*a, **kw)
        h.policy({'kind': 'minimum_cover', 'minimum': 3})
        return plan
    monkeypatch.setattr(workspace_merge, 'run', changed_after_preparation)
    assert process_one(h.app.state.engine)
    row = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert row['status'] == 'failed' and row['error_code'] == 'STALE_INPUTS'
    assert not h.detail(w)['shifts'][0]['participants']


@pytest.mark.parametrize('change', ['cancel', 'source', 'inputs', 'authority'])
def test_late_merge_claim_cannot_override_cancellation_changed_inputs_or_authority(h, change):
    from apps.worker.workspace_merge import run
    w, source = merge_setup(h)
    job = queued_preview(h, w, source)
    claim = retry_write(h.app.state.engine, claim_job)
    plan = run(h.app.state.engine, claim, progress=lambda _: None, cancelled=lambda: False)
    if change == 'cancel':
        h.command('coordinator', 'job.cancel', {'reason': REASON}, job['id'], job['revision'])
        assert not retry_write(h.app.state.engine, lambda db: finish_job(db, claim, plan, None))
    else:
        if change == 'source':
            h.command('coordinator', 'workspace.archive', {'reason': REASON}, source['id'], source['revision'])
        elif change == 'inputs':
            h.policy({'kind': 'minimum_cover', 'minimum': 3})
        else:
            with transaction(h.app.state.engine, write=True) as db:
                binding = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == claim['requested_by'],
                    m.RoleBinding.role == 'coordinator', m.RoleBinding.revoked.is_(False)))
                binding.revoked = True
        if change == 'authority':
            assert retry_write(h.app.state.engine, lambda db: finish_job(db, claim, plan, None))
        else:
            from apps.api.errors import Problem
            with pytest.raises(Problem):
                retry_write(h.app.state.engine, lambda db: finish_job(db, claim, plan, None))
    assert not h.get('admin', '/api/workspaces/' + w['id'])['shifts'][0]['participants']
