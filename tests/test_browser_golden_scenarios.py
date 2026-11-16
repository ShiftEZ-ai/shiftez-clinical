"""Cloud/rack journey: the same golden API seed generates, applies and survives reload."""
import math
import re

import pytest
from playwright.sync_api import expect

from apps.worker.main import process_one
from packages.domain.validation import evaluate
from scripts.golden_scenarios import compile_scenario, load_pack
from .test_browser import wait_run_created, browser, live_server, navigate, open_scheduling, show_names, sign_in  # noqa: F401
from .test_browser_cp_sat import APPLY_ALL, cp_server  # noqa: F401
from .test_staff_wishes import department_settings

pytestmark = pytest.mark.browser


def test_golden_cover_generate_apply_reload(browser, cp_server, h):
    spec = next(row for row in load_pack() if row['id'] == 'A-COVER-01')
    compiled = compile_scenario(h.command, spec, read=h.get, committed=h.committed,
        work=lambda: process_one(h.app.state.engine, solver_workers=1))
    workspace, ids = compiled['workspace'], compiled['ids']
    # These are the disposable test department's real Configuration values, not a per-run scoring bypass.
    me = h.get('admin', '/api/me')
    h.command('admin', 'department.settings', {
        **department_settings(h), 'generation_defaults': spec['inputs']['generation_options']},
        me['department']['id'], me['organisation']['revision'])
    h.refresh_draft_baseline(h.detail(workspace)['workspace'])  # the defaults are newer than the draft's baseline
    context, page = sign_in(browser, cp_server, 'coordinator')
    route = f"schedule/list/2027-01-03?edit={workspace['id']}"
    try:
        navigate(page, route)
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
        expect(panel.get_by_role('progressbar', name='Auto-schedule progress')).to_be_visible()
        wait_run_created(page)
        job = h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])['jobs'][0]
        assert job['generation_engine'] == 'cp_sat'
        assert job['effective_options']['scoring']['weights'] == spec['inputs']['generation_options']['scoring']['weights']
        process_one(h.app.state.engine, solver_workers=1)
        panel.get_by_role('button', name='Review latest result', exact=True).click(timeout=20000)
        review = page.get_by_role('dialog', name='Auto-schedule results', exact=True)
        completed = h.get('coordinator', '/api/jobs/' + job['id'])['job']
        assert completed['status'] == 'succeeded'
        assert completed['result']['status'] == 'candidate_feasible'
        assert not h.detail(workspace)['shifts'][0]['participants']
        review.get_by_role('button', name=APPLY_ALL).click()
        expect(review).not_to_be_visible()
        while process_one(h.app.state.engine):  # the worker finishes the complete check Apply queued (maintenance)
            pass
        applied = h.detail(workspace)
        session = next(row for row in applied['shifts'] if row['id'] == ids['cover_shift'])
        people = {row['person_id'] for row in session['participants']}
        assert len(session['participants']) == len(people) >= spec['expect']['boundary']['minimum']
        assert people <= {ids['p0'], ids['p1'], ids['p2']}
        assert evaluate(h.snapshot(applied['workspace']))['valid']
        assert applied['workspace']['status'] == 'draft'
        assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['status'] == 'applied'
        assert not h.get('coordinator', '/api/publications')['publications']

        page.reload()
        navigate(page, route)
        show_names(page, 'full')
        row = page.locator(f"#schedule-view tr[data-session='{ids['cover_shift']}']")
        expect(row).to_contain_text(compiled['records']['cover_type']['name'])
        names = {person['id']: person['display_name']
                 for person in h.get('coordinator', '/api/catalogues')['people']}
        for person in people:
            expect(row.get_by_text(names[person], exact=True)).to_be_visible()
        assert next(row for row in h.detail(workspace)['shifts'] if row['id'] == session['id'])['participants'] == session['participants']

        score = h.detail(workspace)['workspace']['validation']['score']
        shown = score.get('uncapped_score', score['score'])
        assert h.get('coordinator', f"/api/workspaces/{workspace['id']}/fix-list")['summary']['must'] == 0
        # A7: nothing must be fixed, so the footer shows the score; its popover's Score breakdown explains it.
        pill = page.locator('.app-footer').get_by_role('button', name=f'Ready to submit · score {shown:g}', exact=True)
        expect(pill).to_be_visible()
        pill.click()
        page.locator('.popover.fix-pop').get_by_role('button', name='Score breakdown', exact=True).click()
        why = page.get_by_role('dialog', name='Why this score', exact=True)
        expect(why.get_by_text(f'Schedule score {shown:g} / 100', exact=True)).to_be_visible()
        expect(why).to_contain_text(compiled['records']['bait']['name'])
        labels = page.evaluate("async () => (await window.importBundle('score.js')).COMPONENT_LABELS")
        for name, component in score['components'].items():
            if component['value'] is not None and component['weight']:
                percent = math.floor(component['value'] * 100 + 0.5)
                meter = why.get_by_role('meter', name=f'{labels[name]} {percent}%', exact=True)
                expect(meter).to_be_visible()
                assert float(meter.get_attribute('value')) == pytest.approx(component['value'])
    finally:
        context.close()
