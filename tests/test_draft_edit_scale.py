"""Draft edits at department scale: a Resolve issues batch and its Undo cost what they touch, not the whole rota, and
leave exactly the rota check a whole check gives (docs/design/resolve-issues.md)."""
from datetime import date, datetime, timedelta, timezone
from random import Random
import pytest
from apps.api.services import issues as issues_service, planning
from apps.api.services.issues import issue_key
from tests.test_backend_scale import measure
from tests.test_performance_budget import python_budget
from tests.test_resolve_issues import preview
from packages.domain.validation import INTERNAL_CHECK_KEYS

UNSTABLE = ('evaluated_at', 'fingerprint', 'input_manifest', 'fix_list_state', 'fix_list_delta')  # check identity and edit history


def department_draft(h, people_count=150, per_day=16, days=31, person_rules=40, leave_rate=0.08, seed=5):
    """An October draft inserted directly: `people_count` people, `per_day` sessions a day with two people each (~500
    at the defaults), leave on ~`leave_rate` of person-days, seven department rules and 2 x `person_rules` rules naming
    one person each: several hundred issues, a few dozen of them leave."""
    from sqlalchemy import select
    from apps.api import models as m
    from apps.api.db import transaction
    from apps.api.serialization import record
    s, rng, first = h.seed, Random(seed), date(2026, 10, 1)
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, s['department_id'])
        category = db.scalar(select(m.Membership.category_id).where(m.Membership.person_id == s['people'][0]))
        extra = [m.Person(organisation_id=department.organisation_id, display_name=f'Synthetic person {i:03}') for i in range(people_count - 3)]
        db.add_all(extra)
        db.flush()
        db.add_all([m.Membership(person_id=p.id, category_id=category, department_id=department.id, starts_on=date(2020, 1, 1)) for p in extra])
        people = list(s['people']) + [p.id for p in extra]
    # Rules first, while the draft is empty (activating a rule re-checks open drafts).
    for rule, severity in (({'kind': 'min_rest', 'hours': 11}, 'mandatory'), ({'kind': 'max_duties', 'maximum': 1, 'window': 'day'}, 'mandatory'),
                           ({'kind': 'max_duties', 'maximum': 5, 'window': 'week'}, 'mandatory'),
                           ({'kind': 'max_hours', 'hours': 40, 'window': 'week'}, 'mandatory'),
                           ({'kind': 'max_consecutive_days', 'maximum': 4}, 'mandatory'),
                           ({'kind': 'max_duties', 'maximum': 3, 'window': 'week'}, 'advisory'), ({'kind': 'max_hours', 'hours': 6, 'window': 'day'}, 'advisory')):
        h.policy(rule, severity=severity)
    for i in range(person_rules):
        scope = {'person_id': people[(3 + i) % people_count], 'weekdays': [i % 7], 'start_time': '00:00', 'end_time': '12:00'}
        if i % 2:
            h.policy({'kind': 'max_duties', 'maximum': 0, 'window': 'day'}, scope, severity='advisory')
        else:
            h.policy({'kind': 'restriction'}, scope)
        h.policy({'kind': 'max_hours', 'hours': 30 + i % 10, 'window': 'week'}, {'person_id': people[(60 + i) % people_count]}, severity='advisory')
    w = h.workspace('Department October', first.isoformat(), (first + timedelta(days=days)).isoformat())
    with transaction(h.app.state.engine, write=True) as db:
        typ = db.get(m.AssignmentType, s['type'])
        shifts, rest, n = [], [], 0
        for day in range(days):
            on = first + timedelta(days=day)
            for k in range(per_day):
                hours = [(5, 9), (10, 14), (17, 20)][k % 3]  # UTC; Riyadh is +3
                start = datetime(on.year, on.month, on.day, hours[0], tzinfo=timezone.utc)
                end = datetime(on.year, on.month, on.day, hours[1], tzinfo=timezone.utc)
                shift = m.Shift(id=f'{w["id"][:8]}-{day}-{k}', workspace_id=w['id'], assignment_type_id=typ.id, type_revision=typ.revision,
                                type_snapshot=record(typ), location_id=s['locations'][k % 2], start=start, end=end, attributes={})
                shifts.append(shift)
                rest.append(m.ShiftService(shift_id=shift.id, service_id=s['services'][k % 2]))
                for _ in range(2):
                    rest.append(m.Assignment(shift_id=shift.id, person_id=people[n % people_count], start=start, end=end))
                    n += 1
        db.add_all(shifts)
        db.flush()
        db.add_all(rest)
    w = h.refresh_draft_baseline(h.detail(w)['workspace'])
    with transaction(h.app.state.engine, write=True) as db:
        for person in people:
            for day in range(days):
                if rng.random() < leave_rate:
                    on = datetime.combine(first + timedelta(days=day), datetime.min.time(), timezone.utc) - timedelta(hours=3)
                    db.add(m.Availability(person_id=person, department_id=s['department_id'], kind='recorded_leave', private_reason='Leave',
                                          recorded_by=s['identities']['coordinator'], start=on, end=on + timedelta(days=1)))
    return h.lifecycle(w, 'validate')['workspace'], people


def stored(h, w):
    """The check stored on the rota (the read adds display details, such as the leave type, to its findings)."""
    from apps.api import models as m
    from apps.api.db import transaction
    with transaction(h.app.state.engine) as db:
        return db.get(m.Workspace, w['id']).validation


def whole_check(h, w):
    """A fresh whole check of the rota as it is now, as the stored one is computed (planning.revalidate)."""
    from apps.api import models as m
    from apps.api.db import transaction
    from apps.api.snapshots import domain_snapshot
    from packages.domain.finding_causes import DIAGNOSTIC_VERSION
    from packages.domain.rota_summary import fix_list_context
    from packages.domain.score import score_dependencies
    from packages.domain.validation import evaluate
    with transaction(h.app.state.engine) as db:
        workspace = db.get(m.Workspace, w['id'])
        data = domain_snapshot(db, workspace)
        current_manifest = {k: data['input_manifest'][k] for k in ('format', 'token', 'components', 'dirty_hint')}
        assert workspace.validation['fingerprint'] == data['fingerprint']
        assert workspace.validation['input_manifest'] == current_manifest
        return {**evaluate(data), 'diagnostic_version': DIAGNOSTIC_VERSION,
            'score_dependencies': score_dependencies(db.get(m.Department, h.seed['department_id']).generation_defaults),
            'input_manifest': current_manifest,
            'fix_list_context': fix_list_context(data)}


def same_check(a, b):
    import json
    # Session-cache indices are private; all public check fields still compare exactly.
    a, b = (json.loads(json.dumps({k: v for k, v in c.items() if k not in UNSTABLE + ('score',) and k not in INTERNAL_CHECK_KEYS})) for c in (a, b))
    return a == b


def assert_fix_history(before, after):
    from packages.domain.rota_summary import fix_list_delta, rota_summary
    previous, current = (rota_summary(check)['fix_list'] for check in (before, after))
    assert after['fix_list_delta'] == fix_list_delta(previous, current)
    assert after['fix_list_state']['previous_ids'] == [cause['id'] for cause in previous['causes']]


def draft_edit_measurement(h, monkeypatch):
    """~500 sessions, 150 people, 87 rules and ~1,900 issues: Apply reuses the plan the preview worked out and Undo puts
    back only the sessions the batch changed (both counted, machine-independent), each under a CI-aware budget.
    On a developer machine about 0.5 s for the preview, 1 s for the batch and 0.8 s for the Undo (were 3.1, 5.4, 5.4 s)."""
    plans, restored = [], []
    real_plan, real_copy = issues_service.best_replacements, planning.copy_snapshot_into
    monkeypatch.setattr(issues_service, 'best_replacements', lambda *a: plans.append(1) or real_plan(*a))
    def restore(db, actor, w, source, **kwargs):
        preparation = db.info.get('_planning_preparation')
        restored.append((len(source['shifts']), preparation is None or preparation['replay']))
        return real_copy(db, actor, w, source, **kwargs)
    monkeypatch.setattr(planning, 'copy_snapshot_into', restore)
    w, _ = department_draft(h)
    before = stored(h, w)
    leave = [issue_key(f) for f in before['findings'] if f['code'] == 'UNAVAILABLE']
    shape = len(h.detail(w)['shifts']), len(before['findings']), len(leave)
    keys = leave[:50]

    plan, preview_metrics = measure(h, 'draft-preview', lambda: preview(h, w, keys))
    done, batch_metrics = measure(h, 'draft-batch', lambda: h.client('coordinator').post('/api/commands',
        headers={'idempotency-key': 'department-scale-batch-1'}, json={
        'kind': 'issues.resolve', 'payload': {'workspace_id': w['id'], 'action': 'replace', 'keys': keys, 'reason': 'Resolve leave',
                                              'plan_hash': plan['plan_hash']}, 'expected_revision': w['revision']}))
    assert done.status_code == 200, done.text
    done = done.json()
    batch_check, whole = stored(h, done['workspace']), whole_check(h, done['workspace'])

    w = done['workspace']
    undone, undo_metrics = measure(h, 'draft-undo', lambda: h.client('coordinator').post('/api/commands',
        headers={'idempotency-key': 'draft-scale-undo-1'}, json={
        'kind': 'workspace.undo', 'payload': {'reason': 'Undo the batch'}, 'target_id': w['id'], 'expected_revision': w['revision']}))
    assert undone.status_code == 200, undone.text
    after = stored(h, undone.json()['workspace'])
    assert same_check(after, whole_check(h, undone.json()['workspace']))
    # Local CPU .56/.69/.70 s (coverage 1.30/1.64/1.45); CI Undo's 12.079 wall s crossed 12 s. Exclude host waiting.
    metrics = dict(preview=preview_metrics, batch=batch_metrics, undo=undo_metrics)
    return shape, plans, plan, done, batch_check, whole, restored, before, after, metrics


@pytest.fixture(scope='module')
def draft_edit_scale_measurement(scale_harness):
    with pytest.MonkeyPatch.context() as monkeypatch:
        return draft_edit_measurement(scale_harness('h'), monkeypatch)


def test_a_50_issue_batch_and_its_undo_cost_what_they_touch(draft_edit_scale_measurement):
    shape, plans, plan, done, batch_check, whole, restored, before, after, _ = draft_edit_scale_measurement
    sessions, findings, leave = shape
    assert sessions == 496 and findings > 1000 and leave >= 50
    assert plans == [1], 'Apply reuses the plan the preview worked out'
    assert plan['replaceable'] >= 20 and len(done['shifts']) == len({r['shift_id'] for r in plan['plan'] if r['replacement_id']})
    assert same_check(batch_check, whole)
    assert_fix_history(before, batch_check)
    assert restored and all(count == len(done['shifts']) for count, _ in restored), 'Every preparation restores only changed sessions'
    assert sum(committed for _, committed in restored) == 1, 'Undo commits one restore'
    assert same_check(after, before) and after['score'] == before['score'], 'the rota and its check are as before the batch'
    assert_fix_history(batch_check, after)


@pytest.mark.cpu_budget
def test_a_50_issue_batch_and_its_undo_time(draft_edit_scale_measurement):
    *_, metrics = draft_edit_scale_measurement
    for name, budget in (('preview', 1.5), ('batch', 3.0), ('undo', 2.0)):
        assert metrics[name]['cpu_seconds'] < python_budget(budget), (name, metrics)


@pytest.mark.wall_benchmark
def test_a_50_issue_batch_and_its_undo_wall_benchmark(draft_edit_scale_measurement):
    *_, metrics = draft_edit_scale_measurement
    for name, budget in (('preview', 1.5), ('batch', 3.0), ('undo', 2.0)):
        assert metrics[name]['wall_seconds'] < python_budget(budget) * 4, (name, metrics)


@pytest.mark.parametrize('seed', [1, 2, 3])
def test_random_batches_and_undos_leave_the_whole_check(h, seed):
    """Random Resolve issues batches (replace, unassign, remove, accept) and Undos on a small department: after each
    the stored check is a whole check of the rota, and an Undo gives back exactly the rota and check before the step.
    The open page patches its issues from each reply's `issues` (added, removed): patched that way they are always the
    issues a fresh load lists, including those a batch changed without selecting them."""
    rng = Random(seed)
    w, _ = department_draft(h, people_count=14, per_day=3, days=10, person_rules=4, leave_rate=0.15, seed=seed)
    history, done = [], {'batch': 0, 'undo': 0}
    shown = {issue_key(f) for f in stored(h, w)['findings']}

    def patched(reply):
        nonlocal shown
        shown = (shown - set(reply['issues']['removed'])) | {issue_key(f) for f in reply['issues']['added']}
        assert shown == {issue_key(f) for f in stored(h, reply['workspace'])['findings']}
        assert_fix_history(check, stored(h, reply['workspace']))
        return reply['workspace']
    for step in range(12):
        check = stored(h, w)
        content = h.detail(w)['shifts']
        found = check['findings']
        if history and rng.random() < 0.35:
            w = patched(h.command('coordinator', 'workspace.undo', {'reason': 'Undo a step'}, w['id'], w['revision']))
            was_content, was_check = history.pop()
            history.clear()  # one Undo step only
            done['undo'] += 1
            assert h.detail(w)['shifts'] == was_content
            assert same_check(stored(h, w), was_check)
            assert same_check(stored(h, w), whole_check(h, w))
            continue
        action = rng.choice(['replace', 'unassign', 'remove', 'accept'])
        if action == 'accept':
            pool = [f for f in found if f['severity'] == 'warning']
        else:
            pool = [f for f in found if f.get('shift_id') and (f.get('person_id') or action == 'remove')]
        if not pool:
            continue
        keys = [issue_key(f) for f in rng.sample(pool, min(len(pool), rng.randint(1, 6)))]
        payload = {'workspace_id': w['id'], 'action': action, 'keys': keys, 'reason': f'Random step {step}'}
        if action == 'replace':
            plan = preview(h, w, keys)
            if not plan['replaceable']:
                continue
            payload['plan_hash'] = plan['plan_hash']
        response = h.client('coordinator').post('/api/commands', headers={'idempotency-key': f'random-{seed}-{step}-batch-key'},
                                                json={'kind': 'issues.resolve', 'payload': payload, 'expected_revision': w['revision']})
        if response.status_code == 409:  # e.g. a replacement the whole check refuses: nothing changed
            continue
        assert response.status_code == 200, response.text
        history = [(content, check)]
        w = patched(response.json())
        done['batch'] += 1
        assert same_check(stored(h, w), whole_check(h, w))
    assert done['batch'] >= 3 and done['undo'] >= 1, done


def test_cancel_actions_are_stable_after_undo_and_use_the_current_workspace_revision(h):
    workspace = h.shift(h.workspace())['workspace']
    workspace = h.shift(workspace, service=1)['workspace']
    before = h.detail(workspace)['shifts']
    action = next(action for shift in before for conflict in shift['double_booking_conflicts']
                  for action in conflict['cancel_actions'] if action['side'] == 'this')
    assert action['kind'] == 'shift.action' and not action.get('expected_revision')
    changed = h.shift(workspace, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00')['workspace']
    restored = h.command('coordinator', 'workspace.undo', {'reason': 'Restore synthetic sessions'},
                         changed['id'], changed['revision'])['workspace']
    assert restored['revision'] > workspace['revision']
    assert h.detail(restored)['shifts'] == before
    stale = h.command('coordinator', action['kind'], action['payload'], revision=workspace['revision'], status=409)
    assert stale['error']['code'] == 'STALE_REVISION'
    cancelled = h.command('coordinator', action['kind'], action['payload'])['workspace']
    assert sum(shift['cancelled'] for shift in h.detail(cancelled)['shifts']) == 1
    restored = h.command('coordinator', 'workspace.undo', {'reason': 'Restore cancelled synthetic session'},
                         cancelled['id'], cancelled['revision'])['workspace']
    assert h.detail(restored)['shifts'] == before
