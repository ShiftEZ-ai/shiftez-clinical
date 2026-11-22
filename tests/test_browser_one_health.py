"""One rota health in the footer (UIUX-AUDIT C3): the published rota's Must fix and Could improve are the draft's pills
(fix_list.ts fixPills): the same shapes, tones and words. Could improve is the D78 gold circle on both, never blue;
sessions short of people are Must fix, never a separate "need staff" pill. Real checks, no routed counts."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, open_rota  # noqa: F401
from .test_browser_qa_planner_a2 import settled

pytestmark = pytest.mark.browser


def could_pill(page):
    pill = page.locator('.app-footer .health-pill.could')
    expect(pill).to_be_visible()
    # A check still running draws its spinner in place of the icon (fix_list.ts fixPills): read the settled pill.
    expect(pill).not_to_have_attribute('aria-busy', 'true')
    return pill


def look(pill):
    """What a reader sees of a pill: its tone classes, whether it has the issue icon, and its words."""
    return pill.evaluate("b=>({tone:[...b.classList].filter(c=>c!=='health-pill').sort().join(' '),icon:!!b.querySelector('svg'),"
                         "label:(b.querySelector('.label')||b).textContent.trim()})")


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_published_and_draft_footers_draw_the_same_could_improve(browser, live_server, h, scheme):
    h.policy({'kind': 'max_duties', 'maximum': 0, 'window': 'day'}, {'person_id': h.seed['people'][0]}, severity='advisory')  # an advisory finding on each rota
    published = h.shift(h.workspace('One health published'))['workspace']
    h.lifecycle(published, 'validate')
    h.publish(h.detail(published)['workspace'])
    draft = h.shift(h.workspace('One health draft', start='2026-10-11', end='2026-10-18'),
                    start='2026-10-11T08:00:00+03:00', end='2026-10-11T12:00:00+03:00')['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', color_scheme=scheme)
    try:
        navigate(page, 'schedule/week/2026-10-04?edit=none')
        settled(page)
        footer = page.locator('.app-footer')
        expect(footer).not_to_contain_text('need staff')
        expect(footer.locator('.health-pill.info').filter(has_text='could improve')).to_have_count(0)
        on_published = look(could_pill(page))
        assert on_published['icon'] and 'warn' in on_published['tone'].split(), on_published
        # It opens the published check's warnings as the one issue list (live check F3; no Fix list on a published rota).
        could_pill(page).click()
        expect(page.locator('.popover.issue-pop .iss-row').first).to_be_visible()
        page.keyboard.press('Escape')
        open_rota(page, draft['id'])
        on_draft = look(could_pill(page))
        assert on_draft == on_published, (on_draft, on_published)  # one pill: same tone, icon and words
    finally:
        context.close()
