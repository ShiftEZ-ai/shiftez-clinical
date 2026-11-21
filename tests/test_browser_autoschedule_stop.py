"""Auto-schedule stop and resume (run card, D76): Stop when sits in Setup › Change as one line while the department's
conditions hold and a run sends stop options only when they differ; a run that stopped with a saved checkpoint offers
Continue (job.continue with the minutes chosen); why a search stopped is said once, in the solver receipt, in the contract's words
(GENERATION_STOP_REASONS); a run continued past a rule conflict says its breaks are must-fixes; a rules conflict says
where and when each cause is, offers each cause's smallest fix, and two choices (nothing goes on by itself). Results are real stored results
written by the worker (process_one with a scheduler), never browser edits; the server's own continue, conflict and
stop logic is tested in tests/test_autoschedule_stop_conditions.py and test_autoschedule_conflict_policy.py."""
import re

import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction
from apps.worker.main import process_one
from packages.contracts.schemas import GENERATION_CONFLICT_CHOICES, GENERATION_STOP_REASONS
from .test_browser import auto_setup, open_rota, open_scheduling, wait_run_created, browser, live_server  # noqa: F401
from .test_browser_cp_sat import cp_server  # noqa: F401
from .test_browser_autoschedule_review import _open, FOUR, TWO

pytestmark = pytest.mark.browser


def _commands(page, kind):
    return page.expect_request(lambda r: r.url.endswith('/api/commands') and (r.post_data_json or {}).get('kind') == kind)


def _stored(h, w, make, checkpoint=False):
    """A generation run created through the API and finished by the worker with the result `make(snapshot)` builds."""
    h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'}, revision=h.detail(w)['workspace']['revision'])
    assert process_one(h.app.state.engine, scheduler=lambda snapshot, **options: {
        'algorithm_version': snapshot['compiler_version'], 'input_fingerprint': snapshot['fingerprint'],
        'additions': [], 'removals': [], **make(snapshot)})
    if checkpoint:
        job_id = h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs'][0]['id']
        with transaction(h.app.state.engine, write=True) as db:
            row = db.get(m.Job, job_id)
            row.resume_state = {'hint': [0]}
            row.resume_checkpoint = {'key': 'private-checkpoint', 'hint': [0], 'has_solution': True}
            row.result = {**row.result, 'resume_available': True, 'checkpointed': True}
    return h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs'][0]


def _stopped(reason, **extra):
    """A best-so-far schedule kept with a checkpoint after the search stopped for `reason` (not itself reviewable)."""
    return lambda snapshot: {'status': 'candidate_with_gaps', 'resume_available': True, 'stop_reason': reason,
                             'search_stop': {'reason': reason, 'search_seconds': 42, 'detail': ''}, **extra}


def _clean(h, w):
    for job in h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs']:
        if job['status'] in ('queued', 'running'):  # left for no later test's worker
            h.command('coordinator', 'job.cancel', {'reason': 'Browser test clean-up'}, job['id'], job['revision'])


@pytest.mark.parametrize('view,scheme', FOUR)
def test_stop_when_is_one_line_until_changed_and_a_run_sends_only_a_change(browser, cp_server, h, view, scheme):
    w = h.shift(h.workspace(f"Stop when {view['width']} {scheme}"), people=[])['workspace']
    context, page, panel = _open(browser, cp_server, h, w, view, scheme)
    try:
        auto_setup(panel)
        stops = panel.get_by_role('group', name='Stop when')
        line = stops.locator('.rl-done .grow')
        toggle = stops.get_by_role('button', name='Change stop conditions')
        score = stops.get_by_role('spinbutton', name='Stop at score', exact=True)
        # The department's conditions: one line, the conditions closed under it.
        expect(line).to_have_text(re.compile(r'^After \d+ min, or \d+ s without improvement$'))
        expect(toggle).to_have_attribute('aria-expanded', 'false')
        expect(score).to_be_hidden()
        # Run with them: the run sends none of its own (the department's are the server's).
        with _commands(page, 'job.create') as sent:
            panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
        assert 'generation_options' not in sent.value.post_data_json['payload']
        wait_run_created(page)
        job = h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs'][0]
        h.command('coordinator', 'job.cancel', {'reason': 'Browser test: next run'}, job['id'], job['revision'])
        expect(panel.get_by_text('Auto-schedule cancelled', exact=True)).to_be_visible(timeout=15000)

        # Changed for this run: the line says it, the conditions stay open (no way to hide a changed one).
        auto_setup(panel)
        toggle.click()
        expect(toggle).to_have_attribute('aria-expanded', 'true')
        for name in ('Stop after no improvement', 'Stop when within (%)', 'Stop at score', 'Stop by'):
            expect(stops.get_by_label(name, exact=True)).to_be_visible()
        expect(stops.get_by_label('If rules conflict', exact=True)).to_have_value('report')
        score.fill('90')
        expect(line).to_have_text(re.compile(r', or at score 90$'))
        expect(toggle).to_be_hidden()
        # Stop by: a time already past today is refused before anything is sent.
        clock = stops.get_by_label('Stop by', exact=True)
        clock.fill('00:00')
        panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
        assert not clock.evaluate('(input) => input.checkValidity()')
        assert h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs'][0]['id'] == job['id']
        clock.fill('')
        # Continue with fewest breaks warns that every break is a must-fix.
        stops.get_by_label('If rules conflict', exact=True).select_option('continue_flagged')
        expect(stops.get_by_text('Every break is a must-fix and blocks publishing until fixed.')).to_be_visible()
        expect(line).to_contain_text(GENERATION_CONFLICT_CHOICES['continue_flagged'].lower())
        # The setup's one line says the changed conditions once the fields close.
        panel.locator('.auto-body > .rl-done').get_by_role('button', name='Done', exact=True).click()
        expect(panel.locator('.auto-body > .rl-done')).to_contain_text('stops after')
        with _commands(page, 'job.create') as sent:
            panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
        options = sent.value.post_data_json['payload']['generation_options']
        assert options['target_score'] == 90 and options['on_conflict'] == 'continue_flagged'
        assert options['target_gap_percent'] is None and options['stop_by'] is None
        wait_run_created(page)
    finally:
        context.close()
        _clean(h, w)


@pytest.mark.parametrize('view,scheme', TWO)
def test_a_stopped_run_with_a_checkpoint_continues_for_the_minutes_chosen(browser, cp_server, h, view, scheme):
    w = h.shift(h.workspace(f"Resume {view['width']} {scheme}"), people=[])['workspace']
    job = _stored(h, w, _stopped('operator_stop'), checkpoint=True)
    context, page, panel = _open(browser, cp_server, h, w, view, scheme)
    try:
        card = panel.get_by_role('region', name='Best found before it stopped')
        expect(card).to_be_visible()
        expect(panel.locator('.solver-receipt')).to_contain_text('Stop reason: ' + GENERATION_STOP_REASONS['operator_stop'])
        expect(panel.get_by_role('button', name='Review latest result', exact=True)).to_be_visible()  # its review
        more = card.get_by_role('textbox', name='More search time', exact=True)
        more.fill('4')
        go = panel.get_by_role('button', name='Continue for 4 min', exact=True)
        expect(go).to_have_class(re.compile(r'\bprimary\b'))  # not reviewable here: Continue is the footer's move
        go.click()
        dialog = page.get_by_role('dialog', name='Continue auto-schedule', exact=True)
        dialog.get_by_label('Reason', exact=True).fill('More time for the stopped run')
        with _commands(page, 'job.continue') as sent:
            dialog.get_by_role('button', name='Continue', exact=True).click()
        # The run that stopped, with the minutes chosen (this stored result has no real checkpoint: the server's answer
        # is the API tests'; here, what the card sends).
        assert sent.value.post_data_json['target_id'] == job['id']
        assert sent.value.post_data_json['payload']['minutes'] == 4
        assert sent.value.post_data_json['payload']['reason'] == 'More time for the stopped run'
    finally:
        context.close()
        _clean(h, w)


@pytest.mark.parametrize('view,scheme', TWO)
def test_why_a_search_stopped_is_said_in_the_contract_words(browser, cp_server, h, view, scheme):
    """Every reason a resumable stop can have (a proven best is said by its proposal; cancelled and conflict runs keep
    no best-so-far card)."""
    w = h.shift(h.workspace(f"Stop reasons {view['width']} {scheme}"), people=[])['workspace']
    context, page, panel = _open(browser, cp_server, h, w, view, scheme)
    try:
        # A proven model cannot improve with more search, so it offers no best-so-far card (#34).
        for reason in sorted(set(GENERATION_STOP_REASONS) - {'optimal', 'model_proven', 'cancelled', 'conflict'}):
            _stored(h, w, _stopped(reason), checkpoint=True)
            page.reload()
            open_scheduling(page)
            open_rota(page, w['id'])
            expect(panel.get_by_role('region', name='Best found before it stopped')).to_be_visible()
            expect(panel.locator('.solver-receipt')).to_have_count(1)  # said once
            expect(panel.locator('.solver-receipt')).to_contain_text('Stop reason: ' + GENERATION_STOP_REASONS[reason])
    finally:
        context.close()


@pytest.mark.parametrize('view,scheme', TWO)
def test_a_run_continued_past_a_conflict_says_its_breaks(browser, cp_server, h, view, scheme):
    w = h.shift(h.workspace(f"Breaks {view['width']} {scheme}"), people=[])['workspace']
    shift = h.detail(w)['shifts'][0]['id']
    _stored(h, w, lambda snapshot: {'status': 'candidate_with_gaps', 'on_conflict': 'continue_flagged',
        'breaks': [{'rule_id': None, 'session_id': shift, 'person_id': None}, {'rule_id': None, 'session_id': shift, 'person_id': None}],
        'search_stop': {'reason': 'plateau', 'search_seconds': 12, 'detail': ''},
        'validation': {'valid': False, 'error_count': 2, 'warning_count': 0, 'findings': [], 'coverage': []}, 'summary': {}})
    context, page, panel = _open(browser, cp_server, h, w, view, scheme)
    try:
        review = panel.get_by_role('region', name='Review')
        expect(review.locator('.auto-breaks')).to_have_text('Continued with 2 breaks · each must be fixed before publishing')
        expect(panel.locator('.solver-receipt')).to_have_count(1)  # the run card's, not again in its review
        expect(panel.locator('.solver-receipt')).to_contain_text('Stop reason: ' + GENERATION_STOP_REASONS['plateau'])
    finally:
        context.close()


@pytest.mark.parametrize('view,scheme', TWO)
def test_a_rules_conflict_offers_each_cause_its_smallest_fix_and_two_choices(browser, cp_server, h, view, scheme):
    one_a_day = h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'day'}, exception=True)['id']
    cap = h.policy({'kind': 'max_duties', 'maximum': 3, 'window': 'week'})['id']
    rest = h.policy({'kind': 'min_rest', 'hours': 11})['id']
    person = h.seed['people'][0]
    w = h.shift(h.workspace(f"Conflict {view['width']} {scheme}"), people=[])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    shifts = sorted(h.detail(w)['shifts'], key=lambda s: s['start'])
    first, second = shifts[0], shifts[1]
    day = lambda s: s['start'][:10]

    def decision(kind, rule=None, s=first, **extra):
        return {'kind': kind, 'rule_id': rule, 'person_id': person if kind == 'person_exception' else None,
                'person_ids': [person] if kind in ('person_exception', 'propose_cap_change') else [], 'session_ids': [s['id']],
                'starts_on': day(s), 'ends_on': day(s), 'requires_approval': kind != 'leave_open', **extra}

    def cause(key, message, rules, decisions, s):
        return {'key': key, 'message': message, 'requirements': [{'kind': 'policy', 'rule_id': r, 'name': n, 'safety': safe} for r, n, safe in rules],
                'evidence': {'complete': True, 'required_places': 2, 'maximum_possible': 1, 'message': ''}, 'decisions': decisions,
                'session_ids': [s['id']], 'service_ids': s.get('service_ids') or [], 'starts_on': day(s), 'ends_on': day(s)}
    conflict = {'strict_status': 'INFEASIBLE', 'explanation_status': 'INFEASIBLE', 'proven': True, 'message': 'These rules cannot all be met.',
        'causes': [cause('a', 'One duty a day leaves the clinic short', [(one_a_day, 'One duty a day', False), (cap, 'Weekly cap', False)],
                         [decision('person_exception', one_a_day), decision('leave_open'), decision('open_rule', one_a_day)], first),
                   cause('b', 'The weekly cap leaves Monday short', [(cap, 'Weekly cap', False)], [decision('propose_cap_change', cap, second)], second),
                   cause('c', 'Rest between duties leaves Monday short', [(rest, 'Minimum rest', True)],
                         [decision('propose_cap_change', rest, second), decision('leave_open', s=second)], second)]}
    job = _stored(h, w, lambda snapshot: {'status': 'requirements_conflict', 'on_conflict': 'report', 'stop_reason': 'conflict', 'resume_available': True,
        'search_stop': {'reason': 'conflict', 'search_seconds': 3, 'detail': conflict['message']}, 'conflict': conflict,
        'summary': {'open_places_count': 2, 'sessions_affected': 2}}, checkpoint=True)
    context, page, panel = _open(browser, cp_server, h, w, view, scheme)
    sent = []

    def refuse(route):  # what each choice would start, without starting it (the card stays to try the next)
        body = route.request.post_data_json or {}
        if body.get('kind') in ('job.create', 'job.continue'):
            sent.append(body['payload'])
            route.fulfill(status=503, json={'error': {'code': 'CATALOGUE_BUSY', 'message': 'The system is busy, so this change was not saved. Try again.'}})
        else:
            route.continue_()
    try:
        clash = panel.get_by_role('region', name='Clash')
        expect(clash).to_be_visible()
        # Where and when the first cause is; its own fixes: an exception for the person, and leaving the place open.
        expect(clash.locator('.clash-where')).to_contain_text(re.compile(r'\b' + str(int(day(first)[8:])) + r' Oct'))
        decisions = clash.locator('ul.decisions').first
        expect(decisions.get_by_role('button', name='Allow an exception…', exact=True)).to_be_visible()
        expect(decisions.get_by_role('button', name='Leave as open place', exact=True)).to_be_visible()
        # Each further cause: its smallest fix, one button; never a relax for a safety rule.
        causes = clash.locator('ul.clash-causes > li')
        expect(causes).to_have_count(2)
        expect(causes.nth(0)).to_contain_text('The weekly cap leaves Monday short')
        expect(causes.nth(0).get_by_role('button', name='Allow for this rota…', exact=True)).to_be_visible()
        expect(causes.nth(1).get_by_role('button', name='Allow for this rota…', exact=True)).to_have_count(0)
        expect(causes.nth(1).get_by_role('button', name='Leave as open place', exact=True)).to_be_visible()
        causes.nth(0).get_by_role('button', name='Allow for this rota…', exact=True).click()
        relax = page.get_by_role('dialog', name='Allow for this rota')
        expect(relax).to_be_visible()
        page.keyboard.press('Escape')
        expect(relax).to_have_count(0)

        # The two choices, nothing started by itself; Fill everything else (the primary) warns that every break blocks
        # publishing and continues this run (job.continue: the server flags every break).
        assert h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs'][0]['id'] == job['id']
        choices = panel.get_by_role('region', name='Rules conflict')
        expect(choices.get_by_role('button', name='Stop and fix rules', exact=True)).to_be_visible()
        expect(choices).to_contain_text('Every break is a must-fix and blocks publishing until fixed.')
        fill = choices.get_by_role('button', name='Fill everything else', exact=True)
        expect(fill).to_have_class(re.compile(r'\bprimary\b'))
        expect(panel.get_by_role('button', name='Fill everything else', exact=True)).to_have_count(1)  # one way on, not two
        expect(panel.get_by_role('button', name=GENERATION_CONFLICT_CHOICES['continue_flagged'], exact=True)).to_have_count(0)
        page.route(cp_server + '/api/commands', refuse)
        for press in (lambda: fill.click(),  # Leave as open place is the same way on, from its cause
                      lambda: panel.get_by_role('region', name='Clash').locator('ul.decisions').first.get_by_role('button', name='Leave as open place', exact=True).click()):
            with _commands(page, 'job.continue') as continued:
                press()  # no dialog: the press is the choice, recorded as the run's reason
            assert continued.value.post_data_json['target_id'] == job['id']
            assert continued.value.post_data_json['payload'] == {'reason': 'Chosen: Fill everything else (flag every break)'}
            expect(page.get_by_role('dialog')).to_have_count(0)
            expect(panel.get_by_role('region', name='Rules conflict')).to_be_visible()  # the refused start leaves the card
        assert len(sent) == 2  # nothing started
        # Stop and fix rules opens this rota's rules beside the schedule.
        panel.get_by_role('region', name='Rules conflict').get_by_role('button', name='Stop and fix rules', exact=True).click()
        expect(page.get_by_role('dialog', name='Rules in this rota', exact=True)).to_be_visible()
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()
        _clean(h, w)
