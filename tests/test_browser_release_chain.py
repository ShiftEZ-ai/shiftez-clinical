"""The release journey on one department, through the real browser and scheduler."""

import re
from collections import Counter
from contextlib import ExitStack

import pytest
from playwright.sync_api import expect

from apps.worker.main import process_one
from packages.contracts.schemas import ROLE_LABELS
from .test_browser import (
    add_filter, auto_setup, browser, condition, decide, live_server, navigate, new_draft, open_rota,
    open_scheduling, pick, save_dialog, sign_in, wait_run_created,
)
from .test_browser_cp_sat import cp_server
from .test_browser_fairness_dashboard import october, section
from .test_browser_operator_governance_actions import command_response
from .test_browser_requests import new_request
from .test_browser_rule_filter_builder import pick as pick_condition

pytestmark = pytest.mark.browser


def test_release_chain_from_new_people_to_audit(browser, cp_server, h):
    clinic = 'Continuity clinic'
    duty = 'Clinical review session'
    rota = 'Release journey rota'
    rule = 'Protected scheduling time'
    staff_name, replacement, protected, available = (
        f"{ROLE_LABELS['staff']} {number:02}(test)" for number in (1, 2, 4, 5)
    )

    with ExitStack() as contexts:
        admin_context, admin = sign_in(browser, cp_server, 'admin')
        contexts.callback(admin_context.close)
        navigate(admin, 'people')
        for person in (protected, available):
            admin.get_by_role('button', name='New person', exact=True).click()
            form = admin.get_by_role('dialog', name='New person', exact=True)
            form.get_by_label('Display name', exact=True).fill(person)
            form.get_by_label('Change reason', exact=True).fill(f'Add {person} for this rota')
            save_dialog(form)
            profile = admin.get_by_role('region', name=person, exact=True)
            expect(profile).to_be_visible()
            profile.get_by_role('button', name='Close panel', exact=True).click()

        credential_context, credentialer = sign_in(browser, cp_server, 'credentialer')
        contexts.callback(credential_context.close)
        navigate(credentialer, 'people')
        for person in (protected, available):
            credentialer.get_by_role('button', name='Authorise eligibility', exact=True).click()
            form = credentialer.get_by_role('dialog', name='Authorise clinics', exact=True)
            form.get_by_label('Person', exact=True).select_option(label=person)
            pick(form, 'Clinics', [clinic])
            form.get_by_label('From', exact=True).fill('2026-10-01')
            save_dialog(form, 'Authorise clinics')
            expect(credentialer.get_by_role('row').filter(has_text=person).get_by_text(clinic, exact=True)).to_be_visible()

        staff_context, staff = sign_in(browser, cp_server, 'staff', viewport={'width': 390, 'height': 844})
        contexts.callback(staff_context.close)
        navigate(staff, 'requests')
        form = new_request(staff, 'Leave')
        form.get_by_label('All day').check()
        form.get_by_label('First day', exact=True).fill('2026-10-05')
        form.get_by_label('Last day', exact=True).fill('2026-10-05')
        form.get_by_label('Type', exact=True).select_option(label='Annual')
        form.get_by_label('Reason', exact=True).fill('Fictional leave for one day')
        with staff.expect_response(command_response('request.create')) as leave:
            save_dialog(form, 'Send request')
        expect(staff.get_by_text('Request submitted, not yet approved.', exact=True)).to_be_visible()

        approver_context, approver = sign_in(browser, cp_server, 'approver')
        contexts.callback(approver_context.close)
        navigate(approver, 'requests?open=' + leave.value.json()['record']['id'])
        request = approver.get_by_role('region', name='Annual', exact=True)
        expect(request.get_by_text('Submitted', exact=True)).to_be_visible(timeout=20000)
        request.get_by_role('button', name='Approve', exact=True).click()
        approval = approver.get_by_role('dialog', name='Approve leave', exact=True)
        save_dialog(approval, 'Approve')
        expect(approver.get_by_text(f'Approved · {staff_name} is told.', exact=True)).to_be_visible()

        form = new_request(staff, 'Wish')
        form.get_by_label('I would like', exact=True).select_option('prefer_on')
        form.get_by_label('From', exact=True).fill('2026-10-06')
        form.get_by_label('To (included)', exact=True).fill('2026-10-06')
        form.get_by_label('Points', exact=True).select_option('3')
        save_dialog(form, 'Add wish')
        expect(staff.get_by_text(re.compile(r'^Wish saved'))).to_be_visible()

        coordinator_context, coordinator = sign_in(browser, cp_server, 'coordinator')
        contexts.callback(coordinator_context.close)
        navigate(coordinator, 'rules')
        coordinator.get_by_role('button', name='Add rule', exact=True).click()
        form = coordinator.get_by_role('dialog', name='Add rule', exact=True)
        form.get_by_label('Rule title', exact=True).fill(rule)
        form.get_by_label('Source or departmental decision', exact=True).fill('Protected time agreed for this rota')
        form.get_by_role('radio', name='Time restriction', exact=True).check()
        condition(form, 'Person', [protected])
        expect(form.get_by_label('Classification', exact=True)).to_have_value('mandatory')
        save_dialog(form, 'Propose rule')
        expect(coordinator.get_by_text('Rule proposed. It applies once an approver activates it.', exact=True)).to_be_visible()

        navigate(approver, 'rules')
        approver.get_by_role('button', name=re.compile(r'^Review(?: 1)?$')).click()
        review = approver.get_by_role('dialog', name='Review ' + rule, exact=True)
        review.get_by_label('Reason', exact=True).fill('Independent restriction review')
        review.get_by_role('button', name='Activate', exact=True).click()
        expect(review).to_have_count(0)
        expect(approver.get_by_text('Rule activated.', exact=True)).to_be_visible()

        navigate(coordinator, 'planner')
        new_draft(coordinator)
        form = coordinator.get_by_role('dialog', name='New rota', exact=True)
        form.get_by_label('Rota name', exact=True).fill(rota)
        form.get_by_label('First day', exact=True).fill('2026-10-04')
        form.get_by_label('Last day', exact=True).fill('2026-10-10')
        with coordinator.expect_response(command_response('workspace.create')) as created:
            save_dialog(form, 'Create draft')
        workspace_id = created.value.json()['workspace']['id']
        expect(coordinator.get_by_role('button', name='Switch draft · ' + rota, exact=True)).to_be_visible()

        for day in ('2026-10-06', '2026-10-05'):
            coordinator.get_by_role('main').get_by_role('button', name='Add session', exact=True).click()
            form = coordinator.get_by_role('region', name='Add session', exact=True)
            form.get_by_label('Duty type', exact=True).select_option(label=duty)
            form.get_by_label('Location', exact=True).select_option(label='North consulting room')
            form.get_by_label(clinic, exact=True).check()
            form.get_by_label('Session start', exact=True).fill(day + 'T08:00')
            form.get_by_label('Session end', exact=True).fill(day + 'T12:00')
            if day == '2026-10-06':
                form.get_by_role('button', name='Add participant', exact=True).click()
                form.get_by_label('Participant', exact=True).select_option(label=staff_name)
            save_dialog(form, 'Save draft session')
        manual = coordinator.get_by_role('button', name=re.compile(rf'^{re.escape(staff_name)}, {duty}, .+, 6 Oct 2026 08:00'))
        expect(manual).to_be_visible()
        expect(coordinator.get_by_role('button', name=re.compile(rf'^1 place open on {duty}, .+, 5 Oct 2026 08:00'))).to_be_visible()

        open_scheduling(coordinator)
        panel = coordinator.locator('#auto-panel')
        auto_setup(panel)
        expect(panel.get_by_label('Keep existing assignments', exact=True)).to_be_checked()
        # These three candidates leave exactly one possible assignment after the rule and leave are applied.
        panel.get_by_role('button', name=re.compile('^People to schedule:')).click()
        group = coordinator.get_by_role('dialog', name='People to schedule', exact=True)
        add_filter(group)
        pick_condition(coordinator, group.locator('.cond-line').first, 'Person', [staff_name, protected, available])
        group.get_by_role('button', name='Apply', exact=True).click()
        panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
        expect(panel.get_by_role('progressbar')).to_be_visible()
        wait_run_created(coordinator)
        assert process_one(h.app.state.engine, kinds=('generation',))

        review = panel.get_by_role('region', name='Review')
        expect(review.get_by_role('status').first).to_contain_text('Ready to submit once applied.', timeout=15000)
        panel.get_by_role('button', name='Review latest result', exact=True).click()
        results = coordinator.get_by_role('dialog', name='Auto-schedule results', exact=True)
        expect(results.get_by_text(re.compile(r'^Solver status: OPTIMAL ·'))).to_be_visible()
        expect(results.get_by_role('cell', name=available, exact=True)).to_be_visible()
        results.get_by_text('Run details', exact=True).click()
        results.get_by_role('button', name='View candidates considered', exact=True).click()
        candidates = coordinator.get_by_role('dialog', name='Candidates considered', exact=True)
        monday = candidates.get_by_role('row').filter(has_text='5 Oct 2026 08:00–12:00')
        expect(monday.filter(has=coordinator.get_by_role('cell', name=staff_name, exact=True))).to_contain_text('Recorded unavailability')
        expect(monday.filter(has=coordinator.get_by_role('cell', name=protected, exact=True))).to_contain_text(
            'Active scheduling restriction · ' + rule)
        candidates.get_by_role('button', name='Close dialog', exact=True).click()
        results.get_by_role('button', name='Apply the change', exact=True).click()
        expect(results).not_to_be_visible()
        generated = coordinator.get_by_role('button', name=re.compile(rf'^{re.escape(available)}, {duty}, .+, 5 Oct 2026 08:00'))
        expect(generated).to_be_visible()
        expect(manual).to_be_visible()

        decide(coordinator, 'Submit for review')
        expect(coordinator.get_by_text('Waiting for approval', exact=True).first).to_be_visible()
        open_rota(approver, workspace_id)
        decide(approver, 'Approve rota')
        expect(approver.get_by_text('Approved', exact=True).first).to_be_visible()
        coordinator.reload()
        expect(coordinator.get_by_role('button', name='Publish…', exact=True)).to_be_visible(timeout=15000)
        with coordinator.expect_response(command_response('workspace.publish')) as published:
            decide(coordinator, 'Publish rota')
        expect(coordinator.get_by_text('Published · staff are told.', exact=True)).to_be_visible()

        navigate(staff, 'schedule/week/2026-10-04')
        schedule = staff.get_by_role('main')
        mine = schedule.get_by_role('region', name='6 Oct 2026', exact=True).get_by_role('button', name=re.compile(duty))
        expect(mine).to_be_visible(timeout=16000)
        expect(mine).to_have_attribute('aria-current', 'true')
        expect(schedule.get_by_role('button', name=re.compile(duty))).to_have_count(1)

        navigate(coordinator, 'insights/fairness')
        october(coordinator)
        distribution = section(coordinator, 'distribution')
        before = re.compile(rf'^{re.escape(available)}: 1 duties\b')
        expect(distribution.get_by_role('button', name=before)).to_be_visible()

        publication_id = published.value.json()['publication_id']
        navigate(coordinator, f'schedule/week/2026-10-04?source={publication_id}')
        generated.click()
        coordinator.get_by_role('button', name=f'Change {available} on this session', exact=True).click()
        coordinator.get_by_role('menuitem', name='Replace…', exact=True).click()
        change = coordinator.get_by_role('region', name=f'Replace {available}', exact=True)
        change.get_by_role('radio', name='Sick', exact=True).click()
        change.get_by_role('button', name=f'Assign {replacement}', exact=True).click()
        expect(change.get_by_text('Passes every check', exact=True)).to_be_visible()
        expect(coordinator.get_by_role('button', name='1 change · not sent', exact=True)).to_be_visible()
        with coordinator.expect_response(command_response('publication.changes.submit')) as sent:
            change.get_by_role('button', name='Send for approval', exact=True).click()
        expect(coordinator.get_by_role('button', name='1 change waiting', exact=True)).to_be_visible()
        amendment_id = sent.value.json()['workspace']['id']

        open_rota(approver, amendment_id)
        expect(approver.get_by_role('region', name='What changes').first).to_contain_text(f'{available} → {replacement}')
        decide(approver, 'Approve changes')
        expect(approver.get_by_text('Amendment · Approved', exact=True)).to_be_visible()
        open_rota(coordinator, amendment_id)
        decide(coordinator, 'Publish rota')
        expect(coordinator.get_by_text('Published · staff are told.', exact=True)).to_be_visible()

        navigate(coordinator, 'insights/fairness')
        october(coordinator)
        distribution = section(coordinator, 'distribution')
        expect(distribution.get_by_role('button', name=re.compile(rf'^{re.escape(replacement)}: 1 duties\b'))).to_be_visible()
        expect(distribution.get_by_role('button', name=re.compile(rf'^{re.escape(available)}: 0 duties\b'))).to_be_visible()

        auditor_context, auditor = sign_in(browser, cp_server, 'auditor')
        contexts.callback(auditor_context.close)
        navigate(auditor, 'system/audit')
        references = auditor.get_by_role('button', name=re.compile(r'^Copy reference for \d\d:\d\d(–\d\d:\d\d)? change$'))
        expect(references.first).to_be_visible()
        expected = {
            'admin': ['catalogue.create', 'catalogue.create'],
            'credentialer': ['bulk.apply'],
            'staff': ['request.create', 'wish.create'],
            'approver': ['request.decide', 'policy.activate', 'workspace.approve', 'workspace.approve'],
            'coordinator': ['policy.create', 'workspace.create', 'shift.create', 'job.create', 'generation.apply',
                            'workspace.submit', 'workspace.publish', 'publication.changes.prepare', 'publication.change',
                            'publication.changes.submit', 'workspace.publish'],
        }
        actual = Counter(
            ' · '.join((account, action))
            for account, _, action in (reference.split(' · ') for reference in
                                       references.evaluate_all("buttons => buttons.map(button => button.getAttribute('data-tip'))"))
        )
        required = Counter(f"Account {h.seed['identities'][role]} · {action}"
                           for role, actions in expected.items() for action in actions)
        assert not (required - actual), required - actual
        expect(auditor.get_by_text(re.compile(r'added 2 sessions\b'))).to_be_visible()
