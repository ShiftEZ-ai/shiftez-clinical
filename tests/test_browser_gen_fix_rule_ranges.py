"""An impossible range is said beside the edited line before proposing.

The server's check says it (b18388c1b: contracts number_range_errors, "The maximum is below the minimum: no session
could meet it."), in the line panel's alert and in the check bar once the change is kept; nothing is saved. This
replaced the earlier at-field wording of cycle 5 (D-3), whose two tests in test_browser_cycle5_fixes.py it supersedes:
desktop light and phone dark, as they ran."""
import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401
from .test_browser_rule_catalogue import flag, catalogue_rule, line

pytestmark = [pytest.mark.browser, pytest.mark.postgres,
              pytest.mark.parametrize('h', ['postgresql'], indirect=True)]
RANGE = 'The maximum is below the minimum: no session could meet it.'


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (390, 'dark')])
def test_range_error_and_pool_warning_are_inline(browser, live_server, h, width, theme):
    flag(h)
    candidate = line({'min': {'value': 1, 'level': 'mandatory'}, 'max': {'value': 3, 'level': 'mandatory'}})
    catalogue_rule(h, 'people_per_session', [candidate])
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900}, color_scheme=theme)
    try:
        navigate(page, 'rules/people_per_session/' + candidate['id'])
        panel = page.locator('.rc-line-panel')
        expect(panel.get_by_role('img', name='No duplicate or overlap')).to_have_count(1)  # a valid line, checked
        panel.get_by_label('Minimum', exact=True).fill('40')
        panel.get_by_label('Maximum', exact=True).fill('1')
        # Seen without saving: the server's sentence, and no "No duplicate or overlap" tick.
        expect(panel.get_by_role('alert')).to_contain_text(RANGE)
        expect(panel.get_by_role('img', name='No duplicate or overlap')).to_have_count(0)
        panel.get_by_role('button', name='Keep change', exact=True).click()
        expect(page.locator('.rc-checkbar')).to_contain_text(RANGE)
        panel.get_by_label('Maximum', exact=True).fill('40')
        expect(panel.get_by_role('alert')).to_contain_text('Rule cannot be met')
        expect(panel.get_by_role('alert')).not_to_contain_text(RANGE)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        saved = next(e['rule'] for e in h.get('coordinator', '/api/rules/catalogue')['entries'] if e['id'] == 'people_per_session')
        assert saved['definition']['rule']['lines'][0]['numbers']['min']['value'] == 1
    finally:
        context.close()
