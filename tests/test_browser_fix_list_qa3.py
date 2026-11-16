"""Fix list: demo-blocking findings, one regression test each .

1. Assign is single flight: while its save is on its way the fix box says Saving… (even when the panel is drawn
   again) and a second press sends nothing.
2. A refused Assign (409) says so, assigns nothing and offers to read the rota again. (The "lost assignment" was not a loss: the
   server held 1 person before the Assign and after its Undo; the outcome is checked below.)
3. The gate line keeps a readable width in the narrow panel (it was squeezed one word per line).
4. The footer and the panel say the same must-fix count (one source: the Fix list held for the rota).
5. The fix box never lists the people who cannot work the session, at department scale (136).
6. A coordinator's phone Home reaches a draft's Fix list while its check is out of date (the server's row, Needs a check)."""
import json
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, open_rota, route_fetch  # noqa: F401
from .test_browser_fix_list import serve
from .test_browser import fix_pill
from .test_browser_fix_list_panel import DESKTOP, PHONE, real_gaps, two_gaps, with_gaps

pytestmark = pytest.mark.browser
COMMANDS = re.compile(r'.*/api/commands$')


def open_panel(page):
    fix_pill(page, 'err').click()
    page.locator('.popover.fix-pop').get_by_role('button', name=re.compile(r'^Open all in Fix list \(\d+\)$')).click()
    panel = page.locator('.dock-panel.fix-side')
    expect(panel).to_be_visible()
    return panel


def focused_gap(page, gap):
    """The cause's row with its 8 Oct session in focus, and that session's fix box."""
    row = page.locator(f'.dock-panel.fix-side li[data-cause="{gap["id"]}"]')
    chip = row.locator('.fix-chip', has_text=re.compile(r'^\w{3} 8$'))
    chip.click()
    expect(chip).to_have_attribute('aria-pressed', 'true')
    box = row.locator('.fix-fill-box')
    expect(box.get_by_role('button', name=re.compile(r'^Assign ')).first).to_be_visible()
    return row, box


def test_assign_is_single_flight_while_its_save_is_on_its_way(browser, live_server, h):
    from .test_browser_team_box import open_view
    w, gaps, gap = real_gaps(h, 'single flight')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    held, saves = [], []

    def commands(route):
        if (route.request.post_data_json or {}).get('kind') == 'shift.update':
            saves.append(route.request.post_data_json)
            held.append(route)  # answered below, once the second press and a redraw were tried
            return
        route.continue_()
    try:
        open_view(page, w, 'Week')
        open_panel(page)
        row, box = focused_gap(page, gap)
        page.route(COMMANDS, commands)
        box.get_by_role('button', name=re.compile(r'^Assign ')).first.click()
        expect(box).to_contain_text('Saving…')
        # A second press on any card sends nothing.
        for b in box.get_by_role('button', name=re.compile(r'^Assign ')).all():
            b.click(force=True)
        # The panel drawn again mid-save (another chip, then back) offers no Assign for the saving session.
        row.locator('.fix-chip', has_text=re.compile(r'^\w{3} 9$')).click()
        row.locator('.fix-chip', has_text=re.compile(r'^\w{3} 8$')).click()
        expect(row.locator('.fix-fill-box')).to_contain_text('Saving…')
        expect(row.locator('.fix-fill-box').get_by_role('button', name=re.compile(r'^Assign '))).to_have_count(0)
        assert len(saves) == 1, saves
        held[0].continue_()
        expect(row.locator('.fix-chip.done')).to_have_count(1)
        assert len(saves) == 1
        filled = next(s for s in h.detail(w)['shifts'] if s['id'] == gaps[0])
        assert len(filled['participants']) == 2
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_a_refused_assign_says_so_assigns_nothing_and_reads_the_rota_again(browser, live_server, h):
    from .test_browser_team_box import open_view
    w, gaps, gap = real_gaps(h, 'refused')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    reads = []
    page.on('request', lambda r: reads.append(r.url) if r.method == 'GET' and f'/api/workspaces/{w["id"]}' in r.url else None)

    def commands(route):
        if (route.request.post_data_json or {}).get('kind') == 'shift.update':
            return route.fulfill(status=409, json={'error': {'code': 'STALE_REVISION', 'message': 'The rota changed.'}})
        route.continue_()
    try:
        open_view(page, w, 'Week')
        open_panel(page)
        row, box = focused_gap(page, gap)
        page.route(COMMANDS, commands)
        before = len(reads)
        box.get_by_role('button', name=re.compile(r'^Assign ')).first.click()
        expect(page.get_by_text(re.compile('Your change was not saved'))).to_be_visible()
        expect(row.locator('.fix-chip.done')).to_have_count(0)
        expect(row.locator('.fix-fill-box').get_by_role('button', name=re.compile(r'^Assign ')).first).to_be_visible()
        latest = page.locator('#toast-tray .toast.error').get_by_role('button', name='Show the latest', exact=True)
        expect(latest).to_be_visible()
        with page.expect_request(lambda r: r.method == 'GET' and f'/api/workspaces/{w["id"]}' in r.url):
            latest.click()
        assert len(reads) > before, 'the rota is read again after a refused save'
        filled = next(s for s in h.detail(w)['shifts'] if s['id'] == gaps[0])
        assert len(filled['participants']) == 1
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def readable_gate(gate):
    """maintenance / QA cycle 5 maintenance: the gate's words keep a readable width, "N must fix" on one line, nothing cut or spilling."""
    expect(gate.locator('.fix-since')).to_be_visible()
    words = gate.locator('.grow')
    assert words.evaluate('n=>n.getBoundingClientRect().width') >= 150
    # "N must fix" stays on one line (it was one word per line, broken mid-word).
    assert words.locator('strong').first.evaluate('n=>n.getClientRects().length') == 1
    # The words run on at most three lines, and the line never spills out of its box.
    assert words.evaluate('n=>n.getBoundingClientRect().height/(parseFloat(getComputedStyle(n).lineHeight)||parseFloat(getComputedStyle(n).fontSize)*1.5)') <= 3.5
    assert gate.evaluate('n=>n.scrollWidth<=n.clientWidth+1')


@pytest.mark.parametrize('where', ['panel-default', 'panel-360', 'phone-390'])
def test_the_gate_line_keeps_a_readable_width_in_the_panel(browser, live_server, h, where):
    w, shifts = two_gaps(h, 'Fix list gate width')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=PHONE if where == 'phone-390' else DESKTOP)
    try:
        with_gaps(page, w, shifts)  # its summary says "Today: 6 cleared · 2 new" and when it was checked
        if where == 'phone-390':
            navigate(page, f'schedule/issues?draft={w["id"]}')
            gate = page.locator('#main .fix-page .fix-gate')
        else:
            open_rota(page, w['id'])
            panel = open_panel(page)
            if where == 'panel-360':  # the panel at its narrowest (side_panel.ts: Home on the edge handle)
                # One step on the grip shown now (the automatic check may redraw the panel): the width is remembered.
                panel.get_by_role('separator', name='Resize panel').press('Home')
                page.wait_for_function("()=>Math.round(document.querySelector('.dock-panel.fix-side').getBoundingClientRect().width)<=362")
            gate = panel.locator('.fix-gate')
        readable_gate(gate)
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_footer_and_panel_say_the_same_must_fix_count(browser, live_server, h):
    w, shifts = two_gaps(h, 'Fix list one count')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        body = with_gaps(page, w, shifts)

        def stale_list(route):  # the rota list still says the count of an older check
            reply = route_fetch(route)
            if reply.status == 304 or not reply.body():  # a conditional read that found nothing new
                return route.fulfill(response=reply)
            data = reply.json()
            for row in data.get('workspaces', []):
                if row.get('id') == w['id']:
                    row.setdefault('health', {}).setdefault('summary', {})['fix_list'] = {**body, 'summary': {**body['summary'], 'must': 1}}
            route.fulfill(response=reply, body=json.dumps(data))
        page.route(re.compile(r'.*/api/workspaces\?.*'), stale_list)
        page.reload()
        open_rota(page, w['id'])
        panel = open_panel(page)
        must = body['summary']['must']
        expect(panel.locator('section[data-level=must] .fix-level .badge').first).to_have_text(str(must))
        expect(fix_pill(page, 'err')).to_contain_text(str(must))
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_the_fix_box_never_lists_the_people_who_cannot_work_it(browser, live_server, h):
    w, shifts = two_gaps(h, 'Fix list no blocked')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    person = lambda i, status, **x: {'person_id': f'p{i}', 'display_name': f'Fictional Person {i}', 'status': status,
                                     'findings': [] if status == 'ok' else [{'kind': 'NOT ELIGIBLE', 'message': 'Not eligible for this clinic'}],
                                     'reason_short': '12 hours this week', 'week_hours': 12, 'rota_hours': 12,
                                     'rest_before_hours': 40, 'can_lead': False, 'fills': True, **x}
    options = {'candidates': [person(1, 'ok'), person(2, 'ok')] + [person(i, 'blocked') for i in range(3, 139)],
               'slot': {'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00', 'participation': 'staff',
                        'overtime': False, 'segments': []},
               'source_fingerprint': 'f', 'workspace_revision': w['revision']}
    try:
        with_gaps(page, w, shifts)
        page.route('**/api/planner/fill-options', lambda r: r.fulfill(json=options))
        open_rota(page, w['id'])
        panel = open_panel(page)
        panel.locator('li[data-cause=c1] .fix-chip').first.click()  # candidates only when asked for
        box = panel.locator('li[data-cause=c1] .fix-fill-box')
        expect(box.locator('.wk-cand')).to_have_count(2)
        expect(box.locator('.wk-blocked')).to_have_count(0)
        expect(box).not_to_contain_text('Not available')
        assert box.evaluate('n=>n.getBoundingClientRect().height') < 500
        expect(panel.locator('section[data-level=must] li[data-cause=c2]')).to_be_visible()
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_phone_home_reaches_the_drafts_fix_list_without_a_current_check(browser, live_server, h):
    """QA cycle 3 maintenance, now the server's (GEN-FIX 53c6a2c79): Home keeps a coordinator's draft row while its check is out
    of date, with the last check's Fix list summary. The phone draws that row, says Needs a check before those counts,
    and its Fix list opens the page. (The client's own draft list is gone, and with it RESP-REVIEW's R1.)"""
    two = h.configure('assignment_types', {'name': 'Fictional two-place Home clinic', 'starts_on': '2020-01-01',
                                           'definition': {'requirements': [{'minimum': 2}]}})['record']['id']
    w = h.shift(h.workspace('Phone stale Home draft'), typ=two, people=[])['workspace']  # nobody on two places: must fix
    w = h.shift(w, start='2026-10-04T13:00:00+03:00', end='2026-10-04T17:00:00+03:00', people=[])['workspace']
    h.policy({'kind': 'min_rest', 'hours': 11})  # A structural edit checked itself; changed rule inputs make that check stale.
    row = next(i for i in h.get('coordinator', '/api/home')['attention'] if i.get('rota_id') == w['id'])
    assert row['key'] == 'blocking' and row['checked'] is False, row
    context, page = sign_in(browser, live_server, 'coordinator', viewport=PHONE)
    reads = []
    page.on('request', lambda r: reads.append(r.url) if '/api/workspaces?' in r.url else None)
    try:
        navigate(page, 'home')
        need = page.locator('#main .home-needs li').filter(has_text='Phone stale Home draft')
        expect(need).to_have_count(1)  # once, however Home loads (no second, client-made row)
        expect(need).to_contain_text('Needs a check')
        expect(need).to_contain_text('Must fix before submitting')
        assert not any('limit=50' in u for u in reads), reads  # Home no longer reads the rota list for drafts
        need.get_by_role('button', name='Fix list', exact=True).click()
        expect(page).to_have_url(re.compile(r'#schedule/issues\?.*draft=' + w['id']))
        expect(page.locator('#main .fix-page')).to_be_visible()
    finally:
        context.close()


def test_assign_shows_its_tick_the_next_session_and_undo_while_the_check_is_in_flight(browser, live_server, h):
    """the edit's reply lands while the rota's automatic check is in flight and the
    draft's re-reads are slow. The ✓ chip, the next session and the Undo message come from the reply at once, not after
    them."""
    from .test_browser_team_box import open_view
    w, gaps, gap = real_gaps(h, 'in flight')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    held, armed = [], []

    def commands(route):
        body = route.request.post_data_json or {}
        if body.get('kind') == 'job.create' and (body.get('payload') or {}).get('kind') == 'validation':
            held.append(route)  # the rota's check stays in flight
            return
        route.continue_()

    def reads(route):
        if armed:
            held.append(route)  # the draft's own re-reads are slow from here on
            return
        route.continue_()
    try:
        open_view(page, w, 'Week')
        open_panel(page)
        row, box = focused_gap(page, gap)
        page.route(COMMANDS, commands)
        page.route(re.compile(r'.*/api/workspaces/' + w['id'] + r'(\?.*)?$'), reads)
        armed.append(True)
        box.get_by_role('button', name=re.compile(r'^Assign ')).first.click()
        expect(row.locator('.fix-chip.done')).to_have_count(1)
        expect(row.locator('.fix-fill-box .fix-fill-day')).to_have_text(re.compile(r' 9 Oct$'))
        expect(page.locator('#toast-tray .toast-action', has_text='Undo')).to_be_visible()
    finally:
        for route in held:
            try:
                route.continue_()
            except Exception:  # noqa: BLE001 - the page may have moved on; nothing to release
                pass
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_a_finished_check_redraws_beside_the_open_fix_list_and_keeps_its_focus(browser, live_server, h):
    """the rota's check used to wait for every side panel to close before it redrew,
    so with the Fix list open the footer kept its old counts. It redraws in place now, and the focus stays on the chip
    it was on in the panel."""
    from apps.worker.main import process_one
    from .test_be18_command_latency import worker_up
    from .test_browser import wait_held
    from .test_browser_team_box import open_view
    w, gaps, gap = real_gaps(h, 'check lands')
    old_fingerprint = h.detail(w)['workspace']['validation']['fingerprint']
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    joined, queued = [], []
    page.on('request', lambda r: joined.append(r) if r.url.endswith('/api/commands') and (r.post_data_json or {}).get('kind') == 'job.create' else None)

    def pending_reply(route):
        command = route.request.post_data_json or {}
        if command.get('kind') == 'job.create' and queued and (command.get('payload') or {}).get('kind') == 'validation':
            return route.fulfill(json={'job': queued[0]})
        if command.get('kind') != 'shift.update':
            return route.continue_()
        reply = route_fetch(route)
        data = reply.json()
        # Fingerprint-bound Assign checks synchronously; hold its complete-check delivery behind a real queued job.
        job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'validation'},
                        revision=data['workspace']['revision'])['job']
        assert job['status'] == 'queued'
        queued.append(job)
        data['validation'] = {**data['validation'], 'scope': 'command_changes', 'fingerprint': old_fingerprint}
        data['workspace']['validation'] = data['validation']
        data['fix_list_delta']['summary']['checking'] = True
        route.fulfill(response=reply, body=json.dumps(data))

    try:
        open_view(page, w, 'Week')
        open_panel(page)
        row, box = focused_gap(page, gap)
        # Deliver provisional evidence while the real queued full check waits for this test's worker pass.
        worker_up(h)
        page.route(COMMANDS, pending_reply)
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and
                                  (r.request.post_data_json or {}).get('kind') == 'shift.update') as saved:
            box.get_by_role('button', name=re.compile(r'^Assign ')).first.click()
        assert saved.value.json()['fix_list_delta']['summary']['checking'] is True
        expect(row.locator('.fix-chip.done')).to_have_count(1)
        # DL D-063, kept by DL-018 (Design Lead) + Master 10 Nov: one wording rule; a queued check says Waiting to check
        expect(page.locator('.app-footer')).to_contain_text('Waiting to check')
        chip = row.locator('.fix-chip', has_text=re.compile(r'^\w{3} 9$'))
        chip.focus()
        label = chip.get_attribute('aria-label')
        # The worker's part: once the page has joined the queued check, run it.
        assert any(j['kind'] == 'validation' and j['status'] == 'queued'
                   for j in h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs'])
        wait_held(page, joined)
        while process_one(h.app.state.engine):
            pass
        expect(page.locator('.app-footer')).not_to_contain_text(re.compile('Waiting to check|Checking the rota'))
        expect(page.locator('.dock-panel.fix-side')).to_be_visible()
        assert page.evaluate("()=>!!document.activeElement.closest('.dock-panel.fix-side')")
        assert page.evaluate("()=>document.activeElement.getAttribute('aria-label')") == label
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_phone_home_reaches_an_unchecked_drafts_fix_list_the_same_way(browser, live_server, h):
    """One rule for every draft row (coordinator decision, 7 Nov 2026): a draft whose check is out of date with nothing
    to fix at the last check (the server's `unchecked` row) shows on a phone like a must-fix draft: its name, Needs a
    check, and Fix list opening the page."""
    w = h.shift(h.workspace('Phone unchecked Home draft'))['workspace']  # one person on a one-place session: nothing must be fixed
    h.shift(w, start='2026-10-04T13:00:00+03:00', end='2026-10-04T17:00:00+03:00')
    h.policy({'kind': 'min_rest', 'hours': 11})  # Preserve the stored clear check while changing the inputs it checked.
    row = next(i for i in h.get('coordinator', '/api/home')['attention'] if i['key'] == 'unchecked' and i['rota_id'] == w['id'])
    context, page = sign_in(browser, live_server, 'coordinator', viewport=PHONE)
    try:
        navigate(page, 'home')
        need = page.locator('#main .home-needs li[data-need=unchecked]')
        expect(need).to_have_count(1)
        expect(need).to_contain_text(row['rota_name'])
        expect(need).to_contain_text('Needs a check')
        expect(need).not_to_contain_text('Must fix before submitting')
        need.get_by_role('button', name='Fix list', exact=True).click()
        expect(page).to_have_url(re.compile(r'#schedule/issues\?.*draft=' + row['rota_id']))
        expect(page.locator('#main .fix-page')).to_be_visible()
    finally:
        context.close()


def test_phone_fix_box_folds_the_warned_people_and_never_cuts_a_name(browser, live_server, h):
    """on the phone Fix list the people who fit are the one choice (one primary
    Assign); who would add a warning folds behind its count (that many rows), opens on demand, and every name is shown
    whole (wrapped, never cut, never a word per line)."""
    w, shifts = two_gaps(h, 'Fix list warned fold')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=PHONE)
    long = 'Fictional Doctor Alexandrina Montgomery-Abernathy Featherstonehaugh'
    person = lambda i, status, name=None: {'person_id': f'p{i}', 'display_name': name or f'Fictional Person {i}', 'status': status,
                                           'findings': [] if status == 'ok' else [{'kind': 'RULE', 'message': 'Over the weekly target'}],
                                           'reason_short': '12 hours this week', 'week_hours': 12, 'rota_hours': 12,
                                           'rest_before_hours': 40, 'can_lead': False, 'fills': True}
    options = {'candidates': [person(1, 'ok', long), person(2, 'ok')] + [person(i, 'warn', 'Fictional Dr. Felix Xu' if i == 3 else None) for i in range(3, 8)],
               'slot': {'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00', 'participation': 'staff',
                        'overtime': False, 'segments': []},
               'source_fingerprint': 'f', 'workspace_revision': w['revision']}
    try:
        with_gaps(page, w, shifts)
        page.route('**/api/planner/fill-options', lambda r: r.fulfill(json=options))
        navigate(page, f'schedule/issues?draft={w["id"]}')
        page.locator('#main .fix-page li[data-cause=c1] .fix-chip').first.click()  # candidates only when asked for
        box = page.locator('#main .fix-page li[data-cause=c1] .fix-fill-box')
        expect(box.locator('.wk-cand.ok')).to_have_count(2)
        expect(box.locator('.button.primary')).to_have_count(1)
        fold = box.locator('details.wk-warned')
        expect(fold.locator('summary')).to_have_text('Would add a warning · 5')
        expect(fold).not_to_have_attribute('open', '')
        expect(box.locator('.wk-cand.warn').first).to_be_hidden()
        name = box.locator('.wk-cand.ok strong', has_text='Featherstonehaugh')
        expect(name).to_have_text(long)
        assert name.evaluate('n=>n.scrollWidth<=n.clientWidth&&getComputedStyle(n).textOverflow!=="ellipsis"')
        fold.locator('summary').click()
        expect(box.locator('.wk-cand.warn')).to_have_count(5)  # F4: the count and the rows are one array
        expect(box.get_by_role('button', name=re.compile(r'with a warning$')).first).to_be_visible()
        # F5: on a phone the Assign stacks under the name: no name wraps a word per line (at most two lines here).
        lines = box.locator('.wk-cand.warn strong').evaluate_all(
            "ns=>ns.map(n=>n.getBoundingClientRect().height/(parseFloat(getComputedStyle(n).lineHeight)||parseFloat(getComputedStyle(n).fontSize)*1.5))")
        assert max(lines) <= 2.2, lines
        assert page.evaluate('document.documentElement.scrollWidth') <= PHONE['width']
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()
