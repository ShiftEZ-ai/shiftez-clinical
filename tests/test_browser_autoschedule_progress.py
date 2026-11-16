"""AUTOSCHED-UX: the Auto-schedule panel shows the search as it goes, from the job's own progress only (never simulated):
the stage, the time used of the run's allowance, the best score so far and its line, how many better schedules and when
the last came, "levelling off" once nothing better came within the run's own stop window, then the end (Done, or a run
that could not finish). The job list is the real API's, with a running job's progress written in by the test (the
worker's side is tested in the API's own tests). 1440 and 390, light and dark."""
import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from apps.worker.main import process_one
from packages.contracts.schemas import GenerationOptions
from .test_browser import browser, live_server, open_rota, sign_in, wait_run_created  # noqa: F401
from .test_browser_cp_sat import cp_server  # noqa: F401
from .test_browser_autoschedule_live import Stream, rota, STEP
from .test_browser_autoschedule_review import FOUR

SHOTS = os.getenv('AUTOSCHED_SHOTS')


def shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f'{name}.png'))

pytestmark = pytest.mark.browser


def _running(browser, server, h, view, scheme):
    a, b, _ = h.seed['people'][:3]
    w, ids = rota(h, [(4, 8, [a]), (5, 8, []), (6, 8, [])], name='Progress run')
    # A finished rota check first, so Recent runs lists the running run beside a finished one.
    h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'validation'}, revision=w['revision'])
    process_one(h.app.state.engine)
    w = h.detail(w)['workspace']
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'}, revision=w['revision'])['job']
    context, page = sign_in(browser, server, 'coordinator', viewport=view)
    page.emulate_media(color_scheme=scheme)
    stream = Stream(page, server, w['id'])
    open_rota(page, w['id'])
    panel = page.locator('#auto-panel')
    expect(panel).to_be_visible()  # a run going opens its panel
    return context, page, stream, panel, job, ids, b


def _search(stream, seconds, best=None, event=None):
    stream.progress = {'stage': 'improving', 'tier': 0, 'objective_names': ['coverage'], 'elapsed_seconds': seconds + 2,
                       'search_elapsed_seconds': seconds, 'first_solution_seconds': 3,
                       **({'best_score': {'total': best, 'components': {}}} if best is not None else {})}
    if event:
        stream.event(adds=[event])


@pytest.mark.parametrize('view,scheme', FOUR)
def test_the_panel_follows_the_search_to_a_plateau_and_done(browser, cp_server, h, view, scheme):
    context, page, stream, panel, job, (_, s_open, s_later), b = _running(browser, cp_server, h, view, scheme)
    # A run on the department's options carries none of its own: the panel uses the department's stop window.
    stall = (h.get('coordinator', '/api/jobs/' + job['id'])['job']['effective_options']
             or GenerationOptions().model_dump())['stop_without_improvement_seconds']
    try:
        stage, search = panel.locator('.auto-stage'), panel.locator('.auto-search')
        # Running, before the search: the step only; nothing to say about schedules yet.
        expect(stage).to_contain_text('2/5 Preparing')
        expect(search).to_have_count(0)

        # Improving (D76 run card): the best score big, when the better schedule came, then three numbers; the allowance
        # is the ceiling, said as such.
        _search(stream, 12, best=70.0, event=(s_open, b))
        expect(search.locator('.auto-best')).to_have_text('70best score so far')
        expect(search.locator('.auto-last')).to_have_text('First schedule after 3 s · last better at 12 s')
        facts = search.locator('.auto-facts')
        expect(facts.locator('div')).to_have_text(['Open places0', 'Better schedules1', f'Stops after no gain{stall} s'])
        # Times as people read them reviewed: used, and what is left of the allowance as a ceiling.
        expect(stage).to_have_text(re.compile(r'^3/5 Improving staffing cover · \d+% · 12 s used · up to 1 min 48 s left$'))
        _search(stream, 18, best=72.5, event=(s_later, b))
        expect(search.locator('.auto-best-num')).to_have_text('72.5')
        expect(search.locator('.auto-last')).to_have_text('First schedule after 3 s · last better at 18 s')
        expect(facts.locator('div').nth(1)).to_have_text('Better schedules2')
        expect(search.get_by_role('img', name='Best score so far: from 70 to 72.5')).to_be_visible()
        # Running: Stop and keep best (the primary, it keeps the best schedule) and more time once the search runs; Cancel
        # discards, the same-button confirm; each says what it does in its "?" reviewed.
        stop = panel.get_by_role('button', name='Stop and keep best', exact=True)
        expect(stop).to_have_class(re.compile(r'\bprimary\b'))
        expect(panel.get_by_role('button', name='About Stop and keep best', exact=True)).to_have_attribute(
            'data-tip', re.compile('keeps the best schedule found so far'))
        expect(panel.locator('.run-cancel')).to_have_accessible_name('Cancel auto-schedule')
        expect(panel.get_by_role('button', name='About Cancel', exact=True)).to_have_attribute('data-tip', re.compile('discards'))
        expect(panel.get_by_role('button', name=re.compile(r'^Add \d+ min$'))).to_be_visible()
        # A run still going is a Recent runs row with where it is, never Review; who it schedules is one short line.
        people = h.seed['people'][:3]
        stream.extra = {'generation_scope': {'conditions': {'match': 'all', 'conditions': [
            {'field': 'person_ids', 'op': 'any', 'values': people}]}, 'protection': 'keep_existing'}}
        stream.wait_reads(page)
        running_row = panel.locator('.run-list li').filter(has_text='Auto-schedule')
        scope = running_row.locator('.run-scope')
        expect(scope).to_have_text('3 people')  # never a wall of names reviewed
        expect(scope).to_have_attribute('data-tip', re.compile('^Person is any of '))  # the whole scope on demand
        stream.extra = {}
        expect(running_row.locator('.run-progress')).to_have_text('Improving · 20 s')
        expect(running_row.get_by_role('button', name=re.compile('^Review'))).to_have_count(0)
        expect(panel.locator('.run-list li').filter(has_text='Full rota check').get_by_role('button', name=re.compile('^Review'))).to_have_count(1)
        shot(page, f"run-card-running-{view['width']}-{scheme}")

        # Plateau: nothing better within the run's own stop window is said, never a guessed end time.
        _search(stream, 18 + stall + 2, best=72.5)
        expect(search.locator('.auto-last')).to_have_text('First schedule after 3 s · levelling off, none better since 18 s')
        # No room left under the department's ceiling: no Add time at all (never "Add 0 min").
        ceiling = (h.get('coordinator', '/api/me')['department'].get('generation_defaults') or {}).get(
            'max_search_minutes') or GenerationOptions().max_search_minutes
        stream.extra = {'search_control': {'minutes': ceiling}}
        stream.wait_reads(page)
        expect(panel.get_by_role('button', name=re.compile(r'^Add \d+ min$'))).to_have_count(0)
        expect(stop).to_be_visible()
        stream.extra = {}
        # Motion is never what tells it: the line is static text and an SVG with its numbers in its name.
        assert page.evaluate("() => document.querySelector('#auto-panel .auto-search').getAnimations({subtree:true}).length") == 0

        # Stopped: the real worker finishes the run; the search line gives way to the result.
        stream.on = False
        process_one(h.app.state.engine)
        review = panel.get_by_role('region', name='Review')
        expect(review).to_be_visible(timeout=20000)
        expect(search).to_have_count(0)
        expect(panel.get_by_role('progressbar')).to_have_count(0)
        # The result (D76): one sentence, four numbers, then Discard and Apply.
        expect(review.get_by_role('group', name='Result').locator('.auto-stat small')).to_have_text(
            ['Must fix', 'Could improve', 'Open places', re.compile('^(Score|Changes)$')])
        shot(page, f"run-card-result-{view['width']}-{scheme}")
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def _broken(snapshot, **options):
    raise RuntimeError('solver crashed (test)')


@pytest.mark.parametrize('view,scheme', [FOUR[0], FOUR[3]])
def test_a_run_that_fails_says_so_and_keeps_the_rota(browser, cp_server, h, view, scheme):
    context, page, stream, panel, job, (_, s_open, _), b = _running(browser, cp_server, h, view, scheme)
    try:
        # An older worker sends no best score: the time used and left and the count still show, no number or line.
        _search(stream, 6, event=(s_open, b))
        expect(panel.locator('.auto-facts')).to_contain_text('Better schedules1')
        expect(panel.locator('.auto-stage')).to_contain_text('6 s used · up to 1 min 54 s left')
        expect(panel.locator('.auto-best')).to_have_count(0)
        expect(panel.locator('.auto-search .fx-spark')).to_have_count(0)
        stream.on = False
        process_one(h.app.state.engine, scheduler=_broken)
        result = panel.locator('.auto-outcome.err')
        expect(result).to_contain_text('Auto-schedule could not finish', timeout=20000)
        expect(result).to_contain_text('The rota is unchanged.')
        expect(panel.locator('.auto-search')).to_have_count(0)
        expect(page.locator('.app-footer .asl-strip ' + STEP)).to_have_count(0)  # no Done for a run that did not finish
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_undo_right_after_apply_says_only_undone(browser, cp_server, h):
    """Walk AUTOSCHED-UX: Undo pressed as soon as Apply's toast shows. The rota check Apply started is then out of date
    and refused; that refusal is not the person's lost change, so no "record changed … not saved" error."""
    w, _ = rota(h, [(5, 8, []), (6, 8, [])], name='Apply then undo')
    context, page = sign_in(browser, cp_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        open_rota(page, w['id'])
        page.get_by_role('button', name='Auto-schedule', exact=True).click()
        panel = page.locator('#auto-panel')
        panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
        expect(panel.get_by_role('progressbar')).to_be_visible()
        wait_run_created(page)
        process_one(h.app.state.engine)
        panel.get_by_role('button', name=re.compile(r'^Apply \d+ changes?$')).click()
        toast = page.locator('.toast').filter(has=page.get_by_role('button', name='Undo', exact=True))
        toast.get_by_role('button', name='Undo', exact=True).click()
        expect(page.locator('.toast').filter(has_text=re.compile('^Undone'))).to_be_visible(timeout=20000)
        expect(page.locator('.toast').filter(has_text='This record changed while you were working')).to_have_count(0)
        expect(page.locator('.app-footer')).not_to_contain_text('Not saved')
    finally:
        context.close()


@pytest.mark.parametrize('view,scheme', [FOUR[0], FOUR[3]])
def test_setup_is_one_card_and_a_queued_run_offers_only_cancel(browser, cp_server, h, view, scheme):
    """D76 (run card): no steps; setup is one line with Change, the priorities one segmented choice and the two search
    numbers; a queued run offers only Cancel (Stop and keep best and Add time wait for its search)."""
    w, _ = rota(h, [(5, 8, []), (6, 8, [])], name='Run card setup')
    context, page = sign_in(browser, cp_server, 'coordinator', viewport=view)
    page.emulate_media(color_scheme=scheme)
    try:
        open_rota(page, w['id'])
        page.get_by_role('button', name='Auto-schedule', exact=True).click()
        panel = page.locator('#auto-panel')
        expect(panel.locator('.step-rail')).to_have_count(0)
        expect(panel.locator('.rl-done')).to_have_count(1)
        presets = panel.get_by_role('radiogroup', name='Priorities')
        expect(presets).to_have_class(re.compile(r'\bsegmented\b'))
        expect(presets.get_by_role('radio', checked=True)).to_have_count(1)
        expect(panel.get_by_label('Search time', exact=True)).to_be_visible()
        expect(panel.get_by_label('Keep improving fairness', exact=True)).to_be_visible()
        expect(panel.locator('.auto-fill')).to_have_count(0)  # the dates, clinics and people open under Change
        expect(panel.get_by_role('button', name='Back to the result', exact=True)).to_have_count(0)  # never run: no result
        shot(page, f"run-card-setup-{view['width']}-{scheme}")
        panel.locator('.rl-done').get_by_role('button', name='Change', exact=True).click()
        expect(panel.locator('.auto-fill')).to_be_visible()
        presets.get_by_role('radio').nth(1).click()
        expect(presets.get_by_role('radio').nth(1)).to_have_attribute('aria-checked', 'true')

        panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
        wait_run_created(page)
        expect(panel.get_by_role('button', name='Cancel auto-schedule', exact=True)).to_be_visible()
        expect(panel.get_by_role('button', name='Stop and keep best', exact=True)).to_have_count(0)
        expect(panel.get_by_role('button', name=re.compile(r'^Add \d+ min$'))).to_have_count(0)
        shot(page, f"run-card-queued-{view['width']}-{scheme}")
    finally:
        context.close()


def test_a_rota_check_runs_in_the_footer_not_the_panel(browser, cp_server, h):
    """D76: a check queued while the panel is open is the footer's working item, with Cancel; the panel keeps its setup."""
    w, _ = rota(h, [(5, 8, [])], name='Check in footer')
    context, page = sign_in(browser, cp_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        open_rota(page, w['id'])
        page.get_by_role('button', name='Auto-schedule', exact=True).click()
        panel = page.locator('#auto-panel')
        panel.get_by_role('button', name='Check the whole rota', exact=True).click()
        footer = page.locator('.app-footer')
        # DL D-063, kept by DL-018 (Design Lead) + Master 10 Nov: one wording rule; a queued check says Waiting to check
        expect(footer.get_by_role('status').filter(has_text='Waiting to check')).to_be_visible()
        expect(footer.get_by_role('button', name='Cancel: waiting to check', exact=True)).to_be_visible()
        expect(panel.get_by_role('button', name='Cancel check', exact=True)).to_have_count(0)
        expect(panel.get_by_role('button', name='Run auto-schedule', exact=True)).to_be_visible()
        assert process_one(h.app.state.engine)
        expect(footer.get_by_role('status').filter(has_text=re.compile('Waiting to check|Checking the rota'))).to_have_count(0, timeout=20000)
    finally:
        context.close()
