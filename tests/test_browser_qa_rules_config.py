"""QA stream B (Rules & configuration): page arrival at the top, Configuration section switches that keep the layout
and focus, the unsaved-settings guard, compact rule rows without a duplicate attention banner, read-only lines,
department-derived name examples, Overview counting waiting requests through the status filter, server errors
carrying their reference, and role-appropriate actions on Access and People."""
import json
import re
from datetime import date

import pytest
from playwright.sync_api import expect

from .governance_support import developer
from .test_browser import browser, live_server, sign_in, navigate, open_rule  # noqa: F401
from .test_rule_templates import propose, activate

pytestmark = pytest.mark.browser


def many_rules(h, n=40):
    from apps.api.db import transaction
    from apps.api import models as m
    ids = h.seed['identities']
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all([m.Policy(department_id=h.seed['department_id'], name=f'Rest rule {i:02}', kind='min_rest', status='active',
                             definition={'schema_version': 1, 'rule': {'kind': 'min_rest', 'hours': 11}, 'scope': {}, 'severity': 'mandatory',
                                         'exception_allowed': False},
                             owner='Synthetic owner', source='Synthetic fixture', starts_on=date(2026, 1, 1),
                             created_by=ids['coordinator'], approved_by=ids['approver']) for i in range(n)])


def test_pages_open_at_the_top_and_configuration_sections_keep_layout_and_focus(browser, live_server, h):
    many_rules(h)
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'rules')
        assert page.evaluate('scrollY') == 0  # focusing the page used to scroll its first row under the top bar
        page.evaluate('scrollTo(0, 600)')
        navigate(page, 'configuration')
        assert page.evaluate('scrollY') == 0
        nav = page.get_by_role('navigation', name='Configuration sections')
        nav.get_by_role('button', name='Locations', exact=True).focus()
        page.keyboard.press('Enter')
        expect(page.locator('.config-pane .section-head h2').first).to_have_text('Locations')
        expect(nav).to_be_visible()
        assert page.evaluate("document.activeElement.closest('.config-nav') !== null && document.activeElement.textContent.startsWith('Locations')")
        # The redraw keeps the old section on screen instead of blanking the whole page.
        held = []
        page.route('**/api/access', lambda route: held.append(route))
        with page.expect_request('**/api/access'):
            nav.get_by_role('button', name='Department settings', exact=True).click()
        # The section left stays in place (inert, then its own skeleton once 300 ms pass): never a whole-page loading state.
        expect(page.locator('.config-pane[data-kind=locations]')).to_have_count(1)
        expect(page.locator('#main .loading:not(.config-pane .loading)')).to_have_count(0)
        expect(nav).to_be_visible()
        held.pop().continue_()
        page.unroute('**/api/access')
        expect(page.locator('.config-pane .section-head h2').first).to_have_text('Department settings')
    finally:
        context.close()


def test_unsaved_settings_ask_once_before_leaving(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'configuration')
        nav = page.get_by_role('navigation', name='Configuration sections')
        nav.get_by_role('button', name='Department settings', exact=True).click()
        heading = page.locator('.config-pane .section-head h2').first
        expect(heading).to_have_text('Department settings')
        page.get_by_label('Department name', exact=True).fill('Renamed but not saved')
        page.evaluate("location.hash='rules'")  # another page (a link, Back): asked first, in the one leave dialog
        leave = page.get_by_role('dialog', name='Leave this page?')
        expect(leave).to_contain_text('Not saved')
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'configuration')
        assert page.evaluate('location.hash') == '#configuration/department'
        expect(page.get_by_label('Department name', exact=True)).to_have_value('Renamed but not saved')
        leave.get_by_role('button', name='Discard changes', exact=True).click()  # goes on to the page asked for
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'rules')
        navigate(page, 'configuration')
        nav.get_by_role('button', name='Department settings', exact=True).click()
        expect(page.get_by_label('Department name', exact=True)).not_to_have_value('Renamed but not saved')
        navigate(page, 'rules')  # nothing unsaved: no question
    finally:
        context.close()


def test_rule_rows_are_compact_and_attention_is_not_repeated_in_a_banner(browser, live_server, h):
    activate(h, propose(h, {'kind': 'min_rest', 'hours': 11}, name='Rest between duties'))
    propose(h, {'kind': 'max_consecutive_days', 'maximum': 6}, name='Days in a row')  # waiting: needs attention
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        item = page.locator('.rule-item').filter(has_text='Rest between duties')
        expect(item.locator('.rl-text')).to_contain_text('Rest between duties')
        expect(item.locator('.rl-mark')).to_have_text('Mandatory')  # one mark beside the sentence
        expect(page.locator('.rule-banner')).to_have_count(0)  # Saved views, the section badge and the footer say it
        expect(page.locator('.rule-toolbar button.dt-saved')).to_have_attribute('aria-label', re.compile(r', 1 needs attention$'))
        expect(page.locator('.app-footer')).to_contain_text('1 needs attention')
        lines = open_rule(page, 'Rest between duties').locator('.rl-sec').filter(has_text='Lines for specific people or programmes')
        expect(lines).to_be_visible()
        expect(lines.get_by_role('button', name='New line')).to_be_visible()
        expect(lines).to_contain_text('None: everyone uses the numbers above.')
        expect(lines).not_to_contain_text('copy')
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'rules')
        sheet = open_rule(page, 'Rest between duties')
        expect(sheet.get_by_role('tab', name='Details', exact=True)).to_be_visible()
        expect(sheet.get_by_text('Lines for specific people or programmes')).to_have_count(0)  # no lines and nothing an approver can add
        expect(sheet.locator('.rule-lines')).to_be_hidden()
    finally:
        context.close()


def test_name_examples_come_from_the_department_and_save_sits_in_the_header(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'configuration')
        page.get_by_role('navigation', name='Configuration sections').get_by_role('button', name='Names & codes', exact=True).click()
        pane = page.locator('.config-pane')
        expect(pane.get_by_role('radiogroup', name='Show names as')).to_contain_text('A. Morgan')  # Alex Morgan, from the seed
        expect(pane).not_to_contain_text('Felix Lewis')
        expect(pane.locator('th', has_text='Automatic')).to_have_count(0)  # the code shows once, in its input
        bar = pane.get_by_role('region', name='Unsaved changes')
        expect(bar).to_be_hidden()  # one save control: the unsaved-changes bar, shown once something changed
        expect(pane.locator('.section-head').get_by_role('button', name='Save defaults')).to_have_count(0)
        pane.get_by_label('Avoid duplicate codes automatically', exact=True).uncheck()
        expect(bar.get_by_role('button', name='Save defaults', exact=True)).to_be_enabled()
    finally:
        context.close()


def test_overview_counts_waiting_requests_through_the_status_filter(browser, live_server, h):
    def ask(day):
        return h.command('staff', 'request.create', {'person_id': h.seed['people'][0], 'kind': 'clinic_change',
            'start': f'2026-10-{day}T09:00:00+03:00', 'end': f'2026-10-{day}T10:00:00+03:00', 'private_reason': 'Fictional'})['record']
    ask('04')
    done = ask('05')
    h.command('staff', 'request.decide', {'decision': 'withdraw', 'reason': 'No longer needed'}, done['id'], done['revision'])
    context, page = sign_in(browser, live_server, 'approver')
    try:
        with page.expect_request(re.compile(r'/api/home$')):
            page.reload()
        row = page.locator('.home-needs-list li').filter(has_text='Request to decide')
        expect(row.locator('.home-count')).to_have_text('1')
    finally:
        context.close()


def test_a_server_error_shows_its_reference(browser, live_server, h):
    context, page = sign_in(browser, live_server, developer(h))
    try:
        body = json.dumps({'error': {'code': 'INTERNAL_ERROR', 'message': 'Something went wrong. Quote the reference if you contact support.'},
                           'correlation_id': 'abc123reference'})
        page.route('**/api/operations*', lambda route: route.fulfill(status=500, content_type='application/json', body=body))
        page.evaluate("location.hash='operations'")
        alert = page.locator('#main').get_by_role('alert')
        expect(alert).to_contain_text('Couldn’t load Operations.')
        expect(alert).to_contain_text('Reference: abc123reference')
        expect(page.get_by_role('button', name='Retry', exact=True)).to_be_visible()
    finally:
        context.close()


def test_admin_has_one_primary_action_on_people(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'people')
        expect(page.get_by_role('button', name='New person', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='Authorise eligibility', exact=True)).to_have_count(0)  # a row action for managers
    finally:
        context.close()

def test_menus_open_inside_dialogs_and_a_lookup_dialog_closes_without_asking(browser, live_server, h):
    activate(h, propose(h, {'kind': 'min_rest', 'hours': 11}, name='Rest between duties'))
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        page.get_by_role('button', name='Add rule', exact=True).click()
        form = page.get_by_role('dialog', name='Add rule')
        form.get_by_role('radio', name='Quota or commitment', exact=True).check()
        form.get_by_role('button', name='Add column').click()
        menu = page.get_by_role('menu', name='Add column')
        expect(menu).to_be_visible()
        # In the dialog's top layer: the menu is what a click at its centre reaches (it used to render underneath).
        assert menu.evaluate("m => { const r = m.getBoundingClientRect(); return m.contains(document.elementFromPoint(r.x + r.width / 2, r.y + 8)); }")
        page.keyboard.press('Escape')
        expect(menu).to_have_count(0)
        form.get_by_role('button', name='Close dialog').click()
        form.get_by_role('button', name='Discard changes').click()  # a rule type was chosen: the form asks first
        expect(form).to_have_count(0)
        page.get_by_role('button', name='Which rule applies?').click()
        lookup = page.get_by_role('dialog', name='Which rule applies?')
        lookup.get_by_label('Person', exact=True).select_option(h.seed['people'][0])
        expect(lookup.locator('.which-rule-rows')).to_contain_text('Rest between duties')
        expect(lookup.locator('.which-rule-rows')).to_contain_text('Fair share')
        expect(lookup).to_contain_text('2 rules apply.')
        page.keyboard.press('Escape')
        expect(lookup).to_have_count(0)  # a lookup is not an unsaved change
    finally:
        context.close()


def test_activation_review_shows_the_rule_and_offers_no_checks_without_rotas(browser, live_server, h):
    propose(h, {'kind': 'min_rest', 'hours': 11}, name='Rest between duties')
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'rules')
        page.locator('.rule-item').filter(has_text='Rest between duties').get_by_role('button', name='Review', exact=True).click()
        review = page.get_by_role('dialog', name=re.compile('^Review Rest between duties'))
        expect(review.locator('.rule-review-summary')).to_contain_text('11 hours')
        expect(review).to_contain_text('No rota being planned overlaps these dates.')
        expect(review.get_by_role('button', name='Check all affected rotas')).to_have_count(0)
        expect(review.get_by_role('button', name='Refresh checks')).to_have_count(0)
        expect(review.get_by_role('button', name='Activate', exact=True)).to_be_visible()
    finally:
        context.close()


CONFIG_SECTIONS = {'assignment_types': 'Duty types', 'recurring_series': 'Recurring sessions', 'operating_calendars': 'Opening hours',
                   'resource_pools': 'Resource pools', 'locations': 'Locations', 'location_types': 'Location types',
                   'services': 'Services', 'categories': 'Ranks', 'capabilities': 'Skills & capabilities',
                   'names': 'Names & codes', 'tags': 'Tags', 'department': 'Department settings',
                   'auto_schedule': 'Auto-schedule & scoring', 'integrations': 'Integrations', 'department_content': 'Announcements'}


def test_every_configuration_section_opens_from_its_own_address(browser, live_server, h):
    """#configuration/<section> opens that section (highlighted and shown), a reload keeps it, and pressing the section
    already shown reads it afresh instead of doing nothing. A section that needs a further permission (Integrations is
    the Developer's) is not offered without it, and its address keeps the section shown."""
    context, page = sign_in(browser, live_server, developer(h))
    try:
        nav = page.get_by_role('navigation', name='Configuration sections')
        heading = page.locator('.config-pane .section-head h2').first
        for key, label in CONFIG_SECTIONS.items():
            page.evaluate("key => { document.querySelector('#main').removeAttribute('data-loaded-view'); location.hash = 'configuration/' + key; }", key)
            expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'configuration')
            expect(nav.locator('[aria-current=page]')).to_contain_text(label)
            expect(heading).to_have_text(label)
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'admin')
    try:
        nav = page.get_by_role('navigation', name='Configuration sections')
        heading = page.locator('.config-pane .section-head h2').first
        for key, label in CONFIG_SECTIONS.items():
            if key == 'integrations':
                continue
            # The address says the section's name (#configuration/opening-hours); the old one still opens it.
            page.evaluate("key => { document.querySelector('#main').removeAttribute('data-loaded-view'); location.hash = 'configuration/' + key; }", key)
            expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'configuration')
            current = nav.locator('[aria-current=page]')
            expect(current).to_have_count(1)
            expect(current).to_contain_text(label)
            expect(heading).to_have_text(label)
        page.reload()
        expect(heading).to_have_text('Announcements')
        # Department settings, opened from its address straight after another section (the reported case).
        navigate(page, 'configuration/assignment_types')
        page.evaluate("location.hash='configuration/department'")
        expect(heading).to_have_text('Department settings')
        department = nav.get_by_role('button', name='Department settings', exact=True)
        expect(department).to_have_attribute('aria-current', 'page')
        with page.expect_request('**/api/access'):  # the section shown: read again, never a dead click
            department.click()
        expect(heading).to_have_text('Department settings')
        nav.get_by_role('button', name='Services', exact=True).click()
        expect(page).to_have_url(re.compile(r'#configuration/services$'))
        # Integrations is the Developer's: not offered here, and its address keeps the section shown.
        page.evaluate("document.querySelector('#main').removeAttribute('data-loaded-view'); location.hash = 'configuration/integrations'")
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'configuration')
        expect(nav.get_by_role('button', name='Integrations', exact=True)).to_have_count(0)
        expect(heading).to_have_text('Services')  # the section shown stays, never a refusal
        expect(page).to_have_url(re.compile(r'#configuration/services$'))
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
@pytest.mark.parametrize('viewport', [{'width': 1440, 'height': 900}, {'width': 390, 'height': 844}], ids=['desktop', 'phone'])
def test_department_settings_have_one_save_control_and_save_without_rereading_everything(browser, live_server, h, scheme, viewport):
    context, page = sign_in(browser, live_server, 'admin', viewport)
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'configuration/department')
        pane = page.locator('.config-pane')
        bar = pane.get_by_role('region', name='Unsaved changes')
        expect(bar).to_be_hidden()
        expect(page.get_by_role('button', name=re.compile('^Save (settings|defaults)$'))).to_have_count(0)  # nothing changed
        pane.get_by_label('Department name', exact=True).fill('Renamed department')
        expect(bar).to_be_visible()
        expect(page.get_by_role('button', name='Save settings', exact=True)).to_have_count(1)  # only the bar's
        sent = []
        page.on('request', lambda r: sent.append(r.url) if '/api/' in r.url and not re.search(r'/api/(pulse|events)', r.url) else None)
        bar.get_by_role('button', name='Save settings', exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Reason', exact=True).fill('New department name')
        dialog.get_by_role('button', name='Save settings', exact=True).click()
        expect(page.get_by_role('button', name='Undo', exact=True)).to_be_visible()
        expect(bar).to_be_hidden()
        expect(pane.get_by_label('Department name', exact=True)).to_have_value('Renamed department')
        # The reply patches the page: the session, lists, inbox and access are not read again.
        assert [u for u in sent if re.search(r'/api/(me|catalogues|notifications|access)\b', u)] == [], sent
        assert h.get('admin', '/api/me')['department']['name'] == 'Renamed department'
    finally:
        context.close()


def test_which_rule_links_to_the_schedule_why(browser, live_server, h):
    """RU-12 (lead: a link, no session picker): Which rule applies? leads to that person's day on the Schedule, where each
    session's Why says the rest."""
    activate(h, propose(h, {'kind': 'min_rest', 'hours': 11}, name='Rest between duties'))
    person = h.seed['people'][0]
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        page.get_by_role('button', name='Which rule applies?').click()
        lookup = page.get_by_role('dialog', name='Which rule applies?')
        lookup.get_by_label('Person', exact=True).select_option(person)
        expect(lookup.locator('.which-rule-rows')).to_contain_text('Rest between duties')
        lookup.get_by_role('button', name='See on the Schedule', exact=True).click()
        expect(lookup).to_have_count(0)
        # The settled address: a person link without a draft shows published duties, written as edit=none (app.ts writeRoute).
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
        expect(page).to_have_url(re.compile(r'#schedule/week/\d{4}-\d{2}-\d{2}\?edit=none&person=' + re.escape(person) + '$'))
    finally:
        context.close()
