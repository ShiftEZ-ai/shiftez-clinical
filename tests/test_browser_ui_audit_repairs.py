"""UI audit repairs (audit 2026-10-29, current-source mapping on 16d1e6d6): one behavioural check per repaired item,
through the real UI and API. Item ids: B2-UI-01, F7-UI-01, CC-UI-01, CC-UI-02, AUD3-UI-01, D4-UI-02, A5-UI-01,
B4-F01, B4-F02, C4-F01, C4-F04, C4-F05, C4-F06, LG-F04. P3-UI-01 and PCF-S04 extend tests/test_browser_home.py and
tests/test_browser_w3_screens_a.py when their data set-up is added (not authored here)."""
import re

import pytest
from playwright.sync_api import expect

from .governance_support import developer
from .test_browser import browser, live_server, sign_in, navigate, pick_view, view_settings, open_config  # noqa: F401
from .test_browser_feedback import send_as
from .test_browser_rule_catalogue import flag, rule_action
from .test_browser_rule_catalogue_v2 import v2_rule, line

pytestmark = pytest.mark.browser
DESKTOP = {'width': 1440, 'height': 900}


def week_rota(h, name, people=None):
    """A draft with three sessions on 4-6 Oct 2026 (people=[] leaves them open)."""
    w = h.shift(h.workspace(name), people=people)['workspace']
    for day in ('05', '06'):
        w = h.shift(w, start=f'2026-10-{day}T08:00:00+03:00', end=f'2026-10-{day}T12:00:00+03:00', people=people)['workspace']
    return w


def test_b2_footer_counts_what_the_search_shows(browser, live_server, h):
    w = week_rota(h, 'Footer search rota')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        total = page.locator('.app-footer .footer-total').first
        expect(total).to_have_text('3 sessions in view')
        search = page.get_by_role('searchbox', name='Search sessions', exact=True)
        search.fill('zz no session matches this')
        expect(total).to_have_text('0 sessions in view')
        search.fill('')
        expect(total).to_have_text('3 sessions in view')
    finally:
        context.close()


@pytest.mark.parametrize('role', ['coordinator', 'approver'])
def test_f7_show_on_schedule_leaves_issues_for_the_issue_draft_and_day(browser, live_server, h, role):
    w = week_rota(h, 'Issue show rota', people=[])
    other = h.workspace('Another draft')
    context, page = sign_in(browser, live_server, role, viewport=DESKTOP)
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={other['id']}")  # another draft chosen first: the issue's must win
        navigate(page, f"schedule/issues?draft={w['id']}&view=checks")
        expect(page.locator('#main[data-loaded-view=schedule] .issues-head')).to_be_visible(timeout=20000)
        page.get_by_role('button', name='Show on schedule').first.click()
        expect(page.locator('#main[data-loaded-view=schedule] .planner-main')).to_be_visible(timeout=20000)
        assert '/issues' not in page.url and f"edit={w['id']}" in page.url, page.url
        expect(page.locator('.issue-spot').first).to_be_visible()
    finally:
        context.close()



@pytest.mark.parametrize('role,change', [('coordinator', 'moved'), ('approver', 'moved'), ('coordinator', 'removed')])
def test_f7_show_on_schedule_follows_a_moved_session_and_says_a_removed_one(browser, live_server, h, role, change):
    """The Issues page lists a session; it then moves to 10 Oct or is removed from the draft before Show on schedule. A moved
    session opens on its current day in the issue's draft and is marked; a removed one is said, nothing is marked.
    Design Lead (f7): (1) said in the session's own words; (2) the view never moves to another day without saying so;
    (3) once settled the link leaves the address by replacement, so Back returns to the Issues page, not the link."""
    w = week_rota(h, 'Moving issue rota', people=[])
    target = next(s for s in h.detail(w)['shifts'] if s['start'].startswith('2026-10-04'))
    context, page = sign_in(browser, live_server, role, viewport=DESKTOP)
    try:
        navigate(page, f"schedule/issues?draft={w['id']}&view=checks")
        expect(page.locator('#main[data-loaded-view=schedule] .issues-head')).to_be_visible(timeout=20000)
        show = page.locator('#main [data-row]').filter(has_text='4 Oct').get_by_role('button', name='Show on schedule').first
        expect(show).to_be_visible()
        revision = h.detail(w)['workspace']['revision']
        if change == 'moved':
            h.command('coordinator', 'shift.update', {**{k: target[k] for k in ('assignment_type_id', 'location_id', 'service_ids', 'attributes')},
                'workspace_id': w['id'], 'shift_id': target['id'], 'start': '2026-10-10T08:00:00+03:00', 'end': '2026-10-10T12:00:00+03:00',
                'participants': []}, revision=revision)
        else:
            h.command('coordinator', 'shift.action', {'workspace_id': w['id'], 'shift_ids': [target['id']], 'action': 'remove',
                'reason': 'Session no longer needed'}, revision=revision)
        before = page.evaluate('history.length')
        show.click()
        expect(page.locator('#main[data-loaded-view=schedule] .planner-main')).to_be_visible(timeout=20000)
        assert '/issues' not in page.url and f"edit={w['id']}" in page.url, page.url
        marked = page.locator('[data-rk*=' + repr(f'"shift":"{target["id"]}"') + ']')  # [data-rk*='"shift":"<id>"']
        if change == 'moved':
            # (1)+(2): the view moved to the session's day, and said so in its words (10 Oct 2026 is a Saturday).
            expect(page.locator('.toast[role=status]').filter(has_text='This session moved to Sat 10 Oct')).to_be_visible()
            expect(marked.first).to_be_visible()
            expect(page.locator('.issue-spot').first).to_be_visible()
            expect(page).to_have_url(re.compile(r'#schedule/week/2026-10-10\b'))
        else:
            expect(page.locator('.toast[role=status]').filter(has_text='This session was removed')).to_be_visible()
            expect(page.locator('.issue-spot')).to_have_count(0)
        # (3): settled, the link is gone from the address, replaced in place (one entry for the open), and Back goes to
        # the Issues page without replaying it.
        expect(page).not_to_have_url(re.compile(r'[?&]shift='))
        assert page.evaluate('history.length') == before + 1
        page.go_back()
        expect(page.locator('#main[data-loaded-view=schedule] .issues-head')).to_be_visible(timeout=20000)
        # A page change clears plain notices (app.ts renderPage); none is said again, as nothing is replayed.
        expect(page.locator('.toast').filter(has_text=re.compile('^This session'))).to_have_count(0)
    finally:
        context.close()

def test_cc_ui_01_history_gives_focus_back_after_escape(browser, live_server, h):
    flag(h, legacy=False)
    v2_rule(h, 'duty_spacing', [line({'min': {'value': 11, 'level': 'mandatory'}})])
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        navigate(page, 'rules/duty_spacing')
        # The rule page's own actions sit in the top bar (liftHeader), as Retire rule does.
        history = page.locator('.topbar').get_by_role('button', name='More actions for this rule', exact=True)
        rule_action(page, 'History')
        dialog = page.get_by_role('dialog', name=re.compile(r'^History · '))
        expect(dialog.locator('.rc-history li').first).to_be_visible()
        page.keyboard.press('Escape')
        expect(dialog).to_have_count(0)
        expect(history).to_be_focused()
    finally:
        context.close()


@pytest.mark.parametrize('width,nav', [(800, 'rail'), (900, 'rail'), (720, 'phone'), (1440, 'wide')])
def test_cc_ui_02_docked_panel_starts_beside_the_navigation(browser, live_server, h, width, nav):
    send_as(h, 'staff', 'Docked panel check report')
    context, page = sign_in(browser, live_server, developer(h), viewport={'width': width, 'height': 900})
    try:
        page.goto(live_server + '/#system/feedback')
        # A table row opens from its message; a narrow list shows cards, which open from their title.
        report = page.locator('#main .dt [data-row], #main .dt-card').filter(has_text='Docked panel check report').first
        report.locator('.fb-row-msg, .dt-card-title').first.click()
        panel = page.locator('.dock-panel')
        expect(panel.locator('.side-head h2')).to_be_visible()
        left = panel.evaluate('p => p.getBoundingClientRect().left')
        sidebar = page.evaluate("() => {const s=document.querySelector('.sidebar');const r=s.getBoundingClientRect();"
                                "return getComputedStyle(s).visibility==='hidden'?0:r.right;}")
        assert left >= sidebar - 1, (left, sidebar)
        page.keyboard.press('Escape')
        expect(panel).to_have_count(0)
    finally:
        context.close()


def test_aud3_feedback_search_survives_the_phone_switch(browser, live_server, h):
    send_as(h, 'staff', 'Feedback search survives report')
    context, page = sign_in(browser, live_server, developer(h), viewport=DESKTOP)
    try:
        page.goto(live_server + '/#system/feedback')
        rows = page.locator('#main .dt [data-row], #main .dt-card')
        expect(rows.filter(has_text='Feedback search survives report')).to_have_count(1)
        page.get_by_role('searchbox', name='Search feedback').fill('zz no report matches this')
        expect(rows.filter(has_text='Feedback search survives report')).to_have_count(0)
        for size in ({'width': 320, 'height': 800}, {'width': 768, 'height': 900}):
            page.set_viewport_size(size)
            search = page.get_by_role('searchbox', name='Search feedback')
            if search.count():  # the phone guard may replace the page; once back, the list and its search agree
                expect(search).to_have_value('zz no report matches this')
                expect(rows.filter(has_text='Feedback search survives report')).to_have_count(0)
    finally:
        context.close()


def test_d4_form_footer_keeps_every_action_inside_the_dialog_at_320(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'staff', viewport={'width': 320, 'height': 800})
    try:
        navigate(page, 'requests?new=1')
        dialog = page.get_by_role('dialog').first
        expect(dialog.locator('.form-footer')).to_be_visible()
        outside = dialog.evaluate("""d => {const r=d.getBoundingClientRect();
          return [...d.querySelectorAll('.form-footer > .button')].filter(b=>{const x=b.getBoundingClientRect();
            return x.left<r.left-1||x.right>r.right+1;}).map(b=>b.textContent);}""")
        assert outside == [], outside
    finally:
        context.close()


def test_a5_inverted_rota_dates_are_refused_on_last_day(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        navigate(page, 'schedule')
        page.get_by_role('button', name='New rota', exact=True).first.click()
        dialog = page.get_by_role('dialog', name=re.compile('rota', re.I)).first
        first, last = dialog.get_by_label('First day', exact=True), dialog.get_by_label('Last day', exact=True)
        # The name is typed: it is suggested only from the dates' overlap answer, which inverted dates no longer draw
        # (1ea63032b), and an empty required name would be the first problem, taking the focus before Last day.
        dialog.get_by_label('Rota name', exact=True).fill('A5 inverted dates')
        first.fill('2026-11-10')
        last.fill('2026-11-09')
        sent = []
        page.on('request', lambda r: sent.append(r.url) if r.url.endswith('/api/commands') and r.method == 'POST' else None)
        dialog.locator('button[type=submit]').click()
        expect(last).to_be_focused()
        assert not last.evaluate('i => i.checkValidity()') and sent == []
        last.fill('2026-11-10')  # one day: First day and Last day may be equal
        assert last.evaluate('i => i.checkValidity()')
    finally:
        context.close()


def test_b4_f01_people_name_descending_stays_descending(browser, live_server, h):
    w = week_rota(h, 'People order rota', people=h.seed['people'][:2])
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        pick_view(page, 'People')
        bar = page.locator('.planner-toolbar')
        bar.get_by_role('button', name=re.compile('^Sort')).click()
        page.get_by_role('menuitemradio', name='Name').click()
        bar.get_by_role('button', name=re.compile('^Sort')).click()
        page.get_by_role('radiogroup', name='Sort direction').get_by_role('radio', name='Descending').click()
        bar.get_by_role('button', name=re.compile('^Sort')).click()
        expect(page.get_by_role('radiogroup', name='Sort direction').get_by_role('radio', name='Descending')).to_have_attribute('aria-checked', 'true')
        page.keyboard.press('Escape')
        names = page.locator('.people-grid .person-group .pg-name strong').all_inner_texts()
        assert names == sorted(names, reverse=True), names
    finally:
        context.close()


def test_b4_f02_schedule_search_survives_the_phone_switch(browser, live_server, h):
    w = week_rota(h, 'Search width rota')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        page.get_by_role('searchbox', name='Search sessions', exact=True).fill('zz no session matches this')
        expect(page.locator('.app-footer .footer-total').first).to_have_text('0 sessions in view')
        page.set_viewport_size({'width': 390, 'height': 844})
        page.set_viewport_size(DESKTOP)
        expect(page.get_by_role('searchbox', name='Search sessions', exact=True)).to_have_value('zz no session matches this')
        expect(page.locator('.app-footer .footer-total').first).to_have_text('0 sessions in view')
    finally:
        context.close()


def test_c4_f01_read_only_week_offers_no_dead_group_rules_button(browser, live_server, h):
    from .test_browser_requests import published
    published(h)
    context, page = sign_in(browser, live_server, 'staff', viewport=DESKTOP)
    try:
        navigate(page, 'schedule/week')
        expect(page.locator('#main[data-loaded-view=schedule]')).to_be_visible()
        expect(page.locator('.wk-group-head .rk-pin-btn')).to_have_count(0)
    finally:
        context.close()


def test_c4_f04_phone_year_summary_never_covers_december(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'staff', viewport={'width': 320, 'height': 800})
    try:
        navigate(page, 'schedule/year/2026-12-31')
        expect(page.locator('.sd-cal.is-year')).to_be_visible()
        page.evaluate('scrollTo(0, document.documentElement.scrollHeight)')
        covered = page.evaluate("""() => {const p=document.querySelector('.sd-cal.is-year>.side-panel');
          const cells=[...document.querySelectorAll('.sd-year [data-day]')].slice(-7);
          if(!p) return []; const r=p.getBoundingClientRect();
          return getComputedStyle(p).position==='fixed'?cells.filter(c=>{const x=c.getBoundingClientRect();return x.bottom>r.top&&x.top<r.bottom;}).map(c=>c.dataset.day):[];}""")
        assert covered == [], covered
    finally:
        context.close()


def test_c4_f05_a_hidden_list_column_comes_back_from_view_settings(browser, live_server, h):
    w = week_rota(h, 'List columns rota')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        pick_view(page, 'List')
        header = page.locator('#main thead th').filter(has_text='Duty').first
        header.click(button='right')  # the column menu (table_menu.ts): right-click or Enter on the header
        page.get_by_role('menuitem', name='Hide column').click()
        expect(page.locator('#main thead th').filter(has_text='Duty')).to_have_count(0)
        panel = view_settings(page.locator('.planner-toolbar'), 'Columns')
        panel.get_by_role('checkbox', name='Duty').check()
        expect(page.locator('#main thead th').filter(has_text='Duty')).to_have_count(1)
    finally:
        context.close()


def test_c4_f06_people_group_toggle_keeps_the_focus(browser, live_server, h):
    w = week_rota(h, 'People group rota', people=h.seed['people'][:2])
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        pick_view(page, 'People')
        toggle = page.locator('.people-grid .wk-toggle').first
        name = toggle.locator('strong').inner_text()  # the group's name (its count line changes as it closes)
        toggle.focus()
        page.keyboard.press('Enter')
        again = page.locator('.people-grid .wk-toggle').filter(has_text=name).first
        expect(again).to_have_attribute('aria-expanded', 'false')
        expect(again).to_be_focused()
        page.keyboard.press('Space')
        expect(again).to_have_attribute('aria-expanded', 'true')
        expect(page.locator('.people-grid .wk-toggle').filter(has_text=name).first).to_be_focused()
    finally:
        context.close()


def test_lg_f04_retiring_a_rota_route_says_retire_then_activate(browser, live_server, h):
    h.command('admin', 'review_route.create', {'name': 'Order route', 'purpose': 'rota', 'starts_on': '2026-10-01',
        'ends_on': '2026-12-31', 'definition': {'schema_version': 1, 'steps': [{'key': 'clinical_review',
        'label': 'Independent clinical review', 'role': 'approver'}], 'urgent_steps': None}, 'reason': 'Order route check'})
    route = next(r for r in h.get('admin', '/api/review-routes')['routes'] if r['name'] == 'Order route')
    h.command('approver', 'review_route.activate', {'reason': 'Order route check'}, route['id'], route['revision'])
    context, page = sign_in(browser, live_server, 'approver', viewport=DESKTOP)
    try:
        navigate(page, 'access/routes')
        page.get_by_role('button', name='Retire route', exact=True).first.click()
        dialog = page.get_by_role('dialog', name='Retire review route')
        expect(dialog).to_contain_text('Then activate its replacement: until one is active, rotas in its dates cannot be submitted for review.')
        expect(dialog).not_to_contain_text('Activate its replacement first')
    finally:
        context.close()


def opening_hours_form(page, name):
    open_config(page, 'Opening hours')
    page.get_by_role('button', name='Set opening hours', exact=True).click()
    dialog = page.get_by_role('dialog', name='New opening hours', exact=True)
    dialog.get_by_label('Name', exact=True).fill(name)
    dialog.get_by_role('button', name='Add Sunday window', exact=True).click()
    return dialog


def test_ca_f02_reversed_opening_hours_are_refused_on_the_end_field(browser, live_server, h):
    """The server's rule (an opening window ends after it starts, within one day) is said on Sunday opening end, which
    takes the focus; nothing is sent until it is fixed, then the hours save."""
    name = 'Fictional reversed Sunday hours'
    context, page = sign_in(browser, live_server, 'admin')
    sent = []
    page.on('request', lambda r: sent.append(r.post_data_json) if r.url.endswith('/api/commands') and r.method == 'POST' else None)
    try:
        dialog = opening_hours_form(page, name)
        start, end = dialog.get_by_label('Sunday opening start', exact=True), dialog.get_by_label('Sunday opening end', exact=True)
        start.fill('08:00')
        end.fill('07:00')
        dialog.get_by_role('button', name='Set opening hours', exact=True).click()
        expect(end).to_be_focused()
        assert 'Must be after the opening start' in end.evaluate('i => i.validationMessage')
        assert sent == [] and dialog.get_by_label('Name', exact=True).input_value() == name
        start.fill('06:00')  # changing the start checks the end again
        assert end.evaluate('i => i.checkValidity()')
        start.fill('24:00')
        assert not end.evaluate('i => i.checkValidity()')
        start.fill('08:00')
        end.fill('16:00')
        dialog.get_by_role('button', name='Set opening hours', exact=True).click()
        expect(dialog).to_have_count(0)
        assert [r for r in h.get('admin', '/api/catalogues')['operating_calendars'] if r['name'] == name]
    finally:
        context.close()


def test_ca_f02_a_refused_save_says_its_own_sentence_and_keeps_the_focus(browser, live_server, h):
    """A server refusal of our own rule shows its sentence alone (no field path, no library prefix, no highlighted-fields
    promise) and the focus stays on Set opening hours; the values stay, and the next Save goes through."""
    name = 'Fictional refused opening hours'
    sentence = 'Opening windows must be ordered within one day; split overnight windows at midnight'
    context, page = sign_in(browser, live_server, 'admin')
    refused = []

    def refuse_once(route):
        if refused or (route.request.post_data_json or {}).get('kind') != 'catalogue.create':
            return route.continue_()
        refused.append(1)
        route.fulfill(status=422, json={'error': {'code': 'SCHEMA_INVALID', 'message': 'Review the highlighted fields', 'details': [
            {'field': 'definition.week.0.windows.0', 'code': 'value_error', 'message': 'Value error, ' + sentence}]}})
    try:
        dialog = opening_hours_form(page, name)
        page.route('**/api/commands', refuse_once)
        save = dialog.get_by_role('button', name='Set opening hours', exact=True)
        save.click()
        alert = dialog.get_by_role('alert').first
        expect(alert).to_have_text(sentence)
        for raw in ('definition.', 'Value error', 'highlighted'):
            expect(alert).not_to_contain_text(raw)
        expect(save).to_be_focused()
        assert dialog.get_by_label('Name', exact=True).input_value() == name
        save.click()
        expect(dialog).to_have_count(0)
        assert refused == [1]
    finally:
        context.close()
