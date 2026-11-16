"""The v0.8 journey end to end, in a real browser across roles: scoring settings → rules with conditions on every
dimension and a Balance goal → staff wishes → auto-schedule → score, rules in context and Why? → a rota-only change and
Undo → submit, approve, publish → fairness → an amendment compared with the published rota. Every step checks that the
settings from the first step are the ones the solver, the score and the explanations used (one source)."""
from packages.domain.time import now as clock_now
import re
from datetime import date, timedelta

import pytest
from playwright.sync_api import expect
from apps.worker.main import process_one
from .test_browser import wait_run_created, browser, live_server, sign_in, navigate, save_dialog, condition, open_scheduling, decide, open_config, planner_tool, open_rota, press_escape  # noqa: F401
from .test_browser_cp_sat import _joins, _check_after_apply, cp_server  # noqa: F401
from .rules_support import rule_action, rule_row

pytestmark = pytest.mark.browser
PHONE = {'width': 390, 'height': 844}


def rota_month():
    """The month after next: always in the future, so wishes can be added and nothing is locked."""
    t = clock_now().date()
    first = date(t.year + (t.month + 1) // 12, (t.month + 1) % 12 + 1, 1)
    return first, date(first.year + first.month // 12, first.month % 12 + 1, 1)


def people_records(h, first, last):
    """Alex in a programme (and its cohort), Jordan on a rotation, Taylor tagged Night: every person condition has a match."""
    a, b, c = h.seed['people']
    p = h.configure('programmes', {'name': 'FM residency', 'starts_on': '2020-01-01'})['record']
    h.configure('programme_memberships', {'person_id': a, 'programme_id': p['id'], 'starts_on': '2020-01-01', 'ends_on': '2030-01-01'}, role='coordinator')
    # A rotation needs programme membership for its whole span (ROTATION_MEMBERSHIP).
    h.configure('programme_memberships', {'person_id': b, 'programme_id': p['id'], 'starts_on': '2020-01-01', 'ends_on': '2030-01-01'}, role='coordinator')
    cohort = h.configure('cohorts', {'name': 'Year 2 residents', 'programme_id': p['id'], 'starts_on': '2020-01-01', 'ends_on': '2030-01-01'})['record']
    kind = h.configure('rotation_types', {'name': 'Urgent care block', 'starts_on': '2020-01-01'}, role='coordinator')['record']
    h.configure('rotations', {'person_id': b, 'cohort_id': cohort['id'], 'rotation_type_id': kind['id'],
                              'starts_on': first.isoformat(), 'ends_on': last.isoformat()}, role='coordinator')
    taylor = next(x for x in h.get('admin', '/api/catalogues')['people'] if x['id'] == c)
    h.configure('people', {'display_name': taylor['display_name'], 'tags': ['Night']}, target=c, revision=taylor['revision'])


def month_rota(h, first, last):
    """Eight open weekday sessions over two weeks, alternating clinics and rooms."""
    w = h.workspace('Journey month', first.isoformat(), last.isoformat())
    for d in (2, 3, 4, 5, 9, 10, 11, 12):
        day = first.replace(day=d).isoformat()
        w = h.shift(w, day + 'T08:00:00+03:00', day + 'T12:00:00+03:00', people=[], service=d % 2, location=d % 2)['workspace']
    return w


def expand(row):
    """Open a rule row (it may already be open: the planner remembers the last rule opened)."""
    head = row.locator('.rk-row-head')
    expect(head).to_be_visible()
    if head.get_attribute('aria-expanded') != 'true':
        head.click()
    expect(row.locator('.rk-row-head')).to_have_attribute('aria-expanded', 'true')


def propose(page, kind, title, fill):
    page.get_by_role('button', name='Add rule', exact=True).click()
    form = page.get_by_role('dialog')
    form.get_by_role('radio', name=kind, exact=True).check()
    form.get_by_label('Rule title', exact=True).fill(title)
    form.get_by_label('Source or departmental decision', exact=True).fill('Journey browser test')
    fill(form)
    save_dialog(form, 'Propose rule')


def test_v08_journey_across_roles(browser, cp_server, h):
    first, last = rota_month()
    alex = h.seed['people'][0]
    people_records(h, first, last)
    errors, contexts = [], []

    def open_as(role, viewport=None):
        context, page = sign_in(browser, cp_server, role, viewport)
        page.set_default_timeout(60000)
        contexts.append(context)
        page.on('pageerror', lambda e: errors.append(f'{role}: {e}'))
        return page

    try:
        # 1. Admin: Configuration › Auto-schedule & scoring.
        page = open_as('admin')
        open_config(page, 'Auto-schedule & scoring')
        pane = page.locator('.config-pane')
        pane.get_by_role('tab', name='Staff preferences', exact=True).click()
        pane.get_by_label('Wish points per person (0 = wishes off)', exact=True).fill('6')
        pane.get_by_role('tab', name='How it decides', exact=True).click()
        pane.get_by_label('Coverage weight', exact=True).fill('50')
        pane.get_by_label('Fairness weight', exact=True).fill('30')
        pane.get_by_role('tab', name='Advanced', exact=True).click()  # advisory strengths
        pane.get_by_label('High strength (×)', exact=True).fill('12')
        expect(pane.get_by_label('Fair team-lead turns', exact=True)).to_have_count(0)  # a visible Fair share rule now
        pane.get_by_label('Worst-off fairness emphasis (0–1)', exact=True).fill('0.6')
        pane.get_by_role('tab', name='How it decides', exact=True).click()
        expect(pane.get_by_label('Coverage weight', exact=True)).to_have_accessible_description('40%')  # 50 of 125
        pane.get_by_role('button', name='Save defaults', exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Reason', exact=True).fill('Journey: weigh coverage and fairness')
        dialog.get_by_role('button', name='Save defaults', exact=True).click()
        expect(page.get_by_role('button', name='Undo', exact=True)).to_be_visible()
        scoring = h.get('admin', '/api/me')['department']['generation_defaults']['scoring']
        assert scoring['weights'] == {'coverage': 50, 'rules': 25, 'fairness': 30, 'preferences': 15, 'stability': 5}
        assert scoring['strengths']['high'] == 12 and scoring['wish_budget_points'] == 6 and scoring['fairness_worst_off_emphasis'] == 0.6

        # 2. Coordinator: one rule whose conditions use every dimension, and a Balance goal.
        page = open_as('coordinator')
        navigate(page, 'rules')

        def cap(form):
            form.get_by_label('Maximum duties', exact=True).fill('3')
            for field, names in (('Rank', ['Demo practitioners']), ('Skill', ['Demo independent practice']), ('Programme', ['FM residency']),
                                 ('Rotation type', ['Urgent care block']), ('Cohort', ['Year 2 residents']), ('Tag', ['#Night']),
                                 ('Person', ['Alex Morgan']), ('Clinic', ['Continuity clinic', 'Review clinic']),
                                 ('Location', ['North consulting room', 'South consulting room']), ('Duty type', ['Clinical review session'])):
                condition(form, field, names)
            form.get_by_label('Joiner', exact=True).first.select_option(label='Or')  # the level's joiner: any one of them matches
        propose(page, 'Maximum number of duties', 'Journey weekly duty cap', cap)

        def goal(form):
            builder = form.locator('[data-goal-builder]')
            builder.get_by_label('Count', exact=True).select_option('duties')
            builder.get_by_label('Over', exact=True).select_option('month')
            builder.get_by_label('Aiming for', exact=True).select_option('equal_share')
            builder.get_by_label('Strength', exact=True).select_option('high')
        propose(page, 'Balance goal', 'Journey fair duties', goal)
        rules = {p['name']: p for p in h.get('coordinator', '/api/catalogues')['policies']}
        lines = rules['Journey weekly duty cap']['definition']['scope']['conditions']
        assert lines['match'] == 'any' and {c['field'] for c in lines['conditions']} == {
            'category_ids', 'capability_ids', 'programme_ids', 'rotation_type_ids', 'cohort_ids', 'tags', 'person_ids',
            'service_ids', 'location_ids', 'assignment_type_ids'}
        expect(page.locator('.rule-item').filter(has_text='Journey weekly duty cap')).to_contain_text('Rank is Demo practitioners or Skill is')
        approver = open_as('approver')
        navigate(approver, 'rules')
        for left in (2, 1):
            # Wait for the list to show what is left: a row from before the last activation has a stale revision.
            expect(approver.locator('.rule-item').get_by_role('button', name='Review', exact=True)).to_have_count(left)
            approver.locator('.rule-item').get_by_role('button', name='Review', exact=True).first.click()
            form = approver.get_by_role('dialog')
            form.get_by_label('Reason', exact=True).fill('Journey review')
            expect(form.get_by_role('status').filter(has_text='No rota being planned overlaps')).to_be_visible()  # loaded: nothing to check yet
            save_dialog(form, 'Activate')
        expect(approver.locator('.rule-item').get_by_role('button', name='Review', exact=True)).to_have_count(0)

        # 3. Staff (on a phone): two wishes use the whole budget set in step 1.
        staff = open_as('staff', PHONE)
        navigate(staff,'requests')
        staff.get_by_role('button', name='Wish calendar', exact=True).click()
        panel = staff.get_by_role('region', name='Wishes')
        this_month = staff.locator('.wish-month').inner_text()
        expect(panel).to_contain_text(f'No wishes in {this_month}')  # the list is the month's; wishes elsewhere are not "none yet"
        target = first.strftime('%B %Y')
        while staff.locator('.wish-month').inner_text() != target:
            before = staff.locator('.wish-month').inner_text()
            panel.get_by_role('button', name='Next month').click()
            expect(staff.locator('.wish-month')).not_to_have_text(before)
        expect(panel.get_by_text('0 of 6 points used', exact=False)).to_be_visible()
        wished = (2, 10)
        for day in wished:
            panel.locator('.wish-day:not(.other-month)').nth(day - 1).click()
            dialog = staff.get_by_role('dialog', name='Add a wish')
            dialog.get_by_label('Points', exact=True).select_option('3')
            dialog.get_by_role('button', name='Add wish', exact=True).click()
            expect(dialog).to_have_count(0)
        expect(panel.get_by_text('6 of 6 points used', exact=False)).to_be_visible()
        assert staff.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')

        # 4. Coordinator: auto-schedule the month with the settings from step 1.
        w = month_rota(h, first, last)
        navigate(page, 'planner')
        open_rota(page, w['id'])
        open_scheduling(page)
        page.get_by_role('button', name='Run auto-schedule', exact=True).click()
        expect(page.locator('#auto-panel').get_by_role('progressbar')).to_be_visible()
        wait_run_created(page)
        job = h.get('coordinator', '/api/jobs')['jobs'][0]
        assert job['effective_options']['scoring'] == scoring  # the solver got the department's settings
        process_one(h.app.state.engine)
        page.get_by_role('button', name='Review latest result', exact=True).click()
        result = page.get_by_role('dialog', name='Auto-schedule results')
        # The run's outcome (the solver receipt, also a status, says above it why the search stopped: 73e2eddb2).
        outcome = result.locator('.auto-outcome')
        expect(outcome).to_contain_text('must-fix cause cleared')
        expect(outcome).to_contain_text('Ready to submit once applied')
        joined = _joins(page)
        result.get_by_role('button', name=re.compile(r'^Apply (the change|all \d+ changes)$')).click()
        expect(result).to_have_count(0)
        _check_after_apply(page, h, w['id'], joined)  # the worker finishes the complete check Apply queued (maintenance)
        detail = h.detail(w)
        score = detail['workspace']['validation']['score']
        worked = {s['start'][:10] for s in detail['shifts'] for p in s['participants'] if p['person_id'] == alex}
        assert not worked & {first.replace(day=d).isoformat() for d in wished}  # wishes granted
        assert score['settings'] == scoring and {c: v['weight'] for c, v in score['components'].items()} == scoring['weights']
        scored = [c for c, v in score['components'].items() if v['value'] is not None]
        # Weights from step 1 (components are stored to 4 decimals and the score to 1: within a tenth).
        assert abs(100 - sum(100 * v['weight'] * (1 - v['value']) for c, v in score['components'].items() if c in scored)
                   / sum(score['components'][c]['weight'] for c in scored) - score['score']) < 0.1
        assert round(sum(x['points'] for x in score['why']), 1) == round(100 - score['score'], 1)

        # D9/A7: the shared health pill shows the canonical score once no cause must be fixed.
        pill = page.get_by_role('button', name=f"Ready to submit · score {score['score']:g}", exact=True)
        expect(pill).to_be_visible(timeout=20000)
        pill.click()
        health = page.get_by_role('dialog', name='Rota health')
        expect(health.locator('.score-line')).to_contain_text(f"{score['score']:g}")
        press_escape(page)
        planner_tool(page, 'Rules in this rota')
        rules_panel = page.get_by_role('dialog', name='Rules in this rota')
        duty_cap = rule_row(rules_panel, 'Journey weekly duty cap')  # opens "N more are met" when it is folded there
        expect(rules_panel.locator('.rk-row')).to_have_count(3)
        expand(duty_cap)
        expect(page.locator('.rk-spot.locked')).to_contain_text(re.compile(r'applies to 3 people · 8 sessions'))
        rules_panel.get_by_role('button', name='Close rules panel (Esc)').click()
        page.locator('.pchip[data-rk]').first.click()
        assignment = page.locator('.wk-panel')
        mine = rule_row(assignment.get_by_role('list', name='Rules applied to this assignment'), 'Journey weekly duty cap')
        expect(mine).to_contain_text('3 duties')
        expand(mine)
        expect(mine).to_contain_text('matches the rule’s conditions')
        assignment.get_by_role('button', name='Close panel').click()

        # 6. The rule changed for this rota only (the rota breaks it and cannot be published), then Undo.
        planner_tool(page, 'Rules in this rota')
        rules_panel = page.get_by_role('dialog', name='Rules in this rota')
        duty_cap = rule_row(rules_panel, 'Journey weekly duty cap')
        rule_action(duty_cap, 'Adjust for this rota')
        rules_panel.get_by_label('New number for “Journey weekly duty cap” in this rota').fill('1')
        rules_panel.get_by_label('Justification', exact=True).fill('Agreed with the department for this rota')
        rules_panel.get_by_role('button', name='Use for this rota').click()
        toast = page.locator('.toast').filter(has_text='changed for this rota')
        expect(toast).to_be_visible()
        expect(page.locator('.app-footer .fix-pills').get_by_role('button', name=re.compile(r'^\d+ must fix'))).to_be_visible(timeout=20000)
        expect(page.get_by_role('button', name=re.compile('^Ready to submit · score'))).to_have_count(0)
        expect(page.locator('.rk-mark[aria-label*="Changed for this rota"]').first).to_be_attached()
        toast.get_by_role('button', name='Undo').click()
        expect(page.locator('.toast').filter(has_text='Undone')).to_be_visible()
        expect(page.get_by_role('button', name=f"Ready to submit · score {score['score']:g}", exact=True)).to_be_visible(timeout=20000)
        assert h.detail(w)['workspace']['rule_adjustments'] == []

        # 7. Submit, approve, publish.
        page.get_by_role('dialog', name='Rules in this rota').get_by_role('button', name='Close rules panel (Esc)').click()
        decide(page, 'Submit for review')
        navigate(approver, 'planner')
        open_rota(approver, w['id'])
        decide(approver, 'Approve rota')
        page.reload()
        expect(page.get_by_role('button', name='Publish…', exact=True)).to_be_visible(timeout=15000)
        decide(page, 'Publish rota')

        # 8. Fairness dashboard for the month, and a drill-down to Alex's sessions.
        navigate(page,'insights/fairness')
        page.get_by_label('Period', exact=True).select_option('custom')
        page.get_by_label('From', exact=True).fill(first.isoformat())
        page.get_by_label('Until (included)', exact=True).fill((last - timedelta(days=1)).isoformat())
        page.get_by_label('Until (included)', exact=True).dispatch_event('change')
        expect(page.locator('.fx-page[data-ready=true]')).to_have_count(1, timeout=60000)
        expect(page.locator('[data-section=score]')).to_contain_text(f"{score['score']:g}")
        goals = page.locator('[data-section=goals] .fx-goal')
        expect(goals).to_have_count(2)  # authored duties goal plus seeded workload goal
        expect(goals.filter(has_text='Journey fair duties')).to_contain_text('Journey fair duties')
        page.locator('[data-section=distribution]').get_by_role('button', name=re.compile('^Alex Morgan: ')).click()
        drill = page.get_by_role('region', name='Duties · Alex Morgan')
        expect(drill.locator('tbody tr')).to_have_count(len(worked))
        drill.get_by_role('button', name='Close panel').click()
        # Every session has one person: nobody worked together, and the heatmap says so instead of a grid of zeros.
        expect(page.locator('[data-section=exposure]')).to_contain_text('No sessions together')

        # 9. A second draft for the same period (an amendment), one change, then Compare with the published rota.
        navigate(page, f'schedule/week/{first.isoformat()}?edit=none')
        planner_tool(page, 'Change a published rota…')
        save_dialog(page.get_by_role('dialog'), 'Create draft')
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
        page.locator('.pchip[data-rk]').first.click()
        page.locator('.wk-panel').get_by_role('button', name='Replace', exact=True).click()
        page.locator('.wk-panel .wk-cand button').first.click()
        expect(page.locator('.wk-panel').get_by_role('status').filter(has_text=' replaces ')).to_be_visible()
        planner_tool(page, 'Compare side by side')
        dialog = page.locator('#schedule-view')  # Side by side: the comparison replaces the view, inline
        dialog.get_by_label('Compare with', exact=True).select_option(w['id'])
        # A partial amendment has no whole-rota score and therefore no pairwise score delta.
        expect(dialog.locator('.fx-compare')).to_have_count(0)
        expect(dialog.locator('.score-change')).to_contain_text(f"Schedule score {score['score']:g}")
        expect(dialog.locator('.score-change')).not_to_contain_text('→')
        expect(dialog.get_by_role('heading', name='Sessions', exact=True)).to_be_visible()
        # The published rota's score remains final, rather than becoming stale with its amendment.
        expect(dialog.get_by_text('A score is from an older check', exact=False)).to_have_count(0)
        assert not errors, errors
    finally:
        for context in contexts:
            context.close()
