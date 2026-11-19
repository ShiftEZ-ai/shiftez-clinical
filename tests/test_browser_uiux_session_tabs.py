"""Session editor keyboard tabs have named panels and preserve the saved duty."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser,live_server,sign_in,navigate,open_session,save_dialog,expect_when  # noqa: F401

pytestmark=pytest.mark.browser


def test_session_tabs_keyboard_panel_relationships_lazy_reads_and_save_reload(browser,live_server,h):
    w=h.shift(h.workspace(),start='2026-10-04T09:00:00+03:00',end='2026-10-04T12:30:00+03:00')['workspace']
    original=h.detail(w)['shifts'][0]
    context,page=sign_in(browser,live_server,'coordinator')
    reads=[];errors=[]
    paths={name:f"/api/workspaces/{w['id']}/shifts/{original['id']}/{name}" for name in ('rules','history')}
    try:
        page.on('pageerror',lambda error:errors.append(str(error)))
        navigate(page,'planner');open_session(page)
        page.get_by_role('button',name='Edit session',exact=True).click()
        # The preceding read-only session-details dialog loads its own rules; measure this editor only.
        page.on('request',lambda request:reads.append(request.url) if any(path in request.url for path in paths.values()) else None)
        form=page.locator('.dock-panel');tabs=form.get_by_role('tablist',name='Session editor',exact=True)
        expect(tabs.get_by_role('tab')).to_have_count(4)
        session=tabs.get_by_role('tab',name='Session',exact=True)
        staff=tabs.get_by_role('tab',name='Suggested staff',exact=True)
        checks=tabs.get_by_role('tab',name=re.compile(r'^Checks(?: · \d+)?$'))
        history=tabs.get_by_role('tab',name='History',exact=True)
        names=[session,staff,checks,history];ids=[]
        for tab in names:
            tab_id=tab.get_attribute('id');panel_id=tab.get_attribute('aria-controls')
            assert tab_id and panel_id;ids.extend([tab_id,panel_id])
            panel=form.locator('#'+panel_id)
            expect(panel).to_have_attribute('role','tabpanel')
            expect(panel).to_have_attribute('aria-labelledby',tab_id)
        assert len(set(ids))==8
        panel=lambda tab:form.locator('#'+tab.get_attribute('aria-controls'))
        expect(panel(session)).to_be_visible();expect(session).to_have_attribute('aria-selected','true')
        for tab in (staff,checks,history):expect(panel(tab)).to_be_hidden()
        expect_when(panel(session), 'Session start', '2026-10-04T09:00')
        expect_when(panel(session), 'Session end', '2026-10-04T12:30')
        assert reads==[]
        # Tabs activate manually: arrows and Home/End move focus, Enter opens the focused tab.
        session.focus();session.press('ArrowRight')
        expect(staff).to_be_focused();staff.press('Enter');expect(staff).to_have_attribute('aria-selected','true')
        expect(panel(staff)).to_be_visible();expect(panel(session)).to_be_hidden()
        staff.press('ArrowRight')
        expect(checks).to_be_focused();checks.press('Enter');expect(panel(checks)).to_be_visible()
        checks.press('End')
        expect(history).to_be_focused();history.press('Enter');expect(panel(history)).to_be_visible()
        expect(panel(history).get_by_role('heading',name='History',exact=True)).to_be_visible()
        history.press('Home')
        expect(session).to_be_focused();session.press('Enter');expect(panel(session)).to_be_visible()
        session.press('ArrowLeft')
        expect(history).to_be_focused();history.press('Enter');expect(panel(history)).to_be_visible()
        history.press('Home');session.press('Enter')
        expect(session).to_have_attribute('tabindex','0')
        for tab in (staff,checks,history):expect(tab).to_have_attribute('tabindex','-1')
        assert sum(paths['rules'] in url for url in reads)==1
        assert sum(paths['history'] in url for url in reads)==1
        save_dialog(form,'Save draft session')
        saved=h.detail(w)['shifts'][0]
        for key in ('assignment_type_id','location_id','service_ids','start','end','attributes'):assert saved[key]==original[key]
        fields=('person_id','participation','supervisor_person_id','team_lead','start','end','overtime','fixed','segments')
        assert [{key:p[key] for key in fields} for p in saved['participants']]==[{key:p[key] for key in fields} for p in original['participants']]
        page.reload();navigate(page,'planner');open_session(page)
        page.get_by_role('button',name='Edit session',exact=True).click()
        form=page.locator('.dock-panel');expect(form.get_by_role('tabpanel',name='Session',exact=True)).to_be_visible()
        expect_when(form, 'Session start', '2026-10-04T09:00')
        expect_when(form, 'Session end', '2026-10-04T12:30')
        assert not errors
    finally:context.close()


def test_session_tabs_steps_preference_short_form_fallback_saves_exact_values(browser,live_server,h):
    # This editor has two sections; an old saved "steps" preference changes nothing (forms are all on one page).
    w=h.shift(h.workspace(),start='2026-10-04T15:15:00+03:00',end='2026-10-04T18:45:00+03:00')['workspace']
    original=h.detail(w)['shifts'][0]
    context,page=sign_in(browser,live_server,'coordinator')
    try:
        page.evaluate("() => localStorage.setItem('rota.prefs', JSON.stringify({forms:'steps'}))")
        navigate(page,'planner');open_session(page)
        page.get_by_role('button',name='Edit session',exact=True).click()
        form=page.locator('.dock-panel');tabs=form.get_by_role('tablist',name='Session editor',exact=True)
        session=tabs.get_by_role('tab',name='Session',exact=True)
        history=tabs.get_by_role('tab',name='History',exact=True)
        expect(form.get_by_role('tabpanel',name='Session',exact=True)).to_be_visible()
        expect(form.get_by_role('radiogroup',name='Form layout',exact=True)).to_have_count(0)
        expect(form.locator('.form-step,.step-rail')).to_have_count(0)
        expect(form.get_by_role('button',name='Next',exact=True)).to_have_count(0)
        session.focus();session.press('End')
        expect(history).to_be_focused();history.press('Enter');expect(form.get_by_role('tabpanel',name='History',exact=True)).to_be_visible()
        history.press('Home')
        expect(session).to_be_focused();session.press('Enter');expect(form.get_by_role('tabpanel',name='Session',exact=True)).to_be_visible()
        expect_when(form, 'Session start', '2026-10-04T15:15')
        expect_when(form, 'Session end', '2026-10-04T18:45')
        # Calculate and explicitly save the actual short form, with no helper that could press a hidden calculator.
        calculate=form.get_by_role('button',name='Calculate draft impact',exact=True)
        expect(calculate).to_be_visible();calculate.click()
        expect(form.get_by_text('Not saved yet: review what this changes',exact=False)).to_be_visible(timeout=20000)
        save=form.get_by_role('button',name='Save draft session',exact=True)
        expect(save).to_be_visible();expect(save).to_be_enabled();save.click()
        expect(form).not_to_be_visible(timeout=20000)
        saved=h.detail(w)['shifts'][0]
        for key in ('assignment_type_id','location_id','service_ids','start','end','attributes'):assert saved[key]==original[key]
        fields=('person_id','participation','supervisor_person_id','team_lead','start','end','overtime','fixed','segments')
        assert [{key:p[key] for key in fields} for p in saved['participants']]==[{key:p[key] for key in fields} for p in original['participants']]
    finally:context.close()
# Include this file in hosted supervision and team-lead qualification.
