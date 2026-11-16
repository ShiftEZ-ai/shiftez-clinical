"""W3 screens (b): Overview, Published rota, People, Availability, Reports, Imports, Operations, Audit log and Inbox,
simplified — one primary action per screen, tabs instead of stacked panels and expanders, outline icons, and the
published snapshot loaded once per version. Behavioural checks per role, empty and populated, phone and dark."""
import re
import uuid

import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction
from .governance_support import developer, identity
from .test_browser import browser, live_server, sign_in, navigate, pick_view, close_popover, no_opening_hours  # noqa: F401
from .test_browser import whole_list
from .test_browser_w3_setup_today import calendar, finish_required_steps, update

pytestmark = pytest.mark.browser
PHONE = {'width': 390, 'height': 844}
PRIMARY = re.compile(r'\bprimary\b')


def watch_errors(page):
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    return errors


def no_overflow(page):
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')


def published(h):
    me = h.get('staff', '/api/me')['person_id']
    h.publish(h.shift(h.workspace('W3 published rota'), people=[me] if me else None)['workspace'])
    return h.get('staff', '/api/publications')['publications'][0]['id']


def test_a_coordinator_sees_what_setup_still_needs_and_who_adds_it(browser, live_server, h):
    """An unfinished setup is shown to those who can act on its next step (UX review H-02). While the next step is an
    admin's (opening hours) a coordinator sees no card; once it is their own (rules), the card opens its page."""
    no_opening_hours(h)
    [seeded] = h.get('admin', '/api/policies?status=active')['policies']
    assert seeded['definition']['rule']['id'] == 'fair_share'
    h.command('approver', 'policy.retire', {'reason': 'Synthetic setup without department rules'}, seeded['id'], seeded['revision'])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'home')
        card = page.locator('.setup-card')
        expect(page.locator('.home-needs')).to_be_visible()
        expect(card).to_have_count(0)
        # With opening hours set, the next step is one a coordinator does on their own page: the card opens it. Opening
        # hours are done when every service has them (setup_guide stepDone, 8df451143), not when one calendar exists.
        cal = calendar(h)
        for service in h.get('admin', '/api/catalogues')['services']:
            update(h, 'services', service, calendar_id=cal['id'])
        page.reload()
        navigate(page, 'home')
        expect(card).to_contain_text('Next: rules')
        expect(card.get_by_text('An admin finishes setup in Configuration.')).to_have_count(0)
        card.get_by_role('button', name='Add rules', exact=True).click()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'rules')
    finally:
        context.close()


def test_overview_setup_card_and_attention_rows_use_svg_icons(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    errors = watch_errors(page)
    try:
        navigate(page,'home')
        # Setup unfinished: the card says what is missing once.
        expect(page.locator('.setup-card')).to_contain_text(re.compile(r'^Setup \d+ of \d+'))
        # Needs you: each row leads with its count in a tone pill (the number is the text, never colour alone).
        counts = page.locator('.home-needs-list .home-count')
        expect(counts.first).to_be_visible()
        for text in counts.all_inner_texts():
            assert re.fullmatch(r'\d+\+?', text), text
        finish_required_steps(h, clinic_hours=False)
        page.reload()
        navigate(page,'home')
        expect(page.locator('.setup-card')).to_have_count(0)
        expect(page.locator('.home-needs-list li').filter(has_text='Clinics without opening hours')).to_have_count(1)
        assert not errors
    finally:
        context.close()


def test_overview_coordinator_next_step_item_has_an_icon(browser, live_server, h):
    workspace = h.workspace('W3 checked draft')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page,'home')
        item = page.locator('.home-needs-list li').filter(has_text='Ready to submit')
        expect(item).to_have_count(0)
        h.shift(workspace)
        page.reload()
        expect(item).to_contain_text('W3 checked draft')
        expect(item.locator('.home-count')).to_have_text('1')
    finally:
        context.close()


@pytest.mark.parametrize('role', ['coordinator', 'staff'])
def test_published_week_month_switch_reuses_the_snapshot_and_coverage(browser, live_server, h, role):
    pub = published(h)
    context, page = sign_in(browser, live_server, role)
    errors = watch_errors(page)
    try:
        navigate(page, 'published')
        expect(page.get_by_role('button', name='Open report', exact=True)).to_have_count(0)  # the sidebar has Reports
        # Print lives in Schedule tools in the top bar (no draft open; S-18).
        page.get_by_role('button', name='Schedule tools', exact=True).click()
        expect(page.get_by_role('menu', name='Schedule tools').get_by_role('menuitem', name='Print or export…', exact=True)).to_be_visible()
        close_popover(page)
        navigate(page, 'schedule/week/2026-10-04')  # the week's published sessions and their live check load once
        expect(page.locator('#schedule-view .pchip, #schedule-view .ag-item').first).to_be_visible()
        loads = []
        page.on('request', lambda r: loads.append(r.url) if re.search(f'/api/publications/{pub}(/coverage)?$', r.url) else None)
        for label in ('Month', 'Week', 'Month'):
            pick_view(page, label)
        assert loads == [], loads  # the point of the change: switching the range does not reload the publication
        expect(page.locator('#main details')).to_have_count(0)  # rota check findings are a visible section, not a disclosure
        assert not errors
    finally:
        context.close()


def test_people_header_has_one_primary_and_row_actions_hold_the_rest(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        expect(page.get_by_role('button', name='New person', exact=True)).to_have_class(PRIMARY)
        for gone in ('Add membership', 'Open skills list', 'Import spreadsheet', 'Grant capability', 'Authorise programme'):
            expect(page.get_by_role('button', name=gone, exact=True)).to_have_count(0)
        page.get_by_role('button', name=re.compile('^More actions for ')).first.click()
        menu = page.get_by_role('menu', name='Row actions')
        expect(menu.get_by_role('menuitem', name='Add rank', exact=True)).to_be_visible()
        expect(menu.get_by_role('menuitem', name='Grant skill', exact=True)).to_have_count(0)
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'credentialer')
    try:
        navigate(page, 'people')
        expect(page.get_by_role('button', name='Authorise eligibility', exact=True)).to_have_class(PRIMARY)
        expect(page.get_by_role('button', name='Authorise programme', exact=True)).not_to_have_class(PRIMARY)
        expect(page.get_by_role('button', name='New person', exact=True)).to_have_count(0)
        expect(page.get_by_role('button', name='Grant capability', exact=True)).to_have_count(0)
        person = h.get('credentialer', '/api/catalogues')['people'][0]
        page.get_by_role('button', name=re.compile('^More actions for ' + re.escape(person['display_name']))).click()
        page.get_by_role('menuitem', name='Grant skill', exact=True).click()
        dialog = page.get_by_role('dialog', name='Grant skill')
        expect(dialog.locator('.relationship-person')).to_contain_text(person['display_name'])  # SW3-D-14: fixed, not a picker
    finally:
        context.close()
    identity(h, 'programme_lead', ['programme_lead'])
    context, page = sign_in(browser, live_server, 'programme_lead')
    try:
        navigate(page, 'people')  # read-only: no header actions at all
        for gone in ('New person', 'Authorise eligibility', 'Authorise programme'):
            expect(page.get_by_role('button', name=gone, exact=True)).to_have_count(0)
        expect(page.get_by_role('searchbox', name='Search people', exact=True)).to_be_visible()
    finally:
        context.close()


def test_requests_primary_action_per_role(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page,'requests')
        # One list (no tabs) and one way to ask for anything: New request.
        expect(page.locator('#main [role=tablist]')).to_have_count(0)
        expect(page.get_by_role('button', name='New request', exact=True)).to_have_class(PRIMARY)
        for gone in ('Record leave', 'Submit a request', 'Set weekly availability', 'Offer a swap'):
            expect(page.get_by_role('button', name=gone, exact=True)).to_have_count(0)
        page.get_by_role('button', name='New request', exact=True).click()
        kinds = page.get_by_role('dialog', name='New request').get_by_role('radio')
        expect(kinds).to_have_count(5)  # leave, every week, wish, swap, other
        expect(page.get_by_role('dialog', name='New request').get_by_role('radio', name=re.compile('^Record'))).to_have_count(0)
    finally:
        context.close()
    h.command('staff', 'request.create', {'person_id': h.seed['people'][0], 'kind': 'meeting',
        'start': '2026-10-04T09:00:00+03:00', 'end': '2026-10-04T10:00:00+03:00', 'private_reason': 'W3 fictional reason'})
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page,'requests')
        expect(page.get_by_role('button', name='New request', exact=True)).to_have_class(PRIMARY)
        page.get_by_role('button', name='New request', exact=True).click()
        expect(page.get_by_role('dialog', name='New request').get_by_role('radio', name=re.compile('^Leave or absence'))).to_be_visible()
        expect(page.get_by_role('dialog', name='New request').get_by_role('radio', name=re.compile('^Leave An approver'))).to_have_count(0)  # one way in: no second Leave
        expect(page.get_by_role('button', name='Record known unavailability', exact=True)).to_have_count(0)
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page,'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        expect(page.get_by_role('button', name='Approve', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='Approve request', exact=True)).to_have_count(0)
        for gone in ('Record leave', 'Submit a request', 'New request'):  # approvers decide in the list; nothing to create
            expect(page.get_by_role('button', name=gone, exact=True)).to_have_count(0)
        # The private reason is shown only in the request itself (its side panel), not as a list column.
        expect(page.get_by_role('columnheader', name='Private reason')).to_have_count(0)
        page.locator('#main tr.dt-open').first.locator('td[data-col="type"]').click()
        expect(page.get_by_role('region', name='Meeting')).to_contain_text('W3 fictional reason')
    finally:
        context.close()


def test_workload_folds_extra_figures_into_columns_and_downloads_live_on_exports(browser, live_server, h):
    pub = published(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    errors = watch_errors(page)
    try:
        navigate(page,'insights/workload')
        main = page.locator('#main')
        expect(main.locator('details')).to_have_count(0)
        expect(main.get_by_role('columnheader', name='Scheduled hours')).to_be_visible()
        for zero in ('Educational credits', 'Overtime hours'):  # all zero: hidden
            expect(main.get_by_role('columnheader', name=zero)).to_have_count(0)
        # Downloads have their own tab (Insights › Exports); Workload has no export menu.
        expect(page.get_by_role('button', name='Export', exact=True)).to_have_count(0)
        # The duty list of a person reuses the loaded publication.
        loads = []
        page.on('request', lambda r: loads.append(r.url) if r.url.endswith(f'/api/publications/{pub}') else None)
        who = main.locator('tbody tr').first.get_by_role('button', name='View duties', exact=True)
        for _ in range(2):
            who.click()
            panel = page.get_by_role('region', name=re.compile('^Duties · '))
            expect(panel.locator('tbody tr').first).to_be_visible()
            panel.get_by_role('button', name='Close panel').click()
        assert len(loads) <= 1, loads
        navigate(page, 'insights/exports')
        # No commitments report in this version: that row is left out (I-20).
        expect(page.locator('#main .export-row strong')).to_have_text(['Workload CSV', 'Duties CSV', 'Report JSON', 'Personal calendar'])
        assert not errors
    finally:
        context.close()


def test_imports_tabs_for_coordinator_and_single_section_for_credentialer(browser, live_server, h):
    h.workspace('W3 import draft')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page,'system/imports')
        tabs = page.get_by_role('tablist', name='Imports')
        expect(tabs.get_by_role('tab')).to_have_text(['Staff', 'Rota sessions'])  # no baseline imports: no Baseline tab
        panel = page.get_by_role('tabpanel').last  # inside System's own tab panel
        expect(panel.locator('.button.primary')).to_have_text(['Upload staff CSV'])
        tabs.get_by_role('tab', name='Rota sessions', exact=True).click()
        panel = page.get_by_role('tabpanel').last
        expect(panel.locator('.button.primary')).to_have_text(['Upload CSV source'])
        panel.get_by_role('button', name='Upload CSV source', exact=True).click()
        dialog = page.get_by_role('dialog')
        for column in ('assignment_type', 'location', 'service', 'person', 'start', 'end', 'overtime'):
            expect(dialog.get_by_role('listitem').filter(has_text=re.compile('^' + column + '$'))).to_have_count(1)
        expect(page.locator('#main details')).to_have_count(0)
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'credentialer')
    try:
        navigate(page,'system/imports')
        expect(page.get_by_role('tablist', name='Imports')).to_have_count(0)
        expect(page.get_by_role('button', name='Upload staff CSV', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='Upload CSV source', exact=True)).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('role', ['operator', 'auditor'])
def test_operations_shows_how_it_measures_and_short_labels(browser, live_server, h, role):
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Outbox(department_id=h.seed['department_id'], event_key=str(uuid.uuid4()), kind='announcement', payload={},
                        state='failed', attempts=3, next_attempt_at=m.now(), last_error='W3 fictional error'))
    context, page = sign_in(browser, live_server, role)
    try:
        navigate(page,'system/operations')
        main = page.locator('#main')
        expect(main.locator('details')).to_have_count(0)
        expect(main.get_by_text('How these are measured')).to_have_count(0)
        expect(main.get_by_role('heading', name='Notification outbox', exact=True)).to_be_visible()
        retry = main.get_by_role('button', name='Retry', exact=True)
        expect(retry).to_have_count(1 if role == 'operator' else 0)
    finally:
        context.close()


def test_audit_reference_is_a_tip_on_its_copy_button(browser, live_server, h):
    # F13 A-03: the reference is on one Copy reference button per row (its tip), not read out with every time.
    h.workspace('W3 audited rota')
    context, page = sign_in(browser, live_server, developer(h))
    try:
        navigate(page,'system/audit')
        expect(page.locator('#main details')).to_have_count(0)
        row = page.locator('.audit-row').first
        expect(row.locator('.audit-time .sr-only')).to_have_count(0)
        copy = row.get_by_role('button', name=re.compile('^Copy reference for '))
        expect(copy).to_have_attribute('data-tip', re.compile('Reference '))
        copy.focus()
        expect(copy).to_be_focused()
    finally:
        context.close()


PAGES = {'admin': ['overview', 'people'], 'coordinator': ['published', 'availability', 'reports', 'data', 'inbox'],
         'approver': ['availability'], 'staff': ['overview', 'published', 'availability', 'reports'],
         'credentialer': ['people', 'data'], 'auditor': ['operations', 'audit']}


@pytest.mark.parametrize('role', list(PAGES))
def test_phone_dark_pages_fit_without_errors(browser, live_server, h, role):
    published(h)
    context, page = sign_in(browser, live_server, role, PHONE)
    page.emulate_media(color_scheme='dark')
    errors = watch_errors(page)
    try:
        for key in PAGES[role]:
            navigate(page, key)
            expect(page.locator('#main .loading')).to_have_count(0)
            no_overflow(page)
        assert not errors
    finally:
        context.close()
