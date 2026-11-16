"""About Rota (account menu): the running release as the headline, whether the page, server and worker agree, the build
and engine facts, Copy details; the footer's environment badge names the release. Desktop and phone, light and dark."""
import re

import pytest
from playwright.sync_api import expect
from apps.api.schema import supported_revision
from apps.api.version import __version__, build
from apps.worker.main import process_one, _beat
from packages.domain.solver_rules import COMPILER_VERSION
from .test_browser import browser, live_server, sign_in  # noqa: F401

pytestmark = pytest.mark.browser


def open_about(page):
    page.get_by_role('button', name=re.compile(r'^Account menu, ')).click()
    page.get_by_role('dialog', name='Account').get_by_role('menuitem', name='About Rota').click()
    about = page.get_by_role('dialog', name='About Rota')
    expect(about.locator('.about-release')).to_be_visible()
    return about


def test_about_shows_one_release_with_its_facts_and_copies_them(browser, live_server, h):
    release = build()['release']
    _beat.clear()
    process_one(h.app.state.engine)  # the worker checks in with the release it runs
    context, page = sign_in(browser, live_server, 'staff')
    try:
        context.grant_permissions(['clipboard-read', 'clipboard-write'], origin=live_server)
        badge = page.locator('.app-footer .env-badge')
        expect(badge).to_have_attribute('data-tip', re.compile(re.escape(f'Release {__version__}') + '$'))
        about = open_about(page)
        expect(about.locator('.about-release')).to_have_text(__version__)
        expect(about.locator('.about-status')).to_have_text('Frontend and backend run the same release')
        expect(about.get_by_role('button', name='Reload to update')).to_have_count(0)
        # No subtitle and no Release row: the headline says it.
        expect(about.locator('.dialog-head p')).to_be_hidden()
        expect(about.locator('.about-facts dt', has_text='Release')).to_have_count(0)
        facts = about.locator('.about-facts')
        for label, value in (('Database schema', supported_revision()), ('Solver', COMPILER_VERSION),
                             ('Web page', release), ('Environment', 'Test')):
            expect(facts.locator('dt', has_text=label).first).to_be_visible()
            expect(facts).to_contain_text(value)
        about.get_by_role('button', name='Copy details').click()
        expect(page.get_by_role('status').filter(has_text='Details copied.')).to_be_visible()
        copied = page.evaluate('navigator.clipboard.readText()').replace('\r\n', '\n')  # Windows clipboard line ends
        assert copied.startswith(f'Rota {release}\n') and f'Solver: {COMPILER_VERSION}' in copied
        page.keyboard.press('Escape')
        expect(about).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('viewport,scheme,version', [({'width': 1440, 'height': 1000}, 'light', '9.9.9'),
                                                     ({'width': 390, 'height': 844}, 'dark', __version__)],
                         ids=['desktop-light-newer-version', 'phone-dark-same-version-other-commit'])
def test_about_names_a_stale_page_and_a_worker_on_another_release(browser, live_server, h, viewport, scheme, version):
    # A DEV build of the same version from another commit is a different build too (version+commit is compared).
    release = build()['release']
    context, page = sign_in(browser, live_server, 'coordinator', viewport=viewport)
    try:
        page.emulate_media(color_scheme=scheme)
        # The real About answer, read before routing: the route fulfils at once instead of forwarding over a second
        # connection (a reset keep-alive socket left the request unanswered and the dialog on Loading…).
        real = h.get('coordinator', '/api/about')

        page.route('**/api/about', h.route_json('coordinator', '/api/about', lambda body: {
            **body, 'version': version, 'release': version + '+abc1234',
            'worker': {'release': '9.9.8', 'last_seen_at': '2026-10-09T08:00:00+00:00'}}))
        about = open_about(page)
        expect(about.locator('.about-release')).to_have_text(version)
        status = about.locator('.about-status .badge.warn')
        expect(status).to_have_text([f'This page is {release}; the server runs {version}+abc1234',
                                     f'The worker runs 9.9.8; the server runs {version}+abc1234'])
        expect(about.locator('.badge.ok')).to_have_count(0)
        expect(about.get_by_role('button', name='Reload to update')).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        assert about.evaluate('d => d.getBoundingClientRect().right <= innerWidth')
    finally:
        context.close()


def test_about_says_plainly_when_the_worker_has_not_been_seen(browser, live_server, h):
    _beat.clear()  # no worker has checked in
    context, page = sign_in(browser, live_server, 'admin')
    try:
        page.route('**/api/about', h.route_json('admin', '/api/about', lambda body: {**body, 'worker': None}))
        about = open_about(page)
        expect(about.locator('.about-status .badge.warn')).to_have_text(re.compile(r'^The worker has not been seen yet; the server runs '))
        expect(about.locator('.about-facts')).to_contain_text('Not seen yet')
    finally:
        context.close()


def test_about_says_plainly_when_the_worker_has_not_reported_its_release(browser, live_server, h):
    """A worker that checks in without a release (an older image still running during a deploy): plain words, not
    "The worker runs not reported"."""
    context, page = sign_in(browser, live_server, 'admin')
    try:
        page.route('**/api/about', h.route_json('admin', '/api/about', lambda body: {
            **body, 'worker': {'release': None, 'last_seen_at': '2026-10-13T04:41:30+00:00'}}))
        about = open_about(page)
        expect(about.locator('.about-status .badge.warn')).to_have_text(re.compile(r'^The worker has not reported its release; the server runs '))
        expect(about.locator('.about-facts')).to_contain_text('release not reported · seen')
    finally:
        context.close()


@pytest.mark.parametrize('dev_build', ['DEV build, 3 merges after v0.8.0-rc.15', None], ids=['dev-build', 'no-dev-build'])
def test_about_shows_the_dev_build_line_only_when_the_server_sends_it(browser, live_server, h, dev_build):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        page.route('**/api/about', h.route_json('admin', '/api/about', lambda body: {**body, 'dev_build': dev_build}))
        about = open_about(page)
        if dev_build:
            expect(about.locator('.about-dev-build')).to_have_text(dev_build)
        else:
            expect(about.locator('.about-dev-build')).to_have_count(0)
            expect(about).not_to_contain_text('DEV build')
    finally:
        context.close()
