"""Schedule QA before rc.12: a copied rota takes its source's length; concurrent sessions stack in the Day view;
publishing shows the rota just published, not the next draft; the Day axis fits the sessions; Resolve issues says its
counts once and labels its row action; a copy with nothing to copy says so; approval mentions further reviews only when
the route has them; the footer counts the sessions in view."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, new_draft  # noqa: F401

pytestmark = pytest.mark.browser


def test_copying_a_previous_rota_sets_the_last_day_to_its_length(browser, live_server, h):
    week = h.workspace('One week', '2026-10-04', '2026-10-11')
    h.workspace('Two weeks', '2026-10-11', '2026-10-25')  # the latest: a new draft starts after it, 14 days long
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        new_draft(page)
        dialog = page.get_by_role('dialog', name='New rota', exact=True)
        first, last = dialog.get_by_label('First day', exact=True), dialog.get_by_label('Last day', exact=True)
        expect(first).to_have_value('Sun 25 Oct 2026')
        expect(last).to_have_value('Sat 7 Nov 2026')
        dialog.get_by_label('Start from', exact=True).select_option('previous')
        dialog.get_by_label('Previous rota', exact=True).select_option(week['id'])
        expect(last).to_have_value('Sat 31 Oct 2026')  # 7 days, as the source: the server refuses any other length
        expect(dialog.get_by_label('Previous rota', exact=True)).to_have_value(week['id'])  # a late auto-pick never overrides it
        expect(dialog).to_contain_text('7 days')
    finally:
        context.close()


def test_the_day_view_stacks_sessions_at_the_same_time_in_lanes(browser, live_server, h):
    w = h.workspace('Busy morning')
    for _ in range(3):
        w = h.shift(w, people=[])['workspace']  # three sessions, same clinic, 08:00–12:00
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/day/2026-10-04?edit={w['id']}")
        bars = page.locator('.sd-track .sd-bar')
        expect(bars).to_have_count(3)
        tops = sorted(round(b['y']) for b in (bars.nth(i).bounding_box() for i in range(3)))
        assert len(set(tops)) == 3 and tops[1] - tops[0] >= 30, tops  # one lane each, none on top of another
    finally:
        context.close()


def test_publishing_shows_the_rota_just_published_not_the_next_draft(browser, live_server, h):
    w = h.shift(h.workspace('Published now'))['workspace']
    h.workspace('Next draft', '2026-10-11', '2026-10-18')
    w = h.lifecycle(w, 'submit')['workspace']
    w = h.lifecycle(w, 'approve', 'approver')['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        page.get_by_role('button', name='Publish…', exact=True).click()
        publish = page.get_by_role('dialog').locator('button[type=submit]')
        publish.click()
        expect(publish).to_have_text(re.compile('Confirm publish'))
        publish.click()
        expect(page.locator('.toast').filter(has_text='Published · staff are told.')).to_be_visible(timeout=20000)
        expect(page.get_by_role('heading', name='Next draft')).to_have_count(0)
        names = {p['id']: p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people']}
        expect(page.locator('#schedule-view')).to_contain_text(names[h.seed['people'][0]])  # the published session
    finally:
        context.close()


def test_the_day_axis_fits_the_days_sessions(browser, live_server, h):
    w = h.shift(h.workspace('Morning only'), people=[])['workspace']  # 08:00–12:00
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/day/2026-10-04?edit={w['id']}")
        ticks = page.locator('.sd-axis span')
        expect(ticks.first).to_have_text('07:00')  # an hour before the first start
        assert ticks.count() <= 8 and ticks.last.inner_text() < '13:00', ticks.all_inner_texts()  # not a 24-hour axis
    finally:
        context.close()


def test_resolve_issues_header_rows_and_counts(browser, live_server, h):
    from .test_browser_resolve_issues import conflicted, issues_page
    w, _ = conflicted(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/issues?draft={w['id']}")
        page.locator('#main').get_by_role('button', name=re.compile('^Saved views')).click()
        page.get_by_role('menuitemradio', name=re.compile('^All checks')).click()
        main = issues_page(page)
        head = main.locator('.issues-head')
        expect(head.get_by_role('button', name='Back to schedule', exact=True)).to_be_visible()  # in the header, not a band
        expect(main.locator('tbody tr[data-row]').first.get_by_role('button', name='Show on schedule', exact=True)).to_be_visible()
        expect(page.locator('.app-footer')).not_to_contain_text(' open')  # the counts are said once, in the header
        main.locator('tbody tr[data-row]').first.get_by_role('checkbox').check()
        expect(page.locator('.app-footer')).to_contain_text('1 selected')
    finally:
        context.close()


def test_copying_a_rota_with_no_sessions_says_so(browser, live_server, h):
    empty = h.workspace('Nothing in it', '2026-10-04', '2026-10-11')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        new_draft(page)
        dialog = page.get_by_role('dialog', name='New rota', exact=True)
        dialog.get_by_label('Rota name', exact=True).fill('Copied empty rota')
        dialog.get_by_label('Start from', exact=True).select_option('previous')
        dialog.get_by_label('Previous rota', exact=True).select_option(empty['id'])
        dialog.get_by_label('Run auto-schedule after setup', exact=True).uncheck()
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.method == 'POST'
                                  and r.request.post_data_json.get('kind') == 'workspace.create') as created:
            dialog.get_by_role('button', name=re.compile('^Create draft')).click()
        assert created.value.ok, created.value.text()
        assert created.value.json()['setup']['created'] == 0
        expect(page.locator('.toast').filter(has_text='0 sessions copied (the previous rota has none)')).to_be_visible()
    finally:
        context.close()


def test_one_step_approval_does_not_promise_further_reviews_and_the_footer_counts_the_week(browser, live_server, h):
    w = h.shift(h.workspace('Two weeks', '2026-10-04', '2026-10-18'))['workspace']
    w = h.shift(w, '2026-10-12T08:00:00+03:00', '2026-10-12T12:00:00+03:00')['workspace']  # the second week
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        expect(page.locator('.app-footer .footer-total')).to_have_text('1 session in view')  # this week, not the whole rota
    finally:
        context.close()
    w = h.lifecycle(w, 'submit')['workspace']
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        page.get_by_role('button', name='Approve rota', exact=True).click()
        dialog = page.get_by_role('dialog', name='Approve rota', exact=True)
        expect(dialog).not_to_contain_text('Review step:')  # L11: the review strip already names the step
        expect(dialog).not_to_contain_text('more review step')
        expect(dialog).not_to_contain_text('further')
    finally:
        context.close()
