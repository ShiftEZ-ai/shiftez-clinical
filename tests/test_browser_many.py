"""One form, many people: records that belong to one person are created for everyone picked; the picker remembers recent picks."""
import re
import pytest
from playwright.sync_api import expect
from .test_browser import form_submit, browser, live_server, sign_in, navigate, pick  # noqa: F401
from .test_browser import whole_list

pytestmark = pytest.mark.browser


def names(h):
    return {p['id']: p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people']}


def test_programme_membership_for_several_people_previews_and_saves_one_each(browser, live_server, h):
    programme = h.configure('programmes', {'name': 'Family medicine residency', 'starts_on': '2026-01-01'})['record']
    people = names(h)
    chosen = h.seed['people'][:2]
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'programmes')
        page.get_by_role('button', name=re.compile(r'^Programme memberships')).click()
        page.get_by_role('button', name='New programme membership', exact=True).click()
        form = page.get_by_role('dialog')
        pick(form, 'People', [people[i] for i in chosen])
        form.get_by_label('Programme', exact=True).select_option(programme['id'])
        form.get_by_label('Effective from', exact=True).fill('2026-10-01')
        form.get_by_label('Effective until (included)', exact=True).fill('2026-10-31')
        expect(form.get_by_text('2 records will be saved.', exact=True)).to_be_visible(timeout=20000)
        form_submit(form).click()
        expect(page.get_by_text('2 records saved, one for each person.', exact=False)).to_be_visible()
        saved = h.get('coordinator', '/api/catalogues')['programme_memberships']
        assert sorted(r['person_id'] for r in saved if r['programme_id'] == programme['id']) == sorted(chosen)
    finally:
        context.close()


def test_known_unavailability_for_several_people(browser, live_server, h):
    people = names(h)
    chosen = h.seed['people'][:2]
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page,'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        page.get_by_role('button', name='New request', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_role('radio', name=re.compile('^Leave or absence')).check()
        pick(form, 'People', [people[i] for i in chosen])
        form.get_by_label('All day').uncheck()
        form.get_by_label('Unavailable from', exact=True).fill('2026-10-05T08:00')
        form.get_by_label('Unavailable until', exact=True).fill('2026-10-05T12:00')
        form.get_by_label('Type', exact=True).select_option(label='Annual')
        form.get_by_label('Reason', exact=True).fill('Department training morning')
        form.get_by_role('button', name='Record leave', exact=True).click()
        expect(page.get_by_text('Recorded for 2 people.', exact=False)).to_be_visible()
        records = h.get('coordinator', '/api/availability')['availability']
        assert sorted(r['person_id'] for r in records) == sorted(chosen)
    finally:
        context.close()


def test_recent_picks_come_first_and_group_chips_narrow_the_list(browser, live_server, h):
    kinds = h.get('admin', '/api/catalogues')['location_types']
    kind = kinds[0]['id']
    for n in range(1, 11):
        h.configure('locations', {'name': f'Room {n}', 'starts_on': '2020-01-01', 'location_type_id': kind})
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        h.workspace('Picker draft')  # a draft: Add session opens the session form (a long single choice: Location)
        navigate(page, 'planner')
        page.locator('#main').get_by_role('button', name='Add session', exact=True).click()
        location = page.locator('.dock-panel').get_by_label('Location', exact=True)
        location.click()
        picker = page.locator('.picker')
        picker.get_by_role('option', name='Room 7', exact=True).click()
        expect(picker).to_have_count(0)
        location.click()
        recent = picker.locator('.picker-group').filter(has_text='Recent')
        expect(recent).to_have_count(1)
        expect(recent.locator('xpath=following-sibling::*[1]')).to_contain_text('Room 7')
        page.keyboard.press('Escape')
        page.keyboard.press('Escape')
        expect(picker).to_have_count(0)
    finally:
        context.close()


def test_people_bulk_record_leave_uses_the_one_form_prefilled_with_the_people(browser, live_server, h):
    """People › select two › Record leave opens the shared Record leave form with both
    people chosen and All day shown, and records leave for each."""
    people = names(h)
    chosen = h.seed['people'][:2]
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        for pid in chosen:
            page.locator('#main tr', has_text=people[pid]).locator('.bulk-check').check()
        bar = page.get_by_role('toolbar', name='Bulk actions')
        bar.get_by_role('button', name='More actions', exact=True).click()
        page.get_by_role('menu', name='More actions').get_by_role('menuitem', name='Record leave', exact=True).click()
        form = page.get_by_role('dialog', name='Record leave for 2 people')
        expect(form.get_by_label('All day')).to_be_visible()
        picked = form.get_by_role('group', name='People')
        for pid in chosen:
            expect(picked.get_by_role('checkbox', name=people[pid], exact=True)).to_be_checked()
        form.get_by_label('All day').check()
        form.get_by_label('First day', exact=True).fill('2026-10-06')
        form.get_by_label('Last day', exact=True).fill('2026-10-06')
        form.get_by_label('Type', exact=True).select_option(label='Annual')
        form.get_by_label('Reason', exact=True).fill('Department away day')
        form.get_by_role('button', name='Record leave', exact=True).click()
        expect(page.get_by_text('Recorded for 2 people.', exact=False)).to_be_visible()
        records = h.get('coordinator', '/api/availability')['availability']
        assert sorted(r['person_id'] for r in records) == sorted(chosen)
    finally:
        context.close()
