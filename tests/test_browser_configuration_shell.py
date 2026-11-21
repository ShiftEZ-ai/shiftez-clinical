"""Configuration shell (audit C-01–C-05, C-32, C-35, C-36): no section carries a tagline; what a kind is for is said once,
from one registry, in its empty state (with the one action that adds it) and on its Setup guide step; nav buttons and
the pane carry stable hooks; Names & codes has no count. Admin, 1440 light and 390 dark."""
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401

pytestmark = pytest.mark.browser
VIEWS = [pytest.param({'width': 1440, 'height': 900}, 'light', id='1440-light'),
         pytest.param({'width': 390, 'height': 844}, 'dark', id='390-dark')]
REGISTRY = "async () => (await window.importBundle('config_kinds.js')).CONFIG_KINDS"


def expect_coordinator_sections(page, h):
    """Match every section to the server's edit permits, keeping settings admin-only."""
    kinds = page.evaluate(REGISTRY)
    registry = h.get('coordinator', '/api/contracts')['bulk_collections']
    roles = set(h.get('coordinator', '/api/me')['roles'])
    sections = {'setup', 'scheduling_groups', 'auto_schedule'}
    for kind, spec in kinds.items():
        actions = registry.get(kind, {}).get('actions', {})
        edit = next((actions[name] for name in ('edit', 'update', 'update_dates', 'propose') if name in actions), {})
        if spec.get('group') and not spec.get('settings') and roles.intersection(edit.get('roles', [])):
            sections.add(kind)
    nav = page.get_by_role('navigation', name='Configuration sections')
    expect(nav.locator('[data-section]')).to_have_count(len(sections))
    for kind in kinds:
        expect(nav.locator(f'[data-section="{kind}"]')).to_have_count(int(kind in sections))
    return {kind: spec for kind, spec in kinds.items() if kind in sections}


@pytest.mark.parametrize('view,scheme', VIEWS)
def test_sections_have_no_tagline_and_empty_lists_say_what_they_are_for(browser, live_server, h, view, scheme):
    context, page = sign_in(browser, live_server, 'admin', viewport=view)
    page.emulate_media(color_scheme=scheme)
    try:
        navigate(page, 'configuration/locations')
        kinds = page.evaluate(REGISTRY)
        nav = page.locator('.config-nav')
        sections = nav.locator('[data-section]').evaluate_all('bs => bs.map(b => b.dataset.section)')
        assert {'assignment_types', 'locations', 'names', 'tags', 'department', 'auto_schedule'} <= set(sections)
        expect(nav.locator('[data-section=names] .nav-count-muted')).to_have_count(0)  # not a list of records
        empties = []
        for kind in sections:
            if kind in ('setup', 'integrations'):
                continue
            nav.locator(f'[data-section="{kind}"]').click()
            pane = page.locator(f'.config-pane[data-kind="{kind}"]')
            expect(pane.locator(':scope > .section-head h2')).to_have_text(kinds[kind]['label'])
            expect(pane.locator(':scope > .section-head p.muted')).to_have_count(0)  # no tagline under any title
            state = pane.locator('.empty-state')
            if state.count():
                empties.append(kind)
                expect(state.locator('p')).to_have_text(kinds[kind]['purpose'])
                expect(state).not_to_contain_text('Nothing has been added here yet')
                create = kinds[kind].get('create') or 'New ' + kinds[kind]['singular']  # config_kinds.createLabel (maintenance)
                expect(state.get_by_role('button', name=create, exact=True)).to_be_visible()
                expect(pane.locator('.section-head').get_by_role('button', name=create, exact=True)).to_have_count(0)
        assert empties, 'the seed has at least one empty list (its empty state is what this checks)'
    finally:
        context.close()


def test_empty_tags_follow_the_same_empty_state(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        page.route('**/api/catalogues*', h.route_json('admin', '/api/catalogues', lambda body: {
            **body, 'tags': [], 'tag_members': []}))
        navigate(page, 'configuration/tags')
        purpose = page.evaluate("async () => (await window.importBundle('config_kinds.js')).purposeOf('tags')")
        pane = page.locator('.config-pane[data-kind="tags"]')
        expect(pane.locator('.empty-state p')).to_have_text(purpose)
        expect(pane.locator('.empty-state').get_by_role('button', name='New tag', exact=True)).to_be_visible()
        expect(pane.locator('.section-head').get_by_role('button', name='New tag', exact=True)).to_have_count(0)
    finally:
        context.close()


def test_setup_guide_steps_use_the_same_purpose_and_no_hero_line(browser, live_server):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'configuration/setup')
        kinds = page.evaluate(REGISTRY)
        guide = page.locator('.config-pane[data-kind="setup"]')
        expect(guide.locator('.setup-hero p')).to_have_count(0)  # ring and title only
        expect(guide.locator('.setup-step').filter(has_text=kinds['locations']['purpose'])).to_have_count(1)
        expect(guide.locator('.setup-step').filter(has_text='What staff see first on Home.')).to_have_count(1)
        expect(guide).not_to_contain_text('on the overview')
    finally:
        context.close()


def test_settings_pages_share_tabs_and_weights_show_their_share(browser, live_server):
    """C-06, C-08, C-09, C-10: Department settings in tabs like Auto-schedule & scoring; a wrong value on a hidden tab
    opens that tab; the five weights sit in one row, each with its share; the group help is a 24 px target."""
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'configuration/department')
        pane = page.locator('.config-pane[data-kind=department]')
        tabs = pane.get_by_role('tablist', name='Department settings')
        expect(tabs.get_by_role('tab')).to_have_text(['General', 'Swaps', 'Announcements', 'Governance', 'Advanced'])
        expect(pane.get_by_label('Department name', exact=True)).to_be_visible()
        tabs.get_by_role('tab', name='Swaps', exact=True).click()
        pane.get_by_label('Open swap offers per person', exact=True).fill('0')  # below the minimum
        tabs.get_by_role('tab', name='General', exact=True).click()
        pane.get_by_role('button', name='Save settings', exact=True).click()
        expect(tabs.get_by_role('tab', name='Swaps', exact=True)).to_have_attribute('aria-selected', 'true')
        expect(page.get_by_role('dialog')).to_have_count(0)  # nothing is sent while a value is wrong
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'configuration/auto_schedule')
        weights = page.locator('.config-pane[data-kind=auto_schedule] .score-weights')
        boxes = weights.locator('.field').evaluate_all('fs => fs.map(f => Math.round(f.getBoundingClientRect().top))')
        assert len(boxes) == 5 and len(set(boxes)) == 1, boxes  # one row of five
        expect(weights.locator('.weight-share')).to_have_count(5)
        size = weights.locator('legend .score-help').evaluate('b => [b.offsetWidth, b.offsetHeight]')
        assert size[0] >= 24 and size[1] >= 24, size
        fill = page.get_by_label('Fill priorities', exact=True)
        assert fill.evaluate('s => s.scrollWidth <= s.clientWidth + 1')  # its value is not cut
    finally:
        context.close()
