"""The one issue list reviewed: every issue item, or each session chip of an item
whose words repeat, is a button that shows its session (moving to its week when needed), focused and highlighted; the
list ends with "Open all in Fix list (N)", which opens the Fix list panel at exactly those causes. Used by every marker
popover (day, row, cell, team, person), the session panel and form, and the published-changes check. Real findings:
two-place sessions with one person, checked by the server."""
import json
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401
from .test_browser_fix_list_panel import DESKTOP, real_gaps

pytestmark = pytest.mark.browser


def spot(page, shift):
    """The session's marked places on the schedule (markShift: data-rk names the session)."""
    return page.locator(f"#main [data-rk*='\"shift\":\"{shift}\"']")


def focused_on(page, shift):
    return page.evaluate("s=>{const a=document.activeElement;return !!a&&(a.getAttribute('data-rk')||'').includes('\"shift\":\"'+s+'\"');}", shift)


def cell_popover(page, shift):
    marker = page.locator(f'#schedule-view [data-session~="{shift}"] .wk-open-row .iss-mark')
    marker.click()
    pop = page.get_by_role('dialog', name=re.compile('^Issues: '))
    expect(pop.locator('.iss-row').first).to_be_visible()
    return pop


def test_an_issue_item_shows_its_session_focused_and_highlighted(browser, live_server, h):
    from .test_browser_team_box import open_view
    w, gaps, gap = real_gaps(h, 'issue item')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        open_view(page, w, 'Week')
        pop = cell_popover(page, gaps[0])
        item = pop.locator('button.iss-go, button.fix-chip').first
        expect(item).to_be_visible()
        item.click()
        expect(pop).to_have_count(0)
        expect(spot(page, gaps[0]).and_(page.locator('.issue-spot')).first).to_be_visible()
        assert focused_on(page, gaps[0])
    finally:
        context.close()


def test_an_issue_item_moves_the_schedule_across_a_week_boundary(browser, live_server, h):
    """The list on screen names a session in another week (a rule's or a person's issues do): the schedule moves to that
    week and marks the session once drawn. The real component, hosted in the page with the server's own finding."""
    from .test_browser_team_box import open_view
    w, gaps, gap = real_gaps(h, 'issue week')
    finding = next(f for f in h.detail(w)['workspace']['validation']['findings'] if f.get('shift_id') == gaps[0] and f['severity'] == 'error')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        open_view(page, w, 'Week')
        page.get_by_role('button', name='Next week').first.click()
        expect(spot(page, gaps[0])).to_have_count(0)
        page.evaluate("""async f=>{const m=await import(document.querySelector('script[type=module]').src.replace(/app\\.js.*$/,'issue_marker.js'));
          const host=document.createElement('div');host.id='issue-host';host.append(m.issueList([m.findingIssue(f,'Thu 8 Oct')]));document.body.append(host);}""", finding)
        page.locator('#issue-host button.iss-go').click()
        expect(spot(page, gaps[0]).and_(page.locator('.issue-spot')).first).to_be_visible()
        assert focused_on(page, gaps[0])
        assert '2026-10-0' in page.url  # the week of 8 Oct, not the week after
    finally:
        page.evaluate("document.getElementById('issue-host')?.remove()")
        context.close()


def test_open_all_in_fix_list_opens_the_panel_at_exactly_those_causes(browser, live_server, h):
    from .test_browser_team_box import open_view
    w, gaps, gap = real_gaps(h, 'open all')
    listed = h.get('coordinator', f'/api/workspaces/{w["id"]}/fix-list')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        open_view(page, w, 'Week')
        pop = cell_popover(page, gaps[0])
        n = pop.locator('.iss-row').count()
        pop.get_by_role('button', name=re.compile(r'^Open all in Fix list \(\d+\)$')).click()
        panel = page.locator('.dock-panel.fix-side')
        expect(panel).to_be_visible()
        assert '#schedule/issues' not in page.url
        expect(panel.locator(f'li[data-cause="{gap["id"]}"]')).to_be_visible()
        # Exactly its causes, said with how many of the rota's, and Show all brings back the rest.
        only = panel.locator('.fix-only')
        expect(only).to_contain_text(f'of {len(listed["causes"])}')
        shown = panel.locator('li[data-cause]').count()
        assert 1 <= shown <= max(n, 1)
        only.get_by_role('button', name='Show all').click()
        expect(panel.locator('.fix-only')).to_have_count(0)
        expect(panel.locator('section[data-level=must] li[data-cause]')).to_have_count(sum(c['level'] == 'must' for c in listed['causes']))
    finally:
        context.close()


def test_issue_lists_work_from_the_keyboard(browser, live_server, h):
    from .test_browser_team_box import open_view
    w, gaps, gap = real_gaps(h, 'issue keys')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        open_view(page, w, 'Week')
        marker = page.locator(f'#schedule-view [data-session~="{gaps[0]}"] .wk-open-row .iss-mark')
        marker.focus()
        page.keyboard.press('Enter')
        pop = page.get_by_role('dialog', name=re.compile('^Issues: '))
        expect(pop.locator('.iss-row').first).to_be_visible()
        # Focus moves into the popover onto its first item; Tab reaches Open all; Enter on an item goes to its session.
        first = pop.locator('button.iss-go, button.fix-chip').first
        expect(first).to_be_focused()
        page.keyboard.press('Enter')
        expect(pop).to_have_count(0)
        assert focused_on(page, gaps[0])
        # Escape closes a popover without moving anything.
        marker.focus()
        page.keyboard.press('Enter')
        expect(page.get_by_role('dialog', name=re.compile('^Issues: '))).to_be_visible()
        page.keyboard.press('Escape')
        expect(page.get_by_role('dialog', name=re.compile('^Issues: '))).to_have_count(0)
    finally:
        context.close()


def test_the_same_words_are_one_item_with_their_sessions_as_chips(browser, live_server, h):
    """The owner's screenshot: "More people are scheduled than the configured cover places need" twice. One item, its
    sessions as chips, in the day's popover; each chip goes to its session."""
    from .test_browser_team_box import open_view
    w, gaps, gap = real_gaps(h, 'same words')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        open_view(page, w, 'Week')
        head = page.locator('#schedule-view .wk-rowhead').filter(has_text='Fictional two-place clinic · same words').locator('.iss-mark')
        head.click()
        pop = page.get_by_role('dialog', name=re.compile('^Issues: '))
        texts = [t.strip() for t in pop.locator('.iss-row .iss-text').all_text_contents()]
        words = [re.sub(r' · .*$', '', t) for t in texts]
        assert len(words) == len(set(words)), texts  # no sentence twice
        # The repeated sentence is one item whose chips are its two sessions, in time order.
        chips = pop.locator('.iss-row').filter(has=page.locator('.fix-chips')).first.locator('button.fix-chip:not(.more)')
        expect(chips).to_have_count(2)
        chips.nth(1).click()
        assert focused_on(page, gaps[1])
    finally:
        context.close()
