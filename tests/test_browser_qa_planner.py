"""Planner QA (stream A): redraws keep the screen and never leave a stale popover; the footer runs a check the rota could
not finish; rota health lists name each session; tooltips give way to what their button opened; the View popover fits."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, pick_view, toolbar, close_popover  # noqa: F401
from .test_browser import fix_pill

pytestmark = pytest.mark.browser


def clinic(h):
    return h.configure('assignment_types', {'name': 'QA clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1}]}})['record']['id']


def checked_rota(h, name='QA planner'):
    a, b, _ = h.seed['people']
    typ = clinic(h)
    w = h.workspace(name)
    w = h.shift(w, people=[a, b], typ=typ)['workspace']  # one session staffed by two
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[], typ=typ)['workspace']  # one open place
    return h.lifecycle(w, 'validate')['workspace'], typ


def settled(page):
    expect(page.locator('#main[data-loaded-view=schedule] .planner-main')).to_be_visible()
    expect(page.locator('.health-pill.checking')).to_have_count(0, timeout=20000)


def test_view_changes_keep_the_shell_and_close_a_stale_popover(browser, live_server, h):
    checked_rota(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner'); settled(page)
        nav = page.locator('#department-nav').element_handle()
        # Watch for the page being blanked (the whole-page skeleton) while the next view loads. The body alone going to
        # its view's skeleton is by design (S-27: the old view never mixes with the new one), the shell stays.
        page.evaluate("""() => {window.__blank=0;new MutationObserver(()=>{if(document.querySelector('#main .loading.page-sk'))window.__blank++;})
            .observe(document.body,{subtree:true,childList:true});}""")
        pick_view(page, 'Month'); pick_view(page, 'List'); pick_view(page, 'Week')
        assert nav.evaluate('n => n.isConnected')  # the sidebar was not rebuilt
        assert page.evaluate('window.__blank') == 0
        # The keyed health popover adopts the current pill across a view redraw.
        fix_pill(page, 'err').click()
        health = page.get_by_role('dialog', name='Rota health', exact=True)
        expect(health).to_be_visible()
        panel = health.element_handle()
        page.keyboard.press('l')  # the List shortcut redraws the schedule under the open popover
        expect(page.locator('#schedule-view')).to_have_attribute('aria-label', 'List view')
        expect(fix_pill(page, 'err')).to_have_attribute('aria-expanded', 'true')
        assert fix_pill(page, 'err').evaluate('n=>n.isConnected') and panel.evaluate('n=>n.isConnected')
        fix_pill(page, 'err').click()  # The current anchor owns the existing popover, so it toggles closed.
        expect(health).to_have_count(0)

        # A Week-only action menu has no replacement anchor in List and must close.
        pick_view(page, 'Week')
        opener = page.locator('#schedule-view .wk-more').first
        anchor = opener.element_handle()
        opener.click()
        expect(page.locator('.popover[role=menu]')).to_be_visible()
        opener.focus()  # Keep the menu open while the view shortcut targets its disappearing anchor.
        page.keyboard.press('l')
        expect(page.locator('#schedule-view')).to_have_attribute('aria-label', 'List view')
        assert not anchor.evaluate('n=>n.isConnected')
        expect(page.locator('.popover')).to_have_count(0)
        # A toolbar popover stays open across the redraw its own change causes (it moves to the new button).
        panel = toolbar(page.locator('.planner-toolbar'), 'Filter')
        panel.get_by_label('Only sessions with open places', exact=True).check()
        expect(page.locator('#main[data-loaded-view=schedule]')).to_have_count(1)
        expect(page.get_by_role('dialog', name='Filter', exact=True)).to_be_visible()
        expect(page.locator('.planner-toolbar button.dt-tool[aria-expanded=true]')).to_have_count(1)
        close_popover(page)
    finally:
        context.close()


def test_rota_health_names_sessions_and_a_failed_check_is_rerun_from_the_footer(browser, live_server, h):
    workspace, _ = checked_rota(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        # The automatic check fails once (server error): the footer offers the check instead of a top-bar button.
        page.route(re.compile(r'.*/api/commands$'), lambda route: route.fulfill(status=500, content_type='application/json',
            body='{"error":{"code":"INTERNAL","message":"Temporary failure"}}') if 'workspace.validate' in (route.request.post_data or '') else route.continue_())
        h.policy({'kind': 'min_rest', 'hours': 11})  # a new rule: the stored check is now outdated
        page.reload()
        navigate(page, 'planner')
        footer = page.locator('footer.app-footer')
        # B7 + DL-018: the failed check is said (Check stopped, with the last counts kept) and the footer's pill checks again.
        again = footer.get_by_role('button', name=re.compile(r'Check stopped · Check again'))
        expect(again).to_be_visible(timeout=20000)
        expect(page.locator('.topbar').get_by_role('button', name='Check rota')).to_have_count(0)
        page.unroute(re.compile(r'.*/api/commands$'))
        again.click()
        expect(page.locator('.toast').filter(has_text='Checked:')).to_be_visible()
        # The checked rota's Fix list: each must-fix cause says what and when (the server's words and meta).
        fix = h.get('coordinator', f"/api/workspaces/{workspace['id']}/fix-list")
        must = [c for c in fix['causes'] if c['level'] == 'must']
        blocking = footer.get_by_role('button', name=f"{len(must)} must fix · open the fix list", exact=True)
        blocking.click()
        head = page.locator('.popover.fix-pop')
        expect(head.locator('.iss-row')).to_have_count(len(must))  # the one issue list (live check F6)
        for cause in must:
            expect(head).to_contain_text(cause['sentence'])
        # The open popover hides its button's tooltip. The footer may redraw meanwhile (a late check result): the open
        # popover moves to the new button (adoptPopover), so look the button up afresh until it settles; a one-shot
        # read of the old, detached button returned '' under load.
        page.wait_for_function("""() => {const b=[...document.querySelectorAll('footer.app-footer button')]
            .find(x=>/must fix · open the fix list$/.test(x.getAttribute('aria-label')||x.textContent.trim()));
            return !!b && b.getAttribute('aria-expanded')==='true' && !document.querySelector('[role=tooltip]');}""")
    finally:
        context.close()


def test_over_staffed_session_is_not_an_open_place(browser, live_server, h):
    typ = h.configure('assignment_types', {'name': 'Single night cover', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']['id']
    w = h.shift(h.workspace('Over staffed'), typ=typ, people=h.seed['people'][:2])['workspace']
    h.lifecycle(w, 'validate')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner'); settled(page)
        footer = page.locator('footer.app-footer')
        expect(footer.get_by_role('button', name=re.compile(r'needs? staff$'))).to_have_count(0)
        expect(footer.get_by_role('button', name=re.compile(r'must fix · open the fix list$'))).to_be_visible()
        pick_view(page, 'Coverage')
        expect(page.locator('.cov-cell.full')).to_have_count(1)
    finally:
        context.close()


def test_view_popover_layouts_fit_and_tools_tooltip_gives_way(browser, live_server, h):
    checked_rota(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner'); settled(page)
        panel = toolbar(page.locator('.planner-toolbar'), 'View settings')
        fits = panel.evaluate("""p => {const r=p.getBoundingClientRect();
            return [...p.querySelectorAll('.dt-layouts [role=radio]')].every(b => {const x=b.getBoundingClientRect();return x.left>=r.left&&x.right<=r.right;});}""")
        assert fits and panel.evaluate('p => p.scrollWidth <= p.clientWidth')
        close_popover(page)
        tools = page.get_by_role('button', name='Rota tools', exact=True)
        tools.hover(); tools.click()
        expect(page.get_by_role('menu', name='Rota tools')).to_be_visible()
        expect(page.get_by_role('tooltip')).to_have_count(0)
    finally:
        context.close()


def test_rota_tools_has_export_and_print_sheet_and_two_clearly_different_rules_entries(browser, live_server, h):
    """Rota tools: one "Print or export…" (this view as PDF or PNG, or a published rota's sheet: SR-11); the two rules
    entries say what each does."""
    checked_rota(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner'); settled(page)
        page.get_by_role('button', name='Rota tools', exact=True).click()
        menu = page.get_by_role('menu', name='Rota tools')
        expect(menu.get_by_role('menuitem', name='Print or export…', exact=True)).to_be_visible()
        expect(menu.get_by_role('menuitem', name='Rules in this rota', exact=True)).to_be_visible()
        expect(menu.get_by_role('menuitem', name=re.compile('^Workload targets for this rota'))).to_be_visible()
        expect(menu.get_by_role('menuitem', name='Who’s available', exact=True)).to_be_visible()
        menu.get_by_role('menuitem', name='Print or export…', exact=True).click()
        page.get_by_role('dialog', name='Print or export').get_by_role('button', name='Published sheet…', exact=True).click()
        expect(page.get_by_role('dialog', name='Print sheet')).to_be_visible()
    finally:
        context.close()


ANCHORED = """() => {const p=document.querySelector('.popover.vs-pop'),t=[...document.querySelectorAll('button.dt-tool[aria-expanded=true]')]
    .find(b=>b.isConnected&&(b.getAttribute('aria-label')||'').startsWith('View settings'));
  if(!p||!t)return false;const a=t.getBoundingClientRect(),top=parseFloat(p.style.top),left=parseFloat(p.style.left),w=p.offsetWidth;
  return a.width>0&&top>=a.bottom&&top<=a.bottom+12&&left<=a.right&&left+w>=a.left;}"""


@pytest.mark.parametrize('viewport', [{'width': 848, 'height': 741}, {'width': 1440, 'height': 900}])
def test_view_settings_stays_on_its_button_while_layouts_redraw_the_schedule(browser, live_server, h, viewport):
    """The View settings popover stays anchored under its (redrawn) toolbar button as List ↔ Calendar ↔ Coverage
    redraw the Schedule, never thrown to the top-left corner; Escape still closes it onto that button."""
    checked_rota(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport=viewport)
    try:
        navigate(page, 'planner'); settled(page)
        panel = toolbar(page.locator('.planner-toolbar'), 'View settings')
        for layout in ['List', 'Calendar', 'List', 'Coverage', 'Calendar']:
            radio = panel.get_by_role('radio', name=layout, exact=True)
            if radio.get_attribute('aria-checked') != 'true':
                radio.click()
            expect(panel.get_by_role('radio', name=layout, exact=True)).to_have_attribute('aria-checked', 'true')
            settled(page)
            page.wait_for_function(ANCHORED)
            expect(page.locator('.popover')).to_have_count(1)
        page.keyboard.press('Escape')
        expect(page.locator('.popover')).to_have_count(0)
        expect(page.locator('.planner-toolbar button.dt-tool[aria-label^="View settings"]')).to_be_focused()
    finally:
        context.close()
