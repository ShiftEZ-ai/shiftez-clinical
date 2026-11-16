"""LEAD-QA WCAG pass 2 (v11, 235391bec), fixed once in the shared helpers (dom.ts). Runs in CI only.

- maintenance (3.3.2): a form marks the fewer kind of field in words, "(optional)" or "(required)", never an asterisk; the
  words stay out of the field's name (W3C Understanding 2.5.3, text in parentheses).
- maintenance (3.3.1, 4.1.3): a wrong value is marked (aria-invalid, its message as description) and said politely on leaving
  the field; Save focuses the first problem and says it. Typing is never blocked.
- maintenance (1.4.12): text cut by .truncate shows in full in the shared tooltip.
- maintenance (4.1.2, 3.2.2): "Review and approve" does what it says from the keyboard: opens the effect review, approves
  nothing; the decision is that review's "Approve and activate".
"""
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401
from .governance_support import developer
from .test_browser_accounts import users_file  # noqa: F401  (accounts on: Invite person is offered)
from .test_browser_qa049_proposal_decline import proposal

pytestmark = pytest.mark.browser


def open_new_duty_type(page):
    navigate(page, 'configuration')
    page.get_by_role('button', name='Duty types', exact=True).click()
    page.get_by_role('button', name='New duty type', exact=True).click()
    return page.get_by_role('dialog')


def described(page, field):
    return page.evaluate("f=>(f.getAttribute('aria-describedby')||'').split(' ').map(id=>document.getElementById(id)?.textContent||'').join(' ')", field.element_handle())


def test_duty_type_marks_the_fewer_kind_of_field_in_words(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        dialog = open_new_duty_type(page)
        expect(dialog.get_by_label('Name', exact=True)).to_have_accessible_name('Name')
        expect(dialog.locator('.required-mark')).to_have_count(0)
        # The cue is drawn by CSS: seen in words, never part of the label's text (so label lookups and voice control
        # still find "Name"), and never read as part of the name.
        cues = dialog.locator('[data-cue]')
        expect(cues.first).to_be_visible()
        said = set(cues.evaluate_all("ns => ns.map(n => n.dataset.cue)"))
        assert len(said) == 1 and said <= {'optional', 'required'}, said
        assert f'({next(iter(said))})' in cues.first.evaluate("n => getComputedStyle(n, '::after').content")
        assert '(' not in cues.first.text_content()
        if said == {'optional'}:
            # The optional field says so in its description; a required one never reads as optional.
            optional = dialog.locator('label[data-cue]').first
            control = dialog.locator(f'#{optional.get_attribute("for")}')
            assert 'optional' in described(page, control)
        expect(dialog.locator('label[for][data-cue=optional]', has_text='Name')).to_have_count(0)
    finally:
        context.close()


def test_a_wrong_duration_is_said_on_leaving_it_and_save_focuses_the_first_problem(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        dialog = open_new_duty_type(page)
        duration = dialog.get_by_label('Maximum duration · minutes', exact=True)
        duration.fill('-5')
        expect(duration).to_have_value('-5')  # typing is never blocked
        duration.press('Tab')
        expect(duration).to_have_attribute('aria-invalid', 'true')
        assert 'From 15' in described(page, duration)
        expect(page.locator('#sr-live-polite')).to_contain_text('Maximum duration')
        dialog.locator('.form-footer .button.primary').click()
        focused = page.locator(':focus')
        expect(focused).to_have_attribute('aria-invalid', 'true')
        expect(page.locator('#sr-live-polite')).to_contain_text('to fix')  # New: Name and others are empty too
        expect(duration).to_have_attribute('aria-invalid', 'true')
        duration.fill('30')
        expect(duration).not_to_have_attribute('aria-invalid', 'true')
    finally:
        context.close()


def test_a_wrong_email_is_said_on_leaving_it(users_file, browser, live_server, h):
    context, page = sign_in(browser, live_server, developer(h))
    try:
        navigate(page, 'access/people')
        page.get_by_role('button', name='Invite person', exact=True).click()
        form = page.get_by_role('dialog')
        email = form.get_by_label('Email', exact=True)
        email.fill('not-an-email')
        email.press('Tab')
        expect(email).to_have_attribute('aria-invalid', 'true')
        assert 'Enter an email address' in described(page, email)
        # The problem said aloud names the field once: its words already say "email" (dom.ts problemSaid, 40f90233d).
        expect(page.locator('#sr-live-polite')).to_contain_text('Enter an email address')
        form.get_by_role('button', name='Create invitation', exact=True).click()
        expect(page.locator(':focus')).to_have_attribute('aria-invalid', 'true')
        expect(page.get_by_role('dialog').filter(has_text='Invitation link')).to_have_count(0)
    finally:
        context.close()


def test_cut_text_shows_in_full_in_the_shared_tooltip(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'home')
        full = 'Dr. Fictional Longname Example Person'
        page.evaluate("""t=>{const s=document.createElement('span');s.className='truncate';s.id='cut-probe';s.textContent=t;
            s.style.setProperty('display','block');s.style.setProperty('width','60px');document.querySelector('#main').prepend(s);}""", full)
        page.locator('#cut-probe').hover()
        expect(page.get_by_role('tooltip')).to_have_text(full)
    finally:
        context.close()


def test_review_and_approve_opens_the_review_from_the_keyboard_and_approves_nothing(browser, live_server, h):
    rule, proposed = proposal(h)
    context, page = sign_in(browser, live_server, 'approver')
    activations = []
    page.on('request', lambda r: activations.append(r) if r.method == 'POST' and 'policy.activate' in (r.post_data or '') else None)
    try:
        navigate(page, 'rules/days_in_a_row')
        trigger = page.locator('#main').get_by_role('button', name='Review and approve', exact=True)
        trigger.focus()
        page.keyboard.press('Enter')
        panel = page.locator('.rc-line-panel')
        expect(panel).to_be_visible()
        expect(panel.get_by_role('button', name='Approve and activate', exact=True)).to_be_visible()
        expect(page.locator('.rc-waiting')).to_be_visible()  # still waiting: nothing was decided
        assert not activations
    finally:
        context.close()
