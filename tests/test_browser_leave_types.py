"""Leave types in the web client: Configuration › Leave types (admin), the one Type picker on leave forms (staff and
coordinators), and the type's name on Requests, Home and the People timeline as the department's leave setting allows."""
import re
import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate, open_config, pick, save_dialog  # noqa: F401
from .test_browser import whole_list
from .test_browser_requests import day, published, rows, row, new_request
from .test_leave_disclosure import setting
from .test_leave_types import selected, value

pytestmark = pytest.mark.browser
PHONE = {'width': 390, 'height': 844}
NAMES = ['Annual', 'Sick', 'Study/Conference', 'Emergency', 'Other']


def configure(h, code, **changes):
    """Change one seeded type through the admin's catalogue command (the full list includes archived types)."""
    row = next(t for t in h.get('admin', '/api/catalogues')['leave_types'] if t['short_code'] == code)
    return h.configure('leave_types', value(row, **changes), row['id'], row['revision'])['record']


def ask(h, code, role='staff', person=None, on=None):
    on = on or day()
    return h.command(role, 'request.create', {'person_id': person or h.seed['people'][0], 'start': f'{on}T00:00:00+03:00',
        'end': f'{on}T23:59:00+03:00', 'private_reason': 'Synthetic private note', 'leave_type_id': selected(h, code)['id']})['record']


def type_choices(dialog):
    return [o.strip() for o in dialog.get_by_label('Type', exact=True).locator('option:not([hidden])').all_inner_texts()]


def test_admin_adds_archives_restores_and_reorders_leave_types(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, 'Leave types')
        pane = page.locator('.config-pane')
        listed = pane.locator('tbody tr')
        expect(listed).to_have_count(5)
        for name in NAMES:
            expect(listed.filter(has_text=name)).to_have_count(1)
        expect(pane.get_by_role('columnheader', name=re.compile('^Blocks duties'))).to_be_visible()
        expect(pane.get_by_role('columnheader', name=re.compile('^Order'))).to_be_visible()

        # Add: name, code (upper-cased as typed), colour, and a type that does not block duties.
        pane.get_by_role('button', name='New leave type', exact=True).click()
        dialog = page.get_by_role('dialog', name='New leave type')
        dialog.get_by_label('Name', exact=True).fill('Hajj')
        dialog.get_by_label('Short code', exact=True).fill('hajj')
        expect(dialog.get_by_label('Short code', exact=True)).to_have_value('HAJJ')
        dialog.locator('label.swatch').filter(has=page.get_by_role('radio', name='Purple')).click()
        dialog.get_by_role('checkbox', name='Blocks duties').uncheck()
        save_dialog(dialog)
        hajj = listed.filter(has_text='Hajj')
        expect(hajj).to_contain_text('No · shown only')
        expect(hajj).to_contain_text('Active')
        made = next(t for t in h.get('staff', '/api/leave-types')['leave_types'] if t['name'] == 'Hajj')
        assert made['short_code'] == 'HAJJ' and made['colour'] == 'violet' and made['counts_as_unavailable'] is False
        assert made['sort_order'] == 5  # after the five seeded types

        # Archive (Edit › Offered for new leave off): kept in the list as Archived, gone from the choices; Undo restores.
        hajj.get_by_role('button', name='Edit', exact=True).click()
        dialog = page.get_by_role('dialog', name='Edit leave type')
        dialog.get_by_role('checkbox', name='Offered for new leave').uncheck()
        save_dialog(dialog, 'Save version')
        expect(hajj).to_contain_text('Archived')
        assert 'Hajj' not in [t['name'] for t in h.get('staff', '/api/leave-types')['leave_types']]
        page.locator('.toast').filter(has_text='Hajj saved').get_by_role('button', name='Undo', exact=True).click()
        expect(hajj).to_contain_text('Active')
        assert 'Hajj' in [t['name'] for t in h.get('staff', '/api/leave-types')['leave_types']]

        # Reorder in place: the Order cell saves one audited change, and the picker follows it.
        hajj.locator('td').filter(has_text=re.compile(r'^5$')).click()
        cell = page.get_by_role('spinbutton', name='Order')
        cell.fill('0')
        cell.press('Enter')
        expect(hajj.locator('td').filter(has_text=re.compile(r'^0$'))).to_be_visible()
        order = [t['name'] for t in h.get('staff', '/api/leave-types')['leave_types']]
        assert order.index('Hajj') < order.index('Sick')  # beside Annual (also 0, then by name)
    finally:
        context.close()


@pytest.mark.parametrize('viewport', [None, PHONE], ids=['desktop', 'phone'])
def test_staff_choose_an_active_type_and_approvers_see_it(browser, live_server, h, viewport):
    published(h)
    old = ask(h, 'EMERG', on=day(3))  # asked for before Emergency was archived: it keeps its type
    configure(h, 'EMERG', active=False)
    configure(h, 'STUDY', counts_as_unavailable=False)
    context, page = sign_in(browser, live_server, 'staff', viewport)
    try:
        navigate(page, 'requests')
        dialog = new_request(page, 'Leave')
        # One required Type, active types only, in the department's order; no free text.
        expect(dialog.get_by_label('Type', exact=True)).to_have_attribute('required', '')
        assert type_choices(dialog) == ['Annual', 'Sick', 'Study/Conference', 'Other']
        dialog.get_by_label('All day').check()
        dialog.get_by_label('First day', exact=True).fill(day())
        dialog.get_by_label('Last day', exact=True).fill(day())
        dialog.get_by_label('Reason', exact=True).fill('Family event')
        dialog.get_by_role('button', name='Send request', exact=True).click()
        expect(dialog.get_by_label('Type', exact=True)).to_be_focused()  # nothing is sent without a type
        dialog.get_by_label('Type', exact=True).select_option(label='Study/Conference')
        dialog.get_by_role('button', name='Send request', exact=True).click()
        expect(page.get_by_text('Request submitted, not yet approved.')).to_be_visible()
        sent = next(r for r in h.get('staff', '/api/requests')['requests'] if r['id'] != old['id'])
        assert sent['leave_type_id'] == selected(h, 'STUDY')['id'] and sent['counts_as_unavailable'] is False
        main = page.locator('#main')
        cards = main.locator('.dt-card') if viewport else rows(page)
        study = cards.filter(has_text='Study/Conference')
        expect(study).to_contain_text('Does not block duties')
        expect(cards.filter(has_text='Emergency')).to_have_count(1)  # an archived type still names old leave
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'approver', viewport)
    try:
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        cards = page.locator('#main .dt-card') if viewport else rows(page)
        expect(cards.filter(has_text='Study/Conference')).to_have_count(1)
        expect(cards.filter(has_text='Emergency')).to_have_count(1)
    finally:
        context.close()


@pytest.mark.parametrize('viewport', [None, PHONE], ids=['desktop', 'phone'])
def test_hidden_setting_keeps_others_types_private_but_not_your_own(browser, live_server, h, viewport):
    published(h)
    ask(h, 'SICK')
    setting(h, 'none')
    context, page = sign_in(browser, live_server, 'approver', viewport)
    try:
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        cards = page.locator('#main .dt-card') if viewport else rows(page)
        expect(cards).to_have_count(1)
        expect(cards.first).to_contain_text('Leave')
        expect(cards.first).not_to_contain_text('Sick')
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'staff', viewport)
    try:
        navigate(page, 'requests')
        cards = page.locator('#main .dt-card') if viewport else rows(page)
        expect(cards.first).to_contain_text('Sick')
        navigate(page, 'home')
        expect(page.locator('.home-requests')).to_contain_text('Sick')
    finally:
        context.close()


def test_coordinator_records_leave_with_a_type_and_the_timeline_shows_it(browser, live_server, h):
    h.publish(h.shift(h.workspace(), people=h.seed['people'][:2])['workspace'])
    configure(h, 'STUDY', counts_as_unavailable=False)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        dialog = new_request(page, 'Leave or absence')
        pick(dialog, 'People', ['Staff 01(test)'])
        kind = dialog.get_by_label('Record as', exact=True)
        typ = dialog.get_by_label('Type', exact=True)
        expect(typ).to_be_visible()
        expect(dialog.get_by_label('All day')).to_be_checked()  # leave starts as whole days
        kind.select_option(label='Day off')  # not leave: no type, and times
        expect(typ).to_be_hidden()
        expect(dialog.get_by_label('All day')).not_to_be_checked()
        kind.select_option(label='Leave')
        expect(dialog.get_by_label('All day')).to_be_checked()
        typ.select_option(label='Annual')
        dialog.get_by_label('All day').uncheck()
        dialog.get_by_label('Unavailable from', exact=True).fill('2026-10-05T08:00')
        dialog.get_by_label('Unavailable until', exact=True).fill('2026-10-05T16:00')
        dialog.get_by_label('Reason', exact=True).fill('Synthetic family event')
        save_dialog(dialog, 'Record leave')
        expect(row(page, 'Annual')).to_be_visible()
    finally:
        context.close()
    jordan = h.seed['people'][1]
    assert h.get('coordinator', '/api/availability')['availability'][0]['leave_type_id'] == selected(h, 'ANNUAL')['id']
    h.command('coordinator', 'availability.record', {'person_id': jordan, 'kind': 'recorded_leave', 'start': '2026-10-05T08:00:00+03:00',
        'end': '2026-10-05T16:00:00+03:00', 'private_reason': 'Synthetic course', 'leave_type_id': selected(h, 'STUDY')['id']})
    for mode, blocking, soft in [('type', 'On leave or away · Annual', 'Study/Conference · does not block duties'),
                                 ('none', 'On leave or away', 'Leave · does not block duties')]:
        setting(h, mode)
        context, page = sign_in(browser, live_server, 'coordinator')
        try:
            navigate(page, 'schedule/people/2026-10-04?edit=none')
            alex_row = page.locator('.pg-row').filter(has=page.get_by_role('rowheader', name='Staff 01(test)'))
            jordan_row = page.locator('.pg-row').filter(has=page.get_by_role('rowheader', name='Staff 02(test)'))
            expect(alex_row.locator(f'.pg-cell.leave[data-tip="{blocking}"]')).to_have_count(1)
            expect(jordan_row.locator(f'.pg-cell.leave-soft[data-tip="{soft}"]')).to_have_count(1)
            expect(jordan_row.locator('.pg-cell.leave')).to_have_count(0)  # never shown as blocking
        finally:
            context.close()


def test_admin_edits_and_undoes_leave_allowances(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, 'Leave types')
        page.get_by_role('button', name='Leave allowances', exact=True).click()
        dialog = page.get_by_role('dialog', name='Leave allowances')
        dialog.get_by_role('button', name='Add allowance', exact=True).click()
        dialog.get_by_label('Leave type', exact=True).select_option(label='Annual')
        dialog.get_by_label('Calendar days per year', exact=True).fill('15')
        dialog.get_by_label('Reason', exact=True).fill('Set annual allowance')
        save_dialog(dialog, 'Save allowances')
        saved = h.get('admin', '/api/leave-balance-settings')['configuration']
        assert saved['allowances'] == [{'leave_type_id': selected(h)['id'], 'days': 15,
                                        'person_id': None, 'starts_on': None}]
        page.locator('.toast', has_text='Leave allowances saved').get_by_role('button', name='Undo', exact=True).click()
        expect(page.locator('.toast').filter(has_text='Undone.')).to_be_visible()
        assert h.get('admin', '/api/leave-balance-settings')['configuration']['allowances'] == []
    finally:
        context.close()
