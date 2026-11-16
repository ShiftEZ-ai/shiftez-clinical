"""Operations identifies the deployment, independently of runtime security mode."""
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in, keyboard_focus  # noqa: F401

pytestmark = pytest.mark.browser


@pytest.fixture(autouse=True, params=['dev', 'production'])
def deployment(request, monkeypatch):
    # Display labels only: the API still runs in test mode against a disposable fixture.
    monkeypatch.setenv('ROTA_DEPLOYMENT', request.param)
    return request.param


def test_operations_and_footer_identify_the_same_deployment(browser, live_server, deployment):
    context, page = sign_in(browser, live_server, 'operator')
    try:
        me = context.request.get(live_server + '/api/me').json()
        assert me['deployment'] == deployment
        navigate(page, 'system/operations')
        service = page.locator('.ops-tile').filter(has=page.get_by_text('Service', exact=True))
        expect(service.locator('strong')).to_have_text('Production' if deployment == 'production' else 'Test')
        expect(page.locator('.env-badge')).to_contain_text('Production' if deployment == 'production' else 'Test')
    finally:
        context.close()


@pytest.mark.parametrize('width', [1440, 390])
@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_operations_help_is_readable_and_keyboard_accessible(browser, live_server, width, scheme):
    context, page = sign_in(browser, live_server, 'operator', viewport={'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'system/operations')
        help_button = page.get_by_role('button', name='How these are measured', exact=True)
        keyboard_focus(help_button)
        hint = page.get_by_role('tooltip')
        expect(hint).to_have_text('How these are measured')
        assert hint.evaluate('n => {const r=n.getBoundingClientRect();return r.left>=8 && r.right<=innerWidth-8 && r.top>=8 && r.bottom<=innerHeight-8 && n.scrollWidth<=n.clientWidth;}')
        expect(help_button).to_be_focused()
        help_button.press('Enter')
        expect(hint).to_have_count(0)
        details = page.get_by_role('dialog', name='How these are measured', exact=True)
        # A-20: plain wording instead of the API's technical basis and metric version.
        expect(details).to_have_text('Counts are read from the database when the page loads; each queue is read separately, '
                                     'so they can differ by a few seconds.')
        box = details.bounding_box()
        assert box and box['x'] >= 0 and box['x'] + box['width'] <= width
        assert details.evaluate('(node) => node.scrollWidth <= node.clientWidth')
        page.keyboard.press('Escape')
        expect(details).to_have_count(0)
        expect(help_button).to_be_focused()
        # The returned control remains usable; a fresh focus shows the whole hint at the right-hand edge again.
        page.locator('#main').focus()
        keyboard_focus(help_button)
        expect(hint).to_have_text('How these are measured')
        assert hint.evaluate('n => {const r=n.getBoundingClientRect();return r.left>=8 && r.right<=innerWidth-8;}')
        page.keyboard.press('Escape')
        expect(hint).to_have_count(0)
        expect(help_button).to_be_focused()
    finally:
        context.close()
