"""Cycle 4: checking a rota is not saving it; optional approval notes stay optional."""
import pytest
from playwright.sync_api import expect
from sqlalchemy import select
from apps.api import models as m
from apps.api.db import transaction
from .test_browser import browser, live_server, sign_in, open_rota, navigate, wait_held  # noqa: F401
from .test_browser_requests import leave, day

pytestmark = [pytest.mark.browser, pytest.mark.postgres,
              pytest.mark.parametrize('h', ['postgresql'], indirect=True)]


@pytest.mark.parametrize('note', ['', 'OK'])
def test_request_approval_does_not_invent_a_note(browser, live_server, h, note):
    row = leave(h, on=day(1))
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'requests?open=' + row['id'])
        page.get_by_role('region', name='Other').get_by_role('button', name='Approve', exact=True).click()
        dialog = page.get_by_role('dialog', name='Approve leave')
        dialog.get_by_label('Reason', exact=True).fill(note)
        dialog.get_by_role('button', name='Approve', exact=True).click()
        expect(dialog).not_to_be_visible()
        with transaction(h.app.state.engine) as db:
            assert db.get(m.RequestRecord, row['id']).decision_reason == note
            audit = db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == 'request.decide',
                                                       m.AuditEvent.target_id == row['id']))
            assert audit.reason == note
    finally:
        context.close()
