"""Insights › Workload and › Exports when part of a version fails to load (audit I-12, I-13): the tabs and the version
picker stay, the failure is said where it happened, and there is a way on (Retry, Show the latest version, the other
downloads)."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, wait_held  # noqa: F401
from .test_browser_insights_system import two_versions, source

pytestmark = pytest.mark.browser


def test_a_failing_report_keeps_the_tabs_and_picker_and_recovers(browser, live_server, h):
    pubs = two_versions(h)
    older = next(p for p in pubs if p['sequence'] == 1)
    latest = next(p for p in pubs if p['sequence'] == 2)
    failing = re.compile(r'/api/reports/' + older['id'])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'insights/workload')
        expect(page.locator('[data-table="workload"]')).to_be_visible()
        page.route(failing, lambda route: route.fulfill(status=500, json={'detail': 'Report failed'}))
        source(page).select_option(older['id'])
        main = page.locator('#main')
        expect(main.get_by_role('alert')).to_contain_text('Couldn’t load this version’s report.')
        # Still on the page: the tabs, the picker (on the failing version) and the loaded marker are all there.
        expect(page.get_by_role('tablist', name='Insights').get_by_role('tab')).to_have_text(['Fairness', 'Workload', 'Exports'])
        expect(source(page)).to_have_value(older['id'])
        expect(main).to_have_attribute('data-loaded-tab', 'workload')
        # Retry asks again; once the report answers, the table is back on the same version.
        page.unroute(failing)
        main.get_by_role('button', name='Retry', exact=True).click()
        expect(page.locator('[data-table="workload"]')).to_be_visible()
        expect(source(page)).to_have_value(older['id'])
        # Failing again: Show the latest version leaves the failing one.
        page.route(failing, lambda route: route.fulfill(status=500, json={'detail': 'Report failed'}))
        source(page).select_option(latest['id'])
        expect(page.locator('[data-table="workload"]')).to_be_visible()
        source(page).select_option(older['id'])
        main.get_by_role('button', name='Show the latest version', exact=True).click()
        expect(source(page)).to_have_value(latest['id'])
        expect(page.locator('[data-table="workload"]')).to_be_visible()
    finally:
        context.close()


def test_exports_draw_every_download_when_the_versions_people_fail(browser, live_server, h):
    pubs = two_versions(h)
    latest = next(p for p in pubs if p['sequence'] == 2)
    context, page = sign_in(browser, live_server, 'coordinator')
    page.route(re.compile(r'/api/publications/' + latest['id'] + r'(\?|$)'), lambda route: route.fulfill(status=500, json={'detail': 'Snapshot failed'}))
    try:
        navigate(page, 'insights/exports')
        expect(source(page)).to_have_value(latest['id'])
        # Only the Personal calendar row depends on the version's people: it says so; the other rows still download.
        calendar = page.locator('#main .export-row').filter(has_text='Personal calendar')
        expect(calendar).to_contain_text('Couldn’t load the people in this version.')
        expect(calendar.get_by_role('button', name='Download Personal calendar', exact=True)).to_be_disabled()
        expect(page.locator('#main')).not_to_contain_text('Cannot convert')
        with page.expect_download() as got:
            page.get_by_role('button', name='Download Duties CSV', exact=True).click()
        assert got.value.failure() is None and got.value.suggested_filename == 'published-assignments.csv'
    finally:
        context.close()


def test_personal_calendar_download_waits_for_a_person(browser, live_server, h):
    """I-30, B7 exports: with several people and none chosen, Download is disabled and gives the reason to screen
    readers, with no red text before anything is chosen; the picker's placeholder says what is needed."""
    me = h.get('staff', '/api/me')['person_id']
    other = next(p for p in h.seed['people'] if p != me)
    two_versions(h, people=[me, other])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'insights/exports')
        calendar = page.locator('#main .export-row').filter(has_text='Personal calendar')
        person = calendar.get_by_label('Person', exact=True)
        expect(person).to_have_value('')
        expect(person.locator('option[value=""]')).to_have_text('Choose a person')
        download = calendar.get_by_role('button', name='Download Personal calendar', exact=True)
        expect(download).to_be_disabled()
        # Idle is calm: nothing red; the disabled Download carries the reason for screen readers.
        expect(page.locator('#main .field-error, #main .notice.error')).to_have_count(0)
        expect(download).to_have_accessible_description('Choose a person to download.')
        why = calendar.locator('.sr-only').filter(has_text='Choose a person to download.')
        # The other downloads do not wait for the person picker.
        expect(page.get_by_role('button', name='Download Workload CSV', exact=True)).to_be_enabled()
        person.select_option(other)
        expect(download).to_be_enabled()
        expect(why).to_have_count(0)
        expect(download).not_to_have_attribute('aria-describedby', re.compile('.'))
        with page.expect_download() as got:
            download.click()
        assert got.value.suggested_filename == 'personal-rota.ics'
    finally:
        context.close()


def test_exports_are_usable_while_the_versions_people_are_still_loading(browser, live_server, h):
    """I-13 (review maintenance): the downloads do not wait for the version's people. With that read held, the page is shown
    and another download works; the Personal calendar row fills in once the people arrive."""
    pubs = two_versions(h)
    latest = next(p for p in pubs if p['sequence'] == 2)
    held = []
    snapshot = re.compile(r'/api/publications/' + latest['id'] + r'(\?|$)')
    context, page = sign_in(browser, live_server, 'coordinator')
    page.route(snapshot, lambda route: held.append(route))
    try:
        page.evaluate("location.hash = 'insights/exports'")  # navigate() would wait for the held read
        wait_held(page, held)
        expect(page.locator('#main')).to_have_attribute('data-loaded-tab', 'exports')
        calendar = page.locator('#main .export-row').filter(has_text='Personal calendar')
        expect(calendar).to_contain_text('Loading the people in this version')
        with page.expect_download() as got:
            page.get_by_role('button', name='Download Duties CSV', exact=True).click()
        assert got.value.failure() is None and got.value.suggested_filename == 'published-assignments.csv'
        held.pop().continue_()
        expect(calendar.get_by_label('Person', exact=True)).to_be_visible()
    finally:
        context.close()
