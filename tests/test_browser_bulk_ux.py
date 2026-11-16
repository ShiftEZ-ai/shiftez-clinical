"""Bulk changes from the DEV acceptance (rc.15 phase 2): the Change button says what it does (a "Preview" never applies),
Undo stays after the toast, a reviewed change ends on Results (never closes by itself), rows name their records, and a
new record's Preview shows the values it is created with. Desktop 1440 (bulk changes are desktop only), light and dark."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, condition, holding  # noqa: F401
from .test_browser import whole_list
from .test_browser_bulk_changes import DESKTOP, bar, caret, change_row, errors_of, panel, submit
from .test_browser_bulk_sheet import no_light_path

pytestmark = pytest.mark.browser


def services(h):
    return {s['id']: s for s in h.get('admin', '/api/catalogues')['services']}


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_apply_says_apply_preview_never_applies_and_results_stay_open(browser, live_server, h, scheme):
    clinic = h.seed['services'][0]
    tag = h.configure('tags', {'name': 'walkin', 'labels': ['services']})['record']
    h.command('admin', 'tag.members', {'add': [{'kind': 'services', 'id': clinic}], 'remove': [], 'reason': 'Test setup'}, tag['id'], tag['revision'])
    context, page = sign_in(browser, live_server, 'admin', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'configuration')
        page.get_by_role('button', name=re.compile(r'^Services')).click()
        shown = len(services(h))
        # Records picked by hand, under Most applied directly: the button says it applies, and it does.
        page.get_by_role('checkbox', name='Select all services shown').check()
        bar(page).get_by_role('button', name='Change…').click()
        change_row(page, 'Description', 'Set to').locator('input').fill('Walk-in, no appointment')
        foot = panel(page).locator('.bulk-form .bulk-foot')
        expect(foot.locator('.button.primary')).to_have_text(f'Apply {shown} changes')
        expect(foot).to_contain_text('Anything to resolve opens Preview first.')
        submit(page)
        expect(page.locator('.toast', has_text='changed')).to_be_visible()
        expect(panel(page)).to_have_count(0)
        assert all(s['description'] == 'Walk-in, no appointment' for s in services(h).values())
        # Past the toast, the list keeps the change's Undo.
        page.evaluate("document.querySelectorAll('.toast').forEach(t => t.remove())")
        page.locator('.app-footer').get_by_role('button', name='Undo last change').click()
        expect(page.locator('.toast', has_text='Undone')).to_be_visible()
        assert not any(s['description'] == 'Walk-in, no appointment' for s in services(h).values())
        expect(page.locator('tr.dt-changed')).to_have_count(0)
        bar(page).get_by_role('button', name='Clear selection').click()
        # Over Most applied directly (here 0): "Preview →", and Preview saves nothing.
        no_light_path(h)
        page.reload()
        page.get_by_role('button', name=re.compile(r'^Services')).click()
        caret(page.locator('#main')).get_by_role('menuitem', name='Select by condition…').click()
        pop = page.get_by_role('dialog', name='Select by condition')
        condition(pop, 'Clinic', ['#walkin'])
        pop.get_by_role('button', name='Select', exact=True).click()
        bar(page).get_by_role('button', name='Change…').click()
        change_row(page, 'Description', 'Set to').locator('input').fill('Reviewed first')
        expect(foot.locator('.button.primary')).to_have_text('Preview →')
        expect(foot.locator('.bulk-direct')).to_be_hidden()
        submit(page)
        expect(panel(page).get_by_role('heading', name='Preview — nothing saved yet')).to_be_visible()
        assert services(h)[clinic]['description'] != 'Reviewed first'
        name = services(h)[clinic]['name']
        expect(panel(page).locator('#bulk-preview, .dt-table').first).to_contain_text(name)  # the record by its name
        panel(page).get_by_role('button', name=re.compile(r'^Apply ready rows \(1\)')).click()
        # A reviewed change ends on Results, with its Undo, however clean; nothing closes it but the person.
        results = panel(page).get_by_role('heading', name='Results')
        expect(results).to_be_visible()
        # Still there well after it showed (the page's clock is run on, not a fixed wait): nothing closes it on a timer.
        page.clock.install()
        page.clock.run_for(5000)
        expect(results).to_be_visible()
        expect(panel(page).locator('tbody')).to_contain_text(name)
        expect(panel(page).locator('tbody')).not_to_contain_text(re.compile(r'\bRecord\b|\bRow \d'))
        assert services(h)[clinic]['description'] == 'Reviewed first'
        panel(page).get_by_role('button', name='Undo the whole change').click()
        expect(page.locator('.toast', has_text='Undone')).to_be_visible()
        assert services(h)[clinic]['description'] != 'Reviewed first'
        assert not errors, errors
    finally:
        context.close()


def test_settings_arriving_during_a_preview_never_turn_it_into_apply(browser, live_server, h):
    """The button clicked says Preview (the department's settings not read yet); the settings arrive while the dry run
    is out and turn the button into Apply. The click stays a Preview: the panel shows Preview and nothing is saved.
    The busy spinner that shows 300 ms after the click reads the label then (Applying…), not the stale Preview."""
    context, page = sign_in(browser, live_server, 'admin', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        with holding(page) as held:
            held.hold('**/api/bulk/settings')
            navigate(page, 'configuration')
            page.get_by_role('button', name=re.compile(r'^Services')).click()
            page.get_by_role('checkbox', name='Select all services shown').check()
            bar(page).get_by_role('button', name='Change…').click()
            change_row(page, 'Description', 'Set to').locator('input').fill('Never saved by a Preview')
            foot = panel(page).locator('.bulk-form .bulk-foot')
            button = foot.locator('.button.primary')
            expect(button).to_have_text('Preview →')
            held.hold('**/api/commands/preview')
            # Time stands still: the settings land before the 300 ms busy spinner, whatever the runner's speed.
            page.clock.pause_at(page.evaluate("new Date(Date.now()+1000).toISOString()"))
            with page.expect_request('**/api/commands/preview'):
                submit(page)
            expect(button).to_have_text('Preview →')
            held.release('**/api/bulk/settings')
            expect(foot.locator('.bulk-direct')).to_be_visible()  # the settings arrived (a stable sign, unlike the label)
            page.clock.run_for(400)
            expect(button).to_have_class(re.compile(r'\bis-busy\b'))
            expect(button).to_have_text(re.compile(r'^Applying \d+ change'))  # the spinner reads the label now, not at the click
            page.clock.resume()
            assert held['**/api/commands/preview']
            held.release('**/api/commands/preview')
            expect(panel(page).get_by_role('heading', name='Preview — nothing saved yet')).to_be_visible()
            assert not any(s['description'] == 'Never saved by a Preview' for s in services(h).values())
            assert not errors, errors
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_leave_records_are_named_by_person_and_dates(browser, live_server, h, scheme):
    no_light_path(h)
    types = h.get('admin', '/api/catalogues')['leave_types']
    person = h.seed['people'][0]
    h.command('coordinator', 'availability.record', {'person_id': person, 'kind': 'recorded_leave', 'leave_type_id': types[0]['id'],
              'start': '2027-01-04T00:00:00+03:00', 'end': '2027-01-04T23:00:00+03:00', 'private_reason': 'Imported record'})
    name = next(p['display_name'] for p in h.get('admin', '/api/catalogues')['people'] if p['id'] == person)
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        page.get_by_role('checkbox', name=re.compile('^Select all requests shown')).check()
        bar(page).get_by_role('button', name=re.compile(r'^Change')).click()
        row = change_row(page, re.compile('Leave type', re.I), 'Set to')
        row.locator('.bulk-value select').select_option(label=types[1]['name'])
        expect(panel(page).locator('.bulk-form .bulk-foot .button.primary')).to_have_text('Preview →')
        submit(page)
        table = panel(page).locator('tbody')
        expect(table).to_contain_text(name)
        expect(table).not_to_contain_text(re.compile(r'\bRecord\b|\bRow \d'))
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_grant_preview_shows_the_skill_dates_and_evidence(browser, live_server, h, scheme):
    no_light_path(h)
    skill = h.configure('capabilities', {'name': 'Point-of-care ultrasound', 'starts_on': '2020-01-01'})['record']
    context, page = sign_in(browser, live_server, 'credentialer', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'people')
        page.get_by_role('checkbox', name='Select all people shown').check()
        bar(page).get_by_role('button', name='Change…').click()
        row = change_row(page, 'Grant skills')
        row.get_by_label('Skill', exact=True).select_option(label=skill['name'])
        dates = row.locator('.bulk-value .date-text')
        dates.first.fill('1 Dec 2026')
        dates.first.press('Tab')
        dates.nth(1).fill('31 Dec 2027')
        dates.nth(1).press('Tab')
        row.get_by_label('Evidence', exact=True).fill('Course certificate 2026')
        submit(page)
        expect(panel(page).get_by_role('heading', name='Preview — nothing saved yet')).to_be_visible()
        ready = panel(page).locator('tbody tr', has=page.locator('.badge', has_text='Ready')).first
        for text in (skill['name'], '1 Dec 2026', '31 Dec 2027', 'Course certificate 2026'):
            expect(ready).to_contain_text(text)
        # rc.17 F13: the row names whom the skill is granted to (the person), never a generic Record.
        names = [p['display_name'] for p in h.get('admin', '/api/catalogues')['people']]
        expect(ready).to_contain_text(re.compile('|'.join(map(re.escape, names))))
        expect(ready).not_to_contain_text(re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-'))
        assert not errors, errors
    finally:
        context.close()
