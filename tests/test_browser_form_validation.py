"""Form problems in the field's own words, under the field, with the focus on the first one (DESIGN.md › Forms;
frontend audit X-07, R-02, U-34, X-39)."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, form_submit  # noqa: F401
from .test_browser_requests import new_request

pytestmark = pytest.mark.browser
OWN_WORDS = re.compile(r'^(Choose an? |Add an? |Tick |At least \d+ characters|At most \d+|From |Up to |Enter )')
FOCUSED = """() => {const a = document.activeElement, ids = (a.getAttribute('aria-describedby') || '').split(' ');
  const note = ids.map(id => document.getElementById(id)).find(n => n && n.classList.contains('field-error') && !n.hidden);
  const invalid = [...a.closest('form').querySelectorAll('[aria-invalid=true]')];
  return {invalid: a.getAttribute('aria-invalid'), message: note ? note.textContent : '', first: invalid[0] === a, count: invalid.length,
          label: (document.querySelector(`label[for="${a.id}"]`) || {}).textContent || a.getAttribute('aria-label') || ''}}"""


def test_an_empty_leave_request_says_what_to_choose_and_focuses_the_first_problem(browser, live_server, h):
    """R-02 / X-07: Send on an empty leave request focuses the first problem (in page order) and says what it needs in
    the field's words, not the browser's "Please fill out this field."."""
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'requests')
        dialog = new_request(page, 'Leave')
        form_submit(dialog).click()
        page.wait_for_function("() => document.activeElement && document.activeElement.getAttribute('aria-invalid') === 'true'")
        focus = page.evaluate(FOCUSED)
        assert focus['first'], focus
        assert OWN_WORDS.match(focus['message']), focus
        assert 'Please' not in focus['message']
        for message in dialog.locator('.field-error:visible').all_text_contents():
            assert 'Please' not in message, message
    finally:
        context.close()


def test_add_person_names_its_problems_and_its_save_button(browser, live_server, h):
    """X-07 / X-39: an empty Display name says "Add a display name"; the save button says what it creates ("Add person",
    not "Create record"); the short-code hint offers an automatic code only once there is a name to make it from."""
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'people')
        page.get_by_role('button', name='New person', exact=True).click()
        dialog = page.get_by_role('dialog', name='New person')
        save = dialog.get_by_role('button', name='Add person', exact=True)
        expect(save).to_be_visible()
        code_hint = dialog.get_by_label('Short code', exact=True).locator('xpath=ancestor::div[contains(@class,"field")][1]').locator('.hint')
        expect(code_hint).to_be_hidden()
        expect(dialog).not_to_contain_text('automatic code —')
        save.click()
        name = dialog.get_by_label('Display name', exact=True)
        expect(name).to_be_focused()
        expect(name).to_have_attribute('aria-invalid', 'true')
        assert page.evaluate(FOCUSED)['message'] == 'Add a display name'
        name.fill('Fictional Quartz Reyes')
        expect(name).not_to_have_attribute('aria-invalid', 'true')
        expect(code_hint).to_contain_text(re.compile(r'^Leave empty to use the automatic code [A-Z0-9]+\.$'))
    finally:
        context.close()


def test_form_steps_are_never_cut_off_on_a_phone(browser, live_server, h):
    """X-40: on a 390 phone the step links of a long form wrap instead of being cut at the right edge."""
    context, page = sign_in(browser, live_server, 'admin', {'width': 390, 'height': 844})
    try:
        navigate(page, 'people')
        page.get_by_role('button', name='New person', exact=True).click()
        dialog = page.get_by_role('dialog', name='New person')
        steps = dialog.locator('.stepper .step-link')
        expect(steps.first).to_be_visible()
        assert steps.count() >= 3
        cut = dialog.locator('.stepper').evaluate("""s => [...s.querySelectorAll('.step-link')].filter(b => {
            const r = b.getBoundingClientRect(), box = s.getBoundingClientRect(), label = b.querySelector('.truncate');
            return r.right > box.right + 1 || r.left < box.left - 1 || label.scrollWidth > label.clientWidth + 1;}).map(b => b.textContent)""")
        assert not cut, cut
    finally:
        context.close()
