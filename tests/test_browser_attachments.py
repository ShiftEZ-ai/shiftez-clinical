"""Attachments in the browser: a coordinator attaches a trainee to a consultant for some duty types, required."""
import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate, pick, save_dialog, open_session, expect_loaded  # noqa: F401
from .test_browser_person_profile import open_person, section
from .test_attachments import people, setup
from .test_manual_attachment_pairing import pairing_setup, participants as pairing_participants

pytestmark = pytest.mark.browser


def test_coordinator_attaches_a_trainee_to_a_consultant(browser, live_server, h):
    trainee, consultant, _ = h.seed['people']
    names = {p['id']: p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people']}
    duty = next(t['name'] for t in h.get('coordinator', '/api/catalogues')['assignment_types'] if t['id'] == h.seed['type'])
    context, page = sign_in(browser, live_server, 'coordinator'); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        navigate(page, 'programmes')
        page.get_by_role('button', name='Attachments', exact=True).click()
        page.get_by_role('button', name='New attachment', exact=True).click()
        dialog = page.get_by_role('dialog', name='New attachment', exact=True)
        pick(dialog, 'People', [names[trainee]])
        dialog.get_by_label('Mentor', exact=True).select_option(consultant)
        dialog.get_by_label('Pairing', exact=True).select_option('required')
        pick(dialog, 'Duty types', [duty])
        dialog.get_by_label('Effective from', exact=True).fill('2026-10-01')
        dialog.get_by_label('Last day (included, optional)', exact=True).fill('2026-10-31')
        save_dialog(dialog)
        expect(page.get_by_role('heading', name=f'{names[trainee]} · with {names[consultant]} · required')).to_be_visible()
        assert not errors
    finally:
        context.close()
    row = next(r for r in h.get('coordinator', '/api/catalogues')['mentorships'] if r['person_id'] == trainee)
    assert (row['mentor_id'], row['strength'], row['assignment_type_ids'], row['service_ids']) == (consultant, 'required', [h.seed['type']], [])


@pytest.mark.parametrize('strength,width,scheme', [('required', 1440, 'light'), ('preferred', 390, 'dark')])
def test_open_ended_attachment_create_reload_edit_and_undo(browser, live_server, h, strength, width, scheme):
    trainee, consultant, _ = h.seed['people']
    people = {p['id']: p for p in h.get('coordinator', '/api/catalogues')['people']}
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 900})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'programmes/mentorships')
        page.get_by_role('button', name='New attachment', exact=True).click()
        form = page.get_by_role('dialog', name='New attachment', exact=True)
        pick(form, 'People', [people[trainee]['display_name']])
        form.get_by_label('Mentor', exact=True).select_option(consultant)
        form.get_by_label('Pairing', exact=True).select_option(strength)
        form.get_by_label('Effective from', exact=True).fill('2026-10-01')
        last = form.get_by_label('Last day (included, optional)', exact=True)
        expect(last).to_have_value('')
        assert not last.evaluate('(input) => input.required')
        save_dialog(form)
        saved = next(r for r in h.get('coordinator', '/api/catalogues')['mentorships'] if r['person_id'] == trainee)
        assert (saved['ends_on'], saved['strength']) == (None, strength)

        page.reload()
        expect_loaded(page, 'programmes/mentorships')
        row = page.locator(f'#main [data-row="{saved["id"]}"]')
        expect(row).to_contain_text('no end')
        row.get_by_role('button', name='Edit', exact=True).click()
        form = page.get_by_role('dialog', name='Edit attachment', exact=True)
        expect(form.get_by_label('Last day (included, optional)', exact=True)).to_have_value('')
        form.get_by_label('Last day (included, optional)', exact=True).fill('2026-10-31')
        save_dialog(form, 'Save version')
        expect(row).to_contain_text('31 Oct 2026')
        bounded = next(r for r in h.get('coordinator', '/api/catalogues')['mentorships'] if r['id'] == saved['id'])
        assert bounded['ends_on'] == '2026-11-01'
        page.locator('.toast').filter(has=page.get_by_role('button', name='Undo', exact=True)).last.get_by_role('button', name='Undo', exact=True).click()
        expect(row).to_contain_text('no end')
        page.reload()
        expect_loaded(page, 'programmes/mentorships')
        restored = next(r for r in h.get('coordinator', '/api/catalogues')['mentorships'] if r['id'] == saved['id'])
        assert restored['ends_on'] is None
        expect(row).to_contain_text('no end')

        panel = open_person(page, people[trainee])
        panel.get_by_role('tab', name='Schedule', exact=True).click()
        expect(section(panel, 'Attachments')).to_contain_text('no end')
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize('strength,starts_on,ends_on,active', [
    ('required', '2026-10-01', None, True), ('preferred', '2026-10-01', None, True),
    ('required', '2026-10-01', '2026-10-05', True),
    ('required', '2026-10-01', '2026-10-04', False), ('required', '2026-10-05', None, False)])
def test_attachment_defaults_the_trainee_supervisor_on_its_active_dates(browser, live_server, h, strength, starts_on, ends_on, active):
    # D65 (docs/user-guide.md:49-55): a new or worsened blocker refuses the edit, even with a reason.
    trainee, other, consultant, typ = setup(h, strength, starts_on=starts_on, ends_on=ends_on)
    workspace = h.shift(h.workspace('Attachment supervisor default'), typ=typ,
                        people=people(trainee, other, consultant))['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner/' + workspace['id'])
        open_session(page)
        page.get_by_role('button', name='Edit session', exact=True).click()
        form = page.locator('.dock-panel')
        trainee_row = form.locator(f'.nested-card:has(select[name="Participant"] option[value="{trainee}"]:checked)')
        expected = consultant if active else None
        expect(trainee_row.get_by_label('Named supervisor', exact=True)).to_have_value(expected or '')
        trainee_row.get_by_label('Named supervisor', exact=True).select_option('')
        before = h.detail(workspace)
        if active:
            with page.expect_response(lambda response: response.url.endswith('/api/commands')
                                      and response.request.method == 'POST') as refused:
                form.get_by_role('button', name='Save draft session', exact=True).click()
            assert refused.value.status == 422
            error = refused.value.json()['error']
            assert error['code'] == 'EDIT_INVALID'
            expected_finding = 'ATTACHMENT_PAIRING' if strength == 'required' else 'SUPERVISION_PAIRING'
            assert expected_finding in {f['code'] for f in error['details']['findings']}
            expect(form.get_by_role('alert')).to_be_visible()
            assert h.detail(workspace) == before
        else:
            save_dialog(form, 'Save draft session')
        participants = h.detail(workspace)['shifts'][0]['participants']
        assert next(p for p in participants if p['person_id'] == trainee)['supervisor_person_id'] == expected
        # The default is persisted, rather than only inferred in the edit form.
        page.reload()
        expect_loaded(page, 'planner')
        open_session(page)
        page.get_by_role('button', name='Edit session', exact=True).click()
        expect(trainee_row.get_by_label('Named supervisor', exact=True)).to_have_value(expected or '')
    finally:
        context.close()


def test_clearing_named_supervisors_reserves_the_required_attachment_place(browser, live_server, h):
    # D65 (docs/user-guide.md:49-55): a new or worsened blocker refuses the edit, even with a reason.
    flexible, constrained, first, second, typ = pairing_setup(h, 'required')
    reply = h.shift(h.workspace('Scarce required attachment'), typ=typ,
                    people=pairing_participants([flexible, constrained], [first, second]))
    assert reply['validation']['valid'], reply['validation']['findings']
    workspace = reply['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner/' + workspace['id'])
        open_session(page)
        page.get_by_role('button', name='Edit session', exact=True).click()
        form = page.locator('.dock-panel')
        for trainee in (flexible, constrained):
            row = form.locator(f'.nested-card:has(select[name="Participant"] option[value="{trainee}"]:checked)')
            row.get_by_label('Named supervisor', exact=True).select_option('')
        before = h.detail(workspace)
        with page.expect_response(lambda response: response.url.endswith('/api/commands')
                                  and response.request.method == 'POST') as refused:
            form.get_by_role('button', name='Save draft session', exact=True).click()
        assert refused.value.status == 422
        error = refused.value.json()['error']
        assert error['code'] == 'EDIT_INVALID'
        assert 'ATTACHMENT_PAIRING' in {f['code'] for f in error['details']['findings']}
        expect(form.get_by_role('alert')).to_be_visible()
        assert h.detail(workspace) == before
    finally:
        context.close()
