import copy
from dataclasses import replace

import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate, open_scheduling, open_rota

pytestmark = pytest.mark.browser


def adds_label(count):
    """The review's label for a rota where the rule adds problems (rule_review.ts)."""
    return f'Adds {count} problem{"" if count == 1 else "s"}'


def no_new_label(result):
    """The review's label for a rota where the rule adds no problems (rule_review.ts)."""
    count = result['unchanged_existing_count']
    if count:
        return f'No new problems from this rule · {count} existing problem{"" if count == 1 else "s"} unchanged'
    return 'Works with this rule' if result['full_feasibility_established'] else 'No new problems from this rule'


@pytest.fixture
def checked_rule_server(h, request):
    from apps.api.main import create_app
    h.app.state.engine.dispose()
    h.settings = replace(h.settings, generation_engine='cp_sat')
    h.app = create_app(h.settings)
    h.server_env = {'ROTA_GENERATION_ENGINE': 'cp_sat'}
    return request.getfixturevalue('live_server')


def test_rule_editor_checks_before_save_and_recovers_after_correction(browser, live_server, h, monkeypatch):
    from packages.domain import solver_rules
    monkeypatch.setattr(solver_rules, "SUPPORTED_RULES", solver_rules.SUPPORTED_RULES - {"max_hours"})
    context, page = sign_in(browser, live_server, 'coordinator')
    navigate(page, 'rules')
    page.get_by_role('button', name='Add rule', exact=True).click()
    dialog = page.get_by_role('dialog')
    expect(dialog.get_by_role('radio', name='Maximum hours', exact=True)).to_have_count(0)
    dialog.get_by_label('Rule title', exact=True).fill('FM clinic balancing')
    dialog.get_by_label('Policy owner', exact=True).fill('FM coordinator')
    dialog.get_by_label('Source or departmental decision', exact=True).fill('Synthetic rule compatibility check')
    dialog.get_by_role('radio', name='Allocation preference', exact=True).check()
    dialog.get_by_label('Balance assignments', exact=True).select_option('service_mode')
    dialog.get_by_label('Balancing weight', exact=True).fill('0.1234567')
    expect(dialog.get_by_text('Use a simpler fraction for this value.', exact=True)).to_be_visible()
    dialog.get_by_role('button', name='Propose rule', exact=True).click()
    expect(dialog.get_by_text('Resolve the rule check before saving.', exact=True)).to_be_visible()
    assert not any(p['name'] == 'FM clinic balancing' for p in h.get('coordinator', '/api/catalogues')['policies'])
    dialog.get_by_label('Balancing weight', exact=True).fill('0.25')
    expect(dialog.get_by_text('Rule settings supported. Check a rota before activation.', exact=True)).to_be_visible()
    expect(dialog.get_by_text('Resolve the rule check before saving.', exact=True)).to_have_count(0)
    dialog.get_by_role('button', name='Propose rule', exact=True).click()
    expect(dialog).not_to_be_visible()
    assert next(p for p in h.get('coordinator', '/api/catalogues')['policies'] if p['name'] == 'FM clinic balancing')['status'] == 'proposed'
    context.close()


def test_unsaved_fm_rule_is_checked_through_editor_without_saving(browser, live_server, h):
    from apps.worker.main import process_one
    w = h.shift(h.workspace('FM clinic draft'), people=[])['workspace']
    before = h.snapshot(w)
    context, page = sign_in(browser, live_server, 'coordinator')
    navigate(page, 'rules')
    page.get_by_role('button', name='Add rule', exact=True).click()
    editor = page.get_by_role('dialog', name='Add rule', exact=True)
    editor.get_by_label('Rule title', exact=True).fill('FM protected clinic day')
    editor.get_by_label('Policy owner', exact=True).fill('FM coordinator')
    editor.get_by_label('Source or departmental decision', exact=True).fill('Unsaved rule browser trial')
    editor.get_by_label('Effective from', exact=True).fill('2026-01-01')
    editor.get_by_role('button', name='Check against rotas', exact=True).click()
    review = page.get_by_role('dialog', name='Review FM protected clinic day', exact=True)
    review.get_by_role('button', name='Check rota', exact=True).click()
    expect(review.get_by_text('Waiting to check', exact=True)).to_be_visible()
    process_one(h.app.state.engine)
    expect(review.get_by_text('This rule introduces a conflict', exact=True)).to_be_visible(timeout=15000)
    expect(review.get_by_role('button', name='Activate', exact=True)).to_have_count(0)
    assert not any(p['name'] == 'FM protected clinic day' for p in h.get('coordinator', '/api/catalogues')['policies'])
    assert h.snapshot(w) == before
    context.close()


def test_planner_does_not_present_rule_checks_as_scheduling_runs(browser, live_server, h):
    from apps.worker.main import process_one
    w = h.shift(h.workspace('FM rule-check separation'), people=[])['workspace']
    policy = h.command('coordinator', 'policy.create', {
        'name': 'FM Monday protection', 'owner': 'FM coordinator', 'source': 'Browser separation check',
        'starts_on': '2026-01-01', 'definition': {'rule': {'kind': 'restriction', 'weekdays': [0]}}})['record']
    h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'feasibility',
        'rule_changes': [{'policy_id': policy['id'], 'expected_revision': policy['revision']}]},
        revision=w['revision'])
    context, page = sign_in(browser, live_server, 'coordinator')
    navigate(page, 'planner')
    open_scheduling(page)
    open_rota(page, w['id'])
    # D76: a rule's effect check is not this rota's run: the panel stays on its setup, never shows the check or its
    # Cancel, and Run stays offered.
    panel = page.locator('#auto-panel')
    expect(panel.locator('.auto-body')).to_have_attribute('data-state', 'setup', timeout=15000)
    expect(panel.get_by_role('button', name='Cancel check', exact=True)).to_have_count(0)
    expect(page.get_by_role('button', name='Run auto-schedule', exact=True)).to_be_visible()
    process_one(h.app.state.engine)
    expect(panel.locator('.auto-body')).to_have_attribute('data-state', 'setup')
    expect(page.get_by_role('button', name='Review latest result', exact=True)).to_have_count(0)
    context.close()


@pytest.mark.parametrize('width, color_scheme', [(1440, 'light'), (390, 'dark')])
@pytest.mark.parametrize('outcome', ['unchanged', 'added', 'incomplete', 'incomplete-advisory'],
                         ids=['existing-unchanged', 'fewer-candidates', 'incomplete-baseline', 'incomplete-advisory'])
def test_rule_activation_acknowledges_only_new_problems(browser, checked_rule_server, h, width,
                                                       color_scheme, outcome):
    from apps.api import models as m
    from apps.api.db import transaction
    from apps.worker.main import process_one
    from packages.contracts.schemas import NEVER_OVERRIDDEN
    from packages.domain.feasibility import compare_findings
    adds_problem = outcome == 'added'
    incomplete = outcome in ('incomplete', 'incomplete-advisory')
    needs_acknowledgement = outcome != 'unchanged'
    department = h.get('admin', '/api/me')['department']
    h.command('admin', 'department.display', {'rule_catalogue': False,
        'reason': 'Standalone rule review regression'}, department['id'], department['revision'])
    typ = h.configure('assignment_types', {'name': 'Existing staffing shortage', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 20, 'maximum': 20}]}})['record']['id']
    workspace = h.shift(h.workspace('Existing shortage review'), typ=typ, people=[])['workspace']
    title = 'Person restriction review' if adds_problem else 'Unrelated rest review'
    definition = ({'scope': {'person_id': h.seed['people'][0]}, 'rule': {'kind': 'restriction'}}
                  if adds_problem else {'rule': {'kind': 'min_rest', 'hours': 12}})
    if outcome == 'incomplete-advisory':
        title = 'Unrelated workload review'
        definition = {'severity': 'advisory', 'rule': {'kind': 'max_hours', 'hours': 48}}
    assert (definition['rule']['kind'] in NEVER_OVERRIDDEN) is (outcome != 'incomplete-advisory')
    proposal = h.command('coordinator', 'policy.create', {'name': title, 'owner': 'Synthetic department',
        'source': 'New-problems activation browser regression', 'starts_on': '2026-01-01',
        'definition': definition})['record']
    before = h.detail(workspace)['shifts']
    context, page = sign_in(browser, checked_rule_server, 'approver',
        viewport={'width': width, 'height': 900}, color_scheme=color_scheme)
    try:
        navigate(page, 'rules')
        row = page.locator(f'[data-rule-ids~="{proposal["id"]}"]')
        row.get_by_role('button', name='Review', exact=True).click()
        review = page.get_by_role('dialog', name='Review ' + title, exact=True)
        expect(review.get_by_text('Not checked', exact=True)).to_be_visible()
        review.get_by_role('button', name='Check rota', exact=True).click()
        expect(review.get_by_text('Waiting to check', exact=True)).to_be_visible()
        job = h.get('approver', '/api/jobs?workspace_id=' + workspace['id'])['jobs'][0]
        assert job['kind'] == 'feasibility'
        assert process_one(h.app.state.engine)
        result = h.get('approver', '/api/jobs/' + job['id'])['job']['result']
        assert result['pre_existing_conflict']
        assert result['comparison_complete']
        if incomplete:
            assert result['no_new_problems']
            expect(review.get_by_text(no_new_label(result), exact=True)).to_be_visible()
            result = copy.deepcopy(result)
            result['checks']['baseline'].update(status='unknown_time_limit', diagnostics=[])
            result.update(compare_findings(result['checks']['baseline'], result['checks']['proposed']),
                pre_existing_conflict=False, newly_introduced_contradiction=False,
                full_feasibility_established=False, status='needs_attention')
            assert result['new_or_worse_findings'] and not result['comparison_complete']
            assert not result['no_new_problems']
            with transaction(h.app.state.engine, write=True) as db:
                db.get(m.Job, job['id']).result = result
        review.get_by_role('button', name='Refresh checks', exact=True).click()
        acknowledgement = review.get_by_label(
            'Activate even though some affected rotas are not checked or still have problems.', exact=True)
        review.get_by_label('Reason', exact=True).fill('Independent review of new problems')
        activate = review.get_by_role('button', name='Activate', exact=True)
        if adds_problem:
            count = len(result['new_or_worse_findings'])
            assert count > 0 and not result['no_new_problems']
            assert any(f['code'] == 'FEWER_ELIGIBLE_CANDIDATES' for f in result['new_or_worse_findings'])
            expect(review.get_by_text(adds_label(count), exact=True)).to_be_visible()
        elif incomplete:
            expect(review.get_by_text('Comparison unresolved', exact=True)).to_be_visible()
            expect(review.get_by_text(adds_label(len(result['new_or_worse_findings'])),
                                      exact=True)).to_have_count(0)
        if needs_acknowledgement:
            expect(acknowledgement).to_be_visible()
            expect(acknowledgement).not_to_be_checked()
            with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/api/commands')
                                      and r.request.post_data_json['kind'] == 'policy.activate') as refused:
                activate.click()
            assert refused.value.status == 409
            assert refused.value.json()['error']['code'] == 'RULE_CHECK_UNRESOLVED'
            assert refused.value.request.post_data_json['payload']['acknowledge_unresolved'] is False
            refusal_message = 'Not activated: ' + (
                'Safety rules require a complete effect check; uncertainty cannot be acknowledged'
                if outcome == 'incomplete' else
                'Review and acknowledge the unresolved rota findings before activation')
            expect(review.get_by_text(refusal_message, exact=True)).to_be_visible()
            assert next(p for p in h.get('approver', '/api/catalogues')['policies']
                        if p['id'] == proposal['id'])['status'] == 'proposed'
            assert h.detail(workspace)['shifts'] == before
            acknowledgement.check()
        else:
            count = result['unchanged_existing_count']
            assert count > 0 and result['no_new_problems']
            assert result['new_or_worse_findings'] == []
            expect(review.get_by_text(no_new_label(result),
                                      exact=True)).to_be_visible()
            expect(acknowledgement).not_to_be_visible()
        with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/api/commands')
                                  and r.request.post_data_json['kind'] == 'policy.activate') as activation:
            activate.click()
        assert activation.value.request.post_data_json['payload']['acknowledge_unresolved'] is needs_acknowledgement
        if outcome == 'incomplete':
            assert activation.value.status == 409
            assert activation.value.json()['error']['code'] == 'RULE_CHECK_UNRESOLVED'
            expect(review.get_by_text(refusal_message, exact=True)).to_be_visible()
            assert next(p for p in h.get('approver', '/api/catalogues')['policies']
                        if p['id'] == proposal['id'])['status'] == 'proposed'
            assert h.detail(workspace)['shifts'] == before
            return
        assert activation.value.status == 200
        expect(review).to_have_count(0)
        assert next(p for p in h.get('approver', '/api/catalogues')['policies']
                    if p['id'] == proposal['id'])['status'] == 'active'
        assert h.detail(workspace)['shifts'] == before
        page.reload()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'rules')
        expect(row).to_be_visible()
        expect(row.get_by_role('button', name='Review', exact=True)).to_have_count(0)
    finally:
        context.close()
