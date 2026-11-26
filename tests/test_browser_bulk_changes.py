"""Bulk changes (docs/design/bulk-changes.md): select → Change… → Preview → Apply → Results → Undo, in the real app and
API. Desktop 1440 light and dark; the phone keeps request decisions (a bottom sheet for Decline)."""
import json
from .browser_seed import seeded
import re

import pytest
from playwright.sync_api import expect

from apps.api.settings import Settings

from .test_browser import department_tab, add_filter, browser, live_server, sign_in, navigate, expect_loaded, toolbar, condition, close_popover, sort_direction, holding  # noqa: F401
from .test_browser import whole_list

pytestmark = pytest.mark.browser
DESKTOP, PHONE = {'width': 1440, 'height': 1000}, {'width': 390, 'height': 844}


def bar(page):
    return page.get_by_role('toolbar', name='Bulk actions')


def panel(page):
    return page.locator('.dock-panel.bulk-panel')


def caret(scope):
    """The ▾ beside the select-all box: the selection menu."""
    scope.get_by_role('button', name='Selection options', exact=True).first.click()
    menu = scope.page.get_by_role('menu', name='Selection options')
    expect(menu).to_be_visible()
    return menu


def choose(select, label):
    """Choose an option by its words (a pattern matches the first option it fits)."""
    if isinstance(label, str):
        return select.select_option(label=label)
    words = select.evaluate("s=>[...s.options].map(o=>o.textContent)")
    select.select_option(label=next(w for w in words if label.search(w)))


def submit(page):
    """The Change form's button: "Apply N changes" when a clean result applies at once, else "Preview →"."""
    panel(page).locator('.bulk-form .bulk-foot .button.primary').click()


def change_row(page, what, how=None):
    """The last change row of the universal form: choose what to change (and how)."""
    row = panel(page).locator('.bulk-changes .cond-row').last
    choose(row.get_by_label('What to change', exact=True), what)
    if how:
        row.get_by_label('How', exact=True).select_option(label=how)
    return row


def current_only(page):
    """A child list opens on its collection's first statuses (Current and Future): narrow it to Current, and wait for
    the server's narrowed rows to be drawn. (Drawing them resets the select-all box: a tick before then is lost.)"""
    def narrowed(r):
        body = r.request.post_data_json if '/api/bulk/rows' in r.url and r.request.method == 'POST' else None
        return bool(body) and '"op": "none"' in json.dumps(body.get('selection', {}).get('where', {}).get('conditions'))
    with page.expect_response(narrowed) as reply:
        condition(toolbar(page.locator('#main'), 'Filter'), 'Status', ['Future'], op='none')
    close_popover(page)
    # Every row drawn is one the server sent (the list draws a page of them), and some are drawn when it sent any.
    ids = [str(r['id']) for r in reply.value.json()['rows']]
    page.wait_for_function("""ids => {const shown = [...document.querySelectorAll('#main tbody tr[data-row]')].map(t => t.dataset.row);
        return shown.every(id => ids.includes(id)) && (shown.length > 0) === (ids.length > 0);}""", arg=ids)


def settled(page, left=0):
    """The bulk requests the page is waiting on have been answered and drawn, all but `left` of them (data-bulk-busy,
    bulk.ts bulkWork)."""
    page.wait_for_function("left => Number(document.documentElement.getAttribute('data-bulk-busy') || 0) <= left", arg=left)


def errors_of(page):
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    return errors


def eligibilities(h, person=None):
    rows = h.get('admin', '/api/catalogues')['eligibilities']
    return [r for r in rows if person is None or r['person_id'] == person]


@seeded
def permission_overlap(h):
    alex, clinic = h.seed['people'][0], h.seed['services'][0]
    # Every permission ends on 1 Nov; Alex has a later one at the first clinic from 1 Dec.
    for r in eligibilities(h):
        ended = h.command('credentialer', 'eligibility.end', {'ends_on': '2026-11-01', 'reason': 'Test setup'}, r['id'], r['revision'])['record']
        if r['person_id'] == alex and r['service_id'] == clinic:
            first = ended
    h.command('credentialer', 'eligibility.create', {'person_id': alex, 'service_id': clinic, 'starts_on': '2026-12-01', 'ends_on': None})
    before = {r['id']: r['ends_on'] for r in eligibilities(h)}
    return first, before


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_people_then_their_clinic_permissions_change_last_day_fix_apply_undo(browser, live_server, h, scheme):
    """Scenario A: people → Show their… clinic permissions → Change… last day → Preview shows the overlap with a later
    permission → its best fix → Apply → Results/toast → one Undo puts every last day back."""
    first, before = permission_overlap(h)
    context, page = sign_in(browser, live_server, 'credentialer', viewport=DESKTOP)
    errors = errors_of(page)
    forbidden = []
    page.on('response', lambda r: forbidden.append(r.url) if r.status == 403 else None)
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'people')
        page.get_by_role('checkbox', name='Select all people shown').check()
        bar(page).get_by_role('button', name='Show their…').click()
        menu = page.get_by_role('menu', name='Show their…')
        item = menu.get_by_role('menuitem', name=re.compile('^Clinic permissions'))
        expect(item).to_contain_text(re.compile(r'\d'))  # its count
        # Only the child types a credentialer may read are offered (no Leave, no Scheduling facts), each with its count.
        for each in menu.get_by_role('menuitem').all():
            expect(each).to_contain_text(re.compile(r'\d$'))
        expect(menu.get_by_role('menuitem', name=re.compile('^(Leave|Scheduling facts)'))).to_have_count(0)
        assert not forbidden, forbidden
        item.click()
        expect(page.locator('.bulk-crumb')).to_contain_text('People')
        expect(page.get_by_role('button', name=re.compile(r'^For 3 selected people'))).to_be_visible()
        current_only(page)
        page.get_by_role('checkbox', name=re.compile('^Select all .* shown')).check()
        bar(page).get_by_role('button', name='Change…').click()
        expect(panel(page)).to_be_visible()
        row = change_row(page, re.compile('Ends|Last day|Until'), 'Set to')
        # "Now": every selected permission has 31 Oct as its last day (stored 1 Nov; the list's rows, as read from the server).
        expect(row.locator('.now-values .now-chip')).to_have_text([re.compile(r'31 Oct 2026: 6$')])
        # The calendar button sits inside the date input's right edge.
        box, button = row.locator('.date-text').bounding_box(), row.locator('.date-open').bounding_box()
        assert box['x'] < button['x'] and button['x'] + button['width'] <= box['x'] + box['width'] + 1, (box, button)
        row.locator('.date-text').fill('31 Jan 2027')
        row.locator('.date-text').press('Tab')
        submit(page)
        expect(panel(page).get_by_role('heading', name='Preview — nothing saved yet')).to_be_visible()
        # The overlap with the later permission is a problem group with its best fix.
        group = panel(page).locator('.dt-group', has=page.locator('.bulk-group-fix'))
        expect(group).to_have_count(1)
        expect(group.get_by_role('button', name='Other fixes')).to_be_visible()  # the other fixes, in its menu
        group.locator('.bulk-split .button').first.click()
        expect(panel(page).locator('.badge', has_text='Fixed').first).to_be_visible()
        panel(page).get_by_role('button', name=re.compile(r'^Apply ready rows \(\d+\)')).click()
        toast = page.locator('.toast', has_text=re.compile('changed'))
        expect(toast).to_be_visible()
        after = {r['id']: r['ends_on'] for r in eligibilities(h)}
        assert after != before and after[first['id']] == '2027-02-01'  # last day 31 Jan included (stored as the day after)
        toast.get_by_role('button', name='Undo').click()
        expect(page.locator('.toast', has_text='Undone')).to_be_visible()
        assert {r['id']: r['ends_on'] for r in eligibilities(h)} == before
        # Back keeps the parent selection.
        page.locator('.bulk-crumb').get_by_role('button', name=re.compile('People')).click()
        expect(bar(page)).to_contain_text('3')
        assert not errors, errors
    finally:
        context.close()


def test_held_back_rows_open_results_and_resolve_reopens_just_them(browser, live_server, h):
    """Apply ready rows only: Results opens on its own (Changed / Refused …, counts add up), the held-back rows are kept
    to resolve and Resolve reopens Preview with just them."""
    taylor, clinic = h.seed['people'][2], h.seed['services'][0]
    r = next(x for x in eligibilities(h, taylor) if x['service_id'] == clinic)
    h.command('credentialer', 'eligibility.end', {'ends_on': '2026-11-01', 'reason': 'Test setup'}, r['id'], r['revision'])
    context, page = sign_in(browser, live_server, 'credentialer', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        navigate(page, 'people')
        page.get_by_role('checkbox', name='Select all people shown').check()
        bar(page).get_by_role('button', name='Show their…').click()
        page.get_by_role('menu', name='Show their…').get_by_role('menuitem', name=re.compile('^Clinic permissions')).click()
        page.get_by_role('checkbox', name=re.compile('^Select all .* shown')).check()
        bar(page).get_by_role('button', name='Change…').click()
        row = change_row(page, re.compile('Ends|Last day|Until'), 'Set to')
        row.locator('.date-text').fill('31 Jan 2027')
        row.locator('.date-text').press('Tab')
        submit(page)
        expect(panel(page).locator('.bulk-sum')).to_have_text(re.compile(r'^1 ready .*= 6$'))
        panel(page).get_by_role('button', name='Apply ready rows (1) → Results').click()
        expect(panel(page).get_by_role('heading', name='Results')).to_be_visible()
        tiles = panel(page).locator('.bulk-tiles')
        expect(tiles.locator('.bulk-tile', has_text='Changed')).to_contain_text('1')
        expect(tiles.locator('.bulk-tile', has_text='Skipped')).to_contain_text('5')  # kept to resolve
        # The shared Group control: Results by person.
        panel(page).get_by_role('button', name='Group', exact=True).click()
        page.get_by_role('dialog', name='Group').get_by_role('menuitemradio', name='Person', exact=True).click()
        expect(panel(page).locator('.dt-group')).to_have_count(3)
        expect(panel(page).locator('.dt-group').first).to_contain_text(re.compile('Alex Morgan|Jordan Lee|Taylor Noor'))
        panel(page).get_by_role('button', name='Resolve 5').click()
        expect(panel(page).get_by_role('heading', name='Preview — nothing saved yet')).to_be_visible()
        expect(panel(page).locator('.bulk-sum')).to_have_text(re.compile(r'= 5$'))
        assert not errors, errors
    finally:
        context.close()


def test_services_by_tag_condition_change_light_path_and_switch_off_armed(browser, live_server, h):
    """Scenario B: Select by condition (a tag) on Services → Change… description applies at once (clean and small: the
    light path) with Undo; Switch off goes through Preview where Apply is the armed same-button confirm."""
    services = h.seed['services']
    tag = h.configure('tags', {'name': 'walkin', 'labels': ['services']})['record']
    h.command('admin', 'tag.members', {'add': [{'kind': 'services', 'id': services[0]}], 'remove': [], 'reason': 'Test setup'}, tag['id'], tag['revision'])
    context, page = sign_in(browser, live_server, 'admin', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        navigate(page, 'configuration')
        page.get_by_role('button', name=re.compile(r'^Services')).click()
        caret(page.locator('#main')).get_by_role('menuitem', name='Select by condition…').click()
        pop = page.get_by_role('dialog', name='Select by condition')
        condition(pop, 'Clinic', ['#walkin'])
        expect(pop.locator('.bulk-condition-count')).to_have_text(re.compile(r'^1 service matches'))
        pop.get_by_role('button', name='Select', exact=True).click()
        expect(bar(page)).to_contain_text('1')
        bar(page).get_by_role('button', name='Change…').click()
        row = change_row(page, 'Description', 'Set to')
        row.locator('input').fill('Walk-in, no appointment')
        submit(page)
        toast = page.locator('.toast', has_text='changed')
        expect(toast).to_be_visible()
        expect(panel(page)).to_have_count(0)  # the light path: no panel left open
        assert next(s for s in h.get('admin', '/api/catalogues')['services'] if s['id'] == services[0])['description'] == 'Walk-in, no appointment'
        # The changed row is marked and one toggle away.
        expect(page.locator('tr.dt-changed')).to_have_count(1)
        expect(page.get_by_role('button', name=re.compile(r'^Show changed · 1'))).to_be_visible()
        # The list keeps "Undo last change" until the next change.
        page.locator('.app-footer').get_by_role('button', name='Undo last change').click()
        expect(page.locator('.toast', has_text='Undone')).to_be_visible()
        assert next(s for s in h.get('admin', '/api/catalogues')['services'] if s['id'] == services[0])['description'] != 'Walk-in, no appointment'
        expect(page.locator('tr.dt-changed')).to_have_count(0)
        # Switch off: Preview, then the armed confirm.
        page.get_by_role('checkbox', name='Select all services shown').check()
        bar(page).get_by_role('button', name='Switch off').click()
        apply = panel(page).locator('.bulk-foot .button.primary')
        expect(apply).to_have_text(re.compile(r'^Apply ready rows'))
        apply.click()
        expect(apply).to_have_accessible_name(re.compile(r'^Confirm switch off \d+\?'))
        apply.click()
        expect(page.locator('.toast', has_text='2 services changed')).to_be_visible()
        assert all(s['active'] is False for s in h.get('admin', '/api/catalogues')['services'])
        assert not errors, errors
    finally:
        context.close()


def test_imported_leave_records_retyped_together(browser, live_server, h):
    """Scenario D: 35 recorded leave records → Change… leave type → one change with one Undo (the private reasons stay)."""
    types = h.get('admin', '/api/catalogues')['leave_types']
    first, second = types[0], types[1]
    people = h.seed['people']
    for n in range(35):
        h.command('coordinator', 'availability.record', {'person_id': people[n % 3], 'kind': 'recorded_leave', 'leave_type_id': first['id'],
                  'start': f'2027-0{1 + n // 28}-{1 + n % 28:02d}T00:00:00+03:00', 'end': f'2027-0{1 + n // 28}-{1 + n % 28:02d}T23:00:00+03:00',
                  'private_reason': 'Imported record'})
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        page.get_by_role('checkbox', name=re.compile('^Select all requests shown')).check()
        bar(page).get_by_role('button', name=re.compile(r'^Change')).click()
        row = change_row(page, re.compile('Leave type', re.I), 'Set to')
        row.locator('.bulk-value select').select_option(label=second['name'])
        # The button says it applies (35 picked, under Most applied directly), and that problems open Preview first.
        expect(panel(page).locator('.bulk-form .bulk-foot .button.primary')).to_have_text('Apply 35 changes')
        expect(panel(page)).to_contain_text('Anything to resolve opens Preview first.')
        submit(page)
        expect(page.locator('.toast', has_text=re.compile('35 leave records changed'))).to_be_visible()
        records = [r for r in h.get('coordinator', '/api/requests/all?limit=200')['items'] if r.get('source') == 'availability']
        assert sum(1 for r in records if r.get('leave_type_id') == second['id']) >= 35
        # DEV acceptance B2: past the toast, the list keeps the change's Undo and its changed rows (rc.16).
        expect(page.get_by_role('button', name=re.compile(r'^Show changed · 35'))).to_be_visible()
        page.get_by_role('button', name='Undo last change').click()
        expect(page.locator('.toast', has_text='Undone')).to_be_visible()
        records = [r for r in h.get('coordinator', '/api/requests/all?limit=200')['items'] if r.get('source') == 'availability']
        assert not any(r.get('leave_type_id') == second['id'] for r in records)
        assert not errors, errors
    finally:
        context.close()


def test_people_with_a_tag_set_a_rule_number_is_proposed(browser, live_server, h):
    """Scenario C: people sharing a tag → ⋯ Set a rule number… → one line for the tag → Preview → Propose → Results
    "Proposed · waiting for approval"; nothing applies until another person approves it."""
    people = h.get('admin', '/api/catalogues')['people']
    for p in people[:2]:
        h.command('admin', 'catalogue.update', {'catalogue': 'people', 'value': {**{k: p[k] for k in ('display_name', 'active')}, 'tags': ['student']}, 'reason': 'Test setup'}, p['id'], p['revision'])
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        navigate(page, 'people')
        for name in (people[0]['display_name'], people[1]['display_name']):
            page.locator('#main tr', has_text=name).locator('.bulk-check').check()
        bar(page).get_by_role('button', name='More actions').click()
        page.get_by_role('menu', name='More actions').get_by_role('menuitem', name='Set a rule number…').click()
        dialog = page.get_by_role('dialog', name=re.compile('^Set a rule number'))
        expect(dialog.get_by_label('Lines', exact=True).locator('option').first).to_have_text('One line for #student')
        dialog.get_by_label('Rule', exact=True).select_option(label='Days in a row')
        dialog.get_by_label('Maximum', exact=True).fill('3')
        dialog.get_by_label('Reason', exact=True).fill('Students: at most 3')
        dialog.get_by_role('button', name='Preview', exact=True).click()
        expect(panel(page).get_by_role('heading', name='Preview — nothing saved yet')).to_be_visible()
        panel(page).locator('.bulk-foot .button.primary').click()
        expect(panel(page).get_by_role('heading', name='Proposed · waiting for approval')).to_be_visible()
        assert not errors, errors
    finally:
        context.close()


def test_their_leave_pages_on_the_server_and_selects_all_matching_by_count(browser, live_server, h):
    """Show their… child lists load 200 rows at a time (Show N more); "Select all N matching" asks the resolver
    (Counting…) and the change applies to every matching record, not only the rows loaded."""
    alex = h.seed['people'][0]
    kind = h.get('admin', '/api/catalogues')['leave_types'][0]['id']
    days = [f'2027-{1 + n // 28:02d}-{1 + n % 28:02d}' for n in range(210)]
    h.command('coordinator', 'bulk.apply', {'item_kind': 'availability.record', 'reason': 'Test setup',
              'common': {'person_id': alex, 'kind': 'recorded_leave', 'leave_type_id': kind, 'private_reason': 'Paged record'},
              'items': [{'payload': {'start': f'{d}T08:00:00+03:00', 'end': f'{d}T09:00:00+03:00'}} for d in days]})
    other = h.get('admin', '/api/catalogues')['leave_types'][1]
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        navigate(page, 'people')
        page.locator('#main tr', has_text='Alex Morgan').locator('.bulk-check').check()
        bar(page).get_by_role('button', name='Show their…').click()
        page.get_by_role('menu', name='Show their…').get_by_role('menuitem', name=re.compile('^Leave')).click()
        pop = toolbar(page.locator('#main'), 'Filter')
        pop.get_by_role('button', name='Clear all filters', exact=True).click()
        close_popover(page)
        more = page.locator('.bulk-more')
        expect(more).to_contain_text('Showing 200 of 210')
        page.get_by_role('checkbox', name=re.compile('^Select all .* shown')).check()
        banner = page.locator('.dt-select-banner')
        banner.get_by_role('button', name='Select all 210 matching').click()
        expect(bar(page)).to_contain_text('210')
        bar(page).get_by_role('button', name='Change…').click()
        row = change_row(page, re.compile('Leave type', re.I), 'Set to')
        row.locator('.bulk-value select').select_option(label=other['name'])
        submit(page)
        # The saved change re-reads the list from its first page (bulk_children.ts load): Show more waits for that read,
        # else a click on the old button is dropped when the re-read lands and the button comes back.
        reread = lambda r: '/api/bulk/rows' in r.url and r.request.method == 'POST' and (r.request.post_data_json or {}).get('offset') == 0
        with page.expect_response(reread):
            panel(page).get_by_role('button', name='Apply ready rows (210) → Results').click()
        settled(page)
        expect(page.locator('.toast', has_text=re.compile(r'^210 .* changed'))).to_be_visible()
        expect(more).to_contain_text('Showing 200 of 210')
        with page.expect_response(lambda r: '/api/bulk/rows' in r.url and r.request.method == 'POST'
                                  and (r.request.post_data_json or {}).get('offset') == 200) as appended:
            more.get_by_role('button', name='Show 10 more').click()
        assert appended.value.ok, appended.value.text()
        settled(page)
        expect(page.locator('#main tbody tr[data-row]')).to_have_count(210)
        expect(more).to_be_empty()
        # Someone else's change refreshes the app: the list re-reads in place every row asked for, not a fresh first
        # page that brings Show more back (a new list instance used to start again at 200).
        with page.expect_response(reread) as again:
            h.command('coordinator', 'availability.record', {'person_id': alex, 'kind': 'recorded_leave', 'leave_type_id': kind,
                      'private_reason': 'Paged record', 'start': '2027-12-01T08:00:00+03:00', 'end': '2027-12-01T09:00:00+03:00'})
            page.clock.run_for(61_000)
        assert again.value.json()['count'] == 211 and len(again.value.json()['rows']) == 211
        expect(more).to_be_empty()
        # A refresh that finds a popover open is skipped and owed; once paid (in place), later pulses read nothing again.
        with holding(page) as held:
            held.hold('**/api/leave-types*')
            with page.expect_request('**/api/leave-types*'):
                h.command('coordinator', 'availability.record', {'person_id': alex, 'kind': 'recorded_leave', 'leave_type_id': kind,
                          'private_reason': 'Paged record', 'start': '2027-12-02T08:00:00+03:00', 'end': '2027-12-02T09:00:00+03:00'})
                page.clock.run_for(61_000)
            toolbar(page.locator('#main'), 'Filter')
            # The sidebar counts are read in the same step as the open-popover check (refreshOnce): once that request
            # is out, the refresh has been skipped.
            with page.expect_request(lambda r: '/api/workspaces?limit=50' in r.url):
                held.release('**/api/leave-types*')
        close_popover(page)
        with page.expect_response(reread):  # the owed redraw, paid by the next tick
            page.clock.run_for(10_000)
        reads = []
        page.on('request', lambda r: reads.append(r.url) if '/api/me' in r.url or '/api/bulk/rows' in r.url else None)
        for _ in range(2):
            with page.expect_response('**/api/pulse'):
                page.clock.run_for(61_000)
        assert reads == [], reads
        assert not errors, errors
    finally:
        context.close()


def test_their_leave_keeps_rows_shown_with_show_more_after_a_save(browser, live_server, h):
    """rc.16: "Show 10 more" is out when a save reloads the child list: the reload reads every row asked for (not the
    first 200 again), and the late page is dropped, so all 210 stay and nothing is left to show."""
    alex = h.seed['people'][0]
    kinds = h.get('admin', '/api/catalogues')['leave_types']
    days = [f'2027-{1 + n // 28:02d}-{1 + n % 28:02d}' for n in range(210)]
    h.command('coordinator', 'bulk.apply', {'item_kind': 'availability.record', 'reason': 'Test setup',
              'common': {'person_id': alex, 'kind': 'recorded_leave', 'leave_type_id': kinds[0]['id'], 'private_reason': 'Paged record'},
              'items': [{'payload': {'start': f'{d}T08:00:00+03:00', 'end': f'{d}T09:00:00+03:00'}} for d in days]})
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    held = []
    try:
        navigate(page, 'people')
        page.locator('#main tr', has_text='Alex Morgan').locator('.bulk-check').check()
        bar(page).get_by_role('button', name='Show their…').click()
        page.get_by_role('menu', name='Show their…').get_by_role('menuitem', name=re.compile('^Leave')).click()
        pop = toolbar(page.locator('#main'), 'Filter')
        pop.get_by_role('button', name='Clear all filters', exact=True).click()
        close_popover(page)
        more = page.locator('.bulk-more')
        expect(more).to_contain_text('Showing 200 of 210')
        page.route('**/api/bulk/rows', lambda route: held.append(route) if json.loads(route.request.post_data or '{}').get('offset') == 200 else route.continue_())
        with page.expect_request(lambda r: r.url.endswith('/api/bulk/rows') and json.loads(r.post_data or '{}').get('offset') == 200):
            more.get_by_role('button', name='Show 10 more').click()
        # A save meanwhile (one row retyped, the light path) reloads the list.
        page.locator('#main tbody tr').first.locator('.bulk-check').check()
        bar(page).get_by_role('button', name='Change…').click()
        row = change_row(page, re.compile('Leave type', re.I), 'Set to')
        row.locator('.bulk-value select').select_option(label=kinds[1]['name'])
        submit(page)
        expect(page.locator('.toast', has_text='changed')).to_be_visible()
        expect(more).to_be_empty()
        for route in held:
            route.continue_()
        page.unroute('**/api/bulk/rows')
        expect(more).to_be_empty()  # all 210 still shown (a reload of the first 200 would offer "Show 10 more" again)
        expect(page.locator('#main tbody tr[data-row]')).to_have_count(210)
    finally:
        context.close()


def test_stale_preview_asks_to_preview_again(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin', viewport=DESKTOP)
    try:
        navigate(page, 'configuration')
        page.get_by_role('button', name=re.compile(r'^Services')).click()
        page.get_by_role('checkbox', name='Select all services shown').check()
        bar(page).get_by_role('button', name='Switch off').click()
        expect(panel(page).get_by_role('heading', name='Preview — nothing saved yet')).to_be_visible()
        # Someone else changes one of them meanwhile.
        s = next(x for x in h.get('admin', '/api/catalogues')['services'] if x['id'] == h.seed['services'][0])
        h.command('admin', 'catalogue.update', {'catalogue': 'services', 'value': {**{k: s[k] for k in ('name', 'starts_on', 'ends_on', 'active')}, 'description': 'Changed elsewhere'},
                                               'reason': 'Concurrent edit'}, s['id'], s['revision'])
        apply = panel(page).locator('.bulk-foot .button.primary')
        apply.click(); apply.click()
        expect(panel(page).get_by_text('Things changed since the preview.')).to_be_visible()
        panel(page).get_by_role('button', name='Preview again').click()
        expect(panel(page).get_by_text('Things changed since the preview.')).to_have_count(0)
        assert all(x['active'] is not False for x in h.get('admin', '/api/catalogues')['services'])
    finally:
        context.close()


def test_paste_names_lists_unmatched_and_ambiguous(browser, live_server, h):
    h.configure('people', {'display_name': 'Taylor Noor', 'active': True})  # a second Taylor Noor: ambiguous
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        navigate(page, 'people')
        caret(page.locator('#main')).get_by_role('menuitem', name='Paste names or codes…').click()
        dialog = page.get_by_role('dialog', name='Paste names or codes')
        dialog.get_by_label('Names or codes', exact=True).fill('Alex Morgan\nTaylor Noor\nNobody Here')
        result = dialog.locator('.bulk-paste-result')
        expect(result).to_contain_text('1 matched')
        expect(result).to_contain_text('1 matches more than one')
        expect(result).to_contain_text('1 not found')
        expect(result.locator('.bulk-paste-none')).to_contain_text('Nobody Here')
        result.get_by_label('Taylor Noor', exact=True).select_option(index=1)
        dialog.get_by_role('button', name='Select 2', exact=True).click()
        expect(bar(page)).to_contain_text('2')
        # Nothing found: Select stays off and says why.
        caret(page.locator('#main')).get_by_role('menuitem', name='Paste names or codes…').click()
        dialog = page.get_by_role('dialog', name='Paste names or codes')
        dialog.get_by_label('Names or codes', exact=True).fill('Nobody Here')
        expect(dialog.get_by_text(re.compile('^Nothing matched'))).to_be_visible()
        expect(dialog.get_by_role('button', name='Select', exact=True)).to_be_disabled()
    finally:
        context.close()


def test_select_by_condition_with_a_group_and_as_of(browser, live_server, h):
    people = h.get('admin', '/api/catalogues')['people']
    for p in people[:2]:
        h.command('admin', 'catalogue.update', {'catalogue': 'people', 'value': {**{k: p[k] for k in ('display_name', 'active')}, 'tags': ['student']}, 'reason': 'Test setup'}, p['id'], p['revision'])
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        navigate(page, 'people')
        # As of: one row at the top of Filter; shown in the label only when not today; × goes back to today.
        pop = toolbar(page.locator('#main'), 'Filter')
        expect(pop.locator('.dt-as-of')).to_be_visible()
        close_popover(page)
        # WCAG 2.5.8: the caret is a 24×24 target, clear of the select-all box.
        box, check = page.locator('#main .dt-check-menu').bounding_box(), page.locator('#main th .bulk-check').bounding_box()
        assert box['width'] >= 24 and box['height'] >= 24 and box['x'] - (check['x'] + check['width'] / 2) >= 12, (box, check)
        caret(page.locator('#main')).get_by_role('menuitem', name='Select by condition…').click()
        pop = page.get_by_role('dialog', name='Select by condition')
        condition(pop, 'Tag', ['#student'])
        expect(pop.locator('.bulk-condition-count')).to_have_text(re.compile(r'^2 people match'))
        add_filter(pop, group=True)
        condition(pop, 'Person', [people[2]['display_name']])
        pop.locator('.cond-row').nth(1).get_by_label('Joiner', exact=True).select_option(label='Or')
        expect(pop.locator('.bulk-condition-count')).to_have_text(re.compile(r'^3 people match'))
        pop.get_by_role('button', name='Select', exact=True).click()
        expect(bar(page)).to_contain_text('3')
        # The Gmail banner and Select none.
        caret(page.locator('#main')).get_by_role('menuitem', name='Select none').click()
        expect(bar(page)).to_be_hidden()
    finally:
        context.close()


def test_auditor_reads_lists_without_checkboxes(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'auditor', viewport=DESKTOP)
    try:
        navigate(page, 'people')
        expect(page.locator('#main .dt-table')).to_be_visible()
        expect(page.locator('#main .bulk-check')).to_have_count(0)
        expect(page.get_by_role('button', name='Selection options')).to_have_count(0)
    finally:
        context.close()


def test_phone_bulk_decline_is_a_bottom_sheet_with_reason_and_armed_confirm(browser, live_server, h):
    from .test_browser_requests import leave, day
    for n in (3, 4):
        leave(h, on=day(n))
    context, page = sign_in(browser, live_server, 'approver', viewport=PHONE)
    try:
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        for box in page.locator('.bulk-check:not(th .bulk-check)').all()[:2]:
            box.check()
        bar(page).get_by_role('button', name=re.compile(r'^Decline 2')).click()
        sheet = page.locator('dialog.bulk-sheet')
        expect(sheet).to_be_visible()
        confirm = sheet.locator('.form-footer .button').last
        expect(confirm).to_have_text('Decline 2')
        sheet.get_by_label('Reason', exact=True).fill('Clinic fully booked')
        confirm.click()
        expect(confirm).to_have_accessible_name(re.compile('^Confirm decline 2'))
        confirm.click()
        expect(page.locator('.toast', has_text='2 requests declined')).to_be_visible()
    finally:
        context.close()


def test_phone_bulk_decline_cancel_asks_before_dropping_a_typed_reason(browser, live_server, h):
    """#467 SHOULD 11: Cancel on the phone Decline sheet goes through the dirty-close guard (Keep editing keeps the reason)."""
    from .test_browser_requests import leave, day
    for n in (3, 4):
        leave(h, on=day(n))
    context, page = sign_in(browser, live_server, 'approver', viewport=PHONE)
    try:
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        for box in page.locator('.bulk-check:not(th .bulk-check)').all()[:2]:
            box.check()
        bar(page).get_by_role('button', name=re.compile(r'^Decline 2')).click()
        sheet = page.locator('dialog.bulk-sheet')
        reason = sheet.get_by_label('Reason', exact=True)
        reason.fill('Clinic fully booked')
        sheet.get_by_role('button', name='Cancel', exact=True).click()
        guard = sheet.locator('.unsaved-bar')
        expect(guard).to_contain_text('Not saved. Close anyway?')
        guard.get_by_role('button', name='Keep editing', exact=True).click()
        expect(reason).to_have_value('Clinic fully booked')
        sheet.get_by_role('button', name='Cancel', exact=True).click()
        guard.get_by_role('button', name='Discard changes', exact=True).click()
        expect(sheet).to_have_count(0)
    finally:
        context.close()


def open_their_permissions(page):
    """People › all selected › Show their… › Clinic permissions."""
    navigate(page, 'people')
    page.get_by_role('checkbox', name='Select all people shown').check()
    bar(page).get_by_role('button', name='Show their…').click()
    show_their_permissions(page)


def show_their_permissions(page):
    """Show their… › Clinic permissions, until the child list is on screen: its parent chip ("For 1 selected person",
    "For 3 selected people") comes with the table built from its first read. Until then #main still holds the People
    list, whose rows name clinics too."""
    page.get_by_role('menu', name='Show their…').get_by_role('menuitem', name=re.compile('^Clinic permissions')).click()
    expect(page.get_by_role('button', name=re.compile(r'^For \d+ selected (person|people)'))).to_be_visible()


def test_apply_disarms_after_back_results_close_without_unsaved_and_child_list_survives_reload(browser, live_server, h):
    """End arms Apply; Back and another change disarms it. A committed change leaves nothing "Not saved" (Esc on
    Results closes at once). The child list's parent set is in the address, so it survives a reload."""
    taylor, clinic = h.seed['people'][2], h.seed['services'][0]
    r = next(x for x in eligibilities(h, taylor) if x['service_id'] == clinic)
    h.command('credentialer', 'eligibility.end', {'ends_on': '2026-11-01', 'reason': 'Test setup'}, r['id'], r['revision'])
    context, page = sign_in(browser, live_server, 'credentialer', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        open_their_permissions(page)
        page.reload()
        expect(page.get_by_role('button', name=re.compile(r'^For 3 selected people'))).to_be_visible()
        expect(page.locator('#main tbody tr[data-row]')).to_have_count(6)
        # Status is the server's (Current at the list's day), not worked out in the browser.
        expect(page.locator('#main tbody td[data-col=status]')).to_have_text(['Current'] * 6)
        page.get_by_role('checkbox', name=re.compile('^Select all .* shown')).check()
        bar(page).get_by_role('button', name='Change…').click()
        end = change_row(page, 'End')
        if end.locator('.date-text').count():
            end.locator('.date-text').fill('1 Dec 2026')
            end.locator('.date-text').press('Tab')
        panel(page).get_by_label('Reason', exact=True).fill('Contracts end')
        submit(page)
        apply = panel(page).locator('.bulk-foot .button.primary')
        apply.click()
        expect(apply).to_have_accessible_name(re.compile('^Confirm end'))
        page.keyboard.press('Escape')
        panel(page).get_by_role('button', name='← Back').click()
        row = change_row(page, re.compile('Ends|Last day|Until'), 'Set to')
        row.locator('.date-text').fill('31 Jan 2027')
        row.locator('.date-text').press('Tab')
        submit(page)
        # Five open-ended permissions would shorten (kept to resolve), Taylor's extends: Apply is no longer armed.
        apply = panel(page).locator('.bulk-foot .button.primary')
        expect(apply).to_have_text(re.compile(r'^Apply ready rows'))
        apply.click()
        expect(panel(page).get_by_role('heading', name='Results')).to_be_visible()
        page.keyboard.press('Escape')
        expect(panel(page)).to_have_count(0)
        expect(page.locator('.unsaved-bar')).to_have_count(0)
        assert not errors, errors
    finally:
        context.close()


def test_preview_failure_keeps_the_form_with_try_again(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'credentialer', viewport=DESKTOP)
    try:
        open_their_permissions(page)
        page.get_by_role('checkbox', name=re.compile('^Select all .* shown')).check()
        bar(page).get_by_role('button', name='Change…').click()
        row = change_row(page, re.compile('Ends|Last day|Until'), 'Set to')
        row.locator('.date-text').fill('31 Jan 2027')
        row.locator('.date-text').press('Tab')
        page.route('**/api/commands/preview', lambda route: route.abort())
        submit(page)
        expect(panel(page).locator('.notice.error')).to_be_visible()
        expect(row.locator('.date-text')).not_to_have_value('')  # the form keeps its values
        page.unroute('**/api/commands/preview')
        panel(page).get_by_role('button', name='Try again').click()
        expect(panel(page).get_by_role('heading', name='Preview — nothing saved yet')).to_be_visible()
    finally:
        context.close()


def test_child_list_empty_and_filtered_empty(browser, live_server, h):
    newcomer = h.configure('people', {'display_name': 'Newcomer Without Clinics', 'active': True})['record']
    context, page = sign_in(browser, live_server, 'credentialer', viewport=DESKTOP)
    try:
        navigate(page, 'people')
        page.locator('#main tr', has_text='Newcomer Without Clinics').locator('.bulk-check').check()
        bar(page).get_by_role('button', name='Show their…').click()
        page.get_by_role('menu', name='Show their…').get_by_role('menuitem', name=re.compile('^Clinic permissions')).click()
        expect(page.get_by_role('heading', name=re.compile(r'^None of the 1 person has clinic permissions', re.I))).to_be_visible()
        assert newcomer['id']
        open_their_permissions(page)
        page.locator('#main input.dt-search').fill('nothing matches this')
        expect(page.get_by_role('heading', name=re.compile('^No matching'))).to_be_visible()
        # Review (5th) #12: a condition that matches nothing keeps the list's toolbar (to change or clear it).
        page.locator('#main input.dt-search').fill('')
        main = page.locator('#main')
        pop = toolbar(main, 'Filter')
        pop.get_by_role('button', name='Clear all filters', exact=True).click()
        # The list reads its rows again with the condition (the server's answer, not only the rows drawn before it).
        with page.expect_response(lambda r: '/api/bulk/rows' in r.url and '"conditions"' in (r.request.post_data or '')):
            condition(pop, 'Status', ['Current', 'Future', 'Ended'], op='none')
        close_popover(page)
        page.evaluate('() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))')
        expect(page.get_by_role('heading', name=re.compile('^No matching'))).to_be_visible()
        expect(main.get_by_role('button', name=re.compile('^Filter'))).to_be_visible()
        expect(main.get_by_role('searchbox', name=re.compile('^Search'))).to_be_visible()
        expect(page.get_by_role('heading', name=re.compile('^None of the'))).to_have_count(0)
    finally:
        context.close()


def test_programme_lead_bulk_editor_offers_only_types_it_can_read(browser, live_server, h):
    from .governance_support import identity
    identity(h, 'programme_lead', ['programme_lead'])
    # Every record type is opened in turn, about 30 reads in a minute: at the department's default of 30 bulk reads a
    # minute (bulk_settings rate_bulk_preview_per_minute) the last one is refused 429. The limit has its own tests.
    from apps.api import models as m
    from apps.api.db import transaction
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed['department_id'])
        department.bulk_settings = {**(department.bulk_settings or {}), 'rate_bulk_preview_per_minute': 1000}
    context, page = sign_in(browser, live_server, 'programme_lead', viewport=DESKTOP)
    errors, refused = errors_of(page), []
    page.on('response', lambda r: refused.append(r.url) if '/api/bulk/rows' in r.url and r.status == 403 else None)
    try:
        navigate(page, 'system/bulk')
        types = page.locator('#main').get_by_role('button', name=re.compile('^Record type'))
        expect(types).to_be_visible()
        types.click()
        expect(page.get_by_role('listbox', name='Record types').get_by_role('option').first).to_be_visible()
        total = page.get_by_role('listbox', name='Record types').get_by_role('option').count()
        page.keyboard.press('Escape')
        for i in range(total):
            page.locator('#main').get_by_role('button', name=re.compile('^Record type')).click()
            page.get_by_role('listbox', name='Record types').get_by_role('option').nth(i).click()
            expect(page.locator('#main .dt, #main .notice.error').first).to_be_visible()
            expect(page.locator('#main .notice.error')).to_have_count(0)
        assert not refused, refused
        assert not errors, errors
    finally:
        context.close()


def people_sheet(page):
    """People › View settings › Sheet: the ordinary list drawn as the shared Sheet."""
    navigate(page, 'people')
    main = page.locator('#main')
    toolbar(main, 'View settings').get_by_role('radio', name='Sheet', exact=True).click()
    close_popover(page)
    grid = main.get_by_role('grid', name='People sheet')
    expect(grid).to_be_visible()
    return main, grid


@pytest.mark.parametrize('role', ['auditor', 'credentialer'])
def test_people_sheet_opened_directly_reads_only_for_a_role_that_may_not_change_people(browser, live_server, h, role):
    """Review #6: the Sheet reads roles live, so a role that may not change people opens People › Sheet without
    visiting the Bulk editor first and reads it only (no crash on a context made before sign-in finished)."""
    context, page = sign_in(browser, live_server, role, viewport=DESKTOP)
    errors = errors_of(page)
    try:
        main, grid = people_sheet(page)
        expect(grid).to_have_attribute('aria-readonly', 'true')
        expect(grid.locator('tbody tr').filter(has_text='Alex Morgan')).to_have_count(1)
        assert not errors, errors
    finally:
        context.close()


def test_people_and_its_sheet_make_no_burst_of_requests(browser, live_server, h):
    """Opening People and its Sheet reads the page's data once, not once per person (no N+1 burst of API requests)."""
    for n in range(12):
        h.configure('people', {'display_name': f'Burst Person {n:02d}', 'active': True})
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    calls = []
    try:
        page.wait_for_load_state('networkidle')
        page.on('request', lambda r: calls.append(r.url) if '/api/' in r.url else None)
        navigate(page, 'people')
        expect(page.locator('#main tr', has_text='Burst Person 11')).to_have_count(1)
        page.wait_for_load_state('networkidle')
        opened = len(calls)
        # The page's own reads (catalogues, directory, badges, counts): a handful, independent of how many people.
        assert opened <= 20, calls
        main = page.locator('#main')
        toolbar(main, 'View settings').get_by_role('radio', name='Sheet', exact=True).click()
        close_popover(page)
        expect(main.get_by_role('grid', name='People sheet')).to_be_visible()
        page.wait_for_load_state('networkidle')
        assert len(calls) - opened <= 5, calls[opened:]
    finally:
        context.close()


def test_saved_rule_lines_change_proposes_a_version_for_independent_approval(browser, live_server, h):
    """Review #5: Propose a change… on saved rule lines offers the lines' fields; Preview → Propose leaves one new
    version waiting for another person's approval, and the rule in force is unchanged until then."""
    from .test_browser_rule_catalogue import catalogue_rule, flag, line, selection
    flag(h)
    rule = catalogue_rule(h, 'duties_per_month', [line({'max': {'value': 5, 'level': 'mandatory'}}, note='Everyone')])
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        navigate(page, 'rules/duties_per_month')
        main = page.locator('#main')
        main.locator('tr[data-row]').first.locator('input.bulk-check').check()
        selection(page).get_by_role('button', name='Propose a change…').click()
        what = panel(page).get_by_label('What to change', exact=True)
        words = what.evaluate("s=>[...s.options].map(o=>o.textContent)")
        assert 'Note' in words and 'Numbers' in words, words
        assert 'Settings' not in words, words  # review #11: a free-form object has no input in Change… (its own panel edits it)
        row = change_row(page, 'Note', 'Set to')
        row.locator('.bulk-value input').fill('Changed in bulk')
        submit(page)
        expect(panel(page).get_by_role('heading', name='Preview — nothing saved yet')).to_be_visible()
        panel(page).locator('.bulk-foot .button.primary').click()
        expect(panel(page).get_by_role('heading', name='Proposed · waiting for approval')).to_be_visible()
        policies = h.get('admin', '/api/catalogues')['policies']
        waiting = [p for p in policies if p.get('status') == 'proposed']
        assert len(waiting) == 1, [p.get('status') for p in policies]
        assert waiting[0]['definition']['rule']['lines'][0]['note'] == 'Changed in bulk'
        current = next(p for p in policies if p['id'] == rule['id'])
        assert current['status'] == 'active' and current['definition']['rule']['lines'][0]['note'] == 'Everyone'
        assert not errors, errors
    finally:
        context.close()


def their_leave(page):
    """People › Alex Morgan › Show their… › Leave, every status."""
    navigate(page, 'people')
    page.locator('#main tr', has_text='Alex Morgan').locator('.bulk-check').check()
    bar(page).get_by_role('button', name='Show their…').click()
    page.get_by_role('menu', name='Show their…').get_by_role('menuitem', name=re.compile('^Leave')).click()
    pop = toolbar(page.locator('#main'), 'Filter')
    pop.get_by_role('button', name='Clear all filters', exact=True).click()
    close_popover(page)


def leave_rows(h):
    return h.client('coordinator').post('/api/bulk/rows', json={'selection': {'collection': 'availability'}, 'limit': 500}).json()['rows']


def test_bulk_record_leave_keeps_whole_days_on_tab(browser, live_server, h):
    person_id = h.seed['people'][0]
    people = h.get('admin', '/api/catalogues?include=people')['people']
    person = next(row for row in people if row['id'] == person_id)
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        navigate(page, 'people')
        page.locator('#main tr', has_text=person['display_name']).locator('.bulk-check').check()
        bar(page).get_by_role('button', name='Change…').click()
        row = change_row(page, 'Record leave')
        start, end = (row.get_by_label(label, exact=True) for label in ('From', 'Until'))
        start.fill('31/02/2027')
        start.press('Tab')
        expect(start).to_have_value('31/02/2027')
        assert start.evaluate('(input) => input.validationMessage').startswith('Enter a date')
        for field in (start, end):
            field.fill('10/03/2027')
            field.press('Tab')
            expect(field).to_have_value('Wed 10 Mar 2027')
        expect(row.get_by_label('From time', exact=True)).to_have_attribute('placeholder', 'All day')
        row.get_by_label('Private note', exact=True).fill('Recorded for the whole day')
        submit(page)
        expect(page.locator('.toast').filter(has_text='1 person changed')).to_be_visible()
        made = [r for r in leave_rows(h) if r['person_id'] == person_id]
        assert len(made) == 1
        assert made[0]['start'].startswith('2027-03-09T21:00')
        assert made[0]['end'].startswith('2027-03-10T21:00')
    finally:
        context.close()


def test_leave_dates_and_record_leave_take_a_date_and_a_time(browser, live_server, h):
    """Review #7: date-and-time fields in Change… are the shared field (a date and its own time): Record leave and
    moving a leave's start both complete with the date and the time entered separately."""
    alex = h.seed['people'][0]
    kind = h.get('admin', '/api/catalogues')['leave_types'][0]
    h.command('coordinator', 'bulk.apply', {'item_kind': 'availability.record', 'reason': 'Test setup',
              'common': {'person_id': alex, 'kind': 'recorded_leave', 'leave_type_id': kind['id'], 'private_reason': 'Dated record'},
              'items': [{'payload': {'start': '2027-02-01T08:00:00+03:00', 'end': '2027-02-01T17:00:00+03:00'}}]})
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        navigate(page, 'people')
        page.locator('#main tr', has_text='Alex Morgan').locator('.bulk-check').check()
        bar(page).get_by_role('button', name='Change…').click()
        row = change_row(page, 'Record leave')
        row.get_by_label('From', exact=True).fill('10/03/2027')
        row.get_by_label('From time', exact=True).fill('09:00')
        row.get_by_label('Until', exact=True).fill('10/03/2027')
        row.get_by_label('Until time', exact=True).fill('13:30')
        row.get_by_label('Private note', exact=True).fill('Recorded in bulk')
        submit(page)  # clean and small: applied at once (the light path)
        expect(page.locator('.toast').filter(has_text='1 person changed')).to_be_visible()
        made = [r for r in leave_rows(h) if str(r.get('start', '')).startswith('2027-03-10')]
        assert len(made) == 1 and made[0]['start'].startswith('2027-03-10T06:00') and made[0]['end'].startswith('2027-03-10T10:30'), made  # +03:00
        # The leave list: move a record's start to another time of the same day.
        their_leave(page)
        page.locator('#main tr', has_text='1 Feb 2027').locator('.bulk-check').check()  # in the department's time
        bar(page).get_by_role('button', name='Change…').click()
        row = change_row(page, 'From', 'Set to')
        row.get_by_label('From', exact=True).fill('01/02/2027')
        row.get_by_label('From time', exact=True).fill('10:15')
        submit(page)
        expect(page.locator('.toast').filter(has_text='1 leave & availability changed')).to_be_visible()
        moved = [r for r in leave_rows(h) if str(r.get('start', '')).startswith('2027-02-01')]
        assert len(moved) == 1 and moved[0]['start'].startswith('2027-02-01T07:15'), moved
        assert not errors, errors
    finally:
        context.close()


def test_condition_and_paste_select_every_match_beyond_the_loaded_page(browser, live_server, h):
    """Review #12: on a child list paged at 200 rows, Select by condition and Paste names take all 210 matches (the
    server's selection), not only the rows loaded."""
    alex = h.seed['people'][0]
    kind = h.get('admin', '/api/catalogues')['leave_types'][0]['id']
    days = [f'2027-{1 + n // 28:02d}-{1 + n % 28:02d}' for n in range(210)]
    h.command('coordinator', 'bulk.apply', {'item_kind': 'availability.record', 'reason': 'Test setup',
              'common': {'person_id': alex, 'kind': 'recorded_leave', 'leave_type_id': kind, 'private_reason': 'Paged record'},
              'items': [{'payload': {'start': f'{d}T08:00:00+03:00', 'end': f'{d}T09:00:00+03:00'}} for d in days]})
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        their_leave(page)
        expect(page.locator('.bulk-more')).to_contain_text('Showing 200 of 210')
        main = page.locator('#main')
        caret(main).get_by_role('menuitem', name='Select by condition…').click()
        pop = page.get_by_role('dialog', name='Select by condition')
        condition(pop, 'Person', ['Alex Morgan'])
        expect(pop.locator('.bulk-condition-count')).to_contain_text('210')
        pop.get_by_role('button', name='Select', exact=True).click()
        expect(bar(page)).to_contain_text('210')
        caret(main).get_by_role('menuitem', name='Select none').click()
        expect(bar(page)).to_be_hidden()
        caret(main).get_by_role('menuitem', name='Paste names or codes…').click()
        dialog = page.get_by_role('dialog', name='Paste names or codes')
        dialog.get_by_label('Names or codes', exact=True).fill('Alex Morgan')
        expect(dialog.locator('.bulk-paste-result')).to_contain_text('210 matched')
        dialog.get_by_role('button', name='Select 210', exact=True).click()
        expect(bar(page)).to_contain_text('210')
        bar(page).get_by_role('button', name='Change…').click()
        row = change_row(page, re.compile('Leave type', re.I), 'Set to')
        row.locator('.bulk-value select').select_option(label=h.get('admin', '/api/catalogues')['leave_types'][1]['name'])
        submit(page)
        expect(panel(page).get_by_role('button', name='Apply ready rows (210) → Results')).to_be_visible()
        assert not errors, errors
    finally:
        context.close()


def test_a_server_selection_with_no_loaded_rows_still_offers_its_actions(browser, live_server, h):
    """Review #11: when every match of Select by condition lies beyond the 200 rows loaded, the bar and its actions
    still appear, from the server's selection and count."""
    from .test_browser import route_fetch
    alex, jordan = h.seed['people'][0], h.seed['people'][1]
    kind = h.get('admin', '/api/catalogues')['leave_types'][0]['id']
    def record(person, days):
        h.command('coordinator', 'bulk.apply', {'item_kind': 'availability.record', 'reason': 'Test setup',
                  'common': {'person_id': person, 'kind': 'recorded_leave', 'leave_type_id': kind, 'private_reason': 'Paged record'},
                  'items': [{'payload': {'start': f'{d}T08:00:00+03:00', 'end': f'{d}T09:00:00+03:00'}} for d in days]})
    record(alex, [f'2027-{1 + n // 28:02d}-{1 + n % 28:02d}' for n in range(210)])
    record(jordan, ['2028-01-03', '2028-01-04', '2028-01-05'])
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)

    # The first page the list loads leaves out Jordan's records (they are on a later page), whatever the server order.
    def first_page(route):
        body = route.request.post_data_json or {}
        response = route_fetch(route)
        data = response.json()
        if not body.get('offset') and body.get('limit') == 200:
            data['rows'] = [r for r in data['rows'] if r.get('person_id') != jordan][:197]
        route.fulfill(response=response, json=data)
    page.route('**/api/bulk/rows', first_page)
    try:
        navigate(page, 'people')
        for name in ('Alex Morgan', 'Jordan Lee'):
            page.locator('#main tr', has_text=name).locator('.bulk-check').check()
        bar(page).get_by_role('button', name='Show their…').click()
        page.get_by_role('menu', name='Show their…').get_by_role('menuitem', name=re.compile('^Leave')).click()
        pop = toolbar(page.locator('#main'), 'Filter')
        pop.get_by_role('button', name='Clear all filters', exact=True).click()
        close_popover(page)
        expect(page.locator('#main tr', has_text='Jordan Lee')).to_have_count(0)
        main = page.locator('#main')
        caret(main).get_by_role('menuitem', name='Select by condition…').click()
        pop = page.get_by_role('dialog', name='Select by condition')
        condition(pop, 'Person', ['Jordan Lee'])
        expect(pop.locator('.bulk-condition-count')).to_contain_text('3')
        pop.get_by_role('button', name='Select', exact=True).click()
        expect(bar(page)).to_contain_text('3')
        expect(bar(page).get_by_role('button', name='Change…')).to_be_visible()
        assert not errors, errors
    finally:
        context.close()


def test_a_saved_view_keeps_its_as_of_day_after_a_reload(browser, live_server, h):
    """Review #18: a saved view stores its As of day and brings it back when the list is opened again."""
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        navigate(page, 'people')
        main = page.locator('#main')
        pop = toolbar(main, 'Filter')
        day = pop.locator('.dt-as-of input')
        day.fill('01/09/2026')
        page.keyboard.press('Tab')
        close_popover(page)
        expect(main.get_by_role('button', name=re.compile('^Filter'))).to_contain_text('1')
        toolbar(main, 'Saved views').get_by_role('menuitem', name='New view…').click()
        dialog = page.get_by_role('dialog', name='New view', exact=True)
        dialog.get_by_label('View name', exact=True).fill('On 1 September')
        dialog.get_by_role('button', name='Save view', exact=True).click()
        expect(dialog).not_to_be_visible()
        saved = h.get('coordinator', '/api/table-views?table=people')['views']
        assert next(v for v in saved if v['name'] == 'On 1 September')['definition']['as_of'] == '2026-09-01'
        page.reload()
        navigate(page, 'people')
        # The saved view's As of is back (the Filter count) before the popover is opened.
        expect(page.locator('#main').get_by_role('button', name=re.compile('^Filter'))).to_contain_text('1')
        pop = toolbar(page.locator('#main'), 'Filter')
        expect(pop).to_contain_text(re.compile('1 Sep 2026'))
        expect(pop.locator('.dt-as-of input')).to_have_value(re.compile('1 Sep 2026'))
    finally:
        context.close()


def test_an_explicit_preset_clears_an_earlier_condition(browser, live_server, h):
    """Review #18: Home's "Person without a rank" opens People filtered to Active people without a rank; a "Status is
    not Active" condition left from an earlier visit no longer hides them."""
    h.configure('people', {'display_name': 'Newcomer Without Clinics', 'active': True})
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        navigate(page, 'people')
        main = page.locator('#main')
        pop = toolbar(main, 'Filter')
        condition(pop, 'Status', ['Active'], op='none')
        close_popover(page)
        expect(main.locator('tr', has_text='Newcomer Without Clinics')).to_have_count(0)
        navigate(page, 'home')
        page.locator('.home-needs-list li', has_text=re.compile('without a rank')).get_by_role('button', name='Show').click()
        expect(page.locator('#main tr', has_text='Newcomer Without Clinics')).to_have_count(1)
    finally:
        context.close()


def test_add_to_programme_sends_the_registry_action_and_its_inputs(browser, live_server, h):
    """Review #9: the universal form's Add to programme sends the registry action (catalogue.create) with only its
    inputs; the server completes the membership. Last day 31 Mar is stored as the day after (1 Apr)."""
    programme = h.configure('programmes', {'name': 'Bulk form programme', 'starts_on': '2026-01-01'})['record']
    alex = h.seed['people'][0]
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors, sent = errors_of(page), []
    page.on('request', lambda r: sent.append(r.post_data_json) if r.method == 'POST' and '/api/commands' in r.url and (r.post_data_json or {}).get('kind') == 'bulk.apply' else None)
    try:
        navigate(page, 'people')
        page.locator('#main tr', has_text='Alex Morgan').locator('.bulk-check').check()
        bar(page).get_by_role('button', name='Change…').click()
        row = change_row(page, 'Add to programme')
        row.get_by_label('Programme', exact=True).select_option(label='Bulk form programme')
        row.get_by_label('From', exact=True).fill('01/01/2027')
        row.get_by_label('Last day', exact=True).fill('31/03/2027')
        submit(page)
        expect(page.locator('.toast').filter(has_text='1 person changed')).to_be_visible()
        changes = sent[0]['payload']['changes']
        assert changes == [{'item_kind': 'catalogue.create', 'payload': {'programme_id': programme['id'], 'starts_on': '2027-01-01', 'ends_on': '2027-04-01'}}], changes
        made = [r for r in h.get('admin', '/api/catalogues')['programme_memberships'] if r['person_id'] == alex and r['programme_id'] == programme['id']]
        assert [(r['starts_on'], r['ends_on']) for r in made] == [('2027-01-01', '2027-04-01')], made
        assert not errors, errors
    finally:
        context.close()


def test_outbox_is_not_condition_finds_an_older_event_beyond_the_first_page(browser, live_server, h):
    """Review #10: an Or / is-not condition on a list paged on the server is evaluated there before paging: with the
    100 newest events queued, State is not queued shows the older failed one."""
    from datetime import timedelta
    from apps.api import models as m
    from apps.api.db import transaction
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Outbox(department_id=h.seed['department_id'], event_key='old-failed', kind='notification', payload={},
                        state='failed', created_at=m.now() - timedelta(days=2)))
        db.add_all(m.Outbox(department_id=h.seed['department_id'], event_key=f'new-queued-{n}', kind='notification',
                            payload={}, state='queued', created_at=m.now()) for n in range(100))
    context, page = sign_in(browser, live_server, 'operator', viewport=DESKTOP)
    try:
        navigate(page, 'system/operations')
        events = page.locator('#main .dt').filter(has=page.get_by_role('searchbox', name='Search events'))
        expect(events.locator('tbody tr').filter(has_text=re.compile('failed', re.I))).to_have_count(0)
        pop = toolbar(events, 'Filter')
        condition(pop, 'State', ['queued'], op='none')
        close_popover(page)
        expect(events.locator('tbody tr')).to_have_count(1)
        expect(events.locator('tbody tr').first).to_contain_text(re.compile('failed', re.I))
    finally:
        context.close()


def test_requests_is_not_condition_finds_an_older_decision_beyond_the_first_page(browser, live_server, h):
    """Review #10 (Requests): with 105 newer requests waiting, Status is not Waiting for approval is evaluated by the
    server before it pages (in its stage codes), so the older declined request is shown."""
    from apps.api.db import transaction
    from .test_requests_search import request_row
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all(request_row(h, h.seed['people'][0], 'Synthetic pending', day=n) for n in range(105))
        db.add(request_row(h, h.seed['people'][0], 'Synthetic decided', day=-30, status='rejected'))
    context, page = sign_in(browser, live_server, 'approver', viewport=DESKTOP)
    try:
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        main = page.locator('#main')
        expect(main.locator('tr.dt-open', has_text='Declined')).to_have_count(0)
        pop = toolbar(main, 'Filter')
        condition(pop, 'Status', ['Waiting for approval'], op='none')
        close_popover(page)
        expect(main.locator('tr.dt-open')).to_have_count(1)
        expect(main.locator('tr.dt-open').first).to_contain_text('Declined')
    finally:
        context.close()


def test_audit_filters_find_an_older_change_beyond_the_first_page(browser, live_server, h):
    """Review #10 (Audit): the Audit log's filters are the server's (kind of change, person, dates, search): with the
    100 newest changes to rotas, Kind of change = Configuration finds the older configuration change."""
    from datetime import timedelta
    from apps.api import models as m
    from apps.api.db import transaction
    with transaction(h.app.state.engine, write=True) as db:
        base = dict(department_id=h.seed['department_id'], actor_id=h.seed['identities']['coordinator'], reason='Synthetic history filter', detail={}, correlation_id='synthetic-audit')
        db.add(m.AuditEvent(**base, action='catalogue.create', created_at=m.now() - timedelta(days=2)))
        db.add_all(m.AuditEvent(**base, action='workspace.update', created_at=m.now()) for _ in range(100))
    context, page = sign_in(browser, live_server, 'operator', viewport=DESKTOP)
    try:
        navigate(page, 'system/audit')
        main = page.locator('#main')
        expect(main.get_by_role('button', name='Next page')).to_be_enabled()
        pop = toolbar(main, 'Filter')
        pop.get_by_label('Kind of change', exact=True).select_option(label='Configuration')
        close_popover(page)
        expect(main.locator('.audit-row')).to_have_count(1)
        expect(main.locator('.audit-row').first).to_contain_text('Synthetic history filter')
    finally:
        context.close()


def save_department_settings(page, reason):
    pane = page.locator('.config-pane')
    pane.get_by_role('region', name='Unsaved changes').get_by_role('button', name='Save settings', exact=True).click()
    dialog = page.get_by_role('dialog')
    dialog.get_by_label('Reason', exact=True).fill(reason)
    dialog.get_by_role('button', name='Save settings', exact=True).click()
    expect(dialog).to_be_hidden()


def test_department_settings_bulk_limits_save_reload_and_undo(browser, live_server, h):
    """Review #12: the four bulk limits are Department settings fields (labels and bounds from the contract); a change
    saves, survives a reload, and Undo brings back the previous effective value. Most applied directly can never be
    more than most items per change."""
    before = h.get('admin', '/api/bulk/settings')['settings']
    context, page = sign_in(browser, live_server, 'admin', viewport=DESKTOP)
    try:
        navigate(page, 'configuration/department')
        department_tab(page, 'Advanced')
        pane = page.locator('.config-pane')
        direct = pane.get_by_label('Most applied directly', exact=True)
        most = pane.get_by_label('Most items per change', exact=True)
        expect(direct).to_have_value(str(before['bulk_direct_max']))
        expect(most).to_have_attribute('max', '5000')
        expect(pane.get_by_label('Days staged changes are kept', exact=True)).to_have_value(str(before['bulk_staged_days']))
        expect(pane.get_by_label('Previews per minute', exact=True)).to_have_value(str(before['rate_bulk_preview_per_minute']))
        # Direct above the item limit is refused on the form, before anything is sent.
        most.fill('40')
        direct.fill('41')
        pane.get_by_role('region', name='Unsaved changes').get_by_role('button', name='Save settings', exact=True).click()
        expect(page.get_by_role('dialog')).to_have_count(0)
        assert not direct.evaluate('e => e.validity.valid')
        most.fill(str(before['bulk_max_items']))
        direct.fill('12')
        save_department_settings(page, 'Fewer changes applied without review')
        assert h.get('admin', '/api/bulk/settings')['settings']['bulk_direct_max'] == 12
        page.reload()
        department_tab(page, 'Advanced')
        direct = page.locator('.config-pane').get_by_label('Most applied directly', exact=True)
        expect(direct).to_have_value('12')
        direct.fill('9')
        save_department_settings(page, 'Fewer again')
        assert h.get('admin', '/api/bulk/settings')['settings']['bulk_direct_max'] == 9
        page.get_by_role('button', name='Undo', exact=True).click()
        expect(page.locator('.toast', has_text=re.compile('Undone|undone')).first).to_be_visible()
        assert h.get('admin', '/api/bulk/settings')['settings'] == {**before, 'bulk_direct_max': 12}
        page.reload()
        department_tab(page, 'Advanced')
        expect(page.locator('.config-pane').get_by_label('Most applied directly', exact=True)).to_have_value('12')
    finally:
        context.close()


def switch_account(page, username):
    """Sign out from the account menu and sign in as another account in the same tab."""
    from .conftest import TEST_PASSWORD
    page.get_by_role('button', name=re.compile(r'^Account menu, ')).click()
    page.get_by_role('menuitem', name='Sign out', exact=True).click()
    another = page.get_by_role('button', name='Use another account')
    if another.count():
        another.click()
    page.get_by_label('Username or email', exact=True).fill(username)
    page.get_by_role('button', name='Continue', exact=True).click()
    page.get_by_label('Password', exact=True).fill(TEST_PASSWORD)
    page.get_by_role('button', name='Sign in', exact=True).click()
    expect(page.get_by_role('button', name=re.compile(r'^Account menu, '))).to_be_visible()


def test_a_condition_does_not_filter_the_next_account(browser, live_server, h):
    """Review (4th) #4: account A's "Status is not Waiting for approval" on Requests is forgotten at sign-out; account B
    signing in in the same tab reads every request, with no filter it cannot see."""
    from apps.api.db import transaction
    from .test_requests_search import request_row
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all(request_row(h, h.seed['people'][0], 'Synthetic pending', day=n) for n in range(3))
        db.add(request_row(h, h.seed['people'][0], 'Synthetic decided', day=-30, status='rejected'))
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        main = page.locator('#main')
        pop = toolbar(main, 'Filter')
        condition(pop, 'Status', ['Waiting for approval'], op='none')
        close_popover(page)
        expect(main.locator('tr.dt-open')).to_have_count(1)
        # Listening from before the switch: B's list can be read while the sign-in is still settling (the address is
        # still #requests), so a listener attached after it can miss every read. B's reads follow B's sign-in.
        sent = []
        page.on('request', lambda r: sent.append(r.url))
        switch_account(page, 'approver')
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        main = page.locator('#main')
        expect(main.locator('tr.dt-open', has_text='Waiting for approval').first).to_be_visible()
        expect(main.locator('tr.dt-open')).to_have_count(4)
        signed_in = max(i for i, u in enumerate(sent) if u.endswith('/auth/login'))
        reads = [u for u in sent[signed_in:] if '/api/requests/all' in u]
        assert reads and not any('filter=' in u for u in reads), reads
    finally:
        context.close()


def held_rows(page, keep):
    """Hold /api/bulk/rows requests whose JSON body `keep` picks; the rest go through. Returns the held routes."""
    held = []

    def route(r):
        if keep(r.request.post_data_json or {}):
            held.append(r)
        else:
            r.continue_()
    page.route('**/api/bulk/rows', route)
    return held


def test_a_slower_earlier_search_never_overwrites_a_newer_one_in_a_child_list(browser, live_server, h):
    """Review (4th) #6: in Show their… Clinic permissions, a search whose answer arrives after a newer search's does
    not replace the newer rows."""
    from .test_browser_bulk_sheet import walk_in
    walk_in(h)
    context, page = sign_in(browser, live_server, 'credentialer', viewport=DESKTOP)
    try:
        open_their_permissions(page)
        main = page.locator('#main')
        held = held_rows(page, lambda b: ((b.get('selection') or {}).get('where') or {}).get('q') == 'Walk-in')
        search = main.locator('input.dt-search')
        with page.expect_request(lambda r: '/api/bulk/rows' in r.url and 'Walk-in' in (r.post_data or '')):
            search.fill('Walk-in')
        search.fill('Continuity')
        expect(main.locator('tbody tr', has_text='Continuity clinic').first).to_be_visible()
        expect(main.locator('tbody tr', has_text='Walk-in North')).to_have_count(0)
        with page.expect_response(lambda r: '/api/bulk/rows' in r.url and 'Walk-in' in (r.request.post_data or '')):
            for r in held:
                r.continue_()
        page.unroute('**/api/bulk/rows')
        page.evaluate('() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))')  # its rows drawn, if taken
        # The late Walk-in answer is dropped: the list still shows the Continuity search.
        expect(search).to_have_value('Continuity')
        expect(main.locator('tbody tr', has_text='Continuity clinic').first).to_be_visible()
        expect(main.locator('tbody tr', has_text='Walk-in North')).to_have_count(0)
    finally:
        context.close()


def test_a_child_list_condition_is_evaluated_on_the_server_beyond_the_first_page(browser, live_server, h):
    """Review (4th) #5: Show their… Leave, paged at 200: "Status is not Future" is sent with the list's selection, so
    the one ended record among 210 future ones is the whole result (not a filter of the first 200 loaded)."""
    alex = h.seed['people'][0]
    kind = h.get('admin', '/api/catalogues')['leave_types'][0]['id']
    days = [f'2027-{1 + n // 28:02d}-{1 + n % 28:02d}' for n in range(210)] + ['2026-01-05']
    h.command('coordinator', 'bulk.apply', {'item_kind': 'availability.record', 'reason': 'Test setup',
              'common': {'person_id': alex, 'kind': 'recorded_leave', 'leave_type_id': kind, 'private_reason': 'Paged record'},
              'items': [{'payload': {'start': f'{d}T08:00:00+03:00', 'end': f'{d}T09:00:00+03:00'}} for d in days]})
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        their_leave(page)
        more = page.locator('.bulk-more')
        expect(more).to_contain_text('Showing 200 of 211')
        main = page.locator('#main')
        pop = toolbar(main, 'Filter')
        condition(pop, 'Status', ['Future'], op='none')
        close_popover(page)
        expect(main.locator('tbody tr')).to_have_count(1)
        expect(main.locator('tbody tr').first).to_contain_text('Ended')
        expect(more).to_be_empty()
        assert not errors, errors
    finally:
        context.close()


def propose_max_only(browser, live_server, h):
    """Rule line Minimum 2 (medium) / Maximum 5 (mandatory) / Target 3 (medium) → Change… Numbers, Maximum 6 only → Propose.
    Returns the bulk.apply payloads sent and the waiting proposal's line numbers."""
    from .test_browser_rule_catalogue import catalogue_rule, flag, line, selection
    flag(h)
    numbers = {'min': {'value': 2, 'level': 'medium'}, 'max': {'value': 5, 'level': 'mandatory'}, 'target': {'value': 3, 'level': 'medium'}}
    catalogue_rule(h, 'duties_per_month', [line(numbers)])
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    sent = []
    page.on('request', lambda r: sent.append(r.post_data_json['payload']) if r.method == 'POST' and '/api/commands' in r.url
            and (r.post_data_json or {}).get('kind') == 'bulk.apply' else None)
    try:
        navigate(page, 'rules/duties_per_month')
        page.locator('#main tr[data-row]').first.locator('input.bulk-check').check()
        selection(page).get_by_role('button', name='Propose a change…').click()
        row = change_row(page, 'Numbers', 'Set to')
        row.get_by_label('Max', exact=True).fill('6')
        submit(page)
        expect(panel(page).get_by_role('heading', name='Preview — nothing saved yet')).to_be_visible()
        panel(page).locator('.bulk-foot .button.primary').click()
        expect(panel(page).get_by_role('heading', name='Proposed · waiting for approval')).to_be_visible()
        waiting = [p for p in h.get('admin', '/api/catalogues')['policies'] if p.get('status') == 'proposed']
        return sent, waiting[0]['definition']['rule']['lines'][0]['numbers']
    finally:
        context.close()


def test_rule_line_numbers_change_sends_only_the_number_changed(browser, live_server, h):
    """Review (4th) #6, frontend: changing only Maximum sends {max: 6} alone, with no level, so the line's other
    numbers and every number's own level are not reset by the form."""
    sent, _ = propose_max_only(browser, live_server, h)
    assert sent, sent
    changes = sent[-1]['changes']
    assert changes == [{'field': 'numbers', 'how': 'set', 'value': {'max': {'value': 6}}}], changes


def test_rule_line_numbers_change_keeps_the_other_numbers_and_levels(browser, live_server, h):
    _, numbers = propose_max_only(browser, live_server, h)
    assert numbers == {'min': {'value': 2, 'level': 'medium'}, 'max': {'value': 6, 'level': 'mandatory'},
                       'target': {'value': 3, 'level': 'medium'}}, numbers


def record_enabled(button):
    """Every moment `button` is enabled is recorded in window.__enabled (a brief one would not be seen by polling)."""
    button.evaluate("b => { window.__enabled = []; new MutationObserver(() => { if (!b.disabled) window.__enabled.push(b.textContent); })"
                    ".observe(b, {attributes: true, childList: true, characterData: true, subtree: true}); }")


def test_paste_names_a_late_reply_for_earlier_names_never_selects_them(browser, live_server, h):
    """Review (5th) #3: Alex Morgan's matching reply arrives after Jordan Lee was typed: it never enables Select nor
    selects Alex; only Jordan's own reply counts."""
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    held = []
    try:
        navigate(page, 'people')
        caret(page.locator('#main')).get_by_role('menuitem', name='Paste names or codes…').click()
        dialog = page.get_by_role('dialog', name='Paste names or codes')
        names = dialog.get_by_label('Names or codes', exact=True)
        pick = dialog.locator('.form-footer .button.primary')
        record_enabled(pick)
        page.route('**/api/bulk/resolve', lambda route: held.append(route))
        with page.expect_request('**/api/bulk/resolve'):
            names.fill('Alex Morgan')
        with page.expect_request('**/api/bulk/resolve'):
            names.fill('Jordan Lee')
            held[0].continue_()  # Alex's answer, while Jordan waits to be read
        expect(pick).to_be_disabled()
        assert len(held) == 2, held
        assert page.evaluate('window.__enabled') == [], page.evaluate('window.__enabled')
        held[1].continue_()
        page.unroute('**/api/bulk/resolve')
        expect(dialog.locator('.bulk-paste-result')).to_contain_text('1 matched')
        pick.click()
        expect(page.locator('#main tr', has_text='Jordan Lee').locator('.bulk-check')).to_be_checked()
        expect(page.locator('#main tr', has_text='Alex Morgan').locator('.bulk-check')).not_to_be_checked()
    finally:
        context.close()


def test_select_by_condition_stays_within_the_lists_own_conditions(browser, live_server, h):
    """Review (5th) #4: on a paged child list filtered "Status is not Ended", Select by condition Person = Alex takes
    the 210 future records the list holds, never the ended one the list's own condition hides."""
    alex = h.seed['people'][0]
    kind = h.get('admin', '/api/catalogues')['leave_types'][0]['id']
    days = [f'2027-{1 + n // 28:02d}-{1 + n % 28:02d}' for n in range(210)] + ['2026-01-05']
    h.command('coordinator', 'bulk.apply', {'item_kind': 'availability.record', 'reason': 'Test setup',
              'common': {'person_id': alex, 'kind': 'recorded_leave', 'leave_type_id': kind, 'private_reason': 'Paged record'},
              'items': [{'payload': {'start': f'{d}T08:00:00+03:00', 'end': f'{d}T09:00:00+03:00'}} for d in days]})
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        their_leave(page)
        main = page.locator('#main')
        pop = toolbar(main, 'Filter')
        condition(pop, 'Status', ['Ended'], op='none')
        close_popover(page)
        expect(page.locator('.bulk-more')).to_contain_text('Showing 200 of 210')
        caret(main).get_by_role('menuitem', name='Select by condition…').click()
        pop = page.get_by_role('dialog', name='Select by condition')
        condition(pop, 'Person', ['Alex Morgan'])
        expect(pop.locator('.bulk-condition-count')).to_contain_text(re.compile(r'^210 '))
        pop.get_by_role('button', name='Select', exact=True).click()
        expect(bar(page)).to_contain_text('210')
        expect(bar(page)).not_to_contain_text('211')
    finally:
        context.close()


def test_people_as_of_day_ends_with_the_session(browser, live_server, h):
    """Review (5th) #13: A reads People as of 2000 (no ranks then) and signs out; B signing in in the same tab reads
    People today (Alex's current rank)."""
    people = {p['id']: p for p in h.get('admin', '/api/catalogues')['people']}
    ranks = {c['id']: c['name'] for c in h.get('admin', '/api/catalogues')['categories']}
    rank = ranks[people[h.seed['people'][0]]['current_category_ids'][0]]
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        navigate(page, 'people')
        alex = page.locator('#main tr', has_text='Alex Morgan')
        expect(alex).to_contain_text(rank)
        pop = toolbar(page.locator('#main'), 'Filter')
        day = pop.locator('.dt-as-of input')
        day.fill('01/01/2000')
        page.keyboard.press('Tab')
        close_popover(page)
        expect(page.locator('#main tr', has_text='Alex Morgan')).not_to_contain_text(rank)
        switch_account(page, 'admin')
        navigate(page, 'people')
        expect(page.locator('#main tr', has_text='Alex Morgan')).to_contain_text(rank)
    finally:
        context.close()


def test_a_child_list_reads_its_kept_as_of_day_after_navigating_away_and_back(browser, live_server, h):
    """Review (b4a8) #1: Leave › Current with 205 rows current today and 205 current on 15 Sep 2030. As of September,
    away to People and back: the list still reads September (its shared View's day), so Select all 205 matching and
    Change… change exactly the rows shown, never the other day's."""
    alex = h.seed['people'][0]
    kinds = h.get('admin', '/api/catalogues')['leave_types']
    common = {'person_id': alex, 'kind': 'recorded_leave', 'leave_type_id': kinds[0]['id'], 'private_reason': 'As of record'}
    now, until = '2026-09-15', '2026-10-25'  # around the pinned test day (conftest TEST_NOW, 3 Oct 2026)
    for start, end in ((now + 'T{:02d}:{:02d}:00+00:00', until + 'T08:00:00+00:00'),
                       ('2030-09-01T{:02d}:{:02d}:00+00:00', '2030-09-30T08:00:00+00:00')):
        h.command('coordinator', 'bulk.apply', {'item_kind': 'availability.record', 'reason': 'Test setup', 'common': common,
                  'items': [{'payload': {'start': start.format(n // 60, n % 60), 'end': end}} for n in range(205)]})
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    sent = []
    page.on('request', lambda r: sent.append(r.post_data_json) if r.method == 'POST' and '/api/bulk/rows' in r.url else None)
    try:
        navigate(page, 'people')
        page.locator('#main tr', has_text='Alex Morgan').locator('.bulk-check').check()
        bar(page).get_by_role('button', name='Show their…').click()
        page.get_by_role('menu', name='Show their…').get_by_role('menuitem', name=re.compile('^Leave')).click()
        current_only(page)
        more = page.locator('.bulk-more')
        expect(more).to_contain_text('Showing 200 of 205')
        expect(page.locator('#main tbody tr', has_text=re.compile(r'15 Sept? 2026')).first).to_be_visible()
        pop = toolbar(page.locator('#main'), 'Filter')
        pop.locator('.dt-as-of input').fill('15/09/2030')
        page.keyboard.press('Tab')
        close_popover(page)
        expect(page.locator('#main tbody tr', has_text=re.compile(r'1 Sept? 2030')).first).to_be_visible()
        navigate(page, 'people')
        sent.clear()
        page.go_back()  # back in the app (no reload): the shared View keeps its As of day
        expect(more).to_contain_text('Showing 200 of 205')
        # The rows read on re-entry are September's (the shared View still says As of 15 Sep 2030).
        expect(page.locator('#main tbody tr', has_text=re.compile(r'1 Sept? 2030')).first).to_be_visible()
        expect(page.locator('#main tbody tr', has_text=re.compile(r'15 Sept? 2026'))).to_have_count(0)
        assert sent and all(b['selection']['where'].get('as_of') == '2030-09-15' for b in sent), sent
        page.get_by_role('checkbox', name=re.compile('^Select all .* shown')).check()
        page.locator('.dt-select-banner').get_by_role('button', name='Select all 205 matching').click()
        expect(bar(page)).to_contain_text('205')
        bar(page).get_by_role('button', name='Change…').click()
        row = change_row(page, re.compile('Leave type', re.I), 'Set to')
        row.locator('.bulk-value select').select_option(label=kinds[1]['name'])
        submit(page)
        panel(page).get_by_role('button', name='Apply ready rows (205) → Results').click()
        expect(page.locator('.toast', has_text=re.compile(r'^205 .* changed'))).to_be_visible()
        retyped = {r['start'][:4] for r in leave_rows(h) if r.get('leave_type_id') == kinds[1]['id']}
        assert retyped == {'2030'}, retyped
        assert not errors, errors
    finally:
        context.close()


def test_ticks_and_unticks_after_select_by_condition_overlay_the_server_selection(browser, live_server, h):
    """Review (5fd5) #3: Leave of Alex (195) and Jordan (15), paged at 200. Select by condition Person is Alex (195,
    the server's selection); ticking one of Jordan's rows adds it, unticking one of Alex's further down excludes
    only that one: 195 go to Preview and Apply changes exactly those."""
    alex, jordan = h.seed['people'][0], h.seed['people'][1]
    kinds = h.get('admin', '/api/catalogues')['leave_types']
    for person, count in ((alex, 195), (jordan, 15)):
        days = [f'2027-{1 + n // 28:02d}-{1 + n % 28:02d}' for n in range(count)]
        h.command('coordinator', 'bulk.apply', {'item_kind': 'availability.record', 'reason': 'Test setup',
                  'common': {'person_id': person, 'kind': 'recorded_leave', 'leave_type_id': kinds[0]['id'], 'private_reason': 'Paged record'},
                  'items': [{'payload': {'start': f'{d}T08:00:00+03:00', 'end': f'{d}T09:00:00+03:00'}} for d in days]})
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        navigate(page, 'people')
        for name in ('Alex Morgan', 'Jordan Lee'):
            page.locator('#main tr', has_text=name).locator('.bulk-check').check()
        bar(page).get_by_role('button', name='Show their…').click()
        page.get_by_role('menu', name='Show their…').get_by_role('menuitem', name=re.compile('^Leave')).click()
        main = page.locator('#main')
        pop = toolbar(main, 'Filter')
        pop.get_by_role('button', name='Clear all filters', exact=True).click()
        close_popover(page)
        expect(page.locator('.bulk-more')).to_contain_text('Showing 200 of 210')
        caret(main).get_by_role('menuitem', name='Select by condition…').click()
        pop = page.get_by_role('dialog', name='Select by condition')
        condition(pop, 'Person', ['Alex Morgan'])
        expect(pop.locator('.bulk-condition-count')).to_contain_text('195')
        pop.get_by_role('button', name='Select', exact=True).click()
        expect(bar(page)).to_contain_text('195')
        # Jordan's rows first (sorted on the rows loaded): tick one of them.
        sort = toolbar(main, 'Sort')
        sort.get_by_role('menuitemradio', name='Person', exact=True).click()
        sort_direction(main, 'Descending')
        close_popover(page)
        added = main.locator('tbody tr', has_text='Jordan Lee').first
        added_id = added.get_attribute('data-row')
        added.locator('.bulk-check').check()
        expect(bar(page)).to_contain_text('196')
        # Further down the loaded rows (one page, the server's): untick one of Alex's.
        removed = main.locator('tbody tr', has_text='Alex Morgan').first
        removed_id = removed.get_attribute('data-row')
        removed.locator('.bulk-check').uncheck()
        expect(bar(page)).to_contain_text('195')
        bar(page).get_by_role('button', name='Change…').click()
        row = change_row(page, re.compile('Leave type', re.I), 'Set to')
        row.locator('.bulk-value select').select_option(label=kinds[1]['name'])
        submit(page)
        panel(page).get_by_role('button', name='Apply ready rows (195) → Results').click()
        expect(page.locator('.toast', has_text=re.compile(r'^195 .* changed'))).to_be_visible()
        rows = [r for r in leave_rows(h) if r.get('person_id') in (alex, jordan) and str(r.get('start', '')).startswith('2027')]
        retyped = {r['id'] for r in rows if r['leave_type_id'] == kinds[1]['id']}
        expected = {r['id'] for r in rows if r['person_id'] == alex and r['id'] != removed_id} | {added_id}
        assert retyped == expected, (len(retyped), len(expected))
        assert not errors, errors
    finally:
        context.close()


def test_a_named_views_as_of_day_can_be_changed_and_reset_without_snapping_back(browser, live_server, h):
    """Review (5fd5) #5: on a saved view of People with As of 1 Sep 2026, choosing 15 Sep or Back to today stays (the
    list redraws, the saved day does not return); picking the view again, or a reload, brings its day back."""
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        navigate(page, 'people')
        main = page.locator('#main')
        day = toolbar(main, 'Filter').locator('.dt-as-of input')
        day.fill('01/09/2026')
        page.keyboard.press('Tab')
        close_popover(page)
        toolbar(main, 'Saved views').get_by_role('menuitem', name='New view…').click()
        dialog = page.get_by_role('dialog', name='New view', exact=True)
        dialog.get_by_label('View name', exact=True).fill('On 1 September')
        dialog.get_by_role('button', name='Save view', exact=True).click()
        expect(dialog).not_to_be_visible()

        def as_of():
            value = toolbar(main, 'Filter').locator('.dt-as-of input').input_value()
            close_popover(page)
            return value
        day = toolbar(main, 'Filter').locator('.dt-as-of input')
        day.fill('15/09/2026')
        page.keyboard.press('Tab')
        close_popover(page)
        expect(main.locator('tbody tr').first).to_be_visible()
        assert '15 Sep 2026' in as_of()
        toolbar(main, 'Filter').get_by_role('button', name='Back to today').click()
        close_popover(page)
        expect(main.locator('tbody tr').first).to_be_visible()
        assert '3 Oct 2026' in as_of()
        # Picking the view again brings its day back.
        toolbar(main, 'Saved views').get_by_role('menuitemradio', name=re.compile('^On 1 September')).click()
        close_popover(page)
        assert '1 Sep 2026' in as_of()
    finally:
        context.close()


def test_show_their_opens_the_child_list_on_the_parent_lists_as_of_day(browser, live_server, h):
    """Review (5fd5) #6: People As of 15 Nov 2026 › Show their… › Clinic permissions opens Current on 15 Nov (a
    permission from 1 Nov is shown), not today's and not a day the child list kept from an earlier visit (2030)."""
    alex = h.seed['people'][0]
    service = h.configure('services', {'name': 'November clinic', 'starts_on': '2020-01-01'})['record']
    h.command('credentialer', 'eligibility.create', {'person_id': alex, 'service_id': service['id'],
              'starts_on': '2026-11-01', 'ends_on': '2026-12-01'})
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)

    def as_of(main, day):
        pop = toolbar(main, 'Filter')
        pop.locator('.dt-as-of input').fill(day)
        page.keyboard.press('Tab')
        close_popover(page)
    try:
        # An earlier visit leaves the child list on 1 Jan 2030.
        open_their_permissions(page)
        main = page.locator('#main')
        as_of(main, '01/01/2030')
        expect(main.locator('tbody tr', has_text='November clinic')).to_have_count(0)
        navigate(page, 'people')
        as_of(main, '15/11/2026')
        main.locator('tr', has_text='Alex Morgan').locator('.bulk-check').check()
        bar(page).get_by_role('button', name='Show their…').click()
        # The child list's read is awaited (the rows can be drawn before the assertion would see the request).
        with page.expect_request(lambda r: r.method == 'POST' and '/api/bulk/rows' in r.url) as sent:
            show_their_permissions(page)
        assert sent.value.post_data_json['selection']['where'].get('as_of') == '2026-11-15', sent.value.post_data_json
        expect(main.locator('tbody tr', has_text='November clinic')).to_have_count(1)
        expect(toolbar(main, 'Filter').locator('.dt-as-of input')).to_have_value(re.compile('15 Nov 2026'))
        close_popover(page)
        # The day travels in the address: a reload opens the child list on it again.
        assert 'as_of=2026-11-15' in page.url
        page.reload()
        expect(page.locator('#main tbody tr', has_text='November clinic')).to_have_count(1)
        assert not errors, errors
    finally:
        context.close()


@pytest.mark.parametrize('how', ['all matching', 'condition'])
def test_a_server_selection_holds_after_show_more_loads_the_last_page(browser, live_server, h, how):
    """Review (6210) #2: Select all 210 matching (or by condition) with 200 loaded, then Show 10 more: the selection stays
    the server's 210 (the 10 loaded rows are ticked) and Apply changes all 210."""
    alex = h.seed['people'][0]
    kinds = h.get('admin', '/api/catalogues')['leave_types']
    days = [f'2027-{1 + n // 28:02d}-{1 + n % 28:02d}' for n in range(210)]
    h.command('coordinator', 'bulk.apply', {'item_kind': 'availability.record', 'reason': 'Test setup',
              'common': {'person_id': alex, 'kind': 'recorded_leave', 'leave_type_id': kinds[0]['id'], 'private_reason': 'Paged record'},
              'items': [{'payload': {'start': f'{d}T08:00:00+03:00', 'end': f'{d}T09:00:00+03:00'}} for d in days]})
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        their_leave(page)
        main, more = page.locator('#main'), page.locator('.bulk-more')
        expect(more).to_contain_text('Showing 200 of 210')
        if how == 'all matching':
            page.get_by_role('checkbox', name=re.compile('^Select all .* shown')).check()
            page.locator('.dt-select-banner').get_by_role('button', name='Select all 210 matching').click()
        else:
            caret(main).get_by_role('menuitem', name='Select by condition…').click()
            pop = page.get_by_role('dialog', name='Select by condition')
            condition(pop, 'Person', ['Alex Morgan'])
            expect(pop.locator('.bulk-condition-count')).to_contain_text('210')
            pop.get_by_role('button', name='Select', exact=True).click()
        expect(bar(page)).to_contain_text('210')
        more.get_by_role('button', name='Show 10 more').click()
        expect(more).to_be_empty()
        expect(bar(page)).to_contain_text('210')
        # Every page, the rows just loaded included, shows its boxes ticked, and the header box is ticked, not mixed
        # (#467 SHOULD 13: loaded members join before the roll-up).
        while True:
            expect(main.locator('tbody .bulk-check:not(:checked)')).to_have_count(0)
            expect(page.get_by_role('checkbox', name=re.compile('^Select all .* shown'))).to_be_checked()
            following = main.get_by_role('button', name='Next page')
            if not following.count() or following.is_disabled():
                break
            following.click()
        bar(page).get_by_role('button', name='Change…').click()
        row = change_row(page, re.compile('Leave type', re.I), 'Set to')
        row.locator('.bulk-value select').select_option(label=kinds[1]['name'])
        submit(page)
        panel(page).get_by_role('button', name='Apply ready rows (210) → Results').click()
        expect(page.locator('.toast', has_text=re.compile(r'^210 .* changed'))).to_be_visible()
        assert sum(r['leave_type_id'] == kinds[1]['id'] for r in leave_rows(h) if r['person_id'] == alex) == 210
        assert not errors, errors
    finally:
        context.close()


def test_a_saved_child_view_day_gives_way_to_the_parent_lists_day(browser, live_server, h):
    """Review (6210) #4: a child list's named view saved on 1 Jan 2030 is remembered; after a reload, People As of
    15 Nov 2026 › Show their… opens the child list on 15 Nov (and a link with its day too), not on the view's 2030."""
    alex = h.seed['people'][0]
    service = h.configure('services', {'name': 'November clinic', 'starts_on': '2020-01-01'})['record']
    h.command('credentialer', 'eligibility.create', {'person_id': alex, 'service_id': service['id'],
              'starts_on': '2026-11-01', 'ends_on': '2026-12-01'})
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)

    def as_of(main, day):
        toolbar(main, 'Filter').locator('.dt-as-of input').fill(day)
        page.keyboard.press('Tab')
        close_popover(page)
    try:
        open_their_permissions(page)
        main = page.locator('#main')
        as_of(main, '01/01/2030')
        toolbar(main, 'Saved views').get_by_role('menuitem', name='New view…').click()
        dialog = page.get_by_role('dialog', name='New view', exact=True)
        dialog.get_by_label('View name', exact=True).fill('In 2030')
        dialog.get_by_role('button', name='Save view', exact=True).click()
        expect(dialog).not_to_be_visible()
        page.reload()
        navigate(page, 'people')
        as_of(main, '15/11/2026')
        main.locator('tr', has_text='Alex Morgan').locator('.bulk-check').check()
        bar(page).get_by_role('button', name='Show their…').click()
        show_their_permissions(page)
        expect(main.locator('tbody tr', has_text='November clinic')).to_have_count(1)
        expect(toolbar(main, 'Filter').locator('.dt-as-of input')).to_have_value(re.compile('15 Nov 2026'))
        close_popover(page)
        # A link with the day: the same.
        link = page.url
        page.goto('about:blank')
        page.goto(link)
        expect(page.locator('#main tbody tr', has_text='November clinic')).to_have_count(1)
        expect(toolbar(page.locator('#main'), 'Filter').locator('.dt-as-of input')).to_have_value(re.compile('15 Nov 2026'))
        assert not errors, errors
    finally:
        context.close()


def test_change_proposes_is_not_offered_over_unsaved_rule_edits(browser, live_server, h):
    """Review (7f05) #1: a saved line's Maximum edited 5 → 6 (not saved yet): Propose a change…, which proposes from the
    saved rule, is not offered for the line; Numbers… leads, and the unsaved Maximum 6 stays in the draft."""
    from .test_browser_rule_catalogue import catalogue_rule, flag, line, selection, set_numbers
    flag(h)
    catalogue_rule(h, 'duties_per_month', [line({'max': {'value': 5, 'level': 'mandatory'}}, note='Everyone')])
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        navigate(page, 'rules/duties_per_month')
        main = page.locator('#main')
        main.locator('tr[data-row]').first.locator('input.bulk-check').check()
        expect(selection(page).get_by_role('button', name='Propose a change…')).to_be_visible()
        set_numbers(page, max=6)
        expect(main.locator('tr[data-row]').first).to_contain_text('6')
        if not main.locator('tr[data-row]').first.locator('input.bulk-check').is_checked():
            main.locator('tr[data-row]').first.locator('input.bulk-check').check()
        expect(selection(page).get_by_role('button', name='Numbers…')).to_be_visible()
        expect(selection(page).get_by_role('button', name='Propose a change…')).to_have_count(0)
        expect(main.locator('tr[data-row]').first).to_contain_text('6')
        assert not errors, errors
    finally:
        context.close()


def leave_210(h, alex):
    kinds = h.get('admin', '/api/catalogues')['leave_types']
    days = [f'2027-{1 + n // 28:02d}-{1 + n % 28:02d}' for n in range(210)]
    h.command('coordinator', 'bulk.apply', {'item_kind': 'availability.record', 'reason': 'Test setup',
              'common': {'person_id': alex, 'kind': 'recorded_leave', 'leave_type_id': kinds[0]['id'], 'private_reason': 'Paged record'},
              'items': [{'payload': {'start': f'{d}T08:00:00+03:00', 'end': f'{d}T09:00:00+03:00'}} for d in days]})
    return kinds


def retype_selected(page, h, kinds, count):
    bar(page).get_by_role('button', name='Change…').click()
    row = change_row(page, re.compile('Leave type', re.I), 'Set to')
    row.locator('.bulk-value select').select_option(label=kinds[1]['name'])
    submit(page)
    panel(page).get_by_role('button', name=f'Apply ready rows ({count}) → Results').click()
    # One command applies them all: on a loaded runner it takes longer than a locator's default, up to the server's
    # own write budget. Wait for the panel's Applying status to end, then for the toast it ends with.
    expect(panel(page).locator('.bulk-progress')).to_have_count(0, timeout=Settings().command_write_budget_ms)
    expect(page.locator('.toast', has_text=re.compile(rf'^{count} .* changed'))).to_be_visible()
    return {r['id'] for r in leave_rows(h) if r['leave_type_id'] == kinds[1]['id']}


@pytest.mark.parametrize('how', ['menu', 'keyboard'])
def test_select_all_matching_from_the_menu_and_ctrl_a_takes_every_server_match(browser, live_server, h, how):
    """Review (7f05) #6: with 210 matches and 200 loaded, the selection menu's Select all matching and a second Ctrl A
    take all 210 (the server's selection), as the banner does."""
    alex = h.seed['people'][0]
    kinds = leave_210(h, alex)
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        their_leave(page)
        main = page.locator('#main')
        expect(page.locator('.bulk-more')).to_contain_text('Showing 200 of 210')
        if how == 'menu':
            menu = caret(main)
            item = menu.get_by_role('menuitem', name=re.compile('^Select all matching'))
            expect(item).to_contain_text('210')
            item.click()
        else:
            main.locator('tbody td').first.click()
            page.keyboard.press('Control+a')
            expect(bar(page)).to_contain_text('200')  # the first Ctrl A: every row shown (all 200 loaded)
            page.keyboard.press('Control+a')
        expect(bar(page)).to_contain_text('210')
        assert len(retype_selected(page, h, kinds, 210)) == 210
        assert not errors, errors
    finally:
        context.close()


@pytest.mark.parametrize('redraw', ['activity', 'layout', 'away and back'])
def test_a_server_selection_with_an_exclusion_survives_a_redraw_of_the_list(browser, live_server, h, redraw):
    """Review (7f05) #3: Select all 210 matching, untick one, then the list is drawn again (another person's change
    arrives, Cards and back to Table, or People and back): the bar still says 209 and Apply changes those 209."""
    alex = h.seed['people'][0]
    kinds = leave_210(h, alex)
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        their_leave(page)
        main = page.locator('#main')
        expect(page.locator('.bulk-more')).to_contain_text('Showing 200 of 210')
        page.get_by_role('checkbox', name=re.compile('^Select all .* shown')).check()
        page.locator('.dt-select-banner').get_by_role('button', name='Select all 210 matching').click()
        expect(bar(page)).to_contain_text('210')
        dropped = main.locator('tbody tr[data-row]').first
        dropped_id = dropped.get_attribute('data-row')
        dropped.locator('.bulk-check').uncheck()
        expect(bar(page)).to_contain_text('209')
        page.evaluate('document.activeElement.blur()')  # nobody typing: a live update is drawn at once
        if redraw == 'activity':
            with page.expect_request(lambda r: '/api/bulk/rows' in r.url):
                tag = h.configure('tags', {'name': 'elsewhere', 'labels': ['services']})
                assert tag['record']
        elif redraw == 'layout':
            toolbar(main, 'View settings').get_by_role('radio', name='Cards', exact=True).click()
            toolbar(main, 'View settings').get_by_role('radio', name='Table', exact=True).click()
            close_popover(page)
        else:
            navigate(page, 'people')
            page.go_back()
        expect(page.locator('.bulk-more')).to_contain_text('Showing 200 of 210')
        expect(bar(page)).to_contain_text('209')
        expect(main.locator(f'tr[data-row="{dropped_id}"] .bulk-check')).not_to_be_checked()
        retyped = retype_selected(page, h, kinds, 209)
        assert len(retyped) == 209 and dropped_id not in retyped
        assert not errors, errors
    finally:
        context.close()


def test_change_on_several_people_starts_with_no_field_and_offers_no_identity_fields(browser, live_server, h):
    """UX review P-01: Change… on several people starts with nothing chosen ("Choose…", Continue asks for it) and never
    offers Name, Short code or Staff number, which say which person a record is; one person still offers them."""
    context, page = sign_in(browser, live_server, 'admin', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        navigate(page, 'people')
        page.get_by_role('checkbox', name=re.compile('^Select all .* shown')).check()
        bar(page).get_by_role('button', name='Change…').click()
        what = panel(page).get_by_label('What to change', exact=True)
        expect(what).to_have_value('')
        words = what.evaluate("s=>[...s.options].filter(o=>!o.hidden).map(o=>o.textContent)")
        assert not {'Name', 'Short code', 'Staff number'} & set(words), words
        assert len(words) > 1, words
        expect(panel(page).locator('.bulk-how')).to_be_empty()
        submit(page)
        expect(panel(page).get_by_role('heading', name='Change', exact=True)).to_be_visible()  # still on Change: nothing chosen
        assert what.evaluate('s => !s.checkValidity()')
        page.keyboard.press('Escape')
        discard = page.get_by_role('button', name='Discard changes')
        if discard.count():
            discard.click()
        expect(panel(page)).to_have_count(0)
        page.get_by_role('checkbox', name=re.compile('^Select all .* shown')).uncheck()
        page.locator('#main tbody tr').first.locator('.bulk-check').check()
        bar(page).get_by_role('button', name='Change…').click()
        words = panel(page).get_by_label('What to change', exact=True).evaluate("s=>[...s.options].map(o=>o.textContent)")
        assert 'Name' in words, words
        assert not errors, errors
    finally:
        context.close()


def test_change_offers_no_text_input_for_a_structured_definition(browser, live_server, h):
    """Review (7f05) #4: a Duty type's Details (its definition, an object from its own editor) is not offered in Change… (it would
    become a text box and fail validation); its plain fields still are."""
    context, page = sign_in(browser, live_server, 'admin', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        navigate(page, 'configuration')
        page.get_by_role('button', name=re.compile(r'^Duty types')).click()
        page.get_by_role('checkbox', name=re.compile('^Select all .* shown')).check()
        bar(page).get_by_role('button', name='Change…').click()
        what = panel(page).get_by_label('What to change', exact=True)
        words = what.evaluate("s=>[...s.options].map(o=>o.textContent)")
        assert 'Details' not in words, words  # the duty type's definition
        assert 'Description' in words, words
        assert not errors, errors
    finally:
        context.close()


def test_set_a_rule_number_waits_for_unsaved_edits_of_that_rule(browser, live_server, h):
    """Self-review (c): People › Set a rule number… adds lines to the rule as saved; while that rule has unsaved edits on
    the Rules page it says so instead of proposing (the edits would be dropped by the new proposal)."""
    from .test_browser_rule_catalogue import flag, line, set_numbers
    from .test_browser_rule_catalogue_v2 import v2_rule
    flag(h, legacy=False)
    v2_rule(h, 'workload_limit', [line({'max': {'value': 5, 'level': 'mandatory'}}, measure='duties', period={'kind': 'calendar_week'})])
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        navigate(page, 'rules/workload_limit')
        page.locator('#main tr[data-row]').first.locator('input.bulk-check').check()
        set_numbers(page, max=6)
        page.evaluate("location.hash='people'")  # leaving asks first; the edits stay as a draft either way
        page.get_by_role('dialog', name='Leave this page?').get_by_role('button', name='Go to People', exact=True).click()
        expect_loaded(page, 'people')
        page.locator('#main tr', has_text='Alex Morgan').locator('.bulk-check').check()
        bar(page).get_by_role('button', name='More actions').click()
        page.get_by_role('menu', name='More actions').get_by_role('menuitem', name='Set a rule number…').click()
        dialog = page.get_by_role('dialog', name=re.compile('^Set a rule number'))
        dialog.get_by_label('Rule', exact=True).select_option(label='Workload limit')
        dialog.get_by_label('Maximum', exact=True).fill('7')
        dialog.get_by_label('Reason', exact=True).fill('Alex: at most 7')
        dialog.get_by_role('button', name='Preview', exact=True).click()
        expect(dialog).to_contain_text('unsaved edits on the Rules page')
        assert not [p for p in h.get('admin', '/api/catalogues')['policies'] if p.get('status') == 'proposed']
        assert not errors, errors
    finally:
        context.close()


@pytest.mark.parametrize('child,label', [('policy_lines', 'Rule lines'), ('scheduling_facts', 'Scheduling facts'), ('exceptions', 'Person exceptions')])
def test_show_their_workflow_records_open_on_the_collections_own_statuses(browser, live_server, h, child, label):
    """Review (7f05) #2: rule lines, scheduling facts and person exceptions have workflow statuses (Proposed, Active,
    Approved…), not Current / Future / Ended: Show their… opens them populated on the registry's own first statuses,
    with the Status filter offering those words."""
    from datetime import date
    from apps.api import models as m
    from apps.api.db import transaction
    from .test_rule_catalogue_api import line as api_line, person, propose
    from .test_scheduling_fact_approvals import propose as propose_fact
    who = h.seed['people'][0]
    if child == 'policy_lines':
        propose(h, 'duties_per_week', [api_line(who=person(who), numbers={'max': {'value': 3}}, note='Proposed for this test')])
    elif child == 'scheduling_facts':
        propose_fact(h, person=who)
    else:
        policy = h.policy({'kind': 'max_hours', 'hours': 40})
        with transaction(h.app.state.engine, write=True) as db:
            db.add(m.PolicyException(department_id=h.seed['department_id'], policy_id=policy['id'], policy_revision=policy['revision'],
                   person_id=who, starts_on=date(2026, 9, 1), ends_on=date(2027, 1, 1), reason='Workflow status record',
                   created_by=h.seed['identities']['coordinator'], status='proposed'))
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        navigate(page, 'people')
        page.locator('#main tr', has_text='Alex Morgan').locator('.bulk-check').check()
        bar(page).get_by_role('button', name='Show their…').click()
        page.get_by_role('menu', name='Show their…').get_by_role('menuitem', name=re.compile('^' + label)).click()
        main = page.locator('#main')
        # The record made here, found by its own words (other records, e.g. a seeded rule line, may come first).
        mine = main.locator('tbody tr', has_text='Proposed for this test') if child == 'policy_lines' else main.locator('tbody tr', has_text='Proposed')
        expect(mine.first).to_contain_text('Proposed')
        if child == 'policy_lines':
            # A line of a rule in force (the seeded Fair share rule) is there too: Active is one of the first statuses.
            expect(main.locator('tbody tr', has_text='Active').first).to_be_visible()
        # Its two first statuses are the list's own filter: counted as the view's, without the accent (X-01, F-C2).
        own = main.locator('.dt-toolbar').get_by_role('button', name='Filter (2 from the view)', exact=True)
        expect(own).to_be_visible()
        expect(own).not_to_have_class(re.compile(r'\bon\b'))
        assert 'Current' not in main.locator('tbody').inner_text()
        assert not errors, errors
    finally:
        context.close()


def test_invert_on_a_paged_list_is_every_server_match_except_the_selected(browser, live_server, h):
    """Self-review (a): Invert on a list paged on the server (210 matches, 200 loaded) with 2 rows ticked selects the
    other 208 (the server's selection with those 2 excluded); Apply changes exactly those."""
    alex = h.seed['people'][0]
    kinds = leave_210(h, alex)
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        their_leave(page)
        main = page.locator('#main')
        expect(page.locator('.bulk-more')).to_contain_text('Showing 200 of 210')
        picked = [main.locator('tbody tr[data-row]').nth(i) for i in (0, 1)]
        ids = [r.get_attribute('data-row') for r in picked]
        for r in picked:
            r.locator('.bulk-check').check()
        expect(bar(page)).to_contain_text('2')
        caret(main).get_by_role('menuitem', name='Invert selection').click()
        expect(bar(page)).to_contain_text('208')
        retyped = retype_selected(page, h, kinds, 208)
        assert len(retyped) == 208 and not set(ids) & retyped
        assert not errors, errors
    finally:
        context.close()


def test_change_proposes_selects_the_lines_of_the_rule_in_force_only(browser, live_server, h):
    """Pre-review #2: a rule replaced by a version that keeps its line ids leaves the old version retired with the same
    ids; Propose a change… on the active line sends these lines of this rule version, so one version is proposed (the
    retired one is not selected and nothing is stale)."""
    from .test_browser_rule_catalogue import catalogue_rule, flag, line, selection
    from .test_rule_catalogue_api import approve, propose
    flag(h)
    first = line({'max': {'value': 5, 'level': 'mandatory'}}, note='Everyone')
    rule = catalogue_rule(h, 'duties_per_month', [first])
    active = approve(h, propose(h, 'duties_per_month', [{**first, 'numbers': {'max': {'value': 6, 'level': 'mandatory'}}}],
                                revision=rule['revision'], policy_id=rule['id'])['record'])
    sent = []
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    page.on('request', lambda r: sent.append(r.post_data_json) if r.method == 'POST' and '/api/commands/preview' in r.url else None)
    errors = errors_of(page)
    try:
        navigate(page, 'rules/duties_per_month')
        page.locator('#main tr[data-row]').first.locator('input.bulk-check').check()
        selection(page).get_by_role('button', name='Propose a change…').click()
        row = change_row(page, 'Note', 'Set to')
        row.locator('.bulk-value input').fill('Changed in bulk')
        submit(page)
        expect(panel(page).get_by_role('button', name=re.compile(r'^Propose 1$'))).to_be_visible()
        where = sent[-1]['payload']['selection']['where']['filters']
        assert where == {'policy_id': [active['id']], 'id': [first['id']]}, where
        panel(page).get_by_role('button', name=re.compile(r'^Propose 1$')).click()
        expect(panel(page).get_by_role('heading', name='Proposed · waiting for approval')).to_be_visible()
        assert not errors, errors
    finally:
        context.close()


def two_peoples_leave(h):
    """Leave of Alex (195) and Jordan (15) in 2027, one hour each."""
    alex, jordan = h.seed['people'][0], h.seed['people'][1]
    kinds = h.get('admin', '/api/catalogues')['leave_types']
    for who, count in ((alex, 195), (jordan, 15)):
        days = [f'2027-{1 + n // 28:02d}-{1 + n % 28:02d}' for n in range(count)]
        h.command('coordinator', 'bulk.apply', {'item_kind': 'availability.record', 'reason': 'Test setup',
                  'common': {'person_id': who, 'kind': 'recorded_leave', 'leave_type_id': kinds[0]['id'], 'private_reason': 'Paged record'},
                  'items': [{'payload': {'start': f'{d}T08:00:00+03:00', 'end': f'{d}T09:00:00+03:00'}} for d in days]})
    return alex, jordan, kinds


def their_leave_of(page, *names):
    navigate(page, 'people')
    for name in names:
        page.locator('#main tr', has_text=name).locator('.bulk-check').check()
    bar(page).get_by_role('button', name='Show their…').click()
    page.get_by_role('menu', name='Show their…').get_by_role('menuitem', name=re.compile('^Leave')).click()
    expect(page.get_by_role('button', name=re.compile(r'^For \d+ selected'))).to_be_visible()


def every_status(page):
    pop = toolbar(page.locator('#main'), 'Filter')
    pop.get_by_role('button', name='Clear all filters', exact=True).click()
    close_popover(page)


def test_a_child_lists_selection_belongs_to_its_parents(browser, live_server, h):
    """Pre-review #5: a leave record ticked among Alex's is not still selected (hidden) in Jordan's leave."""
    two_peoples_leave(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        their_leave_of(page, 'Alex Morgan')
        page.locator('#main tbody tr[data-row]').first.locator('.bulk-check').check()
        expect(bar(page)).to_contain_text('1')
        navigate(page, 'people')
        caret(page.locator('#main')).get_by_role('menuitem', name='Select none').click()
        their_leave_of(page, 'Jordan Lee')
        expect(page.locator('#main tbody tr[data-row]').first).to_be_visible()
        expect(bar(page)).to_be_hidden()
        assert not errors, errors
    finally:
        context.close()


def test_invert_after_select_by_condition_is_every_other_match(browser, live_server, h):
    """Pre-review #6: Alex's 195 selected by condition among 210 (paged at 200); Invert selects Jordan's 15."""
    from .test_browser_bulk_sheet import no_light_path
    no_light_path(h)  # 15 would apply at once: Preview shows what goes
    alex, jordan, kinds = two_peoples_leave(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        their_leave_of(page, 'Alex Morgan', 'Jordan Lee')
        main = page.locator('#main')
        every_status(page)
        expect(page.locator('.bulk-more')).to_contain_text('Showing 200 of 210')
        caret(main).get_by_role('menuitem', name='Select by condition…').click()
        pop = page.get_by_role('dialog', name='Select by condition')
        condition(pop, 'Person', ['Alex Morgan'])
        expect(pop.locator('.bulk-condition-count')).to_contain_text('195')
        pop.get_by_role('button', name='Select', exact=True).click()
        expect(bar(page)).to_contain_text('195')
        caret(main).get_by_role('menuitem', name='Invert selection').click()
        expect(bar(page)).to_contain_text('15')
        retyped = retype_selected(page, h, kinds, 15)
        assert {r['person_id'] for r in leave_rows(h) if r['id'] in retyped} == {jordan} and len(retyped) == 15
        assert not errors, errors
    finally:
        context.close()


def test_a_late_select_all_reply_never_undoes_a_clear(browser, live_server, h):
    """Pre-review #7: Select all matching (the server's count still on its way), then Select none: when the count
    arrives, nothing is selected again."""
    alex = h.seed['people'][0]
    leave_210(h, alex)
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    held = []
    try:
        their_leave(page)
        main = page.locator('#main')
        expect(page.locator('.bulk-more')).to_contain_text('Showing 200 of 210')
        page.route('**/api/bulk/resolve', lambda route: held.append(route))
        caret(main).get_by_role('menuitem', name=re.compile('^Select all matching')).click()
        expect(bar(page)).to_contain_text('200')  # the rows loaded, at once
        caret(main).get_by_role('menuitem', name='Select none').click()
        expect(bar(page)).to_be_hidden()
        assert held, 'the count was asked'
        for route in held:
            with page.expect_response(lambda r: '/api/bulk/resolve' in r.url):
                route.continue_()
        page.unroute('**/api/bulk/resolve')
        settled(page)  # the replies are handled
        expect(bar(page)).to_be_hidden()
        expect(main.locator('tbody .bulk-check:checked')).to_have_count(0)
        assert not errors, errors
    finally:
        context.close()


def test_a_late_saved_view_never_overwrites_a_newer_as_of_day(browser, live_server, h):
    """Pre-review #8: People on a saved view of 1 Jan 2030, reloaded; the saved views are slow to arrive and 15 Nov 2026
    is chosen meanwhile: when they arrive, 15 Nov stays."""
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    held = []
    try:
        navigate(page, 'people')
        main = page.locator('#main')
        toolbar(main, 'Filter').locator('.dt-as-of input').fill('01/01/2030')
        page.keyboard.press('Tab')
        close_popover(page)
        toolbar(main, 'Saved views').get_by_role('menuitem', name='New view…').click()
        dialog = page.get_by_role('dialog', name='New view', exact=True)
        dialog.get_by_label('View name', exact=True).fill('In 2030')
        dialog.get_by_role('button', name='Save view', exact=True).click()
        expect(dialog).not_to_be_visible()
        page.route('**/api/table-views*', lambda route: held.append(route) if route.request.method == 'GET' else route.continue_())
        page.reload()
        navigate(page, 'people')
        main = page.locator('#main')
        toolbar(main, 'Filter').locator('.dt-as-of input').fill('15/11/2026')
        page.keyboard.press('Tab')
        close_popover(page)
        assert held, 'the saved views were read'
        for route in held:
            route.continue_()
        page.unroute('**/api/table-views*')
        page.wait_for_load_state('networkidle')
        expect(toolbar(main, 'Filter').locator('.dt-as-of input')).to_have_value(re.compile('15 Nov 2026'))
    finally:
        context.close()


def test_a_link_with_another_day_opens_the_child_list_on_that_day(browser, live_server, h):
    """Pre-review #9: the same parents' Clinic permissions linked on 15 Nov 2026, then on 1 Jan 2030: the second link
    opens on 1 Jan 2030 (not the day remembered from the first)."""
    alex = h.seed['people'][0]
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        navigate(page, 'people')
        main = page.locator('#main')
        for day, shown in (('2026-11-15', '15 Nov 2026'), ('2030-01-01', '1 Jan 2030')):
            page.evaluate("h => { location.hash = h; }", f'people/related/eligibilities?parent=people&of={alex}&as_of={day}')
            expect(page.locator('.bulk-crumb')).to_be_visible()
            expect(toolbar(main, 'Filter').locator('.dt-as-of input')).to_have_value(re.compile(shown))
            close_popover(page)
    finally:
        context.close()


def test_preview_shows_times_in_the_department_timezone(browser, live_server, h):
    """Pre-review #10: a leave from 08:00 Riyadh time moved to 10:15 reads 08:00 to 10:15 in Preview (not UTC 05:00)."""
    from .test_browser_bulk_sheet import no_light_path
    no_light_path(h)
    alex = h.seed['people'][0]
    kind = h.get('admin', '/api/catalogues')['leave_types'][0]
    h.command('coordinator', 'bulk.apply', {'item_kind': 'availability.record', 'reason': 'Test setup',
              'common': {'person_id': alex, 'kind': 'recorded_leave', 'leave_type_id': kind['id'], 'private_reason': 'Dated record'},
              'items': [{'payload': {'start': '2027-02-01T08:00:00+03:00', 'end': '2027-02-01T17:00:00+03:00'}}]})
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        their_leave(page)
        page.locator('#main tr', has_text='1 Feb 2027').locator('.bulk-check').check()
        bar(page).get_by_role('button', name='Change…').click()
        row = change_row(page, 'From', 'Set to')
        row.get_by_label('From', exact=True).fill('01/02/2027')
        row.get_by_label('From time', exact=True).fill('10:15')
        submit(page)
        change = panel(page).locator('.ba').first
        expect(change).to_contain_text('08:00')
        expect(change).to_contain_text('10:15')
        expect(change).not_to_contain_text('05:00')
        assert not errors, errors
    finally:
        context.close()


def test_select_all_matching_keeps_the_records_selected_before(browser, live_server, h):
    """Pre-review 2 #1: one of Jordan's leave ticked, then a search for Alex (210 matches, 200 loaded) and Select all
    210 matching: the selection is the 210 plus Jordan's one (211), and Apply changes exactly those."""
    from .test_browser_bulk_sheet import no_light_path
    no_light_path(h)
    alex, jordan = h.seed['people'][0], h.seed['people'][1]
    kinds = leave_210(h, alex)
    h.command('coordinator', 'bulk.apply', {'item_kind': 'availability.record', 'reason': 'Test setup',
              'common': {'person_id': jordan, 'kind': 'recorded_leave', 'leave_type_id': kinds[0]['id'], 'private_reason': 'Paged record'},
              'items': [{'payload': {'start': f'2027-03-{d:02d}T08:00:00+03:00', 'end': f'2027-03-{d:02d}T09:00:00+03:00'}} for d in range(1, 6)]})
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        their_leave_of(page, 'Alex Morgan', 'Jordan Lee')
        every_status(page)
        main = page.locator('#main')
        search = main.get_by_role('searchbox').first
        search.fill('Jordan')
        expect(main.locator('tbody tr[data-row]')).to_have_count(5)
        kept = main.locator('tbody tr[data-row]').first
        kept_id = kept.get_attribute('data-row')
        kept.locator('.bulk-check').check()
        search.fill('Alex')
        expect(page.locator('.bulk-more')).to_contain_text('Showing 200 of 210')
        page.get_by_role('checkbox', name=re.compile('^Select all .* shown')).check()
        page.locator('.dt-select-banner').get_by_role('button', name='Select all 210 matching').click()
        expect(bar(page)).to_contain_text('211')
        retyped = retype_selected(page, h, kinds, 211)
        mine = {r['id'] for r in leave_rows(h) if r['person_id'] == alex and r['start'].startswith('2027')}
        assert retyped == mine | {kept_id}, (len(retyped), len(mine))
        assert not errors, errors
    finally:
        context.close()



def test_an_older_fix_recheck_never_overwrites_a_newer_choice(browser, live_server, h):
    """Pre-review 2 #6: the overlap's best fix, then Skip all, while the first re-check is slow: Apply waits for the
    re-checks, and when their replies arrive in reverse order the newer choice (Skip) is what Preview shows."""
    from .test_browser_bulk_sheet import no_light_path
    no_light_path(h)
    alex, clinic = h.seed['people'][0], h.seed['services'][0]
    for r in eligibilities(h, alex):
        h.command('credentialer', 'eligibility.end', {'ends_on': '2026-11-01', 'reason': 'Test setup'}, r['id'], r['revision'])
    h.command('credentialer', 'eligibility.create', {'person_id': alex, 'service_id': clinic, 'starts_on': '2026-12-01', 'ends_on': None})
    context, page = sign_in(browser, live_server, 'credentialer', viewport=DESKTOP)
    errors = errors_of(page)
    held = []
    try:
        navigate(page, 'people')
        page.locator('#main tr', has_text='Alex Morgan').locator('.bulk-check').check()
        bar(page).get_by_role('button', name='Show their…').click()
        page.get_by_role('menu', name='Show their…').get_by_role('menuitem', name=re.compile('^Clinic permissions')).click()
        current_only(page)
        page.get_by_role('checkbox', name=re.compile('^Select all .* shown')).check()
        bar(page).get_by_role('button', name='Change…').click()
        row = change_row(page, re.compile('Ends|Last day|Until'), 'Set to')
        row.locator('.date-text').fill('31 Jan 2027')
        row.locator('.date-text').press('Tab')
        submit(page)
        group = panel(page).locator('.dt-group', has=page.locator('.bulk-group-fix'))
        expect(group).to_have_count(1)
        apply = panel(page).locator('.bulk-foot .button.primary')
        page.route('**/api/commands/preview', lambda route: held.append(route) if 'only' in (route.request.post_data_json or {}).get('payload', {}) else route.continue_())
        group.locator('.bulk-split .button').first.click()
        expect(apply).to_be_disabled()  # waits for the re-check
        group.get_by_role('button', name='Skip all').click()
        expect(apply).to_be_disabled()
        assert len(held) == 2, len(held)
        for route in (held[1], held[0]):  # the newer reply first, then the older
            with page.expect_response(lambda r: '/api/commands/preview' in r.url):
                route.continue_()
        page.unroute('**/api/commands/preview')
        settled(page)  # the replies are handled
        expect(panel(page).locator('.bulk-sum')).to_contain_text('+ 1 no change +')  # the skipped row
        expect(panel(page).locator('.badge', has_text='Fixed')).to_have_count(0)
        expect(apply).to_be_enabled()
        assert not errors, errors
    finally:
        context.close()


def test_pasting_a_shared_name_in_a_child_list_selects_the_chosen_persons_records(browser, live_server, h):
    """Pre-review 3 #7: two people named Twin, each with a clinic permission; Paste names "Twin" in their permissions and
    choose the second Twin: that Twin's permission is selected (the choice is a person, read as their records)."""
    service = h.configure('services', {'name': 'Twin clinic', 'starts_on': '2020-01-01'})['record']
    twins = [h.configure('people', {'display_name': 'Twin', 'short_code': code})['record'] for code in ('TW1', 'TW2')]
    permits = [h.command('credentialer', 'eligibility.create', {'person_id': t['id'], 'service_id': service['id'],
                         'starts_on': '2026-10-01', 'ends_on': '2026-12-01'})['record'] for t in twins]
    context, page = sign_in(browser, live_server, 'credentialer', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        navigate(page, 'people')
        for row in page.locator('#main tr', has_text='Twin').all():
            row.locator('.bulk-check').check()
        bar(page).get_by_role('button', name='Show their…').click()
        show_their_permissions(page)
        main = page.locator('#main')
        expect(main.locator('tbody tr', has_text='Twin clinic')).to_have_count(2)
        caret(main).get_by_role('menuitem', name='Paste names or codes…').click()
        dialog = page.get_by_role('dialog', name='Paste names or codes')
        dialog.get_by_label('Names or codes', exact=True).fill('Twin')
        which = dialog.locator('.bulk-paste-list select')
        expect(which).to_have_count(1)
        which.select_option(value=twins[1]['id'])
        dialog.get_by_role('button', name=re.compile(r'^Select')).click()
        expect(dialog).to_be_hidden()
        expect(bar(page)).to_contain_text('1')
        expect(main.locator(f'tr[data-row="{permits[1]["id"]}"] .bulk-check')).to_be_checked()
        expect(main.locator(f'tr[data-row="{permits[0]["id"]}"] .bulk-check')).not_to_be_checked()
        assert not errors, errors
    finally:
        context.close()


def test_an_older_preview_reply_never_replaces_a_newer_one(browser, live_server, h):
    """Pre-review 3 #10: fix re-checks A and B are sent, B's reply comes, then C is sent and A's (older) reply comes:
    A is dropped and Apply keeps waiting for C; C's reply is what Preview shows."""
    from .test_browser_bulk_sheet import no_light_path
    no_light_path(h)
    alex, clinic = h.seed['people'][0], h.seed['services'][0]
    for r in eligibilities(h, alex):
        h.command('credentialer', 'eligibility.end', {'ends_on': '2026-11-01', 'reason': 'Test setup'}, r['id'], r['revision'])
    h.command('credentialer', 'eligibility.create', {'person_id': alex, 'service_id': clinic, 'starts_on': '2026-12-01', 'ends_on': None})
    context, page = sign_in(browser, live_server, 'credentialer', viewport=DESKTOP)
    errors = errors_of(page)
    held = []
    try:
        navigate(page, 'people')
        page.locator('#main tr', has_text='Alex Morgan').locator('.bulk-check').check()
        bar(page).get_by_role('button', name='Show their…').click()
        page.get_by_role('menu', name='Show their…').get_by_role('menuitem', name=re.compile('^Clinic permissions')).click()
        current_only(page)
        page.get_by_role('checkbox', name=re.compile('^Select all .* shown')).check()
        bar(page).get_by_role('button', name='Change…').click()
        row = change_row(page, re.compile('Ends|Last day|Until'), 'Set to')
        row.locator('.date-text').fill('31 Jan 2027')
        row.locator('.date-text').press('Tab')
        submit(page)
        group = panel(page).locator('.dt-group', has=page.locator('.bulk-group-fix'))
        expect(group).to_have_count(1)
        apply = panel(page).locator('.bulk-foot .button.primary')
        page.route('**/api/commands/preview', lambda route: held.append(route) if 'only' in (route.request.post_data_json or {}).get('payload', {}) else route.continue_())
        group.get_by_role('button', name='Skip all').click()   # A: skip
        group.locator('.bulk-split .button').first.click()      # B: the best fix
        assert len(held) == 2, len(held)
        with page.expect_response(lambda r: '/api/commands/preview' in r.url):
            held[1].continue_()                                 # B's reply
        expect(panel(page).locator('.badge', has_text='Fixed').first).to_be_visible()
        expect(apply).to_be_enabled()
        panel(page).get_by_role('button', name='Undo fix').first.click()   # C
        assert len(held) == 3, len(held)
        expect(apply).to_be_disabled()
        with page.expect_response(lambda r: '/api/commands/preview' in r.url):
            held[0].continue_()                                 # A's (older) reply
        settled(page, left=1)  # A handled; only C is still out
        expect(apply).to_be_disabled()                          # still waiting for C
        with page.expect_response(lambda r: '/api/commands/preview' in r.url):
            held[2].continue_()
        page.unroute('**/api/commands/preview')
        expect(apply).to_be_enabled()
        expect(panel(page).locator('.badge', has_text='Fixed')).to_have_count(0)   # C: the fix undone (not A's skip)
        expect(panel(page).locator('.bulk-sum')).to_contain_text('+ 1 to resolve +')
        expect(panel(page).locator('.bulk-sum')).not_to_contain_text('+ 1 no change +')
        assert not errors, errors
    finally:
        context.close()


def test_a_preview_begun_by_one_account_never_shows_under_the_next(browser, live_server, h):
    """Pre-review 3 #2: A starts Switch off on Services (the bulk limits still on their way), signs out; B signs in in
    the same tab. When the old reply arrives, no Preview of A's change opens for B."""
    context, page = sign_in(browser, live_server, 'admin', viewport=DESKTOP)
    errors = errors_of(page)
    held = []
    page.route('**/api/bulk/settings', lambda route: held.append(route))
    try:
        navigate(page, 'configuration')
        page.get_by_role('button', name=re.compile(r'^Services')).click()
        page.get_by_role('checkbox', name='Select all services shown').check()
        with page.expect_request('**/api/bulk/settings'):
            bar(page).get_by_role('button', name='Switch off').click()
        expect(page.locator('.dock-panel.bulk-panel')).to_have_count(0)
        assert held, 'the limits were asked'
        switch_account(page, 'coordinator')
        for route in held:
            route.continue_()
        page.unroute('**/api/bulk/settings')
        settled(page)  # the replies are handled
        expect(page.locator('.dock-panel.bulk-panel')).to_have_count(0)
        assert not [e for e in errors if 'SESSION' not in e and 'account changed' not in e], errors
    finally:
        context.close()


def test_a_late_count_never_revives_a_selection_cleared_after_a_rebuild(browser, live_server, h):
    """Pre-review 3 #3: Select all matching (the count on its way), the list drawn again (People and back), Select none;
    when the old count arrives and the list is drawn again, nothing is selected."""
    alex = h.seed['people'][0]
    leave_210(h, alex)
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    held = []
    try:
        their_leave(page)
        main = page.locator('#main')
        expect(page.locator('.bulk-more')).to_contain_text('Showing 200 of 210')
        page.route('**/api/bulk/resolve', lambda route: held.append(route))
        caret(main).get_by_role('menuitem', name=re.compile('^Select all matching')).click()
        expect(bar(page)).to_contain_text('200')
        navigate(page, 'people')
        page.go_back()
        expect(page.locator('.bulk-more')).to_contain_text('Showing 200 of 210')
        expect(bar(page)).to_contain_text('200')
        caret(main).get_by_role('menuitem', name='Select none').click()
        expect(bar(page)).to_be_hidden()
        assert held, 'the count was asked'
        for route in list(held):
            with page.expect_response(lambda r: '/api/bulk/resolve' in r.url):
                route.continue_()
        page.unroute('**/api/bulk/resolve')
        settled(page)  # the replies are handled
        navigate(page, 'people')
        page.go_back()
        expect(page.locator('.bulk-more')).to_contain_text('Showing 200 of 210')
        expect(bar(page)).to_be_hidden()
        assert not errors, errors
    finally:
        context.close()


def test_change_proposes_rechecks_rule_edits_made_after_it_opened(browser, live_server, h):
    """Review (b0bf) #3: Propose a change… is opened on a saved line, then the rule's Maximum is edited on the page
    (unsaved): Preview does not propose from the saved rule (it says so), and the unsaved Maximum stays."""
    from .test_browser_rule_catalogue import catalogue_rule, flag, line, selection, set_numbers
    flag(h)
    catalogue_rule(h, 'duties_per_month', [line({'max': {'value': 5, 'level': 'mandatory'}}, note='Everyone')])
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        navigate(page, 'rules/duties_per_month')
        main = page.locator('#main')
        main.locator('tr[data-row]').first.locator('input.bulk-check').check()
        selection(page).get_by_role('button', name='Propose a change…').click()
        row = change_row(page, 'Note', 'Set to')
        row.locator('.bulk-value input').fill('Changed in bulk')
        set_numbers(page, max=6)  # on the page, beside the open panel
        expect(main.locator('tr[data-row]').first).to_contain_text('6')
        submit(page)
        expect(page.locator('#toast-tray')).to_contain_text('unsaved edits on the Rules page')
        assert not [p for p in h.get('admin', '/api/catalogues')['policies'] if p.get('status') == 'proposed']
        expect(main.locator('tr[data-row]').first).to_contain_text('6')
        assert not errors, errors
    finally:
        context.close()



def test_rule_editors_wait_while_a_proposal_is_being_sent(browser, live_server, h):
    """Review (6cd6) #1: while Propose is on its way (held), the Rules page is busy and its editors cannot take an edit
    (it would be dropped by the new proposal); once the reply is drawn they work again."""
    from .test_browser_rule_catalogue import catalogue_rule, flag, line, selection
    flag(h)
    catalogue_rule(h, 'duties_per_month', [line({'max': {'value': 5, 'level': 'mandatory'}}, note='Everyone')])
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    held = []
    try:
        navigate(page, 'rules/duties_per_month')
        main = page.locator('#main')
        main.locator('tr[data-row]').first.locator('input.bulk-check').check()
        selection(page).get_by_role('button', name='Propose a change…').click()
        row = change_row(page, 'Note', 'Set to')
        row.locator('.bulk-value input').fill('Changed in bulk')
        submit(page)
        propose = panel(page).get_by_role('button', name=re.compile(r'^Propose 1'))
        expect(propose).to_be_visible()
        page.route('**/api/commands', lambda route: held.append(route) if route.request.method == 'POST' else route.continue_())
        propose.click()
        expect(main).to_have_attribute('aria-busy', 'true')
        remove = main.locator('.selection-bar button', has_text='Remove lines')
        expect(remove).to_have_count(1)  # there, but not taking clicks
        with pytest.raises(Exception):
            remove.click(timeout=1500)  # the page is inert: no edit is taken
        expect(main.locator('tr[data-row]')).to_have_count(1)
        assert held, 'the proposal was sent'
        for route in held:
            route.continue_()
        page.unroute('**/api/commands')
        expect(panel(page).get_by_role('heading', name='Proposed · waiting for approval')).to_be_visible()
        expect(main).not_to_have_attribute('aria-busy', 'true')
        assert not errors, errors
    finally:
        context.close()



def test_a_rule_stays_read_only_on_any_page_while_its_proposal_is_out(browser, live_server, h):
    """Review (407f) #1: Propose is held, the panel closed and the rule opened again from Rules: the page drawn afresh
    says the change is being proposed and offers no editing (an edit then would be lost to the proposal); once the
    reply is in, the proposal is there and nothing was dropped."""
    from .test_browser_rule_catalogue import catalogue_rule, flag, line, selection
    flag(h)
    rule = catalogue_rule(h, 'duties_per_month', [line({'max': {'value': 5, 'level': 'mandatory'}}, note='Everyone')])
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    held = []
    try:
        navigate(page, 'rules/duties_per_month')
        main = page.locator('#main')
        main.locator('tr[data-row]').first.locator('input.bulk-check').check()
        selection(page).get_by_role('button', name='Propose a change…').click()
        row = change_row(page, 'Note', 'Set to')
        row.locator('.bulk-value input').fill('Changed in bulk')
        submit(page)
        propose = panel(page).get_by_role('button', name=re.compile(r'^Propose 1'))
        expect(propose).to_be_visible()
        page.route('**/api/commands', lambda route: held.append(route) if route.request.method == 'POST' else route.continue_())
        with page.expect_request(lambda r: r.method == 'POST' and r.url.endswith('/api/commands')):
            propose.click()
        dock = page.locator('.dock-panel.bulk-panel')
        dock.locator('h2').first.focus()
        page.keyboard.press('Escape')
        discard = dock.get_by_role('button', name='Discard changes')
        if discard.count():
            discard.click()  # its unsaved-close guard (the request stays out)
        expect(dock).to_have_count(0)
        navigate(page, 'people')
        navigate(page, 'rules/duties_per_month')
        main = page.locator('#main')
        expect(main).to_contain_text('is being proposed')
        expect(main.locator('tr[data-row]').first).to_be_visible()
        expect(main.locator('tr[data-row] input.bulk-check')).to_have_count(0)   # no selection, no editing
        expect(main.locator('.rc-new-line')).to_have_count(0)
        assert len(held) == 1, len(held)
        for route in held:
            route.continue_()
        page.unroute('**/api/commands')
        settled(page)
        policies = h.get('admin', '/api/catalogues')['policies']
        assert [p['definition']['rule']['lines'][0]['note'] for p in policies if p.get('status') == 'proposed'] == ['Changed in bulk']
        assert not errors, errors
    finally:
        context.close()


def test_narrowing_a_child_lists_parents_keeps_only_their_selected_rows(browser, live_server, h):
    """Review (407f) #2: Alex's and Jordan's leave both ticked, then Alex removed from the parents: the list is Jordan's
    and the selection is Jordan's ticked row only (Change sends nothing of Alex's)."""
    alex, jordan = h.seed['people'][0], h.seed['people'][1]
    kinds = h.get('admin', '/api/catalogues')['leave_types']
    for who, day in ((alex, '2027-04-01'), (jordan, '2027-04-02')):
        h.command('coordinator', 'bulk.apply', {'item_kind': 'availability.record', 'reason': 'Test setup',
                  'common': {'person_id': who, 'kind': 'recorded_leave', 'leave_type_id': kinds[0]['id'], 'private_reason': 'Narrowed'},
                  'items': [{'payload': {'start': f'{day}T08:00:00+03:00', 'end': f'{day}T09:00:00+03:00'}}]})
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    sent = []
    page.on('request', lambda r: sent.append(r.post_data_json) if r.method == 'POST' and '/api/commands/preview' in r.url else None)
    try:
        their_leave_of(page, 'Alex Morgan', 'Jordan Lee')
        every_status(page)
        main = page.locator('#main')
        rows = {who: main.locator('tbody tr', has_text=day).filter(has_text=name)
                for who, day, name in ((alex, '1 Apr 2027', 'Alex'), (jordan, '2 Apr 2027', 'Jordan'))}
        ids = {who: r.get_attribute('data-row') for who, r in rows.items()}
        for r in rows.values():
            r.locator('.bulk-check').check()
        expect(bar(page)).to_contain_text('2')
        page.get_by_role('button', name=re.compile(r'^For 2 selected')).click()
        page.get_by_role('menu', name='Parents').get_by_role('menuitem', name=re.compile('^Remove Alex')).click()
        expect(page.get_by_role('button', name=re.compile(r'^For 1 selected'))).to_be_visible()
        expect(main.locator(f'tr[data-row="{ids[alex]}"]')).to_have_count(0)
        expect(bar(page)).to_contain_text('1')
        expect(main.locator(f'tr[data-row="{ids[jordan]}"] .bulk-check')).to_be_checked()
        bar(page).get_by_role('button', name='Change…').click()
        row = change_row(page, re.compile('Leave type', re.I), 'Set to')
        row.locator('.bulk-value select').select_option(label=kinds[1]['name'])
        submit(page)
        settled(page)
        assert [i['target_id'] for i in sent[-1]['payload']['items']] == [ids[jordan]], sent[-1]
        assert not errors, errors
    finally:
        context.close()



def test_narrowing_parents_keeps_a_selected_row_beyond_the_first_page(browser, live_server, h):
    """#467 SHOULD 9: Jordan's 205th leave row (loaded by Show more) ticked, then Alex removed from the parents: the pick
    is checked against the whole narrowed list, not only its first page, and stays selected."""
    alex, jordan = h.seed['people'][0], h.seed['people'][1]
    kinds = h.get('admin', '/api/catalogues')['leave_types']
    days = {alex: ['2027-03-01'], jordan: [f'2027-{1 + n // 28:02d}-{1 + n % 28:02d}' for n in range(205)]}
    for who, dates in days.items():
        h.command('coordinator', 'bulk.apply', {'item_kind': 'availability.record', 'reason': 'Test setup',
                  'common': {'person_id': who, 'kind': 'recorded_leave', 'leave_type_id': kinds[0]['id'], 'private_reason': 'Paged'},
                  'items': [{'payload': {'start': f'{d}T08:00:00+03:00', 'end': f'{d}T09:00:00+03:00'}} for d in dates]})
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        their_leave_of(page, 'Alex Morgan', 'Jordan Lee')
        every_status(page)
        main, more = page.locator('#main'), page.locator('.bulk-more')
        # A row the server sent on its second page (offset 200), not one that only reads like a late date.
        later = lambda r: '/api/bulk/rows' in r.url and r.request.method == 'POST' and (r.request.post_data_json or {}).get('offset') == 200
        with page.expect_response(later) as reply:
            more.get_by_role('button', name=re.compile(r'^Show \d+ more')).click()
        expect(more).to_be_empty()
        late = main.locator(','.join(f'tbody tr[data-row="{r["id"]}"]' for r in reply.value.json()['rows'])).filter(has_text='Jordan').first
        while not late.count():  # the list's own pages: the row is on one of the later ones
            main.get_by_role('button', name='Next page').click()
        late_id = late.get_attribute('data-row')
        late.locator('.bulk-check').check()
        expect(bar(page)).to_contain_text('1')
        shown = main.locator('tbody').element_handle()
        page.get_by_role('button', name=re.compile(r'^For 2 selected')).click()
        page.get_by_role('menu', name='Parents').get_by_role('menuitem', name=re.compile('^Remove Alex')).click()
        expect(page.get_by_role('button', name=re.compile(r'^For 1 selected'))).to_be_visible()
        expect(bar(page)).to_contain_text('1')
        page.wait_for_function('t => !t.isConnected', arg=shown)  # the list is read afresh (Loading…), then drawn
        expect(main.locator('tbody tr').first).to_be_visible()
        kept = main.locator(f'tr[data-row="{late_id}"]')
        if not kept.count():  # Alex's row gone, the kept row can move back across a 50-row page boundary
            main.get_by_role('button', name='Previous page', exact=True).click()
        expect(kept.locator('.bulk-check')).to_be_checked()
        assert not errors, errors
    finally:
        context.close()


def held_rule_proposal(page, held, rule_route):
    """Hold the proposal's POST, close its panel and open the rule afresh (navigating away and back)."""
    dock = page.locator('.dock-panel.bulk-panel')
    dock.locator('h2').first.focus()
    page.keyboard.press('Escape')
    discard = dock.get_by_role('button', name='Discard changes')
    if discard.count():
        discard.click()  # its unsaved-close guard (the request stays out)
    expect(dock).to_have_count(0)
    navigate(page, 'people')
    navigate(page, rule_route)
    assert len(held) == 1, len(held)


def test_a_first_rule_version_proposed_from_people_holds_that_rule_read_only(browser, live_server, h):
    """Review (e51a) #1: Set a rule number on a rule not used yet (a first version: no policy id) is held; the rule's page
    drawn meanwhile is read only with the notice, as for a saved rule, and the proposal arrives once released."""
    from .test_browser_rule_catalogue import flag
    flag(h, legacy=False)
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    held = []
    try:
        navigate(page, 'people')
        page.locator('#main tr', has_text='Alex Morgan').locator('.bulk-check').check()
        bar(page).get_by_role('button', name='More actions').click()
        page.get_by_role('menu', name='More actions').get_by_role('menuitem', name='Set a rule number…').click()
        dialog = page.get_by_role('dialog', name=re.compile('^Set a rule number'))
        dialog.get_by_label('Rule', exact=True).select_option(label='Workload limit')
        dialog.get_by_label('Maximum', exact=True).fill('7')
        dialog.get_by_label('Reason', exact=True).fill('Alex: at most 7')
        dialog.get_by_role('button', name='Preview', exact=True).click()
        propose = panel(page).locator('.bulk-foot .button.primary')
        expect(propose).to_have_text(re.compile(r'^Propose'))
        page.route('**/api/commands', lambda route: held.append(route) if route.request.method == 'POST' else route.continue_())
        with page.expect_request(lambda r: r.method == 'POST' and r.url.endswith('/api/commands')):
            propose.click()
        held_rule_proposal(page, held, 'rules/workload_limit')
        main = page.locator('#main')
        expect(main).to_contain_text('is being proposed')
        expect(main.locator('.rc-new-line')).to_have_count(0)
        for route in held:
            route.continue_()
        page.unroute('**/api/commands')
        settled(page)
        assert [p for p in h.get('admin', '/api/catalogues')['policies'] if p.get('status') == 'proposed']
        assert not errors, errors
    finally:
        context.close()


def test_a_failed_proposal_gives_the_rule_page_drawn_meanwhile_back_its_editing(browser, live_server, h):
    """Review (e51a) #6: Propose is held, the rule opened afresh (read only, "being proposed"), and the request fails:
    the page is drawn again, editable, without the notice."""
    from .test_browser_rule_catalogue import catalogue_rule, flag, line, selection
    flag(h)
    catalogue_rule(h, 'duties_per_month', [line({'max': {'value': 5, 'level': 'mandatory'}}, note='Everyone')])
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    held = []
    try:
        navigate(page, 'rules/duties_per_month')
        main = page.locator('#main')
        main.locator('tr[data-row]').first.locator('input.bulk-check').check()
        selection(page).get_by_role('button', name='Propose a change…').click()
        row = change_row(page, 'Note', 'Set to')
        row.locator('.bulk-value input').fill('Changed in bulk')
        submit(page)
        propose = panel(page).get_by_role('button', name=re.compile(r'^Propose 1'))
        expect(propose).to_be_visible()
        page.route('**/api/commands', lambda route: held.append(route) if route.request.method == 'POST' else route.continue_())
        with page.expect_request(lambda r: r.method == 'POST' and r.url.endswith('/api/commands')):
            propose.click()
        held_rule_proposal(page, held, 'rules/duties_per_month')
        main = page.locator('#main')
        expect(main).to_contain_text('is being proposed')
        for route in held:
            route.fulfill(status=503, json={'error': {'code': 'CATALOGUE_BUSY', 'message': 'The system is busy, so this change was not saved. Try again.'}})  # the proposal fails (not saved: an unknown outcome would be confirmed)
        page.unroute('**/api/commands')
        settled(page)
        main = page.locator('#main')
        expect(main).not_to_contain_text('is being proposed')
        expect(main.locator('.rc-new-line')).to_be_visible()
        expect(main.locator('tr[data-row] input.bulk-check').first).to_be_visible()
        assert not [p for p in h.get('admin', '/api/catalogues')['policies'] if p.get('status') == 'proposed']
    finally:
        context.close()



def test_a_proposal_failing_while_the_rule_page_loads_leaves_it_editable(browser, live_server, h):
    """Pre-review (467b) #6: Propose is held; the rule is opened again with its catalogue read held (the page waits behind
    its loading skeleton); the catalogue arrives (the page is built read only) and the proposal fails before the page is
    shown: once on screen the page is drawn again, editable."""
    from .test_browser_rule_catalogue import catalogue_rule, flag, line, selection
    flag(h)
    catalogue_rule(h, 'duties_per_month', [line({'max': {'value': 5, 'level': 'mandatory'}}, note='Everyone')])
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    held, reads = [], []
    try:
        navigate(page, 'rules/duties_per_month')
        main = page.locator('#main')
        main.locator('tr[data-row]').first.locator('input.bulk-check').check()
        selection(page).get_by_role('button', name='Propose a change…').click()
        row = change_row(page, 'Note', 'Set to')
        row.locator('.bulk-value input').fill('Changed in bulk')
        submit(page)
        propose = panel(page).get_by_role('button', name=re.compile(r'^Propose 1'))
        expect(propose).to_be_visible()
        page.route('**/api/commands', lambda route: held.append(route) if route.request.method == 'POST' else route.continue_())
        with page.expect_request(lambda r: r.method == 'POST' and r.url.endswith('/api/commands')):
            propose.click()
        dock = page.locator('.dock-panel.bulk-panel')
        dock.locator('h2').first.focus()
        page.keyboard.press('Escape')
        discard = dock.get_by_role('button', name='Discard changes')
        if discard.count():
            discard.click()
        expect(dock).to_have_count(0)
        navigate(page, 'people')
        page.route('**/api/rules/catalogue**', lambda route: reads.append(route))
        page.evaluate("location.hash = 'rules/duties_per_month'")
        expect(page.locator('#main .page-sk:not(.sk-wait)').first).to_be_visible()  # the loading skeleton, shown
        assert reads, 'the catalogue was asked'
        # The page is built (read only) as the catalogue arrives; the skeleton that showed stays on screen a moment longer
        # (loading.ts settle: shown 400 ms), and the proposal fails around that moment.
        with page.expect_response(lambda r: '/api/rules/catalogue' in r.url):
            for route in reads:
                route.continue_()
        page.unroute('**/api/rules/catalogue**')
        # The proposal fails as the page is built: behind its skeleton or just after it (a loaded runner may have shown
        # the page already). Either way, once on screen the page must end editable.
        for route in held:
            route.fulfill(status=503, json={'error': {'code': 'CATALOGUE_BUSY', 'message': 'The system is busy, so this change was not saved. Try again.'}})
        page.unroute('**/api/commands')
        settled(page)
        main = page.locator('#main')
        expect(main.locator('.rc-new-line')).to_be_visible()
        expect(main).not_to_contain_text('is being proposed')
    finally:
        context.close()



def close_held_panel(page):
    dock = page.locator('.dock-panel.bulk-panel')
    dock.locator('h2').first.focus()
    page.keyboard.press('Escape')
    discard = dock.get_by_role('button', name='Discard changes')
    if discard.count():
        discard.click()
    expect(dock).to_have_count(0)


def test_a_rule_stays_read_only_until_every_proposal_of_it_has_settled(browser, live_server, h):
    """Review (67a5) #3: two proposals of one rule out together (Change… on its page, then a Note edit in the Bulk
    editor's Rule lines sheet); the first fails while the second is held: the rule stays read only until the second is
    in too."""
    from .test_browser_rule_catalogue import flag, line, selection
    from .test_browser_rule_catalogue_v2 import v2_rule
    flag(h, legacy=False)
    v2_rule(h, 'workload_limit', [line({'max': {'value': 5, 'level': 'mandatory'}}, measure='duties', period={'kind': 'calendar_week'}, note='Sheet line')])
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    held = []
    try:
        navigate(page, 'rules/workload_limit')
        main = page.locator('#main')
        main.locator('tr[data-row]').first.locator('input.bulk-check').check()
        selection(page).get_by_role('button', name='Propose a change…').click()
        row = change_row(page, 'Note', 'Set to')
        row.locator('.bulk-value input').fill('Changed in bulk')
        submit(page)
        propose = panel(page).get_by_role('button', name=re.compile(r'^Propose 1'))
        expect(propose).to_be_visible()
        page.route('**/api/commands', lambda route: held.append(route) if route.request.method == 'POST' else route.continue_())
        with page.expect_request(lambda r: r.method == 'POST' and r.url.endswith('/api/commands')):
            propose.click()                                     # A
        close_held_panel(page)
        # B: the same rule's line, its Note edited in the Bulk editor's Rule lines sheet.
        from .test_browser_bulk_sheet import open_sheet, cell
        sheet_main, grid = open_sheet(page, 'Rule lines')
        note = cell(grid, 'Note', 'Sheet line')
        note.click()
        page.keyboard.press('Enter')
        box = sheet_main.get_by_label(re.compile('^Note for '))
        box.fill('Changed in the sheet')
        box.press('Enter')
        sheet_main.locator('.stage-bar').get_by_role('button', name='Save 1 change').click()
        second = panel(page).locator('.bulk-foot .button.primary')
        expect(second).to_have_text(re.compile(r'^Propose 1'))
        with page.expect_request(lambda r: r.method == 'POST' and r.url.endswith('/api/commands')):
            second.click()                                      # B
        close_held_panel(page)
        assert len(held) == 2, len(held)
        held[0].fulfill(status=503, json={'error': {'code': 'CATALOGUE_BUSY', 'message': 'The system is busy, so this change was not saved. Try again.'}})  # A fails; B is still out
        settled(page, left=1)
        navigate(page, 'rules/workload_limit')
        main = page.locator('#main')
        expect(main).to_contain_text('is being proposed')
        expect(main.locator('.rc-new-line')).to_have_count(0)
        held[1].continue_()                                     # B is in
        page.unroute('**/api/commands')
        settled(page)
        expect(page.locator('#main')).not_to_contain_text('is being proposed')
        assert [p for p in h.get('admin', '/api/catalogues')['policies'] if p.get('status') == 'proposed']
    finally:
        context.close()


def test_catalogue_bulk_undo_sends_the_servers_own_reverse(browser, live_server, h):
    """Review (67a5) #4: Undo of a catalogue bulk update (Configuration › Services, Switch off) sends the reverse the
    server returned (its receipt binding, undo_of, included), not one rebuilt in the client."""
    context, page = sign_in(browser, live_server, 'admin', viewport=DESKTOP)
    errors = errors_of(page)
    sent = []
    page.on('request', lambda r: sent.append(r.post_data_json) if r.method == 'POST' and r.url.endswith('/api/commands') else None)
    replies = []
    page.on('response', lambda r: replies.append(r) if r.request.method == 'POST' and r.url.endswith('/api/commands') else None)
    try:
        services = h.get('admin', '/api/catalogues')['services']
        result = page.evaluate("""async ids => { const m = await window.importBundle('api.js');
            return await m.command('catalogue.bulk_update', {catalogue: 'services', items: ids, active: false, reason: 'Switch off for the test'}); }""",
            [{'id': s['id'], 'revision': s['revision']} for s in services[:1]])
        assert result['undo']['payload'].get('undo_of'), result['undo']
        toast = page.locator('.toast').filter(has=page.get_by_role('button', name='Undo')).last
        toast.get_by_role('button', name='Undo').click()
        expect(page.locator('.toast', has_text=re.compile('Undone|undone')).first).to_be_visible()
        undo = sent[-1]
        assert undo['kind'] == 'bulk.apply' and undo['payload'] == result['undo']['payload'], (undo, result['undo'])
        assert next(s for s in h.get('admin', '/api/catalogues')['services'] if s['id'] == services[0]['id'])['active'] is True
        assert not errors, errors
    finally:
        context.close()


def test_department_settings_keep_the_bulk_limits_read_only_when_they_could_not_be_read(browser, live_server, h):
    """Review (67a5) #5: the bulk limits fail to load: the form says so and the limits cannot be edited (no default
    stands in for them); another setting still saves, and the saved limits are untouched."""
    before = h.get('admin', '/api/bulk/settings')['settings']
    context, page = sign_in(browser, live_server, 'admin', viewport=DESKTOP)
    try:
        page.route('**/api/bulk/settings', lambda route: route.fulfill(status=503, content_type='application/json',
                   body='{"error":{"code":"UNAVAILABLE","message":"Settings are unavailable","details":null}}')
                   if route.request.method == 'GET' else route.continue_())
        navigate(page, 'configuration/department')
        pane = page.locator('.config-pane')
        department_tab(page, 'Advanced')
        expect(pane).to_contain_text('Couldn’t load the bulk limits.')
        direct = pane.get_by_label('Most applied directly', exact=True)
        expect(direct).to_be_disabled()
        expect(direct).to_have_value('')
        department_tab(page, 'General')
        name = pane.get_by_label('Department name', exact=True)
        name.fill(name.input_value() + ' ')
        name.fill(name.input_value().rstrip() + ' Unit')
        save_department_settings(page, 'Rename while the limits are unavailable')
        assert h.get('admin', '/api/bulk/settings')['settings'] == before
    finally:
        context.close()



def test_another_credentialer_sees_opens_and_approves_a_change_sent_for_review(browser, live_server, h):
    """Pre-review (full) #3: credentialer A's own clinic permission is sent for independent approval; credentialer B, who
    has no kept changes of their own, sees it waiting on the Clinic permissions list, opens it from the Inbox
    notification (the same Resolve flow, by its staged id) and approves it."""
    from apps.api import models as m
    from apps.api.db import retry_write, transaction
    from apps.worker import main as worker
    from sqlalchemy import select
    from .governance_support import identity
    person = h.seed['people'][0]
    identity(h, 'own_credentialer', ['credentialer'], person=person)
    service = h.configure('services', {'name': 'Reviewed clinic', 'starts_on': '2020-01-01'})['record']
    payload = {'item_kind': 'eligibility.create', 'reason': 'Request independent clinical permission review',
               'items': [{'payload': {'person_id': person, 'service_id': service['id'], 'starts_on': '2026-10-01'}}]}
    sent = h.command('own_credentialer', 'bulk.apply', {**payload, 'resolutions': {'0': 'send_for_approval'}})
    review = sent['reviews'][0]
    retry_write(h.app.state.engine, lambda db: worker.dispatch_batch(db))
    assert h.get('credentialer', '/api/me').get('bulk_review_count') == 1  # the signed-in summary says one waits
    context, page = sign_in(browser, live_server, 'credentialer', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        # The list of clinic permissions says a change waits for this person's review.
        navigate(page, 'people')
        page.locator('#main tr', has_text='Alex Morgan').locator('.bulk-check').check()
        bar(page).get_by_role('button', name='Show their…').click()
        page.get_by_role('menu', name='Show their…').get_by_role('menuitem', name=re.compile('^Clinic permissions')).click()
        note = page.locator('#main .bulk-staged-note')
        expect(note).to_contain_text('sent for independent review')
        expect(note.get_by_role('button', name='Review')).to_be_visible()
        # The Inbox notification opens it in the same flow, by its staged id.
        navigate(page, 'inbox')
        page.locator('.note-main', has_text='independent reviewer').first.click()
        dock = page.locator('.dock-panel.bulk-panel')
        expect(dock.get_by_role('heading', name='Preview — nothing saved yet')).to_be_visible()
        apply = dock.locator('.bulk-foot .button.primary')
        expect(apply).to_have_text(re.compile(r'^Apply ready rows \(1\)'))
        apply.click()
        expect(page.locator('.toast', has_text=re.compile('changed'))).to_be_visible()
        with transaction(h.app.state.engine) as db:
            assert db.get(m.BulkStaged, review['id']).status == 'applied'
            assert db.scalar(select(m.Eligibility.id).where(m.Eligibility.service_id == service['id']))
        assert not errors, errors
    finally:
        context.close()



def test_a_review_notification_waits_for_the_unsaved_editor_open_beside_it(browser, live_server, h):
    """Review (5ea4) #2: a notification's staged change opens through the docked panel's unsaved-changes guard: Keep
    editing keeps what was typed; only an explicit Discard replaces the editor with the change to review."""
    from apps.api.db import retry_write
    from apps.worker import main as worker
    from .governance_support import identity
    person = h.seed['people'][0]
    identity(h, 'own_credentialer', ['credentialer'], person=person)
    service = h.configure('services', {'name': 'Guarded review clinic', 'starts_on': '2020-01-01'})['record']
    h.command('own_credentialer', 'bulk.apply', {'item_kind': 'eligibility.create', 'reason': 'Request independent review',
              'items': [{'payload': {'person_id': person, 'service_id': service['id'], 'starts_on': '2026-10-01'}}],
              'resolutions': {'0': 'send_for_approval'}})
    retry_write(h.app.state.engine, lambda db: worker.dispatch_batch(db))
    context, page = sign_in(browser, live_server, 'credentialer', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        navigate(page, 'people')
        page.locator('#main tr', has_text='Alex Morgan').locator('.bulk-check').check()
        bar(page).get_by_role('button', name='Change…').click()
        reason = panel(page).get_by_label('Reason', exact=True)
        reason.fill('Typed, not saved')

        def open_note():
            page.locator('.topbar .bell').click()
            page.locator('.note-pop .note-main', has_text='independent reviewer').first.click()
            expect(panel(page).get_by_role('alert')).to_contain_text('Not saved.')
        open_note()
        panel(page).get_by_role('button', name='Keep editing', exact=True).click()
        expect(panel(page).get_by_role('alert')).to_have_count(0)
        expect(reason).to_have_value('Typed, not saved')
        expect(panel(page).get_by_role('heading', name='Preview — nothing saved yet')).to_have_count(0)
        open_note()
        panel(page).get_by_role('button', name='Discard changes', exact=True).click()
        expect(panel(page).get_by_role('heading', name='Preview — nothing saved yet')).to_be_visible()
        expect(panel(page).get_by_label('Reason', exact=True)).to_have_count(0)
        assert not errors, errors
    finally:
        context.close()


def test_a_late_apply_never_closes_the_next_review_opened_in_the_same_dock(browser, live_server, h):
    """Review (5ea3) BLOCKING: review A's Apply is held; review B is opened from the bell into the same dock and its
    problem skipped (not applied yet); A's success then arrives: B stays shown with its Skip, and A's toast still says done."""
    from apps.api.db import retry_write
    from apps.worker import main as worker
    from .governance_support import identity
    person = h.seed['people'][0]
    identity(h, 'own_credentialer', ['credentialer'], person=person)
    first, second = (h.configure('services', {'name': name, 'starts_on': '2020-01-01'})['record']
                     for name in ('Held review clinic', 'Later review clinic'))
    for service in (first, second):
        h.command('own_credentialer', 'bulk.apply', {'item_kind': 'eligibility.create', 'reason': 'Request independent review',
                  'items': [{'payload': {'person_id': person, 'service_id': service['id'], 'starts_on': '2026-10-01'}}],
                  'resolutions': {'0': 'send_for_approval'}})
    # B's row now overlaps a permission given meanwhile: its preview has a problem to skip.
    h.command('credentialer', 'eligibility.create', {'person_id': person, 'service_id': second['id'], 'starts_on': '2026-10-01', 'ends_on': None})
    retry_write(h.app.state.engine, lambda db: worker.dispatch_batch(db))
    context, page = sign_in(browser, live_server, 'credentialer', viewport=DESKTOP)
    errors = errors_of(page)
    held = []
    try:
        navigate(page, 'people')
        notes = page.locator('.note-pop .note-main', has_text='independent reviewer')
        dock = page.locator('.dock-panel.bulk-panel')
        heading = dock.get_by_role('heading', name='Preview — nothing saved yet')

        def open_review(overlap):
            for i in range(2):  # the notes do not name the clinic: B is the review with the overlap to resolve
                page.locator('.topbar .bell').click()
                expect(notes).to_have_count(2)
                page.evaluate("document.querySelector('.dock-inner')?.classList.add('was-open')")  # tells the new body from the old
                notes.nth(i).click()
                fresh = dock.locator('.dock-inner:not(.was-open)')
                discard = dock.get_by_role('button', name='Discard changes', exact=True)
                expect(fresh.locator('h3').or_(discard)).to_be_visible()
                if discard.count():
                    discard.click()
                expect(fresh.get_by_role('heading', name='Preview — nothing saved yet')).to_be_visible()
                if bool(fresh.locator('.bulk-group-fix').count()) == overlap:
                    return
            raise AssertionError(f'no review with overlap={overlap}')
        open_review(False)
        apply = dock.locator('.bulk-foot .button.primary')
        expect(apply).to_have_text(re.compile(r'^Apply ready rows \(1\)'))
        page.route('**/api/commands', lambda route: held.append(route) if route.request.method == 'POST' else route.continue_())
        with page.expect_request(lambda r: r.method == 'POST' and r.url.endswith('/api/commands')):
            apply.click()
        open_review(True)
        group = dock.locator('.dt-group', has=page.locator('.bulk-group-fix'))
        expect(group).to_have_count(1)
        group.get_by_role('button', name='Skip all').click()
        expect(dock.locator('.bulk-sum')).to_contain_text('+ 1 no change +')  # the skipped row
        assert len(held) == 1, len(held)
        with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/api/commands')):
            held[0].continue_()
        page.unroute('**/api/commands')
        expect(page.locator('.toast', has_text=re.compile('changed'))).to_be_visible()  # A's own success
        settled(page)
        expect(heading).to_be_visible()  # B is still shown, its Skip kept
        expect(dock.locator('.bulk-sum')).to_contain_text('+ 1 no change +')
        assert not errors, errors
    finally:
        context.close()


def test_an_old_accounts_late_proposal_never_releases_the_next_accounts_rule(browser, live_server, h):
    """Review (4d5b) #2: A's proposal of a rule is held; A signs out and B signs in in the same tab and proposes the same
    rule (held too); A's reply then arrives: B's rule stays read only until B's own proposal is in."""
    from .test_browser_rule_catalogue import catalogue_rule, flag, line, selection
    flag(h)
    catalogue_rule(h, 'duties_per_month', [line({'max': {'value': 5, 'level': 'mandatory'}}, note='Everyone')])
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    held = []

    def propose_note(text):
        navigate(page, 'rules/duties_per_month')
        main = page.locator('#main')
        main.locator('tr[data-row]').first.locator('input.bulk-check').check()
        selection(page).get_by_role('button', name='Propose a change…').click()
        row = change_row(page, 'Note', 'Set to')
        row.locator('.bulk-value input').fill(text)
        submit(page)
        propose = panel(page).get_by_role('button', name=re.compile(r'^Propose 1'))
        expect(propose).to_be_visible()
        with page.expect_request(lambda r: r.method == 'POST' and r.url.endswith('/api/commands')):
            propose.click()
        close_held_panel(page)
    try:
        page.route('**/api/commands', lambda route: held.append(route) if route.request.method == 'POST' and
                   (route.request.post_data_json or {}).get('kind') == 'bulk.apply' else route.continue_())
        propose_note('From A')                                  # A
        switch_account(page, 'admin')
        propose_note('From B')                                  # B, the same rule
        assert len(held) == 2, len(held)
        with page.expect_response(lambda r: r.url.endswith('/api/commands')):
            held[0].continue_()                                 # A's late reply
        settled(page, left=1)
        navigate(page, 'people')
        navigate(page, 'rules/duties_per_month')
        expect(page.locator('#main')).to_contain_text('is being proposed')
        held[1].continue_()
        page.unroute('**/api/commands')
        settled(page)
        expect(page.locator('#main')).not_to_contain_text('is being proposed')
    finally:
        context.close()


def test_a_failed_limits_read_sends_every_change_through_preview(browser, live_server, h):
    """Review (4d5b) #3: when the bulk limits cannot be read, a small clean change is not applied at once (no default
    of 50): it opens Preview, as a department set to 0 would."""
    context, page = sign_in(browser, live_server, 'admin', viewport=DESKTOP)
    errors = errors_of(page)
    try:
        page.route('**/api/bulk/settings', lambda route: route.fulfill(status=503, content_type='application/json',
                   body='{"error":{"code":"UNAVAILABLE","message":"Settings are unavailable","details":null}}'))
        navigate(page, 'configuration')
        page.get_by_role('button', name=re.compile(r'^Services')).click()
        page.get_by_role('checkbox', name='Select all services shown').check()
        bar(page).get_by_role('button', name='Change…').click()
        row = change_row(page, 'Description', 'Set to')
        row.locator('textarea, input').first.fill('Read through Preview')
        submit(page)
        expect(panel(page).get_by_role('heading', name='Preview — nothing saved yet')).to_be_visible()
        assert all(s.get('description') != 'Read through Preview' for s in h.get('admin', '/api/catalogues')['services'])
        assert not [e for e in errors if 'unavailable' not in e.lower()], errors
    finally:
        context.close()



def test_select_all_matching_under_show_changed_takes_only_the_changed_rows(browser, live_server, h):
    """Review (1c7f) #3: Alex's 21 leave records, one changed by a bulk change; "Show changed · 1" shows that one, and
    Select all matching then selects 1 (not the server's 21), so Change… sends that one record."""
    alex = h.seed['people'][0]
    kinds = h.get('admin', '/api/catalogues')['leave_types']
    days = [f'2027-05-{d:02d}' for d in range(1, 22)]
    h.command('coordinator', 'bulk.apply', {'item_kind': 'availability.record', 'reason': 'Test setup',
              'common': {'person_id': alex, 'kind': 'recorded_leave', 'leave_type_id': kinds[0]['id'], 'private_reason': 'Changed view'},
              'items': [{'payload': {'start': f'{d}T08:00:00+03:00', 'end': f'{d}T09:00:00+03:00'}} for d in days]})
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = errors_of(page)
    sent = []
    page.on('request', lambda r: sent.append(r.post_data_json) if r.method == 'POST' and '/api/commands/preview' in r.url else None)
    try:
        their_leave(page)
        main = page.locator('#main')
        changed = main.locator('tbody tr[data-row]').first
        changed_id = changed.get_attribute('data-row')
        changed.locator('.bulk-check').check()
        bar(page).get_by_role('button', name='Change…').click()
        row = change_row(page, re.compile('Leave type', re.I), 'Set to')
        row.locator('.bulk-value select').select_option(label=kinds[1]['name'])
        expect(panel(page).locator('.bulk-form .bulk-foot .button.primary')).to_have_text('Apply 1 change')
        submit(page)
        expect(page.locator('.toast', has_text=re.compile('changed'))).to_be_visible()
        expect(panel(page)).to_have_count(0)
        settled(page)
        main.get_by_role('button', name=re.compile(r'^Show changed · 1')).click()
        expect(main.locator('tbody tr[data-row]')).to_have_count(1)
        menu = caret(main)
        menu.get_by_role('menuitem', name=re.compile('^Select all')).click()
        settled(page)
        expect(bar(page)).to_contain_text('1')
        expect(bar(page)).not_to_contain_text('21')
        bar(page).get_by_role('button', name='Change…').click()
        row = change_row(page, re.compile('Leave type', re.I), 'Set to')
        row.locator('.bulk-value select').select_option(label=kinds[0]['name'])
        expect(panel(page).locator('.bulk-form .bulk-foot .button.primary')).to_have_text('Apply 1 change')
        submit(page)
        settled(page)
        payload = sent[-1]['payload']
        assert 'selection' not in payload and [i['target_id'] for i in payload['items']] == [changed_id], payload
        assert not errors, errors
    finally:
        context.close()


def test_authorise_clinics_fits_the_change_panel(browser, live_server, h):
    """Release QA 2026-10-20: People › Change… › Authorise clinics keeps its clinic and both dates inside the panel
    (each calendar button inside its input)."""
    context, page = sign_in(browser, live_server, 'credentialer', viewport=DESKTOP)
    try:
        navigate(page, 'people')
        page.get_by_role('checkbox', name='Select all people shown').check()
        bar(page).get_by_role('button', name='Change…').click()
        row = change_row(page, re.compile('^Authorise'))
        dock = panel(page).bounding_box()
        for part in row.locator('.bulk-value > *').all():
            b = part.bounding_box()
            assert b['x'] + b['width'] <= dock['x'] + dock['width'], (b, dock)
        for box in row.locator('.date-box').all():
            text, button = box.locator('.date-text').bounding_box(), box.locator('.date-open').bounding_box()
            assert button['x'] + button['width'] <= text['x'] + text['width'] + 1, (text, button)
    finally:
        context.close()


def test_now_says_until_changed_for_a_blank_last_day(browser, live_server, h):
    """A blank last day is Until changed in "Now" (never "none"), its count apart from the value."""
    context, page = sign_in(browser, live_server, 'credentialer', viewport=DESKTOP)
    try:
        open_their_permissions(page)
        page.get_by_role('checkbox', name=re.compile('^Select all .* shown')).check()
        bar(page).get_by_role('button', name='Change…').click()
        row = change_row(page, re.compile('Ends|Last day|Until'), 'Set to')
        expect(row.locator('.now-values .now-chip').filter(has_text='Until changed')).to_have_text([re.compile(r'^Until changed: \d+$')])
        expect(row.locator('.now-values')).not_to_contain_text('none')
    finally:
        context.close()
