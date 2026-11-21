"""A newer live auto-schedule run (queued, running, succeeded or applied) supersedes an older run of the same group
over any shared sessions: Apply/Continue refuse it (409 RUN_SUPERSEDED) and job reads say `currency: superseded`.
Failed or cancelled runs, runs for other sessions, and another planning group's run supersede nothing."""
import pytest

from apps.api import models as m
from apps.api.auth import authenticate
from apps.api.db import transaction
from apps.api.services.job_reads import proposals
from apps.worker.main import process_one
from packages.contracts import schemas as s


def run(h, w, sessions=None, population=None):
    payload = {'workspace_id': w['id'], 'kind': 'generation', **({'session_ids': sessions} if sessions else {}),
               **({'generation_scope': population} if population else {})}
    job = h.command('coordinator', 'job.create', payload, revision=h.detail(w)['workspace']['revision'])['job']
    assert process_one(h.app.state.engine)
    job = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert job['status'] == 'succeeded' and job['result']['additions'], job['result'].get('status')
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, job['id']).supersedes is not None
    return job


def apply(h, w, job, status=200):
    return h.command('coordinator', 'generation.apply', {'workspace_id': w['id'], 'job_id': job['id'],
        'reason': 'Apply the reviewed run'}, revision=h.detail(w)['workspace']['revision'], status=status)


def currency(h, w, job):
    listed = {j['id']: j for j in h.get('coordinator', f"/api/jobs?workspace_id={w['id']}")['jobs']}
    detail = h.get('coordinator', '/api/jobs/' + job['id'])['job']['currency']
    # Lists classify a dirty run as unknown (detail resolves it); supersession reads the same in both.
    assert (listed[job['id']]['currency'] == 'superseded') == (detail == 'superseded'), (listed[job['id']]['currency'], detail)
    return detail


def group(person):
    return {'conditions': {'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': [person]}]},
            'protection': 'keep_existing'}


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_a_superseded_run_is_refused_without_a_write_and_the_latest_run_applies(request, backend):
    h = request.getfixturevalue(backend)
    w = h.shift(h.workspace('Synthetic superseded run'), people=[])['workspace']
    older = run(h, w)
    newer = run(h, w)
    assert (currency(h, w, older), currency(h, w, newer)) == ('superseded', 'current')
    before = h.snapshot(w)
    refused = apply(h, w, older, status=409)
    assert refused['error']['code'] == s.RUN_SUPERSEDED == 'RUN_SUPERSEDED'
    assert h.snapshot(w) == before
    assert h.get('coordinator', '/api/jobs/' + older['id'])['job']['status'] == 'succeeded'
    applied = apply(h, w, newer)
    assert applied['reconciliation']['status'] == 'applied'
    assert h.snapshot(w)['shifts'] != before['shifts']
    assert h.get('coordinator', '/api/jobs/' + newer['id'])['job']['status'] == 'applied'
    # Applying the newer run does not make the older one applicable again, and Home offers it as superseded.
    assert currency(h, w, older) == 'superseded'
    assert apply(h, w, older, status=409)['error']['code'] == 'RUN_SUPERSEDED'
    client = h.client('coordinator')
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=False)
        waiting = proposals(db, actor, [db.get(m.Workspace, w['id'])])[w['id']]
    assert (waiting['job_id'], waiting['currency']) == (older['id'], 'superseded')


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_unfilled_newer_run_is_fail_safe_for_reads_and_apply(request, backend):
    h = request.getfixturevalue(backend)
    w = h.shift(h.workspace('Synthetic unfilled supersession'), people=[])['workspace']
    older = run(h, w)
    newer = run(h, w)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Job, newer['id']).supersedes = None
    assert currency(h, w, older) == 'superseded'
    assert apply(h, w, older, status=409)['error']['code'] == 'RUN_SUPERSEDED'


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('later_status', ['cancelled', 'failed'])
def test_superseded_currency_is_scoped_and_ignores_failed_or_cancelled_later_runs(request, backend, later_status):
    h = request.getfixturevalue(backend)
    w = h.shift(h.workspace('Synthetic supersession currency'), people=[])['workspace']
    older = run(h, w)
    newer = run(h, w)
    assert (currency(h, w, older), currency(h, w, newer)) == ('superseded', 'current')

    other = h.shift(h.workspace('Synthetic other rota'), people=[])['workspace']
    other_run = run(h, other)
    assert currency(h, other, other_run) == 'current'

    independent = h.shift(h.workspace('Synthetic cancelled later run'), people=[])['workspace']
    succeeded = run(h, independent)
    cancelled = h.command('coordinator', 'job.create',
        {'workspace_id': independent['id'], 'kind': 'generation'},
        revision=h.detail(independent)['workspace']['revision'])['job']
    if later_status == 'cancelled':
        h.command('coordinator', 'job.cancel', {'reason': 'Cancel this later run'}, cancelled['id'], cancelled['revision'])
    else:
        with transaction(h.app.state.engine, write=True) as db:
            row = db.get(m.Job, cancelled['id'])
            row.status, row.completed_at = 'failed', m.now()
    assert currency(h, independent, succeeded) == 'current'


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('newer_status', ['queued', 'running'])
def test_queued_or_running_newer_run_supersedes_immediately(request, backend, newer_status):
    h = request.getfixturevalue(backend)
    w = h.shift(h.workspace('Synthetic live supersession'), people=[])['workspace']
    older = run(h, w)
    newer = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'},
                      revision=h.detail(w)['workspace']['revision'])['job']
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, newer['id']).supersedes is not None
    if newer_status == 'running':
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Job, newer['id']).status = 'running'
    assert currency(h, w, older) == 'superseded'


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('newer_status', ['queued', 'succeeded'])
def test_continue_refuses_superseded_needs_more_time_run(request, backend, newer_status):
    h = request.getfixturevalue(backend)
    w = h.shift(h.workspace('Synthetic superseded continuation'), people=[])['workspace']
    previous = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'},
                         revision=w['revision'])['job']
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, previous['id']).supersedes is not None
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, previous['id'])
        row.status, row.completed_at = 'succeeded', m.now()
        row.result = {'status': 'needs_more_time'}
        row.resume_state = {'key': 'old-run'}
        row.resume_checkpoint = {'hint': [1]}
    if newer_status == 'queued':
        newer = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'},
                          revision=h.detail(w)['workspace']['revision'])['job']
    else:
        newer = run(h, w)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, newer['id']).supersedes is not None
    refused = h.command('coordinator', 'job.continue',
                        {'reason': 'Continue the superseded run'}, previous['id'], status=409)
    assert refused['error']['code'] == 'RUN_SUPERSEDED'


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_supersession_housekeeping_rearms_after_an_empty_pass(request, backend, monkeypatch):
    from apps.worker import main as worker

    h = request.getfixturevalue(backend)
    engine = h.app.state.engine
    worker.fill_supersessions(engine)
    first_pass = worker.time.monotonic()
    w = h.shift(h.workspace('Synthetic supersession housekeeping'), people=[])['workspace']
    older = run(h, w)
    cancelled = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'},
                          revision=h.detail(w)['workspace']['revision'])['job']
    h.command('coordinator', 'job.cancel', {'reason': 'Cancel before lease'}, cancelled['id'], cancelled['revision'])
    h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'},
              revision=h.detail(w)['workspace']['revision'])
    with transaction(engine, write=True) as db:
        db.get(m.Job, cancelled['id']).supersedes = None
    monkeypatch.setattr(worker.time, 'monotonic', lambda: first_pass + 61)
    worker.fill_supersessions(engine)
    with transaction(engine) as db:
        assert older['id'] in db.get(m.Job, cancelled['id']).supersedes
    assert currency(h, w, older) == 'superseded'


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('newer_run', ['discarded', 'other_sessions'])
def test_only_a_live_newer_run_over_the_same_sessions_supersedes(request, backend, newer_run):
    h = request.getfixturevalue(backend)
    w = h.shift(h.workspace('Synthetic independent runs'), people=[])['workspace']
    w = h.shift(w, start='2026-10-06T08:00:00+03:00', end='2026-10-06T12:00:00+03:00', people=[])['workspace']
    first, second = h.snapshot(w)['focus_ids'][:2]
    if newer_run == 'discarded':
        older = run(h, w)
        newer = run(h, w)
        h.command('coordinator', 'job.dismiss', {'reason': 'Discard this proposal'}, newer['id'], newer['revision'])
    else:
        older = run(h, w, [first])
        run(h, w, [second])
    assert currency(h, w, older) == 'current'
    applied = apply(h, w, older)
    assert applied['reconciliation']['status'] == 'applied'


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('newer_group', ['other_population', 'same_population'])
def test_planning_groups_over_the_same_sessions_supersede_only_their_own_runs(request, backend, newer_group):
    h = request.getfixturevalue(backend)
    typ = h.configure('assignment_types', {'name': 'Two-person session (test)', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2, 'maximum': 3}]}})['record']
    w = h.shift(h.workspace('Synthetic planning groups'), typ=typ['id'], people=[])['workspace']
    first, second = h.seed['people'][:2]
    older = run(h, w, population=group(first))
    newer = run(h, w, population=group(first if newer_group == 'same_population' else second))
    assert {a['shift_id'] for a in older['result']['additions']} & {a['shift_id'] for a in newer['result']['additions']}
    before = h.snapshot(w)
    if newer_group == 'same_population':
        assert currency(h, w, older) == 'superseded'
        assert apply(h, w, older, status=409)['error']['code'] == 'RUN_SUPERSEDED'
        assert h.snapshot(w) == before
        return
    # The older group's run is not superseded by another group's newer run: it applies.
    assert currency(h, w, older) == 'current'
    assert apply(h, w, older)['reconciliation']['status'] == 'applied'
    assert {p['person_id'] for p in h.detail(w)['shifts'][0]['participants']} == {first}
    # The other group's run is never refused as superseded either; it is rechecked against the first group's write
    # (here its session changed, so it is retained as a content conflict without a write).
    assert currency(h, w, newer) != 'superseded'
    after = h.snapshot(w)
    second_apply = apply(h, w, newer)
    assert second_apply['proposal_retained'] and second_apply['reconciliation']['status'] == 'content_conflict'
    assert h.snapshot(w) == after


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_a_mandatory_rule_activated_after_the_run_blocks_the_write_it_would_break(request, backend):
    h = request.getfixturevalue(backend)
    w = h.shift(h.workspace('Synthetic rule after run'), people=[])['workspace']
    job = run(h, w)
    h.policy({'kind': 'max_duties', 'maximum': 0})  # mandatory: the proposed duty now breaks a hard limit
    before = h.snapshot(w)
    reply = apply(h, w, job)
    assert reply['proposal_retained'] and reply['reconciliation']['status'] == 'unresolved'
    assert reply['reconciliation']['validation_current']
    assert any(finding['code'] == 'MAX_DUTIES' and finding['severity'] == 'error'
               for placement in reply['reconciliation']['conflicting_placements']
               for finding in placement['findings'])
    assert reply['reconciliation']['addition_indexes'] == reply['reconciliation']['removal_indexes'] == []
    assert h.snapshot(w) == before
    assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['status'] == 'succeeded'


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_zero_limit_active_before_generation_prevents_a_conflicting_proposal(request, backend):
    h = request.getfixturevalue(backend)
    person = h.seed['people'][0]
    w = h.shift(h.workspace('Synthetic live zero guard'), people=[person])['workspace']
    w = h.shift(w, people=[], start='2026-10-05T08:00:00+03:00',
                end='2026-10-05T12:00:00+03:00')['workspace']
    h.policy({'kind': 'max_duties', 'maximum': 0})
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation',
        'generation_scope': group(person)}, revision=w['revision'])['job']
    assert process_one(h.app.state.engine)
    saved = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert saved['status'] == 'succeeded' and saved['result']['additions'] == []
    validation = h.lifecycle(w, 'validate')['validation']
    assert any(finding['code'] == 'MAX_DUTIES' and finding['severity'] == 'error'
               for finding in validation['findings'])


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_a_mandatory_minimum_activated_after_the_run_keeps_its_gap_an_error(request, backend):
    h = request.getfixturevalue(backend)
    w = h.shift(h.workspace('Synthetic minimum after run'), people=[])['workspace']
    job = run(h, w)
    h.policy({'kind': 'minimum_cover', 'minimum': 2})
    before = h.snapshot(w)
    reply = apply(h, w, job)
    assert reply['proposal_retained'] and reply['reconciliation']['status'] == 'revalidation_required'
    assert reply['reconciliation']['addition_indexes'] == reply['reconciliation']['removal_indexes'] == []
    assert h.snapshot(w) == before
    validation = h.detail(w)['workspace']['validation']
    assert not validation['valid']
    assert any(f['code'].endswith('COVERAGE_GAP') and f['severity'] == 'error' for f in validation['findings'])


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('change', ['retired', 'line_changed'])
def test_rules_changed_after_run_retain_even_a_safe_proposal(request, backend, change):
    from .test_rule_catalogue_api import approve, line, propose

    h = request.getfixturevalue(backend)
    rule_line = line(numbers={'max': {'value': 5}})
    policy = approve(h, propose(h, 'duties_per_week', [rule_line])['record'])
    w = h.shift(h.workspace('Synthetic changed rule'), people=[])['workspace']
    job = run(h, w)
    if change == 'retired':
        h.command('approver', 'policy.retire', {'reason': 'Retire synthetic duty limit'}, policy['id'], policy['revision'])
    else:
        proposal = propose(h, 'duties_per_week', [{**rule_line, 'numbers': {'max': {'value': 6}}}],
                           policy_id=policy['id'], revision=policy['revision'])['record']
        approve(h, proposal)
    before = h.snapshot(w)
    reply = apply(h, w, job)
    assert reply['proposal_retained'] and reply['reconciliation']['status'] == 'revalidation_required'
    assert reply['reconciliation']['addition_indexes'] == reply['reconciliation']['removal_indexes'] == []
    assert h.snapshot(w) == before
    assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['status'] == 'succeeded'


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('non_rule_change', [False, True])
def test_apply_checks_live_mandatory_rules_independently_of_rule_drift(request, backend, non_rule_change):
    from apps.api.input_changes import rules_changed, watermark
    from apps.api.snapshots import currency_hint, domain_snapshot

    h = request.getfixturevalue(backend)
    person = h.seed['people'][0]
    w = h.shift(h.workspace('Synthetic live mandatory guard'), people=[person])['workspace']
    w = h.shift(w, people=[], start='2026-10-05T08:00:00+03:00',
                end='2026-10-05T12:00:00+03:00')['workspace']
    # #797 ruling 19 Nov: every late mandatory rule, positive included, binds drafts
    # #797: generation reads live mandatory rules, so the rule must become active after the proposal exists
    job = run(h, w, population=group(person))
    assert len(job['result']['additions']) == 1
    cap = h.policy({'kind': 'max_duties', 'maximum': 1})
    # A legacy worker could record the live currency while compiling only captured rules.
    with transaction(h.app.state.engine, write=True) as db:
        stored = db.get(m.Job, job['id'])
        workspace = db.get(m.Workspace, w['id'])
        legacy = domain_snapshot(db, workspace, mandatory_rules=False)
        stored.input_fingerprint = legacy['fingerprint']
        stored.input_snapshot = {**stored.input_snapshot, 'fingerprint': stored.input_fingerprint,
            'input_manifest': legacy['input_manifest'], 'audit_watermark': watermark(db, workspace.department_id),
            'currency_hint': currency_hint(db, workspace, mandatory_rules=False)}
        stored.result = {**stored.result, 'input_fingerprint': stored.input_fingerprint}
    if non_rule_change:
        h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][-1],
            'kind': 'recorded_leave', 'start': '2026-10-04T00:00:00+03:00',
            'end': '2026-10-05T00:00:00+03:00', 'private_reason': 'Synthetic current person fact'})
    before = h.snapshot(w)
    reply = apply(h, w, job)
    assert reply['proposal_retained'] and reply['reconciliation']['status'] == 'unresolved'
    assert reply['reconciliation']['validation_current']
    assert not rules_changed(reply['reconciliation']['changed_facts'])
    assert reply['reconciliation']['addition_indexes'] == reply['reconciliation']['removal_indexes'] == []
    finding, = [finding for placement in reply['reconciliation']['conflicting_placements']
                for finding in placement['findings'] if finding['rule_id'] == cap['id']]
    assert (finding['code'], finding['person_id'], finding['detail']['maximum']) == ('MAX_DUTIES', person, 1)
    assert h.snapshot(w) == before


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('engine', ['legacy', 'cp_sat'])
def test_stricter_safety_rule_reaches_check_solver_apply_and_approval(request, backend, engine):
    from packages.domain.validation import evaluate

    h = request.getfixturevalue('cp_sat_engine' if engine == 'cp_sat' else backend)
    policy = h.policy({'kind': 'min_rest', 'hours': 8})
    person = h.seed['people'][0]
    draft = h.shift(h.workspace('Synthetic strict safety source'), people=[person])['workspace']
    draft = h.shift(draft, start='2026-10-04T22:00:00+03:00', end='2026-10-05T02:00:00+03:00', people=[])['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Policy, policy['id'])
        row.definition = {**row.definition, 'rule': {**row.definition['rule'], 'hours': 12}}
        row.revision += 1
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
    checked = h.snapshot(draft)
    assert next(row for row in checked['policies'] if row['id'] == policy['id'])['definition']['rule']['hours'] == 12
    first, second = sorted(checked['shifts'], key=lambda row: row['start'])
    second['participants'] = [{**first['participants'][0], 'start': second['start'], 'end': second['end']}]
    assert any(f['code'] == 'MIN_REST' and f['severity'] == 'error' for f in evaluate(checked)['findings'])
    job_id = h.command('coordinator', 'job.create', {'workspace_id': draft['id'], 'kind': 'generation'},
                       revision=draft['revision'])['job']['id']
    h.process_jobs()
    job = h.get('coordinator', '/api/jobs/' + job_id)['job']
    assert job['status'] == 'succeeded'
    with transaction(h.app.state.engine) as db:
        job_input = db.get(m.Job, job['id']).input_snapshot
        assert job_input['generation_engine'] == engine
        job_rule = next(row for row in job_input['policies'] if row['id'] == policy['id'])
    assert job_rule['definition']['rule']['hours'] == 12
    assigned, = [row['participant']['person_id'] for row in job['result']['additions']
                 if row['shift_id'] == second['id']]
    assert assigned != person
    applied = apply(h, draft, job)
    assert applied['reconciliation']['status'] == 'applied'
    published = h.publish(applied['workspace'])
    assert published['workspace']['status'] == 'published'


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_apply_rechecks_currency_even_when_semantic_fingerprint_is_unchanged(request, backend, monkeypatch):
    from apps.api.services import generation_reconciliation
    from packages.contracts.schemas import Command

    h = request.getfixturevalue(backend)
    w = h.shift(h.workspace('Synthetic apply currency race'), people=[])['workspace']
    job = run(h, w)
    client = h.client('coordinator')
    payload = {'workspace_id': w['id'], 'job_id': job['id'], 'reason': 'Recheck synthetic source currency'}
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
        plan = generation_reconciliation.prepare(db, actor, Command(kind='generation.apply', payload=payload,
                                                                     expected_revision=w['revision']))
    assert plan['receipt']['addition_indexes']
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
    monkeypatch.setattr(generation_reconciliation, 'prepare', lambda *args: plan)
    before = h.snapshot(w)
    assert before['fingerprint'] == plan['receipt']['current_fingerprint']
    reply = apply(h, w, job)
    assert reply['proposal_retained'] and reply['reconciliation']['status'] == 'revalidation_required'
    assert not reply['reconciliation']['validation_current']
    assert reply['reconciliation']['addition_indexes'] == reply['reconciliation']['removal_indexes'] == []
    assert h.snapshot(w) == before
