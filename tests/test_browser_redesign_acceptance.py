"""Assigned ordinary-user redesign: real saves retain the full canonical session behind a person display."""
import json
import re

import pytest
from playwright.sync_api import expect

from .test_browser import (browser, live_server, sign_in, navigate, close_popover, planner_filter,
                           filter_pick, toolbar, save_dialog, view_settings, show_names)
from .test_browser_combined_acceptance import options, reference, save_view
from .test_browser_schedule_team_pills import fixture, member, NAMES
from .test_browser_rule_catalogue import flag, weekly
from .test_browser_table_columns import drag
from .test_browser_a11y import OBSERVERS
from .browser_acceptance_support import shot

from .test_browser import DRAFT_CHIP, draft_chip  # noqa: F401
pytestmark = pytest.mark.browser


def canonical(h, w, session):
    return next(s for s in h.detail(w)['shifts'] if s['id'] == session['id'])['participants']


def encoded(parts):
    return {p['person_id']: json.dumps(p, sort_keys=True) for p in parts}


def show(page, mode, source='reference'):
    bar = page.locator('.planner-toolbar')
    if source == 'reference':
        reference(bar, 'People', NAMES[1])
        panel = options(bar)
        panel.get_by_role('menu', name='Show', exact=True).get_by_role(
            'menuitemradio', name={'person': 'Matched person', 'team': 'Recorded supervision team'}[mode], exact=True).click()
    else:
        filter_pick(planner_filter(page), 'Person', NAMES[1])
        close_popover(page)
        panel = planner_filter(page)
        panel.get_by_role('radiogroup', name='Show', exact=True).get_by_role(
            'radio', name={'person': 'Matched person', 'team': 'Recorded supervision team'}[mode], exact=True).click()
    close_popover(page)


@pytest.mark.parametrize('source', ['reference', 'filter'])
@pytest.mark.parametrize('mode', ['person', 'team'])
def test_person_display_recovers_the_complete_roster_in_every_view(browser, live_server, h, mode, source):
    w, session, people, _ = fixture(h, absent=True)
    before = encoded(canonical(h, w, session))
    context, page = sign_in(browser, live_server, 'coordinator')
    findings = []
    try:
        for view in ('week', 'month', 'day', 'list'):
            navigate(page, f"schedule/{view}/2026-10-04?edit={w['id']}")
            show_names(page, 'full')
            show(page, mode, source)
            wanted = people[1:2] if mode == 'person' else people[:3]
            grid = page.locator('#schedule-view')
            if view in ('week', 'month'):
                for p in people:
                    expect(member(grid, session['id'], p)).to_have_count(int(p in wanted))
                others = grid.locator(f'button.others[data-shift="{session["id"]}"]')
                expect(others).to_contain_text('+' + str(4 - len(wanted)))
                if mode == 'person':
                    expect(others).to_have_class(re.compile(r'\bissue\b'))
                    expect(others).to_have_attribute('aria-label', re.compile('breaks a rule'))
                others.click()
            elif view == 'day':
                bar = grid.locator('button.sd-bar:not(.key)')
                # What the bar draws: its line of names and its teams (team_view.ts, short names with the full name
                # as each row's tip), not its accessible label.
                expect(bar).to_be_visible()
                shown = bar.evaluate("b=>b.textContent+' '+[...b.querySelectorAll('[data-tip]')].map(n=>n.dataset.tip).join(' ')")
                for p, name in zip(people, NAMES):
                    if p in wanted:
                        assert name in shown, (name, shown)
                    elif p != people[0]:
                        assert name not in shown, (name, shown)
                # A hidden supervisor may still be named in a truthful relationship. They ARE assigned here;
                # displaying only their trainee must never invent absence from the canonical session.
                assert 'not on this session' not in shown.lower(), shown
                bar.click()
            else:
                cell = grid.locator('td[data-col="people"]')
                # Every drawn person is the one person pill (person_pill.ts), alone or in a one-line team (team_view.ts).
                expect(cell.locator('.pill-item:not(.pill-over) .pchip:not(.others):not(.more)')).to_have_count(len(wanted))
                if mode == 'team':
                    cell.get_by_role('button', name='1 more person: show all 4', exact=True).click()
                else:
                    cell.locator('.others').click()
            # Every +N must recover full names; a projected "All people" popover cannot substitute for the roster.
            opened = page.locator('.wk-panel, .pill-pop').last
            expect(opened).to_be_visible()
            missing = [name for name in NAMES if name not in opened.inner_text()]
            if missing:
                findings.append({'view': view, 'mode': mode, 'source': source, 'missing': missing,
                                 'actual': opened.inner_text()})
            shot(page, f'redesign-roster-{view}-{mode}-{source}', full_page=False)
            assert encoded(canonical(h, w, session)) == before, 'Display/open changed the canonical assignments'
            page.keyboard.press('Escape')
        assert not findings, findings
    finally:
        context.close()


@pytest.mark.parametrize('mode', ['person', 'team'])
def test_visible_person_remove_edit_add_and_undo_preserve_hidden_assignment_bytes(browser, live_server, h, mode):
    w, session, people, _ = fixture(h, lead=True)
    before = encoded(canonical(h, w, session))
    target = people[1]
    hidden = set(people) - ({target} if mode == 'person' else set(people[:3]))
    context, page = sign_in(browser, live_server, 'coordinator')
    differences = []
    try:
        for action in ('remove', 'edit', 'add'):
            navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
            show(page, mode)
            grid = page.locator('#schedule-view')
            if action == 'remove':
                member(grid, session['id'], target).click(button='right')
                with page.expect_response(lambda r: r.url.endswith('/api/commands') and
                                          r.request.post_data_json.get('kind') == 'shift.update') as saved:
                    page.get_by_role('menuitem', name='Remove from this session', exact=True).click()
                assert saved.value.ok, saved.value.text()
            else:
                member(grid, session['id'], target).click()
                page.locator('.wk-panel').get_by_role('button', name='Whole session', exact=True).click()
                page.locator('.wk-panel').get_by_role('button', name='Edit session', exact=True).click()
                form = page.get_by_role('region', name='Edit session and participants', exact=True)
                expect(form.get_by_label('Participant', exact=True)).to_have_count(4)
                if action == 'edit':
                    for row in form.locator('.nested-card').filter(has=page.get_by_label('Participant', exact=True)).all():
                        if row.get_by_label('Participant', exact=True).input_value() == target:
                            row.get_by_role('checkbox', name=re.compile('^Keep assigned')).check()
                            break
                    else:
                        raise AssertionError('Visible participant absent from the ordinary edit form')
                else:
                    form.get_by_role('button', name='Add participant', exact=True).click()
                    form.get_by_label('Participant', exact=True).last.select_option(h.seed['people'][2])
                save_dialog(form, 'Save draft session')
            after = encoded(canonical(h, w, session))
            expected_people = set(people) - {target} if action == 'remove' else set(people) | ({h.seed['people'][2]} if action == 'add' else set())
            assert set(after) == expected_people, {'action': action, 'expected': expected_people, 'actual': set(after)}
            for p in hidden:
                if after[p] != before[p]:
                    old, new = json.loads(before[p]), json.loads(after[p])
                    differences.append({'action': action, 'person': p,
                                        'changed_fields': [k for k in old if old[k] != new.get(k)]})
            page.evaluate("() => document.querySelectorAll('.toast').forEach(t => t.dataset.earlier = '1')")
            with page.expect_response(lambda r: r.url.endswith('/api/commands') and
                                      r.request.post_data_json.get('kind') == 'workspace.undo') as undo:
                page.locator('.toast').filter(has=page.get_by_role('button', name='Undo', exact=True)).last.get_by_role('button', name='Undo', exact=True).click()
            assert undo.value.ok, undo.value.text()
            # This Undo's own toast: an earlier action's "Undone" (it offers Redo) may still be on screen.
            expect(page.locator('.toast:not([data-earlier])').filter(has_text='Undone')).to_be_visible()
            assert encoded(canonical(h, w, session)) == before, 'Undo failed to restore every original assignment byte'
        # Collect every mutation's identity failure in one result; no product command is mocked.
        assert not differences, differences
    finally:
        context.close()


@pytest.mark.parametrize('kind', ['or', 'negative', 'text'])
def test_unsupported_person_predicates_keep_the_whole_session(browser, live_server, h, kind):
    w, session, people, _ = fixture(h)
    predicate = {'field': 'person_ids', 'op': 'none' if kind == 'negative' else 'any', 'values': [people[1]]}
    definition = {'planner': {'mode': 'week', 'person_display': 'person'}}
    if kind == 'text':
        definition['planner']['search'] = NAMES[1]
    else:
        definition['conditions'] = {'match': 'any' if kind == 'or' else 'all', 'conditions':
                                    [predicate, {'field': 'person_ids', 'values': [people[3]]}] if kind == 'or' else [predicate]}
    saved = h.command('coordinator', 'table_view.create', {'table_key': 'planner', 'name': 'Unsupported person predicate', 'definition': definition})['view']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        toolbar(page.locator('.planner-toolbar'), 'Saved views').get_by_role('menuitemradio', name=saved['name'], exact=True).click()
        if kind == 'negative':
            # A negative Person filter legitimately removes this session; it cannot offer a person projection.
            expect(page.locator(f'#schedule-view {DRAFT_CHIP}')).to_have_count(0)
        else:
            for p in people:
                expect(member(page.locator('#schedule-view'), session['id'], p)).to_have_count(1)
        panel = options(page.locator('.planner-toolbar'))
        expect(panel.get_by_role('menu', name='Show', exact=True)).to_have_count(0)
        assert len(canonical(h, w, session)) == 4
    finally:
        context.close()


def test_rules_real_columns_and_schedule_widths_survive_save_reload_and_undo(browser, live_server, h):
    flag(h)
    weekly(h)
    h.command('coordinator', 'table_view.create', {'table_key': 'rule-tree', 'name': 'Legacy rule columns',
                                                    'definition': {'wrap': ['where'], 'hidden': ['days'],
                                                                   'order': ['where'], 'widths': {'where': 240}}})
    w = h.shift(h.workspace('Fictional widths'))['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        page.locator('#main button.dt-saved').click()
        page.get_by_role('menuitemradio', name='Legacy rule columns', exact=True).click()
        expect(page.locator('#main .rt-scroll')).to_be_visible()
        expect(page.locator('#main .rt-scroll thead')).to_have_count(0)
        expect(page.locator('#main [role=alert]')).to_have_count(0)
        page.reload()
        expect(page.locator('#main .rt-scroll')).to_be_visible()
        expect(page.locator('#main [role=alert]')).to_have_count(0)
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        bar = page.locator('.planner-toolbar')
        save_view(page, bar, 'Fictional schedule widths')
        day = page.get_by_role('separator', name='Resize Day columns', exact=True)
        day.focus(); day.press('ArrowRight')
        expect(bar.locator('.dt-saved .dt-dirty')).to_have_text('•')
        # Read the stored view once the update's own reply is back (the read raced the save).
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and 'table_view.update' in (r.request.post_data or '')) as saved:
            toolbar(bar, 'Saved views').get_by_role('menuitem', name='Update “Fictional schedule widths”', exact=True).click()
        assert saved.value.ok
        settings = h.get('coordinator', '/api/table-views?table=planner')['views'][0]['definition']['planner']
        assert settings['widths']['date'] > 0
        page.reload()
        expect(page.locator('.wk-grid')).to_have_css('--sched-date', f"{settings['widths']['date']:g}px")
        day = page.get_by_role('separator', name='Resize Day columns', exact=True)
        day.dblclick()
        expect(page.locator('.wk-grid')).to_have_css('--sched-date', '')
        shot(page, 'redesign-saved-widths', full_page=False)
    finally:
        context.close()


def test_people_and_week_corner_names_exclude_their_resize_controls(browser, live_server, h):
    w = h.shift(h.workspace('Fictional corner names'))['workspace']
    people = h.get('coordinator', '/api/catalogues')['people']
    count = sum(p.get('active') is not False for p in people)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/people/2026-10-04?edit={w['id']}")
        grid = page.get_by_role('grid', name='Duties by person and day', exact=True)
        expect(grid.get_by_role('columnheader', name=f'{count} people', exact=True)).to_be_visible()
        expect(grid.get_by_role('separator', name='Resize Row headings', exact=True)).to_be_visible()
        shot(page, 'redesign-people-corner-name', full_page=False)
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        grid = page.locator('#schedule-view .wk-grid')
        expect(grid.get_by_role('columnheader', name='Service', exact=True)).to_be_visible()
        expect(grid.get_by_role('separator', name='Resize Row headings', exact=True)).to_be_visible()
        shot(page, 'redesign-week-corner-name', full_page=False)
    finally:
        context.close()


def test_real_table_resize_cancellation_and_navigation_release_observers(browser, live_server, h):
    h.shift(h.workspace('Fictional resize lifecycle'))
    from .test_browser_rule_catalogue import line
    flag(h)
    h.command('coordinator', 'rule.propose', {'catalogue_id': 'duties_per_week',
        'lines': [line({'max': {'value': 5, 'level': 'mandatory'}})], 'reason': 'Fictional detail table canary'})
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.add_init_script(OBSERVERS)
        page.add_init_script('''
          window.resizeListeners=new Map();
          const add=document.addEventListener.bind(document),remove=document.removeEventListener.bind(document);
          document.addEventListener=(type,fn,opts)=>{
            if(['pointerup','pointercancel','lostpointercapture','keydown'].includes(type)&&document.querySelector('.dt-resize.is-dragging')){
              if(!window.resizeListeners.has(type))window.resizeListeners.set(type,new Set());
              window.resizeListeners.get(type).add(fn);
            }return add(type,fn,opts);
          };
          document.removeEventListener=(type,fn,opts)=>{
            window.resizeListeners.get(type)?.delete(fn);return remove(type,fn,opts);
          };
          window.activeResizeListeners=()=>[...window.resizeListeners.values()].reduce((n,s)=>n+s.size,0);
        ''')
        page.reload()
        navigate(page, 'people')
        grip = page.get_by_role('separator', name='Resize Current rank column', exact=True)
        grip.focus(); grip.press('ArrowRight')
        width = grip.get_attribute('aria-valuenow')
        drag(page, grip, 80, release=False)
        grip.evaluate("g=>g.dispatchEvent(new PointerEvent('pointercancel',{bubbles:true,pointerId:1}))")
        page.mouse.up()
        expect(grip).to_have_attribute('aria-valuenow', width)
        assert page.evaluate('activeResizeListeners()') == 0
        drag(page, grip, 80, release=False)
        assert page.evaluate('activeResizeListeners()') == 4
        navigate(page, 'schedule/coverage/2026-10-04')
        page.mouse.up()
        expect(page.locator('.dt-resizing,.is-dragging')).to_have_count(0)
        assert page.evaluate('activeResizeListeners()') == 0
        matrix = page.locator('.cov table')
        expect(matrix.locator('th .dt-resize').first).to_be_visible()
        expect(matrix.locator('[data-reorder]')).to_have_count(0)
        matrix.locator('th .dt-resize').first.focus()
        page.keyboard.press('ArrowRight')
        navigate(page, 'rules/duties_per_week')
        detail = page.locator('.rc-prop-table')
        grip = detail.get_by_role('separator', name='Resize Change column', exact=True)
        expect(grip).to_be_visible()
        expect(detail.locator('[data-reorder]')).to_have_count(0)
        grip.focus(); grip.press('ArrowRight')
        width = grip.get_attribute('aria-valuenow')
        page.reload()
        expect(grip).to_have_attribute('aria-valuenow', width)
        navigate(page, 'people')
        page.wait_for_function("() => window.__observed().filter(n=>n.tagName==='TH').every(n=>n.isConnected)")
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (1440, 'dark'), (390, 'light'), (390, 'dark')])
def test_compact_and_comfortable_team_names_recover_by_hover_focus_and_open(browser, live_server, h, width, scheme):
    from .test_browser_schedule_team_pills import calendar
    w, session, people, _ = fixture(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900}, color_scheme=scheme)
    try:
        grid = calendar(page, w, 'Week')
        for density in ('Compact', 'Comfortable'):
            panel = view_settings(page.locator('.planner-toolbar'))
            panel.get_by_role('radiogroup', name='Density', exact=True).get_by_role('radio', name=density, exact=True).click()
            close_popover(page)
            show_names(page, 'code')
            for p, name in zip(people[:3], NAMES):
                chip = member(grid, session['id'], p)
                expect(chip).to_have_attribute('aria-label', re.compile(re.escape(name)))
                if p != people[0]:
                    expect(chip).to_have_attribute('aria-label', re.compile('with ' + re.escape(NAMES[0])))
                chip.locator('.pchip-name').hover()
                expect(page.get_by_role('tooltip')).to_contain_text(name)
                page.mouse.move(-1, -1)
                chip.focus()
                expect(page.get_by_role('tooltip')).to_contain_text(name)
            shot(page, f'redesign-team-{density}-{width}-{scheme}', full_page=False)
            member(grid, session['id'], people[1]).click()
            expect(page.locator('.wk-panel')).to_contain_text(NAMES[1])
            page.keyboard.press('Escape')
    finally:
        context.close()


def test_companion_rule_link_opens_canonical_entry_and_all_false_warning_is_truthful(browser, live_server, h):
    from .test_browser_rule_catalogue import catalogue_rule, line
    flag(h)
    trigger, companion, _ = h.seed['people']
    rule = catalogue_rule(h, 'companion_cover', [line({'min': {'value': 2, 'level': 'medium'}},
        who={'conditions': [{'field': 'person_ids', 'values': [companion]}]},
        settings={'minimum_cover': {'when_person_ids': [trigger]}})])
    w = h.shift(h.workspace('Fictional unresolved companion'), people=[trigger])['workspace']
    finding = next(f for f in w['validation']['findings'] if f['rule_id'] == rule['id'])
    assert finding['severity'] == 'warning'
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/issues?draft={w['id']}&view=checks")
        with page.expect_response(lambda r: r.url.endswith('/api/planner/fill-options')) as answer:
            page.get_by_role('button', name='Find someone…', exact=True).click()
        eligible = [c for c in answer.value.json()['candidates'] if c['status'] in ('ok', 'warn')]
        assert eligible and all(c['resolves'] is False for c in eligible)
        panel = page.locator('.wk-panel')
        expect(panel).to_contain_text('Nobody here resolves Companion cover on their own')
        expect(panel.locator('.wk-issue.warning')).to_be_visible()
        panel.get_by_role('link', name='Open the rule', exact=True).click()
        expect(page).to_have_url(re.compile(r'#rules/companion_cover$'))
        expect(page.locator('#main')).not_to_contain_text('This rule is not available')
        expect(page.locator('#main')).to_contain_text('Companion cover')
        shot(page, 'redesign-companion-canonical-entry', full_page=False)
    finally:
        context.close()
