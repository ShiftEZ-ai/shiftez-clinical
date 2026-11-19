"""Programme leads save training data; date-bounded records expose only supported actions."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, save_dialog  # noqa: F401
from .test_browser_operator_role_coverage import all_roles, reload_view  # noqa: F401

pytestmark = pytest.mark.browser


def programme(h):
    assert set(h.get('programme_lead', '/api/me')['roles']) == {'programme_lead'}
    # Programme creation by this actor already has an existing full browser journey. Reuse a fixture here.
    return h.configure('programmes', {'name': 'Fictional subflow programme', 'starts_on': '2026-01-01'},
                       role='programme_lead')['record']


def record(h, kind, ident):
    return next(r for r in h.get('programme_lead', '/api/catalogues')[kind] if r['id'] == ident)


def row(page, name):
    # The same record identity is exposed by both the table and phone card layouts.
    return page.locator('#main [data-row]').filter(has=page.get_by_role('heading', name=name, exact=True))


def test_programme_lead_cohort_create_edit_undo_and_reload(browser, live_server, all_roles):
    h = all_roles
    parent = programme(h)
    context, page = sign_in(browser, live_server, 'programme_lead')
    try:
        navigate(page, 'programmes/cohorts')
        page.get_by_role('button', name='New cohort', exact=True).click()
        form = page.get_by_role('dialog', name='New cohort', exact=True)
        form.get_by_label('Name', exact=True).fill('Fictional lead cohort')
        form.get_by_label('Programme', exact=True).select_option(parent['id'])
        form.get_by_label('Effective from', exact=True).fill('2026-10-01')
        form.get_by_label('Effective until (included)', exact=True).fill('2026-10-31')
        save_dialog(form)
        saved = next(r for r in h.get('programme_lead', '/api/catalogues')['cohorts'] if r['name'] == 'Fictional lead cohort')
        assert (saved['programme_id'], saved['starts_on'], saved['ends_on']) == (parent['id'], '2026-10-01', '2026-11-01')
        reload_view(page, 'programmes/cohorts')
        expect(row(page, saved['name'])).to_be_visible()

        row(page, saved['name']).get_by_role('button', name='Edit', exact=True).click()
        form = page.get_by_role('dialog', name='Edit cohort', exact=True)
        form.get_by_label('Name', exact=True).fill('Fictional lead cohort revised')
        form.get_by_label('Effective until (included)', exact=True).fill('2026-11-14')
        save_dialog(form, 'Save version')
        edited = record(h, 'cohorts', saved['id'])
        assert (edited['name'], edited['ends_on']) == ('Fictional lead cohort revised', '2026-11-15')
        expect(row(page, edited['name'])).to_be_visible()
        # Undo is the saved edit's actual command inverse. Cohorts have dates, not an active/disabled field.
        page.locator('.toast').filter(has=page.get_by_role('button', name='Undo', exact=True)).last.get_by_role('button', name='Undo', exact=True).click()
        expect(row(page, saved['name'])).to_be_visible()
        restored = record(h, 'cohorts', saved['id'])
        assert {k: restored[k] for k in ('name', 'programme_id', 'starts_on', 'ends_on')} == {
            k: saved[k] for k in ('name', 'programme_id', 'starts_on', 'ends_on')}
        reload_view(page, 'programmes/cohorts')
        expect(row(page, saved['name'])).to_be_visible()
        assert record(h, 'cohorts', saved['id'])['ends_on'] == '2026-11-01'
    finally:
        context.close()


def test_date_bounded_programme_records_offer_no_unsupported_switch_actions(browser, live_server, all_roles):
    h = all_roles
    parent = programme(h)
    a, b = h.seed['people'][:2]
    dates = {'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}
    cohort = h.configure('cohorts', {'name': 'Capability fixture cohort', 'programme_id': parent['id'], **dates}, role='programme_lead')['record']
    fixtures = {
        'cohorts': cohort,
        'programme_memberships': h.configure('programme_memberships', {'person_id': a, 'programme_id': parent['id'], **dates}, role='programme_lead')['record'],
        'mentorships': h.configure('mentorships', {'person_id': a, 'mentor_id': b, 'programme_id': parent['id'], **dates}, role='programme_lead')['record'],
        'programme_calendar_entries': h.configure('programme_calendar_entries', {'name': 'Capability fixture block', 'kind': 'academic_block', 'programme_id': parent['id'], **dates}, role='programme_lead')['record'],
        'rotations': h.configure('rotations', {'person_id': a, 'cohort_id': cohort['id'], 'service_id': h.seed['services'][0], **dates}, role='programme_lead')['record'],
    }
    context, page = sign_in(browser, live_server, 'programme_lead')
    try:
        for kind, saved in fixtures.items():
            assert 'active' not in saved
            navigate(page, 'programmes/' + kind)
            page.locator(f'#main [data-row="{saved["id"]}"]').get_by_role('checkbox').check()
            bar = page.get_by_role('toolbar', name='Bulk actions', exact=True)
            expect(bar).to_be_visible()
            expect(bar.get_by_role('button', name='Switch off', exact=True)).to_have_count(0)
            expect(bar.get_by_role('button', name='Switch on', exact=True)).to_have_count(0)
            # No overflow menu may hide the unsupported action either.
            more = bar.get_by_role('button', name='More actions', exact=True)
            if more.count():
                more.click()
                expect(page.get_by_role('menuitem', name=re.compile(r'^Switch (on|off)$'))).to_have_count(0)
                page.keyboard.press('Escape')
            bar.get_by_role('button', name='Clear selection', exact=True).click()
    finally:
        context.close()


def test_programme_lead_saves_academic_dates_without_creating_clinical_authority(browser, live_server, all_roles):
    h = all_roles
    parent = programme(h)
    before = h.get('admin', '/api/catalogues')
    context, page = sign_in(browser, live_server, 'programme_lead', {'width': 390, 'height': 844})
    try:
        navigate(page, 'programmes/programme_calendar_entries')
        page.get_by_role('button', name='New academic calendar entry', exact=True).click()
        form = page.get_by_role('dialog', name='New academic calendar entry', exact=True)
        form.get_by_label('Name', exact=True).fill('Fictional lead teaching week')
        form.get_by_label('Programme', exact=True).select_option(parent['id'])
        form.get_by_label('Entry type', exact=True).select_option('academic_block')
        form.get_by_label('Effective from', exact=True).fill('2026-10-05')
        form.get_by_label('Effective until (included)', exact=True).fill('2026-10-09')
        save_dialog(form)
        saved = next(r for r in h.get('programme_lead', '/api/catalogues')['programme_calendar_entries'] if r['name'] == 'Fictional lead teaching week')
        assert (saved['programme_id'], saved['kind'], saved['starts_on'], saved['ends_on']) == (
            parent['id'], 'academic_block', '2026-10-05', '2026-10-10')
        reload_view(page, 'programmes/programme_calendar_entries')
        expect(row(page, saved['name'])).to_contain_text('No confirmed members cover this period')
        expect(row(page, saved['name']).get_by_role('button', name='Protect this time', exact=True)).to_have_count(0)
        after = h.get('admin', '/api/catalogues')
        for kind in ('programme_memberships', 'eligibilities', 'policies'):
            assert [r['id'] for r in after[kind]] == [r['id'] for r in before[kind]]
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_programme_lead_phone_attachment_picker_done_preserves_dirty_close(browser, live_server, all_roles, scheme):
    h = all_roles
    programme(h)
    people = h.get('admin', '/api/catalogues')['people']
    # Exactly enough options to use the existing >7 searchable-picker path; no department-scale fixture needed.
    for index in range(max(0, 8 - len(people))):
        h.configure('people', {'display_name': f'Fictional picker person {index}'})
    people = {p['id']: p['display_name'] for p in h.get('programme_lead', '/api/catalogues')['people']}
    trainee, mentor = h.seed['people'][:2]
    before = h.get('programme_lead', '/api/catalogues')['mentorships']
    context, page = sign_in(browser, live_server, 'programme_lead', {'width': 390, 'height': 844})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'programmes/mentorships')
        page.get_by_role('button', name='New attachment', exact=True).click()
        form = page.get_by_role('dialog', name='New attachment', exact=True)
        trigger = form.get_by_role('button', name=re.compile('^People:'))
        trigger.click()
        picker = page.get_by_role('dialog', name='People', exact=True)
        picker.get_by_role('combobox', name='Search People', exact=True).fill(people[trainee])
        choice = picker.get_by_role('option', name=people[trainee], exact=True)
        page.keyboard.press('Enter')  # the search keeps focus; Enter ticks the highlighted match
        expect(choice).to_be_checked()
        # First Escape clears a nonempty search. Done explicitly closes the picker before parent Cancel.
        picker.get_by_role('button', name='Done', exact=True).click()
        expect(picker).to_have_count(0)
        form.get_by_label('Mentor', exact=True).select_option(mentor)
        form.get_by_label('Pairing', exact=True).select_option('required')
        form.get_by_label('Last day (included, optional)', exact=True).fill('2026-10-31')
        form.get_by_role('button', name='Cancel', exact=True).click()
        guard = form.locator('.unsaved-bar')
        expect(guard).to_contain_text('Not saved. Close anyway?')
        guard.get_by_role('button', name='Keep editing', exact=True).click()
        expect(form.get_by_label('Mentor', exact=True)).to_have_value(mentor)
        expect(trigger).to_contain_text(people[trainee])
        form.get_by_role('button', name='Cancel', exact=True).click()
        guard.get_by_role('button', name='Discard changes', exact=True).click()
        expect(form).to_have_count(0)
        reload_view(page, 'programmes/mentorships')
        assert h.get('programme_lead', '/api/catalogues')['mentorships'] == before
    finally:
        context.close()
# Include this file in hosted supervision and team-lead qualification.
