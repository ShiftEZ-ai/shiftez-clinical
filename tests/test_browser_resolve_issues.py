"""Schedule › Resolve issues: one page for every issue of a draft, opened from the auto-schedule's "Resolve these first",
resolved in batches (Replace with a preview, Remove sessions with a same-button confirm) that patch the table in place,
each with one Undo; read-only for reviewers, closed to staff; desktop and phone (dark)."""
import re
import pytest
from playwright.sync_api import expect
from .test_browser import auto_setup, route_fetch, browser, live_server, sign_in, navigate, open_scheduling, wait_held, press_escape, choose, wait_run_created  # noqa: F401
from .test_browser_qa_planner_a2 import settled, cp_server  # noqa: F401

pytestmark = pytest.mark.browser


def conflicted(h):
    """Alex works a morning and an afternoon on 4–6 Oct (three rest breaches) and is on annual leave 5–6 Oct (four leave
    issues): a run must keep this work, so it stops with "Resolve these first"."""
    alex = h.seed['people'][0]
    proposal = h.command('coordinator', 'policy.create', {'name': 'Minimum rest', 'owner': 'Test policy owner',
        'source': 'Constructed fixture, not approved hospital policy', 'starts_on': '2020-01-01',
        'definition': {'rule': {'kind': 'min_rest', 'hours': 12}, 'scope': {}, 'severity': 'mandatory', 'exception_allowed': False}})['record']
    rule = h.command('approver', 'policy.activate', {'reason': 'Independent synthetic review'}, proposal['id'], proposal['revision'])['record']
    w = h.workspace('October issues')
    for day in (4, 5, 6):
        w = h.shift(w, f'2026-10-{day:02}T08:00:00+03:00', f'2026-10-{day:02}T12:00:00+03:00', people=[alex])['workspace']
        w = h.shift(w, f'2026-10-{day:02}T13:00:00+03:00', f'2026-10-{day:02}T17:00:00+03:00', people=[alex])['workspace']
    # Recorded after the draft exists: leave binds an existing draft live (no baseline refresh).
    h.command('coordinator', 'availability.record', {'person_id': alex, 'kind': 'recorded_leave', 'leave_type': 'Annual leave',
        'start': '2026-10-05T00:00:00+03:00', 'end': '2026-10-07T00:00:00+03:00', 'private_reason': 'Family medicine congress'})
    w = h.lifecycle(h.detail(w)['workspace'], 'validate')['workspace']
    return w, rule


def issues_page(page):
    expect(page.locator('#main[data-loaded-view=schedule] .issues-head')).to_be_visible(timeout=20000)
    return page.locator('#main')


def group(main, name):
    return main.locator('tr.dt-group').filter(has_text=name)


def test_resolve_from_auto_schedule_replace_with_preview_undo_and_remove(browser, cp_server, h):
    from apps.worker.main import process_one
    w, rule = conflicted(h)
    context, page = sign_in(browser, cp_server, 'coordinator')
    try:
        navigate(page, 'planner'); settled(page)
        open_scheduling(page)
        auto_setup(page.locator('#auto-panel'))
        page.get_by_label('Keep existing assignments', exact=True).check()  # keep every assignment
        page.get_by_role('button', name='Run auto-schedule', exact=True).click()
        expect(page.locator('#auto-panel').get_by_role('progressbar')).to_be_visible()  # drawn as soon as Run is pressed
        wait_run_created(page)  # the worker can only take the run once job.create has answered
        process_one(h.app.state.engine)
        found = page.locator('#auto-panel').get_by_role('region', name='Resolve these first')
        expect(found).to_be_visible(timeout=15000)
        found.get_by_role('button', name='Open Fix list', exact=True).click()
        # The Fix list first, beside this rota on a desk (openIssues, d413c4189); its All checks view is the table.
        expect(page.get_by_role('region', name='Fix list', exact=True)).to_be_visible()
        assert 'edit=' + w['id'] in page.url
        navigate(page, f'schedule/issues?draft={w["id"]}&view=checks')

        main = issues_page(page)
        expect(main.get_by_role('heading', name='All checks · October', exact=True)).to_be_visible()
        expect(main.locator('.issues-counts')).to_contain_text(re.compile(r'^\d+ open · 0 resolved$'))
        expect(main.get_by_role('list', name='Open issues by reason')).to_have_count(0)  # the groups say the reasons, once
        leave = group(main, 'On leave or away')
        expect(leave).to_be_visible()
        expect(main.locator('tbody tr[data-row]').filter(has_text='Annual').first).to_contain_text('5–6 Oct')
        page.evaluate("document.querySelector('.issues-head').dataset.mark = 'kept'")  # a reload would drop this

        # Select the whole leave group, preview the replacements, apply them as one batch.
        leave.get_by_role('checkbox', name='Select all On leave or away', exact=True).check()
        bar = page.get_by_role('toolbar', name='Bulk actions')
        expect(bar).to_contain_text('4 issues selected')
        bar.get_by_role('button', name='Replace…').click()
        banner = main.locator('.issues-preview')
        expect(banner).to_contain_text(re.compile(r'Replace with best available · \d of 4 have someone free who no rule stops'))
        expect(main.locator('td[data-col=fix]').filter(has_text=re.compile('^→ (Jordan Lee|Taylor Noor)$')).first).to_be_visible()
        banner.get_by_role('button', name=re.compile(r'^Apply \d replacements?$')).click()
        # The run above left its proposal waiting: a draft edit asks first, as every draft edit does then.
        guard = page.get_by_role('dialog', name='Edit the draft now?')
        expect(guard).to_contain_text('checked again before you apply it')
        guard.get_by_role('button', name='Edit anyway', exact=True).click()
        expect(main.locator('.issues-counts')).to_contain_text(re.compile(r'[1-9]\d* resolved'))
        expect(main.locator('td[data-col=fix]').filter(has_text='Replaced by').first).to_be_visible()
        expect(group(main, 'Resolved')).to_be_visible()
        assert page.evaluate("document.querySelector('.issues-head').dataset.mark") == 'kept', 'patched in place, not reloaded'
        assert not any(p['person_id'] == h.seed['people'][0] for s in h.detail(w)['shifts'] if s['start'].startswith('2026-10-05')
                       for p in s['participants'])

        # One Undo puts the whole batch back.
        page.locator('.toast').last.get_by_role('button', name='Undo').click()
        main = issues_page(page)
        expect(main.locator('.issues-counts')).to_contain_text(' · 0 resolved', timeout=15000)

        # Remove a session: it acts at once (Undo in the draft, D42).
        rest = group(main, rule['name'])
        rest.get_by_role('checkbox', name=f'Select all {rule["name"]}', exact=True).check()
        remove = page.get_by_role('toolbar', name='Bulk actions').get_by_role('button', name='Remove sessions')
        before = len(h.detail(w)['shifts'])
        remove.click()
        expect(main.locator('td[data-col=fix]').filter(has_text='Session removed').first).to_be_visible()
        assert len(h.detail(w)['shifts']) < before
    finally:
        context.close()


def test_undo_applies_in_place_with_one_request_keeping_scroll_selection_and_focus(browser, live_server, h):
    """Undo patches the table from its reply as the batch did: no page, session or list reload (one request: the undo
    itself), the main pane keeps its scroll, the selection stays and focus stays on the checkbox it was on. Resolved rows
    cannot be selected: no checkbox, and select-all and group boxes leave them out."""
    w, rule = conflicted(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 600})
    try:
        navigate(page, f'schedule/issues?draft={w["id"]}&view=checks')
        main = issues_page(page)
        counts = main.locator('.issues-counts')
        total = int(re.match(r'(\d+) open', counts.inner_text()).group(1))
        bar = page.get_by_role('toolbar', name='Bulk actions')
        group(main, 'On leave or away').get_by_role('checkbox', name='Select all On leave or away', exact=True).check()
        bar.get_by_role('button', name='Unassign').click()
        expect(counts).not_to_contain_text(' · 0 resolved')
        left = int(re.match(r'(\d+) open', counts.inner_text()).group(1))
        assert left < total

        # Resolved rows have no checkbox; select-all takes only the open ones (the counts agree).
        resolved = group(main, 'Resolved')
        expect(resolved).to_be_visible()
        expect(resolved.get_by_role('checkbox')).to_have_count(0)
        expect(main.locator('tbody tr[data-row]').filter(has=page.locator('td[data-col=fix] .muted')).get_by_role('checkbox')).to_have_count(0)
        main.get_by_role('checkbox', name='Select all issues shown', exact=True).check()
        expect(bar).to_contain_text(f'{left} issues selected')
        bar.get_by_role('button', name='Clear selection').click()

        # One open row selected and focused, the main pane scrolled: Undo keeps all three.
        page.evaluate("document.querySelector('.issues-head').dataset.mark = 'kept'")
        box = group(main, rule['name']).locator('xpath=following-sibling::tr[@data-row][1]').get_by_role('checkbox')
        name = box.get_attribute('aria-label')
        box.check()
        box.focus()
        scroll = main.locator('.dt-body > .table-scroll')
        scroll.evaluate('n => { n.scrollTop = 200; }')
        scrolled = scroll.evaluate('n => n.scrollTop')
        assert scrolled > 0
        sent = []
        # The navigation counts (app.ts BADGE_SOURCES: the Schedule's rota list, the waiting requests) follow the live
        # stream after any rota change, the batch's or the Undo's, whenever its pulse lands: before or after this check
        # on a loaded runner. They are not the Undo reading the page again; the page mark below proves no reload.
        nav_counts = ('/api/workspaces?limit=50&offset=0', '/api/requests/all?status=waiting&limit=1')
        page.on('request', lambda r: sent.append(r.url) if '/api/' in r.url and not re.search(r'/api/(pulse|events)', r.url)
                and not r.url.endswith(nav_counts) else None)
        page.locator('.toast').filter(has_text='issues resolved').get_by_role('button', name='Undo').dispatch_event('click')
        expect(page.locator('.toast').filter(has_text='Undone')).to_be_visible()
        expect(counts).to_contain_text(f'{total} open · 0 resolved')
        expect(group(main, 'Resolved')).to_have_count(0)
        assert len(sent) <= 1, sent
        assert page.evaluate("document.querySelector('.issues-head').dataset.mark") == 'kept', 'patched in place, not reloaded'
        again = main.get_by_role('checkbox', name=name, exact=True)
        expect(again).to_be_checked()
        expect(again).to_be_focused()
        expect(bar).to_contain_text('1 issue selected')
        # Undo removes the batch's new issues and the Resolved group, so the page gets shorter and the browser clamps the
        # scroll to the new end: kept means as far as the page still goes, never back to the top.
        end = scroll.evaluate('n => n.scrollHeight - n.clientHeight')
        assert end > 0 and scroll.evaluate('n => n.scrollTop') == min(scrolled, end)
    finally:
        context.close()


def test_undo_of_a_batch_that_brought_new_issues_patches_the_page_to_what_a_reload_shows(browser, live_server, h):
    """A batch can change issues it did not select: replacing Alex's leave puts Jordan or Taylor over an advisory weekly
    hours rule (a new issue, named after the replacement). Undo takes that issue away again: the patched page (counts,
    reasons, rows and their state) equals a fresh load, with no stray "Resolved" row (seen on dev in rc.10)."""
    w, _ = conflicted(h)
    h.policy({'kind': 'max_hours', 'hours': 2, 'window': 'week'}, severity='advisory')
    w = h.refresh_draft_baseline(h.detail(w)['workspace'])  # the rule is newer than the draft's baseline
    w = h.lifecycle(w, 'validate')['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')

    def view(main):
        rows = main.locator('tbody tr[data-row]')
        return (main.locator('.issues-counts').inner_text(),
                sorted(zip(rows.evaluate_all('rs => rs.map(r => r.dataset.row)'), rows.locator('td[data-col=fix]').all_inner_texts())))
    try:
        navigate(page, f'schedule/issues?draft={w["id"]}&view=checks')
        main = issues_page(page)
        fresh = view(main)
        main.get_by_role('checkbox', name='Select all On leave or away', exact=True).check()
        page.get_by_role('toolbar', name='Bulk actions').get_by_role('button', name='Replace…').click()
        main.locator('.issues-preview').get_by_role('button', name=re.compile(r'^Apply \d replacements?$')).click()
        expect(page.locator('.toast').filter(has_text=re.compile(r'issues? resolved · \d+ new issues?'))).to_be_visible()
        page.get_by_role('button', name='Undo last batch', exact=True).click()
        expect(page.locator('.toast').filter(has_text='Undone')).to_be_visible()
        expect(main.locator('.issues-counts')).to_contain_text(' · 0 resolved')
        patched = view(main)
        page.reload()
        assert patched == view(issues_page(page)) == fresh
    finally:
        context.close()


@pytest.mark.parametrize('viewport', [{'width': 1440, 'height': 900}, {'width': 390, 'height': 844}], ids=['desktop', 'phone'])
def test_the_last_batch_keeps_its_undo_after_the_message_goes(browser, live_server, h, viewport):
    """A batch's message waits while the pointer is on it (WCAG 2.2.1) and then goes; "Undo last batch" stays in the
    header until it is used (or the next batch, or leaving the page)."""
    w, rule = conflicted(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport)
    try:
        phone = viewport['width'] < 720
        if phone:
            page.emulate_media(color_scheme='dark')
        navigate(page, f'schedule/issues?draft={w["id"]}&view=checks')
        main = issues_page(page)
        counts = main.locator('.issues-counts')
        total = int(re.match(r'(\d+) open', counts.inner_text()).group(1))
        undo_batch = page.get_by_role('button', name='Undo last batch', exact=True)
        expect(undo_batch).to_have_count(0)
        main.get_by_role('checkbox', name='Select all On leave or away', exact=True).check()
        bar = page.get_by_role('toolbar', name='Bulk actions')
        if phone:  # secondary actions fold into ⋯ More on a phone
            bar.get_by_role('button', name='More actions').click()
            page.get_by_role('menu', name='More actions').get_by_role('menuitem', name='Unassign').click()
        else:
            bar.get_by_role('button', name='Unassign').click()
        toast = page.locator('.toast').filter(has_text='issues resolved')
        expect(toast).to_be_visible()
        expect(undo_batch).to_be_visible()
        toast.hover()
        page.clock.run_for(20000)  # held while pointed at
        expect(toast).to_be_visible()
        page.mouse.move(1, 1)
        page.clock.run_for(20000)
        expect(toast).to_have_count(0)
        expect(undo_batch).to_be_visible()  # the Undo outlives its message
        undo_batch.click()
        expect(counts).to_contain_text(f'{total} open · 0 resolved')
        expect(undo_batch).to_have_count(0)
    finally:
        context.close()


def test_phone_dark_cards_select_a_group(browser, live_server, h):
    w, _ = conflicted(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 390, 'height': 844})
    try:
        page.emulate_media(color_scheme='dark')
        navigate(page, f'schedule/issues?draft={w["id"]}&view=checks')
        main = issues_page(page)
        expect(main.locator('.dt-card').first).to_be_visible()  # cards on a phone
        main.get_by_role('checkbox', name='Select all On leave or away', exact=True).check()
        bar = page.get_by_role('toolbar', name='Bulk actions')
        expect(bar).to_contain_text('4 issues selected')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), 'no sideways scroll'
        # The bar fits its list without scrolling sideways: secondary actions fold into ⋯ More, still one tap away.
        assert bar.evaluate('b => b.scrollWidth <= b.clientWidth + 1'), 'every action in the bar is visible'
        expect(bar.get_by_role('button', name='Replace…')).to_be_visible()
        expect(bar.get_by_role('button', name='Remove sessions')).to_be_visible()
        bar.get_by_role('button', name='More actions').click()
        expect(page.get_by_role('menu', name='More actions').get_by_role('menuitem', name='Unassign')).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize('width', [1440, 390], ids=['desktop', 'phone'])
def test_reason_groups_and_session_cards_keep_the_reason_in_its_group(browser, live_server, h, width):
    from apps.api.services.issues import issue_key
    from .test_leave_disclosure import setting
    w, rule = conflicted(h)
    w = h.shift(w, '2026-10-07T08:00:00+03:00', '2026-10-07T12:00:00+03:00', people=[])['workspace']
    w = h.lifecycle(w, 'validate')['workspace']
    setting(h, 'details')
    gap = next(f for f in w['validation']['findings'] if f['code'] == 'COVERAGE_GAP' and f['start'].startswith('2026-10-07'))
    shift = next(s for s in h.detail(w)['shifts'] if s['id'] == gap['shift_id'])
    clinic = next(s['name'] for s in h.get('coordinator', '/api/catalogues')['services'] if s['id'] == shift['service_ids'][0])
    explanation = f'{clinic} places are free but not assigned: Needs 1; 3 possible, 0 assigned'
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 844})
    try:
        if width == 390:
            page.emulate_media(color_scheme='dark')
        navigate(page, f'schedule/issues?draft={w["id"]}&view=checks')
        main = issues_page(page)
        expect(main.get_by_role('heading', name='All checks · October', exact=True)).to_be_visible()
        # The reasons are the groups (no second row of reason pills above them, cut off on a phone).
        expect(main.get_by_role('list', name='Open issues by reason')).to_have_count(0)
        expect(main.get_by_role('checkbox', name='Select all Minimum rest', exact=True)).to_be_visible()
        reason = 'Fewer people than the minimum — this session still needs someone'

        if width == 1440:
            main.get_by_role('button', name='View settings', exact=True).click()
            page.get_by_role('dialog', name='View settings', exact=True).get_by_role('radio', name='Cards', exact=True).click()
            page.keyboard.press('Escape')
        card = main.locator(f'.dt-card[data-row="{issue_key(gap)}"]')
        # With the server's cause groups the page opens grouped by cause, whose heading names the reason too: the card
        # does not repeat it.
        more = main.get_by_role('button', name=re.compile(r'^Show \d+ more in .*Fewer people than the minimum'))
        if more.count():
            more.click()
        expect(card.locator('.dt-card-line')).to_contain_text(explanation)
        expect(card).not_to_contain_text(reason)
        # Grouped by Reason, each card leaves its reason to the group heading as well.
        choose(main, 'Group', 'Group by', 'Reason')
        more = main.get_by_role('button', name=re.compile(r'^Show \d+ more in Fewer people than the minimum'))
        if more.count():
            more.click()
        expect(card.locator('.dt-card-title')).to_have_text(f'{shift["type_snapshot"]["name"]} · {clinic} · Wed 7 Oct 2026 · 08:00–12:00')
        expect(card).not_to_contain_text(reason)
        expect(card.locator('.dt-card-line')).to_contain_text(explanation)
        rest = main.locator('.dt-card').filter(has_text='1 h rest (needs 12 h)').first
        expect(rest.locator('.dt-card-line')).to_contain_text('Alex Morgan · Minimum rest not met · 1 h rest (needs 12 h)')
        assert rest.locator('.dt-card-line').inner_text().count('Minimum rest not met') == 1
        leave = main.locator('.dt-card').filter(has_text='Annual').first
        expect(leave.locator('.dt-card-line')).to_contain_text('Alex Morgan Annual · 5–6 Oct · Family medicine congress')

        # Changing the saved collection grouping must retain the explanation on each card.
        main.get_by_role('button', name='Group: Reason', exact=True).click()
        page.get_by_role('dialog', name='Group', exact=True).get_by_role('button', name='Remove grouping', exact=True).click()
        page.keyboard.press('Escape')
        expect(card.locator('.dt-card-title')).not_to_contain_text(reason)
        expect(card.locator('.dt-card-line')).to_contain_text(reason)
        expect(card.locator('.dt-card-line')).to_contain_text(explanation)
        expect(rest.locator('.dt-card-line')).to_contain_text('Alex Morgan · Minimum rest · Minimum rest not met · 1 h rest (needs 12 h)')

        main.get_by_role('button', name='Group', exact=True).click()
        page.get_by_role('menu', name='Group by', exact=True).get_by_role('menuitemradio', name='Person', exact=True).click()
        editor = page.get_by_role('dialog', name='Group', exact=True)
        editor.get_by_role('button', name='+ Add subgroup', exact=True).click()
        editor.get_by_label('Then by', exact=True).select_option('reason')
        page.keyboard.press('Escape')
        assert rest.locator('.dt-card-line').inner_text().count('Minimum rest not met') == 1
        main.get_by_role('button', name=re.compile(r'^View settings')).click()
        page.get_by_role('dialog', name='View settings', exact=True).get_by_role('radio', name='Board', exact=True).click()
        page.keyboard.press('Escape')
        # Boards only draw the first grouping: the cards must carry the missing second-level reason.
        expect(main.locator('.dt-lane').filter(has_text='Alex Morgan').locator('header').first).to_contain_text('Alex Morgan')
        expect(card.locator('.dt-card-line')).to_contain_text(reason)
        expect(card.locator('.dt-card-line')).to_contain_text(explanation)
        expect(rest.locator('.dt-card-line')).to_contain_text('Alex Morgan · Minimum rest · Minimum rest not met · 1 h rest (needs 12 h)')
    finally:
        context.close()


@pytest.mark.parametrize('width', [1440, 390], ids=['desktop', 'phone'])
@pytest.mark.parametrize('catalogue', [False, True], ids=['legacy-rule', 'catalogue-rule'])
def test_missing_rule_name_uses_the_rule_type_label_without_ids(browser, live_server, h, width, catalogue):
    w, rule = conflicted(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 844})
    try:
        def omit_name(route):
            response = route_fetch(route)
            data = response.json()
            if 'policies' not in data:  # the lean boot read: the rules arrive with the deferred lists
                return route.fulfill(response=response)
            policy = next(p for p in data['policies'] if p['id'] == rule['id'])
            policy.pop('name')
            if catalogue:
                policy['definition']['rule'] = {'kind': 'catalogue', 'id': 'rest_between_duties'}
            route.fulfill(response=response, json=data)
        page.route(re.compile(r'/api/catalogues(\?|$)'), omit_name)
        page.reload(wait_until='domcontentloaded')
        navigate(page, f'schedule/issues?draft={w["id"]}&view=checks')
        main = issues_page(page)
        label = 'Rest between duties' if catalogue else 'Minimum rest'
        expect(main.get_by_role('checkbox', name='Select all ' + label, exact=True)).to_be_visible()
        expect(main).not_to_contain_text(rule['id'])
        if width == 1440:
            expect(main.locator('td[data-col=rule]').filter(has_text=label).first).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize('role', ['approver', 'auditor'])
def test_reviewers_read_the_issues_without_changing_them(browser, live_server, h, role):
    w, _ = conflicted(h)
    context, page = sign_in(browser, live_server, role)
    try:
        navigate(page, f'schedule/issues?draft={w["id"]}&view=checks')
        main = issues_page(page)
        expect(main.locator('.issues-counts')).to_contain_text('read only')
        expect(main.locator('tbody tr[data-row]').first).to_be_visible()
        expect(main.get_by_role('checkbox')).to_have_count(0)
        expect(page.get_by_role('toolbar', name='Bulk actions')).to_have_count(0)
    finally:
        context.close()


def test_staff_cannot_open_rota_issues(browser, live_server, h):
    w, _ = conflicted(h)
    context, page = sign_in(browser, live_server, 'staff')
    try:
        page.evaluate("key => { location.hash = '#' + key; }", f'schedule/issues?draft={w["id"]}&view=checks')
        expect(page.locator('#main[data-loaded-view=restricted]')).to_contain_text('Restricted page')  # the shared page (SW3-C-12)
    finally:
        context.close()


def test_the_issues_list_scrolls_with_the_window_on_a_desktop(browser, live_server, h):
    """The Schedule page is a fixed-height planner; Resolve issues is a list, so the window scrolls it (a mouse wheel
    reaches the last issue)."""
    w, _ = conflicted(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 520})
    try:
        navigate(page, f'schedule/issues?draft={w["id"]}&view=checks')
        main = issues_page(page)
        last = main.locator('tbody tr[data-row]').last
        expect(last).not_to_be_in_viewport()
        page.mouse.move(700, 400)
        for _ in range(10):
            page.mouse.wheel(0, 600)
        expect(last).to_be_in_viewport()
    finally:
        context.close()


def test_a_restricted_schedule_is_not_stretched_to_the_window(browser, live_server):
    """Anything on the Schedule page that is not the planner (Restricted page, an error) keeps its natural height."""
    context, page = sign_in(browser, live_server, 'credentialer')
    try:
        page.evaluate("location.hash = 'schedule'")
        restricted = page.locator('#main .empty-state').filter(has=page.get_by_role('heading', name='Restricted page'))
        expect(restricted).to_be_visible()
        assert restricted.bounding_box()['height'] < 300, 'the message and its button stay together'
    finally:
        context.close()


@pytest.mark.parametrize('historical_exception_flag', [False, True], ids=['current-safety', 'historical-safety'])
def test_issue_words_cards_and_actions_that_do_not_apply(browser, live_server, h, historical_exception_flag):
    """Each row says its issue once (the reason says how the rule is broken, the Rule column names it; overlapping
    duties are not "-4 h rest"); a phone card shows the leave dates and note (department setting: details); actions that
    do not apply to the selection say so without asking the server; rules that keep people safe get no empty exception
    form; Esc cancels a preview; the toast counts every issue a batch resolved."""
    from .test_leave_disclosure import setting
    w, rule = conflicted(h)  # rest keeps people safe and never permits exceptions
    if historical_exception_flag:
        from apps.api import models as m
        from apps.api.db import transaction
        # Historical storage may predate the safety contract; its old flag must grant no exception.
        with transaction(h.app.state.engine, write=True) as db:
            stored = db.get(m.Policy, rule['id'])
            stored.definition = {**stored.definition, 'exception_allowed': True}
    alex = h.seed['people'][0]
    w = h.shift(w, '2026-10-07T08:00:00+03:00', '2026-10-07T12:00:00+03:00', people=[alex])['workspace']
    w = h.shift(w, '2026-10-07T10:00:00+03:00', '2026-10-07T14:00:00+03:00', people=[alex])['workspace']
    w = h.lifecycle(h.detail(w)['workspace'], 'validate')['workspace']
    setting(h, 'details')
    # A phone card shows the leave's dates and note.
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 390, 'height': 844})
    try:
        navigate(page, f'schedule/issues?draft={w["id"]}&view=checks')
        main = issues_page(page)
        card = main.locator('.dt-card').filter(has_text='Annual').first
        expect(card).to_contain_text('5–6 Oct · Family medicine congress')
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f'schedule/issues?draft={w["id"]}&view=checks')
        main = issues_page(page)
        rest = main.locator('tbody tr[data-row]').filter(has_text=rule['name'])
        reason = rest.first.locator('td[data-col=reason] .issue-reason')
        expect(reason.locator(':scope > span')).to_have_text('Minimum rest not met')
        expect(reason.locator('small')).to_have_text('Mandatory · 1 h rest (needs 12 h)')
        assert reason.inner_text().count('Minimum rest') == 1
        expect(main.locator('td[data-col=reason]').filter(has_text='overlaps another duty (needs 12 h between)')).not_to_have_count(0)
        expect(main.locator('td[data-col=reason]').filter(has_text=re.compile(r'-\d+ h rest'))).to_have_count(0)
        # Checkboxes are named by person, issue and session, not just "—" or a name shared by many rows.
        expect(main.get_by_role('checkbox', name=re.compile(r'^Select Alex Morgan · On leave or away · .*Mon 5 Oct'))).not_to_have_count(0)

        posts = []
        page.on('request', lambda r: posts.append(r.url) if r.method == 'POST' else None)
        bar = page.get_by_role('toolbar', name='Bulk actions')
        # Open places have no person: they offer Find someone… (the fill panel), never a Replace or Unassign that fails.
        gaps = group(main, 'Fewer people than the minimum')
        if gaps.count():
            gaps.get_by_role('checkbox').first.check()
            expect(bar.get_by_role('button', name='Unassign')).to_have_count(0)
            expect(bar.get_by_role('button', name='Replace…')).to_have_count(0)
            expect(bar.get_by_role('button', name='Find someone…')).to_be_visible()
            bar.get_by_role('button', name='Clear selection').click()
        # A rest rule keeps people safe: no exception form with nothing in it.
        group(main, rule['name']).get_by_role('checkbox', name=f'Select all {rule["name"]}', exact=True).check()
        before_exception = h.snapshot(w)
        # A stored historical flag on a safety rule grants nothing: Exception… is not offered, as for a current safety rule (G-36).
        expect(bar.get_by_role('button', name='Exception…')).to_have_count(0)
        expect(page.get_by_role('dialog')).to_have_count(0)
        assert not [u for u in posts if '/api/commands' in u], posts
        assert h.snapshot(w) == before_exception

        # Esc cancels a preview; the toast counts every issue the batch resolved (the rest issue went with the leave).
        bar.get_by_role('button', name='Clear selection').click()
        group(main, 'On leave or away').get_by_role('checkbox', name='Select all On leave or away', exact=True).check()
        bar.get_by_role('button', name='Replace…').click()
        banner = main.locator('.issues-preview')
        expect(banner).to_be_visible()
        press_escape(page)
        expect(banner).to_be_hidden()
        bar.get_by_role('button', name='Unassign').click()
        expect(main.locator('.issues-counts')).not_to_contain_text(' · 0 resolved')
        resolved = int(re.search(r'(\d+) resolved', main.locator('.issues-counts').inner_text()).group(1))
        assert resolved > 4, 'the leave rows and the rest issues that went with them'
        expect(page.locator('.toast').filter(has_text='issues resolved').first).to_contain_text(f'{resolved} issues resolved')
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
def test_zero_candidate_replace_focuses_cancel_and_escape_keeps_selection_without_writes(browser, live_server, h, width, scheme):
    from .test_browser_rule_catalogue import bar_action
    restriction = h.policy({'kind': 'restriction'})  # Applies to every candidate, so nobody may take the duty.
    workspace = h.shift(h.workspace('No eligible replacement'), seed_restricted=True)['workspace']
    finding = next(f for f in workspace['validation']['findings'] if f['code'] == 'RESTRICTION' and f['rule_id'] == restriction['id'])
    key = '|'.join(str(finding.get(k) or '') for k in ('code', 'rule_id', 'shift_id', 'person_id', 'start'))
    before = h.snapshot(workspace)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900})
    commands = []
    page.on('request', lambda request: commands.append(request.post_data_json)
            if request.method == 'POST' and request.url.endswith('/api/commands') else None)
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, f'schedule/issues?draft={workspace["id"]}&view=checks')
        main = issues_page(page)
        record = main.locator(f'[data-row="{key}"]')
        record.get_by_role('checkbox').check()
        bar = page.get_by_role('toolbar', name='Bulk actions')
        expect(bar).to_contain_text('1 issue selected')
        with page.expect_response(lambda response: response.url.endswith('/api/planner/issues/replace-preview')) as preview:
            bar_action(page, 'Replace…')
        assert preview.value.status == 200
        assert preview.value.json()['replaceable'] == 0
        banner = main.locator('.issues-preview')
        expect(banner).to_be_visible()
        expect(banner.get_by_role('button', name='Apply 0 replacements', exact=True)).to_be_disabled()
        cancel = banner.get_by_role('button', name='Cancel', exact=True)
        expect(cancel).to_be_focused()
        press_escape(page)  # focus is on Cancel
        expect(banner).to_be_hidden()
        expect(record.get_by_role('checkbox')).to_be_checked()
        expect(bar).to_contain_text('1 issue selected')
        assert not commands
        assert h.snapshot(workspace) == before
    finally:
        context.close()


def test_accepted_advisory_issues_leave_the_footer_warnings(browser, live_server, h):
    from apps.api.services.issues import issue_key
    from .test_resolve_issues import leave_rota, issues, resolve
    from .test_browser import open_rota
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week'}, severity='advisory')
    w = leave_rota(h)
    advisory = [issue_key(f) for f in issues(h, w) if f['severity'] != 'error']
    assert advisory
    resolve(h, w, 'accept', advisory)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        open_rota(page, w['id'])
        expect(page.locator('.app-footer .health-pill.err:not(.score-pill)')).to_be_visible()
        expect(page.locator('.app-footer .health-pill.info')).to_have_count(0)  # every warning was accepted
    finally:
        context.close()



def test_revealed_issues_clear_selection_and_undo_stay_clickable_beside_the_toast(browser, live_server, h):
    """A shown skeleton must not make the fixed selection bar scroll into an actionable toast."""
    w, _ = conflicted(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 600})
    try:
        held = []
        route_url = '**/api/workspaces/' + w['id']
        page.route(route_url, lambda route: held.append(route))
        page.evaluate("id=>location.hash='schedule/issues?view=checks&draft='+id", w['id'])
        wait_held(page, held)
        skeleton = page.locator('#main .loading.page-sk')
        expect(skeleton).to_be_visible()
        expect(skeleton).not_to_have_class(re.compile(r'\bsk-wait\b'))
        page.route(route_url, lambda route: route.continue_())
        for route in held:
            route.continue_()
        main = issues_page(page)
        revealed = main.locator('.issues-list.fade-in')
        expect(revealed).to_be_visible()
        revealed.evaluate('e=>Promise.all(e.getAnimations().map(animation=>animation.finished))')
        counts = main.locator('.issues-counts')
        total = int(re.match(r'(\d+) open', counts.inner_text()).group(1))
        bar = page.get_by_role('toolbar', name='Bulk actions')
        group(main, 'On leave or away').get_by_role('checkbox', name='Select all On leave or away', exact=True).check()
        bar.get_by_role('button', name='Unassign', exact=True).click()
        toast = page.locator('.toast').filter(has_text='issues resolved')
        expect(toast).to_be_visible()
        main.get_by_role('checkbox', name='Select all issues shown', exact=True).check()
        clear = bar.get_by_role('button', name='Clear selection', exact=True)
        clear.scroll_into_view_if_needed()
        clear.click()
        expect(bar).to_be_hidden()
        # Expiring the message must not be what allowed the preceding real pointer click to work.
        expect(toast).to_be_visible()
        toast.get_by_role('button', name='Undo', exact=True).click()
        expect(counts).to_contain_text(f'{total} open · 0 resolved')
    finally:
        context.close()


def test_mandatory_person_row_has_replace_on_the_row(browser, live_server, h):
    """SR-20: a person breaking a mandatory rule has Replace… on the row (previewed first), not only in the bulk bar."""
    w, rule = conflicted(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f'schedule/issues?draft={w["id"]}&view=checks')
        main = issues_page(page)
        row = main.locator('tbody tr[data-row]').filter(has_text=rule['name']).first
        row.get_by_role('button', name='Replace…', exact=True).click()
        expect(main.locator('.issues-preview')).to_be_visible()
    finally:
        context.close()


def test_session_level_rows_name_their_session(browser, live_server, h):
    """LIVE-44: an issue about a session (no person) names the session where the person goes and says what is wrong in
    full, never "—" with "Breaks an advisory rule"."""
    w, _ = conflicted(h)
    w = h.shift(h.detail(w)['workspace'], '2026-10-08T08:00:00+03:00', '2026-10-08T12:00:00+03:00', people=[])['workspace']
    w = h.lifecycle(h.detail(w)['workspace'], 'validate')['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f'schedule/issues?draft={w["id"]}&view=checks')
        main = issues_page(page)
        rows = main.locator('tbody tr[data-row]').filter(has=page.get_by_role('button', name='Find someone…', exact=True))
        expect(rows.first).to_be_visible()
        expect(rows.first.locator('td[data-col=person]')).not_to_have_text('—')
        expect(rows.first.locator('td[data-col=reason]')).not_to_contain_text('Breaks an')
    finally:
        context.close()
