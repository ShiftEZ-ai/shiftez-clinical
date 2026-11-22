"""Issued evidence stays frozen while changes, staff reports and independent reviews remain usable."""
import copy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import published_changes, swaps
from apps.api.services.leave_disclosure import PLANNING_ROLES, leave_wording
from packages.contracts.schemas import ABSENCE_REPORTER_CATEGORIES, ROLE_NAMES
from packages.domain.validation import evaluate
from .governance_support import identity
from .test_timestamp_contract import assert_same_instant

pytestmark = pytest.mark.postgres


@pytest.fixture
def h(pg):
    return pg


def issued(h):
    identity(h, 'staff_two', ['staff'], h.seed['people'][1])
    w = h.shift(h.workspace())["workspace"]
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[h.seed['people'][1]])['workspace']
    pub = h.publish(w)['publication_id']
    shifts = h.get('coordinator', '/api/publications/' + pub)['snapshot']['shifts']
    return pub, shifts


def leave_display(h, mode):
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed['department_id'])
        department.leave_reason_display = mode
        department.revision += 1


def change(h, pub, sh, **values):
    pending = h.get('coordinator', f'/api/publications/{pub}/pending-changes')['workspace']
    return h.command('coordinator', 'publication.change', {
        'kind': 'replace', 'shift_id': sh['id'], 'assignment_id': sh['participants'][0]['id'],
        'person_id': h.seed['people'][2], 'reason': 'Arrange cover for a published duty', **values},
        target=pub, revision=pending['revision'] if pending else None)


def drift(h):
    with transaction(h.app.state.engine, write=True) as db:
        typ = db.get(m.AssignmentType, h.seed['type'])
        typ.definition = {**typ.definition, 'maximum_duration_minutes': 720}
        typ.revision += 1


def test_scoped_amendment_and_swap_accept_issued_type_and_retain_evidence(h):
    pub, shifts = issued(h)
    frozen = h.get('coordinator', '/api/publications/' + pub)['snapshot']
    drift(h)
    w = h.workspace(base=pub)
    check = h.detail(w)['workspace']['validation']
    assert not any(f['code'] == 'TYPE_VERSION_CHANGED' for f in check['findings'])
    with transaction(h.app.state.engine) as db:
        eligibility = swaps.Eligibility(db, db.get(m.Publication, pub))
        assert eligibility.pair(shifts[0]['participants'][0]['id'], shifts[1]['participants'][0]['id'])['eligible']
    result = change(h, pub, shifts[0])
    assert result['validation']['valid'] and result['validation']['scope'] == 'published_changes'
    from apps.api.services.planning import revalidate
    with transaction(h.app.state.engine) as db:
        workspace = db.get(m.Workspace, result['workspace']['id'])
        captured, own = [], []
        checked = revalidate(db, workspace, own, data_out=captured, store=False)
        assert checked['scope'] == 'published_changes'
        assert set(captured[0]['focus_ids']) == {row['id'] for row in own}
        assert len(own) == 2
        assert not {'person_rule_subjects', 'quota_subjects', '_published_session_checks'} & captured[0].keys()
    assert h.get('coordinator', '/api/publications/' + pub)['snapshot'] == frozen
    review = h.get('approver', '/api/workspaces/' + result['workspace']['id'])
    assert len(review['change_diff']['sessions']) == 1
    row = review['change_diff']['sessions'][0]
    assert row['before']['participants'][0]['person_id'] == h.seed['people'][0]
    assert row['after']['participants'][0]['person_id'] == h.seed['people'][2]


def test_direct_amendment_domain_check_still_blocks_affected_leave(h):
    pub, shifts = issued(h)
    result = change(h, pub, shifts[0])
    w = result['workspace']
    h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][2],
        'start': shifts[0]['start'], 'end': shifts[0]['end'], 'kind': 'actual_absence',
        'private_reason': 'Synthetic unavailability check'})
    data = h.snapshot(w)
    with transaction(h.app.state.engine) as db:
        published_changes.validation_scope(db, db.get(m.Workspace, w['id']), data)
    check = evaluate(data)
    assert not check['valid']
    assert any(f['code'] == 'UNAVAILABLE' for f in check['findings'])


def test_change_list_shared_hidden_review_publish_diff_and_undo(h):
    pub, shifts = issued(h)
    first = change(h, pub, shifts[0])
    second = change(h, pub, shifts[1])
    assert first['workspace']['id'] == second['workspace']['id']
    listed = h.get('coordinator', '/api/workspaces')
    assert second['workspace']['id'] not in {w['id'] for w in listed['workspaces']}
    undone = h.command('coordinator', 'publication.change.undo', {'change_id': second['change']['id'], 'reason': 'Keep the second duty as issued'},
                       target=pub, revision=second['workspace']['revision'])
    sent = h.command('coordinator', 'publication.changes.submit', {'reason': 'Review this published cover'}, target=pub, revision=undone['workspace']['revision'])
    submitted = next(c for c in sent['changes'] if c['status'] == 'submitted')
    assert submitted['urgency'] == 'routine' and submitted['submitted_at'] == sent['workspace']['submitted_at']
    pending = h.get('coordinator', f'/api/publications/{pub}/pending-changes')
    assert next(c for c in pending['changes'] if c['id'] == submitted['id'])['submitted_at'] == submitted['submitted_at']
    assert len(sent['change_diff']['sessions']) == 1
    approved = h.lifecycle(sent['workspace'], 'approve', 'approver')
    assert len(approved['change_diff']['sessions']) == 1
    assert h.get('coordinator', f'/api/publications/{pub}/pending-changes')['changes'][0]['status'] == 'approved'
    final = h.lifecycle(approved['workspace'], 'publish')
    issued_rows = h.get('coordinator', '/api/publications/' + final['publication_id'])['snapshot']['shifts']
    assert issued_rows[0]['participants'][0]['person_id'] == h.seed['people'][2]
    assert issued_rows[1]['participants'][0]['person_id'] == h.seed['people'][1]


def test_amendment_submit_refuses_stale_review_counters(h, monkeypatch):
    pub, shifts = issued(h)
    draft = change(h, pub, shifts[0])
    lifecycle = published_changes.planning.lifecycle

    def move_revision_after_check(db, actor, command):
        result = lifecycle(db, actor, command)
        # Inject drift in this transaction because the input row is already locked.
        db.get(m.Organisation, actor.organisation_id).availability_revision += 1
        db.flush()
        return result

    monkeypatch.setattr(published_changes.planning, 'lifecycle', move_revision_after_check)
    reply = h.command('coordinator', 'publication.changes.submit', {'reason': 'Review published cover'},
                      target=pub, revision=draft['workspace']['revision'], status=409)
    assert reply['error']['code'] == 'STALE_INPUTS'
    assert reply['error']['details']['recoverable']
    pending = h.get('coordinator', f'/api/publications/{pub}/pending-changes')
    assert [(row['id'], row['status']) for row in pending['changes']] == [(draft['change']['id'], 'draft')]


def test_urgent_not_offered_until_versioned_route_is_activated(h):
    pub, shifts = issued(h)
    result = change(h, pub, shifts[0])
    assert result['urgent_route'] == {'active': False, 'approver_count': 0}
    route = h.command('admin', 'review_route.create', {'name': 'Published changes review', 'starts_on': '2026-01-01',
        'definition': {'steps': [{'key': 'routine', 'label': 'Independent review'}],
                       'urgent_steps': [{'key': 'urgent', 'label': 'Urgent review'}]}, 'reason': 'Configure independent urgent decisions'})['route']
    h.command('approver', 'review_route.activate', {'reason': 'Independent route approval'}, target=route['id'], revision=route['revision'])
    pending = h.get('coordinator', f'/api/publications/{pub}/pending-changes')
    assert pending['urgent_route']['active'] and pending['urgent_route']['approver_count'] >= 1


def test_absence_report_approval_records_absence_replace_action_and_undo(h):
    pub, shifts = issued(h)
    payload = {'kind': 'absence_report', 'publication_id': pub, 'assignment_id': shifts[0]['participants'][0]['id'], 'reason': 'Cannot attend this duty'}
    row = h.command('staff', 'request.create', payload)['record']
    approved = h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Approve the absence and arrange cover'}, target=row['id'], revision=row['revision'])
    assert approved['replace_action']['shift_id'] == shifts[0]['id']
    with transaction(h.app.state.engine) as db:
        absence = db.scalar(select(m.Availability).where(m.Availability.request_id == row['id']))
        assert absence.person_id == h.seed['people'][0] and not absence.retracted
    h.command('approver', 'request.undo', {'reason': 'Undo this decision'}, target=row['id'], revision=approved['record']['revision'])
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Availability).where(m.Availability.request_id == row['id'])).retracted


def test_submitted_absence_is_visible_to_coordinator_and_publication_reads_are_cacheable(h, monkeypatch):
    pub, shifts = issued(h)
    assignment = shifts[0]['participants'][0]
    before_pulse = h.get('coordinator', '/api/pulse')['activity']
    report = h.command('staff', 'request.create', {
        'kind': 'absence_report', 'publication_id': pub, 'assignment_id': assignment['id'],
        'reason': 'Private reason must not be projected'})['record']

    assert h.get('coordinator', '/api/pulse')['activity'] > before_pulse
    from apps.api import main
    original_snapshot = main.in_force_snapshot
    monkeypatch.setattr(main, 'in_force_snapshot', lambda *args: {**original_snapshot(*args),
        'fairness_qualification': {'large_private_evidence': 'x' * 10000}})
    publication = h.get('coordinator', '/api/publications/' + pub)
    assert 'fairness_qualification' not in publication['snapshot']
    projected = next(shift for shift in publication['snapshot']['shifts'] if shift['id'] == shifts[0]['id'])
    assert projected['absence_reports'] == [{'request_id': report['id'], 'person_id': assignment['person_id'],
                                             'assignment_id': assignment['id'], 'status': 'submitted'}]
    assert 'Private reason' not in str(publication)
    schedule = h.get('coordinator', '/api/schedule/sessions?start=2026-10-01&end=2026-10-12')
    scheduled = next(shift for shift in schedule['shifts'] if shift['id'] == shifts[0]['id'])
    assert scheduled['absence_reports'] == projected['absence_reports']
    attention = next(item for item in h.get('coordinator', '/api/home')['attention'] if item['key'] == 'absence_reported')
    assert attention['count'] == 1 and attention['sample'][0]['person_id'] == assignment['person_id']
    assert attention['sample'][0]['replace_action']['assignment_id'] == assignment['id']

    client = h.client('coordinator')
    response = client.get('/api/publications/' + pub)
    assert response.headers['etag']
    cached = client.get('/api/publications/' + pub, headers={'if-none-match': response.headers['etag']})
    assert cached.status_code == 304 and cached.headers['etag'] == response.headers['etag']


def test_absence_document_setting_and_ownership(h):
    pub, shifts = issued(h)
    h.command('admin', 'absence_report.settings', {'document_required': True, 'reason': 'Require supporting evidence'}, revision=1)
    payload = {'kind': 'absence_report', 'publication_id': pub, 'assignment_id': shifts[0]['participants'][0]['id'], 'reason': 'Cannot attend this duty'}
    assert h.command('staff', 'request.create', payload, status=422)['error']['code'] == 'ABSENCE_DOCUMENT_REQUIRED'
    payload['document_reference'] = 'protected-document-reference'
    assert h.command('staff_two', 'request.create', payload, status=404)['error']['code'] == 'SWAP_ASSIGNMENT_NOT_FOUND'
    assert h.command('staff', 'request.create', payload)['record']['details']['document_reference'] == payload['document_reference']


@pytest.mark.parametrize('role', ROLE_NAMES)
def test_published_commands_permissions_for_authoritative_roles(h, role):
    identity(h, 'only_' + role, [role], h.seed['people'][0] if role == 'staff' else None)
    pub, shifts = issued(h)
    response = h.client('only_' + role).post('/api/commands', headers={'idempotency-key': m.uid()}, json={
        'kind': 'publication.change', 'target_id': pub, 'payload': {'kind': 'cancel', 'shift_id': shifts[0]['id'], 'reason': 'Explicit session cancellation'}})
    assert response.status_code == (200 if role in {'admin', 'coordinator'} else 403), response.text
    read = h.client('only_' + role).get(f'/api/publications/{pub}/pending-changes')
    assert read.status_code == (200 if role in {'admin', 'coordinator', 'approver', 'programme_lead', 'auditor'} else 403), read.text
    settings = h.command('only_' + role, 'absence_report.settings', {'document_required': False, 'reason': 'Role boundary for absence policy'}, revision=1, status=200 if role == 'admin' else 403)
    assert settings is not None


def test_mark_absent_removes_only_that_assignment_and_can_undo(h):
    w = h.shift(h.workspace(), people=h.seed['people'][:2])['workspace']
    pub = h.publish(w)['publication_id']
    sh = h.get('coordinator', '/api/publications/' + pub)['snapshot']['shifts'][0]
    result = change(h, pub, sh, kind='absence', person_id=None)
    current = h.detail(result['workspace'])['shifts'][0]
    assert not current['cancelled'] and len(current['participants']) == 1
    assert current['participants'][0]['person_id'] == sh['participants'][1]['person_id']
    undone = h.command('coordinator', 'publication.change.undo', {'change_id': result['change']['id'], 'reason': 'Undo the absence'}, target=pub, revision=result['workspace']['revision'])
    assert len(h.detail(undone['workspace'])['shifts'][0]['participants']) == 2


def test_known_empty_swap_audience_is_visible_and_notified(h):
    pub, shifts = issued(h)
    row = h.command('staff', 'swap.offer', {'kind': 'give', 'publication_id': pub,
        'assignment_id': shifts[0]['participants'][0]['id'], 'expires_at': (m.now()+timedelta(hours=1)).isoformat(),
        'reason': 'Please take this duty'})['swap']
    with transaction(h.app.state.engine, write=True) as db:
        event = db.scalar(select(m.Outbox).where(m.Outbox.event_key == f"swap:{row['id']}:1:offered"))
        swaps.store_audience(db, {'swap_id': row['id'], 'event_key': event.event_key}, {'person_ids': []})
        projected = swaps.projection(db, db.get(m.SwapRequest, row['id']))
        assert projected['audience_count'] == 0 and projected['no_eligible_recipients']
        assert db.scalar(select(m.Outbox).where(m.Outbox.event_key.like('%no_recipients'))) is not None


def configure_urgent(h):
    route = h.command('admin', 'review_route.create', {'name': 'Urgent independent review', 'starts_on': '2026-01-01',
        'definition': {'steps': [{'key': 'routine', 'label': 'Independent review'}],
                       'urgent_steps': [{'key': 'urgent', 'label': 'Urgent review'}]}, 'reason': 'Configure urgent route'})['route']
    return h.command('approver', 'review_route.activate', {'reason': 'Activate independent pathway'}, target=route['id'], revision=route['revision'])


def test_urgent_subset_keeps_unsent_change_for_successor_publication(h):
    configure_urgent(h)
    pub, shifts = issued(h)
    one, two = change(h, pub, shifts[0]), change(h, pub, shifts[1])
    sent = h.command('coordinator', 'publication.changes.submit', {'change_ids': [one['change']['id']], 'urgency': 'urgent',
        'reason': 'Urgent cover for the first duty'}, target=pub, revision=two['workspace']['revision'])
    assert len(sent['change_diff']['sessions']) == 1
    approved = h.lifecycle(sent['workspace'], 'approve', 'approver')['workspace']
    result = h.lifecycle(approved, 'publish')
    pending = h.get('coordinator', f"/api/publications/{result['publication_id']}/pending-changes")
    assert len(pending['changes']) == 1 and pending['changes'][0]['id'] == two['change']['id']
    assert pending['changes'][0]['status'] == 'draft'
    sent = h.command('coordinator', 'publication.changes.submit', {'reason': 'Send the remaining cover'},
        target=result['publication_id'], revision=pending['workspace']['revision'])
    assert len(sent['change_diff']['sessions']) == 1


def test_submitted_change_can_be_withdrawn_and_undone(h):
    pub, shifts = issued(h)
    result = change(h, pub, shifts[0])
    h.command('coordinator', 'publication.changes.submit', {'reason': 'Review the cover'}, target=pub, revision=result['workspace']['revision'])
    pending = h.get('coordinator', f'/api/publications/{pub}/pending-changes')
    withdrawn = h.command('coordinator', 'publication.changes.withdraw', {'reason': 'Return the changes for editing'}, target=pub, revision=pending['workspace']['revision'])
    assert withdrawn['changes'][0]['status'] == 'draft'
    undone = h.command('coordinator', 'publication.change.undo', {'change_id': result['change']['id'], 'reason': 'Restore original assignment'}, target=pub, revision=withdrawn['workspace']['revision'])
    assert undone['changes'][0]['status'] == 'withdrawn'


def test_unrelated_amendment_continues_open_offer_without_rewriting_consent(h):
    pub, shifts = issued(h)
    offer = h.command('staff', 'swap.offer', {'kind': 'give', 'publication_id': pub,
        'assignment_id': shifts[0]['participants'][0]['id'], 'expires_at': (m.now()+timedelta(hours=1)).isoformat(),
        'reason': 'Offer remains valid on an unchanged duty'})['swap']
    result = change(h, pub, shifts[1])
    sent = h.command('coordinator', 'publication.changes.submit', {'reason': 'Review unrelated replacement'}, target=pub, revision=result['workspace']['revision'])
    approved = h.lifecycle(sent['workspace'], 'approve', 'approver')['workspace']
    final = h.lifecycle(approved, 'publish')
    with transaction(h.app.state.engine) as db:
        old = db.get(m.SwapRequest, offer['id'])
        successor = db.scalar(select(m.SwapRequest).where(m.SwapRequest.reopened_from_id == old.id))
        assert old.publication_id == pub and old.first_assignment_id == offer['first_assignment_id']
        assert successor.publication_id == final['publication_id'] and swaps.effective_status(db, successor) == 'offered'
        assert db.scalar(select(m.Job).where(m.Job.kind == 'swap_audience', m.Job.input_snapshot['swap_id'].as_string() == successor.id))


def test_credential_end_returns_and_notifies_affected_published_duties(h):
    pub, shifts = issued(h)
    with transaction(h.app.state.engine) as db:
        row = db.scalar(select(m.Eligibility).where(m.Eligibility.department_id == h.seed['department_id'],
            m.Eligibility.person_id == h.seed['people'][0], m.Eligibility.service_id == h.seed['services'][0]))
        target, revision = row.id, row.revision
    result = h.command('credentialer', 'eligibility.end', {'ends_on': '2026-10-04', 'reason': 'Credential ends before this published duty'}, target=target, revision=revision)
    assert result['published_impact']['count'] == 1
    assert result['published_impact']['sessions'][0]['shift_id'] == shifts[0]['id']
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Outbox).where(m.Outbox.kind == 'credential_impact')) is not None


def test_closed_session_why_returns_closed_in_api_contract(h, monkeypatch):
    from apps.api.services import staffing
    w = h.shift(h.workspace())['workspace']
    source = h.snapshot(w)
    source['shifts'][0]['closed'] = True
    sid = source['shifts'][0]['id']
    monkeypatch.setattr(staffing, 'prepare_why', lambda *args: (source, {}))
    result = h.client('coordinator').post('/api/planner/why', json={'workspace_id': w['id'], 'expected_revision': w['revision'], 'shift_ids': [sid]})
    assert result.status_code == 200, result.text
    assert result.json()['sessions'][0]['closed']


def test_fill_options_counts_focus_instead_of_1054_context_duties(h, monkeypatch):
    from apps.api.services import staffing
    pub, shifts = issued(h)
    draft = h.workspace(base=pub)
    source = h.snapshot(draft)
    original = copy.deepcopy(source)
    peer = source['shifts'][0]
    context = [{**peer, 'id': m.uid(), 'workspace_id': m.uid(), 'participants': [],
                'start': '2026-09-01T08:00:00+03:00', 'end': '2026-09-01T12:00:00+03:00'} for _ in range(1054)]
    monkeypatch.setattr(staffing, 'domain_snapshot', lambda *args: {**copy.deepcopy(original), 'shifts': copy.deepcopy(original['shifts'] + context)})
    sid, aid = peer['id'], peer['participants'][0]['id']
    result = h.client('coordinator').post('/api/planner/fill-options', json={'workspace_id': draft['id'], 'expected_revision': draft['revision'], 'shift_id': sid, 'replace_assignment_id': aid})
    assert result.status_code == 200, result.text
    assert result.json()['candidates']
    assert all('override_allowed' in r for r in result.json()['candidates'])


def test_fill_options_offer_a_reasoned_leave_override_in_a_draft_but_not_in_a_change_list(h):
    """RUL-E04: a draft's assign takes a reason to place someone on leave (planning.write_shift), so fill-options allows
    it there; a publication's change list has no override (publication.change), so the same person is not breakable."""
    pub, shifts = issued(h)
    away, first = h.seed['people'][2], shifts[0]
    h.command('coordinator', 'availability.record', {'person_id': away, 'start': first['start'], 'end': first['end'],
              'kind': 'actual_absence', 'private_reason': 'Synthetic unavailability check'})

    def allowed(w, sid, replace=None):
        result = h.client('coordinator').post('/api/planner/fill-options', json={
            'workspace_id': w['id'], 'expected_revision': w['revision'], 'shift_id': sid,
            **({'replace_assignment_id': replace} if replace else {})})
        assert result.status_code == 200, result.text
        [row] = [c for c in result.json()['candidates'] if c['person_id'] == away]
        assert row['status'] == 'blocked' and any(f['code'] == 'UNAVAILABLE' for f in row['findings']), row
        return row['override_allowed']

    draft = h.shift(h.workspace(), first['start'], first['end'], people=[])['workspace']
    open_shift = next(x for x in h.detail(draft)['shifts'] if not x['participants'])
    assert allowed(draft, open_shift['id']) is True
    prepared = h.command('coordinator', 'publication.changes.prepare', {'reason': 'Prepare a change to the published rota'}, target=pub)
    assert h.detail(prepared['workspace'])['workspace']['baseline']['snapshot_hash']
    copy_ = next(x for x in prepared['shifts'] if x['start'] == first['start'] and x['participants'])
    assert allowed(prepared['workspace'], copy_['id'], copy_['participants'][0]['id']) is False


def test_change_list_rejects_leave_override_but_draft_assign_accepts_reason(h):
    pub, shifts = issued(h)
    away, first = h.seed['people'][2], shifts[0]
    h.command('coordinator', 'availability.record', {'person_id': away, 'start': first['start'], 'end': first['end'],
              'kind': 'actual_absence', 'private_reason': 'Synthetic unavailability check'})
    prepared = h.command('coordinator', 'publication.changes.prepare', {'reason': 'Prepare a change to the published rota'}, target=pub)
    refused = h.command('coordinator', 'publication.change', {
        'kind': 'replace', 'shift_id': first['id'], 'assignment_id': first['participants'][0]['id'],
        'person_id': away, 'reason': 'Arrange cover for a published duty'}, target=pub,
        revision=prepared['workspace']['revision'], status=409)
    assert refused['error']['code'] == 'PERSON_UNAVAILABLE'

    draft = h.shift(h.workspace(), first['start'], first['end'], people=[])['workspace']
    shift = next(row for row in h.detail(draft)['shifts'] if not row['participants'])
    saved = h.command('coordinator', 'shift.update', {
        'workspace_id': draft['id'], 'shift_id': shift['id'], 'assignment_type_id': shift['assignment_type_id'],
        'location_id': shift['location_id'], 'service_ids': shift['service_ids'], 'start': shift['start'],
        'end': shift['end'], 'attributes': shift.get('attributes', {}),
        'participants': [{'person_id': away, 'start': shift['start'], 'end': shift['end']}],
        'override_reason': 'Reviewed leave exception for cover'}, revision=draft['revision'])
    assert saved['shift_id'] == shift['id']


def test_away_conflict_never_offers_whole_session_cancellation(h):
    from apps.api.services.series_conflicts import cancel_actions
    from apps.api.auth import authenticate
    published = h.publish(h.shift(h.workspace())['workspace'])['publication_id']
    w = h.workspace(base=published)
    sh = h.detail(w)['shifts'][0]
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, h.client('coordinator').cookies['rota_session'])
        conflict = {'code': 'UNAVAILABLE', 'shift_id': sh['id'], 'person_id': h.seed['people'][0]}
        assert cancel_actions(db, actor, [conflict])[0]['cancel_actions'] == []


def test_absent_usual_session_stays_open_can_explain_and_replace(h):
    from .test_series_overlap_api import candidate, reviewed, absence
    from .test_series_reconciliation import materialise
    value = candidate(h, 'Published clinic with a cover requirement')
    value['definition']['close_when_all_usual_unavailable'] = True
    w = materialise(h, reviewed(h, value))
    pub = h.publish(w)['publication_id']
    sh = h.get('coordinator', '/api/publications/' + pub)['snapshot']['shifts'][0]
    absence(h, day='2026-10-05')
    draft = h.workspace(base=pub)
    detail = h.detail(draft)
    open_shift = detail['shifts'][0]
    why = h.client('coordinator').post('/api/planner/why', json={'workspace_id': draft['id'], 'expected_revision': draft['revision'], 'shift_ids': [open_shift['id']]})
    assert why.status_code == 200, why.text
    assert not why.json()['sessions'][0].get('closed', False)
    options = h.client('coordinator').post('/api/planner/fill-options', json={'workspace_id': draft['id'], 'expected_revision': draft['revision'], 'shift_id': open_shift['id']})
    assert options.status_code == 200, options.text
    assert options.json()['candidates']
    result = change(h, pub, sh)
    current = h.detail(result['workspace'])['shifts'][0]
    assert not current.get('closed') and current['participants'][0]['person_id'] == h.seed['people'][2]
    assert result['validation']['valid']


def test_scoped_check_retains_untouched_issued_coverage_without_rechecking(h):
    pub, shifts = issued(h)
    drift(h)
    result = change(h, pub, shifts[0])
    validation = h.detail(result['workspace'])['workspace']['validation']
    assert len({r['shift_id'] for r in validation['coverage']}) == 2
    assert validation['retained_checks']['session_count'] == 1
    assert validation['retained_checks']['publication_id'] == pub
    assert len(result['change']['sessions']) == 1


def test_an_absence_that_leaves_a_gap_cannot_be_sent(h):
    pub, shifts = issued(h)
    result = change(h, pub, shifts[0], kind='absence', person_id=None)
    assert not result['validation']['valid']
    assert result['changes'][0]['must_fix']
    error = h.command('coordinator', 'publication.changes.submit', {'reason': 'Attempt to send an uncovered absence'}, target=pub, revision=result['workspace']['revision'], status=422)
    assert error['error']['code'] == 'VALIDATION_FAILED'


def test_all_change_authors_remain_excluded_from_independent_review(h):
    identity(h, 'first_editor', ['coordinator', 'approver'])
    pub, shifts = issued(h)
    result = h.command('first_editor', 'publication.change', {'kind': 'replace', 'shift_id': shifts[0]['id'],
        'assignment_id': shifts[0]['participants'][0]['id'], 'person_id': h.seed['people'][2], 'reason': 'First editor arranges cover'}, target=pub)
    sent = h.command('coordinator', 'publication.changes.submit', {'reason': 'Another coordinator submits the change'}, target=pub, revision=result['workspace']['revision'])
    assert h.lifecycle(sent['workspace'], 'approve', 'first_editor', status=403)['error']['code'] == 'SELF_APPROVAL'


def test_absence_document_setting_retains_versions_and_supports_undo(h):
    result = h.command('admin', 'absence_report.settings', {'document_required': True, 'reason': 'Require evidence for reports'}, revision=1)
    undo = result['undo']
    restored = h.command('admin', undo['kind'], undo['payload'], revision=undo['expected_revision'])
    assert restored['document_required'] is False
    with transaction(h.app.state.engine) as db:
        history = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == 'absence_report_settings')))
        assert len(history) == 3 and {r.object_revision for r in history} == {1, 2, 3}


def test_staff_can_report_a_locked_duty(h):
    w = h.shift(h.workspace())['workspace']
    sh = h.detail(w)['shifts'][0]
    w = h.command('coordinator', 'shift.action', {'workspace_id': w['id'], 'shift_ids': [sh['id']], 'action': 'lock', 'reason': 'Lock the planned assignment'}, revision=w['revision'])['workspace']
    pub = h.publish(w)['publication_id']
    result = h.command('staff', 'request.create', {'kind': 'absence_report', 'publication_id': pub, 'assignment_id': sh['participants'][0]['id'], 'reason': 'Cannot attend this locked duty'})
    assert result['record']['kind'] == 'absence_report'


@pytest.mark.parametrize('kind', ['add', 'move', 'swap', 'cancel'])
def test_remaining_published_actions_retain_issued_evidence_and_undo(h, kind):
    pub, shifts = issued(h)
    before = h.get('coordinator', '/api/publications/' + pub)['snapshot']
    one, two = shifts
    payload = {'kind': kind, 'shift_id': one['id'], 'reason': 'Review a published session action'}
    if kind == 'add':
        payload['person_id'] = h.seed['people'][2]
    elif kind in {'move', 'swap'}:
        payload.update(assignment_id=one['participants'][0]['id'], target_shift_id=two['id'])
        if kind == 'swap':
            payload['target_assignment_id'] = two['participants'][0]['id']
    result = h.command('coordinator', 'publication.change', payload, target=pub)
    rows = h.detail(result['workspace'])['shifts']
    assert len(result['change']['sessions']) == (2 if kind in {'move', 'swap'} else 1)
    if kind == 'cancel':
        assert rows[0]['cancelled'] and not rows[1]['cancelled']
    elif kind == 'add':
        assert {p['person_id'] for p in rows[0]['participants']} == {h.seed['people'][0], h.seed['people'][2]}
    elif kind == 'move':
        assert rows[0]['participants'] == [] and len(rows[1]['participants']) == 2
    else:
        assert rows[0]['participants'][0]['person_id'] == h.seed['people'][1]
        assert rows[1]['participants'][0]['person_id'] == h.seed['people'][0]
    assert h.get('coordinator', '/api/publications/' + pub)['snapshot'] == before
    undone = h.command('coordinator', 'publication.change.undo', {'change_id': result['change']['id'],
        'reason': 'Restore issued duties'}, target=pub, revision=result['workspace']['revision'])
    assert h.detail(undone['workspace'])['workspace']['validation']['valid']
    assert not h.get('approver', '/api/workspaces/' + undone['workspace']['id'])['change_diff']['sessions']


def test_scoped_checks_retain_unaffected_people_quota_evidence(h):
    from .test_block_quotas import quota
    quota(h, scope={'person_id': h.seed['people'][1]}, comparison='target', amount=2, severity='advisory')
    pub, shifts = issued(h)
    old = h.get('coordinator', '/api/publications/' + pub)['original_validation']
    result = change(h, pub, shifts[0])
    current = result['validation']
    pid = h.seed['people'][1]
    assert [r for r in current['quota_report']['rows'] if r['person_id'] == pid] == [r for r in old['quota_report']['rows'] if r['person_id'] == pid]
    assert [f for f in current['findings'] if f['person_id'] == pid] == [f for f in old['findings'] if f['person_id'] == pid]


def test_failed_swap_audience_does_not_send_a_false_empty_audience_notice(h):
    pub, shifts = issued(h)
    row = h.command('staff', 'swap.offer', {'kind': 'give', 'publication_id': pub,
        'assignment_id': shifts[0]['participants'][0]['id'], 'expires_at': (m.now()+timedelta(hours=1)).isoformat(),
        'reason': 'Find eligible cover'})['swap']
    with transaction(h.app.state.engine, write=True) as db:
        event = db.scalar(select(m.Outbox).where(m.Outbox.event_key == f"swap:{row['id']}:1:offered"))
        swaps.store_audience(db, {'swap_id': row['id'], 'event_key': event.event_key}, {}, error='Unresolved audience')
        projected = swaps.projection(db, db.get(m.SwapRequest, row['id']))
        assert projected['audience_failed'] and not projected['no_eligible_recipients']
        assert db.scalar(select(m.Outbox).where(m.Outbox.event_key.like('%no_recipients'))) is None


def test_hidden_change_workspace_cannot_bypass_change_authorship_or_submission(h):
    pub, shifts = issued(h)
    result = change(h, pub, shifts[0])
    w = result['workspace']
    sh = h.detail(w)['shifts'][0]
    error = h.command('coordinator', 'shift.action', {'workspace_id': w['id'], 'shift_ids': [sh['id']],
        'action': 'cancel', 'reason': 'Try a generic hidden draft mutation'}, revision=w['revision'], status=409)
    assert error['error']['code'] == 'PUBLICATION_CHANGE_LIST'
    error = h.lifecycle(w, 'submit', status=409)
    assert error['error']['code'] == 'PUBLICATION_CHANGE_LIST'
    sent = h.command('coordinator', 'publication.changes.submit', {'reason': 'Use the governed list submission'}, target=pub, revision=w['revision'])
    assert sent['workspace']['status'] == 'submitted'


def test_narrowing_legacy_rule_checks_every_context_without_changing_publication(h):
    from apps.api.serialization import record
    from apps.worker.main import process_one
    from tests.test_contextual_feasibility import use_checked_activation
    from tests.test_rule_templates import propose

    rule = {'kind': 'minimum_cover', 'minimum': 1}
    source = h.policy(rule, scope={'weekdays': [0, 1, 2, 3, 6]}, severity='advisory')
    pub, shifts = issued(h)
    pending = change(h, pub, shifts[0])['workspace']
    draft = h.shift(h.workspace(start='2026-10-11', end='2026-10-18'),
                    start='2026-10-15T08:00:00+03:00', end='2026-10-15T12:00:00+03:00')['workspace']
    proposal = propose(h, rule, scope={'weekdays': [0, 1, 2, 6]}, severity='advisory', replaces_id=source['id'])
    before_shifts = {w['id']: h.detail(w)['shifts'] for w in (draft, pending)}
    with transaction(h.app.state.engine) as db:
        frozen = record(db.get(m.Publication, pub))
        frozen_changes = record(published_changes.change_list(db, pub))
    use_checked_activation(h)
    # Existing clients refer to the legacy-engine app; use the production checked-activation path.
    for client in h.clients.values():
        client.close()
    h.clients.clear()
    # Fixed published assignments can leave candidate comparison unresolved; acknowledgement never replaces receipts.
    payload = {'reason': 'Narrow the legacy weekday requirement; accept unresolved fixed-duty candidate comparisons',
               'check_job_ids': [], 'acknowledge_unresolved': True}
    for workspace in (draft, pending):
        denied = h.command('approver', 'policy.activate', payload, proposal['id'], proposal['revision'], status=409)
        assert denied['error']['code'] == 'RULE_CHECK_REQUIRED'
        job = h.command('approver', 'job.create', {'workspace_id': workspace['id'], 'kind': 'feasibility',
            'rule_changes': [{'policy_id': proposal['id'], 'expected_revision': proposal['revision']}]},
            revision=workspace['revision'])['job']
        assert process_one(h.app.state.engine)
        checked = h.get('approver', '/api/jobs/' + job['id'])['job']
        assert checked['status'] == 'succeeded'
        assert checked['result']['full_feasibility_established']
        assert not checked['result']['newly_introduced_contradiction']
        assert checked['result']['new_or_worse_findings'] == []
        payload['check_job_ids'].append(job['id'])
    denied = h.command('coordinator', 'job.create', {'workspace_id': pending['id'], 'kind': 'generation'},
                       revision=pending['revision'], status=409)
    assert denied['error']['code'] == 'PUBLICATION_CHANGE_LIST'
    activated = h.command('approver', 'policy.activate', payload, proposal['id'], proposal['revision'])
    assert activated['record']['status'] == 'active'
    assert {r['workspace_id'] for r in activated['context_check_evidence']} == {draft['id'], pending['id']}
    assert activated['unresolved_acknowledged']
    assert {w['id']: h.detail(w)['shifts'] for w in (draft, pending)} == before_shifts
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, source['id']).status == 'retired'
        assert record(db.get(m.Publication, pub)) == frozen
        assert record(published_changes.change_list(db, pub)) == frozen_changes


def test_remaining_absence_rebinds_ids_checks_and_followup_cover_after_urgent_publish(h):
    configure_urgent(h)
    pub, shifts = issued(h)
    first = change(h, pub, shifts[0])
    second = change(h, pub, shifts[1], kind='absence', person_id=None)
    sent = h.command('coordinator', 'publication.changes.submit', {'change_ids': [first['change']['id']],
        'urgency': 'urgent', 'reason': 'Publish urgent cover separately'}, target=pub, revision=second['workspace']['revision'])
    approved = h.lifecycle(sent['workspace'], 'approve', 'approver')['workspace']
    new_pub = h.lifecycle(approved, 'publish')['publication_id']
    pending = h.get('coordinator', f'/api/publications/{new_pub}/pending-changes')
    remaining = pending['changes'][0]
    assert remaining['must_fix']
    source = h.get('coordinator', '/api/publications/' + new_pub)['snapshot']['shifts'][1]
    assert remaining['shift_id'] == source['id']
    assert remaining['assignment_id'] == source['participants'][0]['id']
    covered = h.command('coordinator', 'publication.change', {'kind': 'add', 'shift_id': remaining['shift_id'],
        'person_id': h.seed['people'][2], 'reason': 'Arrange remaining cover'}, target=new_pub, revision=pending['workspace']['revision'])
    assert covered['validation']['valid']
    assert h.command('coordinator', 'publication.changes.submit', {'reason': 'Review the remaining cover'}, target=new_pub,
        revision=covered['workspace']['revision'])['workspace']['status'] == 'submitted'


def test_absence_report_reason_is_never_a_public_leave_label(h):
    pub, shifts = issued(h)
    reason = 'Confidential absence explanation'
    asked = h.command('staff', 'request.create', {'kind': 'absence_report', 'publication_id': pub,
        'assignment_id': shifts[0]['participants'][0]['id'], 'reason': reason})['record']
    h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Independent absence decision'},
        target=asked['id'], revision=asked['revision'])
    with transaction(h.app.state.engine) as db:
        absence = db.scalar(select(m.Availability).where(m.Availability.request_id == asked['id']))
        assert absence.private_reason == '' and absence.leave_type == ''


@pytest.mark.parametrize('mode', ['none', 'type', 'details'])
def test_absence_report_serializer_uses_leave_display_for_reporter_decider_and_third_person(h, mode):
    pub, shifts = issued(h)
    leave_display(h, mode)
    note = 'Private scheduling note'
    request = h.command('staff', 'request.create', {'kind': 'absence_report', 'publication_id': pub,
        'assignment_id': shifts[0]['participants'][0]['id'], 'reason': 'Private health explanation',
        'category': 'sick', 'note': note, 'document_reference': 'Private document ref'})['record']
    assert request['details']['absence_reason'] == 'sick' and request['details']['note'] == note
    # The decider reads the report as leave_reason_display allows (leave_disclosure.leave_wording): nothing at
    # "none", the category at "type", category + note + document reference at "details".
    expected = {} if mode == 'none' else {'absence_reason': 'sick'} | ({'note': note} if mode == 'details' else {})
    item = h.get('approver', '/api/requests/all?id=' + request['id'])['item']
    base = {key: item['details'][key] for key in ('replace_action', 'availability_kind', 'leave_type', 'document_policy')
            if key in item['details']}
    if mode == 'details':
        base['document_reference'] = 'Private document ref'
    assert item['details'] == {**base, **expected}
    # A third person outside the team (auditor) cannot open someone else's request at all, as on dev.
    h.get('auditor', '/api/requests/all?id=' + request['id'], status=404)
    approved = h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Approved'},
        request['id'], request['revision'])['record']
    assert approved['details'].get('absence_reason') == expected.get('absence_reason')
    assert ('note' in approved['details']) is (mode == 'details')
    assert 'Private health explanation' not in str(item) and 'Private health explanation' not in str(approved)


def test_absence_report_serializer_uses_planning_roles_and_legacy_other_fallback():
    row = SimpleNamespace(kind='absence_report', person_id='owner', private_reason='Legacy private reason',
                          details={'note': 'Legacy note'})
    department = SimpleNamespace(leave_reason_display='details')
    operator = SimpleNamespace(person_id='other', permits=lambda *roles: 'operator' in roles)
    assert set(PLANNING_ROLES) == {'admin', 'coordinator', 'approver', 'programme_lead', 'auditor'}
    assert leave_wording(department, operator, row) == {}
    coordinator = SimpleNamespace(person_id='other', permits=lambda *roles: 'coordinator' in roles)
    assert leave_wording(department, coordinator, row) == {'absence_reason': 'Legacy private reason', 'note': 'Legacy note'}


@pytest.mark.parametrize(('mode', 'reason', 'expected'), [
    ('none', 'Legacy private reason', None), ('type', 'Legacy private reason', 'other'),
    ('details', 'Legacy private reason', 'Legacy private reason'), ('type', 'Sick', 'sick'), ('type', 'Leave', 'leave')])
def test_published_change_reason_uses_the_same_disclosure(mode, reason, expected, h):
    pub, shifts = issued(h)
    leave_display(h, mode)
    result = change(h, pub, shifts[0], absence_reason=reason)
    shown = result['change']
    assert shown.get('absence_reason') == expected
    assert 'reason' not in shown
    if mode == 'none':
        assert reason not in str(shown)


@pytest.mark.parametrize('category', ['sick', None])
def test_absence_request_bulk_and_audit_keep_reason_note_private_and_category_optional(h, category):
    pub, shifts = issued(h)
    identity(h, 'audit_admin', ['admin', 'auditor'])
    payload = {'kind': 'absence_report', 'publication_id': pub,
        'assignment_id': shifts[0]['participants'][0]['id'], 'reason': 'Private health explanation',
        'note': 'Private note', **({'category': category} if category is not None else {})}
    reply = h.command('staff', 'request.create', payload)
    asked = reply['record']
    rows = h.client('coordinator').post('/api/bulk/rows', json={'selection': {
        'collection': 'requests', 'include_ids': [asked['id']]}, 'limit': 10}).json()['rows']
    assert len(rows) == 1 and 'details' not in rows[0] and 'private_reason' not in rows[0]
    for role in ('auditor', 'audit_admin'):
        event = next(event for event in h.get(role, '/api/audit?people=false')['events']
                     if event['action'] == 'request.create' and event['detail'].get('receipt_id') == reply['receipt_id'])
        assert event['reason'] == ''
        assert event['detail'].get('absence_category') == category
        assert 'Private health explanation' not in str(event) and 'Private note' not in str(event)


def test_absence_attention_is_generic_coordinator_only_and_withdrawn_with_the_report(h):
    """The live absence_attention (6c085cf97) is generic and remains coordinator-only."""
    pub, shifts = issued(h)
    asked = h.command('staff', 'request.create', {'kind': 'absence_report', 'publication_id': pub,
        'assignment_id': shifts[0]['participants'][0]['id'], 'reason': 'Private health explanation',
        'category': 'emergency', 'note': 'Private note'})['record']
    attention = next(row for row in h.get('coordinator', '/api/home')['attention'] if row['key'] == 'absence_reported')
    sample = attention['sample'][0]
    assert sample['request_id'] == asked['id']
    assert set(sample) == {'request_id', 'person_id', 'person', 'start', 'end', 'status', 'replace_action'}
    assert not {'private_reason', 'absence_category', 'details'} & set(sample)
    for role in ('staff', 'staff_two', 'approver', 'auditor'):
        assert 'absence_reported' not in {row['key'] for row in h.get(role, '/api/home')['attention']}
    h.command('staff', 'request.decide', {'decision': 'withdraw', 'reason': 'Reported by mistake'},
        target=asked['id'], revision=asked['revision'])
    assert 'absence_reported' not in {row['key'] for row in h.get('coordinator', '/api/home')['attention']}


@pytest.mark.parametrize('category', ABSENCE_REPORTER_CATEGORIES)
def test_absence_category_is_separate_and_only_in_the_reporters_own_summary(h, category):
    pub, shifts = issued(h)
    assignment = shifts[0]['participants'][0]['id']
    reason = 'Confidential explanation, not a public category'
    asked = h.command('staff', 'request.create', {'kind': 'absence_report', 'publication_id': pub,
        'assignment_id': assignment, 'reason': reason, 'category': category, 'note': 'Own note'})['record']
    assert 'reporter_category' not in asked['details'] and 'private_reason' not in asked
    own = next(row for row in h.get('staff', '/api/home')['my_requests'] if row['id'] == asked['id'])
    assert own['assignment_id'] == assignment and own['absence_category'] == category
    own_request = h.get('staff', '/api/requests/all?id=' + asked['id'])['item']
    assert own_request['details']['absence_reason'] == category and own_request['details']['note'] == 'Own note'
    assert_same_instant(own['created_at'], asked['created_at'])
    assert not {'private_reason', 'details', 'publication_id', 'workspace_id', 'decision_reason'} & own.keys()
    for role in ('coordinator', 'approver'):
        other = h.get(role, '/api/requests/all?id=' + asked['id'])['item']
        assert 'absence_category' not in other and 'reporter_category' not in other['details']
        assert asked['id'] not in {row['id'] for row in h.get(role, '/api/home')['my_requests']}
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.RequestRecord, asked['id'])
        assert stored.private_reason == reason and stored.details['reporter_category'] == category


def test_legacy_absence_reason_is_not_inferred_as_a_category(h):
    pub, shifts = issued(h)
    asked = h.command('staff', 'request.create', {'kind': 'absence_report', 'publication_id': pub,
        'assignment_id': shifts[0]['participants'][0]['id'], 'reason': 'sick'})['record']
    own = next(row for row in h.get('staff', '/api/home')['my_requests'] if row['id'] == asked['id'])
    assert 'absence_category' not in own and 'reporter_category' not in asked['details']
    own_request = h.get('staff', '/api/requests/all?id=' + asked['id'])['item']
    assert own_request['details']['absence_reason'] == 'sick'
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.RequestRecord, asked['id'])
        assert stored.private_reason == 'sick' and 'reporter_category' not in stored.details
        assert 'other' not in str(stored.details)


@pytest.mark.parametrize('category', ['Confidential free text', 'Sick', '', 1])
def test_absence_category_rejects_values_outside_the_explicit_contract(category):
    from pydantic import ValidationError
    from packages.contracts.schemas import AbsenceReportInput
    with pytest.raises(ValidationError):
        AbsenceReportInput(publication_id=m.uid(), assignment_id=m.uid(), reason='Confidential explanation', category=category)
