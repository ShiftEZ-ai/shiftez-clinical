"""The real sign-in assets stay centred and the deployment is named without a routine notice."""
import re

import pytest
from playwright.sync_api import expect

from packages.contracts import schemas as s
from .test_browser import browser, live_server, new_context, open_app, route_fetch  # noqa: F401
from .test_browser_final_shared_controls import capture

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (1440, 'dark'), (390, 'light'), (390, 'dark')])
@pytest.mark.parametrize('deployment,environment,icon,label', [
    ('dev', 'production', 'flask', 'Test environment: test accounts only'),
    ('production', 'test', 'shield', 'Production: real rotas and people'),
])
def test_signin_brand_and_deployment_icon(browser, live_server, tmp_path, width, scheme,
                                         deployment, environment, icon, label):
    context = new_context(browser, {'width': width, 'height': 900 if width == 1440 else 844},
                          color_scheme=scheme)

    def config(route):
        # As in the routed sign-in fixture, retain the real answer and change display configuration only.
        reply = route_fetch(route)
        route.fulfill(response=reply, json={**reply.json(), 'deployment': deployment, 'environment': environment})

    context.route('**/auth/config', config)
    try:
        page = open_app(context, live_server)
        card = page.locator('.login-form')
        expect(card.get_by_role('heading', name='Sign in', exact=True)).to_be_visible()
        username = card.get_by_label('Username or email', exact=True)
        expect(username).to_be_focused()
        expect(card.get_by_role('button', name='Continue', exact=True)).to_be_in_viewport()
        expect(username).to_be_in_viewport()
        brand = card.locator('.login-brand')
        name = brand.locator('strong')
        expect(name).to_have_text(s.BRANDING_DEFAULTS['app_name'])
        expect(name).to_be_visible()
        expect(brand.locator('img')).to_have_count(0)
        centre = name.evaluate('n => {const r=n.getBoundingClientRect();return r.x+r.width/2;}')
        for container in (brand, card):
            box = container.bounding_box()
            assert abs(centre - (box['x'] + box['width'] / 2)) <= 1, (centre, box)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        visible = page.locator('body').inner_text()
        assert 'Test environment. Use your test account to sign in.' not in visible
        assert 'Shared departmental rota' not in visible

        badge = card.get_by_role('img', name=label, exact=True)
        expect(badge).to_be_visible()
        # The badge does nothing, so it is not a Tab stop (maintenance, WCAG 2.4.3): its name is its label, its help is on hover.
        expect(badge).to_have_js_property('tabIndex', -1)
        expect(badge.locator('svg')).to_have_attribute('data-icon', icon)
        expect(badge.locator('svg')).to_have_attribute('aria-hidden', 'true')
        expect(badge.locator('svg path')).to_have_attribute('d', re.compile('.+'))
        expect(badge.locator('.sr-only')).to_have_text('Production' if deployment == 'production' else 'Test environment')
        assert badge.evaluate('''n => [...n.querySelectorAll('*')].filter(e =>
            [...e.childNodes].some(c => c.nodeType === 3 && c.textContent.trim())
            && e.getBoundingClientRect().width > 2).map(e => e.textContent)''') == []
        assert badge.bounding_box()['width'] < username.bounding_box()['width'] / 4
        capture(page, tmp_path, f'signin-{width}-{scheme}-{deployment}')
        tooltip = page.get_by_role('tooltip')
        expect(tooltip).to_have_count(0)
        badge.hover()
        expect(tooltip).to_have_text(label)
        expect(tooltip).to_be_in_viewport()
        capture(page, tmp_path, f'signin-tooltip-{width}-{scheme}-{deployment}')
    finally:
        context.close()
