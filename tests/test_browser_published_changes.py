"""Changes to a published rota (mockups B1, B2; D5, D10–D12): the published session's person menu and session actions,
Replace (pick → review → send) with Urgent only when an urgent route is active, the change list with Undo and a Send
that waits for the Must fix, the armed Close the session (SX-16: one name), the phone sheet and Send while offline.

The maintenance endpoints are not in this branch yet: `FakeChanges` fulfils `GET /api/publications/{id}/pending-changes`,
the `publication.*` commands (incl. `publication.changes.prepare`, whose hidden copy is answered by fill-options) in
the final contract's shapes (build/contracts/maintenance). Drop the fulfilment when maintenance lands; everything else is the real
app and API."""
import os
import re
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect

from packages.domain.time import now as clock_now
from .test_browser import browser, live_server, sign_in  # noqa: F401
from .test_browser_schedule import show

pytestmark = pytest.mark.browser
SHOTS = os.getenv('PUBLISHED_CHANGES_SHOTS')  # a folder: B1/B2 states per size and theme, for review against the mockups
PHONE = {'width': 390, 'height': 844}
DESKTOP = {'width': 1440, 'height': 900}


def local_today():
    return clock_now().astimezone(ZoneInfo('Asia/Riyadh')).date()


def published(h):
    """A published rota from today for two weeks: tomorrow afternoon Alex and Jordan, in three days Taylor."""
    a, b, c = h.seed['people']
    day = local_today()
    at = lambda d, hour: f'{d.isoformat()}T{hour:02}:00:00+03:00'
    w = h.workspace('Published changes rota', day.isoformat(), (day + timedelta(days=14)).isoformat())
    for d, people in ((day + timedelta(days=1), [a, b]), (day + timedelta(days=3), [c])):
        w = h.shift(w, start=at(d, 13), end=at(d, 16), people=[{'person_id': p, 'start': at(d, 13), 'end': at(d, 16)} for p in people])['workspace']
    h.publish(w)
    return day + timedelta(days=1), day + timedelta(days=3)


class FakeChanges:
    """maintenance in the documented shapes: one hidden change list, its revision, the urgent route and the commands sent."""
    def __init__(self, h, urgent=False, closes=False):
        self.h, self.changes, self.revision, self.sent, self.prepared, self.closes = h, [], 1, [], 0, closes
        self.route = {'active': urgent, 'approver_count': 3}
        a, b, c = h.seed['people']
        ok = lambda pid, name, hours: {'person_id': pid, 'display_name': name, 'status': 'ok', 'findings': [], 'week_hours': hours,
                                       'rota_hours': hours * 4, 'rest_before_hours': 18, 'can_lead': False, 'fills': True, 'override_allowed': True}
        blocked = lambda pid, name, kind, code, message, allowed: {'person_id': pid, 'display_name': name, 'status': 'blocked', 'week_hours': 0,
            'rota_hours': 0, 'rest_before_hours': None, 'fills': True, 'override_allowed': allowed,
            'findings': [{'kind': kind, 'code': code, 'severity': 'error', 'message': message}]}
        self.candidates = [ok(c, 'Taylor Noor', 6), ok('p-sk', 'Keiko Stone', 9),
                           blocked('p-zr', 'Grace Xu', 'CLASH', 'TIME_OVERLAP', 'On duty 13:00–16:00 at another clinic', False),
                           blocked('p-aa', 'Keiko Underwood', 'LEAVE', 'UNAVAILABLE', 'Annual leave to Thursday', False),
                           blocked('p-ma', 'Grace Jensen', 'RULE', 'MIN_REST', 'Minimum rest: 9 h since the last duty, needs 11 h', True)]

    def set(self):
        return {'workspace': {'id': 'hidden', 'revision': self.revision, 'validation': {'error_count': 0}} if self.changes else None,
                'changes': self.changes, 'urgent_route': self.route}

    def install(self, page):
        self.shifts = self.copies()  # read now: a route callback only fulfils (conftest Harness.route_json)
        page.route('**/api/publications/*/pending-changes', lambda route: route.fulfill(json=self.set()))
        page.route('**/api/planner/fill-options', self.options)
        page.route('**/api/commands', self.command)

    def copies(self):
        """The hidden draft's sessions: the issued ones (the same slots), as `publication.changes.prepare` returns them."""
        day = local_today()
        window = self.h.get('coordinator', f'/api/schedule/sessions?start={day.isoformat()}&end={(day + timedelta(days=15)).isoformat()}')
        return [s for s in window['shifts'] if not s.get('context')]

    def options(self, route):
        body = route.request.post_data_json or {}
        if body.get('workspace_id') != 'hidden':
            return route.fallback()
        route.fulfill(json={'candidates': self.candidates, 'basis': 'People who pass every rule for this session, best first.'})

    def command(self, route):
        body = route.request.post_data_json or {}
        kind, payload = body.get('kind', ''), body.get('payload') or {}
        if not kind.startswith('publication.'):
            return route.fallback()
        if kind == 'publication.changes.prepare':  # reads (or starts) the hidden list; no change is made
            self.prepared += 1
            return route.fulfill(json={**self.set(), 'workspace': {'id': 'hidden', 'revision': self.revision}, 'shifts': self.shifts})
        self.sent.append((kind, payload, body.get('expected_revision')))
        self.revision += 1
        reply = {}
        if kind == 'publication.change':
            change = {'id': f'c{len(self.changes) + 1}', 'status': 'draft', 'workspace_id': 'hidden', 'created_at': clock_now().isoformat(), **payload}
            if payload['kind'] == 'absence':
                change['must_fix'] = [{'severity': 'error', 'code': 'MIN_PARTICIPANTS', 'message': '1 place open on this session',
                                       'shift_id': payload['shift_id']}]
                if self.closes:  # maintenance / D38: the only person's absence leaves nobody, but never closes the session
                    change['sessions'] = [{'id': payload['shift_id'], 'closed': False, 'closes_session': True,
                                           'open_places': 1, 'replace_available': True}]
            self.changes = [*self.changes, change]
            reply = {'change': change, 'validation': {'error_count': 0, 'warning_count': 0, 'scope': 'published_changes'}, 'issues': {'added': []}}
        else:
            ids = ([c['id'] for c in self.changes if c['status'] == 'submitted'] if kind == 'publication.changes.withdraw'
                   else payload.get('change_ids') or ([payload['change_id']] if payload.get('change_id') else [c['id'] for c in self.changes if c['status'] == 'draft']))
            status = {'publication.change.undo': 'withdrawn', 'publication.changes.submit': 'submitted', 'publication.changes.withdraw': 'draft'}[kind]
            self.changes = [{**c, 'status': status, 'urgency': payload.get('urgency'), 'submitted_at': clock_now().isoformat()} if c['id'] in ids else c
                            for c in self.changes]
        route.fulfill(json={'workspace': {'id': 'hidden', 'revision': self.revision}, 'changes': self.changes, 'urgent_route': self.route, **reply})


def open_session(page, h, day, person=0):
    show(page, f'schedule/week/{day.isoformat()}')
    chip = page.locator(f'#schedule-view [data-person~="{h.seed["people"][person]}"]:visible').first
    expect(chip).to_be_visible()  # the grid has settled (a redraw replaces its chips)
    chip.click()
    panel = page.locator('.dock-panel')
    expect(panel).to_be_visible()
    return panel


def shot(page, name, size, theme):
    if SHOTS:
        page.screenshot(path=os.path.join(SHOTS, f'{name}--{size}-{theme}.png'))


def test_a_coordinator_changes_a_person_on_a_published_session_and_staff_cannot(browser, live_server, h):
    tomorrow, _ = published(h)
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        FakeChanges(h).install(page)
        panel = open_session(page, h, tomorrow)
        panel.get_by_role('button', name='Change Alex Morgan on this session').click()
        menu = page.get_by_role('menu', name='Change Alex Morgan')
        for item in ('Replace…', 'Swap with another duty…', 'Mark absent · Sick', 'Mark absent · Leave', 'Mark absent · Emergency', 'Mark absent · Other', 'Move to another session…'):
            expect(menu.get_by_role('menuitem', name=item)).to_be_visible()
        page.keyboard.press('Escape')
        expect(panel.get_by_role('button', name='Add a person')).to_be_visible()
        expect(panel.get_by_role('button', name='Close the session…')).to_be_visible()
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'staff', DESKTOP)
    try:
        panel = open_session(page, h, tomorrow)
        expect(panel.get_by_role('heading', level=3, name='People')).to_be_visible()
        expect(panel.get_by_role('button', name=re.compile('on this session$'))).to_have_count(0)
        expect(panel.get_by_role('button', name='Close the session…')).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('urgent', [False, True], ids=['no-urgent-route', 'urgent-route'])
def test_replace_picks_reviews_and_sends_with_urgent_only_when_a_route_is_active(browser, live_server, h, urgent):
    tomorrow, _ = published(h)
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        fake = FakeChanges(h, urgent)
        fake.install(page)
        panel = open_session(page, h, tomorrow)
        panel.get_by_role('button', name='Change Alex Morgan on this session').click()
        page.get_by_role('menuitem', name='Replace…').click()
        expect(panel.get_by_role('heading', name='Replace Alex Morgan')).to_be_visible()
        expect(panel.get_by_role('button', name='Back to session')).to_be_visible()
        expect(panel.get_by_role('radio', name='Sick')).to_have_attribute('aria-checked', 'true')
        # One primary: the first who passes every rule; every Assign says whom.
        expect(panel.locator('.wk-cand .button.primary')).to_have_count(1)
        expect(panel.get_by_role('button', name='Assign Taylor Noor')).to_have_class(re.compile('primary'))
        blocked = panel.locator('.wk-blocked li').filter(has_text=fake.candidates[-1]['display_name'])
        expect(blocked).to_be_visible()
        expect(blocked).to_contain_text(fake.candidates[-1]['findings'][0]['message'])
        # A published change never breaks a rule on purpose (maintenance has no override): not even an overridable one.
        expect(panel.get_by_role('button', name=re.compile('^Break rule and assign'))).to_have_count(0)
        assert fake.prepared == 1  # candidates come from the hidden draft's copy of the session
        panel.get_by_role('radio', name='Leave').click()
        panel.get_by_role('button', name='Assign Taylor Noor').click()
        # Review: was / now, the scoped check, the route, who is told; the change is in the list, not sent.
        expect(panel.get_by_text('Passes every check')).to_be_visible()
        expect(panel.locator('.change-summary')).to_contain_text('Alex Morgan · leave')
        expect(panel.locator('.change-summary')).to_contain_text('Taylor Noor')
        expect(page.locator('.pc-chip')).to_have_text('1 change · not sent')
        expect(panel.get_by_role('radio', name=re.compile('^Urgent'))).to_have_count(1 if urgent else 0)
        expect(panel.get_by_text('Taylor Noor and Alex Morgan are told once it is approved.')).to_be_visible()
        kind, payload, _ = fake.sent[0]
        assert kind == 'publication.change' and payload['kind'] == 'replace' and payload['absence_reason'] == 'leave'  # the wire key (DL-035, AbsenceCategory); its label is 'Leave'
        assert payload['person_id'] == h.seed['people'][2]
        send = panel.get_by_role('button', name='Send for urgent approval' if urgent else 'Send for approval')
        send.click()
        expect(page.locator('.toast').filter(has_text='Taylor Noor shows as waiting until it is approved')).to_be_visible()
        assert fake.sent[-1][0] == 'publication.changes.submit' and fake.sent[-1][1]['urgency'] == ('urgent' if urgent else 'routine')
        assert ('change_ids' in fake.sent[-1][1]) == urgent  # urgent goes alone; routine sends the whole list
        # Sent: the panel says so with Withdraw; the grid shows who leaves and who joins; the chip waits (gold).
        expect(panel.get_by_role('button', name='Withdraw')).to_be_visible()
        expect(panel.get_by_text(re.compile('^Waiting for approval'))).to_be_visible()
        expect(page.locator('.pc-chip')).to_have_text('1 change waiting')
        expect(page.locator(f'#schedule-view .pchip.pc-leaving[data-person="{h.seed["people"][0]}"]')).to_have_count(1)
        # The shared person pill carries its full, short and initials forms (one shows by width): read its name.
        expect(page.locator('#schedule-view .pchip.pc-joining')).to_have_attribute('data-tip', 'Taylor Noor')
        page.locator('.toast').get_by_role('button', name='Undo').click()
        expect(page.locator('.pc-chip')).to_have_text('1 change · not sent')
        assert fake.sent[-1][0] == 'publication.changes.withdraw' and set(fake.sent[-1][1]) == {'reason'}
    finally:
        context.close()


def test_the_change_list_undoes_and_waits_for_the_open_place_before_sending(browser, live_server, h):
    tomorrow, _ = published(h)
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        fake = FakeChanges(h)
        fake.install(page)
        panel = open_session(page, h, tomorrow)
        # A closing started and left with Keep it open leaves nothing unsaved: the list then opens without the guard.
        panel.get_by_role('button', name='Close the session…').click()
        panel.get_by_label('Reason').fill('Typed, then kept')
        panel.get_by_role('button', name='Keep it open').click()
        panel.get_by_role('button', name='Change Jordan Lee on this session').click()
        page.get_by_role('menuitem', name='Mark absent · Sick').click()
        expect(page.locator('.toast').filter(has_text='Jordan Lee marked absent')).to_be_visible()
        page.locator('.pc-chip').click()
        expect(page.get_by_text('Not saved.')).to_have_count(0)
        changes = page.locator('.dock-panel.pc-panel')
        expect(changes.get_by_role('heading', name=re.compile('^Changes to '))).to_be_visible()
        row = changes.locator('.pc-changes li')
        expect(row).to_have_count(1)
        expect(row).to_contain_text('Absence')
        expect(row).to_contain_text('Jordan Lee · absent')
        expect(row).to_contain_text('1 must fix')
        expect(row.get_by_role('button', name='Find someone')).to_be_visible()
        send = changes.get_by_role('button', name='Send 1 change for approval')
        expect(send).to_be_disabled()
        expect(changes.get_by_text('Fill the open place to send')).to_be_visible()
        row.get_by_role('button', name='Undo this change: Jordan Lee · absent').click()
        expect(changes.get_by_text('No changes.')).to_be_visible()
        expect(page.locator('.pc-chip')).to_have_count(0)
        assert [k for k, _, _ in fake.sent] == ['publication.change', 'publication.change.undo']
        assert fake.sent[1][2] == 2  # the hidden list's revision binds the Undo
    finally:
        context.close()


def test_cancel_session_needs_a_reason_and_the_armed_confirm(browser, live_server, h):
    _, later = published(h)
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        fake = FakeChanges(h, urgent=True)
        fake.install(page)
        panel = open_session(page, h, later, person=2)
        panel.get_by_role('button', name='Close the session…').click()
        expect(panel.get_by_text('Taylor Noor is freed and told once it is approved. You can undo it.')).to_be_visible()
        expect(panel.get_by_role('radio', name=re.compile('^Routine'))).to_be_checked()  # a later day
        panel.get_by_label('Reason').fill('Clinic closed for an inspection')
        panel.get_by_role('button', name='Close session').click()
        confirm = panel.get_by_role('button', name=re.compile('^Confirm close'))
        expect(confirm).to_have_class(re.compile('armed'))
        assert fake.sent == []
        confirm.click()
        expect(page.locator('.toast').filter(has_text='Closing added to the changes')).to_be_visible()
        assert [(k, p['kind'], p['reason']) for k, p, _ in fake.sent] == [('publication.change', 'cancel', 'Clinic closed for an inspection')]
        expect(page.locator('.pc-chip')).to_have_text('1 change · not sent')
    finally:
        context.close()


def test_a_coordinator_replaces_someone_from_a_phone(browser, live_server, h):
    tomorrow, _ = published(h)
    # The phone scope is on (no "Continue anyway"): the published Schedule opens for a coordinator (D10).
    context, page = sign_in(browser, live_server, 'coordinator', PHONE, phone_notice=True)
    try:
        FakeChanges(h).install(page)
        show(page, f'schedule/week/{tomorrow.isoformat()}')
        expect(page.get_by_role('heading', name='Best on a desktop')).to_have_count(0)
        page.locator(f'#schedule-view [data-person~="{h.seed["people"][0]}"]').first.click()
        panel = page.locator('.dock-panel')
        panel.get_by_role('button', name='Change Alex Morgan on this session').click()
        page.get_by_role('menuitem', name='Replace…').click()
        panel.get_by_role('button', name='Assign Taylor Noor').click()
        expect(panel.get_by_role('button', name='Send for approval')).to_be_visible()
        assert page.evaluate('() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1')
    finally:
        context.close()


def test_send_waits_while_offline_and_keeps_the_change(browser, live_server, h):
    tomorrow, _ = published(h)
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        FakeChanges(h, urgent=True).install(page)
        panel = open_session(page, h, tomorrow)
        panel.get_by_role('button', name='Change Alex Morgan on this session').click()
        page.get_by_role('menuitem', name='Replace…').click()
        panel.get_by_role('button', name='Assign Taylor Noor').click()
        send = panel.get_by_role('button', name='Send for urgent approval')
        expect(send).to_be_enabled()
        context.set_offline(True)
        expect(send).to_be_disabled()
        expect(panel.get_by_text('Offline. Your change is kept here; send it when the connection is back.')).to_be_visible()
        context.set_offline(False)
        expect(send).to_be_enabled()
    finally:
        context.close()


@pytest.mark.skipif(not SHOTS, reason='set PUBLISHED_CHANGES_SHOTS to a folder to render the B1/B2 states')
@pytest.mark.parametrize('theme', ['light', 'dark'])
@pytest.mark.parametrize('size', ['desktop', 'phone'])
def test_render_published_changes_states(browser, live_server, h, size, theme):
    tomorrow, later = published(h)
    context, page = sign_in(browser, live_server, 'coordinator', PHONE if size == 'phone' else DESKTOP, color_scheme=theme)
    try:
        FakeChanges(h, urgent=True).install(page)
        panel = open_session(page, h, tomorrow)
        panel.get_by_role('button', name='Change Alex Morgan on this session').click()
        shot(page, 'b2-published-changes--menu', size, theme)
        page.get_by_role('menuitem', name='Replace…').click()
        expect(panel.get_by_role('button', name='Assign Taylor Noor')).to_be_visible()
        shot(page, 'b1-replace-on-published--pick', size, theme)
        panel.get_by_role('button', name='Assign Taylor Noor').click()
        send = panel.get_by_role('button', name='Send for urgent approval')
        expect(send).to_be_visible()
        shot(page, 'b1-replace-on-published--review', size, theme)
        send.click()
        expect(panel.get_by_role('button', name='Withdraw')).to_be_visible()
        shot(page, 'b1-replace-on-published--sent', size, theme)
        page.locator('.toast').get_by_role('button', name='Dismiss message').click()
        panel.get_by_role('button', name='Close panel').click()
        panel = open_session(page, h, later, person=2)
        panel.get_by_role('button', name='Change Taylor Noor on this session').click()
        page.get_by_role('menuitem', name='Mark absent · Sick').click()
        panel.get_by_role('button', name='Close the session…').click()
        panel.get_by_label('Reason').fill('Clinic closed for an inspection')
        panel.get_by_role('button', name='Close session').click()
        shot(page, 'b2-published-changes--cancel', size, theme)
        panel.get_by_role('button', name='Keep it open').click()
        if size == 'phone':  # the session sheet covers the top bar on a phone
            panel.get_by_role('button', name='Close panel').click()
        page.locator('.pc-chip').click()
        expect(page.locator('.pc-changes li')).to_have_count(2)
        shot(page, 'b2-published-changes--changes', size, theme)
    finally:
        context.close()
