"""A draft stays on its visible window until an action needs the whole rota."""
from contextlib import contextmanager
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from .test_browser import (PAGE_LOAD_MS, browser, holding, live_server, navigate,  # noqa: F401
                           planner_tool, sign_in, wait_held)
from .test_browser_planner import needing_type

pytestmark = pytest.mark.browser
CHIP = '#schedule-view button.pchip[data-assignment]'
TARGETS = 'Workload targets for this rota'


@pytest.fixture
def window_rota(h):
    typ = needing_type(h, 'Window duty')
    w = h.workspace('Window actions', end='2026-10-18')
    w = h.shift(w, typ=typ)['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00',
                people=[], typ=typ)['workspace']
    w = h.shift(w, start='2026-10-12T08:00:00+03:00', end='2026-10-12T12:00:00+03:00', typ=typ)['workspace']
    return h.lifecycle(w, 'validate')['workspace']


def record_reads(page, w):
    reads = []

    def requested(request):
        url = urlsplit(request.url)
        if request.method == 'GET' and url.path in (f"/api/workspaces/{w['id']}", f"/api/workspaces/{w['id']}/parts"):
            reads.append((url.path, parse_qs(url.query)))

    page.on('request', requested)
    return reads


def whole_read(w):
    return lambda url: urlsplit(url).path == f"/api/workspaces/{w['id']}" and urlsplit(url).query == 'view=schedule'


def whole_count(reads, w):
    return sum(path == f"/api/workspaces/{w['id']}" and query == {'view': ['schedule']} for path, query in reads)


def assert_window_reads(reads, w, start, end, compact=1):
    assert whole_count(reads, w) == 0, reads
    assert [(path, query) for path, query in reads if not path.endswith('/parts')] == [
        (f"/api/workspaces/{w['id']}", {'view': ['compact']})] * compact, reads
    parts = [query for path, query in reads if path.endswith('/parts')]
    assert {q['part'][0] for q in parts} == {'shifts', 'validation'}, reads
    assert all(q['start'] == [start] and q['end'] == [end] for q in parts), reads
    assert len({(q['part'][0], q['offset'][0]) for q in parts}) == len(parts), reads


@contextmanager
def opened_window(browser, live_server, w, layout='week', viewport=None):
    context, page = sign_in(browser, live_server, 'coordinator', viewport=viewport)
    try:
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        reads = record_reads(page, w)
        navigate(page, f"schedule/{layout}/2026-10-04?edit={w['id']}")
        expect(page.locator('#schedule-view:not([data-reading])')).to_be_visible(timeout=PAGE_LOAD_MS)
        yield page, reads
        assert not errors, errors
    finally:
        context.close()


@pytest.mark.parametrize(('layout', 'start', 'end'), [
    ('week', '2026-10-04', '2026-10-11'),
    ('day', '2026-10-04', '2026-10-05'),
    ('month', '2026-10-01', '2026-11-01'),
])
def test_a_draft_opens_with_only_compact_detail_and_visible_window_parts(browser, live_server, window_rota, layout, start, end):
    with opened_window(browser, live_server, window_rota, layout) as (page, reads):
        expect(page.get_by_role('button', name='Auto-schedule', exact=True)).to_be_enabled()
        assert_window_reads(reads, window_rota, start, end)


def test_a_phone_still_reads_the_whole_draft_at_once(browser, live_server, window_rota):
    with opened_window(browser, live_server, window_rota, viewport={'width': 390, 'height': 844}) as (page, reads):
        expect(page.locator('#schedule-view .ag')).to_be_visible()
        assert whole_count(reads, window_rota) == 1, reads
        assert not any(path.endswith('/parts') for path, _ in reads), reads


def test_next_week_reads_only_the_new_windows_parts(browser, live_server, window_rota):
    with opened_window(browser, live_server, window_rota) as (page, reads):
        assert_window_reads(reads, window_rota, '2026-10-04', '2026-10-11')
        reads.clear()
        grid = page.locator('#schedule-view').element_handle()
        page.get_by_role('button', name='Next week', exact=True).click()
        page.wait_for_function('g => !g.isConnected', arg=grid)
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
        expect(page.locator(CHIP)).to_have_count(1)
        assert_window_reads(reads, window_rota, '2026-10-11', '2026-10-18')


TOOLS = {
    'rules': 'Rules in this rota', 'available': 'Who’s available', 'targets': TARGETS,
    'resolve': 'Resolve problems one by one', 'cover': 'Prepare cover for recorded absences',
    'series_add': 'Add recurring sessions', 'series_update': 'Update recurring sessions', 'exchange': 'Exchange two assignments',
}


def press_feature(page, feature):
    if feature in TOOLS:
        planner_tool(page, TOOLS[feature])
    elif feature == 'session':
        page.locator(CHIP).first.click()
    elif feature == 'menu':
        page.locator(CHIP).first.click(button='right')
    elif feature == 'menu_affordance':
        chip = page.locator(CHIP).first
        chip.hover()
        chip.locator('.pchip-more').click()
    elif feature == 'selection':
        page.locator(CHIP).first.click(modifiers=['Control'])
    elif feature == 'bulk':
        page.locator(CHIP).first.press('x')
    elif feature == 'add':
        page.get_by_role('button', name='Add session', exact=True).click()
    elif feature == 'context_rules':
        page.locator('#schedule-view .rk-pin-btn').first.click()
    elif feature == 'auto':
        page.get_by_role('button', name='Auto-schedule', exact=True).click()
    elif feature == 'submit':
        page.get_by_role('button', name='Submit for review…', exact=True).click()
    else:
        raise AssertionError(feature)


def assert_feature(page, feature, w):
    if feature == 'session':
        expect(page.locator('.wk-panel')).to_be_visible()
    elif feature in ('menu', 'menu_affordance'):
        expect(page.get_by_role('menu').get_by_role('menuitem', name='Replace…', exact=True)).to_be_visible()
    elif feature == 'selection':
        panel = page.locator('.rk-panel')
        expect(panel.get_by_role('tab', name='Details', exact=True)).to_have_attribute('aria-selected', 'true')
        expect(page.locator('.rk-selected').first).to_be_visible()
        expect(page.locator('.wk-panel')).to_have_count(0)
    elif feature == 'bulk':
        expect(page.locator('input.wk-check:checked')).to_have_count(1)
        expect(page.get_by_role('toolbar', name='Actions for selected sessions').get_by_role('button', name='Close sessions', exact=True)).to_be_visible()
    elif feature == 'add':
        expect(page.get_by_role('region', name='Add session', exact=True)).to_be_visible()
    elif feature == 'context_rules':
        expect(page.locator('.rk-panel')).to_be_visible()
    elif feature in ('rules', 'available', 'series_add', 'series_update', 'exchange'):
        expect(page.get_by_role('dialog', name=TOOLS[feature], exact=True)).to_be_visible()
    elif feature == 'targets':
        expect(page.locator('[data-rota-targets]')).to_contain_text('No workload targets')
    elif feature == 'resolve':
        expect(page.locator('.wk-panel .resolve-bar')).to_contain_text('1 to resolve')
    elif feature == 'cover':
        expect(page.get_by_role('dialog', name='Prepare automatic cover', exact=True).get_by_role('button', name='Calculate impact')).to_be_visible()
    elif feature == 'auto':
        expect(page.locator('#auto-panel').get_by_role('button', name='Run auto-schedule', exact=True)).to_be_visible()
    elif feature == 'submit':
        expect(page.get_by_role('dialog', name=f"Submit {w['name']} for review", exact=True)).to_be_visible()
    else:
        raise AssertionError(feature)


@pytest.mark.parametrize('feature', ['session', 'menu', 'menu_affordance', 'selection', 'bulk', 'add', 'context_rules', 'rules', 'available',
                                    'targets', 'resolve', 'cover', 'auto', 'submit', 'series_add', 'series_update', 'exchange'])
def test_each_whole_rota_feature_waits_for_one_read_then_works(browser, live_server, h, window_rota, feature):
    w = window_rota
    if feature == 'cover':
        h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0],
                  'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00',
                  'kind': 'actual_absence', 'private_reason': 'Synthetic absence'})
        w = h.lifecycle(h.detail(w)['workspace'], 'validate')['workspace']
    with opened_window(browser, live_server, w) as (page, reads), holding(page) as held:
        assert whole_count(reads, w) == 0, reads
        pattern = whole_read(w)
        held.hold(pattern)
        press_feature(page, feature)
        wait_held(page, held[pattern])
        assert whole_count(reads, w) == 1, reads
        assert len(held[pattern]) == 1
        expect(page.locator('.wk-panel, .rk-panel, .dock-panel, dialog[open], #auto-panel:visible')).to_have_count(0)
        expect(page.locator('input.wk-check:checked')).to_have_count(0)
        held.release(pattern)
        assert_feature(page, feature, w)
        page.keyboard.press('Escape')
        page.get_by_role('button', name='Add session', exact=True).click()
        expect(page.get_by_role('region', name='Add session', exact=True)).to_be_visible()
        assert whole_count(reads, w) == 1, reads


def test_overlaps_read_whole_on_first_use(browser, live_server, h):
    from .test_browser_schedule import october, overlapping

    _, rota = october(h)
    overlapping(h)
    with opened_window(browser, live_server, rota) as (page, reads), holding(page) as held:
        pattern = whole_read(rota)
        held.hold(pattern)
        page.locator('footer').get_by_role('button', name='1 other draft for these days').click()
        wait_held(page, held[pattern])
        assert whole_count(reads, rota) == 1, reads
        dialog = page.get_by_role('dialog', name='Other drafts cover these days')
        expect(dialog).to_have_count(0)
        held.release(pattern)
        expect(dialog).to_be_visible()
        assert whole_count(reads, rota) == 1, reads


def test_quick_repeat_presses_read_once_and_replay_once(browser, live_server, window_rota):
    with opened_window(browser, live_server, window_rota) as (page, reads), holding(page) as held:
        pattern = whole_read(window_rota)
        held.hold(pattern)
        page.locator(CHIP).first.evaluate("""chip => {
          window.windowReplayCount=0;
          document.addEventListener('click', e => {
            const n=e.target.closest('button.pchip[data-assignment]');
            if(n && n!==chip)window.windowReplayCount++;
          },true);
          for(let i=0;i<4;i++)chip.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));
        }""")
        wait_held(page, held[pattern])
        assert len(held[pattern]) == 1
        expect(page.locator('.wk-panel')).to_have_count(0)
        held.release(pattern)
        expect(page.locator('.wk-panel')).to_have_count(1)
        expect(page.locator('.wk-panel')).to_be_visible()
        assert page.evaluate('window.windowReplayCount') == 1
        assert whole_count(reads, window_rota) == 1, reads


def test_a_session_deleted_during_the_whole_read_is_said_and_not_replayed(browser, live_server, h, window_rota):
    with opened_window(browser, live_server, window_rota) as (page, reads), holding(page) as held:
        chip = page.locator(CHIP).first
        assignment, sid = chip.get_attribute('data-assignment'), chip.get_attribute('data-shift')
        pattern = whole_read(window_rota)
        held.hold(pattern)
        chip.click()
        wait_held(page, held[pattern])
        # The authoritative reply arrives after another editor removed the session.
        reply = h.detail(window_rota)
        reply['shifts'] = [s for s in reply['shifts'] if s['id'] != sid]
        reply['workspace']['revision'] += 1
        held[pattern].pop().fulfill(json=reply)
        page.unroute(pattern)
        expect(page.locator('.toast.info')).to_contain_text('This duty is no longer in the rota.')
        expect(page.locator(f'{CHIP}[data-assignment="{assignment}"]')).to_have_count(0)
        expect(page.locator('.wk-panel')).to_have_count(0)
        assert whole_count(reads, window_rota) == 1, reads


def test_focus_keys_leave_the_rota_on_its_window(browser, live_server, window_rota):
    with opened_window(browser, live_server, window_rota) as (page, reads):
        for key in ('ArrowRight', 'ArrowLeft', 'ArrowDown', 'ArrowUp', 'Home', 'End', 'Control+Home', 'Control+End', 'Tab', 'Shift+Tab'):
            page.locator(CHIP).first.focus()
            page.keyboard.press(key)
        expect(page.locator('#schedule-view:not([data-reading])')).to_be_visible()
        assert whole_count(reads, window_rota) == 0, reads


def test_rule_hover_stays_on_the_window_and_keyboard_open_replays_once(browser, live_server, window_rota):
    with opened_window(browser, live_server, window_rota) as (page, reads):
        day = page.locator('.wk-day[data-rk-pin="1"]').first
        day.hover()
        expect(page.locator('.planner-main')).to_have_attribute('data-rk-rules', re.compile(r'^\d+$'))
        assert whole_count(reads, window_rota) == 0, reads
        day.press('Enter')
        expect(page.locator('.rk-panel')).to_be_visible()
        assert whole_count(reads, window_rota) == 1, reads


@pytest.mark.parametrize('key', ['x', 'ContextMenu', 'Shift+F10'])
def test_selection_and_menu_keys_wait_for_the_whole_rota_then_act(browser, live_server, window_rota, key):
    with opened_window(browser, live_server, window_rota) as (page, reads), holding(page) as held:
        pattern = whole_read(window_rota)
        held.hold(pattern)
        page.locator(CHIP).first.press(key)
        wait_held(page, held[pattern])
        expect(page.locator('input.wk-check:checked')).to_have_count(0)
        expect(page.get_by_role('menu')).to_have_count(0)
        held.release(pattern)
        assert_feature(page, 'bulk' if key == 'x' else 'menu', window_rota)
        assert whole_count(reads, window_rota) == 1, reads


def test_a_failed_whole_read_says_so_and_leaves_the_week_usable(browser, live_server, window_rota):
    with opened_window(browser, live_server, window_rota) as (page, reads):
        pattern = whole_read(window_rota)
        page.route(pattern, lambda route: route.fulfill(status=502, json={
            'error': {'code': 'READ_FAILED', 'message': 'Synthetic whole read failure'}}))
        page.locator(CHIP).first.click()
        expect(page.locator('.toast.error', has_text='Couldn’t load the whole rota.')).to_be_visible()
        expect(page.locator('#schedule-view')).not_to_have_attribute('aria-busy', 'true')
        expect(page.locator(CHIP).first).to_be_enabled()
        expect(page.locator('.wk-panel')).to_have_count(0)
        assert whole_count(reads, window_rota) == 1, reads
        reads.clear()
        grid = page.locator('#schedule-view').element_handle()
        page.get_by_role('button', name='Next week', exact=True).click()
        page.wait_for_function('g => !g.isConnected', arg=grid)
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
        assert_window_reads(reads, window_rota, '2026-10-11', '2026-10-18')
        page.unroute(pattern)
        page.locator(CHIP).first.click()
        expect(page.locator('.wk-panel')).to_be_visible()
        assert whole_count(reads, window_rota) == 1, reads


@pytest.mark.parametrize('open_card', ['hover', 'focus'])
def test_name_cards_omit_rota_totals_on_a_window_and_show_them_once_whole(browser, live_server, window_rota, open_card):
    with opened_window(browser, live_server, window_rota) as (page, reads):
        chip = page.locator(CHIP).first

        def show_card():
            if open_card == 'focus':
                page.keyboard.press('Tab')
                chip.focus()
                expect(chip).to_be_focused()
            else:
                page.locator('.planner-toolbar').hover()
                chip.hover()

        show_card()
        card = page.locator('.hovercard')
        expect(card).to_be_visible()
        expect(card).not_to_contain_text(re.compile(r'\d+ h this (week|rota)|No earlier duty|Rested \d+ h|Off \d+ days before'))
        assert whole_count(reads, window_rota) == 0, reads
        chip.click()
        panel = page.locator('.wk-panel')
        expect(panel).to_be_visible()
        panel.get_by_role('button', name='Close panel', exact=True).click()
        expect(panel).to_have_count(0)
        show_card()
        expect(card).to_contain_text('4 h this week · 8 h this rota')
        expect(card).to_contain_text('No earlier duty')
        assert whole_count(reads, window_rota) == 1, reads


def test_a_stale_window_checks_itself_without_reading_the_whole_rota(browser, live_server, h, window_rota):
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'metric': 'duties',
              'comparison': 'target', 'amount': 2, 'prorate_department_days': True, 'reference_days': 28}, severity='advisory')
    w = h.refresh_draft_baseline(h.detail(window_rota)['workspace'])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        reads = record_reads(page, w)
        checks = []

        def command_sent(request):
            if request.method == 'POST' and urlsplit(request.url).path == '/api/commands':
                body = request.post_data_json
                if body['kind'] == 'workspace.validate' or body['kind'] == 'job.create' and body['payload'].get('kind') == 'validation':
                    checks.append(body)

        page.on('request', command_sent)
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        wait_held(page, checks)
        assert len(checks) == 1, checks
        assert checks[0]['payload']['automatic'] is True
        assert checks[0]['expected_revision'] == w['revision']
        expect(page.locator('.health-pill.checking')).to_have_count(0, timeout=PAGE_LOAD_MS)
        expect(page.locator('#schedule-view:not([data-reading])')).to_be_visible()
        assert whole_count(reads, w) == 0, reads
    finally:
        context.close()


def test_undo_reads_the_window_again_without_a_whole_read(browser, live_server, h, window_rota):
    with opened_window(browser, live_server, window_rota) as (page, reads):
        before = page.locator(CHIP).count()
        grid = page.locator('#schedule-view').element_handle()
        reads.clear()
        page.get_by_role('button', name='Undo last edit', exact=True).click()
        expect(page.locator('.toast', has_text='Undone.')).to_be_visible()
        # The toast offers Redo too: the planner's own control is the one asserted.
        expect(page.locator('.planner-bar').get_by_role('button', name='Redo', exact=True)).to_be_visible()
        page.wait_for_function('g => !g.isConnected', arg=grid)
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
        expect(page.locator(CHIP)).to_have_count(before)
        assert len(h.detail(window_rota)['shifts']) == 2
        # Undo uses the revision on screen, then reads the changed window's compact detail.
        assert_window_reads(reads, window_rota, '2026-10-04', '2026-10-11')


def test_a_submitted_rota_reads_whole_at_once(browser, live_server, h):
    w = h.lifecycle(h.shift(h.workspace('Review window'))['workspace'], 'submit')['workspace']
    context, page = sign_in(browser, live_server, 'approver')
    try:
        reads = record_reads(page, w)
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        expect(page.get_by_role('button', name='Approve rota', exact=True)).to_be_visible()
        assert whole_count(reads, w) == 1, reads
    finally:
        context.close()


def test_publish_reuses_the_approved_rota_read_at_open(browser, live_server, h):
    w = h.lifecycle(h.shift(h.workspace('Publish window'))['workspace'], 'submit')['workspace']
    w = h.lifecycle(w, 'approve', 'approver')['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        reads = record_reads(page, w)
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        assert whole_count(reads, w) == 1, reads
        page.get_by_role('button', name='Publish…', exact=True).click()
        expect(page.get_by_role('dialog', name=f"Publish {w['name']}", exact=True)).to_be_visible()
        assert whole_count(reads, w) == 1, reads
    finally:
        context.close()
