"""Session form: inline participant switches, formatted whole-session time, read-only, phone and dark mode."""
import re
import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate, save_dialog, open_session, open_rota  # noqa: F401

pytestmark = pytest.mark.browser
OVERTIME = re.compile('^Overtime — ')
KEEP = re.compile('^Keep assigned — ')


def edit_session(page):
    navigate(page, 'planner')
    open_session(page)
    page.get_by_role('button', name='Edit session', exact=True).click()
    return page.locator('.dock-panel')


def watch(page):
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    return errors


def test_participant_switches_are_inline_and_persist(browser, live_server, h):
    w = h.shift(h.workspace(), start='2026-10-04T22:00:00+03:00', end='2026-10-05T02:00:00+03:00')['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    errors = watch(page)
    try:
        form = edit_session(page)
        expect(form.get_by_text('Whole session for everyone · 4 Oct 2026, 22:00 – 5 Oct 2026, 02:00', exact=True)).to_be_visible()
        expect(page.get_by_text('More participant options')).to_have_count(0)
        assert not form.locator('details').count()
        name = form.get_by_label('Participant', exact=True).evaluate('s => s.selectedOptions[0].textContent')
        overtime = form.get_by_role('checkbox', name=f'Overtime — {name}', exact=True)
        keep = form.get_by_role('checkbox', name=f'Keep assigned — {name}', exact=True)
        expect(overtime).to_be_visible(); expect(keep).to_be_visible()
        overtime.check(); keep.check()
        save_dialog(form, 'Save draft session')
        saved = h.detail(w)['shifts'][0]['participants'][0]
        assert saved['overtime'] and saved['fixed']
        page.reload()
        form = edit_session(page)
        expect(form.get_by_role('checkbox', name=OVERTIME)).to_be_checked()
        expect(form.get_by_role('checkbox', name=KEEP)).to_be_checked()
        # Same-day sessions show the end as a time only.
        form.get_by_label('Session start', exact=True).fill('2026-10-06T08:00')
        form.get_by_label('Session end', exact=True).fill('2026-10-06T12:30')
        expect(form.get_by_text('Whole session for everyone · 6 Oct 2026, 08:00 – 12:30', exact=True)).to_be_visible()
        assert not errors
    finally:
        context.close()


def test_read_only_role_sees_no_session_editing(browser, live_server, h):
    w = h.shift(h.workspace())['workspace']
    context, page = sign_in(browser, live_server, 'auditor')
    try:
        open_rota(page, w['id'])
        open_session(page)
        expect(page.get_by_role('button', name='Edit session', exact=True)).to_have_count(0)
        expect(page.get_by_role('checkbox', name=OVERTIME)).to_have_count(0)
        expect(page.get_by_role('checkbox', name=KEEP)).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('viewport,scheme', [({'width': 390, 'height': 844}, 'light'), (None, 'dark')], ids=['phone', 'dark'])
def test_session_form_fits_phone_and_renders_dark(browser, live_server, h, viewport, scheme):
    h.shift(h.workspace())
    context, page = sign_in(browser, live_server, 'coordinator', viewport)
    errors = watch(page)
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'planner')
        # Phone has no List view: start from Add session and add a person.
        page.locator('#main').get_by_role('button', name='Add session', exact=True).click()
        form = page.locator('.dock-panel')
        form.get_by_role('button', name='Add participant', exact=True).click()
        expect(form.get_by_role('checkbox', name=KEEP)).to_be_visible()
        assert form.evaluate('d => d.scrollWidth <= d.clientWidth + 1')
        assert page.evaluate('document.scrollingElement.scrollWidth <= innerWidth + 1')
        if scheme == 'dark':
            bg = form.locator('.nested-card').first.evaluate('n => getComputedStyle(n).backgroundColor')
            assert sum(int(n) for n in re.findall(r'\d+', bg)[:3]) < 384, bg
        assert not errors
    finally:
        context.close()


def test_add_session_uses_shared_date_time_fields_and_order(browser, live_server, h):
    """SR-12 / LIVE-37: Add session asks Duty type → Clinics → Location → when, with the shared
    date field (no browser picker), and starts on the first day in view, not the rota's first day."""
    w = h.workspace('Add session order', '2026-10-04', '2026-10-25')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-12?edit={w['id']}")
        page.locator('#main').get_by_role('button', name='Add session', exact=True).click()
        form = page.locator('.dock-panel')
        text = form.inner_text()
        order = [text.index(x) for x in ('Duty type', 'Clinics', 'Location', 'Session start')]
        assert order == sorted(order), text[:400]
        expect(form.locator('input[type=date], input[type=datetime-local]')).to_have_count(0)
        start = form.get_by_label('Session start', exact=True).input_value()
        assert '11 Oct' in start or '12 Oct' in start, start  # the week in view, not 4 Oct
    finally:
        context.close()
