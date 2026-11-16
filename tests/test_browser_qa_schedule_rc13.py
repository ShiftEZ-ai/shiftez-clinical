"""Schedule QA before rc.13: an incomplete auto-schedule proposal shows the same score as the footer will (before the
blocking-issue cap) with its blocking count in its own tile; the selection bar's open More button stays visible; a filter popover
opens at its first filter; read-only viewers are not told to add a session."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, open_rota  # noqa: F401
from .test_browser_cp_sat import cp_server, open_scheduling  # noqa: F401
from .test_browser_rule_catalogue import shot
from .test_autoschedule_backend_audit import run_job

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('occupied,prefilled,expected_before,expected_saved,expected_delta', [
    (0, 0, 0, 84.1, 84.1),
    (4, 0, 54.5, 68.9, 14.4),
    (4, 2, 65.2, 68.9, 3.7),
])
def test_an_incomplete_proposal_shows_the_footer_score_and_its_blocking_issues(
        browser, cp_server, h, occupied, prefilled, expected_before, expected_saved, expected_delta):
    typ = h.configure('assignment_types', {'name': 'Four required places', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 4, 'maximum': 4}]}})['record']['id']
    workspace = h.workspace('Incomplete proposal')
    for day in range(4, 4 + occupied):
        workspace = h.shift(workspace, start=f'2026-10-{day:02}T08:00:00+03:00',
                            end=f'2026-10-{day:02}T12:00:00+03:00',
                            people=[h.seed['people'][0]])['workspace']
    # Empty preserves the legacy ledger; two prefilled places also exercise a capped baseline and uncapped gain.
    workspace = h.shift(workspace, typ=typ, people=h.seed['people'][1:1 + prefilled],
                        start='2026-10-08T08:00:00+03:00', end='2026-10-08T10:00:00+03:00')['workspace']
    job = run_job(h, workspace, diagnostic_proposal=True)
    summary = job['result']['summary']
    saved, before = summary['score_saved'], summary['score_before']
    assert job['result']['status'] == 'candidate_with_gaps' and saved['capped'] and saved['uncapped_score'] != saved['score']
    assert (saved['uncapped_score'], saved['score']) == (expected_saved, 59)
    assert (before['uncapped_score'], before['score'], before['capped']) == (
        expected_before, min(expected_before, 59), expected_before > 59)
    context, page = sign_in(browser, cp_server, 'coordinator')
    try:
        navigate(page, 'planner')
        open_scheduling(page)
        open_rota(page, workspace['id'])
        tile = page.locator('#auto-panel .auto-stat').filter(has_text='Score')
        # The change is between the two shown (uncapped) scores (maintenance, maintenance), even when neither side is publishable:
        # never the capped one (59 − 0, 59 − 54.5, 59 − 59 here).
        delta = summary['score_delta']
        assert delta == expected_delta == round(saved['uncapped_score'] - before['uncapped_score'], 1) > 0
        assert not before['publishable']
        # A3/A7 (TEST-REPAIR): while anything must be fixed the result shows the score only as that change, never a
        # standalone score.
        expect(tile).to_have_text(re.compile(r'^Score\s*\+' + re.escape(f'{delta:g}') + r'\s*vs now$'))
        # The blocking causes are said beside it in their own tile, never folded into the score.
        must = job['result']['fix_list_after']['summary']['must']
        assert must and saved['blocking_count']
        expect(page.locator('#auto-panel .auto-stat').filter(has_text='Must fix').locator('b')).to_have_text(str(must))
        shot(page, f'incomplete-proposal-uncapped-baseline-{occupied}-{prefilled}', full_page=False)
    finally:
        context.close()


def test_the_open_more_button_in_the_selection_bar_stays_visible(browser, live_server, h):
    w = h.shift(h.shift(h.workspace('Selection more'), people=[])['workspace'], people=[])['workspace']  # two sessions
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        page.get_by_role('button', name=re.compile('^Actions for 4 Oct')).first.click()
        page.get_by_role('menuitem', name=re.compile('^Select these')).click()
        more = page.locator('.selection-bar .sel-more')
        more.click()
        expect(more).to_have_attribute('aria-expanded', 'true')
        # The bar is dark ink on light: the open button keeps the bar's text colour (it was the accent, invisible).
        same = page.evaluate("""() => {const b=document.querySelector('.selection-bar'),m=b.querySelector('.sel-more');
            return getComputedStyle(m).color===getComputedStyle(b).color;}""")
        assert same
    finally:
        context.close()


def test_the_schedule_filter_opens_at_its_first_filter(browser, live_server, h):
    w = h.shift(h.workspace('Filter focus'), people=[])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        page.locator('.planner-toolbar').get_by_role('button', name='Filter', exact=True).click()
        # SR-06: the two Only switches sit above the shared condition builder; the first of them is the first filter.
        first = page.locator('.popover').last.get_by_label('Only sessions with open places', exact=True)
        expect(first).to_be_focused()  # not the last checkbox, which scrolled a tall panel past the first filter
        assert page.evaluate("[...document.querySelectorAll('.popover')].pop().scrollTop") == 0
    finally:
        context.close()


def test_read_only_viewers_are_not_told_to_add_a_session(browser, live_server, h):
    w = h.workspace('Empty draft')
    for role, told in (('auditor', False), ('coordinator', True)):
        context, page = sign_in(browser, live_server, role)
        try:
            navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
            empty = page.locator('.wk-empty')
            expect(empty).to_contain_text('No sessions this week')
            # A coordinator is pointed to Add session; a read-only viewer is not.
            expect(empty.get_by_role('button', name='Add session', exact=True)).to_be_visible() if told else expect(empty).not_to_contain_text('Add session')
        finally:
            context.close()


def test_a_link_to_a_published_only_week_shows_the_published_rota_not_a_draft_elsewhere(browser, live_server, h):
    h.publish(h.shift(h.workspace('Published week'))['workspace'])  # 4–10 Oct, one session
    h.shift(h.workspace('Later draft', '2026-10-18', '2026-10-25'), start='2026-10-18T08:00:00+03:00',
            end='2026-10-18T12:00:00+03:00')  # the newest draft, another week
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule/week/2026-10-04')
        expect(page.locator('header').first).not_to_contain_text('Later draft')
        names = {p['id']: p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people']}
        expect(page.locator('#schedule-view')).to_contain_text(names[h.seed['people'][0]])
        expect(page.get_by_role('contentinfo')).to_contain_text('1 session in view')  # the published session counts
        assert 'edit=none' in page.url
    finally:
        context.close()


def test_a_link_to_a_day_opens_the_draft_that_covers_it(browser, live_server, h):
    h.workspace('Earlier draft')  # 4–10 Oct
    h.workspace('Covering draft', '2026-10-11', '2026-10-18')
    h.workspace('Newest draft elsewhere', '2026-10-25', '2026-11-01')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule/week/2026-10-12')
        expect(page.locator('header').first).to_contain_text('Covering draft')
        navigate(page, 'schedule/week/2026-10-05')  # another dated link chooses again, by its own day
        expect(page.locator('header').first).to_contain_text('Earlier draft')
    finally:
        context.close()


def test_several_drafts_covering_a_day_open_the_newest_and_count_the_others(browser, live_server, h):
    h.workspace('Older cover')
    h.workspace('Newer cover')  # same days, created last
    h.workspace('Elsewhere', '2026-10-25', '2026-11-01')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule/week/2026-10-04')
        expect(page.locator('header').first).to_contain_text('Newer cover')
        expect(page.get_by_role('contentinfo')).to_contain_text(re.compile(r'1\s*other draft'))
    finally:
        context.close()
