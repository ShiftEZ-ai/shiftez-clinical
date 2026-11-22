"""A rota check never overwrites review decisions recorded by an approval step while it ran."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import uuid

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import planning
from tests.governance_support import identity, route
from tests.test_workspace_check_concurrency import check_harness, CHECK_PAYLOAD, stored_check  # noqa: F401


def submitted_with_review_items(h):
    """A submitted rota on a two-step route whose review items need decisions."""
    identity(h, 'programme', ['programme_lead'])
    route(h, [{'key': 'programme', 'label': 'Programme review', 'role': 'programme_lead'},
              {'key': 'clinical', 'label': 'Clinical review', 'role': 'approver'}])
    quota = {'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'metric': 'duties'}
    h.policy({**quota, 'comparison': 'minimum', 'amount': 3}, {'person_id': h.seed['people'][0]})
    target = h.policy({**quota, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'comparison': 'target',
                       'amount': 4, 'prorate_department_days': True}, severity='advisory')
    w = h.shift(h.workspace())['workspace']
    w = h.command('coordinator', 'workspace.target_override', {
        'policy_id': target['id'], 'person_id': h.seed['people'][1], 'amount': 2, 'reason': 'Adjust the planned target',
    }, w['id'], w['revision'])['workspace']
    cause, = [c for c in h.get('coordinator', f"/api/workspaces/{w['id']}/fix-list")['causes'] if c['acknowledgeable']]
    reply = h.command('coordinator', 'workspace.submit', {
        'reason': 'Submit for review', 'acknowledgements': [{'cause_id': cause['id'], 'reason': 'Acknowledge the shortfall'}],
    }, w['id'], w['revision'])
    items = {item['kind']: item['id'] for item in reply['review_items']}
    decisions = [
        {'item_id': items['target_override'], 'kind': 'adjustment', 'accept': True, 'reason': 'Target reviewed'},
        {'item_id': items['acknowledged_gap'], 'kind': 'gap', 'accept': True, 'reason': 'Shortfall reviewed'},
    ]
    return reply['workspace'], decisions


def first_step(h, w, decisions):
    """The real approve command for the first of two steps: the rota stays submitted."""
    reply = h.command('programme', 'workspace.approve', {'reason': 'Programme review done', 'decisions': decisions},
                      w['id'], w['revision'])
    assert reply['workspace']['status'] == 'submitted'
    assert reply['workspace']['content_revision'] == w['content_revision']
    assert reply['review_item_decisions']
    return reply


def pause_next_check(monkeypatch):
    """Hold the next rota check inside its evaluation; set up after the rota so set-up checks run freely."""
    entered, release, evaluate = Event(), Event(), planning.evaluate

    def held(data, **options):
        if not entered.is_set():
            entered.set()
            assert release.wait(30), 'Test did not release the rota check'
        return evaluate(data, **options)

    monkeypatch.setattr(planning, 'evaluate', held)
    return entered, release


def stored(h, w):
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, w['id'])
        return row.revision, row.validation


def test_check_refuses_to_store_over_decisions_recorded_while_it_ran(check_harness, monkeypatch):
    h = check_harness
    w, decisions = submitted_with_review_items(h)
    entered, release = pause_next_check(monkeypatch)
    key = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=1) as pool:
        check = pool.submit(h.command, 'coordinator', 'workspace.validate', CHECK_PAYLOAD,
                            w['id'], w['revision'], key=key, status=409)
        try:
            assert entered.wait(10)
            step = first_step(h, w, decisions)
        finally:
            release.set()
        refused = check.result(timeout=30)['error']
    assert refused['code'] == 'STALE_INPUTS'
    assert refused['details']['recoverable'] is True
    assert refused['details']['current_revision'] == step['workspace']['revision']
    revision, validation = stored(h, w)
    assert revision == step['workspace']['revision']
    assert validation['review_item_decisions'] == step['review_item_decisions']
    with transaction(h.app.state.engine) as db:
        assert db.query(m.CommandReceipt).filter(m.CommandReceipt.key == key).count() == 0


def test_check_without_a_concurrent_step_stores_and_keeps_recorded_decisions(check_harness, monkeypatch):
    h = check_harness
    w, decisions = submitted_with_review_items(h)
    w = first_step(h, w, decisions)['workspace']
    entered, release = pause_next_check(monkeypatch)
    release.set()
    reply = h.command('coordinator', 'workspace.validate', CHECK_PAYLOAD, w['id'], w['revision'])
    assert entered.is_set()
    revision, validation = stored(h, w)
    assert revision == w['revision']
    assert validation['evaluated_at'] == reply['validation']['evaluated_at']
    assert stored_check(h, w) == reply['validation']
    assert validation['review_item_decisions'] == h.detail(w)['workspace']['validation']['review_item_decisions']
    assert [row['item_id'] for row in validation['review_item_decisions']] == [d['item_id'] for d in decisions]


def test_revision_only_race_with_unchanged_decisions_still_stores(check_harness, monkeypatch):
    h = check_harness
    w, decisions = submitted_with_review_items(h)
    w = first_step(h, w, decisions)['workspace']
    recorded = stored(h, w)[1]['review_item_decisions']
    entered, release = pause_next_check(monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        check = pool.submit(h.command, 'coordinator', 'workspace.validate', CHECK_PAYLOAD, w['id'], w['revision'])
        try:
            assert entered.wait(10)
            # A plain revision bump (as from sharing) leaves every check input and the decisions unchanged.
            with transaction(h.app.state.engine, write=True) as db:
                db.get(m.Workspace, w['id']).revision += 1
        finally:
            release.set()
        reply = check.result(timeout=30)
    assert reply['workspace']['revision'] == w['revision'] + 1
    assert reply['workspace']['content_revision'] == w['content_revision']
    revision, validation = stored(h, w)
    assert revision == w['revision'] + 1
    assert validation['evaluated_at'] == reply['validation']['evaluated_at']
    assert validation['review_item_decisions'] == recorded
