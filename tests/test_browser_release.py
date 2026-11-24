"""Release sweep regressions: untouched forms close at once, no request fails behind the scenes, header actions carry icons."""
import re
import pytest
from playwright.sync_api import expect
from apps.api import models as m
from apps.api.db import transaction
from .governance_support import identity
from .test_browser import filter_pick, browser, live_server, sign_in, navigate, open_rota, pick_view  # noqa: F401

pytestmark = pytest.mark.browser


def failed_requests(page):
    bad = []
    page.on('response', lambda r: bad.append(f'{r.status} {r.request.method} {r.url}') if r.status >= 400 else None)
    return bad


def test_rule_quick_start_opens_untouched_so_escape_closes_it(browser, live_server, h):
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed['organisation_id'], name='Never authored quick-start department')
        db.add(department)
        db.flush()
        department_id = department.id
    identity(h, 'quickstart-admin', ['admin'], department=department_id)
    assert not h.get('quickstart-admin', '/api/catalogues')['policies']
    context, page = sign_in(browser, live_server, 'quickstart-admin')
    try:
        navigate(page, 'rules')
        page.get_by_role('button', name=re.compile(r'^Minimum rest')).first.click()
        dialog = page.get_by_role('dialog')
        expect(dialog.get_by_role('radio', name='Minimum rest', exact=True)).to_be_checked()
        expect(dialog.get_by_label('Rule title', exact=True)).not_to_have_value('')
        page.keyboard.press('Escape')
        expect(page.locator('dialog[open]')).to_have_count(0)
    finally:
        context.close()


def test_new_session_asks_for_a_service_instead_of_a_failed_preview(browser, live_server, h):
    w = h.workspace('Service check rota')
    context, page = sign_in(browser, live_server, 'coordinator')
    bad = failed_requests(page)
    try:
        navigate(page, 'planner')
        open_rota(page, w['id'])
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
        page.locator('#main').get_by_role('button', name='Add session', exact=True).click()
        dialog = page.locator('.dock-panel')
        page.wait_for_timeout(1500)  # the live preview would have run by now
        assert not [b for b in bad if '/api/planner/preview' in b]
        expect(dialog.locator('.notice.error')).to_have_count(0)
        # A list with several choices starts on "Choose…" (one choice is picked for you); nothing is previewed yet.
        location = dialog.get_by_label('Location', exact=True)
        expect(location).to_have_value('')
        location.select_option(index=1)
        first = dialog.get_by_label('Continuity clinic', exact=True)
        assert first.evaluate('n => n.validationMessage') == 'Choose at least one clinic.'  # D-074: the field's own word
        # D-074: Save stops at the field and says why, before anything is sent.
        dialog.get_by_role('button', name='Save draft session', exact=True).click()
        expect(dialog.get_by_text('Choose at least one clinic.', exact=True)).to_be_visible()
        first.check()
        expect(dialog.get_by_text('Not saved yet: review what this changes', exact=False)).to_be_visible(timeout=20000)
        assert not bad
    finally:
        context.close()


@pytest.mark.parametrize('role', ['auditor'])
def test_people_view_does_not_ask_for_availability_a_role_cannot_read(browser, live_server, h, role):
    h.shift(h.workspace('Read-only people view'))
    context, page = sign_in(browser, live_server, role)
    bad = failed_requests(page)
    try:
        navigate(page, 'planner')
        pick_view(page, 'People')
        expect(page.get_by_role('region', name='People view', exact=True)).to_be_visible()
        page.wait_for_timeout(500)
        assert not bad
    finally:
        context.close()


# Admin no longer reads the audit log (Developer/Auditor do); Developer pages cover System.
ROLE_PAGES = {'admin': ['access', 'configuration', 'rules', 'governance', 'people'],
              'operator': ['audit', 'operations'],
              'coordinator': ['people', 'availability', 'rules', 'swaps', 'programmes', 'published'],
              'staff': ['availability', 'swaps', 'published', 'today']}


@pytest.mark.parametrize('role', sorted(ROLE_PAGES))
def test_every_header_action_has_an_icon_and_a_short_main_action_keeps_its_words(browser, live_server, h, role):
    h.publish(h.shift(h.workspace('Icons rota'))['workspace'])
    context, page = sign_in(browser, live_server, role)
    try:
        for route in ROLE_PAGES[role]:
            navigate(page, route)
            bare = page.locator('.page-actions .button:not(:has(svg)), #main .section-head .actions .button:not(:has(svg))')
            assert bare.count() == 0, (route, bare.all_inner_texts())
        # On a phone a lone main action under its heading has room for its words.
        page.set_viewport_size({'width': 390, 'height': 844})
        navigate(page, ROLE_PAGES[role][0])
        for label in page.locator('#main .section-head .actions:not(:has(>.button~.button~.button)) .button.primary .label').all():
            expect(label).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize('viewport', [None, {'width': 390, 'height': 360}], ids=['desktop', 'short-phone'])
def test_team_calendar_filters_live_in_the_collection_toolbar(browser, live_server, h, viewport):
    """The schedule's People view (the old team calendar) filters with the shared Filter popover: counted on the button,
    cleared inside, the last field reachable even on a short phone screen."""
    context, page = sign_in(browser, live_server, 'coordinator', viewport=viewport)
    try:
        navigate(page, 'calendar')
        filter_button = page.get_by_role('button', name=re.compile('^Filter'))
        filter_button.click()
        pop = page.get_by_role('dialog', name='Filter')
        filter_pick(pop, 'Person', 'Staff 01(test)')
        expect(page.get_by_role('button', name=re.compile(r'^Filter: .+ \(1 active\)$'))).to_be_visible()
        filter_pick(pop, 'Consultation mode', 'Virtual')  # the last field is reachable
        expect(page.get_by_role('button', name=re.compile(r'^Filter: .+ \(2 active\)$'))).to_be_visible()
        assert pop.evaluate('p => { const r = p.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; }')
        pop.get_by_role('button', name='Clear all filters', exact=True).click()
        expect(page.get_by_role('button', name='Filter', exact=True)).to_be_visible()
        expect(pop.locator('.cond-line .pick-button').filter(has_text='Staff 01(test)')).to_have_count(0)
        page.keyboard.press('Escape')
        expect(pop).to_have_count(0)
        expect(page.locator('details.cal-filters')).to_have_count(0)
    finally:
        context.close()
