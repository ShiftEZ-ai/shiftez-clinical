"""Inbox, audit log, Names & codes and access history use the one collection toolbar: filter, clear, count."""
import re

from datetime import datetime, timedelta

import pytest
from playwright.sync_api import expect

from apps.api.db import transaction
from apps.api import models as m
from .test_browser import browser, live_server, sign_in, navigate, condition, planner_filter, pick_view, close_popover, toolbar, save_dialog  # noqa: F401
from .test_browser_uiux_polling import notification
from .governance_support import developer
from .collection_fixture import mount

pytestmark = pytest.mark.browser


def open_filter(scope):
    scope.get_by_role('button', name=re.compile('^Filter')).click()
    pop = scope.page.get_by_role('dialog', name='Filter')
    expect(pop).to_be_visible()
    return pop


def pick(page, pop, label, value):
    """Filter is the shared condition builder: "Where <label> is <value>"."""
    condition(pop, label, [value])
    page.keyboard.press('Escape')


def choose_view(scope, label):
    """A built-in view from the Saved views menu (its own filter is the view, not one the person set: X-01)."""
    scope.get_by_role('button', name=re.compile('^Saved views')).click()
    scope.page.get_by_role('menu', name='Saved views').get_by_role('menuitemradio', name=re.compile('^' + label)).click()


def clear(scope):
    open_filter(scope).get_by_role('button', name='Clear all filters', exact=True).click()
    scope.page.keyboard.press('Escape')


def test_inbox_opens_on_unread_and_filters_by_read_state(browser, live_server, h):
    recipient = h.get('coordinator', '/api/me')['identity_id']
    with transaction(h.app.state.engine, write=True) as db:
        read = notification(db, h, recipient, 'Already read notice')
        notification(db, h, recipient, 'Waiting notice')
        db.get(m.Notification, read).acknowledged_at = m.now()
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'inbox')
        count = page.locator('.app-footer .dt-count')
        # Unread first: the Inbox's built-in Unread view (f8), named on the views button; its own filter is the view,
        # so Filter stays plain (X-01, no accent) but counts it as the view's (F-C2), and the footer says the read notice
        # is one view away.
        expect(page.get_by_text('Waiting notice', exact=True)).to_be_visible()
        expect(page.get_by_text('Already read notice', exact=True)).to_have_count(0)
        expect(page.locator('#main').get_by_role('button', name=re.compile('^Saved views'))).to_contain_text('Unread')
        expect(page.locator('#main').get_by_role('button', name='Filter (1 from the view)', exact=True)).to_be_visible()
        expect(count).to_have_text('1 of 2 notifications')
        # Grouped by day, in cards.
        expect(page.locator('.dt-card-group').first).to_contain_text('Today')
        choose_view(page.locator('#main'), 'All notifications')
        expect(page.get_by_text('Already read notice', exact=True)).to_be_visible()
        expect(count).to_have_text('2 notifications')
        pick(page, open_filter(page.locator('#main')), 'Read', 'Read')
        expect(page.get_by_text('Waiting notice', exact=True)).to_have_count(0)
        expect(count).to_have_text('1 of 2 notifications')
    finally:
        context.close()


def test_audit_log_filters_on_the_server_and_clears(browser, live_server, h):
    h.configure('categories', {'name': 'Audit filter rank', 'starts_on': '2020-01-01'})
    h.workspace('Audit filter rota')
    h.get(developer(h), '/api/me')  # sign the API client in first: its sign-in is a change in the log too
    context, page = sign_in(browser, live_server, 'developer')
    try:
        navigate(page,'system/audit')
        rows = page.locator('.audit-row')
        expect(rows.first).to_be_visible()
        before = rows.count()
        chips = page.locator('.audit-row .role-chip')
        expect(chips.filter(has_text=re.compile('^Rotas$'))).not_to_have_count(0)
        count = page.locator('.app-footer .footer-status')
        expect(count).to_have_text(f'1–{h.get("developer", "/api/audit")["events"].__len__()} changes')
        pop = open_filter(page.locator('#main'))
        pop.get_by_label('Kind of change', exact=True).select_option('config')
        page.keyboard.press('Escape')
        expect(page.get_by_role('button', name='Filter (1 active)', exact=True)).to_be_visible()
        expect(chips.filter(has_text=re.compile('^Settings$'))).not_to_have_count(0)
        expect(chips.filter(has_text=re.compile('^Rotas$'))).to_have_count(0)
        page.get_by_label('Search history', exact=True).fill('no change has this reason')
        expect(page.get_by_role('heading', name='Nothing matches', exact=True)).to_be_visible()
        expect(count).to_have_text('No changes')
        page.get_by_label('Search history', exact=True).fill('')
        clear(page.locator('#main'))
        expect(page.get_by_role('button', name='Filter', exact=True)).to_be_visible()
        expect(rows).to_have_count(before)
    finally:
        context.close()


def test_names_and_codes_filter_by_code_status(browser, live_server, h):
    people = [p for p in h.get('admin', '/api/catalogues')['people'] if p.get('active', True)]
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'configuration')
        page.get_by_role('button', name='Names & codes').click()
        table = page.locator('#main .dt')
        count = page.locator('.app-footer .dt-count')
        expect(count).to_have_text(f'{len(people)} people')
        expect(table.get_by_label('Short code for ' + people[0]['display_name'], exact=True)).to_be_visible()
        pick(page, open_filter(table), 'Code status', 'Automatic')
        expect(table.get_by_role('button', name='Filter: Automatic (1 active)', exact=True)).to_be_visible()
        table.get_by_label('Find people', exact=True).fill('nobody has this name')
        expect(table.get_by_role('heading', name='No matching people', exact=True)).to_be_visible()
        expect(count).to_have_text(f'0 of {len(people)} people')
        table.get_by_label('Find people', exact=True).fill('')
        clear(table)
        expect(count).to_have_text(f'{len(people)} people')
    finally:
        context.close()


def test_access_history_is_grouped_by_person_and_filters_by_state(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page,'access/people')
        page.get_by_role('tab', name=__import__('re').compile('^Role history')).click()
        history = page.get_by_role('tabpanel')
        expect(history.locator('.dt-group').first).to_be_visible()
        bindings = h.get('admin', '/api/access')['bindings']
        total = len(bindings)
        current = sum(1 for b in bindings if not b['revoked'] and not b['ends_on'])
        # Role history is its own tab: its count is the footer's.
        count = page.locator('.app-footer .dt-count')
        expect(count).to_have_text(f'{total} grants')
        pick(page, open_filter(history), 'State', 'Current')
        expect(history.get_by_role('button', name='Filter: Current (1 active)', exact=True)).to_be_visible()
        expect(count).to_have_text(f'{current} grants' if current == total else f'{current} of {total} grants')
        history.get_by_label('Search access history', exact=True).fill('nobody has this name')
        expect(history.get_by_role('heading', name='No matching grants', exact=True)).to_be_visible()
        history.get_by_label('Search access history', exact=True).fill('')
        clear(history)
        expect(count).to_have_text(f'{total} grants')
    finally:
        context.close()


def anchored(trigger, panel):
    """The panel opens against its trigger: below (or above) it, overlapping it horizontally."""
    t, p = trigger.bounding_box(), panel.bounding_box()
    vertical = p['y'] >= t['y'] + t['height'] - 1 or p['y'] + p['height'] <= t['y'] + 1
    return vertical and p['x'] < t['x'] + t['width'] and p['x'] + p['width'] > t['x']


def test_filter_popover_is_tied_to_its_button_and_lists_active_filters(browser, live_server, h):
    recipient = h.get('coordinator', '/api/me')['identity_id']
    with transaction(h.app.state.engine, write=True) as db:
        read = notification(db, h, recipient, 'Tethered read notice')
        notification(db, h, recipient, 'Tethered waiting notice')
        db.get(m.Notification, read).acknowledged_at = m.now()
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'inbox')
        # A filter the person sets (Read, on All notifications; Unread would be the built-in Unread view) is summarised on the Filter
        # button itself; there is no separate chip row.
        choose_view(page.locator('#main'), 'All notifications')
        pick(page, open_filter(page.locator('#main')), 'Read', 'Read')
        button = page.locator('#main .dt-toolbar').get_by_role('button', name=re.compile('^Filter'))
        expect(button).to_have_accessible_name('Filter: Read (1 active)')
        expect(button).to_contain_text('Filter: Read')
        expect(page.locator('#main .dt-chips')).to_have_count(0)
        button.click()
        pop = page.get_by_role('dialog', name='Filter')
        expect(pop).to_be_visible()
        expect(button).to_have_attribute('aria-expanded', 'true')
        expect(button).to_have_class(re.compile(r'\bis-open\b'))
        expect(page.locator('.pop-arrow.top')).to_have_count(1)  # points up at the button
        assert anchored(button, pop)
        expect(pop.locator('.dt-active')).to_contain_text('Active · 1 of 2 notifications')
        # A picker inside opens against its own button, which shows it is open.
        pick_button = pop.get_by_role('button', name=re.compile('^Read: '))
        pick_button.click()
        picker = page.locator('.picker')
        expect(picker).to_be_visible()
        expect(pick_button).to_have_class(re.compile(r'\bis-open\b'))
        assert anchored(pick_button, picker)
        page.keyboard.press('Escape')
        expect(pick_button).not_to_have_class(re.compile(r'\bis-open\b'))
        # The rule is a row of the builder (no second list with ×); Clear all drops it and the popover stays open.
        expect(pop.locator('.dt-active-list')).to_have_count(0)
        pop.get_by_role('button', name='Clear all filters', exact=True).click()
        expect(page.locator('.app-footer .dt-count')).to_have_text('2 notifications')
        expect(pop.locator('.dt-active')).to_be_hidden()
        expect(page.locator('#main').get_by_role('button', name='Filter', exact=True)).to_be_visible()
        page.keyboard.press('Escape')
        expect(pop).to_have_count(0)
        expect(button).to_have_attribute('aria-expanded', 'false')
        expect(button).not_to_have_class(re.compile(r'\bis-open\b'))
        expect(page.locator('.pop-arrow')).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('width', [1440, 390])
def test_view_menu_focuses_checked_layout_without_changing_it(browser, live_server, width):
    """Inbox live defect: Cards was chosen but View settings focused Table (the first radio). Any collection: entry
    focus goes to the checked layout, and Space keeps it."""
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 900})
    try:
        navigate(page, 'people')
        trigger = page.locator('#main .dt-toolbar').get_by_role('button', name=re.compile('^View settings'))
        panel = page.get_by_role('dialog', name='View settings', exact=True)
        layouts = panel.get_by_role('radiogroup', name='Layout', exact=True)
        cards = layouts.get_by_role('radio', name='Cards', exact=True)
        table = layouts.get_by_role('radio', name='Table', exact=True)
        trigger.focus()
        page.keyboard.press('Enter')
        if cards.get_attribute('aria-checked') != 'true':
            cards.click()
            page.keyboard.press('Escape')
            trigger.focus()
            page.keyboard.press('Enter')
        expect(cards).to_have_attribute('aria-checked', 'true')
        expect(cards).to_be_focused()
        page.keyboard.press('Space')
        expect(cards).to_have_attribute('aria-checked', 'true')
        expect(table).to_have_attribute('aria-checked', 'false')
        page.keyboard.press('ArrowLeft')
        expect(table).to_be_focused()
        # Layout redraws the list, so its arrows only move (activation 'manual', bc5e698e3): Space chooses.
        expect(table).to_have_attribute('aria-checked', 'false')
        page.keyboard.press('Space')
        expect(table).to_have_attribute('aria-checked', 'true')
        page.keyboard.press('Escape')
        expect(panel).to_have_count(0)
        expect(trigger).to_be_focused()
        page.keyboard.press('Enter')
        expect(table).to_be_focused()
        page.keyboard.press('Escape')
        expect(trigger).to_be_focused()
    finally:
        context.close()


def test_a_lists_own_filter_is_not_on_and_the_popover_opens_on_its_rule(browser, live_server, h):
    """X-01, X-02: a list's default filter (Status = Active) is its own view, not a filter the person set: Filter stays
    plain. The popover shows the rule once, as a builder row, and opens on it, never on Clear all."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        fixture = mount(page, 'fixture-default-filter', defaults={'filters': {'status': ['Active']}})
        button = fixture.locator('.dt-toolbar').get_by_role('button', name=re.compile('^Filter'))
        # F-C2: the view's own rule is counted on the button and named as the view's, without the accent.
        expect(button).to_have_accessible_name('Filter (1 from the view)')
        expect(button).not_to_have_class(re.compile(r'\bon\b'))
        expect(button).not_to_have_class(re.compile(r'\bon\b'))
        expect(fixture.locator('tbody tr[data-row]')).to_have_count(8)
        button.click()
        pop = page.get_by_role('dialog', name='Filter')
        expect(pop.locator('.dt-filter-builder .cond-line')).to_have_count(1)  # the default rule, once, as a row
        expect(pop.locator('.dt-active-list')).to_have_count(0)  # not even an empty, hidden one: no second list of the rules
        expect(pop.get_by_role('button', name='Clear all filters')).to_be_visible()  # one click back to every row
        assert pop.evaluate("p => !!document.activeElement.closest('.dt-filter-builder')")
        # A change of the person's own lights it, and Clear all appears without a second list of the same rules.
        pop.locator('.cond-line .pick-button').first.click()
        page.locator('.picker').get_by_role('option', name='Inactive', exact=True).check()
        page.keyboard.press('Escape')
        expect(button).to_have_class(re.compile(r'\bon\b'))
        expect(button).to_have_accessible_name('Filter: Active, Inactive (2 active)')
        expect(pop.get_by_role('button', name='Clear all filters')).to_be_visible()
        expect(pop.locator('.dt-active-list')).to_have_count(0)
        page.keyboard.press('Escape')
        button.click()
        assert not page.evaluate("document.activeElement.matches('.dt-clear')")
    finally:
        context.close()


def test_a_built_in_view_keeps_filter_plain_and_its_count_while_searching(browser, live_server, h):
    """X-01 / A-33 (a built-in view's own status filter is not "on") and R-35 (the view's count is its rows, not the
    search's): the views menu says "Active only 8" while a search finds nothing; the button carries no count, which is the
    footer's ."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        views = [{'label': 'All rows', 'filters': {'status': []}}, {'label': 'Active only', 'filters': {'status': ['Active']}}]
        fixture = mount(page, 'fixture-builtin', views=views, defaults={'filters': {'status': ['Active']}})
        toolbar_ = fixture.locator('.dt-toolbar')
        expect(toolbar_.get_by_role('button', name=re.compile('^Filter'))).to_have_accessible_name('Filter (1 from the view)')
        saved = toolbar_.get_by_role('button', name=re.compile('^Saved views'))
        expect(saved).to_contain_text('Active only')
        expect(saved).not_to_contain_text('8')
        fixture.get_by_role('searchbox').fill('zzzz')
        expect(fixture.locator('.empty-state')).to_be_visible()
        saved.click()
        expect(page.get_by_role('menu', name='Saved views').get_by_role('menuitemradio', name=re.compile('^Active only'))).to_contain_text('8')
    finally:
        context.close()


def test_department_scale_sort_reads_each_key_once_and_a_tick_never_sorts_again(browser, live_server, h):
    """AD-1, AD-2: 5,000 rows sorted by a key: each row's key is read once per draw (not per comparison), and ticking a
    row (bulk "matching") reuses the draw's rows instead of filtering and sorting the list again."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        fixture = mount(page, 'fixture-sort-scale', rows=5000, bulk=True, defaults={'sort': ['rank', 'asc']})
        expect(fixture.locator('tbody tr[data-row]')).to_have_count(50)
        assert page.evaluate('window.fixture.sortCalls') == 5000
        page.evaluate('window.fixture.sortCalls = 0')
        fixture.locator('tbody tr[data-row]').first.get_by_role('checkbox').check()
        expect(page.get_by_text('1 row selected')).to_be_attached()
        fixture.locator('tbody tr[data-row]').nth(1).get_by_role('checkbox').check()
        assert page.evaluate('window.fixture.sortCalls') == 0
    finally:
        context.close()


def test_a_failed_server_search_does_not_leave_the_rows_busy(browser, live_server, h):
    """SH-05 follow-up (review of #552): a search the server runs marks the rows busy until its answer; when that read
    fails, the rows shown are no longer marked busy (the page reports the failure)."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        fixture = mount(page, 'fixture-server-search-fails', rows=60, serverSearch='fails')
        page.on('pageerror', lambda e: None)
        fixture.get_by_role('searchbox').fill('row 004')
        body = fixture.locator('.dt-body')
        expect(body).to_have_attribute('aria-busy', 'true')
        page.evaluate('window.fixture.failSearch()')
        expect(body).to_have_attribute('aria-busy', 'false')
        assert page.evaluate('window.fixture.failed') == 'Fictional search failure'  # settled by the failure, not a draw
        expect(fixture.locator('tbody tr[data-row]')).to_have_count(50)  # the rows shown stay
    finally:
        context.close()


def test_show_more_appends_rows_and_keeps_the_rows_already_drawn(browser, live_server, h):
    """SH-02: more rows of the same list (Show more) are appended into their groups; the rows already drawn keep their
    nodes, the group counts follow and a kept row's box still ticks."""
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        navigate(page, 'people')
        fixture = mount(page, 'fixture-append', rows=100, pageSize=100000, bulk=True, defaults={'group': 'status', 'layout': 'table'})
        rows = fixture.locator('tbody tr[data-row]')
        expect(rows).to_have_count(100)
        page.evaluate("""() => {window.firstRow = document.querySelector('#fixture tbody tr[data-row]');
          window.removed = 0; new MutationObserver(list => {for (const m of list) for (const n of m.removedNodes) if (n.dataset?.row) window.removed++;})
            .observe(document.querySelector('#fixture .dt-body'), {childList: true, subtree: true});}""")
        for _ in range(2):
            page.evaluate('window.fixture.more(100); window.fixture.table.refresh()')
        expect(rows).to_have_count(300)
        assert page.evaluate("window.firstRow === document.querySelector('#fixture tbody tr[data-row]') && window.firstRow.isConnected")
        assert page.evaluate('window.removed') == 0
        expect(fixture.locator('.dt-group-toggle[data-group="Active"]')).to_contain_text('200')
        # Rows stay in their groups: every Inactive row follows the Inactive heading.
        assert page.evaluate("""() => {let group = ''; for (const tr of document.querySelectorAll('#fixture tbody tr')) {
          if (tr.classList.contains('dt-group')) group = tr.querySelector('.dt-group-toggle').dataset.group;
          else if (window.fixture.rows.find(r => r.id === tr.dataset.row).status !== group) return false;} return true;}""")
        rows.first.get_by_role('checkbox').check()
        expect(rows.first.get_by_role('checkbox')).to_be_checked()
        expect(page.get_by_text('1 row selected')).to_be_attached()
    finally:
        context.close()


def test_long_list_search_draws_once_per_pause_and_server_search_draws_once(browser, live_server, h):
    """PI-3: typing into a long list draws the rows once per pause, not once per key. SH-05: a search the server runs
    keeps the rows (busy) until its answer, then draws once."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        # The page clock holds every pause: the list's own first work settles before counting, and no real time between
        # keys can split one pause into two.
        start = datetime(2026, 10, 5, 9, 0)
        page.clock.install(time=start)
        fixture = mount(page, 'fixture-search-draws', rows=400)
        page.clock.pause_at(start + timedelta(seconds=5))  # an installed clock still runs with real time until paused
        count_draws = """() => {window.bodyDraws = 0; new MutationObserver(list => {for (const m of list) if (m.addedNodes.length) window.bodyDraws++;})
          .observe(document.querySelector('#fixture .dt-body'), {childList: true});}"""
        page.evaluate(count_draws)
        fixture.get_by_role('searchbox').press_sequentially('row 001')
        assert page.evaluate('window.bodyDraws') == 0  # nothing drawn while typing
        page.clock.run_for(1000)
        expect(fixture.locator('tbody tr[data-row]')).to_have_count(10)
        assert page.evaluate('window.bodyDraws') == 1
        page.clock.resume()
        page.evaluate("document.querySelector('#fixture').remove()")
        fixture = mount(page, 'fixture-server-search', rows=60, serverSearch=True)
        page.evaluate(count_draws)
        fixture.get_by_role('searchbox').fill('row 004')
        expect(fixture.locator('.dt-body')).to_have_attribute('aria-busy', 'true')
        expect(fixture.locator('tbody tr[data-row]')).to_have_count(10)
        expect(fixture.locator('.dt-body')).to_have_attribute('aria-busy', 'false')
        assert page.evaluate('window.fixture.searches') == ['row 004']
        assert page.evaluate('window.bodyDraws') == 1
    finally:
        context.close()


def test_a_draw_does_not_read_layout_straight_after_writing_rows(browser, live_server, h):
    """PI-5: the table's height is measured in the next frame, not right after its rows are written (a forced layout per
    draw)."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        mount(page, 'fixture-layout-read', rows=200)
        reads = page.evaluate("""async () => {let n = 0; const own = Element.prototype.getBoundingClientRect;
          Element.prototype.getBoundingClientRect = function () {n++; return own.call(this);};
          try {window.fixture.table.refresh();} finally {Element.prototype.getBoundingClientRect = own;}
          await new Promise(r => requestAnimationFrame(() => r(undefined)));
          return {sync: n, top: getComputedStyle(document.querySelector('#fixture .dt')).getPropertyValue('--dt-top')};}""")
        assert reads['sync'] == 0
        assert reads['top'].strip().endswith('px')
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_schedule_filter_is_the_shared_condition_builder(browser, live_server, h, scheme):
    """SR-06: the Schedule's Filter is the shared condition builder (is not, And/Or, groups), saved with the view."""
    other = h.configure('assignment_types', {'name': 'FM other clinic', 'starts_on': '2020-01-01',
                        'definition': {'requirements': [{'minimum': 1}]}})['record']['id']
    w = h.shift(h.workspace('Filter builder rota'))['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', typ=other)['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', color_scheme=scheme)
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        pick_view(page, 'List')
        rows = page.locator('tr.dt-open')
        expect(rows).to_have_count(2)
        pop = planner_filter(page)
        expect(pop.locator('.dt-filter-builder')).to_have_count(1)
        expect(pop.locator('.dt-pop-fields')).to_have_count(0)  # no per-field pickers of its own
        expect(pop.get_by_role('checkbox', name='Only sessions with open places', exact=True)).to_be_visible()
        condition(pop, 'Duty type', ['FM other clinic'], op='none')
        expect(rows).to_have_count(1)
        expect(rows.first).not_to_contain_text('FM other clinic')
        close_popover(page)
        toolbar(page.locator('.planner-toolbar'), 'Saved views').get_by_role('menuitem', name='New view…', exact=True).click()
        form = page.get_by_role('dialog', name='New view')
        form.get_by_label('View name', exact=True).fill('Not the other clinic')
        save_dialog(form, 'Save view')
        saved = h.get('coordinator', '/api/table-views?table=planner')['views'][0]['definition']
        line = saved['conditions']['conditions'][0]
        assert (line['field'], line['op'], line['values']) == ('assignment_type_ids', 'none', [other])
        page.reload()
        navigate(page, 'planner')
        pick_view(page, 'Not the other clinic')
        expect(rows).to_have_count(1)
    finally:
        context.close()


def test_group_popover_note_always_shown(browser, live_server, h):
    """FD-01: the Group popover says "Saved with the view" whether or not a grouping is set; "Then by" waits for a first one."""
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'people')
        # People opens ungrouped: the popover lists the fields to group by, with the note.
        group = toolbar(page.locator('#main'), 'Group')
        expect(group.get_by_text('Saved with the view', exact=True)).to_be_visible()
        expect(group.get_by_text('Then by', exact=True)).to_have_count(0)
        group.get_by_role('menuitemradio', name=re.compile('^Current rank')).click()
        group = toolbar(page.locator('#main'), 'Group')
        expect(group.get_by_role('button', name='Remove grouping', exact=True)).to_be_visible()
        expect(group.get_by_text('Saved with the view', exact=True)).to_be_visible()
        group.get_by_role('button', name='Remove grouping', exact=True).click()
        group = toolbar(page.locator('#main'), 'Group')
        expect(group.get_by_text('Saved with the view', exact=True)).to_be_visible()
        expect(group.get_by_text('Then by', exact=True)).to_have_count(0)
    finally:
        context.close()


def test_schedule_toolbar_controls_do_not_vanish_between_views(browser, live_server, h):
    """SR-07 / SR-10: the Schedule's one search box is named by what it finds (sessions, or people in People), and Year
    keeps it in place, switched off, instead of removing it."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule/week/2026-10-05')
        expect(page.get_by_role('searchbox', name='Search sessions', exact=True)).to_be_enabled()
        navigate(page, 'schedule/people/2026-10-05')
        expect(page.get_by_role('searchbox', name='Search people', exact=True)).to_be_visible()
        navigate(page, 'schedule/year/2026-10-05')
        expect(page.get_by_role('searchbox', name='Search sessions (not in Year)', exact=True)).to_be_disabled()
    finally:
        context.close()


def test_rules_uses_shared_view_settings(browser, live_server, h):
    """FD-02: Rules' View settings is the shared Shown properties (show, hide and move), not a checklist of its own."""
    from .test_browser_rule_catalogue import flag
    flag(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        view = toolbar(page.locator('#main'), 'View settings')
        props = view.get_by_role('list', name='Shown properties')
        expect(props.locator('.dt-prop')).to_have_count(5)
        props.get_by_role('button', name='Move Where down', exact=True).click()
        expect(page.locator('#main thead th').nth(3)).to_contain_text('Numbers')
        expect(view).to_be_visible()  # moving keeps the popover open, as on every collection
    finally:
        context.close()
