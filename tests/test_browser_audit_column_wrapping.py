"""Column wrapping uses one menu in compact details, collection lists and table matrices.

Owners: apps/web/public/assets/app.css, apps/web/src/table_view.ts, apps/web/src/table_menu.ts.
"""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401
from .test_browser_fairness_dashboard import published_month, october, section
from .test_browser_qa_planner import checked_rota
from .test_browser_rule_catalogue import shot

pytestmark = pytest.mark.browser
PRESENTATIONS = [(1440, 'light'), (1440, 'dark'), (390, 'light'), (390, 'dark')]


def column_menu(page, button, label, keyboard=False):
    if keyboard:
        button.focus()
        button.press('Enter')
    else:
        button.click()
    menu = page.get_by_role('menu', name=f'{label} column', exact=True)
    expect(menu).to_be_visible()
    return menu


@pytest.mark.parametrize('width,scheme', PRESENTATIONS)
def test_plain_table_wraps_each_or_all_columns_and_keeps_controls(browser, live_server, width, scheme):
    context, page = sign_in(browser, live_server, 'auditor', viewport={'width': width, 'height': 900})
    page.emulate_media(color_scheme=scheme)
    try:
        navigate(page, 'people')
        page.evaluate("""async () => {
          const {el,table}=await window.importBundle('dom.js');
          const fixture=el('section',{id:'wrap-fixture'});window.wrapActions={heading:0,row:0,bubble:0};
          const heading=el('button',{type:'button'},'Heading action');
          heading.addEventListener('click',()=>window.wrapActions.heading++);
          const rowButton=el('button',{type:'button'},'Row action');
          rowButton.addEventListener('click',()=>window.wrapActions.row++);
          const long=s=>Array(6).fill(s).join(' ');  // wider than any screen unwrapped: its columns can wrap, so they have menus
          fixture.append(table(['Name',heading,''],[[long('A long departmental duty name with several readable words'),long('A second long explanation of the duty'),rowButton]]));
          fixture.querySelector('thead th:nth-child(2)').addEventListener('click',()=>window.wrapActions.bubble++);
          fixture.append(table(['Empty name','Empty explanation'],[],'No matching records'));
          document.querySelector('#main').prepend(fixture);
        }""")
        grid = page.locator('#wrap-fixture table').first
        name = grid.get_by_role('button', name='Name', exact=True)
        first = grid.locator('tbody td').first
        expect(grid.locator('tbody td.dt-wrap')).to_have_count(2)
        assert first.evaluate("n=>getComputedStyle(n).whiteSpace") == 'normal'
        expect(grid.locator('thead th').last.get_by_role('button')).to_have_count(0)

        menu = column_menu(page, name, 'Name', keyboard=True)
        expect(menu.get_by_role('menuitemcheckbox', name='Wrap all columns')).to_have_attribute('aria-checked', 'true')
        menu.get_by_role('menuitemcheckbox', name='Wrap this column').click()
        expect(name).to_be_focused()
        assert first.evaluate("n=>getComputedStyle(n).whiteSpace") == 'nowrap'
        expect(grid.locator('tbody td').nth(1)).to_have_class(re.compile(r'\bdt-wrap\b'))
        menu = column_menu(page, name, 'Name')
        expect(menu.get_by_role('menuitemcheckbox', name='Wrap all columns')).to_have_attribute('aria-checked', 'false')
        menu.get_by_role('menuitemcheckbox', name='Wrap all columns').click()
        expect(grid.locator('tbody td.dt-wrap')).to_have_count(2)

        # An interactive heading keeps its own action; its separate menu does not also activate that heading.
        options = grid.get_by_role('button', name='Heading action column options', exact=True)
        menu = column_menu(page, options, 'Heading action', keyboard=True)
        menu.get_by_role('menuitemcheckbox', name='Wrap all columns').click()
        expect(grid.locator('tbody td.dt-wrap')).to_have_count(0)
        expect(options).to_be_focused()
        assert page.evaluate('window.wrapActions') == {'heading': 0, 'row': 0, 'bubble': 0}
        options.click(button='right')
        expect(page.get_by_role('menu', name='Heading action column')).to_be_visible()
        page.keyboard.press('Escape')
        expect(options).to_be_focused()
        grid.get_by_role('button', name='Heading action', exact=True).click()
        grid.get_by_role('button', name='Row action', exact=True).click()
        assert page.evaluate('window.wrapActions') == {'heading': 1, 'row': 1, 'bubble': 1}

        # Empty-state colspan remains a single useful message, never a data column.
        empty = page.locator('#wrap-fixture table').nth(1)
        expect(empty.locator('tbody td')).to_have_count(1)
        expect(empty.locator('tbody td')).to_have_attribute('colspan', '2')
        expect(empty.locator('tbody td')).to_have_text('No matching records')
        expect(empty.locator('thead button')).to_have_count(0)  # nothing to wrap: plain headings (X-18)
        expect(empty.locator('tbody td')).not_to_have_class(re.compile(r'\bdt-column-cell\b'))
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', PRESENTATIONS)
def test_planner_list_and_coverage_share_column_wrap_actions(browser, live_server, h, width, scheme):
    workspace, _ = checked_rota(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900})
    page.emulate_media(color_scheme=scheme)
    try:
        navigate(page, f"schedule/list/2026-10-04?edit={workspace['id']}")
        grid = page.locator('[data-table=planner-list]')
        # The planner owns the toolbar; all-column wrapping must remain reachable without a second toolbar.
        expect(grid.locator('.dt-toolbar')).to_have_count(0)
        if width > 720:
            expect(grid.locator('tbody tr[data-row]')).to_have_count(2)
            duty = grid.locator('thead').get_by_role('button', name='Duty', exact=True)
            column_menu(page, duty, 'Duty', keyboard=True).get_by_role('menuitemcheckbox', name='Wrap all columns').click()
            expect(grid.locator('tbody td[data-col]:not(.dt-wrap)')).to_have_count(0)
            expect(duty).to_be_focused()
            column_menu(page, duty, 'Duty').get_by_role('menuitemcheckbox', name='Wrap this column').click()
            expect(grid.locator('tbody td[data-col=duty].dt-wrap')).to_have_count(0)
            expect(grid.locator('tbody td[data-col=place].dt-wrap')).to_have_count(2)
        else:
            # The existing automatic Cards layout has no table columns; it keeps complete session facts.
            expect(grid.locator('.dt-card[data-row]')).to_have_count(2)
            expect(grid.locator('.dt-card').first).to_contain_text('QA clinic')

        navigate(page, f"schedule/coverage/2026-10-04?edit={workspace['id']}")
        coverage = page.get_by_role('table', name='Coverage by duty type and day')
        expect(coverage.locator('tbody tr:not(.cov-sum)')).to_have_count(1)
        # A fixed day matrix has nothing to sort, filter or hide: plain headings, no column menus (X-18, 6f0fceb8);
        # the day headings keep their rule-context action.
        expect(coverage.locator('thead .dt-sort')).to_have_count(0)
        expect(coverage.get_by_role('columnheader', name='Duty', exact=True)).to_be_visible()
        expect(coverage.locator('thead th[data-rk-pin]')).not_to_have_count(0)
        staffed = coverage.locator('button.cov-cell.full').first
        staffed.click()
        expect(page.get_by_role('dialog', name=re.compile('QA clinic sessions on'))).to_be_visible()
        # A phone names people short by default (app.ts labelNow: full names default to the short label on a phone).
        expect(page.get_by_role('dialog', name=re.compile('QA clinic sessions on'))).to_contain_text('A. Morgan' if width < 720 else 'Alex Morgan')
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', PRESENTATIONS)
def test_exposure_wrap_controls_keep_matrix_keyboard_and_drilldown(browser, live_server, h, width, scheme):
    published_month(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900})
    page.emulate_media(color_scheme=scheme)
    try:
        navigate(page, 'insights/fairness')
        october(page)
        heat = section(page, 'exposure').locator('table.fx-heat')
        expect(heat.locator('tbody tr')).to_have_count(3)
        person = heat.locator('thead').get_by_role('button', name='Person', exact=True)
        column_menu(page, person, 'Person', keyboard=True).get_by_role('menuitemcheckbox', name='Wrap all columns').click()
        expect(heat.locator('.dt-column-cell:not(.dt-wrap)')).to_have_count(0)
        expect(person).to_be_focused()
        jordan = heat.locator('thead').get_by_role('button', name='Jordan Lee', exact=True)
        column_menu(page, jordan, 'Jordan Lee').get_by_role('menuitemcheckbox', name='Wrap this column').click()
        expect(heat.locator('thead th').filter(has=page.get_by_role('button', name='Jordan Lee', exact=True))).not_to_have_class(re.compile(r'\bdt-wrap\b'))
        expect(heat.get_by_role('button', name='Alex Morgan with Jordan Lee: 2')).to_have_text('2')
        heat.locator('td[tabindex="0"]').focus()
        page.keyboard.press('ArrowRight')
        cell = heat.locator('td[data-i="0"][data-j="1"]')
        expect(cell).to_be_focused()
        cell.press('Enter')
        drill = page.get_by_role('region', name='Duties shared with a partner · Alex Morgan with Jordan Lee')
        expect(drill.locator('tbody tr')).to_have_count(2)
        # The compact detail table gets the shared column menus only when, unwrapped, it is wider than its box (X-18):
        # it fits on a desktop and needs them on a phone.
        menus = drill.locator('thead .dt-sort')
        if width > 720:
            expect(menus).to_have_count(0)
        else:
            expect(menus).not_to_have_count(0)
    finally:
        context.close()


def one_line(detail):
    # A one-line clamp avoids making all the text the table's minimum column width.
    expect(detail).to_have_css('white-space', 'normal')
    expect(detail).to_have_css('-webkit-line-clamp', '1')
    expect(detail).to_have_css('overflow', 'hidden')
    assert detail.evaluate("x=>Math.abs(x.clientHeight-parseFloat(getComputedStyle(x).lineHeight))<=1")
    assert detail.evaluate("x=>x.clientWidth<=52*parseFloat(getComputedStyle(x).fontSize)")


@pytest.mark.parametrize('width,scheme', [*PRESENTATIONS, (1024, 'dark'), (320, 'dark')])
def test_catalogue_titles_obey_wrap_and_scroll_without_covering_adjacent_columns(browser, live_server, h, width, scheme):
    from apps.api import models as m
    from apps.api.db import transaction
    from .test_browser_overflow import LONG, WORD

    service_name = 'Continuity of care and chronic disease management clinic (adult, extended hours)'
    description = ('A long description covering the purpose of this service, who it supports and the available '
                   'appointments. All of this wording must remain readable when the column is wrapped. ') * 4
    a, b = h.seed['people'][:2]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, a).display_name = LONG
        db.get(m.Person, b).display_name = WORD
        db.get(m.Service, h.seed['services'][0]).name = service_name
        db.get(m.Service, h.seed['services'][0]).description = description
        duty_type = db.get(m.AssignmentType, h.seed['type'])
        duty_type.description = description
        duty_name = duty_type.name
    attachment = h.configure('mentorships', {'person_id': a, 'mentor_id': b, 'starts_on': '2026-10-01',
                            'ends_on': '2026-11-01', 'strength': 'required',
                            'assignment_type_ids': [h.seed['type']]}, role='coordinator')['record']
    context, page = sign_in(browser, live_server, 'admin', {'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        for route, kind, identity, label, wording in [
            ('configuration/assignment_types', 'assignment_types', h.seed['type'], 'Duty type', duty_name),
            ('configuration/services', 'services', h.seed['services'][0], 'Service', service_name),
            ('programmes/mentorships', 'mentorships', attachment['id'], 'Attachment', LONG + ' · with ' + WORD + ' · required'),
        ]:
            navigate(page, route)
            grid = page.locator(f'[data-table="catalogue-{kind}"]')
            if width <= 720:
                grid.get_by_role('button', name='View settings', exact=True).click()
                view = page.get_by_role('dialog', name='View settings', exact=True)
                view.get_by_role('radio', name='Table', exact=True).click()
                page.keyboard.press('Escape')
                expect(view).to_have_count(0)
            row = grid.locator(f'tr[data-row="{identity}"]')
            title = row.locator('.ct-name h3')

            detail = row.locator('.ct-name > small')
            cell = row.locator('td[data-col=name]')
            header = grid.locator('th[data-col=name]').get_by_role('button', name=label, exact=True)
            scroll = grid.locator('.dt-body > .table-scroll')
            expect(title).to_have_text(wording)
            expect(title).to_have_css('white-space', 'nowrap')
            one_line(detail)
            if kind != 'mentorships':
                expect(detail).to_have_attribute('data-ctip', description)
            assert title.evaluate('(x)=>x.scrollWidth<=x.clientWidth+1')
            expect(cell).to_have_css('position', 'relative')  # A long title never pins over the other columns.
            if kind == 'mentorships':
                before = cell.bounding_box()['x']
                moved = scroll.evaluate('(s)=>{s.scrollLeft=s.scrollWidth;return s.scrollLeft}')
                assert moved > 0
                assert cell.bounding_box()['x'] < before - 1
                status = row.locator('td[data-col=status]')
                assert status.evaluate('''(c)=>{const r=c.getBoundingClientRect(),hit=document.elementFromPoint(
                    r.left+r.width/2,r.top+r.height/2);return !!hit&&c.contains(hit);}'''), 'The status column remains reachable, not covered by a pinned name'
            scroll.evaluate('(s)=>{s.scrollLeft=0}')
            column_menu(page, header, label, keyboard=True).get_by_role('menuitemcheckbox', name='Wrap this column', exact=True).click()
            expect(header).to_be_focused()
            expect(cell).to_have_class(re.compile(r'\bdt-wrap\b'))
            expect(title).to_have_css('white-space', 'normal')
            assert title.evaluate('(x)=>x.scrollWidth<=x.clientWidth+1')
            expect(title).to_have_text(wording)
            expect(detail).to_have_css('white-space', 'normal')
            if kind != 'mentorships':
                expect(detail).to_have_text(description)
                assert detail.evaluate('''x=>x.clientHeight>2*parseFloat(getComputedStyle(x).lineHeight)
                    &&x.scrollHeight<=x.clientHeight+1&&x.scrollWidth<=x.clientWidth+1'''), 'Wrapped description is readable beyond two lines'
            shot(page, f'table-description-{kind}-{width}-{scheme}')
            # The per-column action and all-column action control this same composed name cell.
            column_menu(page, header, label).get_by_role('menuitemcheckbox', name='Wrap all columns', exact=True).click()
            column_menu(page, header, label).get_by_role('menuitemcheckbox', name='Wrap this column', exact=True).click()
            expect(cell).not_to_have_class(re.compile(r'\bdt-wrap\b'))
            expect(row.locator('td[data-col=status]')).to_have_class(re.compile(r'\bdt-wrap\b'))
            one_line(detail)
            if kind != 'mentorships':
                expect(detail).to_have_attribute('data-ctip', description)
            page.reload()
            one_line(detail)
            if kind != 'mentorships':
                expect(detail).to_have_attribute('data-ctip', description)
            expect(row.locator('td[data-col=status]')).to_have_class(re.compile(r'\bdt-wrap\b'))
            menu = column_menu(page, header, label)
            expect(menu.get_by_role('menuitemcheckbox', name='Wrap this column', exact=True)).to_have_attribute('aria-checked', 'false')
            expect(menu.get_by_role('menuitemcheckbox', name='Wrap all columns', exact=True)).to_have_attribute('aria-checked', 'false')
            menu.get_by_role('menuitemcheckbox', name='Wrap all columns', exact=True).click()
            column_menu(page, header, label).get_by_role('menuitemcheckbox', name='Wrap all columns', exact=True).click()
            expect(cell).not_to_have_class(re.compile(r'\bdt-wrap\b'))
            expect(title).to_have_css('white-space', 'nowrap')
            assert title.evaluate('(x)=>x.scrollWidth<=x.clientWidth+1')
            assert page.evaluate('()=>document.documentElement.scrollWidth<=innerWidth+1')
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', PRESENTATIONS)
def test_composed_free_text_obeys_column_wrap_without_changing_dates_or_cards(browser, live_server, width, scheme):
    """Exercise the shared table with the composed text used by Requests, Exceptions and Inbox."""
    context, page = sign_in(browser, live_server, 'admin', {'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'people')
        page.evaluate('''async () => {
          const {el,dateUnits}=await window.importBundle('dom.js');
          const {dataTable}=await window.importBundle('table_view.js');
          const text='A detailed explanation with several readable words about this request and its outcome. '.repeat(8);
          const renderers=[
            ()=>el('span',{class:'request-title'},el('strong',{},text)),
            ()=>el('span',{class:'request-detail'},text),
            ()=>el('span',{class:'swap-reason'},text),
            ()=>el('button',{type:'button',class:'note-main'},el('span',{class:'note-msg'},text)),
          ];
          const fixture=dataTable({id:'wrap-composed-copy',noun:['record','records'],
            rows:renderers.map((cell,i)=>({id:String(i),cell})),rowId:r=>r.id,
            columns:[{key:'copy',label:'Copy',cell:r=>r.cell()},
              {key:'date',label:'Date',cell:()=>el('span',{class:'request-when'},dateUnits(['17 November 2026','– 21 December 2026']))}],
            defaults:{layout:'table'}});
          const compact=el('section',{id:'wrap-compact-copy'},
            el('div',{class:'ct-name'},el('small',{},text)),el('span',{class:'note-msg'},text));
          document.querySelector('#main').replaceChildren(fixture.root,compact);
        }''')
        grid = page.locator('[data-table="wrap-composed-copy"]')
        grid.get_by_role('button', name='View settings', exact=True).click()
        view = page.get_by_role('dialog', name='View settings', exact=True)
        view.get_by_role('radio', name='Table', exact=True).click()
        page.keyboard.press('Escape')
        header = grid.locator('th[data-col=copy]').get_by_role('button', name='Copy', exact=True)
        texts = grid.locator('td[data-col=copy] :is(.request-title strong,.request-detail,.swap-reason,.note-msg)')
        nowrap = grid.locator('td[data-col=copy] :is(.request-title strong,.request-detail,.swap-reason)')
        for text in nowrap.all():
            expect(text).to_have_css('white-space', 'nowrap')
        one_line(grid.locator('.note-msg'))
        expect(grid.locator('.request-when').first).to_have_css('white-space', 'nowrap')
        column_menu(page, header, 'Copy', keyboard=True).get_by_role('menuitemcheckbox', name='Wrap all columns', exact=True).click()
        for text in texts.all():
            expect(text).to_have_css('white-space', 'normal')
            assert text.evaluate('''x=>x.clientHeight>2*parseFloat(getComputedStyle(x).lineHeight)
                &&x.scrollHeight<=x.clientHeight+1&&x.scrollWidth<=x.clientWidth+1'''), 'Wrapped free text is complete, not clamped'
        assert grid.locator('.swap-reason').evaluate('''x=>{const cell=x.closest('td'),s=getComputedStyle(cell);
            return x.getBoundingClientRect().width>=cell.clientWidth-parseFloat(s.paddingLeft)-parseFloat(s.paddingRight)-1;
        }'''), 'Wrapped exception text uses the available column width instead of a narrow strip'
        for atom in grid.locator('.date-units > span').all():
            expect(atom).to_have_css('white-space', 'nowrap')
        shot(page, f'table-composed-copy-{width}-{scheme}')
        column_menu(page, header, 'Copy').get_by_role('menuitemcheckbox', name='Wrap this column', exact=True).click()
        for text in nowrap.all():
            expect(text).to_have_css('white-space', 'nowrap')
        one_line(grid.locator('.note-msg'))
        expect(grid.locator('td[data-col=date]').first).to_have_class(re.compile(r'\bdt-wrap\b'))
        for compact in page.locator('#wrap-compact-copy :is(small,.note-msg)').all():
            expect(compact).to_have_css('-webkit-line-clamp', '2')
        assert page.evaluate('()=>document.documentElement.scrollWidth<=innerWidth+1')
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', PRESENTATIONS)
def test_a_short_table_that_never_wraps_has_plain_headings(browser, live_server, width, scheme):
    """X-18 (review of maintenance): column menus whose one choice is Wrap appear only where a table, unwrapped, would be wider
    than its box. A two-column Shortcut/Action table keeps plain headings."""
    context, page = sign_in(browser, live_server, 'auditor', viewport={'width': width, 'height': 900})
    page.emulate_media(color_scheme=scheme)
    try:
        navigate(page, 'people')
        page.evaluate("""async () => {
          const {el,table}=await window.importBundle('dom.js');
          const fixture=el('section',{id:'short-fixture'});
          fixture.append(table(['Shortcut','Action'],[['Ctrl K','Search'],['Ctrl Z','Undo']]));
          document.querySelector('#main').prepend(fixture); }""")
        grid = page.locator('#short-fixture table')
        expect(grid.locator('tbody tr')).to_have_count(2)
        expect(grid.locator('thead th').first).to_have_text('Shortcut')
        page.evaluate('() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))')  # after its first layout check
        expect(grid.locator('thead button')).to_have_count(0)
    finally:
        context.close()
# Include this file in hosted supervision and team-lead qualification.
