"""A give-away from Requests: the colleague accepts the duty without offering one back, the proposer confirms."""
from datetime import timedelta

import pytest
from playwright.sync_api import expect

from packages.domain.time import now
from .governance_support import identity
from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401
from .test_browser_requests import published, row

pytestmark = pytest.mark.browser


def test_a_give_away_is_accepted_and_confirmed_in_requests(browser, live_server, h):
    identity(h, 'staff_two', ['staff'], h.seed['people'][1])
    publication = published(h)
    duty = h.get('staff', '/api/swaps/options')['assignments'][0]
    swap = h.command('staff', 'swap.offer', {'kind': 'give', 'publication_id': publication,
                     'assignment_id': duty['assignment_id'], 'recipient_person_id': h.seed['people'][1],
                     'expires_at': (now() + timedelta(days=1)).isoformat(), 'reason': 'Can you take this one?'})['swap']
    context, page = sign_in(browser, live_server, 'staff_two')
    try:
        navigate(page, 'requests')
        expect(row(page, 'Swap')).to_contain_text('given away')
        expect(row(page, 'Swap').get_by_role('button', name='Offer a duty in return', exact=True)).to_have_count(0)
        row(page, 'Swap').get_by_role('button', name='Accept duty', exact=True).click()
        dialog = page.get_by_role('dialog', name='Accept duty')
        expect(dialog).to_contain_text('give nothing in return')
        dialog.get_by_label('Reason', exact=True).fill('Happy to cover')
        dialog.get_by_role('button', name='Accept duty', exact=True).click()
        expect(page.get_by_text('Accepted. They confirm next', exact=False)).to_be_visible()
    finally:
        context.close()
    assert h.get('staff', '/api/swaps/' + swap['id'])['swap']['status'] == 'counteroffered'
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'requests')
        row(page, 'Swap').get_by_role('button', name='Confirm swap', exact=True).click()
        dialog = page.get_by_role('dialog', name='Confirm swap')
        expect(dialog).to_contain_text('you receive no duty in return')
        dialog.get_by_label('Reason', exact=True).fill('Thank you')
        dialog.get_by_role('button', name='Confirm swap', exact=True).click()
        # Automatic preparation is on by default: the hand-over goes straight to independent approval.
        expect(page.get_by_text('The rota change was prepared and sent for independent approval', exact=False)).to_be_visible()
    finally:
        context.close()
    assert h.get('staff', '/api/swaps/' + swap['id'])['swap']['status'] == 'reviewing'


def test_accepting_a_give_away_can_be_undone(browser, live_server, h):
    identity(h, 'staff_two', ['staff'], h.seed['people'][1])
    publication = published(h)
    duty = h.get('staff', '/api/swaps/options')['assignments'][0]
    h.command('staff', 'swap.offer', {'kind': 'give', 'publication_id': publication,
              'assignment_id': duty['assignment_id'], 'recipient_person_id': h.seed['people'][1],
              'expires_at': (now() + timedelta(days=1)).isoformat(), 'reason': 'Can you take this one?'})
    context, page = sign_in(browser, live_server, 'staff_two')
    try:
        navigate(page, 'requests')
        row(page, 'Swap').get_by_role('button', name='Accept duty', exact=True).click()
        dialog = page.get_by_role('dialog', name='Accept duty')
        dialog.get_by_label('Reason', exact=True).fill('Happy to cover')
        dialog.get_by_role('button', name='Accept duty', exact=True).click()
        toast = page.locator('#toast-tray .toast', has_text='Accepted. They confirm next')
        toast.get_by_role('button', name='Undo', exact=True).click()
        expect(page.locator('#toast-tray').get_by_text('Undone.', exact=False)).to_be_visible()
        # Undo goes forward: the offer is open again for this colleague to accept.
        expect(row(page, 'Swap').get_by_role('button', name='Accept duty', exact=True)).to_be_visible()
    finally:
        context.close()


def test_an_eligible_colleague_accepts_an_open_give_away_in_requests(browser, live_server, h):
    from tests.outbox_support import process_one_after_undo as process_one
    identity(h, 'staff_two', ['staff'], h.seed['people'][1])
    publication = published(h)
    duty = h.get('staff', '/api/swaps/options')['assignments'][0]
    swap = h.command('staff', 'swap.offer', {'kind': 'give', 'publication_id': publication,
                     'assignment_id': duty['assignment_id'], 'recipient_person_id': None,
                     'expires_at': (now() + timedelta(days=1)).isoformat(), 'reason': 'Anyone free?'})['swap']
    for _ in range(30):
        if not process_one(h.app.state.engine):
            break
    context, page = sign_in(browser, live_server, 'staff_two')
    try:
        navigate(page, 'requests')
        expect(row(page, 'Open give-away')).to_contain_text('given away')
        row(page, 'Open give-away').get_by_role('button', name='Accept duty', exact=True).click()
        dialog = page.get_by_role('dialog', name='Accept duty')
        dialog.get_by_label('Reason', exact=True).fill('I can take it')
        dialog.get_by_role('button', name='Accept duty', exact=True).click()
        expect(page.get_by_text('Accepted. They confirm next', exact=False)).to_be_visible()
    finally:
        context.close()
    claimed = h.get('staff', '/api/swaps/' + swap['id'])['swap']
    assert claimed['status'] == 'counteroffered' and claimed['recipient_person_id'] == h.seed['people'][1]



def test_accepting_an_open_give_away_can_be_undone(browser, live_server, h):
    from tests.outbox_support import process_one_after_undo as process_one
    identity(h, 'staff_two', ['staff'], h.seed['people'][1])
    publication = published(h)
    duty = h.get('staff', '/api/swaps/options')['assignments'][0]
    swap = h.command('staff', 'swap.offer', {'kind': 'give', 'publication_id': publication,
                     'assignment_id': duty['assignment_id'], 'recipient_person_id': None,
                     'expires_at': (now() + timedelta(days=1)).isoformat(), 'reason': 'Anyone free?'})['swap']
    for _ in range(30):
        if not process_one(h.app.state.engine):
            break
    context, page = sign_in(browser, live_server, 'staff_two')
    try:
        navigate(page, 'requests')
        row(page, 'Open give-away').get_by_role('button', name='Accept duty', exact=True).click()
        dialog = page.get_by_role('dialog', name='Accept duty')
        dialog.get_by_label('Reason', exact=True).fill('I can take it')
        dialog.get_by_role('button', name='Accept duty', exact=True).click()
        toast = page.locator('#toast-tray .toast', has_text='Accepted. They confirm next')
        toast.get_by_role('button', name='Undo', exact=True).click()
        expect(page.locator('#toast-tray').get_by_text('Undone.', exact=False)).to_be_visible()
    finally:
        context.close()
    # Undo goes forward: the claimed swap closes and a new open offer takes its place.
    assert h.get('staff', '/api/swaps/' + swap['id'])['swap']['status'] == 'withdrawn'
    for _ in range(30):
        if not process_one(h.app.state.engine):
            break
    context, page = sign_in(browser, live_server, 'staff_two')
    try:
        navigate(page, 'requests')
        expect(row(page, 'Open give-away').get_by_role('button', name='Accept duty', exact=True)).to_be_visible()
    finally:
        context.close()
