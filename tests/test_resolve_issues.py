"""Schedule › Resolve issues: one command per batch (replace with the best available, unassign, remove sessions,
accept advisory issues, exceptions), each one change set with one Undo, refused when the rota moved on."""
from .clock_support import freeze_clock
import gc
import time
from datetime import timedelta
import pytest
from sqlalchemy import select
from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.issues import issue_key, best_replacements
from apps.api.services.staffing import best_order_key
from packages.domain.time import aware
from tests.test_scheduler_performance import _budget

PREVIEW = '/api/planner/issues/replace-preview'


def leave_rota(h, days=('04', '05'), *, seed_restricted=False):
    """Alex on three morning sessions (4, 5 and 6 Oct) and on leave 4–5 Oct: two leave issues."""
    alex = h.seed['people'][0]
    w = h.workspace('Resolve issues')
    for day in ('04', '05', '06'):
        w = h.shift(w, f'2026-10-{day}T08:00:00+03:00', f'2026-10-{day}T12:00:00+03:00',
                    people=[alex], seed_restricted=seed_restricted)['workspace']
    # Recorded after the draft exists: leave binds an existing draft live (no baseline refresh).
    h.command('coordinator', 'availability.record', {'person_id': alex, 'kind': 'recorded_leave', 'leave_type': 'Annual leave',
        'start': f'2026-10-{days[0]}T00:00:00+03:00', 'end': f'2026-10-{int(days[-1]) + 1:02d}T00:00:00+03:00', 'private_reason': 'Conference'})
    w = h.lifecycle(h.detail(w)['workspace'], 'validate')['workspace']
    return w


def issues(h, w, code=None):
    found = h.detail(w)['workspace']['validation']['findings']
    return [f for f in found if code is None or f['code'] == code]


def preview(h, w, keys, role='coordinator', status=200):
    response = h.client(role).post(PREVIEW, json={'workspace_id': w['id'], 'expected_revision': w['revision'], 'keys': keys})
    assert response.status_code == status, response.text
    return response.json()


def resolve(h, w, action, keys, plan_hash=None, status=200, role='coordinator'):
    return h.command(role, 'issues.resolve', {'workspace_id': w['id'], 'action': action, 'keys': keys, 'reason': 'Resolve issues',
                                              **({'plan_hash': plan_hash} if plan_hash else {})}, revision=w['revision'], status=status)


def test_replace_previews_the_best_available_then_applies_it_in_one_undoable_change(h, monkeypatch):
    alex, jordan, taylor = h.seed['people']
    w = leave_rota(h)
    leave = issues(h, w, 'UNAVAILABLE')
    keys = [issue_key(f) for f in leave]
    assert len(keys) == 2 and all(f['leave']['type'] == 'Annual' for f in leave)
    first = preview(h, w, keys)
    from apps.api.services import issues as service
    service.PLANS.clear()
    assert first == preview(h, w, keys), 'recomputing the same rota gives the same plan and hash'
    # Session order; ties use the session salt, and the first pick counts for the second (fewer hours).
    assert [r['replacement_id'] for r in first['plan']] == sorted(
        (jordan, taylor), key=lambda pid: best_order_key(0, 0, pid, first['plan'][0]['shift_id']))
    assert first['replaceable'] == 2 and first['open'] == 0 and all(r['status'] == 'ok' for r in first['plan'])
    # The first pick is Pick someone's first choice for that place (one ranking).
    head = first['plan'][0]
    fill = h.client('coordinator').post('/api/planner/fill-options', json={'workspace_id': w['id'], 'shift_id': head['shift_id'],
        'expected_revision': w['revision'], 'replace_assignment_id': head['assignment_id']}).json()
    assert fill['candidates'][0]['person_id'] == head['replacement_id']

    before = h.detail(w)['shifts']
    replaced_at = m.now() + timedelta(minutes=1)
    freeze_clock(monkeypatch, lambda: replaced_at)
    service.PLANS.clear()
    done = resolve(h, w, 'replace', keys, first['plan_hash'])
    assert set(keys) <= set(done['issues']['removed']) and not done['issues']['added']  # the leave and its cover gaps
    assert {p['person_id'] for s in done['shifts'] for p in s['participants']} == {jordan, taylor}
    assert len(done['shifts']) == 2 and done['removed_shift_ids'] == []
    for shift in done['shifts']:
        for participant in shift['participants']:
            assert participant['source'] == 'manual' and participant['source_run_id'] is None
            assert participant['source_actor_id'] == h.seed['identities']['coordinator']
            assert aware(participant['source_at']) == replaced_at
    w = done['workspace']
    assert not issues(h, w, 'UNAVAILABLE')

    undone = h.command('coordinator', 'workspace.undo', {'reason': 'Undo replace'}, w['id'], w['revision'])
    assert len(issues(h, undone['workspace'], 'UNAVAILABLE')) == 2, 'one Undo brings every replaced person back'
    # The reply says what changed, as the batch did (the page patches it in place): the two sessions, the issues back.
    assert sorted(s['id'] for s in undone['shifts']) == sorted(s['id'] for s in done['shifts']) and undone['removed_shift_ids'] == []
    assert set(keys) <= {issue_key(f) for f in undone['issues']['added']} and undone['health']['checked']
    assert all(p['person_id'] == alex for s in h.detail(w)['shifts'] for p in s['participants'])
    assert h.detail(undone['workspace'])['shifts'] == before


def test_replace_never_picks_someone_a_mandatory_rule_blocks(h):
    alex, jordan, taylor = h.seed['people']
    h.policy({'kind': 'restriction', 'weekdays': [6], 'start_time': '00:00', 'end_time': '24:00'}, {'person_id': jordan})  # Sundays (4 Oct)
    w = leave_rota(h)
    plan = preview(h, w, [issue_key(f) for f in issues(h, w, 'UNAVAILABLE')])['plan']
    assert [r['replacement_id'] for r in plan] == [taylor, jordan]


def resolution_history(h, workspace):
    with transaction(h.app.state.engine) as db:
        return ([row.id for row in db.scalars(select(m.ChangeSet).where(m.ChangeSet.workspace_id == workspace['id']))],
                [row.id for row in db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == 'issues.resolve'))])


def test_accept_refuses_mandatory_issues_by_code(h):
    workspace = leave_rota(h, days=('04',))
    keys = [issue_key(f) for f in issues(h, workspace, 'UNAVAILABLE')]
    before, history = h.detail(workspace), resolution_history(h, workspace)
    refused = resolve(h, workspace, 'accept', keys, status=409)
    assert refused['error']['code'] == 'ACCEPT_ADVISORY_ONLY'
    assert h.detail(workspace) == before and resolution_history(h, workspace) == history


def test_replace_is_rolled_back_when_the_full_check_finds_a_mandatory_breach(h, monkeypatch):
    from apps.api.services import issues as service
    jordan = h.seed['people'][1]
    h.policy({'kind': 'max_duties', 'maximum': 0, 'window': 'week'}, {'person_id': jordan})
    workspace = leave_rota(h, days=('04',))
    keys = [issue_key(f) for f in issues(h, workspace, 'UNAVAILABLE')]
    real = service.best_replacements

    def unsafe_plan(*args):
        rows = real(*args)
        # Even a defective reduced plan cannot override the final whole-rota check.
        rows[0].update(replacement_id=jordan, replacement_name='Synthetic blocked replacement', status='ok', findings=[])
        return rows

    monkeypatch.setattr(service, 'best_replacements', unsafe_plan)
    plan = preview(h, workspace, keys)
    before, history = h.detail(workspace), resolution_history(h, workspace)
    refused = resolve(h, workspace, 'replace', keys, plan['plan_hash'], status=409)
    assert refused['error']['code'] == 'REPLACEMENT_BLOCKED'
    assert h.detail(workspace) == before and resolution_history(h, workspace) == history


def test_replacement_preserves_unrelated_existing_mandatory_findings(h):
    h.policy({'kind': 'restriction', 'weekdays': [1]}, {'person_id': h.seed['people'][0]})
    workspace = leave_rota(h, days=('04',), seed_restricted=True)
    keys = [issue_key(f) for f in issues(h, workspace, 'UNAVAILABLE')]
    existing = {issue_key(f) for f in issues(h, workspace, 'RESTRICTION')}
    assert existing
    plan = preview(h, workspace, keys)
    done = resolve(h, workspace, 'replace', keys, plan['plan_hash'])
    assert not issues(h, done['workspace'], 'UNAVAILABLE')
    assert {issue_key(f) for f in issues(h, done['workspace'], 'RESTRICTION')} == existing


def test_replacement_can_improve_a_mandatory_minimum_without_finishing_it(h):
    jordan, other = h.seed['people'][1:]
    # Keep the quota improvement independent of the equal-load tie order.
    h.policy({'kind': 'restriction', 'weekdays': list(range(7))}, {'person_id': other})
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
              'metric': 'duties', 'comparison': 'minimum', 'amount': 3}, {'person_id': jordan})
    workspace = leave_rota(h, days=('04',))
    keys = [issue_key(f) for f in issues(h, workspace, 'UNAVAILABLE')]
    plan = preview(h, workspace, keys)
    assert plan['plan'][0]['replacement_id'] == jordan
    done = resolve(h, workspace, 'replace', keys, plan['plan_hash'])
    minimum = next(f for f in issues(h, done['workspace'], 'QUOTA_BELOW_MINIMUM') if f['person_id'] == jordan)
    assert minimum['detail']['actual'] == 1 and minimum['detail']['amount'] == 3
    assert not issues(h, done['workspace'], 'UNAVAILABLE')


@pytest.mark.parametrize('action', ['unassign', 'remove'])
def test_issue_batch_locked_selection_is_atomic(h, action):
    workspace = leave_rota(h)
    keys = [issue_key(f) for f in issues(h, workspace, 'UNAVAILABLE')]
    rows = h.detail(workspace)['shifts']
    workspace = h.command('coordinator', 'shift.action', {'workspace_id': workspace['id'],
        'shift_ids': [rows[1]['id']], 'action': 'lock', 'reason': 'Keep this reviewed session'},
        revision=workspace['revision'])['workspace']
    before, history = h.detail(workspace), resolution_history(h, workspace)
    refused = resolve(h, workspace, action, keys, status=409)
    assert refused['error']['code'] == 'ASSIGNMENT_LOCKED'
    assert h.detail(workspace) == before and resolution_history(h, workspace) == history


@pytest.mark.parametrize('action,invalid', [
    ('lock', 'foreign'), ('lock', 'missing'), ('lock', 'locked'),
    ('remove', 'foreign'), ('remove', 'missing'), ('remove', 'locked'), ('copy', 'foreign'), ('copy', 'missing')])
def test_bulk_session_selection_keeps_scope_and_preconditions(h, action, invalid):
    workspace = h.shift(h.workspace())['workspace']
    first = h.detail(workspace)['shifts'][0]['id']
    bad = m.uid()
    if invalid == 'foreign':
        other = h.shift(h.workspace('Other owned synthetic rota'))['workspace']
        bad = h.detail(other)['shifts'][0]['id']
    elif invalid == 'locked':
        workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')['workspace']
        bad = next(row['id'] for row in h.detail(workspace)['shifts'] if row['id'] != first)
        workspace = h.command('coordinator', 'shift.action', {'workspace_id': workspace['id'],
            'shift_ids': [bad], 'action': 'lock', 'reason': 'Protect the second session'}, revision=workspace['revision'])['workspace']
    before, history = h.detail(workspace), resolution_history(h, workspace)
    payload = {'workspace_id': workspace['id'], 'shift_ids': [first, bad], 'reason': 'Atomic synthetic action'}
    payload.update({'offset_days': 1} if action == 'copy' else {'action': action})
    refused = h.command('coordinator', 'shift.copy' if action == 'copy' else 'shift.action', payload,
        revision=workspace['revision'], status=409 if invalid == 'locked' else 404)
    assert refused['error']['code'] == ('ASSIGNMENT_LOCKED' if invalid == 'locked' else 'NOT_FOUND')
    assert h.detail(workspace) == before and resolution_history(h, workspace) == history


def test_chunked_batch_helpers_keep_unselected_people_and_other_workspaces(h, monkeypatch):
    from apps.api.services import planning
    workspace = h.workspace()
    for day in ('04', '05', '06'):
        workspace = h.shift(workspace, start=f'2026-10-{day}T08:00:00+03:00',
            end=f'2026-10-{day}T12:00:00+03:00', people=h.seed['people'][:2])['workspace']
    ids = {row['id'] for row in h.detail(workspace)['shifts']}
    other = h.shift(h.workspace('Untouched batch helper control'))['workspace']
    original = planning.chunks
    monkeypatch.setattr(planning, 'chunks', lambda values, size=1000: original(values, size=2))
    with transaction(h.app.state.engine, write=True) as db:
        rows = planning.selected_shifts(db, db.get(m.Workspace, workspace['id']), ids)
        assert set(rows) == ids
        planning.remove_participants(db, [(sid, h.seed['people'][0]) for sid in ids])
        assert set(db.scalars(select(m.Assignment.person_id).where(m.Assignment.shift_id.in_(ids)))) == {h.seed['people'][1]}
        planning.remove_shifts(db, ids)
        assert not list(db.scalars(select(m.Shift).where(m.Shift.id.in_(ids))))
        assert not list(db.scalars(select(m.Assignment).where(m.Assignment.shift_id.in_(ids))))
        assert not list(db.scalars(select(m.ShiftService).where(m.ShiftService.shift_id.in_(ids))))
    assert h.detail(workspace)['shifts'] == []
    assert len(h.detail(other)['shifts'][0]['participants']) == 1


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_replace_says_who_stays_open_and_refuses_a_changed_plan(request, backend):
    h = request.getfixturevalue(backend)
    w = leave_rota(h, days=('04',))
    keys = [issue_key(f) for f in issues(h, w, 'UNAVAILABLE')]
    first = preview(h, w, keys)
    jordan, taylor = sorted(h.seed['people'][1:],
                            key=lambda pid: best_order_key(0, 0, pid, first['plan'][0]['shift_id']))
    assert first['plan'][0]['replacement_id'] == jordan
    # The first choice's leave is recorded after the preview: same rota revision, different plan.
    h.command('coordinator', 'availability.record', {'person_id': jordan, 'kind': 'recorded_leave', 'private_reason': 'Away',
        'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-05T00:00:00+03:00'})
    before = h.detail(w)
    assert resolve(h, w, 'replace', keys, first['plan_hash'], status=409)['error']['code'] == 'PLAN_CHANGED'
    assert h.detail(w) == before, 'a changed replacement plan never changes the draft or stored check'
    w = h.lifecycle(h.detail(w)['workspace'], 'validate')['workspace']
    before = h.detail(w)
    assert resolve(h, w, 'replace', keys, first['plan_hash'], status=409)['error']['code'] == 'PLAN_CHANGED'
    assert h.detail(w) == before
    h.command('coordinator', 'availability.record', {'person_id': taylor, 'kind': 'recorded_leave', 'private_reason': 'Away',
        'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-05T00:00:00+03:00'})
    w = h.lifecycle(h.detail(w)['workspace'], 'validate')['workspace']
    again = preview(h, w, keys)
    assert again['open'] == 1 and again['plan'][0]['replacement_id'] is None and again['plan'][0]['why']
    assert resolve(h, w, 'replace', keys, again['plan_hash'], status=409)  # nothing to replace


@pytest.mark.parametrize('action', ['preview', 'replace', 'unassign', 'remove', 'accept'])
@pytest.mark.parametrize('change', ['record', 'retract'])
def test_resolving_requires_a_current_check_after_external_inputs_change(h, action, change):
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week'}, severity='advisory')
    w = leave_rota(h)
    findings = issues(h, w)
    keys = [issue_key(f) for f in findings if (f['severity'] != 'error' if action == 'accept' else f['code'] == 'UNAVAILABLE')]
    plan_hash = preview(h, w, keys)['plan_hash'] if action == 'replace' else None
    if change == 'record':
        h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0], 'kind': 'recorded_leave',
            'start': '2026-10-06T08:00:00+03:00', 'end': '2026-10-06T12:00:00+03:00', 'private_reason': 'New absence'})
    else:
        leave = h.get('coordinator', '/api/availability')['availability'][0]
        h.command('coordinator', 'availability.retract', {'reason': 'Leave cancelled'}, leave['id'], leave['revision'])
    before = h.detail(w)
    assert before['workspace']['revision'] == w['revision']
    assert before['workspace']['validation']['fingerprint'] != before['current_fingerprint']
    result = preview(h, w, keys, status=409) if action == 'preview' else resolve(h, w, action, keys, plan_hash, status=409)
    assert result['error']['code'] == 'STALE_CHECK'
    after = h.detail(w)
    assert after['shifts'] == before['shifts']
    assert after['workspace'] == before['workspace']
    checked = h.lifecycle(w, 'validate')['workspace']
    assert checked['validation']['fingerprint'] == before['current_fingerprint']
    unavailable = issues(h, checked, 'UNAVAILABLE')
    if change == 'record':
        shifts = {shift['id']: shift for shift in before['shifts']}
        assert {shifts[f['shift_id']]['start'][:10] for f in unavailable} == {'2026-10-04', '2026-10-05', '2026-10-06'}
        assert {f['person_id'] for f in unavailable} == {h.seed['people'][0]}
    else:
        assert not unavailable


@pytest.mark.parametrize('action', ['preview', 'replace', 'unassign', 'remove', 'accept'])
@pytest.mark.parametrize('person,day,check_current', [(0, '2026-12-01', True), (1, '2026-10-10', False)])
def test_resolving_rechecks_and_proceeds_after_unrelated_availability_changes(h, action, person, day, check_current):
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week'}, severity='advisory')
    w = leave_rota(h)
    keys = [issue_key(f) for f in issues(h, w)
            if (f['severity'] != 'error' if action == 'accept' else f['code'] == 'UNAVAILABLE')]
    first = preview(h, w, keys) if action == 'replace' else None
    # Far-future leave stays outside the scoped inputs. In-window leave on another day changes the fingerprint
    # but not these findings or the replacement plan, so an automatic recheck can still proceed.
    h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][person], 'kind': 'recorded_leave',
        'start': day + 'T08:00:00+03:00', 'end': day + 'T12:00:00+03:00', 'private_reason': 'Unrelated absence'})
    before = h.detail(w)
    assert before['workspace']['revision'] == w['revision']
    assert before['workspace']['validation']['fingerprint'] == w['validation']['fingerprint']
    assert (before['workspace']['validation']['fingerprint'] == before['current_fingerprint']) is check_current
    if action == 'replace':
        assert preview(h, w, keys) == first
    result = preview(h, w, keys) if action == 'preview' else resolve(h, w, action, keys, first['plan_hash'] if first else None)
    if action == 'preview':
        assert h.detail(w) == before, 'preview does not save a check or change the draft'
    else:
        assert result['workspace']['revision'] == w['revision'] + 1
        assert result['validation']['fingerprint'] == h.detail(result['workspace'])['current_fingerprint']


@pytest.mark.parametrize('action', ['preview', 'replace', 'unassign', 'remove', 'accept'])
def test_resolving_refuses_changed_issues_from_new_rules_in_force(h, action):
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week'}, severity='advisory')
    w = leave_rota(h)
    keys = [issue_key(f) for f in issues(h, w)
            if (f['severity'] != 'error' if action == 'accept' else f['code'] == 'UNAVAILABLE')]
    plan_hash = preview(h, w, keys)['plan_hash'] if action == 'replace' else None
    h.policy({'kind': 'restriction', 'weekdays': [1], 'start_time': '00:00', 'end_time': '24:00'},
             {'person_id': h.seed['people'][0]})  # Tuesday, 6 Oct: a new mandatory issue.
    before = h.detail(w)
    result = preview(h, w, keys, status=409) if action == 'preview' else resolve(h, w, action, keys, plan_hash, status=409)
    assert result['error']['code'] == 'STALE_CHECK'
    assert h.detail(w) == before, 'a stale selection never changes the draft or its stored check'


@pytest.mark.parametrize('change', ['severity', 'detail', 'duplicate'])
def test_freshness_checks_complete_finding_values_even_when_the_issue_key_stays_the_same(monkeypatch, change):
    from types import SimpleNamespace
    from apps.api.errors import Problem
    from apps.api.services import issues as service
    finding = {'code': 'MAX_DUTIES', 'rule_id': 'rule', 'shift_id': 'shift', 'person_id': 'person', 'start': '2026-10-04',
               'severity': 'warning', 'detail': {'maximum': 1}}
    current = [{**finding}]
    if change == 'severity':
        current[0]['severity'] = 'error'
    elif change == 'detail':
        current[0]['detail'] = {'maximum': 0}
    else:
        current.append({**finding, 'severity': 'error'})
    assert {issue_key(f) for f in current} == {issue_key(finding)}
    workspace = SimpleNamespace(validation={'fingerprint': 'old', 'findings': [finding]})
    monkeypatch.setattr(service, 'fingerprint', lambda db, w: 'current')
    monkeypatch.setattr(service, 'domain_snapshot', lambda db, w: {})
    monkeypatch.setattr(service, 'evaluate', lambda source: {'findings': current})
    with pytest.raises(Problem) as error:
        service.chosen(None, workspace, [issue_key(finding)])
    assert error.value.code == 'STALE_CHECK'


def test_replace_refuses_new_candidate_warnings_even_when_the_person_stays_the_same(h):
    _, jordan, taylor = h.seed['people']
    w = leave_rota(h, days=('04',))
    keys = [issue_key(f) for f in issues(h, w, 'UNAVAILABLE')]
    first = preview(h, w, keys)
    h.policy({'kind': 'restriction', 'weekdays': [6], 'start_time': '00:00', 'end_time': '24:00'},
             {'person_ids': [jordan, taylor]}, severity='advisory')
    again = preview(h, w, keys)
    assert first['plan'][0]['replacement_id'] == again['plan'][0]['replacement_id'] == min(
        (jordan, taylor), key=lambda pid: best_order_key(0, 0, pid, first['plan'][0]['shift_id']))
    assert first['plan'][0]['status'] == 'ok' and again['plan'][0]['status'] == 'warn'
    assert again['plan'][0]['findings'] and again['plan_hash'] != first['plan_hash']
    assert resolve(h, w, 'replace', keys, first['plan_hash'], status=409)['error']['code'] == 'PLAN_CHANGED'


def test_replace_refuses_tokens_from_the_previous_fingerprint_based_format(h):
    from apps.api.snapshots import snapshot_hash
    w = leave_rota(h)
    keys = [issue_key(f) for f in issues(h, w, 'UNAVAILABLE')]
    first = preview(h, w, keys)
    old_hash = snapshot_hash({'fingerprint': h.snapshot(w)['fingerprint'],
                             'plan': [[row['assignment_id'], row['replacement_id']] for row in first['plan']]})
    assert resolve(h, w, 'replace', keys, old_hash, status=409)['error']['code'] == 'PLAN_CHANGED'
    refreshed = preview(h, w, keys)
    done = resolve(h, w, 'replace', keys, refreshed['plan_hash'])
    assert set(keys) <= set(done['issues']['removed']) and not issues(h, done['workspace'], 'UNAVAILABLE')


def test_unassign_remove_and_accept_are_one_change_each_and_check_what_they_touch(h):
    alex = h.seed['people'][0]
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week'}, severity='advisory')
    w = leave_rota(h)
    leave = [issue_key(f) for f in issues(h, w, 'UNAVAILABLE')]
    advisory = [issue_key(f) for f in issues(h, w) if f['severity'] != 'error']
    assert advisory, 'three duties in a week break the advisory weekly maximum'
    resolve(h, w, 'accept', leave[:1], status=409)  # a mandatory issue cannot be accepted
    accepted = resolve(h, w, 'accept', advisory)
    assert [a['key'] for a in accepted['workspace']['accepted_issues']] == advisory
    w = accepted['workspace']

    unassigned = resolve(h, w, 'unassign', leave[:1])
    assert unassigned['issues']['removed'] and unassigned['shifts'][0]['participants'] == []
    w = unassigned['workspace']
    gone = resolve(h, w, 'remove', leave[1:])
    assert len(gone['removed_shift_ids']) == 1 and len(h.detail(w)['shifts']) == 2
    w = gone['workspace']
    assert resolve(h, w, 'remove', leave[1:], status=409), 'an issue that is gone is refused'
    # Each batch is one Undo step: undoing the removal brings the session back with Alex on it.
    back = h.command('coordinator', 'workspace.undo', {'reason': 'Undo remove'}, w['id'], w['revision'])
    assert len(h.detail(back['workspace'])['shifts']) == 3
    assert [s['id'] for s in back['shifts']] == gone['removed_shift_ids'] and back['removed_shift_ids'] == []
    assert any(p['person_id'] == alex for s in h.detail(w)['shifts'] for p in s['participants'])


def test_resolving_is_revision_safe_and_for_coordinators_only(h):
    w = leave_rota(h)
    keys = [issue_key(f) for f in issues(h, w, 'UNAVAILABLE')]
    resolve(h, {**w, 'revision': w['revision'] + 1}, 'unassign', keys, status=409)
    for role in ('approver', 'auditor', 'staff'):
        resolve(h, w, 'unassign', keys, status=403, role=role)
        preview(h, w, keys, role=role, status=403)
    # Reviewers read the same issues (read-only); staff cannot read the rota at all.
    assert issues(h, w, 'UNAVAILABLE')
    for role in ('approver', 'auditor'):
        assert h.get(role, '/api/workspaces/' + w['id'])['workspace']['validation']['findings']
    h.get('staff', '/api/workspaces/' + w['id'], status=403)
    h.command('coordinator', 'issues.resolve', {'workspace_id': w['id'], 'action': 'replace', 'keys': keys, 'reason': 'No preview'},
              revision=w['revision'], status=422)


def test_exceptions_for_many_issues_never_cover_a_safety_rule(h):
    alex, jordan, _ = h.seed['people']
    cap = h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week'}, exception=True)
    rest = h.policy({'kind': 'restriction', 'weekdays': [6], 'start_time': '00:00', 'end_time': '24:00'})
    items = [{'policy_id': cap['id'], 'person_id': p, 'starts_on': '2026-10-04', 'ends_on': '2026-10-11'} for p in (alex, jordan)]
    made = h.command('coordinator', 'exception.create_many', {'items': items, 'reason': 'Cover the leave week'})
    assert [r['status'] for r in made['records']] == ['proposed', 'proposed']
    safety = [{'policy_id': rest['id'], 'person_id': alex, 'starts_on': '2026-10-04', 'ends_on': '2026-10-05'}]
    h.command('coordinator', 'exception.create_many', {'items': items + safety, 'reason': 'Not for safety'}, status=409)
    h.command('approver', 'exception.create_many', {'items': items, 'reason': 'Not mine'}, status=403)


def department_rota(h, seats=500):
    """150 people over 28 days, 6 clinics (~370 sessions) staffed in turn so each person works about every sixth day
    (inside the department's rules), and `seats` of those places to refill."""
    from tests.scheduler_departments import department
    data, _ = department(h, 11, people=150, days=28, rules='full', leave_rate=0.05, clinics=6)
    # Demo people have UUID4 IDs; keep seeded staffing independent of identity values.
    people = sorted(data['people'], key=lambda pid: data['people'][pid]['display_name'].casefold())
    targets, n = {}, 0
    for shift in sorted(data['shifts'], key=lambda s: (s['start'], s['id'])):
        for _ in range(1 if shift['type_snapshot']['definition'].get('requirements', [{}])[0].get('maximum') == 1 else 2):
            pid = people[n % len(people)]
            shift['participants'].append({'id': f'seat-{n}', 'shift_id': shift['id'], 'person_id': pid, 'start': shift['start'],
                                          'end': shift['end'], 'participation': 'staff', 'overtime': False, 'segments': [],
                                          'fixed': False, 'supervisor_person_id': None, 'team_lead': False})
            if len(targets) < seats:
                targets[(shift['id'], pid)] = [f'k{n}']
            n += 1
    return data, targets, set(people)


def replacements_measurement(h, monkeypatch):
    """The work is counted (validator checks per issue, machine-independent) and timed with a CI-aware budget: about
    8 reduced checks an issue here, ~7 s for 500 on a developer machine."""
    from apps.api.services import staffing
    data, targets, people = department_rota(h)
    # Warm the pure ranking path; collect fixture garbage before counting this batch's work.
    best_replacements(data, dict([next(iter(targets.items()))]), people)
    gc.collect()
    checks = []
    real = staffing.evaluate
    monkeypatch.setattr(staffing, 'evaluate', lambda data, **kwargs: checks.append(1) or real(data, **kwargs))
    # This pure calculation runs on this thread; process CPU also charges background API/browser threads.
    started, process_started, wall_started = time.thread_time(), time.process_time(), time.perf_counter()
    rows = best_replacements(data, targets, people)
    elapsed = time.thread_time() - started
    print('replace department scale', {'issues': len(rows), 'checks': len(checks), 'cpu_seconds': elapsed,
                                       'process_cpu_seconds': time.process_time() - process_started,
                                       'wall_seconds': time.perf_counter() - wall_started})
    return len(targets), rows, len(checks), elapsed


@pytest.fixture(scope='module')
def best_replacements_measurement(scale_harness):
    with pytest.MonkeyPatch.context() as monkeypatch:
        return replacements_measurement(scale_harness('h'), monkeypatch)


def test_best_replacements_for_500_issues_stay_fast_at_department_scale(best_replacements_measurement):
    targets, rows, checks, _ = best_replacements_measurement
    assert targets == 500
    assert len(rows) == 500 and sum(r['replacement_id'] is not None for r in rows) > 400
    assert checks < 12 * 500, checks


@pytest.mark.cpu_budget
def test_best_replacements_for_500_issues_time(best_replacements_measurement):
    *_, elapsed = best_replacements_measurement
    assert elapsed < _budget(30), f'{elapsed:.1f} CPU s'


def test_every_draft_edit_says_what_changed_like_a_batch_and_its_undo(h):
    """Forward edits reply in the shape of a batch and an undo (planning.changed): the rota and its health, the sessions
    changed or removed and the issues that came and went, so the Schedule patches them in place."""
    w = leave_rota(h)
    leave = {issue_key(f) for f in issues(h, w, 'UNAVAILABLE')}
    created = h.shift(w, '2026-10-07T08:00:00+03:00', '2026-10-07T12:00:00+03:00')
    assert [s['start'][:10] for s in created['shifts']] == ['2026-10-07'] and created['removed_shift_ids'] == []
    assert created['health']['checked'] and not created['issues']['removed']
    w = created['workspace']
    first = next(s for s in h.detail(w)['shifts'] if s['start'].startswith('2026-10-04'))
    cleared = h.command('coordinator', 'shift.update', {'workspace_id': w['id'], 'shift_id': first['id'], 'assignment_type_id': first['assignment_type_id'],
        'location_id': first['location_id'], 'service_ids': first['service_ids'], 'start': first['start'], 'end': first['end'],
        'attributes': first.get('attributes') or {}, 'participants': []}, revision=w['revision'])
    assert [s['id'] for s in cleared['shifts']] == [first['id']] and cleared['shifts'][0]['participants'] == []
    assert set(cleared['issues']['removed']) & leave  # Alex's leave issue on 4 Oct went with the edit
    w = cleared['workspace']
    gone = h.command('coordinator', 'shift.action', {'workspace_id': w['id'], 'shift_ids': [first['id']], 'action': 'remove', 'reason': 'Not needed'},
                     revision=w['revision'])
    assert gone['removed_shift_ids'] == [first['id']] and gone['shifts'] == []


def test_replacement_apply_reports_resolved_keys_for_the_seven_issue_browser_fixture(h):
    h.policy({'kind': 'min_rest', 'hours': 12})
    person = h.seed['people'][0]
    workspace = h.workspace('Replacement lifecycle count')
    for day in (4, 5, 6):
        for start, end in (('08', '12'), ('13', '17')):
            workspace = h.shift(workspace, f'2026-10-{day:02}T{start}:00:00+03:00',
                                f'2026-10-{day:02}T{end}:00:00+03:00', people=[person])['workspace']
    h.command('coordinator', 'availability.record', {'person_id': person, 'kind': 'recorded_leave',
        'leave_type': 'Annual leave', 'start': '2026-10-05T00:00:00+03:00',
        'end': '2026-10-07T00:00:00+03:00', 'private_reason': 'Synthetic absence'})
    workspace = h.lifecycle(h.detail(workspace)['workspace'], 'validate')['workspace']
    before = {issue_key(f) for f in issues(h, workspace)}
    keys = [issue_key(f) for f in issues(h, workspace, 'UNAVAILABLE')]
    assert len(before) == 7 and len(keys) == 4
    plan = preview(h, workspace, keys)
    assert plan['replaceable'] == 4
    result = resolve(h, workspace, 'replace', keys, plan['plan_hash'])
    after = {issue_key(f) for f in issues(h, result['workspace'])}
    assert set(result['issues']['removed']) == before - after
    assert set(keys) <= set(result['issues']['removed'])
    assert not issues(h, result['workspace'], 'UNAVAILABLE')
    undone = h.command('coordinator', 'workspace.undo', {'reason': 'Restore the replacement batch'},
                       workspace['id'], result['workspace']['revision'])
    assert set(keys) <= {issue_key(f) for f in undone['issues']['added']}
    assert {issue_key(f) for f in issues(h, undone['workspace'])} == before
