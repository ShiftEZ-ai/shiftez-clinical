"""First UX slice: real saved-view, scoped repair and setup journeys on fictional data."""
import re
from copy import deepcopy
from datetime import date
import uuid

import pytest
from playwright.sync_api import expect

from apps.api.services.issues import issue_key
from packages.domain.rota_summary import generation_fix_lists
from .test_browser import (minutes_words, browser, live_server, sign_in, navigate, open_rota, open_scheduling, auto_setup, auto_result,
                           choose, toolbar, close_popover, view_settings, save_dialog)
from .test_browser_autoschedule_review import FOUR, _open
from .test_browser_cp_sat import cp_server, _review
from .test_browser_legends import two_duties, key
from .legend_guard import legend_entries
from .test_browser_pairs import paired
from .browser_acceptance_support import shot
from .test_fill_options import companion_case
from .test_staff_wishes import department_settings
from .test_browser_audit_table_identity import expire, login_api
from apps.api import models as m
from .conftest import TEST_PASSWORD

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('view,scheme', FOUR)
def test_configured_duty_times_then_by_and_legend_survive_saved_view_edits(browser, live_server, h, view, scheme):
    w = paired(h, two_duties(h, 'Configured clocks'))
    w = h.shift(w, '2026-10-04T06:13:00+03:00', '2026-10-04T07:42:00+03:00')['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', viewport=view, color_scheme=scheme)
    bar = page.locator('.planner-toolbar')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        if view['width'] == 390:
            page.get_by_role('radiogroup', name='Calendar layout').get_by_role('radio', name='Grid', exact=True).click()
        choose(bar, 'Group', 'Group by', 'Duty time')
        choose(bar, 'Group', 'Then by', 'Service')
        heads = page.locator('.wk-group:not(.wk-subgroup) .wk-toggle strong')
        expect(heads).to_have_text(['06:13–07:42', '08:00–12:00', '20:00–06:00'])
        expect(page.locator('.wk-fact-time, .wk-fact-service')).to_have_count(0)
        expect(page.locator('.wk-subgroup .wk-toggle strong').first).to_have_text('Continuity clinic')
        collapse = page.locator('.wk-group:not(.wk-subgroup) .wk-toggle').first
        collapse.click()
        expect(collapse).to_have_attribute('aria-expanded', 'false')
        toolbar(bar, 'Saved views').get_by_role('menuitem', name='New view…', exact=True).click()
        form = page.get_by_role('dialog', name='New view')
        form.get_by_label('View name', exact=True).fill('Actual duty clocks')
        save_dialog(form, 'Save view')
        saved = h.get('coordinator', '/api/table-views?table=planner')['views'][0]['definition']['planner']
        assert (saved['group_by'], saved['subgroup'], saved['collapsed']) == ('time', 'service', ['06:13–07:42'])
        page.reload()
        if view['width'] == 390:  # Agenda/Grid is per-page; the saved view owns the grouping, not this phone switch.
            page.get_by_role('radiogroup', name='Calendar layout').get_by_role('radio', name='Grid', exact=True).click()
        expect(heads).to_have_text(['06:13–07:42', '08:00–12:00', '20:00–06:00'])
        expect(collapse).to_have_attribute('aria-expanded', 'false')
        collapse.click()
        toolbar(bar, 'Group').get_by_role('button', name='Move Service up', exact=True).click()
        close_popover(page)
        view_settings(bar).get_by_role('button', name='Update view', exact=True).click()
        expect(page.locator('.toast').filter(has_text='View updated')).to_be_visible()
        page.reload()
        if view['width'] == 390:
            page.get_by_role('radiogroup', name='Calendar layout').get_by_role('radio', name='Grid', exact=True).click()
        expect(page.locator('.wk-subgroup .wk-toggle strong')).to_have_text(['06:13–07:42', '08:00–12:00', '20:00–06:00'])
        saved = h.get('coordinator', '/api/table-views?table=planner')['views'][0]['definition']['planner']
        assert (saved['group_by'], saved['subgroup']) == ('service', 'time')
        # The same key/control owner on every relevant calendar surface.
        for layout in ('week', 'month', 'day', 'list'):
            navigate(page, f"schedule/{layout}/2026-10-05?edit={w['id']}")
            if layout == 'month':
                # Give the pair its own lane; a shared service can put the whole team behind More.
                choose(bar, 'Group', 'Group by', 'Duty type')
                team = page.locator('.wk-grid .team-box')
                expect(team).to_be_visible()
                expect(team.locator('button.pchip')).to_have_count(2)
            legend = key(page)
            expect(legend).to_have_count(1)
            with legend_entries(page, legend) as entries:
                expect(entries.filter(has_text='Night cover')).to_have_count(1)
            view_settings(bar)
            expect(page.get_by_role('dialog', name='View settings', exact=True)).to_be_visible()
            expect(bar.get_by_role('button', name=re.compile(r'^View settings: '))).to_have_attribute('aria-expanded', 'true')
            shot(page, f"ux-first-{layout}-settings-{view['width']}-{scheme}", full_page=False)
            close_popover(page)
            shot(page, f"ux-first-{layout}-{view['width']}-{scheme}", full_page=False)
    finally:
        context.close()


@pytest.mark.parametrize('pooled', [False, True], ids=['proven-true-false', 'unknown'])
def test_companion_repair_retains_warning_key_and_uses_server_resolution_proof(browser, live_server, h, pooled):
    w, shift, finding = companion_case(h, pooled=pooled)
    original = h.detail(w)['shifts']
    title = next(p['name'] for p in h.snapshot(w)['policies'] if p['id'] == finding['rule_id'])
    assert finding['severity'] == 'warning'
    companion, other = h.seed['people'][1:]
    names = {p['id']: p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people']}
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/issues?draft={w['id']}&view=checks")
        page.locator('.issues-list input.bulk-check').first.check()
        with page.expect_response(lambda r: r.url.endswith('/api/planner/fill-options')) as reply:
            page.get_by_role('toolbar', name='Bulk actions').get_by_role('button', name='Find someone…', exact=True).click()
        assert reply.value.ok, reply.value.text()
        assert reply.value.request.post_data_json['finding_key'] == issue_key(finding)
        candidates = {c['person_id']: c for c in reply.value.json()['candidates']}
        panel = page.locator('.wk-panel')
        expect(panel.locator('.wk-issue.warning')).to_contain_text(title)
        expect(panel.get_by_role('link', name='Open the rule', exact=True)).to_be_visible()
        expect(panel).not_to_contain_text('Every place on this session is filled')
        if pooled:
            assert all(c['resolves'] is None for c in candidates.values())
            expect(panel.locator('.wk-cand').first).to_be_visible()
            expect(panel).not_to_contain_text('Resolves ' + title)
            expect(panel).not_to_contain_text('Does not resolve ' + title + ' alone')
            return
        assert candidates[companion]['resolves'] is True and candidates[other]['resolves'] is False
        assert candidates[companion]['fills'] and candidates[other]['fills'], 'Headcount is already met'
        expect(panel.locator('.wk-cand').first).to_contain_text(names[companion])
        expect(panel.locator('.wk-cand').filter(has_text=names[companion])).to_contain_text('Resolves ' + title)
        expect(panel.locator('.wk-cand').filter(has_text=names[other])).to_contain_text('Does not resolve ' + title + ' alone')
        shot(page, 'ux-first-companion-warning-1440-light', full_page=False)
        # A non-resolving assignment stays advisory and cannot claim this rule is met.
        panel.locator('.wk-cand').filter(has_text=names[other]).get_by_role('button', name=re.compile(r'^Assign .* with a warning$' if candidates[other]['status'] == 'warn' else r'^Assign (?!.* with a warning$)')).click()
        expect(panel).to_contain_text(names[other] + ' added to the draft')
        expect(panel.locator('.wk-issue.warning')).to_be_visible()
        expect(panel).not_to_contain_text('met on this session')
        assert any(f['code'] == finding['code'] and f['rule_id'] == finding['rule_id'] for f in h.detail(w)['workspace']['validation']['findings'])
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.method == 'POST'
                                  and r.request.post_data_json.get('kind') == 'workspace.undo') as undone:
            page.locator('.toast').get_by_role('button', name='Undo', exact=True).click()
        assert undone.value.ok, undone.value.text()
        expect(page.locator('.toast').filter(has_text='Undone')).to_be_visible()
        # The ordinary assign flow previews, saves, rechecks and offers Undo.
        panel.locator('.wk-cand').filter(has_text=names[companion]).get_by_role('button', name=re.compile(r'^Assign (?!.* with a warning$)')).click()
        expect(panel).to_contain_text(title + ': met on this session.')
        assert not any(f['code'] == finding['code'] and f['rule_id'] == finding['rule_id'] for f in h.detail(w)['workspace']['validation']['findings'])
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.method == 'POST'
                                  and r.request.post_data_json.get('kind') == 'workspace.undo') as undone:
            page.locator('.toast').get_by_role('button', name='Undo', exact=True).click()
        assert undone.value.ok, undone.value.text()
        # The first Undo's message (it offers Redo, so it stays a while) may still be shown: this Undo's is the newest.
        expect(page.locator('.toast').filter(has_text='Undone').last).to_be_visible()
        assert h.detail(w)['shifts'] == original
    finally:
        context.close()


def test_reloaded_run_setup_rechecks_current_bounds_and_keeps_jobs_server_authoritative(browser, cp_server, h):
    w = h.shift(h.workspace('Durable setup'), people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'admin')
    try:
        open_rota(page, w['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        auto_setup(panel)  # D76: the setup's fields open under Change
        panel.get_by_label('Keep existing assignments', exact=True).uncheck()
        auto_setup(panel, fields=False)
        panel.get_by_role('radio', name='Fairness first', exact=True).click()
        panel.get_by_role('textbox', name='Search time', exact=True).fill('7')
        page.reload()
        open_rota(page, w['id'])
        open_scheduling(page)
        auto_setup(panel)  # D76: the setup's fields open under Change
        expect(panel.get_by_label('Keep existing assignments', exact=True)).not_to_be_checked()
        auto_setup(panel, fields=False)
        expect(panel.get_by_role('radio', name='Fairness first', exact=True)).to_be_checked()
        expect(panel.get_by_role('textbox', name='Search time', exact=True)).to_have_value('7 min')
        # A changed department ceiling wins over the saved intent after reload.
        h.command('admin', 'department.settings', {**department_settings(h),
                  'generation_defaults': {'max_search_minutes': 2, 'search_minutes': 1}}, h.seed['department_id'],
                  h.get('admin', '/api/me')['organisation']['revision'])
        page.reload()
        open_rota(page, w['id'])
        open_scheduling(page)
        auto_setup(panel, fields=False)
        search = panel.get_by_role('textbox', name='Search time', exact=True)
        expect(search).to_have_value('1 min')
        search.fill('3 min')
        expect(search).to_have_js_property('validationMessage', 'Between 1 min and 2 min.')
        expect(panel.get_by_role('progressbar')).to_have_count(0)
        expect(panel.get_by_role('button', name=re.compile('^Continue for'))).to_have_count(0)
        assert h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs'] == []
    finally:
        context.close()


def test_selected_success_keeps_whole_rota_blockers_and_comparison_requires_fresh_inputs(browser, cp_server, h):
    w = h.shift(h.workspace('Selected work and whole-rota blockers'), people=[])['workspace']
    context, page, panel = _open(browser, cp_server, h, w, {'width': 1440, 'height': 1000}, 'light')
    try:
        _review(page, h, w)
        latest = h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs'][0]
        base = h.get('coordinator', '/api/jobs/' + latest['id'])
        data = h.detail(w)
        findings = [{'code': code, 'severity': 'error', 'message': f'Whole-rota problem {i}',
                     'shift_id': None, 'person_id': h.seed['people'][i], 'rule_id': None,
                     'start': None, 'end': None, 'detail': {'display': f'{i + 1} of 4'}}
                    for i, code in enumerate(('TIME_OVERLAP', 'QUOTA_BELOW_MINIMUM', 'QUOTA_ABOVE_MAXIMUM'))]
        current = data['current_fingerprint']
        # The A3 result reads the server's whole-rota Fix lists (maintenance), projected here as the worker does it; the
        # before list stays the run's own (its one open place, the selected session this run fills).
        source = {**h.snapshot(w), 'fix_list_before': base['job']['result']['fix_list_before']}
        for freshness in ('fresh', 'stale', 'absent'):
            drawn, result = deepcopy(data), deepcopy(base)
            check = drawn['workspace']['validation']
            check['fingerprint'] = current if freshness == 'fresh' else 'older-inputs'
            check['findings'] = deepcopy(findings[:2])
            check['findings'][1]['detail'] = {'display': '0 of 4'}
            if freshness == 'absent':
                drawn['workspace']['validation'] = None
            job = result['job']
            job['session_ids'] = [data['shifts'][0]['id']]
            job['result']['validation'].update(findings=findings, error_count=3)
            summary = job['result']['summary']
            summary.update(open_places=[], open_place_count=0, required_places=1, filled_places=1)
            summary['rota_summary'].update(places_needed=1, places_filled=1, places_open=0, cause_groups=[])
            for key in ('score', 'score_saved'):
                if isinstance(summary.get(key), dict):
                    summary[key]['blocking_count'] = 3
            job['result'].update(generation_fix_lists(source, job['result']))
            after = job['result']['fix_list_after']
            # One must-fix cause per whole-rota problem: the selection's success does not hide any of them.
            assert job['result']['fix_list_before']['summary']['must'] == 1 and after['summary']['must'] == 3
            page.route('**/api/workspaces/' + w['id'] + '?view=schedule', lambda route: route.fulfill(json=drawn))
            page.route('**/api/jobs/' + latest['id'], lambda route: route.fulfill(json=result))
            page.reload()
            open_rota(page, w['id'])
            open_scheduling(page)
            auto_result(panel)
            review = panel.get_by_role('region', name='Review')
            status = review.get_by_role('status').first
            # Filling the selected place never reads as a rota ready to submit while whole-rota blockers remain.
            expect(status).to_contain_text('Worse: 2 new must-fix causes')
            for claim in ('Ready to submit', 'Nothing must be fixed', 'Better', 'cleared'):
                expect(status).not_to_contain_text(claim)
            expect(review.get_by_role('group', name='Result')).to_contain_text('Must fix3')
            # D76: the one next step is the top cause; the others are counted with the way to the Fix list.
            still = review.get_by_role('region', name='Still to fix').locator('li[data-level="must"]')
            expect(still).to_have_count(1)
            must = [c for c in after['causes'] if c['level'] == 'must']
            top = must[0]
            expect(still.and_(review.locator(f'li[data-cause="{top["id"]}"]'))).to_contain_text(top['sentence'])
            expect(review.get_by_role('button', name=f"{len(must) - 1} more causes in the Fix list", exact=True)).to_be_visible()
            # The comparison is the server's before/after Fix lists, made from current inputs (jobs.py reusable_check):
            # the draft's own check, fresh, stale or absent, never adds a client-side "since" comparison.
            expect(review.locator('.issue-since')).to_have_count(0)
            page.unroute_all(behavior='ignoreErrors')
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


@pytest.mark.parametrize('destination', ['account', 'department'])
def test_run_setup_does_not_follow_an_expired_session_to_another_account_or_department(browser, cp_server, h, destination):
    w = h.workspace('Private setup')
    target_role, target_department, target = 'admin', h.seed['department_id'], w
    context, page = sign_in(browser, cp_server, 'coordinator')
    if destination == 'department':
        target_role, target_department = 'coordinator', str(uuid.uuid4())
        with h.app.state.engine.begin() as db:
            db.execute(m.Department.__table__.insert().values(id=target_department,
                organisation_id=h.seed['organisation_id'], name='Setup isolation fixture'))
            db.execute(m.RoleBinding.__table__.insert().values(identity_id=h.seed['identities']['coordinator'],
                department_id=target_department, role='coordinator', starts_on=date(2020, 1, 1)))
        client = h.client('coordinator')
        assert client.post('/auth/login', json={'username': 'coordinator', 'password': TEST_PASSWORD,
                                               'department_id': target_department}).status_code == 200
        client.headers['x-csrf-token'] = client.cookies['rota_csrf']
        target = h.workspace('Other department setup')
    try:
        open_rota(page, w['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        auto_setup(panel, fields=False)
        panel.get_by_role('radio', name='Fairness first', exact=True).click()
        panel.get_by_role('textbox', name='Search time', exact=True).fill('7')
        expire(page, h, h.seed['identities']['coordinator'])
        current = login_api(context, cp_server, target_role, target_department)
        jobs = context.request.get(cp_server + '/api/jobs?workspace_id=' + target['id'])
        assert jobs.ok, jobs.text()
        default_minutes = current['department'].get('generation_defaults', {}).get('search_minutes',
            jobs.json()['generation_capability']['default_options']['search_minutes'])
        page.reload()
        open_rota(page, target['id'])
        open_scheduling(page)
        auto_setup(panel, fields=False)
        expect(panel.get_by_role('radio', name='Department defaults', exact=True)).to_be_checked()
        expect(panel.get_by_role('textbox', name='Search time', exact=True)).to_have_value(minutes_words(default_minutes))
        expect(panel.get_by_role('button', name=re.compile('^Continue for'))).to_have_count(0)
    finally:
        context.close()
