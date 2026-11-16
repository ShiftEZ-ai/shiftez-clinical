"""Show information, don't hide it : a custom field's conditional requirement and the people a
programme authorisation covers are labelled sections in the form, never a closed <details>. Desktop 1440, light and
dark (both forms are operator screens)."""
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401

pytestmark = pytest.mark.browser
DESKTOP = {'width': 1440, 'height': 1000}


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_custom_field_conditional_requirement_is_shown(browser, live_server, h, scheme):
    context, page = sign_in(browser, live_server, 'admin', viewport=DESKTOP, color_scheme=scheme)
    try:
        navigate(page, 'configuration')
        page.get_by_role('button', name='Duty types', exact=True).click()
        page.get_by_role('button', name='New duty type', exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_role('button', name='Add custom field', exact=True).click()
        group = dialog.get_by_role('group', name='Conditional requirement', exact=True)
        expect(group.get_by_label('Required when field key', exact=True)).to_be_visible()
        expect(group.get_by_label('Required when equals', exact=True)).to_be_visible()
        expect(dialog.locator('details')).to_have_count(0)
    finally:
        context.close()


def test_duty_type_editor_shows_and_normalises_short_code(browser, live_server, h):
    duty = h.configure('assignment_types', {'name': 'Configured short code duty', 'starts_on': '2020-01-01',
                                            'definition': {}})['record']
    context, page = sign_in(browser, live_server, 'admin', viewport=DESKTOP)
    try:
        navigate(page, f'configuration/assignment_types?open={duty["id"]}')
        dialog = page.get_by_role('dialog', name='Edit duty type', exact=True)
        code = dialog.get_by_label('Short code', exact=True)
        expect(code).to_be_visible()
        code.fill('ab-2')
        expect(code).to_have_value('AB2')
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_programme_authorisation_lists_the_people_it_covers(browser, live_server, h, scheme):
    programme = h.configure('programmes', {'name': 'Visible roster programme', 'starts_on': '2020-01-01'})['record']
    person = h.configure('people', {'display_name': 'Roster colleague'})['record']
    h.configure('programme_memberships', {'person_id': person['id'], 'programme_id': programme['id'],
                                          'starts_on': '2020-01-01', 'ends_on': '2030-01-01'}, role='coordinator')
    context, page = sign_in(browser, live_server, 'credentialer', viewport=DESKTOP, color_scheme=scheme)
    try:
        navigate(page, 'people')
        page.get_by_role('button', name='Authorise programme', exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Programme', exact=True).select_option(label=programme['name'])
        covered = dialog.get_by_role('list', name='People covered')
        expect(covered.get_by_role('listitem')).to_have_text(['Roster colleague'])
        expect(dialog.locator('details')).to_have_count(0)
    finally:
        context.close()
