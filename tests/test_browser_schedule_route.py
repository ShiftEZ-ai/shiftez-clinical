"""The Schedule keeps its view and day in the address, but never takes the address back from a navigation already asked
for: a redraw that finishes after the hash changed (before its event is handled) used to put #schedule back, so the
navigation landed on the Schedule."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401

pytestmark = pytest.mark.browser


def test_a_schedule_redraw_never_takes_back_an_address_already_changed(browser, live_server, h):
    w = h.shift(h.workspace('Address race'))['workspace']
    h.lifecycle(w, 'validate')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule')
        bar = page.locator('.planner-toolbar')
        # Visit the next week and come back: both weeks are now read, so a move between them redraws from memory only
        # (no request), finishing in the same turn as the click, before the browser reports a hash change.
        for name in ('Next week', 'Previous week'):
            bar.get_by_role('button', name=name, exact=True).click()
            expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
        page.evaluate("""() => { document.querySelector('.planner-toolbar button[aria-label="Next week"]').click();
                                 location.hash = 'rules'; }""")
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'rules')
        assert page.evaluate('location.hash') == '#rules'
    finally:
        context.close()


def test_unknown_schedule_view_address_says_so(browser, live_server, h):
    """LIVE-35: an address naming no view says so; an old name (counts) opens its view."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule/nonsense/2026-10-05')
        expect(page.locator('.toast').filter(has_text='There is no “nonsense” view')).to_be_visible()
        navigate(page, 'schedule/counts/2026-10-05')
        expect(page.get_by_role('button', name=re.compile(r'^View settings: Coverage'))).to_be_visible()
    finally:
        context.close()


def test_rota_switcher_groups_editable_and_in_review(browser, live_server, h):
    """SR-01: the switcher's rotas are grouped Drafts / In review / Published, not one list called Drafts."""
    editable = h.workspace('Group editable', '2026-10-04', '2026-10-11')
    review = h.shift(h.workspace('Group in review', '2026-10-11', '2026-10-18'),  # a session inside its own dates
                     start='2026-10-12T08:00:00+03:00', end='2026-10-12T12:00:00+03:00')['workspace']
    h.lifecycle(review, 'submit')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-05?edit={editable['id']}")
        page.get_by_role('button', name=re.compile(r'^(Switch draft · |Choose a draft)')).click()
        picker = page.get_by_role('dialog', name='Drafts', exact=True)
        expect(picker).to_contain_text('In review')
        expect(picker.get_by_role('option').filter(has_text='Group in review')).to_contain_text('Waiting for approval')
    finally:
        context.close()


def test_undated_open_says_why_this_draft(browser, live_server, h):
    """SR-03: the rota the page opened by itself says why in the switcher."""
    h.workspace('Opened by itself', '2026-10-04', '2026-10-11')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule/week/2026-10-05')
        page.get_by_role('button', name=re.compile(r'^(Switch draft · |Choose a draft)')).click()
        option = page.get_by_role('dialog', name='Drafts', exact=True).get_by_role('option').filter(has_text='Opened by itself')
        expect(option).to_contain_text('Opened: covers the linked day')
    finally:
        context.close()
