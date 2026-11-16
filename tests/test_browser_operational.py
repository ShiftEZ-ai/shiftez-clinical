"""Real browser, actual HTTP API and migrated DB for operational catalogue journeys."""
import re
import pytest
from playwright.sync_api import expect
from .test_browser import form_submit, browser, live_server, sign_in, navigate, save_dialog, fill_catalogue, planner_tool
from .test_series_reconciliation import template, materialise, amend_template

pytestmark=pytest.mark.browser


def test_multiple_patterns_share_one_review_and_save_in_planner(browser,live_server,h):
    template(h, days=(0,)); template(h, days=(1,))
    future = template(h, days=(0,), starts_on='2026-11-01', ends_on='2026-12-01')
    w = h.workspace()
    context,page = sign_in(browser,live_server,'coordinator')
    try:
        navigate(page,'planner')
        planner_tool(page, 'Add recurring sessions')
        form = page.get_by_role('dialog')
        # One field for one or several: always the shared multiple-choice picker (C-07), never a mode toggle.
        expect(form.get_by_label('Choose several',exact=True)).to_have_count(0)
        form.get_by_role('button',name='Recurring sessions: Choose recurring sessions',exact=True).click()
        picker=page.locator('.picker')
        expect(picker.get_by_text(future['name'],exact=True)).to_have_count(0)
        # Two choices: each is one tick, with no Select all on a list of three or fewer (X-37).
        expect(picker.get_by_role('option')).to_have_count(2)
        expect(picker.get_by_role('button',name=re.compile(r'^Select all'))).to_have_count(0)
        for option in picker.get_by_role('option').all():
            option.check()
        page.keyboard.press('Escape')  # closes the list only
        form.get_by_role('button',name='Calculate draft impact',exact=True).click()
        expect(form.get_by_text('Not saved yet: review what this changes',exact=False)).to_be_visible()
        assert h.detail(w)['shifts'] == []
        form.get_by_role('button',name='Add sessions',exact=True).click()
        expect(form).not_to_be_visible()
        assert len(h.detail(w)['shifts']) == 2
    finally:
        context.close()


def test_weekly_recurring_pattern_can_be_created_without_monthly_ordinal(browser,live_server,h):
    context,page=sign_in(browser,live_server,'admin')
    try:
        navigate(page,'configuration')
        page.get_by_role('button',name='Recurring sessions',exact=True).click()
        page.get_by_role('button',name='New recurring session',exact=True).click()
        form=fill_catalogue(page,'Weekly pattern without monthly restriction')
        form.get_by_label('Effective until (included)',exact=True).fill('2026-10-31')
        form.get_by_label('First week of the pattern',exact=True).fill('2026-10-01')
        form.get_by_label('Duty type',exact=True).select_option(h.seed['type'])
        form.get_by_label('Location',exact=True).select_option(h.seed['locations'][0])
        form.get_by_role('group',name='Clinics and services',exact=True).get_by_role('checkbox').first.check()
        form.get_by_role('checkbox',name='Monday',exact=True).check()
        expect(form.get_by_label('Monthly weekday',exact=True)).to_have_value('')
        if form.get_by_role('heading',name='Review configuration consequences',exact=True).count():
            expect(form.get_by_text(re.compile(r'active rotas? checked\.|Configuration checked\.|records will be saved\.')).first).to_be_visible(timeout=20000)
        with page.expect_response(lambda response: response.url.endswith('/api/commands')
                and response.request.method=='POST'
                and response.request.post_data_json.get('kind')=='catalogue.create'
                and response.request.post_data_json.get('payload',{}).get('catalogue')=='recurring_series') as created:
            form_submit(form).click()
        assert created.value.ok,created.value.text()
        expect(form).not_to_be_visible(timeout=10000)
        record=h.get('admin','/api/catalogues')['recurring_series'][0]
        assert record['definition'].get('month_occurrence') is None
        assert record['definition']['days'][0]['weekday']==0
    finally:
        context.close()


def test_opening_calendar_resource_and_exact_configuration_impact_in_real_ui(browser,live_server,h):
    context,page=sign_in(browser,live_server,'admin')
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        navigate(page,'configuration')
        page.get_by_role('button',name='Opening hours',exact=True).click()
        page.get_by_role('button',name='Set opening hours',exact=True).click()
        form=fill_catalogue(page,'Reviewed clinic opening')
        form.get_by_role('button',name='Add Sunday window',exact=True).click()
        form.get_by_label('Sunday opening start',exact=True).fill('08:00')
        form.get_by_label('Sunday opening end',exact=True).fill('12:00')
        form.get_by_role('button',name='Add date override',exact=True).click()
        form.get_by_label('Override date',exact=True).fill('2026-10-11')
        form.get_by_label('Override label',exact=True).fill('Explicit fictional closed holiday')
        save_dialog(form)
        page.get_by_role('button',name='Resource pools',exact=True).click()
        page.get_by_role('button',name='New resource pool',exact=True).click()
        form=fill_catalogue(page,'Shared examination kits')
        form.get_by_label('Pool capacity · units',exact=True).fill('2')
        form.get_by_label('Opening hours',exact=True).select_option(label='Reviewed clinic opening')
        # The impact is worked out as the form is filled in (no click), and nothing is saved by it.
        expect(form.get_by_text('Configuration checked.',exact=True)).to_be_visible()
        assert h.get('admin','/api/catalogues')['resource_pools']==[]
        # Save straight after a change: the impact for the new values is worked out first; it changes nothing, so the
        # same click saves.
        form.get_by_label('Pool capacity · units',exact=True).fill('3')
        form_submit(form).click()
        expect(form).not_to_be_visible()
        record=h.get('admin','/api/catalogues')['resource_pools'][0]
        assert record['capacity']==3 and record['calendar_id']
        assert not errors
    finally:
        context.close()


def test_following_occurrence_preview_and_apply_in_real_ui(browser,live_server,h):
    series=template(h);w=materialise(h,series);before=h.detail(w)['shifts'];series=amend_template(h,series)
    context,page=sign_in(browser,live_server,'coordinator')
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        navigate(page,'planner')
        planner_tool(page, 'Update recurring sessions')
        form=page.get_by_role('dialog')
        form.get_by_label('Recurring session',exact=True).select_option(series['id'])
        form.get_by_label('From',exact=True).fill('2026-10-05')
        form.get_by_label('Change reason',exact=True).fill('Reviewed same-series future timing')
        # The optional preview shows the impact and saves nothing.
        form.get_by_role('button',name='Calculate draft impact',exact=True).click()
        expect(form.get_by_text('Not saved yet: review what this changes',exact=False)).to_be_visible()
        assert h.detail(w)['shifts']==before
        # D-077: values changed after the preview still save on ONE press; Save never runs or waits for a preview.
        form.get_by_label('Change reason',exact=True).fill('Reviewed same-series future timing, confirmed')
        expect(form.get_by_text('Values changed. Recalculate draft impact.',exact=False)).to_be_visible()
        previews=[];page.on('request',lambda r:previews.append(r.url) if '/api/planner/preview' in r.url else None)
        form.get_by_role('button',name='Update sessions',exact=True).click()
        expect(form).not_to_be_visible()
        assert previews==[]
        after=h.detail(w)['shifts']
        assert after[0]==before[0] and after[1]['id']==before[1]['id'] and after[1]['series_revision']==2
        assert h.detail(w)['workspace']['status']=='draft' and not h.get('coordinator','/api/publications')['publications']
        assert not errors
    finally:
        context.close()
