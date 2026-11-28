"""Auto-schedule review says one honest thing at a time (e2e audit, 8 Oct 2026; since cycle 2 in Fix-list terms, mockups
A3/A4, owner decision D9): an infeasible run leads with the clash; the run's must-fix causes before and after, its open
places and its score change; what is still to fix as Fix list rows, each with its one action; the changes grouped by
the cause they work on; after Apply the run reads Applied with its Undo, after Undo it is a proposal to check again
(A4); no browser-only Accept; All checks without repeated pills. Coordinator, desktop 1440 and phone 390, light and
dark."""
import re
import pytest
from playwright.sync_api import expect
from apps.worker.main import process_one
from packages.contracts.schemas import AUTO_SCHEDULE_LIMITS, GENERATION_WEIGHT_PRESETS
from .test_browser import minutes_words, wait_run_created, browser, live_server, sign_in, navigate, open_scheduling, open_rota, route_fetch, auto_setup, auto_result
from .test_browser_cp_sat import cp_server, _review, _finish, _job, _still, _apply
from .browser_acceptance_support import shot
from .test_browser_disclosure import no_disclosure_markers
from .test_browser_person_card import shoot
from .test_browser_rule_catalogue import flag

pytestmark = pytest.mark.browser
DESKTOP, PHONE = {'width': 1440, 'height': 1000}, {'width': 390, 'height': 844}
FOUR = [pytest.param(DESKTOP, 'light', id='1440-light'), pytest.param(DESKTOP, 'dark', id='1440-dark'),
        pytest.param(PHONE, 'light', id='390-light'), pytest.param(PHONE, 'dark', id='390-dark')]
TWO = [pytest.param(DESKTOP, 'light', id='1440-light'), pytest.param(PHONE, 'dark', id='390-dark')]
# Operator screens are desktop-only: 1440 light and dark, and 1024 (the panel stacks under the schedule).
DESK = [pytest.param(DESKTOP, 'light', id='1440-light'), pytest.param(DESKTOP, 'dark', id='1440-dark'),
        pytest.param({'width': 1024, 'height': 900}, 'light', id='1024-light')]


def _open(browser, server, h, w, view, scheme):
    context, page = sign_in(browser, server, 'coordinator', viewport=view)
    page.emulate_media(color_scheme=scheme)
    navigate(page, 'planner')
    open_scheduling(page)
    open_rota(page, w['id'])
    return context, page, page.locator('#auto-panel')


def _run(page, h, panel):
    panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
    expect(panel.get_by_role('progressbar')).to_be_visible()
    wait_run_created(page)
    process_one(h.app.state.engine)


def _name(view, scheme):
    return f"{view['width']}-{scheme}"


def _undo(panel):
    """The applied run's own Undo in Recent runs (a rota check may be listed above it, BE-18)."""
    return panel.locator('.run-list').get_by_role('button', name=re.compile('^Undo Auto-schedule '))


@pytest.mark.parametrize('view,scheme', DESK)
def test_partial_one_run_says_what_needs_you_then_apply_and_undo(browser, cp_server, h, view, scheme):
    h.policy({'kind': 'restriction', 'weekdays': [6]})  # nobody may work Sundays
    w = h.shift(h.workspace('FM Sunday cannot fill'), people=[])['workspace']  # Sunday: nobody can work it
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']  # Monday: fine
    context, page, panel = _open(browser, cp_server, h, w, view, scheme)
    try:
        _run(page, h, panel)
        _finish(page, h)  # the person chooses Fill everything else when the run hit a conflict
        review = panel.get_by_role('region', name='Review')
        expect(review).to_be_visible(timeout=15000)
        # Outcome first, in plain words (A3: a proven clash leads), then exactly one primary action.
        status = review.get_by_role('status').first
        expect(status).to_have_text(re.compile(r'^These rules can’t all be met.*Everything else is filled\. 1 place stays open\.'))
        expect(panel.locator('.button.primary')).to_have_count(1)
        apply = panel.get_by_role('button', name='Apply 1 change', exact=True)
        expect(apply).to_be_visible()
        expect(panel.get_by_role('button', name='Fill everything else', exact=True)).to_have_count(0)
        if view['width'] == 1024:  # stacked under the schedule: most of the height, Apply above the status footer
            assert panel.evaluate('p => p.getBoundingClientRect().height') > 0.6 * view['height']
            footer = page.locator('.app-footer').evaluate('f => f.getBoundingClientRect().top')
            assert apply.evaluate('b => b.getBoundingClientRect().bottom') <= footer
        # The three numbers, each with its change from the rota now: must-fix causes, open places, changes.
        metrics = review.get_by_role('group', name='Result')
        expect(metrics.locator('.auto-stat').filter(has_text='Must fix')).to_have_text(re.compile(r'Must fix\s*1\s*−1 from 2'))
        expect(metrics.locator('.auto-stat').filter(has_text='Open places')).to_have_text(re.compile(r'Open places\s*1\s*−1 from 2'))
        expect(metrics.locator('.auto-stat').filter(has_text='Changes')).to_have_text(re.compile(r'Changes\s*1$'))
        # What is still to fix: the Sunday, ONE Fix list cause with its one fix (a safety kind: never acknowledged).
        still = _still(review)
        expect(still).to_have_count(1)
        job = _job(h, w)
        cause = next(c for c in job['result']['fix_list_after']['causes'] if c['level'] == 'must')
        expect(still.first).to_contain_text(cause['sentence'])
        expect(still.first.locator('.fix-tag')).to_have_text('Safety')
        expect(still.first.get_by_role('button', name='Who can work it', exact=True)).to_be_visible()
        # What changes: grouped by the cause each change works on, with its count.
        changes = review.get_by_role('region', name='Changes')
        expect(changes.get_by_role('heading')).to_have_text('1 change')
        expect(changes.locator('li')).to_have_count(len(job['result']['changes_by_cause']))
        # One press of Run is one run: the stopped run and its follow-on are not two rows (G-11).
        expect(panel.locator('.run-list')).to_have_count(0)
        expect(review).not_to_contain_text('Ruled out')
        expect(review).not_to_contain_text('Incomplete proposal')
        # The run's setup is one line with Change.
        expect(panel.locator('.rl-done')).to_have_count(1)
        shot(page, f'asr-partial-{_name(view, scheme)}', full_page=False)
        panel.get_by_role('button', name='Review latest result', exact=True).click()
        dialog = page.get_by_role('dialog', name='Auto-schedule results')
        expect(dialog.get_by_role('status').first).to_contain_text('These rules can’t all be met')
        expect(_still(dialog)).to_have_count(1)
        expect(dialog.get_by_role('button', name='Apply the change', exact=True)).to_be_visible()
        shot(page, f'asr-partial-dialog-{_name(view, scheme)}', full_page=False)
        page.keyboard.press('Escape')
        _apply(page, h, panel, 'Apply 1 change')
        expect(page.locator('.toast').filter(has_text='Suggestions applied')).to_be_visible()
        expect(panel.locator('.run-list li').filter(has_text='Auto-schedule')).to_contain_text('Applied')
        _undo(panel).click()
        expect(page.get_by_text('Undone.', exact=False)).to_be_visible()
        assert not any(s['participants'] for s in h.detail(w)['shifts'])
    finally:
        context.close()


@pytest.mark.parametrize('view,scheme', DESK)
def test_stored_partial_opened_later_applies_and_undoes(browser, cp_server, h, view, scheme):
    """A run that found a place nobody can fill, opened later (this panel did not start it): it waits at the clash, the
    person chooses Fill everything else, and the stored result says what is still to fix with one primary action; Apply, then Undo,
    keep the open place open and start no other run."""
    h.policy({'kind': 'restriction', 'weekdays': [6]})  # nobody may work Sundays
    w = h.shift(h.workspace('FM stored partial'), people=[])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']  # Monday: fine
    context, page, panel = _open(browser, cp_server, h, w, view, scheme)
    try:
        panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
        expect(panel.get_by_role('progressbar')).to_be_visible()
        wait_run_created(page)
        page.reload()  # a later visit
        process_one(h.app.state.engine)
        open_rota(page, w['id'])
        open_scheduling(page)
        _finish(page, h)  # the stored run waits at its clash for the person's Fill everything else (2850a3ee6)
        auto_result(panel)
        review = panel.get_by_role('region', name='Review')
        expect(review.get_by_role('status').first).to_contain_text('1 place stays open', timeout=15000)
        expect(_still(review)).to_have_count(1)
        apply = panel.get_by_role('button', name='Apply 1 change', exact=True)
        expect(apply).to_have_class(re.compile(r'\bprimary\b'))
        expect(panel.locator('.button.primary')).to_have_count(1)
        expect(panel.get_by_role('button', name='Fill everything else', exact=True)).to_have_count(0)
        shot(page, f'asr-stored-partial-{_name(view, scheme)}', full_page=False)
        _apply(page, h, panel, 'Apply 1 change')
        expect(panel.locator('.run-list li').filter(has_text='Auto-schedule')).to_contain_text('Applied')
        filled = {s['start'][:10]: bool(s['participants']) for s in h.detail(w)['shifts']}
        assert filled == {'2026-10-04': False, '2026-10-05': True}
        _undo(panel).click()
        expect(page.get_by_text('Undone.', exact=False)).to_be_visible()
        assert not any(s['participants'] for s in h.detail(w)['shifts'])
        expect(panel.get_by_role('progressbar')).to_have_count(0)
        jobs = h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs']
        assert [j['kind'] for j in jobs].count('generation') == 1  # Apply and Undo started no other run
    finally:
        context.close()


@pytest.mark.parametrize('view,scheme', TWO)
def test_locked_short_session_says_unlock_or_add_by_hand(browser, cp_server, h, view, scheme):
    pair = h.configure('assignment_types', {'name': 'FM locked pair', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2}]}})['record']['id']
    w = h.shift(h.workspace('FM locked session'), typ=pair)['workspace']  # one person of two
    locked = h.detail(w)['shifts'][0]
    w = h.command('coordinator', 'shift.action', {'workspace_id': w['id'], 'shift_ids': [locked['id']], 'action': 'lock',
                                                  'reason': 'Kept as agreed'}, revision=w['revision'])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    context, page, panel = _open(browser, cp_server, h, w, view, scheme)
    try:
        _run(page, h, panel)
        _finish(page, h)
        # The lock is the reason the place stays open: said once, with nothing locked changed, and the session itself
        # one press away (Show on schedule), where it is unlocked or someone is added by hand.
        expect(panel).to_contain_text('Locked sessions still have open places. The editable sessions have been scheduled; no locked assignments were changed.', timeout=15000)
        expect(panel.get_by_role('button', name='Show on schedule', exact=True)).to_be_visible()
        expect(_still(panel.get_by_role('region', name='Review'))).to_have_count(1)
        assert [p['person_id'] for p in next(s for s in h.detail(w)['shifts'] if s['id'] == locked['id'])['participants']] == \
            [p['person_id'] for p in locked['participants']]
        shot(page, f'after-locked-{_name(view, scheme)}', full_page=False)
    finally:
        context.close()


@pytest.mark.parametrize('view,scheme', DESK)
def test_success_ready_to_apply_one_score_apply_and_undo(browser, cp_server, h, view, scheme):
    w = h.shift(h.workspace('FM apply and undo'), start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    w = h.shift(w, start='2026-10-06T08:00:00+03:00', end='2026-10-06T12:00:00+03:00', people=[])['workspace']
    context, page, panel = _open(browser, cp_server, h, w, view, scheme)
    try:
        # Keep existing assignments is the department default (owner decision, 8 Oct 2026), and the run's only switch for it.
        auto_setup(panel)  # D76: the setup's fields open under Change
        expect(panel.get_by_label('Keep existing assignments', exact=True)).to_be_checked()
        _run(page, h, panel)
        review = panel.get_by_role('region', name='Review')
        expect(review).to_be_visible(timeout=15000)
        # A second run from the setup line: the first goes to Recent runs.
        panel.locator('.rl-done').get_by_role('button', name='Change', exact=True).click()
        _run(page, h, panel)
        expect(review).to_be_visible(timeout=15000)
        # Better or worse in must-fix causes, said once (G-31, A3).
        expect(review.get_by_role('status').first).to_have_text(re.compile(r'^Better: all 1 must-fix cause clearedReady to submit once applied\.$'))
        expect(review.get_by_role('region', name='Still to fix')).to_have_count(0)
        expect(review.get_by_role('note')).to_have_count(0)  # CP-SAT weighs wishes, patterns and fairness
        runs = panel.locator('.run-list')
        expect(runs).to_contain_text('Ready for review')
        # One score: the change the run makes to the rota's score (A7: a change between two versions is always valid).
        job = _job(h, w)
        after = job['result']['fix_list_after']['summary']['score']
        score = review.get_by_role('group', name='Result').locator('.auto-stat').filter(has_text='Score')
        expect(score).to_have_text(re.compile(r'^Score\s*\+' + re.escape(f"{job['result']['score_delta']:g}") + r'\s*vs now$'))
        expect(review.get_by_role('group', name='Result').locator('.auto-stat').filter(has_text='Open places')).to_contain_text('−2 from 2')
        expect(review.get_by_role('region', name='Changes').get_by_role('heading')).to_have_text('2 changes')
        # One primary action; Discard is a quiet link; reversibility on one line.
        expect(panel.locator('.button.primary')).to_have_count(1)
        expect(panel.get_by_role('button', name='Discard', exact=True)).to_have_class(re.compile(r'link-button'))
        hint = panel.locator('.auto-foot .hint')
        expect(hint).to_have_text('You can undo')
        assert hint.evaluate('e => { const r = document.createRange(); r.selectNodeContents(e); return r.getClientRects().length; }') == 1
        details = panel.get_by_role('button', name='Review latest result', exact=True)
        assert details.evaluate('b => getComputedStyle(b).borderTopWidth') != '0px'
        shot(page, f'asr-success-{_name(view, scheme)}', full_page=False)
        _apply(page, h, panel, 'Apply 2 changes')
        toast = page.locator('.toast').filter(has_text='Suggestions applied')
        expect(toast).to_be_visible()
        expect(panel).not_to_contain_text('to add')
        # One score: the run's score after it is the footer's once applied (and the stored rota's).
        stored = h.detail(w)['workspace']['validation']['score']['score']
        assert f'{stored:g}' == f'{after:g}', (stored, after)
        expect(page.locator('.app-footer').get_by_role('button', name=f'Ready to submit · score {after:g}', exact=True)).to_be_visible()  # A7
        # The applied run keeps Undo; the older run names the newer run that replaced it.
        rows = runs.locator('li').filter(has_text='Auto-schedule')
        expect(rows).to_have_count(2)
        expect(rows.nth(0)).to_contain_text('Applied')
        expect(rows.nth(0)).not_to_contain_text('Out of date')
        expect(rows.nth(1)).to_contain_text('Replaced by a newer run')
        expect(rows.nth(1).get_by_role('button', name=re.compile('^Undo'))).to_have_count(0)
        shot(page, f'asr-applied-{_name(view, scheme)}', full_page=False)
        _undo(panel).click()
        expect(page.get_by_text('Undone.', exact=False)).to_be_visible()
        assert not any(s['participants'] for s in h.detail(w)['shifts'])
        # Undone (A4, BE-1): the run is a proposal the rota changed under again. It says so and is applied only after a
        # check against the current rota; no counts of what was applied, and the older run stays replaced.
        expect(panel.locator('.auto-stale')).to_have_text('The rota changed since this run · Apply checks it against the current rota first')
        # The run's outcome is said as the run's, never that the rota is ready once applied.
        expect(review.locator('.auto-outcome').first).to_have_text('When it ran: all 1 must-fix cause cleared')
        expect(panel).not_to_contain_text('Ready to submit once applied')
        expect(panel).not_to_contain_text('added ·')
        expect(panel.get_by_role('button', name='Check again and apply', exact=True)).to_be_visible()
        expect(panel.get_by_role('button', name=re.compile(r'^Apply \d+ changes?$'))).to_have_count(0)
        expect(rows.nth(0)).to_contain_text('Check again to apply')
        expect(rows.nth(1)).to_contain_text('Replaced by a newer run')
    finally:
        context.close()


@pytest.mark.parametrize('view,scheme', DESK)
def test_nothing_to_do_and_a_run_that_could_not_finish(browser, cp_server, h, view, scheme):
    w = h.shift(h.workspace('FM already staffed'))['workspace']  # one person on the one session: nothing to change
    context, page, panel = _open(browser, cp_server, h, w, view, scheme)
    try:
        _run(page, h, panel)
        review = panel.get_by_role('region', name='Review')
        expect(review.get_by_role('status').first).to_have_text(re.compile(r'^Nothing to changeEvery place is filled and every mandatory rule is met\.'), timeout=15000)
        expect(panel.get_by_role('button', name=re.compile(r'^Apply'))).to_have_count(0)
        expect(panel.get_by_role('button', name='Run auto-schedule', exact=True)).to_be_visible()
        shot(page, f'asr-nothing-{_name(view, scheme)}', full_page=False)
        panel.locator('.rl-done').get_by_role('button', name='Change', exact=True).click()
        panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
        expect(panel.get_by_role('progressbar')).to_be_visible()  # drawn as soon as Run is pressed
        wait_run_created(page)  # the worker can only take the run once job.create has answered

        def broken(snapshot, **options):
            raise RuntimeError('synthetic scheduler failure')
        process_one(h.app.state.engine, scheduler=broken)
        failed = panel.locator('.auto-outcome.err')
        expect(failed).to_contain_text('Auto-schedule could not finish', timeout=15000)
        expect(panel.get_by_role('button', name=re.compile(r'^Apply'))).to_have_count(0)
        expect(panel.locator('.run-list')).to_contain_text('Could not finish')
        shot(page, f'asr-failed-{_name(view, scheme)}', full_page=False)
    finally:
        context.close()


def test_score_equals_insights_after_apply_and_publish(browser, cp_server, h):
    w = h.shift(h.workspace('FM score everywhere'), start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    context, page, panel = _open(browser, cp_server, h, w, DESKTOP, 'light')
    try:
        _review(page, h, w)
        after = _job(h, w)['result']['fix_list_after']['summary']['score']
        shown = f'{after:g}'
        _apply(page, h, panel, re.compile(r'^Apply \d+ changes?$'))
        expect(page.locator('.app-footer').get_by_role('button', name=f'Ready to submit · score {shown}', exact=True)).to_be_visible()  # A7
        h.publish(h.detail(w)['workspace'])
        navigate(page, 'insights/fairness')
        section = page.locator('[data-section=score]')
        expect(section).not_to_have_attribute('aria-busy', 'true', timeout=60_000)
        expect(section).to_contain_text(shown)
    finally:
        context.close()


@pytest.mark.parametrize('view,scheme', TWO)
def test_review_counts_places_numbers_and_week_ranges(browser, cp_server, h, view, scheme):
    typ = h.configure('assignment_types', {'name': 'FM review clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 10}]}})['record']['id']
    h.policy({'kind': 'max_hours', 'hours': 2, 'window': 'week'}, severity='advisory')
    w = h.shift(h.workspace('FM honest counts'), typ=typ, people=[])['workspace']
    w = h.shift(w, typ=typ, people=[])['workspace']  # same time: whoever works one is already working then
    context, page, panel = _open(browser, cp_server, h, w, view, scheme)
    try:
        review = _review(page, h, w)
        result = _job(h, w)['result']
        before, after = result['fix_list_before']['summary'], result['fix_list_after']['summary']
        # Places, not sessions (two sessions of ten): the open places before and after, from the run's own Fix lists.
        places = review.get_by_role('group', name='Result').locator('.auto-stat').filter(has_text='Open places')
        expect(places).to_have_text(re.compile(rf"^Open places\s*{after['places_open']}\s*−{before['places_open'] - after['places_open']} from {before['places_open']}$"))
        assert after['places_open'] > 2  # more places than sessions: never counted as sessions
        # The two short sessions are ONE cause (the same reason), counted once, not again as a problem each.
        still = _still(review)
        expect(still).to_have_count(1)  # the one next step (D76); the other must-fix causes are counted, in the Fix list
        rest = after['must'] - 1
        expect(review.get_by_role('button', name=re.compile(rf'^{rest} more causes? in the Fix list$'))).to_have_count(1 if rest else 0)
        expect(still.first).to_contain_text('2 sessions')
        expect(review).not_to_contain_text('problem')
        # Advisory notes never stop Apply and are not decided here: no Accept (the Fix list's All checks saves it).
        expect(review.get_by_role('button', name=re.compile('^Accept'))).to_have_count(0)
        shot(page, f'after-counts-{_name(view, scheme)}', full_page=False)
        panel.get_by_role('button', name='Review latest result', exact=True).click()
        dialog = page.get_by_role('dialog', name='Auto-schedule results')
        dialog.locator('details.auto-run-details > summary').click()  # the whole rota check is support detail
        expect(dialog.get_by_role('cell', name=re.compile(r'^\d+–\d+ Oct 2026$')).first).to_be_visible()
        expect(dialog).not_to_contain_text('00:00–00:00')
        shot(page, f'after-week-range-{_name(view, scheme)}', full_page=False)
    finally:
        context.close()


@pytest.mark.parametrize('view,scheme', TWO)
def test_run_priorities_link_to_the_fair_share_rule(browser, cp_server, h, view, scheme):
    flag(h, legacy=False)
    w = h.shift(h.workspace(), people=[])['workspace']
    context, page, panel = _open(browser, cp_server, h, w, view, scheme)
    try:
        priorities = panel.get_by_role('radiogroup', name='Priorities')
        expect(priorities).to_have_accessible_description('Fairness balances duties only where a Fair share rule applies.')
        link = panel.get_by_role('link', name='Fair share', exact=True)
        expect(link).to_be_visible()
        link.click()
        expect(page.locator('#main').get_by_role('heading', name='Fair share', exact=True)).to_be_visible()
    finally:
        context.close()


def test_run_priorities_are_presets_and_scoring_lives_in_configuration(browser, cp_server, h):
    w = h.shift(h.workspace('FM presets only'), people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'admin', viewport=DESKTOP)
    try:
        navigate(page, 'planner')
        open_scheduling(page)
        open_rota(page, w['id'])
        panel = page.locator('#auto-panel')
        expect(panel.get_by_role('radiogroup', name='Priorities').get_by_role('radio')).to_have_count(len(GENERATION_WEIGHT_PRESETS))
        minutes = panel.get_by_role('textbox', name='Search time', exact=True)
        defaults = h.get('admin', '/api/policies/capabilities')['generation_defaults']
        expect(minutes).to_be_visible()
        # A time in words (83ae3d14b durationField): its bounds are the contract's minimum and the department's maximum.
        chosen = minutes.input_value()
        minutes.fill(minutes_words(defaults['max_search_minutes'] + 1))
        expect(minutes).to_have_js_property('validationMessage', f"Between {minutes_words(AUTO_SCHEDULE_LIMITS['minimum_minutes'])} and {minutes_words(defaults['max_search_minutes'])}.")
        minutes.fill(chosen)
        expect(minutes).to_have_js_property('validationMessage', '')
        for label in ('Coverage weight', 'Low strength (×)', 'Catch-up for declined wishes (up to ×)', 'Maximum search time'):
            expect(panel.get_by_label(label, exact=True)).to_have_count(0)
        panel.get_by_role('radio', name='Fairness first', exact=True).click()
        auto_setup(panel)
        panel.get_by_label('Keep existing assignments', exact=True).click()  # a preset stays when Keep changes
        expect(panel.get_by_role('radio', name='Fairness first', exact=True)).to_be_checked()
        shot(page, 'after-priorities-presets-1440-light', full_page=False)
        panel.get_by_role('button', name='Change scoring settings', exact=True).click()
        main = page.locator('#main')
        # How it decides is the everyday tab (with the score weights); search time and its ceiling are under Timing,
        # advisory strengths under Advanced.
        expect(main.get_by_label('Fairness weight', exact=True)).to_be_visible()
        expect(main.get_by_label('Search time', exact=True)).to_be_hidden()
        main.get_by_role('tab', name='Timing', exact=True).click()
        for label in ('Search time', 'Maximum search time'):
            expect(main.get_by_label(label, exact=True)).to_be_visible()
        main.get_by_role('tab', name='Advanced', exact=True).click()
        expect(main.get_by_label('Low strength (×)', exact=True)).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize('view,scheme', TWO)
def test_resolve_issues_has_no_repeated_reason_pills(browser, live_server, h, view, scheme):
    w = h.shift(h.workspace('FM resolve clean'), people=[])['workspace']
    w = h.lifecycle(w, 'validate')['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', viewport=view)
    page.emulate_media(color_scheme=scheme)
    try:
        navigate(page, f'schedule/issues?draft={w["id"]}&view=checks')
        main = page.locator('#main')
        expect(main.get_by_role('heading', name=re.compile('^All checks'))).to_be_visible()
        expect(main.locator('.issues-counts')).to_contain_text('open')
        expect(main.get_by_role('list', name='Open issues by reason')).to_have_count(0)
        expect(main.locator('.issue-pills')).to_have_count(0)
        shot(page, f'after-resolve-{_name(view, scheme)}', full_page=False)
    finally:
        context.close()


@pytest.mark.parametrize('view,scheme', TWO)
def test_advisory_is_one_line_and_undoing_an_accept_keeps_the_proposal(browser, cp_server, h, view, scheme):
    person = h.seed['people'][0]
    h.policy({'kind': 'max_duties', 'maximum': 0, 'window': 'day'}, {'person_id': person}, severity='advisory')
    w = h.shift(h.workspace('FM accept once'), people=[person])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    w = h.lifecycle(h.detail(w)['workspace'], 'validate')['workspace']
    context, page, panel = _open(browser, cp_server, h, w, view, scheme)
    try:
        review = _review(page, h, w)
        # Advisory findings never stop Apply and are not repeated here (A3 lists what must be fixed); no Accept.
        expect(review.get_by_role('status').first).to_contain_text('Better: all 1 must-fix cause cleared')
        expect(review.get_by_role('region', name='Still to fix')).to_have_count(0)
        expect(review.get_by_role('button', name=re.compile('^Accept'))).to_have_count(0)
        # Accepting is saved in the Fix list's All checks; undoing only an Accept keeps the proposal current (#276).
        job = _job(h, w)
        could = [c for c in job['result']['fix_list_after']['causes'] if c['level'] == 'could']
        assert could  # the advisory finding is the run's Could improve
        issue = next(i for i in job['advisory_acceptance']['issues'] if i['can_accept'])
        h.command('coordinator', 'issues.resolve', {'workspace_id': w['id'], 'action': 'accept', 'keys': [issue['key']],
                                                    'reason': 'Accepted in Resolve issues'}, revision=job['advisory_acceptance']['workspace_revision'])
        h.command('coordinator', 'workspace.undo', {'reason': 'Undo the accept'}, w['id'], h.detail(w)['workspace']['revision'])
        assert not h.detail(w)['workspace']['accepted_issues']
        page.reload()
        open_rota(page, w['id'])
        open_scheduling(page)
        expect(panel.locator('.auto-stale')).to_have_count(0)
        expect(panel.locator('.auto-body')).to_have_attribute('data-state', 'run')
        expect(panel.get_by_role('button', name='Apply 1 change', exact=True)).to_be_enabled()
        shot(page, f'asr-advisory-{_name(view, scheme)}', full_page=False)
        # Where it is reviewed later: the rota's Fix list, Could improve.
        navigate(page, f"schedule/issues?draft={w['id']}")
        expect(page.locator('#main .fix-page')).to_contain_text(could[0]['sentence'])
    finally:
        context.close()


def test_derived_shortfalls_are_said_inside_the_one_coverage_gap(browser, live_server, h):
    typ = h.configure('assignment_types', {'name': 'FM desired cover', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'target': 3}]}})['record']['id']
    w = h.shift(h.workspace('FM one gap'), typ=typ, people=[])['workspace']
    w = h.lifecycle(w, 'validate')['workspace']
    codes = [f['code'] for f in w['validation']['findings']]
    assert 'COVERAGE_GAP' in codes and 'COVERAGE_TARGET' not in codes
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f'schedule/issues?draft={w["id"]}&view=checks')
        main = page.locator('#main')
        expect(main.locator('tbody tr[data-row]')).to_have_count(1)
        expect(main.locator('tbody tr[data-row]')).to_contain_text('Also below the preferred 3')
    finally:
        context.close()


def test_a_skill_shortage_says_who_can_fix_it(browser, cp_server, h):
    skill = h.configure('capabilities', {'name': 'Suturing', 'starts_on': '2020-01-01'})['record']['id']
    typ = h.configure('assignment_types', {'name': 'FM suturing clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'capabilities': [{'capability_id': skill, 'minimum': 1}]}]}})['record']['id']
    w = h.shift(h.workspace('FM nobody has the skill'), typ=typ, people=[])['workspace']
    context, page, panel = _open(browser, cp_server, h, w, DESKTOP, 'light')
    try:
        review = _review(page, h, w)
        # Someone can stand in the place, but nobody has the skill: it stays a must-fix cause (the run cleared none),
        # ONE row, and its fix is in People (where eligibility and skills are given; no extra authority here).
        must = review.get_by_role('group', name='Result').locator('.auto-stat').filter(has_text='Must fix')
        expect(must).to_have_text(re.compile(r'^Must fix\s*1\s*±0$'))
        still = _still(review)
        expect(still).to_have_count(1)
        cause = next(c for c in _job(h, w)['result']['fix_list_after']['causes'] if c['level'] == 'must')
        assert cause['action']['kind'] == 'open_eligibility'
        expect(still.first).to_contain_text(cause['sentence'])
        expect(still.first.get_by_role('button', name='Who can work it', exact=True)).to_be_visible()
    finally:
        context.close()


def test_each_rota_keeps_its_own_run_setup(browser, cp_server, h):
    a = h.shift(h.workspace('FM rota A'), people=[])['workspace']
    b = h.shift(h.workspace('FM rota B'), people=[])['workspace']
    context, page, panel = _open(browser, cp_server, h, a, DESKTOP, 'light')
    try:
        panel.get_by_role('radio', name='Fairness first', exact=True).click()
        panel.get_by_role('textbox', name='Search time', exact=True).fill('3')
        page.reload()
        open_rota(page, a['id'])
        open_scheduling(page)
        auto_setup(panel, fields=False)
        expect(panel.get_by_role('radio', name='Fairness first', exact=True)).to_be_checked()
        expect(panel.get_by_role('textbox', name='Search time', exact=True)).to_have_value('3 min')
        open_rota(page, b['id'])
        panel = page.locator('#auto-panel')
        auto_setup(panel, fields=False)
        expect(panel.get_by_role('radio', name='Department defaults', exact=True)).to_be_checked()
        open_rota(page, a['id'])
        panel = page.locator('#auto-panel')
        auto_setup(panel, fields=False)
        expect(panel.get_by_role('radio', name='Fairness first', exact=True)).to_be_checked()
    finally:
        context.close()


@pytest.mark.parametrize('view,scheme', [pytest.param(DESKTOP, 'light', id='1440-light'), pytest.param(DESKTOP, 'dark', id='1440-dark')])
def test_department_scale_result_stays_a_few_lines(browser, cp_server, h, view, scheme):
    """Department scale (a week of 520 new placements, 110 people taken off, 40 open places and 12 must-fix causes, the
    shape the owner saw on DEV): the panel says the run in a few lines, each must-fix cause one row and the changes as
    one row per cause they work on, never a list of names; the Results dialog lists every change."""
    h.policy({'kind': 'restriction', 'weekdays': [6]})
    w = h.shift(h.workspace('FM department scale'), people=[])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    sunday = h.detail(w)['shifts'][0]
    context, page, panel = _open(browser, cp_server, h, w, view, scheme)
    try:
        _review(page, h, w)
        latest = h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs'][0]
        people = h.seed['people']

        def inflate(route):
            response = route_fetch(route)
            body = response.json()
            r = body['job']['result']
            add = r['additions'][0]
            r['additions'] = [{**add, 'participant': {**add['participant'], 'person_id': people[i % len(people)]}} for i in range(520)]
            r['removals'] = [{'shift_id': sunday['id'], 'assignment_id': f'synthetic-{i}', 'person_id': people[i % len(people)],
                              'start': sunday['start'], 'end': sunday['end'], 'session_label': add['session_label'], 'service_names': add['service_names'],
                              'reason': 'Known unavailability overlaps the scheduled participation; no private reason is disclosed' if i % 10 == 0
                              else 'Rebalanced automatic proposal'} for i in range(110)]
            before, after = r['fix_list_before'], r['fix_list_after']
            template = next(c for c in after['causes'] if c['level'] == 'must')
            after['causes'] = [{**template, 'id': f'scale-{i}', 'sentence': f'Synthetic cause {i} needs cover'} for i in range(12)]
            after['summary'].update(must=12, places_open=40, places_total=2032, places_filled=1992)
            before['summary'].update(must=30, places_open=470, places_total=2032, places_filled=1562)
            r['open_places_before'], r['open_places_after'] = 470, 40
            r['changes_by_cause'] = [{'cause_id': None, 'sentence': f'Synthetic group {i}', 'level': 'must', 'changes': n}
                                     for i, n in enumerate((400, 120, 110))]
            route.fulfill(response=response, json=body)

        page.route('**/api/jobs/' + latest['id'], inflate)
        page.reload()
        open_rota(page, w['id'])
        open_scheduling(page)
        auto_result(panel)
        review = panel.get_by_role('region', name='Review')
        expect(review.get_by_role('group', name='Result').locator('.auto-stat').filter(has_text='Must fix')).to_have_text(
            re.compile(r'^Must fix\s*12\s*−18 from 30$'), timeout=15000)
        expect(review.get_by_role('group', name='Result').locator('.auto-stat').filter(has_text='Open places')).to_have_text(
            re.compile(r'^Open places\s*40\s*−430 from 470$'))
        expect(panel.get_by_role('button', name='Apply 630 changes', exact=True)).to_be_visible()
        # The header names the rota only; the outcome box owns the result (G-19).
        expect(panel.locator('.auto-head small')).to_have_text('FM department scale')
        # D76: the one next step is the top must-fix cause, the rest counted with the way to the Fix list; the changes
        # are one row per cause, with their counts.
        expect(_still(review)).to_have_count(1)
        expect(review.get_by_role('button', name='11 more causes in the Fix list', exact=True)).to_be_visible()
        changes = review.get_by_role('region', name='Changes')
        expect(changes.get_by_role('heading')).to_have_text('630 changes')
        expect(changes.locator('li')).to_have_count(3)
        expect(changes.locator('li .home-count')).to_have_text(['400', '120', '110'])
        # No run-on lists of names: nothing in the panel names the people moved.
        expect(review).not_to_contain_text('Rebalanced automatic proposal')
        assert review.locator('li').count() < 40  # a few lines for 630 changes: one row per cause, never per person
        shot(page, f'asr-scale-{_name(view, scheme)}', full_page=False)
        panel.get_by_role('button', name='Review latest result', exact=True).click()
        dialog = page.get_by_role('dialog', name='Auto-schedule results')
        expect(dialog.get_by_role('button', name='Apply all 630 changes', exact=True)).to_be_visible()
        expect(dialog).to_contain_text('520 new placements')
        shot(page, f'asr-scale-dialog-{_name(view, scheme)}', full_page=False)
    finally:
        context.close()


def test_assignment_hover_card_leads_with_facts_and_one_rules_line(browser, live_server, h):
    """SI-03/SI-04/SI-46/LIVE-40/SI-02/SI-09: a name's card says who, their hours and rest, and the rules in one line from the
    rota check, without reading the session's rules on hover; the score effect is read only after the pointer rests."""
    w = h.shift(h.workspace('FM hover facts'))['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    reads = []
    page.on('request', lambda r: reads.append(r.url) if re.search(r'/shifts/[^/]+/rules$|/api/planner/why$', r.url) else None)
    try:
        open_rota(page, w['id'])
        chips = page.locator('.pchip:not(.gap)')
        chips.first.click()
        session = page.locator('.wk-panel')
        expect(session).to_be_visible()
        session.get_by_role('button', name='Close panel', exact=True).click()
        reads.clear()
        chips.first.hover()
        card = page.get_by_role('tooltip')
        expect(card).to_contain_text('h this week')
        expect(card).to_contain_text('h this rota')
        expect(card.locator('.hc-line').filter(has_text=re.compile(r'^.?(Rules|\d+ broken rule)'))).to_have_count(1)
        expect(card).not_to_contain_text('(automatic)')
        expect(card).not_to_contain_text('Loading')
        assert card.locator('.hc-line').count() <= 8
        assert not [u for u in reads if u.endswith('/rules')], reads
        # The score effect arrives after a dwell, as its own list.
        expect(card.get_by_role('list', name='Why here')).to_contain_text('with them here')
        assert not [u for u in reads if u.endswith('/rules')], reads
    finally:
        context.close()


def test_basic_scheduler_proposal_says_what_it_did_not_weigh(browser, live_server, h):
    """The legacy greedy engine (ROTA_GENERATION_ENGINE unset) names the inputs it ignores, beside its proposal (audit F5)."""
    w = h.shift(h.workspace('FM greedy note'), start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    context, page, panel = _open(browser, live_server, h, w, DESKTOP, 'light')
    try:
        _run(page, h, panel)
        review = panel.get_by_role('region', name='Review')
        # AGENTS §2 (coordinator, D76 follow-up): what changes what the proposal means stays in sight, one line; the plain
        # explanation is one closed "How this was made", last, after the next step.
        expect(review.get_by_role('note')).to_have_text('Basic scheduler · wishes and most advisory goals are not weighed', timeout=15000)
        how = review.locator('details.auto-how')
        expect(how).to_have_count(1)
        expect(how).not_to_have_attribute('open', '')
        expect(how.get_by_role('list', name='How this was made')).to_be_hidden()
        assert review.evaluate("r => r.lastElementChild.matches('details.auto-how')")
        # A plain text control, never a disclosure arrow (owner, DESIGN.md › Disclosure).
        no_disclosure_markers(page)
        shoot(page, 'autoschedule-how-closed-1440-light')
        how.locator('summary').click()
        expect(how.get_by_role('list', name='How this was made')).to_contain_text('Made by the basic scheduler')
        no_disclosure_markers(page)
        shoot(page, 'autoschedule-how-open-1440-light')
        expect(how.get_by_role('list', name='How this was made')).to_contain_text('Dated wishes are not used to select assignments.')
        # The basic scheduler has no priorities: none are offered (A-03), and the score is never a bare dash.
        expect(panel.get_by_role('radiogroup', name='Priorities')).to_have_count(0)
        expect(panel.get_by_role('group', name='Result')).not_to_contain_text('–')
    finally:
        context.close()


def test_basic_scheduler_blocking_is_the_proposals_own_not_the_saved_rotas(browser, live_server, h):
    """A basic-scheduler proposal is counted on its own Fix list, not the saved rota's: filling the one empty place
    leaves nothing to fix (0, from the saved rota's 1)."""
    w = h.shift(h.workspace('FM greedy blocking'), start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    assert h.detail(w)['workspace']['validation']['score']['blocking_count'] >= 1  # the empty place blocks the saved rota
    context, page, panel = _open(browser, live_server, h, w, DESKTOP, 'light')
    try:
        _run(page, h, panel)
        result = panel.get_by_role('group', name='Result')
        expect(result).to_be_visible(timeout=15000)
        must = result.locator('.auto-stat').filter(has_text='Must fix')
        expect(must.locator('b')).to_have_text('0')
        expect(must).to_contain_text('−1 from 1')
        expect(must).not_to_have_class(re.compile(r'\berr\b'))
    finally:
        context.close()


@pytest.mark.parametrize('view,scheme', TWO)
def test_resolve_issues_offers_only_what_the_selection_allows(browser, live_server, h, view, scheme):
    """G-36: a mandatory open place offers Find someone… and Remove sessions, never Accept or Exception… that would
    then refuse. G-20: the counts line has no process text, the footer no legend of badges the page does not show, and
    the Rule column is hidden while no issue names a rule."""
    w = h.lifecycle(h.shift(h.workspace('FM issue actions'), people=[])['workspace'], 'validate')['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', viewport=view)
    page.emulate_media(color_scheme=scheme)
    try:
        navigate(page, f'schedule/issues?draft={w["id"]}&view=checks')
        main = page.locator('#main')
        expect(main.locator('.issues-counts')).to_have_text(re.compile(r'^\d+ open · 0 resolved$'))
        expect(page.locator('.app-footer')).not_to_contain_text('Mandatory')
        expect(page.locator('.app-footer')).not_to_contain_text('one Undo')
        if view['width'] > 400:
            expect(main.locator('.issues-list thead').get_by_role('button', name=re.compile('^Rule'))).to_have_count(0)
        main.locator('.issues-list').get_by_role('checkbox', name=re.compile('^Select ')).first.check()
        bar = page.get_by_role('toolbar', name='Bulk actions')
        expect(bar.get_by_role('button', name='Find someone…', exact=True)).to_be_visible()
        expect(bar.get_by_role('button', name='Accept', exact=True)).to_have_count(0)
        expect(bar.get_by_role('button', name='Exception…', exact=True)).to_have_count(0)
    finally:
        context.close()


def test_basic_scheduler_blocking_counts_a_place_left_open(browser, live_server, h):
    """A basic-scheduler proposal that leaves a place open (more people needed than the department has) still has it to
    fix: Must fix counts it from the proposal's own Fix list, and it is one row, not a row per open place."""
    typ = h.configure('assignment_types', {'name': 'FM crowd clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 60}]}})['record']['id']  # more people than the department has
    w = h.shift(h.workspace('FM greedy one open'), typ=typ, people=[])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    context, page, panel = _open(browser, live_server, h, w, DESKTOP, 'light')
    try:
        _run(page, h, panel)
        review = panel.get_by_role('region', name='Review')
        must = review.get_by_role('group', name='Result').locator('.auto-stat').filter(has_text='Must fix')
        expect(must.locator('b')).to_have_text('1', timeout=15000)
        expect(must).to_have_class(re.compile(r'\berr\b'))
        expect(_still(review)).to_have_count(1)
        assert _job(h, w)['result']['fix_list_after']['summary']['places_open'] > 1  # many places, one cause
    finally:
        context.close()


@pytest.mark.parametrize('view,scheme', TWO)
def test_one_press_is_one_run_row_and_a_queued_run_says_queued(browser, cp_server, h, view, scheme):
    """G-11: a run waiting to start reads "Queued" in Recent runs, never a raw status. G-19: the panel's subtitle is the
    rota, not a second copy of the result."""
    w = h.shift(h.workspace('FM queued label'), people=[])['workspace']
    h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'validation'}, revision=w['revision'])
    process_one(h.app.state.engine)
    w = h.detail(w)['workspace']
    h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'}, revision=w['revision'])
    context, page = sign_in(browser, cp_server, 'coordinator', viewport=view)
    page.emulate_media(color_scheme=scheme)
    panel = page.locator('#auto-panel')
    try:
        open_rota(page, w['id'])
        expect(panel).to_be_visible()  # a run going opens the panel
        expect(panel.locator('.run-list li').first.locator('.badge')).to_have_text('Queued')
        expect(panel.locator('.auto-head small')).to_have_text(re.compile(r'^FM queued label( · queued)?$'))
        process_one(h.app.state.engine)
        expect(panel.get_by_role('region', name='Review')).to_be_visible(timeout=15000)
        expect(panel.locator('.auto-head small')).to_have_text('FM queued label')
    finally:
        context.close()


def test_applied_run_says_why_undo_is_gone(browser, cp_server, h):
    """SR-27: after Apply, a later edit takes the run's Undo away; Recent runs says so on that run instead of dropping it
    silently, also when the rota check that followed Apply (BE-18) is listed above it."""
    w = h.shift(h.workspace('Undo gone after an edit'), start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    context, page, panel = _open(browser, cp_server, h, w, DESKTOP, 'light')
    try:
        _run(page, h, panel)
        _apply(page, h, panel, re.compile(r'^Apply \d+ changes?$'))
        expect(_undo(panel)).to_be_visible()
        h.shift(h.detail(w)['workspace'], start='2026-10-06T08:00:00+03:00', end='2026-10-06T12:00:00+03:00', people=[])
        open_rota(page, w['id'])
        auto_result(page.locator('#auto-panel'))  # Recent runs
        row = page.locator('#auto-panel .run-list li').filter(has_text='Auto-schedule')
        expect(row.locator('.run-no-undo')).to_have_text('Undo not available')
        expect(row.get_by_role('button', name=re.compile('^Undo '))).to_have_count(0)
    finally:
        context.close()


def test_basic_scheduler_says_what_it_does_not_weigh_before_the_run(browser, live_server, h):
    """RUL-E05: the basic scheduler's omissions are said on What to fill, before Run, not only in its result."""
    w = h.shift(h.workspace('FM greedy before run'), people=[])['workspace']
    context, page, panel = _open(browser, live_server, h, w, DESKTOP, 'light')
    try:
        auto_setup(panel)  # D76: the setup's fields open under Change
        expect(panel.locator('.auto-basic')).to_contain_text('not weighed: dated wishes, work patterns')
    finally:
        context.close()


def test_change_count_matches_change_rows(browser, cp_server, h):
    """LIVE-24: the changes' heading, its rows and Apply count the same thing (a person moved on one session is one
    change)."""
    w = h.shift(h.workspace('FM change count'), start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    context, page, panel = _open(browser, cp_server, h, w, DESKTOP, 'light')
    try:
        _run(page, h, panel)
        changes = panel.get_by_role('region', name='Changes')
        heading = changes.get_by_role('heading')
        expect(heading).to_have_text(re.compile(r'^\d+ changes?$'), timeout=15000)
        n = int(heading.inner_text().split(' ')[0])
        assert sum(int(x) for x in changes.locator('li .home-count').all_inner_texts()) == n
        expect(panel.get_by_role('button', name=f"Apply {n} change{'s' if n != 1 else ''}", exact=True)).to_be_visible()
    finally:
        context.close()


def test_discarded_proposal_stays_discarded_after_reload(browser, cp_server, h):
    """Discard is saved with the job, and reload opens setup without the discarded proposal."""
    w = h.shift(h.workspace('FM discard persists'), start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    context, page, panel = _open(browser, cp_server, h, w, DESKTOP, 'light')
    try:
        _run(page, h, panel)
        panel.get_by_role('button', name='Discard', exact=True).click()
        panel.get_by_role('button', name='Discard?', exact=True).click()  # job.dismiss cannot be undone: armed
        expect(panel).to_contain_text('Proposal discarded')
        page.reload()
        open_scheduling(page)
        auto = page.locator('#auto-panel')
        expect(auto.locator('.auto-body')).to_have_attribute('data-state', 'setup', timeout=15000)
        expect(auto.get_by_role('region', name='Review')).to_have_count(0)
        assert h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs'][0]['status'] == 'dismissed'
    finally:
        context.close()
