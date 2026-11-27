"""Audit T2: unpublished effects, exact deltas, actionable review and personal notices."""
import copy
from pathlib import Path
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import planning, published_changes, swaps
from apps.worker.main import deliver_notifications
from packages.contracts.schemas import RotaReviewItem
from .governance_support import identity
from .test_published_changes import change, configure_urgent, drift, issued

pytestmark = pytest.mark.postgres


@pytest.fixture
def h(pg):
    return pg


def send(h, pub, result, **values):
    return h.command('coordinator', 'publication.changes.submit', {'reason': 'Review the precise published change', **values},
                     target=pub, revision=result['workspace']['revision'])


def notify(h, event_key, role='staff'):
    with transaction(h.app.state.engine, write=True) as db:
        event = db.scalar(select(m.Outbox).where(m.Outbox.event_key == event_key))
        assert event is not None
        deliver_notifications(db, event)
    return next(n for n in h.get(role, '/api/notifications')['notifications'] if n['outbox_id'] == event.id)


def clinic(h):
    from .test_series_overlap_api import candidate, reviewed
    from .test_series_reconciliation import materialise
    value = candidate(h)
    value['definition']['close_when_all_usual_unavailable'] = True
    w = materialise(h, reviewed(h, value))
    w = h.shift(w, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00', people=[h.seed['people'][1]])['workspace']
    pub = h.publish(w)['publication_id']
    return pub, h.get('coordinator', '/api/publications/' + pub)['snapshot']['shifts']


def test_unsent_absence_leaves_place_open_and_explicit_close_is_undoable(h):
    configure_urgent(h)
    pub, shifts = clinic(h)
    frozen = h.get('coordinator', '/api/publications/' + pub)['snapshot']
    absent = change(h, pub, shifts[0], kind='absence', person_id=None)
    session = absent['change']['sessions'][0]
    assert not session['closed'] and session['closes_session']
    assert session['open_places'] == 1 and session['replace_available']
    assert not h.detail(absent['workspace'])['shifts'][0]['closed']
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.Availability)) == 0
        assert not db.scalar(select(m.Outbox.id).where(m.Outbox.kind == 'availability'))
    # Explicit closure is a separate session-level change; it does not come
    # from the absence above and is itself undoable.
    explicit = change(h, pub, shifts[0], kind='cancel', assignment_id=None, person_id=None,
                      reason='Explicitly remove the needed place')
    closed = explicit['change']['sessions'][0]
    assert closed['cancelled'] and closed['closes_session']
    assert closed['removes_needed_place']
    assert closed['open_places'] == 0 and not closed['replace_available']
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.action == 'publication.change',
            m.AuditEvent.target_id == pub, m.AuditEvent.reason == 'Explicitly remove the needed place'))
    restored_close = h.command('coordinator', 'publication.change.undo', {'change_id': explicit['change']['id'],
        'reason': 'Keep the required place open'}, target=pub, revision=explicit['workspace']['revision'])
    restored_close_session = next(row for row in h.detail(restored_close['workspace'])['shifts']
                                  if row['id'] == closed['id'])
    assert not restored_close_session['cancelled'] and not restored_close_session.get('closed', False)
    assert restored_close_session['participants'] == []
    assert next(c for c in restored_close['changes'] if c['id'] == absent['change']['id'])['sessions'][0]['open_places'] == 1
    replacement = change(h, pub, shifts[1])
    sent = send(h, pub, replacement, urgency='urgent', change_ids=[replacement['change']['id']])
    assert len(sent['change_diff']['sessions']) == 1
    assert not h.detail(sent['workspace'])['shifts'][0]['closed']
    approved = h.lifecycle(sent['workspace'], 'approve', 'approver')['workspace']
    new_pub = h.lifecycle(approved, 'publish')['publication_id']
    published = h.get('coordinator', '/api/publications/' + new_pub)['snapshot']
    assert not published['shifts'][0]['closed']
    assert published['shifts'][0]['participants'][0]['person_id'] == h.seed['people'][0]
    old_list = h.get('coordinator', f'/api/publications/{pub}/pending-changes')['changes']
    assert next(c for c in old_list if c['id'] == absent['change']['id'])['status'] == 'moved'
    pending = h.get('coordinator', f'/api/publications/{new_pub}/pending-changes')
    assert not h.detail(pending['workspace'])['shifts'][0]['closed']
    undone = h.command('coordinator', 'publication.change.undo', {'change_id': absent['change']['id'],
        'reason': 'Restore the unpublished absence'}, target=new_pub, revision=pending['workspace']['revision'])
    restored = h.detail(undone['workspace'])['shifts'][0]
    assert not restored['closed'] and restored['participants'][0]['person_id'] == h.seed['people'][0]
    assert h.get('coordinator', '/api/publications/' + new_pub)['snapshot'] == published
    assert h.get('coordinator', '/api/publications/' + pub)['snapshot'] == frozen
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.Availability)) == 0


@pytest.mark.parametrize('kind', ['absence', 'replace'])
def test_only_approved_candidate_absences_are_committed_and_notified(h, kind, monkeypatch):
    pub, shifts = clinic(h)
    values = {'kind': kind, 'absence_reason': 'Synthetic sick cover'}
    if kind == 'absence':
        values['person_id'] = None
    result = change(h, pub, shifts[0], **values)
    if kind == 'absence':
        assert not result['validation']['valid']
        result = change(h, pub, shifts[0], kind='cancel', assignment_id=None, person_id=None,
                        reason='Explicitly remove this required place for the approved closure')
    sent = send(h, pub, result)
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.Availability)) == 0
    approved = h.lifecycle(sent['workspace'], 'approve', 'approver')['workspace']
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Outbox.id).where(m.Outbox.kind == 'absence_approved'))
        assert not db.scalar(select(m.Outbox.id).where(m.Outbox.kind == 'availability'))
    from apps.api.services import fairness
    on_publish, observed = fairness.on_publish, []
    def ledger(db, *args, **kwargs):
        observed.append(db.scalar(select(func.count()).select_from(m.Availability)))
        assert observed[-1] == 1
        assert kwargs.get('data') is None  # Newly materialized absence inputs require fresh reconstruction.
        return on_publish(db, *args, **kwargs)
    monkeypatch.setattr(fairness, 'on_publish', ledger)
    final = h.lifecycle(approved, 'publish')
    assert observed
    issued = h.get('coordinator', '/api/publications/' + final['publication_id'])['snapshot']['shifts'][0]
    assert not issued['closed']
    assert issued['cancelled'] is (kind == 'absence')
    with transaction(h.app.state.engine) as db:
        absence = db.scalar(select(m.Availability))
        assert absence.person_id == h.seed['people'][0] and not absence.retracted
        # SW3-B-08: one notice per person per amendment.
        assert db.scalar(select(m.Outbox.id).where(m.Outbox.event_key == f"availability-published:{sent['workspace']['id']}:{absence.person_id}"))


def test_same_slot_sessions_pair_by_lineage_through_review_and_publication(h):
    w = h.shift(h.workspace())['workspace']
    w = h.shift(w, people=[h.seed['people'][1]])['workspace']
    pub = h.publish(w)['publication_id']
    shifts = h.get('coordinator', '/api/publications/' + pub)['snapshot']['shifts']
    drift(h)
    first = change(h, pub, shifts[0])
    sent = send(h, pub, first)
    for candidate in (first, sent):
        assert len(candidate['change_diff']['sessions']) == 1
        assert not any(f['code'] == 'TYPE_VERSION_CHANGED' for f in candidate['validation']['findings'])
    review = h.get('approver', '/api/workspaces/' + sent['workspace']['id'])
    assert len(review['change_diff']['sessions']) == 1
    assert review['change_diff']['review_week_start'] == '2026-10-04'
    assert h.get('approver', f"/api/workspaces/{sent['workspace']['id']}/fix-list")['summary']['must'] == 0
    approved = h.lifecycle(sent['workspace'], 'approve', 'approver')['workspace']
    assert h.lifecycle(approved, 'publish')['publication_id']
    from packages.domain.input_manifest import manifest
    data = h.snapshot(approved)
    original = manifest(data)['token']
    data['shifts'][0]['lineage_id'] = m.uid()
    assert manifest(data)['token'] != original


def test_legacy_pairing_matches_unchanged_duplicates_before_changed_slot(h):
    pub, shifts = issued(h)
    old = [shifts[0], {**copy.deepcopy(shifts[0]), 'id': m.uid()}, shifts[1]]
    new = [{**copy.deepcopy(sh), 'id': m.uid(), 'lineage_id': None} for sh in old]
    new[-1]['participants'][0]['person_id'] = h.seed['people'][2]
    result = published_changes.delta(old, new)
    assert len(result) == 1 and result[0]['before']['id'] == old[-1]['id']
    assert all(a and b for a, b in published_changes.pairs(old, new))


@pytest.mark.parametrize('action', ['approve', 'publish'])
def test_real_changed_session_blocker_is_refused_server_side(h, monkeypatch, action):
    pub, shifts = issued(h)
    sent = send(h, pub, change(h, pub, shifts[0]))
    workspace = sent['workspace']
    session, = sent['change_diff']['sessions']
    shift = session['after']
    finding = {'code': 'NOT_ELIGIBLE', 'severity': 'error', 'message': 'Current eligibility missing',
               'shift_id': shift['id'], 'person_id': h.seed['people'][2], 'rule_id': None,
               'start': shift['start'], 'end': shift['end']}

    def with_finding(result):
        return {**result, 'valid': False, 'error_count': result['error_count'] + 1,
                'findings': [*result['findings'], finding]}

    if action == 'approve':
        # Approval reuses the submitted check while its input fingerprint matches.
        with transaction(h.app.state.engine, write=True) as db:
            row = db.get(m.Workspace, workspace['id'])
            row.validation = {**with_finding(row.validation),
                              'change_findings': [*row.validation['change_findings'], finding]}
    else:
        workspace = h.lifecycle(workspace, 'approve', 'approver')['workspace']
        evaluate = planning.evaluate

        def unsafe(data, **kwargs):
            assert shift['id'] in data['focus_ids']
            return with_finding(evaluate(data, **kwargs))

        monkeypatch.setattr(planning, 'evaluate', unsafe)

    refused = h.lifecycle(workspace, action, 'approver' if action == 'approve' else 'coordinator', status=422)
    assert refused['error']['code'] == 'VALIDATION_FAILED'
    assert any(f['code'] == finding['code'] and f['shift_id'] == shift['id'] and f['person_id'] == finding['person_id']
               for f in refused['error']['details']['findings'])
    assert h.detail(workspace)['workspace']['status'] == workspace['status']


def test_inherited_adjustments_are_effective_but_only_new_named_items_need_review(h):
    from .test_block_quotas import quota
    rule = quota(h, comparison='target', amount=1, severity='advisory')
    w = h.shift(h.workspace())['workspace']
    w = h.command('coordinator', 'workspace.rule_adjust', {'policy_id': rule['id'], 'mode': 'off',
        'reason': 'Original independently reviewed adjustment'}, target=w['id'], revision=w['revision'])['workspace']
    items = h.detail(w)['review_items']
    assert len(items) == 1 and RotaReviewItem.model_validate(items[0]).name == rule['name']
    sent = h.lifecycle(w, 'submit')['workspace']
    approved = h.command('approver', 'workspace.approve', {'reason': 'Accept original adjustment',
        'decisions': [{'item_id': items[0]['id'], 'kind': 'adjustment', 'accept': True}]}, target=sent['id'], revision=sent['revision'])['workspace']
    pub = h.lifecycle(approved, 'publish')['publication_id']
    shift = h.get('coordinator', '/api/publications/' + pub)['snapshot']['shifts'][0]
    result = change(h, pub, shift)
    assert result['workspace']['rule_adjustments'][0]['mode'] == 'off'
    assert h.detail(result['workspace'])['review_items'] == []
    submitted = send(h, pub, result)
    assert submitted['review_items'] == []
    assert h.lifecycle(submitted['workspace'], 'approve', 'approver')['workspace']['status'] == 'approved'


def test_change_findings_include_person_dates_and_level_before_and_after_send(h):
    from .test_block_quotas import quota
    pub, shifts = issued(h)
    quota(h, scope={'person_id': h.seed['people'][2]}, comparison='target', amount=20, severity='advisory')
    result = change(h, pub, shifts[0])
    findings = result['change']['findings']
    assert findings and all(f['person_name'] and f['starts_on'] and f['level'] == 'could' for f in findings)
    sent = send(h, pub, result)
    assert sent['changes'][0]['findings']
    assert {f['person_id'] for f in sent['changes'][0]['findings']} == {h.seed['people'][2]}


@pytest.mark.parametrize('legacy', [False, True])
def test_returned_amendment_exposes_reason_and_edit_undo_state(h, legacy):
    pub, shifts = issued(h)
    sent = send(h, pub, change(h, pub, shifts[0]))
    h.command('approver', 'workspace.reopen', {'reason': 'Use Replace before resending'},
              target=sent['workspace']['id'], revision=sent['workspace']['revision'])
    if legacy:
        with transaction(h.app.state.engine, write=True) as db:
            row = db.scalar(select(m.PublicationChangeList).where(m.PublicationChangeList.publication_id == pub))
            row.changes = [{**c, 'status': 'submitted', 'return_note': None} for c in row.changes]
    pending = h.get('coordinator', f'/api/publications/{pub}/pending-changes')
    returned = pending['changes'][0]
    assert returned['status'] == 'submitted'
    assert returned['returned']['reason'] == 'Use Replace before resending'
    assert returned['returned']['by_name']
    assert returned['can_edit'] and returned['can_undo'] and returned['can_resubmit']
    withdrawn = h.command('coordinator', 'publication.changes.withdraw',
        {'reason': 'Taken back to edit after it was returned'}, target=pub, revision=pending['workspace']['revision'])
    assert withdrawn['changes'][0]['status'] == 'draft' and withdrawn['changes'][0]['returned'] is None
    undone = h.command('coordinator', 'publication.change.undo',
        {'change_id': returned['id'], 'reason': 'Undo the returned amendment'}, target=pub,
        revision=withdrawn['workspace']['revision'])
    assert undone['changes'][0]['status'] == 'withdrawn'
    assert not undone['changes'][0]['can_undo'] and undone['changes'][0]['returned'] is None


def test_absence_report_home_replace_approval_notice_and_resolved_row(h):
    pub, shifts = issued(h)
    requested = h.command('staff', 'request.create', {'kind': 'absence_report', 'publication_id': pub,
        'assignment_id': shifts[0]['participants'][0]['id'], 'reason': 'Confidential synthetic report'})['record']
    row = next(r for r in h.get('coordinator', '/api/home')['attention'] if r['key'] == 'absence_reported')
    assert row['sample'][0]['status'] == 'submitted'
    assert row['sample'][0]['replace_action']['assignment_id'] == shifts[0]['participants'][0]['id']
    h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Independent absence decision'},
              target=requested['id'], revision=requested['revision'])
    notice = notify(h, 'request-decision:' + requested['id'], 'coordinator')
    assert 'Replace' in notice['message'] and 'Oct' in notice['message']
    assert notice['replace_action']['shift_id'] == shifts[0]['id']
    assert 'Confidential' not in notice['message']
    changed = change(h, pub, shifts[0])
    sent = send(h, pub, changed)
    approved = h.lifecycle(sent['workspace'], 'approve', 'approver')['workspace']
    h.lifecycle(approved, 'publish')
    assert not any(r['key'] == 'absence_reported' for r in h.get('coordinator', '/api/home')['attention'])


def test_home_merges_approved_absences_and_operational_warning_per_session(h):
    identity(h, 'staff_two', ['staff'], h.seed['people'][1])
    pub = h.publish(h.shift(h.workspace('Two absent staff'), people=h.seed['people'][:2])['workspace'])['publication_id']
    shift = h.get('coordinator', f'/api/publications/{pub}')['snapshot']['shifts'][0]
    requests = []
    by_person = {p['person_id']: p for p in shift['participants']}
    for role, assignment in (('staff', by_person[h.seed['people'][0]]), ('staff_two', by_person[h.seed['people'][1]])):
        requests.append(h.command(role, 'request.create', {'kind': 'absence_report', 'publication_id': pub,
            'assignment_id': assignment['id'], 'reason': 'Cannot attend this duty'})['record'])
    for request in requests:
        h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Approve absence for cover'},
                  target=request['id'], revision=request['revision'])
    attention = h.get('coordinator', '/api/home')['attention']
    absence = next(row for row in attention if row['key'] == 'absence_reported')
    assert absence['count'] == 1 and len(absence['sample']) == 1
    assert not any(row['key'] == 'published_absence_conflicts' for row in attention)


def test_single_absence_reports_only_current_change_causes_and_the_actual_open_place(h):
    # Safety kinds are always mandatory. A cover target supplies the unchanged warnings.
    h.cover_target(2)
    pub, shifts = issued(h)
    result = change(h, pub, shifts[0], kind='absence', person_id=None)
    session = result['change']['sessions'][0]
    assert session['open_places'] == 1 and not session['closed']
    assert session['closes_session'] and session['replace_available']
    # Unchanged advisory checks remain publication evidence, but are not new
    # department-wide amendment causes or new toast sentences.
    assert len({f['shift_id'] for f in result['validation']['findings'] if f.get('shift_id')}) == 2
    for cause in result['fix_list']['causes']:
        assert set(cause['action']['scope']['shift_ids']) <= {session['id']}
        assert '2 sessions' not in cause['sentence']
    summary = h.get('coordinator', f"/api/workspaces/{result['workspace']['id']}/fix-list")
    assert summary['summary']['places_open'] == 1
    assert {c['id'] for c in summary['causes']} == {c['id'] for c in result['fix_list']['causes']}


def test_amendment_notice_reaches_removed_person_with_own_before_after(h):
    pub, shifts = issued(h)
    sent = send(h, pub, change(h, pub, shifts[0]))
    approved = h.lifecycle(sent['workspace'], 'approve', 'approver')['workspace']
    new_pub = h.lifecycle(approved, 'publish')['publication_id']
    notice = notify(h, 'publication:' + new_pub)
    assert notice['title'].startswith('Your duties in ') and notice['title'].endswith(' changed')  # SW3-B-08: names the rota
    assert 'Was:' in notice['message'] and 'Now: No duty' in notice['message']
    assert len(notice['duty_changes']['removed']) == 1 and notice['duty_changes']['added'] == []
    assert not any(n['outbox_id'] == notice['outbox_id'] for n in h.get('staff_two', '/api/notifications')['notifications'])


@pytest.mark.parametrize('kind', ['exchange', 'give'])
def test_swap_picker_explains_ineligible_recipients_and_empty_audience(h, kind):
    identity(h, 'staff_three', ['staff'], h.seed['people'][2])
    pub, shifts = issued(h)
    with transaction(h.app.state.engine, write=True) as db:
        rows = list(db.scalars(select(m.Eligibility).where(m.Eligibility.person_id == h.seed['people'][1])))
        for row in rows:
            row.ends_on = m.now().date()
    options = h.get('staff', f"/api/swaps/options?assignment_id={shifts[0]['participants'][0]['id']}")
    two = next(r for r in options['recipients'] if r['id'] == h.seed['people'][1])
    three = next(r for r in options['recipients'] if r['id'] == h.seed['people'][2])
    assert not two['eligible'] and 'not eligible' in two['message']
    assert not three['eligible'] and 'no future duty' in three['message']
    if kind == 'give':
        with transaction(h.app.state.engine, write=True) as db:
            for row in db.scalars(select(m.Eligibility).where(m.Eligibility.person_id == h.seed['people'][2])):
                row.ends_on = m.now().date()
    refused = h.command('staff', 'swap.offer', {'publication_id': pub,
        'kind': kind,
        'assignment_id': shifts[0]['participants'][0]['id'], 'expires_at': (m.now() + timedelta(hours=1)).isoformat(),
        'reason': 'Seek a usable exchange'}, status=422)
    assert refused['error']['code'] == 'SWAP_NO_RECIPIENTS'
    assert refused['error']['details']['audience_count'] == 0
    assert 'Nobody can take this duty' in refused['error']['message']


def test_credential_impact_preview_is_read_only_and_notice_names_duties(h):
    pub, shifts = issued(h)
    with transaction(h.app.state.engine) as db:
        row = db.scalar(select(m.Eligibility).where(m.Eligibility.person_id == h.seed['people'][0],
            m.Eligibility.service_id == h.seed['services'][0]))
        target, revision, original_end = row.id, row.revision, row.ends_on
    payload = {'ends_on': '2026-10-04', 'reason': 'Preview the affected published duties'}
    response = h.client('credentialer').post(f'/api/eligibilities/{target}/published-impact', json=payload)
    assert response.status_code == 200, response.text
    preview = response.json()['published_impact']
    assert preview['count'] == 1 and preview['sessions'][0]['type'] and preview['sessions'][0]['person']
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Eligibility, target).ends_on == original_end
        assert not db.scalar(select(m.Outbox.id).where(m.Outbox.kind == 'credential_impact'))
    result = h.command('credentialer', 'eligibility.end', payload, target=target, revision=revision)
    assert result['published_impact'] == preview
    notice = notify(h, f'credential-impact:{target}:{result["record"]["revision"]}', 'coordinator')
    assert 'Oct' in notice['message'] and preview['sessions'][0]['person'] in notice['message']
    assert notice['published_impact'] == preview
    assert h.client('staff').post(f'/api/eligibilities/{target}/published-impact', json=payload).status_code == 403


def test_credential_impact_preview_includes_shared_drafts_without_disclosing_private_or_archived_rotas(h):
    shared = h.shift(h.workspace('Affected shared draft'))['workspace']
    private = h.command('coordinator', 'workspace.create', {'name': 'Private draft', 'starts_on': shared['starts_on'],
        'ends_on': shared['ends_on'], 'visibility': 'private', 'purpose': 'sandbox'})['workspace']
    h.shift(private)
    archived = h.shift(h.workspace('Archived draft'))['workspace']
    h.command('coordinator', 'workspace.archive', {'reason': 'Discard this alternative'},
              target=archived['id'], revision=archived['revision'])
    with transaction(h.app.state.engine) as db:
        credential = db.scalar(select(m.Eligibility).where(m.Eligibility.person_id == h.seed['people'][0],
            m.Eligibility.service_id == h.seed['services'][0]))
        target, original_end = credential.id, credential.ends_on
    response = h.client('credentialer').post(f'/api/eligibilities/{target}/published-impact',
        json={'ends_on': '2026-10-04', 'reason': 'Preview future draft duties'})
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview['published_impact'] == {'count': 0, 'sessions': []}
    assert preview['draft_impact']['count'] == 1
    session, = preview['draft_impact']['sessions']
    assert session['workspace_id'] == shared['id'] and session['rota_name'] == shared['name']
    assert session['status'] == 'draft' and session['assignment_id'] and session['person_id'] == h.seed['people'][0]
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Eligibility, target).ends_on == original_end
        assert not db.scalar(select(m.Outbox.id).where(m.Outbox.kind == 'credential_impact'))


def test_lineage_migration_preserves_all_existing_columns_and_rows(tmp_path):
    from alembic import command
    from sqlalchemy import MetaData, create_engine
    from scripts.manage import alembic_config
    from .migration_support import historical_seed
    from migrations import safety
    url = 'sqlite:///' + str(tmp_path / 'owned-lineage.db')
    cfg = alembic_config(url)
    command.upgrade(cfg, '0129_job_worker_owner')
    engine = create_engine(url)
    try:
        with transaction(engine, write=True) as db:
            historical_seed(db)
        metadata = MetaData()
        metadata.reflect(bind=engine)
        def snapshot():
            with engine.connect() as db:
                return {name: list(db.execute(table.select().order_by(*table.primary_key.columns)))
                        for name, table in metadata.tables.items() if name != 'alembic_version'}
        before = snapshot()
        command.upgrade(cfg, '0130_session_lineage')
        assert snapshot() == before
        migration = Path(__file__).resolve().parents[1] / 'migrations/versions/0130_session_lineage.py'
        assert safety.classification(migration) == 'additive' and not safety.declaration_errors(migration)
    finally:
        engine.dispose()


def test_legacy_type_checks_do_not_reappear_and_original_evidence_is_immutable(h):
    from .test_be10_scoped_checks import publish_imported_check
    w = h.shift(h.workspace())['workspace']
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[h.seed['people'][1]])['workspace']
    detail = h.detail(w)
    frozen_check = copy.deepcopy(detail['workspace']['validation'])
    legacy = {'code': 'TYPE_VERSION_CHANGED', 'severity': 'error', 'rule_id': None, 'person_id': None,
              'shift_id': detail['shifts'][1]['id'], 'message': 'Type changed; explicitly refresh the draft definition and review its impact'}
    frozen_check['findings'].append(legacy)
    frozen_check.update(valid=False, error_count=frozen_check['error_count'] + 1)
    pub = publish_imported_check(h, w, frozen_check)
    with transaction(h.app.state.engine) as db:
        original = copy.deepcopy(db.get(m.Publication, pub).snapshot)
    drift(h)
    result = change(h, pub, original['shifts'][0])
    sent = send(h, pub, result)
    for checked in (result, sent):
        assert not any(f['code'] == 'TYPE_VERSION_CHANGED' for f in checked['validation']['findings'])
    approved = h.lifecycle(sent['workspace'], 'approve', 'approver')['workspace']
    h.lifecycle(approved, 'publish')
    with transaction(h.app.state.engine) as db:
        evidence = db.get(m.Publication, pub)
        assert evidence.snapshot == original and evidence.validation == frozen_check


def test_session_only_findings_follow_selected_candidate_ids(h, monkeypatch):
    pub, shifts = issued(h)
    evaluate = planning.evaluate
    def warning(data, **kwargs):
        result = evaluate(data, **kwargs)
        sh = next(sh for sh in data['shifts'] if sh['id'] in data['focus_ids'])
        return {**result, 'warning_count': result['warning_count'] + 1, 'findings': [*result['findings'], {
            'code': 'CALENDAR_CLOSED', 'severity': 'warning', 'rule_id': None, 'person_id': None,
            'shift_id': sh['id'], 'message': 'Calendar warning for the selected occurrence'}]}
    monkeypatch.setattr(planning, 'evaluate', warning)
    result = change(h, pub, shifts[0])
    sent = send(h, pub, result)
    finding, = sent['changes'][0]['findings']
    assert finding['starts_on'] == '2026-10-04' and finding['ends_on'] == '2026-10-04'
    assert finding['level'] == 'could'
    assert 'submitted_session_ids' not in sent['changes'][0]
    assert finding['shift_id'] != result['change']['sessions'][0]['id']


def test_review_and_personal_delta_ignore_replaced_days(h):
    from datetime import date
    from types import SimpleNamespace
    from apps.api.services import schedule
    pub, _ = issued(h)
    with transaction(h.app.state.engine) as db:
        old = db.get(m.Publication, pub)
        # A historical retained snapshot may include duties outside the publication's in-force slice.
        previous = SimpleNamespace(id=old.id, snapshot=old.snapshot, department_id=old.department_id,
            starts_on=old.starts_on, ends_on=old.ends_on, effective_until=date(2026, 10, 5), sequence=old.sequence)
        sliced = schedule.in_force_snapshot(db, previous)
        fresh = copy.deepcopy(sliced)
        fresh['shifts'][0]['participants'][0]['person_id'] = h.seed['people'][1]
        successor = SimpleNamespace(id=m.uid(), snapshot=fresh, effective_until=None, sequence=old.sequence + 1)
        changes = published_changes.personal_changes(db, previous, successor)
        assert set(changes) == {h.seed['people'][0], h.seed['people'][1]}
        assert all(d['start'] < '2026-10-05' for c in changes.values() for side in c.values() for d in side)
    # Use the normal replacement workflow to exercise the public review contract on that slice.
    replacement = h.command('coordinator', 'workspace.create', {
        'name': 'Later replacement', 'starts_on': '2026-10-05', 'ends_on': '2026-10-11',
        'replaces_publication_id': pub})['workspace']
    replacement = h.shift(replacement, '2026-10-05T13:00:00+03:00', '2026-10-05T17:00:00+03:00', people=[h.seed['people'][1]])['workspace']
    h.publish(replacement)
    original = h.get('coordinator', '/api/publications/' + pub)['snapshot']
    result = change(h, pub, original['shifts'][0])
    assert len(result['change_diff']['sessions']) == 1


def test_personal_location_changes_and_long_delayed_notice_preserve_was_now(h):
    from types import SimpleNamespace
    from apps.worker.main import describe
    pub, _ = issued(h)
    with transaction(h.app.state.engine) as db:
        old = db.get(m.Publication, pub)
        newer = copy.deepcopy(old.snapshot)
        shift = newer['shifts'][0]
        place = m.uid()
        newer['locations'][place] = {'name': 'New clinic ' + 'x' * 500}
        shift['location_id'] = place
        later = SimpleNamespace(id=m.uid(), snapshot=newer, effective_until=None, current=False, sequence=old.sequence + 1, workspace_id=old.workspace_id)
        changes = published_changes.personal_changes(db, old, later)
        person = h.seed['people'][0]
        assert set(changes) == {person}
        assert changes[person]['removed'][0]['location_id'] != changes[person]['added'][0]['location_id']
        event = SimpleNamespace(kind='publication', payload={'person_changes': changes})
        title, message = describe(db, event, later, db.get(m.Identity, h.seed['identities']['staff']))
        assert title.startswith('Your duties in ') and title.endswith(' changed')
        assert 'Was:' in message and 'Now:' in message and 'Open Schedule' in message
        assert 'newer rota' in message and 'New clinic' in message
        assert len(message) <= m.Notification.__table__.c.message.type.length


def test_recipient_directory_never_names_a_private_return_duty(h):
    identity(h, 'staff_two', ['staff'], h.seed['people'][1])
    typ = h.configure('assignment_types', {'name': 'PRIVATE RETURN LABEL', 'starts_on': '2020-01-01',
        'definition': {}})['record']['id']
    w = h.shift(h.workspace())['workspace']
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00',
        people=[h.seed['people'][1]], typ=typ)['workspace']
    pub = h.publish(w)['publication_id']
    shifts = h.get('coordinator', '/api/publications/' + pub)['snapshot']['shifts']
    h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0],
        'start': shifts[1]['start'], 'end': shifts[1]['end'], 'kind': 'actual_absence',
        'private_reason': 'Private absence explanation'})
    options = h.get('staff', '/api/swaps/options?assignment_id=' + shifts[0]['participants'][0]['id'])
    recipient = next(r for r in options['recipients'] if r['id'] == h.seed['people'][1])
    # A staff reader reads the neutral finding (issue 1659); the code that names absence stays out of the answer.
    assert not recipient['eligible'] and 'COLLEAGUE_NOT_FREE' in {f['code'] for f in recipient['findings']}
    assert 'UNAVAILABLE' not in str(options)
    assert 'PRIVATE RETURN LABEL' not in str(options) and 'Private absence explanation' not in str(options)
    assert '05 Oct' not in recipient['message']


def test_an_amendment_tells_each_person_once_naming_every_absence(h):
    """SW3-B-08: two changed duties of one person read as two identical "Your absence was approved" (and "recorded")
    notices with no dates. One notice per person per amendment now names each absence with its year."""
    person = h.seed['people'][0]
    w = h.shift(h.workspace())['workspace']
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00')['workspace']
    pub = h.publish(w)['publication_id']
    shifts = h.get('coordinator', '/api/publications/' + pub)['snapshot']['shifts']
    for sh in shifts:
        result = change(h, pub, sh, absence_reason='Synthetic sick cover')
    sent = send(h, pub, result)
    approved = h.lifecycle(sent['workspace'], 'approve', 'approver')['workspace']
    with transaction(h.app.state.engine) as db:
        events = list(db.scalars(select(m.Outbox).where(m.Outbox.kind == 'absence_approved')))
    assert [e.payload['person_id'] for e in events] == [person] and len(events[0].payload['spans']) == 2
    notice = notify(h, events[0].event_key)
    assert notice['title'] == 'Your absence was approved'
    assert 'Sun 04 Oct 2026 08:00–12:00' in notice['message'] and 'Mon 05 Oct 2026 08:00–12:00' in notice['message']
    assert 'sick' not in notice['message'].lower()  # never the private reason
    h.lifecycle(approved, 'publish')
    recorded = notify(h, f"availability-published:{sent['workspace']['id']}:{person}")
    assert recorded['title'] == 'Your leave or absence was recorded' and 'Mon 05 Oct 2026' in recorded['message']
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.Outbox).where(m.Outbox.kind == 'availability')) == 1
