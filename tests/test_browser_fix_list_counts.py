"""One Fix list, one count (D9): Home, the footer, Rota tools, the Fix list page and Submit all say the server's must-fix
causes for the same draft (GET /api/workspaces/{id}/fix-list), never a client recount. Real backend data (maintenance)."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, open_rota, save_dialog  # noqa: F401

pytestmark = pytest.mark.browser


def test_home_footer_fix_list_and_submit_say_the_same_must_fix_count(browser, live_server, h):
    h.policy({'kind': 'minimum_cover', 'minimum': 2})
    w = h.shift(h.workspace('Fix list one count'), people=[])['workspace']
    fix = h.get('coordinator', f"/api/workspaces/{w['id']}/fix-list")
    must, could = fix['summary']['must'], fix['summary']['could']
    assert must >= 1 and must == sum(c['level'] == 'must' for c in fix['causes'])
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        navigate(page, 'home')
        row = page.locator('.home-needs-list li').filter(has_text='Fix list one count')
        expect(row.locator('.home-count')).to_have_text(str(must))
        open_rota(page, w['id'])
        expect(page.locator('.app-footer .fix-pills').get_by_role('button', name=re.compile(f'^{must} must fix'))).to_be_visible()
        if could:
            expect(page.locator('.app-footer .fix-pills')).to_contain_text(f'{could} could improve')
        page.get_by_role('button', name=re.compile(r'^Submit for review')).first.click()
        dialog = page.get_by_role('dialog', name=re.compile('^Submit .* for review$'))
        expect(dialog.locator('.fix-gate')).to_contain_text(f'{must} cause')
        expect(dialog.locator('.fix-row')).to_have_count(must)
        expect(dialog.locator('button[type=submit]')).to_be_disabled()
        page.keyboard.press('Escape')
        navigate(page, f'schedule/issues?draft={w["id"]}')
        expect(page.locator('#main .fix-gate')).to_contain_text(f'{must} must fix before you submit')
        expect(page.locator('#main section[data-level=must] .fix-level .badge')).to_have_text(str(must))
        # FIXLIST-UX: each row's line is the server's plain one; the scorer's units (checks) are never on a row.
        metas = page.locator('.fix-list > li[data-cause] .home-need-text small')
        expect(metas.first).to_be_visible()
        for meta in metas.all_text_contents():
            assert not re.search(r'\b\d+ checks?\b', meta), meta
    finally:
        context.close()


def test_adding_unstaffed_session_reports_the_fix_list_total_in_the_undo_message(browser, live_server, h):
    duty_types = [
        h.configure('assignment_types', {'name': f'Cover for {minimum}', 'starts_on': '2020-01-01',
            'definition': {'requirements': [{'minimum': minimum, 'maximum': minimum}]}})['record']['id']
        for minimum in (2, 4)
    ]
    w = h.workspace('Session save counts')
    # Two checks with the same missing headcount form one cause. The new duty needs different cover.
    for day in ('2026-10-04', '2026-10-05'):
        w = h.shift(w, start=f'{day}T08:00:00+03:00', end=f'{day}T12:00:00+03:00',
                    typ=duty_types[0], people=[])['workspace']
    fix_path = f"/api/workspaces/{w['id']}/fix-list"
    assert h.get('coordinator', fix_path)['summary']['must'] == 1

    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        open_rota(page, w['id'])
        pills = page.locator('.app-footer .fix-pills')
        expect(pills.get_by_role('button', name=re.compile(r'^1 must fix'))).to_be_visible()
        page.locator('#main').get_by_role('button', name='Add session', exact=True).click()
        form = page.locator('.dock-panel')
        form.get_by_label('Duty type', exact=True).select_option(duty_types[1])
        form.get_by_label('Location', exact=True).select_option(h.seed['locations'][0])
        form.locator(f"fieldset[name=Clinics] input[value='{h.seed['services'][0]}']").check()
        form.get_by_label('Session start', exact=True).fill('2026-10-06T08:00')
        form.get_by_label('Session end', exact=True).fill('2026-10-06T12:00')
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and
                                  r.request.post_data_json.get('kind') == 'shift.create') as saved:
            save_dialog(form, 'Save draft session')
        assert saved.value.ok, saved.value.text()
        result = saved.value.json()
        must = result['fix_list_delta']['summary']['must']
        assert result['validation']['error_count'] == 3
        assert must == 2
        messages = page.get_by_role('region', name='Messages')
        toast = messages.locator('.toast').filter(has_text=f'Session added. {must} must fix.')
        expect(toast.get_by_role('button', name='Undo', exact=True)).to_be_visible()
        expect(messages.get_by_text(re.compile(r'^Draft saved with .* mandatory findings?'))).to_have_count(0)
        expect(pills.get_by_role('button', name=re.compile(f'^{must} must fix'))).to_be_visible()
        navigate(page, f'schedule/issues?draft={w["id"]}')
        expect(page.locator('#main .fix-gate')).to_contain_text(f'{must} must fix before you submit')
    finally:
        context.close()
