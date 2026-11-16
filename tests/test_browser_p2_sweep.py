"""Hands-on look at every Phase 2 screen (P2-FE) on the real backend (P2-BE): desktop 1440 and phone 390, light and
dark, saved to $P2_FE_SHOTS for a person to look at. Skipped unless P2_FE_SHOTS names a folder; the behaviour itself is
tested in test_browser_p2_groups."""
import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from .test_browser import auto_setup, browser, live_server, sign_in, navigate, open_rota, open_scheduling  # noqa: F401
from .test_browser_p2_groups import two_groups, fork, put, adjusted_rota, switcher, rota_tools, cp_server, run_jobs  # noqa: F401

pytestmark = [pytest.mark.browser, pytest.mark.skipif(not os.getenv('P2_FE_SHOTS'), reason='hands-on screenshots only')]
SIZES = {'desktop': {'width': 1440, 'height': 900}, 'phone': {'width': 390, 'height': 844}}
VARIANTS = [(size, scheme) for size in SIZES for scheme in ('light', 'dark')]


def save(page, name, size, scheme):
    """Popovers and panels fade in: wait until nothing is still animating (a spinner loops for ever), then keep the screen."""
    page.wait_for_function("() => document.getAnimations().every(a => a.playState !== 'running' || a.effect?.getTiming().iterations === Infinity)")
    folder = Path(os.environ['P2_FE_SHOTS'])
    folder.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(folder / f'sweep-{name}-{size}-{scheme}.png'))


@pytest.mark.parametrize('size,scheme', VARIANTS)
def test_sweep_planning(browser, cp_server, h, size, scheme):
    rota, mine, other = two_groups(h)
    draft = fork(h, rota)
    draft = put(h, draft, h.detail(draft)['shifts'][0], h.seed['people'][:2])
    context, page = sign_in(browser, cp_server, 'admin', SIZES[size], color_scheme=scheme)
    try:
        navigate(page, 'configuration/scheduling-groups')
        expect(page.locator('#main').get_by_text('Rota Coordinators and private drafts').first).to_be_visible()
        save(page, 'config-groups', size, scheme)
        page.locator('#main').get_by_role('button', name='New scheduling group').first.click()
        expect(page.get_by_role('dialog', name='New scheduling group')).to_be_visible()
        save(page, 'config-group-form', size, scheme)
    finally:
        context.close()
    context, page = sign_in(browser, cp_server, 'coordinator', SIZES[size], color_scheme=scheme)
    try:
        open_rota(page, rota['id'])
        expect(page.locator('.footer-group')).to_have_text(mine['definition']['name'])
        save(page, 'g2-planner', size, scheme)
        switcher(page)
        save(page, 'g1-drafts-picker', size, scheme)
        page.keyboard.press('Escape')
        navigate(page, f'schedule/issues?draft={rota["id"]}')
        expect(page.locator('#main').get_by_role('region', name='Other groups')).to_be_visible()
        save(page, 'g2-fix-list', size, scheme)
        if size == 'desktop':
            open_rota(page, rota['id'])
            open_scheduling(page)
            panel = page.locator('#auto-panel')
            auto_setup(panel)  # D76: the setup's fields open under Change
            expect(page.locator('#auto-panel').get_by_label(re.compile('^Leave room for'))).to_be_visible()
            save(page, 'g2-autoschedule', size, scheme)
        open_rota(page, draft['id'])
        expect(page.locator('.planner-bar .rota-status')).to_have_text('Private draft')
        save(page, 'g1-private-draft', size, scheme)
        rota_tools(page, 'Share with schedulers…')
        expect(page.get_by_role('dialog', name=re.compile('^Share '))).to_be_visible()
        save(page, 'g1-share', size, scheme)
        page.keyboard.press('Escape')
        run_jobs(h)
        page.locator('.planner-bar').get_by_role('button', name='Merge into rota…').click()
        panel = page.locator('.dock-panel.merge-panel')
        expect(panel).to_contain_text('Waiting to start')
        save(page, 'g3-merge-waiting', size, scheme)
        run_jobs(h)
        expect(panel.get_by_role('region', name='Need a look')).to_be_visible()
        save(page, 'g3-merge', size, scheme)
        panel.get_by_role('button', name=re.compile('^Merge ')).click()
        expect(panel.locator('.merge-foot')).to_contain_text('Waiting to start')
        save(page, 'g3-merge-applying', size, scheme)
    finally:
        context.close()


@pytest.mark.parametrize('size,scheme', VARIANTS)
def test_sweep_review(browser, live_server, h, size, scheme):
    rota = adjusted_rota(h)
    if size == 'desktop':
        context, page = sign_in(browser, live_server, 'coordinator', SIZES[size], color_scheme=scheme)
        try:
            open_rota(page, rota['id'])
            page.locator('.planner-bar').get_by_role('button', name='Submit for review…').click()
            expect(page.get_by_role('dialog', name=re.compile('^Submit '))).to_be_visible()
            save(page, 'g4-submit', size, scheme)
        finally:
            context.close()
    h.lifecycle(rota, 'submit')
    context, page = sign_in(browser, live_server, 'approver', SIZES[size], color_scheme=scheme)
    try:
        navigate(page, 'home')
        page.locator('#main').get_by_role('button', name=re.compile('^Review')).first.click()
        expect(page.get_by_role('dialog', name='Approve rota')).to_be_visible()
        save(page, 'g5-approve', size, scheme)
    finally:
        context.close()
