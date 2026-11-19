"""Real browser task coverage for saved layouts and coherent bulk impact previews."""
import contextlib
import re
import pytest
from playwright.sync_api import expect, Error as PlaywrightError
from .browser_acceptance_support import shot
from .test_browser import wait_held, session_action, browser, live_server, sign_in, navigate, save_dialog, open_scheduling, pick_view, session_row, open_session, planner_filter, filter_pick, toolbar, close_popover

from .test_browser import DRAFT_CHIP, draft_chip  # noqa: F401
pytestmark=pytest.mark.browser


def test_absent_participant_is_not_labelled_covered_and_closure_reason_is_visible(browser,live_server,h):
    w=h.shift(h.workspace('FM clinic absence review'),people=h.seed['people'][:2])['workspace']
    h.command('coordinator','availability.record',{'person_id':h.seed['people'][0],
        'start':'2026-10-04T08:00:00+03:00','end':'2026-10-04T12:00:00+03:00',
        'kind':'actual_absence','private_reason':'Private detail never shown'})
    h.lifecycle(w,'validate')
    context,page=sign_in(browser,live_server,'coordinator')
    try:
        navigate(page,'planner')
        row=session_row(page)  # the List view shows each session's status
        expect(row.get_by_text('Absent · needs cover',exact=True)).to_be_visible()  # SW3-C-02: says why, never Covered
        expect(row.get_by_text('Covered',exact=True)).to_have_count(0)
        open_session(page)
        details,dialog=page.locator('.wk-panel'),page.get_by_role('dialog')  # the session panel; the impact check is a dialog
        # The type and day (the default leave display, in the server's words: clashText), never the note.
        expect(details.get_by_text(re.compile(r'Other, Sun 4 Oct')).first).to_be_visible()
        expect(details.get_by_text('Private detail never shown',exact=True)).to_have_count(0)
        session_action(page, 'Prepare automatic cover')
        dialog.get_by_label('Change reason',exact=True).fill('Keep the FM clinic open with available staff')
        dialog.get_by_role('button',name='Calculate impact',exact=True).click()
        expect(dialog.get_by_text('Not saved yet.',exact=False)).to_be_visible(timeout=20000)
        save_dialog(dialog,'Apply previewed draft change')
        expect(session_row(page).get_by_text('Covered',exact=True)).to_be_visible()
        assert [p['person_id'] for p in h.detail(w)['shifts'][0]['participants']]==[h.seed['people'][1]]
        open_session(page)
        session_action(page, 'Close session')
        dialog.get_by_label('Closure reason',exact=True).fill('Reviewed test clinic closure')
        dialog.get_by_role('button',name='Calculate impact',exact=True).click()
        expect(dialog.get_by_text('Not saved yet.',exact=False)).to_be_visible(timeout=20000)
        save_dialog(dialog,'Apply previewed draft change')
        expect(session_row(page).get_by_text('Closed',exact=True)).to_be_visible()
        # The calendar grid shows the closed session with its reason.
        pick_view(page,'Week')
        expect(page.locator('.pchip.closed[data-tip="Reviewed test clinic closure"]')).to_be_visible()
        # QA-134: a screen reader hears the reason too, not only "Closed" (its visible word first: label in name).
        expect(page.get_by_role('button', name='Closed: Reviewed test clinic closure', exact=True)).to_be_visible()
    finally:
        context.close()


def test_idle_planner_poll_preserves_input_and_detects_real_changes(browser,live_server,h):
    h.shift(h.workspace('Stable polling'))
    context,page=sign_in(browser,live_server,'coordinator')
    try:
        navigate(page,'planner')
        before=page.locator('#main .planner-main').element_handle()  # the shell stays; the planner body is redrawn
        search=page.get_by_label('Search sessions',exact=True)
        search.fill('Unsubmitted search')
        # The search applies as you type (one redraw, from memory); the box keeps its text and focus.
        page.wait_for_function('(el) => !el.isConnected',arg=before)
        expect(search).to_have_value('Unsubmitted search')
        expect(search).to_be_focused()
        main=page.locator('#main .planner-main').element_handle()
        with page.expect_response(lambda r: '/api/pulse' in r.url,timeout=20000):
            # A live update check arrives while the search box has focus: the app's 10 s timer reads the pulse once a minute
            # with the stream live, and its first tick may already have run in real time, so step past the next minute's check.
            page.clock.run_for(71_000)
        assert main.evaluate('(el) => el.isConnected')
        expect(search).to_have_value('Unsubmitted search')
        expect(search).to_be_focused()
        # Real updates wait until typing finishes; permissions are checked separately.
        h.policy({'kind':'min_rest','hours':12})
        page.wait_for_timeout(6000)  # the change is pushed within a few seconds and held while typing
        assert main.evaluate('(el) => el.isConnected')
        expect(search).to_have_value('Unsubmitted search')
        search.fill('')
        page.locator('.wk-nav strong').click()
        page.wait_for_function('(el) => !el.isConnected',arg=main,timeout=25000)
        expect(page.locator('#main')).to_have_attribute('data-loaded-view','schedule')
    except Exception:
        print(page.locator('#main').inner_text())
        raise
    finally:
        context.close()


def test_saved_month_person_layout_and_bulk_impact_are_real_server_workflows(browser,live_server,h):
    w=h.shift(h.workspace('Planner tools browser source'))['workspace']
    w=h.shift(w,'2026-10-05T08:00:00+03:00','2026-10-05T12:00:00+03:00',people=[h.seed['people'][1]])['workspace']
    context,page=sign_in(browser,live_server,'coordinator')
    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    try:
        navigate(page,'planner')
        open_scheduling(page)
        pick_view(page,'People')
        expect(page.locator('.person-group').first).to_be_visible()
        # Month reads the month's days (health dots); sessions are filtered in the week grid.
        pick_view(page,'Month')
        expect(page.locator('.planner-toolbar .wk-nav strong')).to_have_text('October 2026')
        pick_view(page,'Week')
        expect(page.locator('.wk-grid')).to_be_visible()
        expect(page.locator('.wk-grid .wk-day')).to_have_count(7)
        filters=planner_filter(page)
        person = next(r['display_name'] for r in h.get('coordinator', '/api/catalogues')['people'] if r['id'] == h.seed['people'][0])
        filter_pick(filters, 'Person', person)
        # The filter popover stays open across the planner's redraw, keeping its choice.
        expect(page.locator('#main .wk-grid')).to_be_visible()
        expect(filters).to_be_visible()
        expect(filters.locator('.cond-line .pick-button').first).to_contain_text(person)
        expect(page.locator('.planner-toolbar').get_by_role('button',name=re.compile(r'^Filter: .+ \(1 active\)$'))).to_be_visible()
        close_popover(page)
        # The scheduling disclosure survives the same rerender used by polling; planner tools stay one menu away.
        pick_view(page,'Week')
        page.get_by_role('button',name='Rota tools',exact=True).click()
        expect(page.get_by_role('menu',name='Rota tools').get_by_role('menuitem',name='Add recurring sessions',exact=True)).to_be_visible()
        close_popover(page)
        expect(page.get_by_role('button',name='Check the whole rota',exact=True)).to_be_visible()
        pick_view(page,'Month')
        # Saved planner views use the same saved-views menu as every list.
        toolbar(page.locator('.planner-toolbar'),'Saved views').get_by_role('menuitem',name='New view…').click()
        form=page.get_by_role('dialog',name='New view');form.get_by_label('View name',exact=True).fill('My saved month and person')
        save_dialog(form,'Save view')
        saved=h.get('coordinator','/api/table-views?table=planner')['views'][0]['definition']['planner']
        assert saved['mode']=='week' and saved['range']=='month'  # the calendar layout over a month
        page.reload();navigate(page,'planner')
        # A saved view brings back its view and filters.
        pick_view(page,'My saved month and person')
        expect(page.get_by_role('button',name='Saved views: My saved month and person',exact=True)).to_be_visible()
        expect(page.locator('#schedule-view')).to_have_attribute('aria-label','Month view')
        pick_view(page,'Week')
        expect(planner_filter(page).locator('.cond-line .pick-button').first).to_contain_text(person)
        close_popover(page)
        expect(page.get_by_role('checkbox',name=re.compile('^Select '))).to_have_count(1)
        page.get_by_role('checkbox',name=re.compile('^Select ')).first.check()
        page.locator('#selection-bar').get_by_role('button',name='Close sessions',exact=True).click()
        form=page.get_by_role('dialog');form.get_by_label('Closure reason',exact=True).fill('Preview one exact cancellation')
        form.get_by_role('button',name='Calculate impact',exact=True).click()
        expect(form.get_by_text('Not saved yet.',exact=False)).to_be_visible()
        assert not any(s['cancelled'] for s in h.detail(w)['shifts'])
        save_dialog(form,'Apply previewed draft change')
        assert sum(s['cancelled'] for s in h.detail(w)['shifts'])==1
        assert h.detail(w)['workspace']['status']=='draft'
        # Only per-viewer display preferences and the sign-in page's remembered account (with its expiry timestamp) may be
        # stored; never rota, people or session data. (The phone notice's "Continue anyway" pages are one:
        # tests/test_browser.py seeds it; sign-out removes it. The shared picker's recently chosen ids, rota.recent.*,
        # are another: picker.ts, cleared at sign-out. rota.rotated is when the session's token pair was last rotated.)
        stored=page.evaluate("({keys:Object.keys(localStorage),session:Object.keys(sessionStorage),prefs:JSON.parse(localStorage.getItem('rota.prefs')||'{}'),account:JSON.parse(localStorage.getItem('rota.account')||'{}')})")
        assert set(stored['session'])<={'fmuc.phone-continue'} and {k for k in stored['keys'] if not k.startswith('rota.recent.')}<={'rota.prefs','rota.account','rota.rotated'}
        assert set(stored['account'])<={'identity','name','username','at'}
        assert set(stored['prefs'])<={'theme','navPinned','names','forms'}
        assert not errors
    finally:
        context.close()


def hold_rota_review(page, held):
    """Holds the Submit dialog's first read (the rota's overlaps) until the test lets it answer. The fixture's rota was
    checked by its last command, so Submit runs no new check: the button is busy with these reads."""
    page.route('**/api/schedule/overlaps*', lambda route: held.append(route))


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_submit_busy_label_says_checking(browser, live_server, h, scheme):
    """LIVE-01 / FD-05: while Submit for review only checks the rota, the button says Checking..., never Submitting."""
    w = h.shift(h.workspace('Checking label rota'))['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', color_scheme=scheme)
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        held = []
        hold_rota_review(page, held)
        submit = page.get_by_role('button', name='Submit for review…', exact=True)
        submit.click()
        wait_held(page, held)
        expect(submit).to_contain_text('Checking…')
        expect(submit).to_be_disabled()
        expect(submit).not_to_contain_text('Submitting')
        held.pop().continue_()
        expect(page.get_by_role('dialog', name='Submit Checking label rota for review', exact=True)).to_be_visible()
    finally:
        context.close()


def test_submit_dialog_is_cancelled_when_leaving_schedule(browser, live_server, h):
    """LIVE-01: leaving the Schedule while the check runs cancels the Submit dialog: it never opens over the next page."""
    w = h.shift(h.workspace('Left behind rota'))['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        held = []
        hold_rota_review(page, held)
        page.get_by_role('button', name='Submit for review…', exact=True).click()
        wait_held(page, held)
        navigate(page, 'people')
        # Leaving aborted the page's reads (api.ts leavePage), so the held read has no answer to wait for.
        with contextlib.suppress(PlaywrightError):
            held.pop().continue_()
        # Its reads ended after the person left: the dialog never opens over People.
        with pytest.raises(AssertionError):
            expect(page.get_by_role('dialog')).to_be_visible(timeout=3000)
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'people')
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_empty_session_cell_opens_fill_panel(browser, live_server, h, scheme):
    """SI-17: a session with no minimum and no people is a target: "No one yet" opens the fill panel, by pointer and key."""
    # A duty type with no minimum: an empty session is "No one yet", not an open place.
    typ = h.configure('assignment_types', {'name': 'Optional session fixture', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 0, 'maximum': 1}]}})['record']
    w = h.shift(h.workspace('Optional session rota'), typ=typ['id'], people=[])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', color_scheme=scheme)
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        empty = page.locator('button.pchip.empty')
        expect(empty).to_have_count(1)
        expect(empty).to_have_text('No one yet')
        empty.click()
        expect(page.locator('.wk-panel')).to_be_visible()
        page.get_by_role('button', name='Close panel', exact=True).click()
        expect(page.locator('.wk-panel')).to_have_count(0)
        empty.focus()
        page.keyboard.press('Enter')
        expect(page.locator('.wk-panel')).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_assign_anyway_labels_distinguish_strength(browser, live_server, h, scheme):
    """SR-13 / RUL-E04: adding a warning and breaking a mandatory rule are two named buttons; the second asks for a reason,
    then its own button arms ("Confirm?") before anything is sent."""
    h.policy({'kind': 'restriction'}, {'person_id': h.seed['people'][1]}, severity='advisory')
    # A mandatory rule broken only with a reason (a zero limit), never a safety kind such as restriction (BE-24, AGENTS §3).
    h.policy({'kind': 'max_hours', 'hours': 0, 'window': 'day'}, {'person_id': h.seed['people'][2]})
    w = h.shift(h.workspace('Two strengths rota'), people=[])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', color_scheme=scheme)
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        page.locator('button.pchip.empty, button.pchip.gap').first.click()
        panel = page.locator('.wk-panel')
        expect(panel.get_by_role('button', name=re.compile(r'^Assign .* with a warning$')).first).to_be_visible(timeout=20000)
        expect(panel.get_by_role('button', name='Assign anyway', exact=True)).to_have_count(0)
        blocked = panel.get_by_role('region', name=re.compile(r'^Not available · \d+$'))
        breaking = blocked.get_by_role('button', name=re.compile(r'^Break rule and assign ')).first
        breaking.click()
        form = blocked.locator('form.wk-override')
        reason = form.get_by_role('textbox', name=re.compile(r'^Reason to assign .+ against the rule$'))
        expect(reason).to_be_focused()
        go = form.get_by_role('button', name='Break rule and assign', exact=True)
        go.click()  # no reason: nothing is armed or sent
        expect(blocked.locator('button.armed')).to_have_count(0)
        reason.fill('Only cover available this morning')
        go.click()
        expect(blocked.locator('button.armed')).to_have_count(1)
        expect(blocked.locator('button.armed')).to_contain_text('Confirm?')
        assert not h.detail(w)['shifts'][0]['participants']
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.method == 'POST'):
            blocked.locator('button.armed').click()
        assert [p['override_reason'] for p in h.detail(w)['shifts'][0]['participants']] == ['Only cover available this morning']
    finally:
        context.close()


def three_session_week(h, name):
    w = h.shift(h.workspace(name))['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')['workspace']
    return h.shift(w, start='2026-10-06T08:00:00+03:00', end='2026-10-06T12:00:00+03:00')['workspace']


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_week_selection_target_is_24px_and_visible_at_rest(browser, live_server, h, scheme):
    """SI-11: the session's select box has a 24 px target that does not cover a name, and shows at rest."""
    w = three_session_week(h, 'Selection target rota')
    context, page = sign_in(browser, live_server, 'coordinator', color_scheme=scheme)
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        hit = page.locator('.wk-grid .wk-check-hit').first
        box = hit.bounding_box()
        assert box['width'] >= 24 and box['height'] >= 24, box
        assert float(page.locator('.wk-grid .wk-check').first.evaluate('b => getComputedStyle(b).opacity')) > 0
        chip = page.locator('.wk-grid button.pchip').first.bounding_box()
        assert chip['x'] + chip['width'] <= box['x'] + 0.5, (chip, box)  # beside the name, not over it
        hit.click()
        expect(page.locator('#selection-bar')).to_contain_text('1 session selected')
    finally:
        context.close()


def test_x_ticks_session_and_shift_x_ticks_range(browser, live_server, h):
    """SI-13: x ticks the focused session; Shift+x ticks every session from the last one ticked."""
    w = three_session_week(h, 'Keyboard tick rota')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        chips = page.locator('.wk-grid button.pchip')
        expect(chips).to_have_count(3)
        chips.nth(0).focus()
        page.keyboard.press('x')
        bar = page.locator('#selection-bar')
        expect(bar).to_contain_text('1 session selected')
        chips.nth(2).focus()
        page.keyboard.press('Shift+X')
        expect(bar).to_contain_text('3 sessions selected')
        expect(page.locator('.wk-grid .wk-check:checked')).to_have_count(3)
    finally:
        context.close()


def test_escape_clears_selection_then_closes_panel(browser, live_server, h):
    """SI-12: from the grid, Esc clears the selection first, then closes the session panel; focus stays on the name."""
    w = three_session_week(h, 'Escape order rota')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        chip = page.locator('.wk-grid button.pchip').first
        chip.click()
        expect(page.locator('.wk-panel')).to_be_visible()
        chip.focus()
        page.keyboard.press('x')
        expect(page.locator('#selection-bar')).to_contain_text('1 session selected')
        page.keyboard.press('Escape')
        expect(page.locator('#selection-bar')).to_be_hidden()
        expect(page.locator('.wk-panel')).to_be_visible()
        page.keyboard.press('Escape')
        expect(page.locator('.wk-panel')).to_have_count(0)
        expect(chip).to_be_focused()
    finally:
        context.close()


def test_hover_card_closes_with_panel_escape(browser, live_server, h):
    """SI-01: a panel opened with Enter and closed with Esc leaves no help card on the chip focus returns to."""
    w = three_session_week(h, 'Hover card rota')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        chip = page.locator('.wk-grid button.pchip').first
        chip.focus()
        page.keyboard.press('Enter')
        panel = page.locator('.wk-panel')
        expect(panel).to_be_visible()
        page.keyboard.press('Escape')
        expect(panel).to_have_count(0)
        expect(chip).to_be_focused()
        page.mouse.move(5, 5)
        expect(page.locator('.hovercard')).to_have_count(0, timeout=400)
        page.wait_for_timeout(600)  # past the help delay: still no card
        expect(page.locator('.hovercard')).to_have_count(0)
    finally:
        context.close()


def test_copy_previous_rota_lists_all_or_says_cut(browser, live_server, h):
    """UX-15: with more than 100 rotas, New rota › Copy a previous rota says the list is the latest 100."""
    for i in range(101):
        h.workspace(f'Old rota {i}', '2026-01-04', '2026-01-11')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule/week/2026-10-05?edit=none')
        # SX-14: an empty week offers New rota once, in its empty state (the toolbar's is hidden then).
        page.locator('.wk-empty').get_by_role('button', name='New rota', exact=True).click()
        dialog = page.get_by_role('dialog', name='New rota')
        dialog.get_by_label('Start from', exact=True).select_option('previous')
        expect(dialog.get_by_text('Showing the latest 100 rotas', exact=True)).to_be_visible()
    finally:
        context.close()


# ---- FE-A1 (audit 24 Oct 2026): Planner grid & interactions ----

def two_people_week(h, name, end='2026-10-11'):
    """Two sessions on two days, one person each: what an exchange, a drag or a chip menu needs."""
    a, b = h.seed['people'][:2]
    w = h.shift(h.workspace(name, end=end), people=[a])['workspace']
    return h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[b])['workspace']


def needing_type(h, name):
    return h.configure('assignment_types', {'name': name, 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 2}]}})['record']['id']


def test_chip_menu_offers_replace_exchange_remove(browser, live_server, h):
    """LIVE-36: a name's own menu starts with Replace…, Exchange with… and Remove; Remove takes them off (Undo offered)."""
    w = two_people_week(h, 'Chip menu rota')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        page.locator('.wk-grid button.pchip').first.click(button='right')
        items = page.get_by_role('menu').get_by_role('menuitem')
        expect(items.nth(0)).to_contain_text('Replace…')
        expect(items.nth(1)).to_contain_text('Exchange with…')
        expect(items.nth(2)).to_contain_text('Remove from this session')
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.method == 'POST'):
            items.nth(2).click()
        expect(page.locator('.wk-grid button.pchip.empty, .wk-grid button.pchip.gap')).to_have_count(1)
        assert sorted(len(s['participants']) for s in h.detail(w)['shifts']) == [0, 1]
        expect(page.locator('.toast').get_by_role('button', name='Undo', exact=True)).to_be_visible()
    finally:
        context.close()


def test_exchange_from_chip_menu_prefills_first_side(browser, live_server, h):
    """SR-14 / SI-26 / LIVE-38: Shift+F10 on a name › Exchange with… opens the dialog with that assignment first and only
    the same week's other assignments to pick from (the shared picker), no helper sentence: three steps, not six."""
    w = two_people_week(h, 'Exchange from chip rota', end='2026-10-18')  # 12 Oct lies inside the rota
    w = h.shift(w, start='2026-10-12T08:00:00+03:00', end='2026-10-12T12:00:00+03:00', people=[h.seed['people'][2]])['workspace']
    first = next(s for s in h.detail(w)['shifts'] if s['start'].startswith('2026-10-04'))['participants'][0]
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        page.locator(f".wk-grid {draft_chip(first['person_id'])}").focus()
        page.keyboard.press('Shift+F10')
        page.get_by_role('menuitem', name=re.compile('^Exchange with')).click()
        dialog = page.get_by_role('dialog', name='Exchange two assignments')
        expect(dialog.get_by_label('First assignment', exact=True)).to_have_value(first['id'])
        # The second side lists the other session in that week only (not the one on 12 Oct, not the first itself).
        assert dialog.get_by_label('Second assignment', exact=True).evaluate('s => [...s.options].filter(o => o.value).length') == 1
        expect(dialog).not_to_contain_text('both change together or not at all')
    finally:
        context.close()


def test_drag_lights_targets_hides_card_and_has_no_native_title(browser, live_server, h):
    """SI-22 / SI-24 / SI-06 / SI-25: no native title beside the card; a drag closes the card, outlines every name it can
    land on, and promises a link (an exchange), not a move."""
    w = two_people_week(h, 'Drag targets rota')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        chips = page.locator('.wk-grid button.pchip.draggable')
        expect(chips).to_have_count(2)
        expect(page.locator('button.pchip[title]')).to_have_count(0)
        chips.first.hover()
        expect(page.locator('.hovercard')).to_have_count(1)
        # A script-made DataTransfer is not a drag's (Chromium ignores effectAllowed on it): record what the page sets.
        effect = chips.first.evaluate("""c => {const dt = new DataTransfer(); let set = 'none';
          Object.defineProperty(dt, 'effectAllowed', {get: () => set, set: v => { set = v; }});
          c.dispatchEvent(new DragEvent('dragstart', {bubbles: true, dataTransfer: dt})); return dt.effectAllowed;}""")
        assert effect == 'link'
        expect(page.locator('.hovercard')).to_have_count(0)
        assert chips.nth(1).evaluate('c => getComputedStyle(c).outlineStyle') == 'dashed'
        chips.first.dispatch_event('dragend')
    finally:
        context.close()


def test_drop_shows_the_preview_before_the_reason(browser, live_server, h):
    """SI-23: dropping one name on another opens the exchange with both sides chosen and its impact already worked out;
    the optional note comes after it, and one Save applies it."""
    w = two_people_week(h, 'Drop preview rota')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        chips = page.locator('.wk-grid button.pchip.draggable')
        chips.first.drag_to(chips.nth(1))
        dialog = page.get_by_role('dialog', name='Exchange two assignments')
        expect(dialog.get_by_text('Not saved yet', exact=False)).to_be_visible(timeout=20000)
        assert dialog.evaluate("""d => {const p = d.querySelector('.form-section'), r = d.querySelector('textarea');
          return !!(p.compareDocumentPosition(r) & Node.DOCUMENT_POSITION_FOLLOWING);}"""), 'the preview comes before the note'
    finally:
        context.close()


def test_names_carry_no_drag_listeners_of_their_own(browser, live_server, h):
    """RESP-REVIEW: dragging is handled once by the document, not by five listeners on every name: a department week
    redraws thousands of names on every edit. A drag still starts, outlines the other names and ends cleanly."""
    w = two_people_week(h, 'Delegated drag rota')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        chips = page.locator('.wk-grid button.pchip.draggable')
        expect(chips).to_have_count(2)
        cdp = context.new_cdp_session(page)
        chip = cdp.send('Runtime.evaluate', {'expression': "document.querySelector('.wk-grid button.pchip.draggable')"})['result']
        own = {x['type'] for x in cdp.send('DOMDebugger.getEventListeners', {'objectId': chip['objectId']})['listeners']}
        # Nor a click or menu of its own (RESP-REVIEW): the grid handles them and reads the draft as held now.
        assert not own & {'dragstart', 'dragend', 'dragover', 'dragleave', 'drop', 'click', 'contextmenu'}, own
        chips.first.evaluate("c => c.dispatchEvent(new DragEvent('dragstart', {bubbles: true, dataTransfer: new DataTransfer()}))")
        expect(page.locator('body')).to_have_class(re.compile(r'\bis-dragging\b'))
        assert chips.nth(1).evaluate('c => getComputedStyle(c).outlineStyle') == 'dashed'
        chips.first.dispatch_event('dragend')
        expect(page.locator('body')).not_to_have_class(re.compile(r'\bis-dragging\b'))
    finally:
        context.close()


LIVE_GRID = """async () => {
  const m = await window.importBundle('planner_week.js');
  const days = Array.from({length: 7}, (_, i) => `2026-10-${String(4 + i).padStart(2, '0')}`);
  const shift = participants => ({id: 's1', workspace_id: 'w', assignment_type_id: 't', location_id: 'l', service_ids: ['v'],
    start: '2026-10-04T08:00:00+03:00', end: '2026-10-04T12:00:00+03:00', attributes: {}, participants,
    type_snapshot: {name: 'Clinic duty', definition: {fields: [], requirements: [{minimum: 1}]}}});
  const a = {id: 'a1', person_id: 'p1'}, b = {id: 'b1', person_id: 'p2'};
  const ctx = {me: {organisation: {timezone: 'Asia/Riyadh', week_start: 6}, person_id: null},
    catalogues: {people: [{id: 'p1', display_name: 'Drawn One'}, {id: 'p2', display_name: 'Live Two'}], services: [{id: 'v', name: 'Clinic'}],
      locations: [{id: 'l', name: 'Room'}], memberships: [], categories: [], assignment_types: []}};
  const drawn = {workspace: {id: 'w', revision: 1, status: 'draft', starts_on: '2026-10-04', ends_on: '2026-10-11',
    validation: {findings: [], coverage: []}}, shifts: [shift([a])], wishes: []};
  // The draft as held now: a later revision, someone added since the grid was drawn (an edit's delta, app.ts heldDraft).
  const live = {...drawn, workspace: {...drawn.workspace, revision: 7}, shifts: [shift([a, b])]};
  const hooks = {name: p => (p && p.display_name) || '', code: () => 'X', details: () => {}, dnd: n => n, proposal: null,
    editable: true, fresh: true, selected: new Set(), onSelect: () => {}, refresh: async () => {}, tallies: true, live: () => live};
  const root = m.weekGrid(ctx, drawn, drawn.shifts, {days, page: 0, has_previous: false, has_next: false, unit: 'week'}, () => {}, hooks);
  // In a modal dialog (the top layer), so the page's fixed bars never cover it; its menu opens inside it (dom.ts popover).
  const layer = document.createElement('dialog'); layer.id = 'live-grid'; layer.setAttribute('aria-label', 'Live grid'); layer.append(root);
  document.body.append(layer); layer.showModal();
}"""


def test_a_names_menu_acts_on_the_draft_as_held_now_not_as_drawn(browser, live_server, h):
    """RESP-REVIEW: Remove from a name's menu sends the draft's current revision and the session's current roster, not
    the ones captured when the grid was drawn (a cell left in place by a partial redraw must never send a stale save)."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        page.evaluate(LIVE_GRID)
        page.route('**/api/commands', lambda r: r.fulfill(status=409, json={'error': {'code': 'STALE_REVISION', 'message': 'Test refusal.'}}))
        page.locator('#live-grid button.pchip[data-assignment="a1"]').click(button='right')
        with page.expect_request(lambda r: r.url.endswith('/api/commands') and r.method == 'POST') as sent:
            page.get_by_role('menuitem', name=re.compile('^Remove')).click()
        body = sent.value.post_data_json
        assert body['kind'] == 'shift.update' and body['expected_revision'] == 7, body
        assert [x['person_id'] for x in body['payload']['participants']] == ['p2'], body
    finally:
        context.close()


def test_select_these_ticks_in_place_and_the_selection_survives_a_week_change(browser, live_server, h):
    """SI-14 / SI-15: Select these ticks the boxes without drawing the page again; paging to another week keeps the
    selection and the bar says how many are out of view."""
    w = three_session_week(h, 'Select these rota')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        grid = page.locator('.wk-grid').element_handle()
        page.locator('.wk-rowhead').first.click(button='right')
        page.get_by_role('menuitem', name=re.compile(r'^Select these 3 sessions')).click()
        bar = page.locator('#selection-bar')
        expect(bar).to_contain_text('3 sessions selected')
        assert grid.evaluate('g => g.isConnected')
        expect(page.locator('.wk-grid .wk-check:checked')).to_have_count(3)
        page.get_by_role('button', name='Next week').first.click()
        expect(bar).to_contain_text('3 sessions selected')
        expect(bar).to_contain_text('3 in other weeks')
    finally:
        context.close()


def test_selection_bar_offers_resolve_these(browser, live_server, h):
    """SI-16: sessions with open places selected: the bar offers Resolve these, which steps through only them."""
    typ = needing_type(h, 'Needs one fixture')
    w = h.shift(h.workspace('Resolve these rota'), people=[], typ=typ)['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[], typ=typ)['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        expect(page.locator('.wk-grid button.pchip.gap')).to_have_count(2, timeout=20000)
        page.locator('.wk-grid .wk-check-hit').first.click()
        page.locator('#selection-bar').get_by_role('button', name='Resolve these').click()
        expect(page.locator('.resolve-bar')).to_contain_text('1 to resolve')
    finally:
        context.close()


def test_lock_icon_has_a_24px_target(browser, live_server, h):
    """SI-21: the lock on a locked session is a 24 px target (WCAG 2.5.8)."""
    w = h.shift(h.workspace('Lock target rota'))['workspace']
    sid = h.detail(w)['shifts'][0]['id']
    h.command('coordinator', 'shift.action', {'workspace_id': w['id'], 'shift_ids': [sid], 'action': 'lock', 'reason': 'Keep this session'}, revision=w['revision'])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        # WCAG 2.5.8 counts CSS pixels: the used size, and the box at rest (animations finished) in layout units (1/64 px),
        # so float noise from a transform (23.99998) is not read as a smaller target.
        size = page.locator('button.wk-lock').evaluate('''b => {
          document.getAnimations().filter(a => a.effect.getComputedTiming().endTime!==Infinity).forEach(a => a.finish());
          const s = getComputedStyle(b), r = b.getBoundingClientRect(), unit = v => Math.round(v * 64) / 64;
          return {css: [parseFloat(s.width), parseFloat(s.height)], box: [unit(r.width), unit(r.height)]};
        }''')
        assert min(size['css'] + size['box']) >= 24, size
    finally:
        context.close()


def test_session_panel_steps_to_the_next_session(browser, live_server, h):
    """SI-38: the session panel's shared head says "Session 1 of 3" and ↓ opens the next one, the focus staying on ↓."""
    w = three_session_week(h, 'Panel steps rota')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        page.locator('.wk-grid button.pchip').first.click()
        head = page.locator('.wk-panel .wk-panel-head')
        expect(head).to_contain_text('Session 1 of 3')
        head.get_by_role('button', name='Next session').click()
        expect(head).to_contain_text('Session 2 of 3')
        expect(head.get_by_role('button', name='Next session')).to_be_focused()
        expect(page.locator('.wk-cell.is-explained')).to_have_count(1)
    finally:
        context.close()


def test_why_tab_and_mode_move_the_ring_without_drawing_the_grid_again(browser, live_server, h):
    """SI-40 / SI-10: changing the panel's tab or the Why mode keeps the same grid; the ringed names are aria-current."""
    w = three_session_week(h, 'Why ring rota')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        page.locator('.wk-grid button.pchip').first.click()
        expect(page.locator('[data-session].is-explained [aria-current="true"]')).to_have_count(1)
        grid = page.locator('.wk-grid').element_handle()
        panel = page.locator('.wk-panel')
        panel.get_by_role('tab', name='Why', exact=True).click()
        panel.get_by_role('radio', name='A person', exact=True).click()
        expect(page.locator('.wk-grid .pchip[aria-current="true"]')).not_to_have_count(0)
        panel.get_by_role('tab', name='Rules', exact=True).click()
        assert grid.evaluate('g => g.isConnected')
    finally:
        context.close()


def test_open_place_card_says_places_filled(browser, live_server, h):
    """SR-24: the open place's card counts places filled, not "counted"."""
    typ = needing_type(h, 'Needs one card fixture')
    w = h.shift(h.workspace('Gap card rota'), people=[], typ=typ)['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        page.locator('.wk-grid button.pchip.gap').hover(timeout=20000)
        card = page.get_by_role('tooltip')
        expect(card).to_contain_text('0 of 1 places filled')
        expect(card).not_to_contain_text('counted')
    finally:
        context.close()


def test_help_lists_every_view_letter(browser, live_server, h):
    """SR-09: the help names every view letter, Timeline's T included (built from the views list)."""
    w = three_session_week(h, 'Help letters rota')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        page.get_by_role('button', name='How to read this schedule').click()
        expect(page.locator('.wk-help')).to_contain_text('Press Y, M, W, D, T, P, C or L')
    finally:
        context.close()


def test_refused_save_marks_the_session(browser, live_server, h):
    """SI-29: a refused edit puts the names back and marks the session "Couldn't save" for a few seconds."""
    w = two_people_week(h, 'Refused save rota')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        page.route('**/api/commands', lambda route: route.fulfill(status=409, json={'error': {'code': 'STALE_REVISION', 'message': 'The rota changed.'}}))
        page.locator('.wk-grid button.pchip').first.click(button='right')
        page.get_by_role('menuitem', name=re.compile('^Remove from this session')).click()
        expect(page.locator('.wk-grid .save-mark.failed')).to_contain_text("Couldn't save")
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_chip_names_say_only_the_exceptions(browser, live_server, h):
    """SI-31: a passing name's accessible name does not end in "no broken rules" (silence is fine)."""
    w = three_session_week(h, 'Quiet names rota')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        chips = page.locator('.wk-grid button.pchip')
        expect(chips).to_have_count(3)
        assert not [n for n in chips.evaluate_all('cs => cs.map(c => c.getAttribute("aria-label"))') if 'no broken rules' in n]
    finally:
        context.close()


def test_filtered_day_numbers_say_filtered(browser, live_server, h):
    """SI-34 / SI-32: with a filter on, the day numbers say they count the filtered sessions only."""
    typ = needing_type(h, 'Needs one filter fixture')
    w = h.shift(h.workspace('Filtered days rota'), people=[], typ=typ)['workspace']
    w = h.shift(w)['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        expect(page.locator('.wk-grid button.pchip.gap')).to_have_count(1, timeout=20000)
        filters = planner_filter(page)
        filters.get_by_label('Only sessions with open places', exact=True).check()
        close_popover(page)
        expect(page.locator('.wk-day').nth(0)).to_have_attribute('aria-label', re.compile('filtered'))
    finally:
        context.close()


def test_collapsed_groups_are_saved_with_the_view(browser, live_server, h):
    """SI-35: a collapsed group is part of the saved view and comes back with it."""
    from .test_browser_collection_filters import save_view
    w = h.shift(h.shift(h.workspace('Collapsed view rota'))['workspace'], service=1)['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        page.locator('.wk-toggle').first.click()
        expect(page.locator('.wk-toggle').first).to_have_attribute('aria-expanded', 'false')
        save_view(page.locator('.planner-toolbar'), 'Collapsed groups')
        page.reload()
        expect(page.locator('.wk-toggle').first).to_have_attribute('aria-expanded', 'false')
    finally:
        context.close()


def test_hover_card_returns_after_a_redraw_under_a_resting_pointer(browser, live_server, h):
    """SI-49: a live refresh redraws the grid under a resting pointer; its card comes back without moving the mouse."""
    w = three_session_week(h, 'Rehover rota')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        page.locator('.wk-grid button.pchip').first.hover()
        expect(page.locator('.hovercard')).to_have_count(1)
        grid = page.locator('.wk-grid').element_handle()
        h.shift(h.detail(w)['workspace'], start='2026-10-07T08:00:00+03:00', end='2026-10-07T12:00:00+03:00')
        page.wait_for_function('g => !g.isConnected', arg=grid, timeout=30000)
        expect(page.locator('.hovercard')).to_have_count(1, timeout=2000)
    finally:
        context.close()


def test_grid_tab_order_settles_once_per_frame(browser, live_server, h):
    """SI-39 / SI-45: many changes to the grid in one go rewrite its tab order once, not once per change."""
    w = three_session_week(h, 'Settle once rota')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        writes = page.evaluate("""async () => {
          const grid = document.querySelector('.wk-grid'), frames = () => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
          await frames();
          let n = 0; const seen = new MutationObserver(rs => { n += rs.length; });
          seen.observe(grid, {attributes: true, subtree: true, attributeFilter: ['tabindex']});
          for (let i = 0; i < 100; i++) { const s = document.createElement('span'); grid.append(s); s.remove(); }
          await frames(); seen.disconnect(); return n; }""")
        focusables = page.locator('.wk-grid').evaluate("g => g.querySelectorAll('button,a[href],input,select,textarea,[tabindex]').length")
        assert writes <= focusables, (writes, focusables)
    finally:
        context.close()


def test_row_head_card_shows_status_not_configuration(browser, live_server, h):
    """SI-07: the duty row's card says what it needs and how it stands; no eligibility or supervision configuration."""
    w = three_session_week(h, 'Row card rota')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        page.locator('.wk-rowhead').first.hover()
        card = page.locator('.hovercard')
        expect(card).to_be_visible()
        expect(card).not_to_contain_text('Eligibility')
        expect(card).not_to_contain_text('No trainee supervision rule')
    finally:
        context.close()


def test_pair_sub_line_truncates_before_the_name(browser, live_server, h):
    """SI-28: the "↳ with Name" line has its own truncation, so the person's name never gives way first."""
    w = three_session_week(h, 'Pair truncation rota')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        css = page.evaluate("""() => {const s = document.createElement('span'); s.className = 'pchip-sub'; document.body.append(s);
          const c = getComputedStyle(s), out = [c.minWidth, c.textOverflow, c.whiteSpace]; s.remove(); return out;}""")
        assert css == ['0px', 'ellipsis', 'nowrap'], css
    finally:
        context.close()


def test_agenda_swipes_to_the_next_week(browser, live_server, h):
    """SI-42: on a phone, swiping the day list sideways pages to the next week (the arrows stay)."""
    h.publish(three_session_week(h, 'Swipe rota'))
    context, page = sign_in(browser, live_server, 'staff', viewport={'width': 390, 'height': 844})
    try:
        navigate(page, 'schedule/week/2026-10-04')
        ag = page.locator('.ag').first
        expect(ag).to_be_visible()  # drawn before it is measured (bounding_box does not wait)
        heading = page.locator('.wk-nav strong').first
        expect(heading).to_contain_text('Oct')
        before = heading.text_content()
        box = ag.bounding_box()
        y = box['y'] + 40
        ag.dispatch_event('pointerdown', {'pointerType': 'touch', 'clientX': box['x'] + 300, 'clientY': y})
        ag.dispatch_event('pointerup', {'pointerType': 'touch', 'clientX': box['x'] + 60, 'clientY': y})
        expect(heading).not_to_have_text(before)
    finally:
        context.close()


def test_exchange_refusal_is_said_at_once_and_save_does_not_ask_again(browser, live_server, h):
    """H-03: an exchange the server refuses (it would put someone in two places at once) says so in the dialog with the
    clash on the exchanged sessions first, and Exchange then repeats that refusal at once: no second preview, no spinner.
    The pickers name each assignment's day, since one person can hold several sessions in a week."""
    a, b = h.seed['people'][:2]
    w = two_people_week(h, 'Exchange refusal rota')
    # A also works Monday's morning elsewhere: taking B's Monday morning would double-book A.
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[a])['workspace']
    shifts = h.detail(w)['shifts']
    sunday = next(s for s in shifts if s['start'].startswith('2026-10-04'))['participants'][0]
    monday_b = next(p for s in shifts if s['start'].startswith('2026-10-05') for p in s['participants'] if p['person_id'] == b)
    context, page = sign_in(browser, live_server, 'coordinator')
    previews = []
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        page.on('request', lambda r: previews.append(r.url) if r.url.endswith('/api/planner/preview') else None)
        page.locator(f".wk-grid {draft_chip(b)}").focus()
        page.keyboard.press('Shift+F10')
        page.get_by_role('menuitem', name=re.compile('^Exchange with')).click()
        dialog = page.get_by_role('dialog', name='Exchange two assignments')
        second = dialog.get_by_label('Second assignment', exact=True)
        second.select_option(sunday['id'])
        expect(dialog.get_by_label('First assignment', exact=True).locator('option:checked')).to_contain_text('5 Oct')
        expect(second.locator('option:checked')).to_contain_text('4 Oct')
        refusal = dialog.locator('.note.error, [role=alert]').filter(has_text='neither side was saved')
        expect(refusal).to_be_visible(timeout=20000)
        expect(dialog.locator('.preview-findings').first).to_contain_text('Checks for changed sessions')
        asked = len(previews)
        dialog.get_by_role('button', name='Exchange', exact=True).click()
        expect(dialog.get_by_role('button', name='Exchange', exact=True)).to_be_enabled()
        # Exchange sends nothing and says so at the button, in the server's words (the refusal above stays as it was).
        expect(dialog.get_by_role('alert').filter(has_text='Not saved: ')).to_contain_text('neither side was saved')
        expect(refusal.filter(has_not_text='Not saved: ')).to_have_count(1)
        assert len(previews) == asked, previews
        assert [p['person_id'] for s in h.detail(w)['shifts'] for p in s['participants'] if p['id'] == monday_b['id']] == [b]
        shot(page, 'exchange-refusal-1440-light', full_page=False)
    finally:
        context.close()
# Include this file in hosted supervision and team-lead qualification.
