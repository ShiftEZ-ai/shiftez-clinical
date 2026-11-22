"""A person deactivated from a date in the edit form (#1155): the date saves, survives a reload, is refused when it is
not a date, and Undo puts the earlier state back."""
from datetime import timedelta

import pytest
from playwright.sync_api import expect

from apps.api.auth import _local_today
from apps.api.db import transaction
from .test_browser import browser, live_server, sign_in, expect_loaded  # noqa: F401
from .test_browser_person_profile import open_person, person

pytestmark = pytest.mark.browser


def _saved(h, person_id):
    return next(p for p in h.get('coordinator', '/api/catalogues')['people'] if p['id'] == person_id)


def _edit(page, p):
    open_person(page, p).get_by_role('button', name=f"Edit {p['display_name']}", exact=True).click()
    return page.get_by_role('dialog', name='Edit person', exact=True)


def test_dated_deactivation_saves_survives_reload_and_undo_restores_it(browser, live_server, h):
    p = person(h)
    with transaction(h.app.state.engine) as db:
        today = _local_today(db, h.seed['department_id'])
    first, moved = (today + timedelta(days=30)).isoformat(), (today + timedelta(days=45)).isoformat()
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        form = _edit(page, p)
        expect(form.get_by_label('Inactive from (optional)', exact=True)).to_be_hidden()
        form.get_by_label('Active for future use', exact=True).uncheck()
        form.get_by_label('Inactive from (optional)', exact=True).fill(first)
        form.get_by_label('Change reason', exact=True).fill('Leaves next month')
        form.get_by_role('button', name='Save version', exact=True).click()
        expect(form).to_have_count(0)
        expect(page.locator('#toast-tray .toast').filter(has_text='saved and is live')).to_be_visible()
        saved = _saved(h, p['id'])
        assert saved['active'] is False and saved['inactive_from'] == first, saved

        page.reload()
        expect_loaded(page, 'people')
        form = _edit(page, p)
        expect(form.get_by_label('Active for future use', exact=True)).not_to_be_checked()
        date = form.get_by_label('Inactive from', exact=True)
        expect(date).to_have_value(first)
        date.fill(moved)
        form.get_by_role('button', name='Save version', exact=True).click()
        expect(form).to_have_count(0)
        toast = page.locator('#toast-tray .toast').filter(has_text='saved and is live').last
        toast.get_by_role('button', name='Undo', exact=True).click()
        expect(page.locator('#toast-tray')).to_contain_text('Undone')
        restored = _saved(h, p['id'])
        assert restored['active'] is False and restored['inactive_from'] == first, restored

        page.reload()
        expect_loaded(page, 'people')
        expect(_edit(page, p).get_by_label('Inactive from', exact=True)).to_have_value(first)
    finally:
        context.close()


def test_an_inactive_date_that_is_not_a_date_is_said_beside_the_field(browser, live_server, h):
    p = person(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    sent = []
    # A save (/api/commands), not the form's impact preview.
    page.on('request', lambda r: r.method == 'POST' and '/api/commands' in r.url and 'catalogue.update' in (r.post_data or '') and sent.append(r))
    try:
        form = _edit(page, p)
        form.get_by_label('Active for future use', exact=True).uncheck()
        date = form.get_by_label('Inactive from (optional)', exact=True)
        date.fill('next month')
        form.get_by_role('button', name='Save version', exact=True).click()
        expect(date).to_have_attribute('aria-invalid', 'true')
        expect(date).to_be_focused()
        expect(form.locator('.field-error').filter(has_text='Enter a date')).to_be_visible()
        assert not sent
        assert _saved(h, p['id'])['active'] is True
    finally:
        context.close()
