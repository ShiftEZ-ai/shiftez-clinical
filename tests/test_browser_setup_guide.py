"""Data health stays reachable (audit C-24, C-27, C-33): once setup is done the guide is still first in Configuration,
named Data health with what is left to fix; the Opening hours step opens Services filtered to the clinics without hours; an item
whose setup step is not done yet (no ranks, no clinics) is not listed as a data problem; an empty list that needs another
kind first (recurring sessions need duty types) offers that step instead of a Create that cannot be saved (C-28)."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401
from .test_browser_configuration_shell import expect_coordinator_sections
from .test_browser_w3_setup_today import finish_required_steps, empty_department, waiting_rule
from .test_resources_calendars import update

pytestmark = pytest.mark.browser


def test_data_health_stays_first_and_set_hours_opens_the_clinics_without_hours(browser, live_server, h):
    # Opening hours is done only when every clinic has them (8df451143): setup done needs them all, so the one thing to
    # fix is a rule waiting for approval; a clinic losing its hours then reopens the step, whose Set opening hours lists it.
    finish_required_steps(h, clinic_hours=True)
    waiting_rule(h)
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'configuration')
        expect(page).to_have_url(re.compile(r'#configuration/assignment_types$'))  # setup done: opens on a list
        health = page.locator('.config-nav [data-section=setup]')
        expect(health).to_have_accessible_name('Data health, 1 thing to fix')
        expect(health.locator('.nav-count')).to_have_text('1')
        health.click()
        expect(page.locator('.setup-hero h2')).to_have_text('Ready to plan · 1 thing to fix')
        clinic = next(s for s in h.get('admin', '/api/catalogues')['services'] if s.get('active') is not False)
        update(h, 'services', clinic, calendar_id=None)
        page.reload()
        expect(page.locator('.setup-hero h2')).to_have_text('Set up your department')
        step = page.locator('.setup-step').filter(has=page.get_by_text('Opening hours', exact=True))
        expect(step).to_contain_text('1 service without opening hours')
        # Said once: the unfinished step says it; Data health does not list it again beside it.
        expect(page.locator('.setup-health')).not_to_contain_text('opening hours')
        step.get_by_role('button', name='Set opening hours', exact=True).click()
        expect(page).to_have_url(re.compile(r'#configuration/services'))
        rows = page.locator('.config-pane[data-kind=services] .dt-body tbody tr')
        expect(rows).to_have_count(1)  # only the clinic without opening hours
        expect(rows.filter(has_text=clinic['name']).filter(has_text='Not set')).to_have_count(1)
    finally:
        context.close()


def test_coordinator_keeps_data_health_beside_tags_on_a_phone(browser, live_server, h):
    finish_required_steps(h, clinic_hours=True)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 390, 'height': 844})
    page.emulate_media(color_scheme='dark')
    try:
        navigate(page, 'configuration/tags')
        nav = page.locator('.config-nav')
        expect_coordinator_sections(page, h)
        expect(nav.locator('[data-section=auto_schedule]')).to_have_count(1)
        expect(nav.locator('[data-section=setup]')).to_have_text('Data health')
        expect(nav.locator('[data-section=scheduling_groups]')).to_have_count(1)
        expect(nav.locator('[data-section=tags]')).to_have_attribute('aria-current', 'page')
    finally:
        context.close()


def test_health_lists_no_problem_whose_setup_step_is_not_done(browser, live_server):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'configuration/setup')
        titles = page.evaluate("""async () => { const {dataHealth} = await window.importBundle('setup_guide.js');
          const ctx = c => ({me: {organisation: {timezone: 'Asia/Riyadh'}}, catalogues: c});
          const open = {catalogue() {}, page() {}};
          const person = {id: 'p1', display_name: 'Fictional Person'};
          return [dataHealth(ctx({people: [person]}), open).map(i => i.title),
                  dataHealth(ctx({people: [person], categories: [{id: 'r'}], services: [{id: 's', name: 'Clinic', calendar_id: 'c'}]}), open).map(i => i.title)]; }""")
        assert titles[0] == []  # no ranks or clinics yet: those steps say so, not Data health
        assert titles[1] == ['People without a current rank', 'People not eligible for any clinic']
    finally:
        context.close()


def test_empty_recurring_sessions_offer_the_missing_step_not_create(browser, live_server):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'configuration/recurring_series')
        empty_department(page)
        state = page.locator('.config-pane[data-kind=recurring_series] .empty-state')
        expect(state).to_contain_text('No duty types yet.')
        expect(state.get_by_role('button', name='New recurring session')).to_have_count(0)
        state.get_by_role('button', name='Add a duty type', exact=True).click()
        expect(page).to_have_url(re.compile(r'#configuration/assignment_types$'))
    finally:
        context.close()
