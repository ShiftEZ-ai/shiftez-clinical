"""The Fix list beside the Planner (FIXLIST-UX direction A, coordinator decision 7 Nov 2026 under D80).

On a desktop the Fix list is the Planner's docked side panel: every must-fix cause open in plain words, its sessions as
date chips, and the one in focus with Assign in place (the session panel's candidates). After an Assign the edit's
fix_list_delta updates the row at once: the fixed session's chip says ✓ and who, and the focus moves to the next open
session. Could improve opens collapsed. A phone keeps the page, with the same rows and Assign (the D10 exception for
this flow). Layout tests serve causes in the contract's shape (test_browser_fix_list.serve); the Assign and marker tests
use real findings and the server's own Fix list."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, open_rota, fix_list_reply  # noqa: F401
from .test_browser_fix_list import cause, serve, shot
from .test_browser import fix_pill

pytestmark = pytest.mark.browser
DESKTOP, PHONE = {'width': 1440, 'height': 900}, {'width': 390, 'height': 844}
SENTENCE = 'Weekend UCC evenings need a 2nd Consultant'


def two_gaps(h, name):
    """A draft with two empty sessions, the cause's two places to fill."""
    w = h.shift(h.workspace(name), people=[])['workspace']
    w = h.shift(w, start='2026-10-06T08:00:00+03:00', end='2026-10-06T12:00:00+03:00', people=[])['workspace']
    return w, [s['id'] for s in sorted(h.detail(w)['shifts'], key=lambda s: s['start'])]


def with_gaps(page, w, shifts):
    """The served Fix list, its first must-fix cause now about the two sessions in the server's plain words."""
    body = serve(page, w['id'])
    gap = cause(1, 'must', 2, SENTENCE, '4–6 Oct 2026 · People of a kind per session', 'assign',
                safety=True, acknowledgeable=False, shift_ids=shifts, detail_line='2 shifts · each has 1 of 2 Consultants')
    gap['action']['scope'].update(shift_ids=shifts, shift_id=shifts[0])
    gap['fixes'] = [{**gap['action'], 'available': True, 'unavailable_reason': None}]
    gap['where'] = {'label': 'Weekend UCC evenings'}  # already in the sentence: the line does not repeat it
    body['causes'][0] = gap
    page.route(f'**/api/workspaces/{w["id"]}/fix-list*', lambda r: fix_list_reply(r, body))
    page.reload()
    return body


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_fix_list_opens_beside_the_planner_with_plain_rows_and_chips(browser, live_server, h, scheme):
    w, shifts = two_gaps(h, 'Fix list panel')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP, color_scheme=scheme)
    try:
        with_gaps(page, w, shifts)
        open_rota(page, w['id'])
        fix_pill(page, 'err').click()
        page.locator('.popover.fix-pop').get_by_role('button', name=re.compile(r'^Open all in Fix list \(\d+\)$')).click()
        panel = page.locator('.dock-panel.fix-side')
        expect(panel).to_be_visible()
        # One place: the schedule stays beside it (no page jump).
        expect(page.locator('#main .planner-main')).to_be_visible()
        assert 'schedule/issues' not in page.url
        must = panel.locator('section[data-level=must]')
        row = must.locator('li[data-cause=c1]')
        expect(row.locator('.fix-row strong').first).to_contain_text(SENTENCE)
        expect(row.locator('.fix-safety')).to_have_text('Safety')
        expect(row.locator('.fix-meta')).to_have_text('2 shifts · each has 1 of 2 Consultants')
        expect(panel).not_to_contain_text(re.compile(r'\d+ checks?\b'))
        # S5: no native title. A cut line's whole text is the shared hover card's (data-ctip), and with keyboard focus in
        # the row (its Details button) the line shows all of itself.
        meta = row.locator('.fix-meta')
        assert meta.get_attribute('title') is None
        expect(meta).to_have_attribute('data-ctip', '2 shifts · each has 1 of 2 Consultants')
        assert panel.locator('[title]').count() == 0
        row.locator('.fix-row .fix-more').focus()
        assert meta.evaluate('n=>getComputedStyle(n).whiteSpace') == 'normal'
        page.keyboard.press('Shift+Tab')
        # Where it is: one chip per session, the first in focus; its fix is in place, the day on one line.
        chips = row.locator('.fix-chips .fix-chip')
        expect(chips).to_have_count(2)
        expect(chips.first).to_have_attribute('aria-pressed', 'true')
        expect(row.locator('.fix-fill-box .fix-fill-day')).to_have_text(re.compile(r'^\w{3} 4 Oct$'))
        expect(page.locator('#main .cause-ring')).not_to_have_count(0)  # the session in focus is ringed on the schedule
        # Could improve opens collapsed, behind its own Show.
        could = panel.locator('section[data-level=could]')
        toggle = could.get_by_role('button', name='Show')
        expect(toggle).to_have_attribute('aria-expanded', 'false')
        expect(could.locator('.fix-row').first).to_be_hidden()
        toggle.click()
        expect(could.locator('.fix-row').first).to_be_visible()
        shot(page, 'fixlist-panel', DESKTOP, scheme)
        # × closes it; the schedule is still there.
        panel.get_by_role('button', name='Close panel').click()
        expect(panel).to_have_count(0)
        expect(page.locator('#main .planner-main')).to_be_visible()
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_rota_tools_and_home_open_the_same_panel(browser, live_server, h):
    w, shifts = two_gaps(h, 'Fix list panel entries')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        with_gaps(page, w, shifts)
        open_rota(page, w['id'])
        page.locator('.topbar').get_by_role('button', name='Rota tools').click()
        page.get_by_role('menuitem', name=re.compile('^Fix list')).click()
        expect(page.locator('.dock-panel.fix-side li[data-cause=c1]')).to_be_visible()
        assert 'schedule/issues' not in page.url
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_phone_fix_list_page_has_the_same_rows_and_assign(browser, live_server, h, scheme):
    """D10 exception (FIXLIST-UX): a coordinator assigns from the phone's Fix list; the rest of planning stays desktop."""
    w, shifts = two_gaps(h, 'Fix list phone assign')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=PHONE, color_scheme=scheme)
    try:
        with_gaps(page, w, shifts)
        navigate(page, f'schedule/issues?draft={w["id"]}')
        row = page.locator('#main .fix-page li[data-cause=c1]')
        expect(row.locator('.fix-chips .fix-chip')).to_have_count(2)
        row.locator('.fix-chips .fix-chip').first.click()  # candidates only when asked for
        expect(row.locator('.fix-fill-box').get_by_role('button', name=re.compile(r'^Assign ')).first).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth') <= PHONE['width']
        shot(page, 'fixlist-phone', PHONE, scheme)
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_a_must_fix_markers_fix_opens_its_cause_in_the_panel(browser, live_server, h):
    """D78 ↔ FIXLIST-UX: a must-fix marker's Fix opens the Fix list panel at the cause holding that session (the same
    item), with that session ringed on the schedule. Real findings: a two-place session with one person, checked by
    the server; its Fix list is the server's own (no page.route)."""
    from .test_browser_team_box import fixture, day, open_view, issues_dialog, SOLO
    w, ids = fixture(h)
    two = h.configure('assignment_types', {'name': 'Fictional two-place marker clinic', 'starts_on': '2020-01-01',
                                           'definition': {'requirements': [{'minimum': 2}]}})['record']['id']
    a, b = day(5, '13:00', '17:00')
    w = h.shift(w, a, b, typ=two, people=[{'person_id': ids[SOLO], 'start': a, 'end': b}])['workspace']
    w = h.lifecycle(w, 'validate')['workspace']
    shift = next(s['id'] for s in h.detail(w)['shifts'] if s['assignment_type_id'] == two)
    fixes = h.get('coordinator', f'/api/workspaces/{w["id"]}/fix-list')
    cause = next(c for c in fixes['causes'] if c['level'] == 'must' and shift in c['shift_ids'])
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        open_view(page, w, 'Week')
        marker = page.locator(f'#schedule-view [data-session~="{shift}"] .wk-open-row .iss-mark')
        expect(marker).to_have_class(re.compile(r'\berror\b'))
        marker.click()
        # The one issue list (issue_marker.ts): a must fix's way to its fix is Open all in Fix list.
        issues_dialog(page).get_by_role('button', name=re.compile(r'^Open all in Fix list \(\d+\)$')).click()
        panel = page.locator('.dock-panel.fix-side')
        expect(panel).to_be_visible()
        assert '#schedule/issues' not in page.url
        expect(panel.locator(f'li[data-cause="{cause["id"]}"]')).to_be_visible()
        # The same item: the marker's session is the one in focus, ringed on the schedule beside the panel.
        expect(page.locator(f'#schedule-view [data-session~="{shift}"].cause-ring').first).to_be_visible()
        expect(page.locator('#schedule-view')).to_be_visible()
    finally:
        context.close()


def real_gaps(h, name):
    """Two two-place sessions with one person each, checked by the server: one must-fix cause, Assign its best fix."""
    from .test_browser_team_box import fixture, day, SOLO
    _, ids = fixture(h)
    # Reuse the eligible people without the team draft's unrelated supervision findings.
    w = h.workspace(f'Fictional two-place gaps · {name}')
    two = h.configure('assignment_types', {'name': f'Fictional two-place clinic · {name}', 'starts_on': '2020-01-01',
                                           'definition': {'requirements': [{'minimum': 2}]}})['record']['id']
    for n in (4, 5):
        a, b = day(n, '13:00', '17:00')
        w = h.shift(w, a, b, typ=two, people=[{'person_id': ids[SOLO], 'start': a, 'end': b}])['workspace']
    w = h.lifecycle(w, 'validate')['workspace']
    gaps = [s['id'] for s in sorted(h.detail(w)['shifts'], key=lambda s: s['start']) if s['assignment_type_id'] == two]
    listed = h.get('coordinator', f'/api/workspaces/{w["id"]}/fix-list')
    gap = next(c for c in listed['causes'] if c['level'] == 'must' and set(gaps) <= set(c['shift_ids'])
               and {e['code'] for e in c['evidence']} == {'COVERAGE_GAP'})
    assert set(gap['shift_ids']) == set(gaps)
    assert gap['fixes'][0]['kind'] == 'assign' and gap['fixes'][0]['available'], gap['fixes']
    assert gap['detail_line'] and gap['session_findings'].get(gaps[0])
    return w, gaps, gap


def test_assign_in_place_updates_the_row_from_the_edits_fix_list_delta(browser, live_server, h):
    """FIXLIST-BACKEND §5: the Assign's own reply (fix_list_delta) takes the filled session out of its cause, so its
    chip becomes ✓ with who, and the focus moves to the cause's next open session, without waiting for the check."""
    from .test_browser_team_box import open_view
    w, gaps, gap = real_gaps(h, 'delta')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        open_view(page, w, 'Week')
        fix_pill(page, 'err').click()
        page.locator('.popover.fix-pop').get_by_role('button', name=re.compile(r'^Open all in Fix list \(\d+\)$')).click()
        row = page.locator(f'.dock-panel.fix-side li[data-cause="{gap["id"]}"]')
        expect(row.locator('.fix-meta')).to_contain_text(gap['detail_line'])
        chips = row.locator('.fix-chips .fix-chip')
        expect(chips).to_have_count(len(gap['shift_ids']))
        # The fixture has other sessions and causes: put this cause in focus at its 8 Oct session.
        first = row.locator('.fix-chip', has_text=re.compile(r'^\w{3} 8$'))
        first.click()
        expect(first).to_have_attribute('aria-pressed', 'true')
        expect(row.locator('.fix-fill-box .fix-fill-day')).to_have_text(re.compile(r' 8 Oct$'))
        assign = row.locator('.fix-fill-box').get_by_role('button', name=re.compile(r'^Assign '))
        expect(assign.first).to_be_visible()
        # Each candidate says the server's one short reason (fill-options reason_short) on one line, not its facts, and
        # the people who cannot work it stay in the session panel.
        box = row.locator('.fix-fill-box')
        expect(box.locator('.wk-cand .wk-reason').first).to_have_text(re.compile(r'\S'))
        expect(box.locator('.wk-facts')).to_have_count(0)
        expect(box.locator('.wk-blocked')).to_have_count(0)
        assert box.locator('.wk-cand .wk-reason').first.evaluate('n=>getComputedStyle(n).whiteSpace') == 'nowrap'
        assign.first.click()
        # ✓ only ever comes from the reply's delta (the server says the session left the cause).
        done = row.locator('.fix-chip.done')
        expect(done).to_have_count(1)
        expect(done).to_have_accessible_name(re.compile(r'fixed, .+ assigned$'))
        expect(row.locator('.fix-chip.on')).to_have_attribute('aria-pressed', 'true')
        expect(row.locator('.fix-fill-box .fix-fill-day')).to_have_text(re.compile(r' 9 Oct$'))
        filled = next(s for s in h.detail(w)['shifts'] if s['id'] == gaps[0])
        assert len(filled['participants']) == 2
        # The gate's counts are the delta's summary: a recheck is in flight, said, never blank.
        expect(page.locator('.dock-panel.fix-side .fix-gate')).to_contain_text('must fix')
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_phone_page_applies_the_edits_fix_list_delta_without_reading_again(browser, live_server, h, scheme):
    """The phone's Fix list page (D10 exception) uses the same in-place update as the panel: after an Assign the
    reply's fix_list_delta turns the session's chip into ✓, and the page draws again without reading the Fix list."""
    w, gaps, gap = real_gaps(h, 'phone delta')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=PHONE, color_scheme=scheme)
    reads = []
    page.on('request', lambda r: reads.append(r.url) if '/fix-list' in r.url and r.method == 'GET' else None)
    try:
        navigate(page, f'schedule/issues?draft={w["id"]}')
        row = page.locator(f'#main .fix-page li[data-cause="{gap["id"]}"]')
        first = row.locator('.fix-chip', has_text=re.compile(r'^\w{3} 8$'))
        first.click()
        expect(first).to_have_attribute('aria-pressed', 'true')
        assign = row.locator('.fix-fill-box').get_by_role('button', name=re.compile(r'^Assign '))
        expect(assign.first).to_be_visible()
        before = len(reads)
        assign.first.click()
        expect(row.locator('.fix-chip.done')).to_have_count(1)
        expect(row.locator('.fix-fill-box .fix-fill-day')).to_have_text(re.compile(r' 9 Oct$'))
        assert len(reads) == before, reads[before:]
        assert page.evaluate('document.documentElement.scrollWidth') <= PHONE['width']
        shot(page, 'fixlist-phone-delta', PHONE, scheme)
    finally:
        context.close()


def test_no_eligible_people_offers_who_can_work_it_and_where_names_the_chips(browser, live_server, h):
    """Server fields that make a row clearer: with nobody eligible (eligible_people 0) the server's best fix is "Who can
    work it", never Assign; a shared pattern (where) the sentence does not say leads the line under it."""
    w, shifts = two_gaps(h, 'Fix list no eligible')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        body = serve(page, w['id'])
        nobody = cause(1, 'must', 2, 'Nobody eligible can cover 2 sessions.', '4–6 Oct 2026', 'open_eligibility',
                       shift_ids=shifts, detail_line='2 shifts affected', eligible_people=0,
                       where={'label': 'Weekday Physical Urgent Care Day shifts'})
        nobody['action']['scope']['shift_ids'] = shifts
        nobody['fixes'] = [{**nobody['action'], 'available': True, 'unavailable_reason': None},
                           {'kind': 'assign', 'scope': {'shift_ids': shifts, 'shift_id': shifts[0]}, 'available': False,
                            'unavailable_reason': 'No eligible people are recorded for these sessions'}]
        body['causes'][0] = nobody
        page.route(f'**/api/workspaces/{w["id"]}/fix-list*', lambda r: fix_list_reply(r, body))
        page.reload()
        open_rota(page, w['id'])
        fix_pill(page, 'err').click()
        page.locator('.popover.fix-pop').get_by_role('button', name=re.compile(r'^Open all in Fix list \(\d+\)$')).click()
        row = page.locator('.dock-panel.fix-side li[data-cause=c1]')
        expect(row.locator('.fix-meta')).to_have_text('Weekday Physical Urgent Care Day shifts · 2 shifts affected')
        expect(row.locator('.fix-chips')).to_have_attribute('aria-label', 'Sessions: Weekday Physical Urgent Care Day shifts')
        expect(row.get_by_role('button', name='Who can work it', exact=True)).to_be_visible()
        expect(row.get_by_role('button', name=re.compile(r'^Assign'))).to_have_count(0)
        expect(row.locator('.fix-fill-box')).to_have_count(0)
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()
