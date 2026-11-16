"""Page changes orient keyboard users after the asynchronous shell render."""
import re

import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401

pytestmark = pytest.mark.browser


def test_navigation_sets_page_title_and_moves_focus_to_main(browser, live_server):
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'home', timeout=20000)
        page.get_by_role('navigation', name='Department pages').get_by_role('button', name='People', exact=True).focus()
        page.keyboard.press('Enter')
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'people', timeout=20000)
        expect(page).to_have_title(re.compile(r'^People'))
        expect(page.locator('#main')).to_be_focused()
        # The inbox opens from the bell in the top bar (it has no sidebar entry).
        page.locator('.topbar .bell').click()
        page.get_by_role('dialog', name='Inbox').get_by_role('button', name='Open inbox', exact=True).click()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'inbox', timeout=20000)
        expect(page).to_have_title('Inbox · Rota')
        expect(page.locator('#main')).to_be_focused()
    finally:
        context.close()


@pytest.mark.parametrize('role,gap', [
    ('credentialer', 'no_eligibility'), ('coordinator', 'no_rank'),
    ('admin', 'no_eligibility'), ('admin', 'no_rank'),
])
def test_home_people_attention_opens_exact_active_gap_and_replaces_saved_filters(browser, live_server, h, role, gap):
    person = h.configure('people', {'display_name': 'Fictional active review'})['record']
    inactive = h.configure('people', {'display_name': 'Fictional inactive review', 'active': False})['record']
    other = h.configure('people', {'display_name': 'Fictional other gap'})['record']
    if gap == 'no_eligibility':
        h.command('credentialer', 'eligibility.create', {'person_id': other['id'],
            'service_id': h.seed['services'][0], 'starts_on': '2020-01-01'})
        label, facts = 'Person without clinics', 'eligibilities'
    else:
        h.command('coordinator', 'membership.create', {'person_id': other['id'],
            'category_id': h.seed['category'], 'starts_on': '2020-01-01'})
        label, facts = 'Person without a rank', 'memberships'
    before = h.get('admin', '/api/catalogues')[facts]
    context, page = sign_in(browser, live_server, role)
    try:
        # A previous People view must not narrow or broaden a Home attention link.
        navigate(page, 'people')
        page.get_by_role('button', name=re.compile('^Filter')).click()
        filters = page.get_by_role('dialog', name='Filter', exact=True)
        filters.get_by_role('button', name='Clear all filters', exact=True).click()
        from .test_browser import condition
        condition(filters, 'Status', ['Inactive'])
        page.keyboard.press('Escape')
        page.get_by_label('Search people', exact=True).fill(inactive['display_name'])
        expect(page.locator('tbody tr[data-row]')).to_have_count(1)
        expect(page.locator('tbody tr[data-row]')).to_have_attribute('data-row', inactive['id'])

        navigate(page, 'home')
        row = page.locator('.home-needs-list li').filter(has_text=label)
        expect(row.locator('.home-count')).to_have_text('1')
        count = int(row.locator('.home-count').inner_text())
        expect(page.locator('#main .home-clear')).to_have_count(0)
        if role == 'credentialer':
            expect(page.locator('.home-needs-list li').filter(has_text='without a rank')).to_have_count(0)
        row.get_by_role('button', name='Show', exact=True).click()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'people')
        for reload in (False, True):
            if reload:
                page.reload()
                expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'people')
            expect(page.get_by_label('Search people', exact=True)).to_have_value('')
            expect(page.locator('tbody tr[data-row]')).to_have_count(count)
            expect(page.locator('tbody tr[data-row]')).to_have_attribute('data-row', person['id'])
            expect(page.locator('tbody tr[data-row]')).to_contain_text(person['display_name'])
        assert h.get('admin', '/api/catalogues')[facts] == before
    finally:
        context.close()


@pytest.mark.parametrize('viewport', [{'width': 1440, 'height': 1000}, {'width': 390, 'height': 844}], ids=['desktop', 'phone'])
def test_admin_overview_zero_active_setup_is_honest_and_links_to_configuration(browser, live_server, h, viewport):
    from .test_browser_w3_setup_today import finish_required_steps
    finish_required_steps(h, clinic_hours=False)  # setup done: what is still missing shows as rows in Needs attention
    context, page = sign_in(browser, live_server, 'admin', viewport)
    try:
        navigate(page,'home')

        def row(text):
            return page.locator('.home-needs-list li').filter(has_text=text)

        expect(page.locator('.setup-card')).to_have_count(0)
        expect(row('Clinics without opening hours').locator('.home-count')).to_have_text('2')
        expect(row('without clinics')).to_have_count(0)  # everyone has one
        row('Clinics without opening hours').get_by_role('button', name='Set hours', exact=True).click()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'configuration')
        expect(page.get_by_role('heading', name='Services', exact=True)).to_be_visible()
        expect(page.get_by_role('searchbox', name='Search services', exact=True)).to_be_visible()

        catalogues = h.get('admin', '/api/catalogues')
        for kind in ('people', 'services'):
            h.command('admin', 'catalogue.bulk_update', {'catalogue': kind,
                'items': [{'id': row['id'], 'revision': row['revision']} for row in catalogues[kind]],
                'active': False, 'reason': 'Synthetic department setup boundary'})
        current = h.get('admin', '/api/catalogues')
        assert not any(row['active'] for kind in ('people', 'services') for row in current[kind])
        page.reload()
        navigate(page,'home')
        # No active people or services: setup is unfinished again, and its card says so once (no zero rows repeating it).
        expect(page.locator('.setup-card h2')).to_have_text(re.compile(r'^Setup \d+ of \d+ · Next: '))
        expect(row('Services without opening hours')).to_have_count(0)
    finally:
        context.close()
