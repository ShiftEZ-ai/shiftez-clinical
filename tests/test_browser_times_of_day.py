"""Configuration › Times of day: the department's parts of the day are listed by start with where each ends, a name,
code or start used twice is said on both rows before saving, a save asks for a reason and Undo brings back the
defaults. Admin, 1440 light and 390 dark."""
import pytest
from playwright.sync_api import expect

from packages.contracts import schemas as s
from .test_browser import browser, live_server, sign_in, navigate, save_dialog  # noqa: F401

pytestmark = pytest.mark.browser

PURE = """async () => { const {timeOfDayAt, untilWords} = await window.importBundle('times_of_day.js');
  const {TIMES_OF_DAY_DEFAULTS: t} = await window.importBundle('generated.js');
  return {at: ['03:00', '06:00', '11:59', '12:00', '20:59', '21:00', '23:59'].map(h => timeOfDayAt(t, h).code),
          until: untilWords([{start: '19:00'}, {start: '07:30'}])}; }"""


@pytest.mark.parametrize('view,scheme', [({'width': 1440, 'height': 900}, 'light'), ({'width': 390, 'height': 844}, 'dark')])
def test_times_of_day_are_checked_saved_with_a_reason_and_undone(browser, live_server, h, view, scheme):
    context, page = sign_in(browser, live_server, 'admin', viewport=view)
    page.emulate_media(color_scheme=scheme)
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        navigate(page, 'configuration/times-of-day')
        expect(page.locator('.config-pane .section-head h2').first).to_have_text('Times of day')
        rows = page.locator('.tod-row')
        expect(rows).to_have_count(4)
        expect(page.locator('.tod-until')).to_have_text(['Until 12:00', 'Until 17:00', 'Until 21:00', 'Until 06:00 next day'])
        expect(rows.nth(0).locator('.tod-code')).to_have_text('M')
        expect(rows.nth(0).locator('.tod-code')).to_have_class('tod-code duty-blue')
        # A code used twice is said on both rows, and the field is marked invalid.
        code = rows.nth(1).get_by_label('Code', exact=True)
        code.fill('m')
        expect(code).to_have_value('M')
        expect(code).to_have_attribute('aria-invalid', 'true')
        expect(rows.nth(1).locator('.field-error:visible')).to_have_text('Morning already uses M')
        expect(rows.nth(0).locator('.field-error:visible')).to_have_text('Afternoon already uses M')
        code.fill('L')
        expect(page.locator('.tod-row .field-error:visible')).to_have_count(0)
        rows.nth(1).get_by_label('Starts', exact=True).fill('13:00')
        expect(page.locator('.tod-until').first).to_have_text('Until 13:00')
        page.get_by_role('region', name='Unsaved changes').get_by_role('button', name='Save', exact=True).click()
        dialog = page.get_by_role('dialog', name='Save times of day')
        dialog.get_by_label('Reason', exact=True).fill('Our afternoon starts later')
        save_dialog(dialog, 'Save')
        toast = page.locator('.toast').filter(has_text='Times of day saved.')
        expect(toast).to_be_visible()
        saved = h.get('admin', '/api/me')['department']['display_settings']['times_of_day']
        assert saved[1] == {**s.TIMES_OF_DAY_DEFAULTS[1], 'code': 'L', 'start': '13:00'}
        toast.get_by_role('button', name='Undo', exact=True).click()
        expect(page.locator('.toast').filter(has_text='Undone.')).to_be_visible()
        assert h.get('admin', '/api/me')['department']['display_settings']['times_of_day'] == s.TIMES_OF_DAY_DEFAULTS
        expect(rows.nth(1).get_by_label('Code', exact=True)).to_have_value('A')
        # A new one starts an hour after the last, with a free code, and its name is ready to type over.
        page.get_by_role('button', name='Add time of day', exact=True).click()
        expect(rows).to_have_count(5)
        added = rows.nth(4)
        expect(added.get_by_label('Name', exact=True)).to_be_focused()
        expect(added.get_by_label('Name', exact=True)).to_have_value('Time of day 5')
        expect(added.get_by_label('Starts', exact=True)).to_have_value('22:00')
        expect(added.get_by_label('Code', exact=True)).to_have_value('B')
        added.get_by_role('button', name='Remove Time of day 5', exact=True).click()
        expect(rows).to_have_count(4)
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
        got = page.evaluate(PURE)
        assert got['at'] == ['N', 'M', 'M', 'A', 'E', 'N', 'N']
        assert got['until'] == ['Until 07:30 next day', 'Until 19:00']
        assert errors == []
    finally:
        context.close()
