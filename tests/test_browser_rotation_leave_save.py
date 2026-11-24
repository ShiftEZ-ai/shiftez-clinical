"""A rotation saved over leave says so on save: the clash, what it means, and a way to the Timeline to fix it."""
import re
from datetime import date
from json import dumps

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, save_dialog  # noqa: F401

pytestmark = pytest.mark.browser

REASON = 'Synthetic rotation leave clash'


def rotation_with_leave_after(h, policy):
    programme = h.configure('programmes', {'name': 'Clash programme', 'starts_on': '2026-01-01'})['record']
    cohort = h.configure('cohorts', {'name': 'Clash cohort', 'programme_id': programme['id'],
                                     'starts_on': '2026-01-01', 'ends_on': '2027-01-01'})['record']
    person = h.seed['people'][0]
    h.configure('programme_memberships', {'person_id': person, 'programme_id': programme['id'],
                                          'starts_on': '2026-01-01', 'ends_on': '2027-01-01'})
    typ = h.configure('rotation_types', {'name': 'Clash rotation type', 'starts_on': '2026-01-01', 'leave_policy': policy})['record']
    rotation = h.configure('rotations', {'person_id': person, 'cohort_id': cohort['id'], 'rotation_type_id': typ['id'],
                                         'starts_on': '2026-10-01', 'ends_on': '2026-11-01'})['record']
    # Leave after the rotation ends: no clash yet.
    h.command('coordinator', 'availability.record', {'person_id': person, 'kind': 'recorded_leave',
              'start': '2026-11-10T08:00:00+03:00', 'end': '2026-11-10T16:00:00+03:00', 'private_reason': REASON})
    return rotation


def extend(page, rotation, until):
    navigate(page, 'programmes/rotations')
    page.locator(f'#main [data-row="{rotation["id"]}"]').get_by_role('button', name='Edit', exact=True).click()
    form = page.get_by_role('dialog')
    form.get_by_label('Effective until (included)', exact=True).fill(until)
    save_dialog(form, 'Save version')


def test_rotation_saved_over_leave_warns_explains_and_opens_the_timeline(browser, live_server, h):
    rotation = rotation_with_leave_after(h, 'needs_approval')
    context, page = sign_in(browser, live_server, 'admin')
    try:
        extend(page, rotation, '2026-11-30')
        warning = page.locator('#toast-tray .toast.warning')
        expect(warning).to_contain_text('Saved with 1 leave clash')
        expect(warning).to_contain_text('move one of them on the Programmes timeline')
        saved = next(r for r in h.get('admin', '/api/catalogues')['rotations'] if r['person_id'] == rotation['person_id'])
        assert saved['ends_on'] == '2026-12-01'
        warning.get_by_role('button', name='Open timeline', exact=True).click()
        expect(page).to_have_url(re.compile(r'#programmes/board$'))
        expect(page.get_by_role('heading', name='Programmes & rotations', exact=True)).to_be_visible()
    finally:
        context.close()


def test_rotation_saved_without_leave_shows_no_clash(browser, live_server, h):
    rotation = rotation_with_leave_after(h, 'needs_approval')
    context, page = sign_in(browser, live_server, 'admin')
    try:
        extend(page, rotation, '2026-11-05')
        expect(page.locator('#toast-tray .toast').filter(has_text='Published rotas stay as they were')).to_be_visible()
        expect(page.locator('#toast-tray .toast.warning')).to_have_count(0)
    finally:
        context.close()


def test_import_review_and_rotation_show_the_retained_leave_conflict(browser, live_server, h, monkeypatch):
    from apps.api.services import independence
    from .test_rotation_leave_policy import record_away, resident

    monkeypatch.setattr(independence, 'today', lambda db, department_id: date(2026, 11, 1))
    _, value, _ = resident(h, create=False)
    record_away(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        navigate(page, 'programmes/board')
        page.get_by_role('button', name='More rotation actions').click()
        page.get_by_role('menuitem', name='Import reference rotations').click()
        upload = page.get_by_role('dialog', name='Import reference rotations')
        upload.get_by_label('Reference file').set_input_files({
            'name': 'rotations.json', 'mimeType': 'application/json',
            'buffer': dumps({'rotations': [value]}).encode('utf-8'),
        })
        upload.get_by_label('Reason', exact=True).fill(REASON)
        upload.get_by_role('button', name='Review file').click()
        review = page.get_by_role('dialog', name='Review reference rotations')
        expect(review).to_contain_text('Leave or time away is not allowed during this rotation.')
        review.get_by_role('button', name='Import rotations').click()
        expect(page.locator('#toast-tray')).to_contain_text('Reference rotations imported')
        rotation = h.get('coordinator', '/api/catalogues')['rotations'][0]
        page.locator(f'.tl button[data-bar="{rotation["id"]}"]').click()
        expect(page.locator('.dock-panel').get_by_role('heading', name='Leave conflict at import')).to_be_visible()
    finally:
        context.close()
