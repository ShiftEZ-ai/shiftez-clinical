"""Auto-schedule reports kept conflicts and fills chosen sessions without worsening them.

Problems elsewhere in the rota (a rest breach between kept duties, a kept week over its hours, open places in other
sessions) are reported with the result, the run cannot make them worse, and the validator still has the last word.
"""
from dataclasses import replace

from apps.api.auth import authenticate
from apps.api.db import transaction
from apps.api.services.jobs import create_job
from apps.worker.main import process_one
from packages.contracts.schemas import Command
from packages.domain.cp_sat import generate
from packages.domain.validation import evaluate


def _rota(h, rule):
    """Two kept duties on 4 Oct for person 0 (they break `rule`), person 1 kept there while on leave, and an open
    session on 10 Oct: the chosen scope."""
    people = h.seed['people']
    h.policy(rule)
    w = h.shift(h.workspace(), people=people[:2])['workspace']
    w = h.shift(w, start='2026-10-04T13:00:00+03:00', end='2026-10-04T17:00:00+03:00', people=people[:1])['workspace']
    w = h.shift(w, start='2026-10-10T08:00:00+03:00', end='2026-10-10T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    by_start = sorted(data['shifts'], key=lambda s: s['start'])
    for s in by_start[:2]:
        s['locked'] = True
    data['availability'].append({'id': 'kept-leave', 'person_id': people[1], 'kind': 'recorded_leave',
                                 'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-05T00:00:00+03:00'})
    return w, data, by_start


def test_problems_outside_the_chosen_sessions_are_reported_not_blocking(h):
    people = h.seed['people']
    _, data, (kept, _, open_session) = _rota(h, {'kind': 'min_rest', 'hours': 12})
    data['generation_session_ids'] = [open_session['id']]
    result = generate(data, 5)
    assert result['status'] == 'candidate_with_gaps', result.get('message')
    assert {i['code'] for i in result['outside_scope_issues']} == {'MIN_REST', 'UNAVAILABLE'}
    assert [a['shift_id'] for a in result['additions']] == [open_session['id']]
    # The validator is the final authority: the kept problems remain, nothing new was added.
    before = {(f['code'], f['shift_id'], f['person_id']) for f in evaluate(data)['findings'] if f['severity'] == 'error'}
    after = {(f['code'], f['shift_id'], f['person_id']) for f in result['validation']['findings'] if f['severity'] == 'error'}
    assert after <= before
    # The whole rota is the scope by default: kept problems remain visible and do not stop another session filling.
    del data['generation_session_ids']
    whole = generate(data, 5)
    assert whole['status'] == 'candidate_with_gaps'
    assert {i['code'] for i in whole['issues']} == {'MIN_REST', 'UNAVAILABLE'}
    assert [a['shift_id'] for a in whole['additions']] == [open_session['id']]
    assert not whole['removals']
    # A kept problem inside the chosen sessions is reported without blocking the open session.
    data['generation_session_ids'] = [kept['id'], open_session['id']]
    inside = generate(data, 5)
    assert inside['status'] == 'candidate_with_gaps'
    assert {i['person_id'] for i in inside['issues']} == {people[1]}
    assert [a['shift_id'] for a in inside['additions']] == [open_session['id']]
    assert not inside['removals']


def test_a_kept_breach_is_not_made_worse(h):
    """A person already over the week's hours in kept work is not given more; others fill the place."""
    people = h.seed['people']
    _, data, (_, _, open_session) = _rota(h, {'kind': 'max_hours', 'hours': 6, 'window': 'week'})
    data['generation_session_ids'] = [open_session['id']]
    result = generate(data, 5)
    assert result['status'] == 'candidate_with_gaps', result.get('message')
    assert 'MAX_HOURS' in {i['code'] for i in result['outside_scope_issues']}
    assert result['additions'] and all(a['participant']['person_id'] != people[0] for a in result['additions'])
    # Fixed context is raised to its existing workload bound before the no-worse pass; no constraint is loosened.
    assert result['loosened_constraints'] == 0


def test_a_kept_breach_behind_a_window_condition_is_not_made_worse(h):
    """Duty limits are enforced only when the window holds work the run decides (a derived flag); a kept breach there
    is found through that flag and loosened, so the run goes ahead without giving that person more."""
    people = h.seed['people']
    _, data, (_, _, open_session) = _rota(h, {'kind': 'max_duties', 'maximum': 1, 'window': 'rolling', 'window_days': 8})
    data['generation_session_ids'] = [open_session['id']]
    result = generate(data, 5)
    assert result['status'] == 'candidate_with_gaps', result.get('message')
    assert 'MAX_DUTIES' in {i['code'] for i in result['outside_scope_issues']}
    assert result['additions'] and all(a['participant']['person_id'] != people[0] for a in result['additions'])


def test_fixed_context_workload_breach_is_reported_but_boundary_stays_strict(h):
    """A pre-existing cap breach must not make a strict scoped probe impossible."""
    people = h.seed['people']
    _, data, (_, _, open_session) = _rota(h, {'kind': 'max_duties', 'maximum': 1, 'window': 'week'})
    data['availability'] = []
    data['generation_session_ids'] = [open_session['id']]
    result = generate(data, 5, feasibility_only=True)
    assert result['status'] == 'candidate_feasible', result
    assert 'loosened_constraints' not in result
    assert len(result['fixed_breaches']) == 1
    assert result['fixed_breaches'][0]['code'] == 'MAX_DUTIES'
    assert result['message'] == 'Feasible except 1 fixed breaches outside auto-schedule control.'
    assert result['summary']['message'] == result['message']
    assert any(f['code'] == 'MAX_DUTIES' for f in result.get('outside_scope_issues', []))
    assert result['additions'] and all(a['shift_id'] == open_session['id'] for a in result['additions'])
    assert all(a['participant']['person_id'] not in people[:2] for a in result['additions'])

    # If the only eligible person is already over the cap, the context link
    # remains binding and the probe must still report a real conflict.
    data['people'] = {people[0]: data['people'][people[0]]}
    blocked = generate(data, 5, feasibility_only=True)
    assert blocked['status'] == 'requirements_conflict', blocked


def test_understaffed_context_coverage_is_report_only(h):
    """A fixed context duty's own coverage requirement cannot block the chosen window."""
    _, data, (context, _, open_session) = _rota(h, {'kind': 'max_duties', 'maximum': 10, 'window': 'week'})
    context['workspace_id'] = 'published-context'
    context['type_snapshot']['definition']['requirements'] = [{
        'minimum': 2, 'target': None, 'maximum': None, 'from_minute': 0,
        'to_minute': None, 'capabilities': [], 'distinct_capability_seats': False,
    }]
    data['generation_session_ids'] = [open_session['id']]

    result = generate(data, 15, feasibility_only=True)

    assert result['status'] == 'candidate_feasible', result
    findings = [f for f in result.get('outside_scope_issues', []) if f.get('shift_id') == context['id']]
    assert findings and all(f.get('context') for f in findings)
    assert all('context, fixed' in f['message'] for f in findings)


def test_open_places_elsewhere_do_not_stop_a_scoped_run(h):
    w = h.shift(h.workspace(), people=[])['workspace']
    w = h.shift(w, start='2026-10-10T08:00:00+03:00', end='2026-10-10T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    later = max(data['shifts'], key=lambda s: s['start'])
    data['generation_session_ids'] = [later['id']]
    result = generate(data, 5)
    assert result['status'] == 'candidate_with_gaps', result.get('message')
    assert [a['shift_id'] for a in result['additions']] == [later['id']]
    assert 'outside_scope_issues' not in result  # an open place elsewhere is not a problem to report


def test_api_runs_and_applies_a_scoped_proposal_despite_problems_elsewhere(h):
    people = h.seed['people']
    h.policy({'kind': 'min_rest', 'hours': 12})
    w = h.shift(h.workspace(), people=people[:1])['workspace']
    w = h.shift(w, start='2026-10-04T13:00:00+03:00', end='2026-10-04T17:00:00+03:00', people=people[:1])['workspace']
    w = h.shift(w, start='2026-10-10T08:00:00+03:00', end='2026-10-10T12:00:00+03:00', people=[])['workspace']
    shifts = sorted(h.detail(w)['shifts'], key=lambda s: s['start'])
    assert not h.detail(w)['workspace']['validation']['valid']  # the kept rest breach
    client = h.client('coordinator')
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
        job = create_job(db, actor, Command(kind='job.create', expected_revision=w['revision'], payload={
            'workspace_id': w['id'], 'kind': 'generation', 'session_ids': [shifts[-1]['id']],
            'generation_options': {'mode': 'fill_gaps'}}), replace(h.settings, generation_engine='cp_sat'))['job']
    process_one(h.app.state.engine, settings=replace(h.settings, generation_engine='cp_sat'))
    result = h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']
    assert result['status'] == 'candidate_with_gaps', result.get('message')
    assert {i['code'] for i in result['outside_scope_issues']} == {'MIN_REST'}
    applied = h.command('coordinator', 'generation.apply', {'workspace_id': w['id'], 'job_id': job['id'],
        'reason': 'Fill the open session'}, revision=w['revision'])
    codes = [f['code'] for f in applied['validation']['findings'] if f['severity'] == 'error']
    assert codes == ['MIN_REST']  # only the kept problem; the open place is filled
