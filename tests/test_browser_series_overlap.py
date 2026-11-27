"""Double-booked people in recurring sessions, the whole journey in the browser (docs/design/series-overlap.md): the
recurring-session form warns live before saving, saving needs the acknowledgement, the schedule marks both sessions in
words, the rota check names both, and cancelling one occurrence clears the clash, with Undo bringing it back."""
import os
import re

import pytest
from playwright.sync_api import expect

from .test_browser import form_submit, browser, live_server, sign_in, open_config, open_rota, navigate, view_settings, choose_layout, close_popover  # noqa: F401
from .test_series_overlap_api import candidate, preview, reviewed, save
from .test_series_reconciliation import materialise
from .test_browser_recurring_sessions import layout

pytestmark = pytest.mark.browser
SHOTS = os.getenv('RECURRING_SHOTS')


def shot(page, name):
    if SHOTS:
        page.screenshot(path=os.path.join(SHOTS, name + '.png'), full_page=False)


def overlaps(h, workspace):
    return [f for f in (h.detail(workspace)['workspace'].get('validation') or {}).get('findings', []) if f['code'] == 'TIME_OVERLAP']


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_double_booking_journey_warns_live_saves_with_acknowledgement_marks_both_and_cancels_one_with_undo(browser, live_server, h, scheme):
    peer_value = candidate(h, 'Autism clinic', service=1)
    peer = save(h, peer_value, token=preview(h, peer_value)['conflict_token'])['record']
    person = next(p for p in h.get('admin', '/api/catalogues')['people'] if p['id'] == h.seed['people'][0])['display_name']
    context, page = sign_in(browser, live_server, 'admin', viewport={'width': 1440, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        open_config(page, 'Recurring sessions')
        page.get_by_role('button', name='New recurring session', exact=True).click()
        form = page.get_by_role('dialog', name='New recurring session')
        form.get_by_label('Duty type', exact=True).select_option(h.seed['type'])
        form.get_by_label('Location', exact=True).select_option(h.seed['locations'][0])
        form.get_by_role('group', name='Clinics and services').get_by_role('checkbox').first.check()
        form.get_by_label('Effective from', exact=True).fill('2026-10-01')
        form.get_by_label('Effective until (included)', exact=True).fill('2026-10-31')
        form.get_by_role('checkbox', name='Monday', exact=True).check()
        form.get_by_label('Monday start', exact=True).fill('08:00')
        form.get_by_label('Monday end', exact=True).fill('12:00')
        check = form.locator('.clash-check')
        expect(check).to_contain_text('No one is double-booked', timeout=10000)
        # Choosing the person who already works Autism on Mondays: warned while filling in, before any save.
        form.get_by_role('group', name='Usual assignees').get_by_role('checkbox', name=person, exact=True).check()
        form.get_by_role('checkbox', name='Keep the usual assignees on every session').check()
        expect(check).to_contain_text('Double-booked 4 times: someone is on two sessions at the same time.', timeout=10000)
        expect(check).to_contain_text(f'{person} is already in Autism clinic, Mon 5 Oct 08:00–12:00')
        expect(check.get_by_role('button', name=re.compile('^Close this session: '))).to_have_count(4)
        check.scroll_into_view_if_needed()
        shot(page, f'series-overlap-form-{scheme}')
        # Fixing one here: its date becomes an excluded date and the check runs again.
        check.get_by_role('button', name=re.compile(r'^Close this session: .*Mon 26 Oct')).click()
        expect(form.get_by_role('group', name='Excluded dates').locator('.date-chip')).to_have_count(1)
        expect(check).to_contain_text('Double-booked 3 times', timeout=10000)
        # Save is refused until the remaining clashes are acknowledged (once the consequence check has settled, so its
        # redraw cannot land on the refusal).
        expect(form.get_by_text(re.compile(r'Configuration checked\.'))).to_be_visible(timeout=20000)
        form_submit(form).click()
        expect(form.get_by_text(re.compile('Tick “Save with these double bookings”'))).to_be_visible()
        form.get_by_role('checkbox', name='Save with these double bookings').check()
        expect(form.get_by_text(re.compile(r'Configuration checked\.'))).to_be_visible(timeout=20000)
        form_submit(form).click()
        expect(form).to_have_count(0, timeout=15000)
        made = next(s for s in h.get('admin', '/api/catalogues')['recurring_series'] if s['id'] != peer['id'])
        assert made['definition']['excluded_dates'] == ['2026-10-26']
        [version] = h.get('admin', f"/api/catalogues/recurring_series/{made['id']}/history")['versions']
        assert version['snapshot']['overlap_acknowledged'] and len(version['snapshot']['overlap_conflicts']) == 3

        # The schedule: both sessions of 5 Oct are marked in words; the panel explains and offers the fix.
        workspace = materialise(h, peer)
        workspace = materialise(h, made, workspace)
        assert len(overlaps(h, workspace)) >= 1  # the rota check runs inside each command
        open_rota(page, workspace['id'])
        marks = page.locator('.clash-mark')
        expect(marks).to_have_count(2, timeout=15000)
        expect(marks.first).to_have_text('Double-booked')
        expect(marks.first).to_have_attribute('data-tip', re.compile(f'{re.escape(person)} is already in '))
        shot(page, f'series-overlap-schedule-{scheme}')

        # The rota check names both sessions and offers the same cancel actions.
        navigate(page, f'schedule/issues?draft={workspace["id"]}&view=checks')
        row = page.locator('tr[data-row]').filter(has_text='is booked in both')
        expect(row.first).to_contain_text(f'{person} is booked in both')
        expect(page.get_by_text('Double-booked', exact=True).first).to_be_visible()  # the group: one reason, not one per clash
        expect(row.first.get_by_role('button', name=re.compile('^Close the other session: '))).to_be_visible()
        shot(page, f'series-overlap-issues-{scheme}')

        # Cancel one from the schedule (same-button confirm), the clash and its finding go; Undo brings both back.
        open_rota(page, workspace['id'])
        expect(marks).to_have_count(2, timeout=15000)
        marks.first.click()
        cancel = page.locator('.popover').get_by_role('button', name=re.compile('^Close the other session: '))
        cancel.click()
        # The message carries the Fix list's knock-on (R3, 36aea7939): the double booking was the one cause cleared.
        expect(page.get_by_text('Autism clinic on Mon 5 Oct closed. 1 cleared.', exact=True)).to_be_visible()
        expect(marks).to_have_count(0, timeout=15000)
        assert not overlaps(h, workspace)  # the page showed the command's reply
        page.locator('.toast').filter(has_text='Autism clinic on Mon 5 Oct closed.').get_by_role('button', name='Undo', exact=True).click()
        expect(marks).to_have_count(2, timeout=15000)
        assert len(overlaps(h, workspace)) >= 1

        # The People timeline (duty codes) marks the person's duty on both sessions too (icon and words, the shared mark).
        choose_layout(view_settings(page.locator('.planner-toolbar')), 'Timeline')
        close_popover(page)
        expect(page.locator('.pg-chip .clash-tag')).to_have_count(2, timeout=15000)
        expect(page.locator('.pg-chip .clash-tag').first).to_have_text('Double-booked')
        shot(page, f'series-overlap-people-{scheme}')

        # Resolve issues: cancel from the row; the rota is checked again by itself (no "Check rota" needed).
        navigate(page, f'schedule/issues?draft={workspace["id"]}&view=checks')
        row = page.locator('tr[data-row]').filter(has_text='is booked in both')
        row.first.get_by_role('button', name=re.compile('^Close the other session: ')).click()
        expect(page.locator('tr[data-row]').filter(has_text='is booked in both')).to_have_count(0, timeout=15000)
        expect(page.get_by_role('button', name='Check rota', exact=True)).to_have_count(0)
        assert not overlaps(h, workspace)  # the page showed the command's reply

        # A quick table edit that would double-book: the same clashes in a dialog, saved only with the acknowledgement.
        open_config(page, 'Recurring sessions')
        layout(page, 'Table')
        clinic = next(x['name'] for x in h.get('admin', '/api/catalogues')['services'] if x['id'] == h.seed['services'][1])
        peer_row = page.locator('[data-table="catalogue-recurring_series"] tbody tr[data-row]').filter(has_text=clinic)
        peer_row.locator('td[data-col=times]').click()
        page.get_by_label('Start', exact=True).fill('09:00')
        page.get_by_label('End', exact=True).fill('11:00')
        page.get_by_role('button', name='Save', exact=True).click()
        dialog = page.get_by_role('dialog', name='Double bookings')
        expect(dialog).to_contain_text(f'{person} is already in ', timeout=10000)
        expect(dialog.get_by_role('button', name=re.compile('^Close this session: '))).not_to_have_count(0)
        shot(page, f'series-overlap-quick-edit-{scheme}')
        dialog.get_by_role('button', name='Save with these double bookings', exact=True).click()
        expect(dialog).to_have_count(0, timeout=15000)
        expect(peer_row.locator('td[data-col=times]')).to_contain_text('09:00', timeout=15000)  # the saved row
        assert {d['start_time'] for d in next(x for x in h.get('admin', '/api/catalogues')['recurring_series']
                                              if x['id'] == peer['id'])['definition']['days']} == {'09:00'}
    finally:
        context.close()


@pytest.mark.parametrize('mode', ['none', 'type'])
def test_leave_clash_reads_as_the_server_words_it(browser, live_server, h, mode):
    """A published session's leave flag shows the server's wording for the department's leave display setting, and
    nothing more (no note unless the server sends it)."""
    from .test_leave_disclosure import setting
    from .test_series_overlap_leave_flags import absence, NOTE
    from .test_series_overlap_schedule import DAY
    workspace = h.shift(h.workspace(), *DAY)['workspace']
    h.publish(h.detail(workspace)['workspace'])
    absence(h)  # recorded after publication: the published session now clashes with it
    setting(h, mode)
    person = next(p for p in h.get('admin', '/api/catalogues')['people'] if p['id'] == h.seed['people'][0])['display_name']
    [flag] = h.get('coordinator', '/api/schedule/sessions?start=2026-10-05&end=2026-10-06')['shifts'][0]['double_booking_conflicts']
    words = flag['label']
    assert (words == 'Unavailable') == (mode == 'none') and NOTE not in words
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        # The Schedule (GET /api/schedule/sessions), as everyone reads published rotas.
        navigate(page, 'schedule/week/2026-10-05')
        page.locator('.pchip', has_text=person).first.click()
        dialog = page.locator('.dock-panel')  # a published session opens docked, read-only (S-22)
        expect(dialog.locator('.clash-note')).to_contain_text(f'{person}: {words}, Mon 5 Oct 08:00–12:00')
        expect(dialog).not_to_contain_text(NOTE)
        expect(page.locator('.clash-mark')).to_have_count(0)  # leave is an absence, not a double booking
    finally:
        context.close()


def one_monday(h, name, service, person, active=True, ends='2026-10-06'):
    """A fixed recurring session on Mondays from 5 Oct 2026 (one occurrence by default), 08:00-12:00, in its own clinic."""
    return h.configure('recurring_series', {'name': name, 'starts_on': '2026-10-05', 'ends_on': ends, 'active': active,
        'assignment_type_id': h.seed['type'], 'location_id': h.seed['locations'][service], 'definition': {'anchor': '2026-10-05',
            'days': [{'weekday': 0, 'start_time': '08:00', 'end_time': '12:00'}], 'service_ids': [h.seed['services'][service]],
            'default_person_ids': [person], 'usual_assignees_fixed': True}})['record']


def two_rows(h, page, active=True, people=None, ends='2026-10-06'):
    """Two rows (two clinics, the same Monday mornings), both selected in the Recurring sessions table."""
    p = h.seed['people']
    rows = [one_monday(h, f'Synthetic combined clinic {i}', i, (people or p[:2])[i], active, ends) for i in range(2)]
    # The rows were saved behind the open app's back: reload so it starts from them. Otherwise its catch-up refresh can
    # redraw Configuration while View settings is open, and the Table choice lands on the replaced list (a CI flake).
    page.reload()
    expect(page.locator('#main')).to_have_attribute('data-loaded-view', re.compile('.'))
    open_config(page, 'Recurring sessions')
    layout(page, 'Table')
    table = page.locator('[data-table="catalogue-recurring_series"] tbody tr[data-row]')
    expect(table).to_have_count(2)
    names = {x['id']: x['name'] for x in h.get('admin', '/api/catalogues')['services']}
    lines = [table.filter(has_text=names[h.seed['services'][i]]) for i in range(2)]
    for line in lines:
        line.locator('input.bulk-check').check()
    return rows, lines


def saved(h, rows):
    ids = {r['id'] for r in rows}
    return [r for r in h.get('admin', '/api/catalogues')['recurring_series'] if r['id'] in ids]


def to_p3(h, page, lines):
    """Change the first selected row's usual assignees (P1) to P3: the edit applies to both selected rows."""
    people = {x['id']: x['display_name'] for x in h.get('admin', '/api/catalogues')['people']}
    lines[0].locator('td[data-col=people]').click()
    picker = page.locator('.picker')
    for person in (h.seed['people'][0], h.seed['people'][2]):
        picker.locator('.picker-row').filter(has_text=people[person]).first.locator('.picker-label').click()
    page.keyboard.press('Escape')  # the picker closes; the cell's editor stays
    expect(picker).to_have_count(0)
    page.locator('.dt-cell-pop').get_by_role('button', name='Save', exact=True).click()
    return page.get_by_role('dialog', name='Double bookings')


@pytest.mark.parametrize('answer,scheme', [('save', 'light'), ('save', 'dark'), ('cancel', 'light'), ('other', 'light'), ('other', 'dark')])
def test_several_rows_changed_to_one_person_are_checked_together(browser, live_server, h, answer, scheme):
    """P1 and P2 cover two clinics at the same time; both rows changed to P3 together clash with each other, though each
    alone against the saved rows would not. The dialog shows the clash once; saving acknowledges the whole change, and
    cancelling either occurrence (both rows are in the change) stages it in the change, then the change saves once,
    with one Undo."""
    p3 = h.seed['people'][2]
    p3_name = next(x['display_name'] for x in h.get('admin', '/api/catalogues')['people'] if x['id'] == p3)
    context, page = sign_in(browser, live_server, 'admin', viewport={'width': 1440, 'height': 1000})
    try:
        page.emulate_media(color_scheme=scheme)
        rows, lines = two_rows(h, page)
        before = saved(h, rows)
        dialog = to_p3(h, page, lines)
        expect(dialog).to_contain_text(p3_name, timeout=10000)
        expect(dialog.get_by_role('button', name=re.compile('^Close this session: '))).to_have_count(1)  # the clash once
        shot(page, f'series-overlap-combined-{answer}-{scheme}')
        if answer == 'save':
            dialog.get_by_role('button', name='Save with these double bookings', exact=True).click()
        elif answer == 'cancel':
            dialog.get_by_role('button', name=re.compile('^Close this session: ')).click()
        else:
            # The other row is in this change too: staged in its pending value (no command of its own).
            dialog.get_by_role('button', name=re.compile('^Close the other session: ')).click()
        expect(page.get_by_text('Usual assignees updated for 2', exact=False)).to_be_visible(timeout=15000)
        after = saved(h, rows)
        assert all(r['definition']['default_person_ids'] == [p3] for r in after)
        assert [r['revision'] for r in after] == [b['revision'] + 1 for b in before]  # one save each: the change's own
        cancelled = [r for r in after if '2026-10-05' in (r['definition'].get('excluded_dates') or [])]
        assert len(cancelled) == (0 if answer == 'save' else 1)
        if answer == 'other':
            # One Undo puts both rows back: their people, and the occurrence the change cancelled.
            page.get_by_role('button', name='Undo', exact=True).click()
            expect(page.get_by_text('Undone', exact=False)).to_be_visible(timeout=15000)
            undone = saved(h, rows)
            assert [r['definition']['default_person_ids'] for r in undone] == [b['definition']['default_person_ids'] for b in before]
            assert not any(r['definition'].get('excluded_dates') for r in undone)
    finally:
        context.close()


def test_leaving_the_clash_dialog_saves_nothing(browser, live_server, h):
    """Cancelling the other row's occurrence is only staged in the change: leaving the dialog (Back) before saving
    leaves both rows exactly as they were, with no cancelled occurrence saved."""
    context, page = sign_in(browser, live_server, 'admin', viewport={'width': 1440, 'height': 1000})
    try:
        rows, lines = two_rows(h, page, ends='2026-10-13')  # two Mondays: a clash on each
        before = saved(h, rows)
        dialog = to_p3(h, page, lines)
        other = dialog.get_by_role('button', name=re.compile('^Close the other session: '))
        expect(other).to_have_count(2, timeout=10000)
        other.first.click()
        dialog = page.get_by_role('dialog', name='Double bookings')
        expect(dialog.get_by_role('button', name=re.compile('^Close the other session: '))).to_have_count(1, timeout=10000)  # checked again
        dialog.get_by_role('button', name='Back', exact=True).click()
        expect(page.get_by_text('Not saved: this change would double-book someone.', exact=False)).to_be_visible()
        assert saved(h, rows) == before
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_switching_several_rows_on_is_checked_together(browser, live_server, h, scheme):
    """Two switched-off rows with the same person at the same time: switching both on shows the clash, and saving with
    it acknowledged switches both on."""
    p3 = h.seed['people'][2]
    context, page = sign_in(browser, live_server, 'admin', viewport={'width': 1440, 'height': 1000})
    try:
        page.emulate_media(color_scheme=scheme)
        rows, lines = two_rows(h, page, active=False, people=[p3, p3])
        lines[0].locator('td[data-col=status]').click()
        dialog = page.get_by_role('dialog', name='Double bookings')
        expect(dialog.get_by_role('button', name=re.compile('^Close this session: '))).to_have_count(1, timeout=10000)
        dialog.get_by_role('button', name='Save with these double bookings', exact=True).click()
        expect(page.get_by_text('Status updated for 2', exact=False)).to_be_visible(timeout=15000)
        assert all(r['active'] for r in saved(h, rows))
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_cancel_the_other_saved_session_in_a_combined_change(browser, live_server, h, scheme):
    """Both rows' Monday is already a draft session. Moving both to 09:00-11:00 still clashes with the saved sessions;
    "Cancel the other occurrence" cancels the other saved session with the server's command (excluding a template date
    would leave the session itself), the change then saves, and Undo of the cancellation brings the session back."""
    values = []
    for i in range(2):
        value = candidate(h, f'Synthetic materialised clinic {i}', service=i)
        value.update(starts_on='2026-10-05', ends_on='2026-10-06', location_id=h.seed['locations'][i])
        values.append(value)
    rows = [reviewed(h, v) for v in values]
    w = h.workspace('Synthetic materialised cancellation', start='2026-10-05', end='2026-10-06')
    for row in rows:
        w = materialise(h, row, w)
    assert len(h.detail(w)['shifts']) == 2
    context, page = sign_in(browser, live_server, 'admin', viewport={'width': 1440, 'height': 1000})
    try:
        page.emulate_media(color_scheme=scheme)
        open_config(page, 'Recurring sessions')
        layout(page, 'Table')
        names = {x['id']: x['name'] for x in h.get('admin', '/api/catalogues')['services']}
        lines = [page.locator('[data-table="catalogue-recurring_series"] tbody tr[data-row]').filter(has_text=names[h.seed['services'][i]]) for i in range(2)]
        for line in lines:
            line.locator('input.bulk-check').check()
        lines[0].locator('td[data-col=times]').click()
        cell = page.locator('.dt-cell-pop')
        cell.get_by_label('Start', exact=True).fill('09:00')
        cell.get_by_label('End', exact=True).fill('11:00')
        cell.get_by_role('button', name='Save', exact=True).click()
        dialog = page.get_by_role('dialog', name='Double bookings')
        other = dialog.get_by_role('button', name=re.compile('^Close the other session: '))
        expect(other).to_have_count(2, timeout=10000)  # each row's new time against the other row's saved session
        other.first.click()  # a saved session is cancelled at once, with Undo (D42)
        expect(page.locator('.toast').filter(has_text='closed.')).to_be_visible(timeout=15000)
        # The saved session cancelled leaves nothing to clash: the change saves with no acknowledgement.
        expect(page.get_by_text('Times updated for 2', exact=False)).to_be_visible(timeout=15000)
        after = h.detail(w)
        assert sum(s['cancelled'] for s in after['shifts']) == 1
        assert not any(f['code'] == 'TIME_OVERLAP' for f in after['workspace']['validation']['findings'])
        stored = [r for r in h.get('admin', '/api/catalogues')['recurring_series'] if r['id'] in {x['id'] for x in rows}]
        assert not any(r['definition'].get('excluded_dates') for r in stored)  # no template exclusion stands in for it
        assert all({d['start_time'] for d in r['definition']['days']} == {'09:00'} for r in stored)
        # The cancellation keeps its own Undo (its toast): the session comes back.
        page.locator('.toast').filter(has_text='closed.').get_by_role('button', name='Undo', exact=True).click()
        expect(page.locator('.toast').filter(has_text='Undone')).to_be_visible(timeout=15000)
        assert not any(s['cancelled'] for s in h.detail(w)['shifts'])
    finally:
        context.close()


def test_full_form_cancels_a_saved_occurrence_with_the_server(browser, live_server, h):
    """In the full recurring form, an occurrence already a draft session is cancelled by the server's command (the
    session itself), never by excluding its date locally, which would clear the clash on screen while the session stays."""
    values = []
    for i in range(2):
        value = candidate(h, f'Synthetic saved clash {i}', service=i)
        value.update(starts_on='2026-10-05', ends_on='2026-10-06', location_id=h.seed['locations'][i])
        values.append(value)
    rows = [reviewed(h, v) for v in values]
    w = h.workspace('Synthetic saved clash', start='2026-10-05', end='2026-10-06')
    for row in rows:
        w = materialise(h, row, w)
    assert len(h.detail(w)['shifts']) == 2
    context, page = sign_in(browser, live_server, 'admin', viewport={'width': 1440, 'height': 1000})
    try:
        navigate(page, 'configuration/recurring_series')
        page.locator('.sw-entry').first.click()  # either series: its own occurrence is a saved session
        form = page.get_by_role('dialog', name=re.compile('^Edit recurring session'))
        check = form.locator('.clash-check')
        own = check.get_by_role('button', name=re.compile('^Close this session: '))
        expect(own).to_have_count(1, timeout=10000)
        expect(form.get_by_text(re.compile(r'Configuration checked\.'))).to_be_visible(timeout=20000)
        own.click()  # the server's command, at once (D42: its message carries Undo)
        expect(page.locator('.toast').filter(has_text='closed.')).to_be_visible(timeout=15000)
        expect(check).to_contain_text('No one is double-booked', timeout=10000)
        expect(form.get_by_role('group', name='Excluded dates').locator('.date-chip')).to_have_count(0)
        assert sum(s['cancelled'] for s in h.detail(w)['shifts']) == 1
    finally:
        context.close()


def test_full_form_never_cancels_a_locked_saved_occurrence_locally(browser, live_server, h):
    """Both occurrences are saved, locked sessions: the server offers no cancel for them, and the form must not
    exclude the date instead (that would clear the preview while the locked session stays). The cancel shows as
    unavailable, with the reason."""
    values = []
    for i in range(2):
        value = candidate(h, f'Synthetic locked clash {i}', service=i)
        value.update(starts_on='2026-10-05', ends_on='2026-10-06', location_id=h.seed['locations'][i])
        values.append(value)
    rows = [reviewed(h, v) for v in values]
    w = h.workspace('Synthetic locked clash', start='2026-10-05', end='2026-10-06')
    for row in rows:
        w = materialise(h, row, w)
    shifts = h.detail(w)['shifts']
    assert len(shifts) == 2
    w = h.command('coordinator', 'shift.action', {'workspace_id': w['id'], 'shift_ids': [s['id'] for s in shifts], 'action': 'lock',
                                                  'reason': 'Keep reviewed'}, revision=w['revision'])['workspace']
    context, page = sign_in(browser, live_server, 'admin', viewport={'width': 1440, 'height': 1000})
    try:
        navigate(page, 'configuration/recurring_series')
        page.locator('.sw-entry').first.click()
        form = page.get_by_role('dialog', name=re.compile('^Edit recurring session'))
        check = form.locator('.clash-check')
        own = check.get_by_role('button', name=re.compile('^Close this session: '))
        expect(own).to_have_count(1, timeout=10000)
        expect(own).to_be_disabled()
        expect(check).to_contain_text('Saved session: close it on its rota')
        expect(form.get_by_role('group', name='Excluded dates').locator('.date-chip')).to_have_count(0)
        assert not any(s['cancelled'] for s in h.detail(w)['shifts'])
    finally:
        context.close()
