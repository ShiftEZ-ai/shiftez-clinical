"""Send feedback from any page (docs/design/feedback.md): every kind, from an error message, private fields masked in the
page copy, Mark a spot and Hide area, My feedback and System › Feedback, triage with the reporter's Inbox notice, Open
where it happened, and the capture time on a department-scale Schedule. Desktop light and dark; staff on a phone.
FEEDBACK_SHOTS=<folder> keeps a screenshot of every state for review."""
import os
import re
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import expect
from tests.outbox_support import process_one_after_undo as process_one
from .test_browser import browser, live_server, restart_app, sign_in  # noqa: F401
from .test_home import big_department
from .governance_support import developer

pytestmark = pytest.mark.browser
DESKTOP, PHONE = {'width': 1440, 'height': 1000}, {'width': 390, 'height': 844}


def shot(page, name):
    folder = os.getenv('FEEDBACK_SHOTS')
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(folder) / f'{name}.png'))


def open_with_shortcut(page):
    page.keyboard.press('Control+Shift+F')
    dialog = page.get_by_role('dialog', name='Send feedback')
    expect(dialog).to_be_visible()
    page_copied(dialog)
    return dialog


def page_copied(dialog):
    """The page is copied on the frame after the dialog opens (window.__fbSnapshot is then set)."""
    expect(dialog).to_have_attribute('data-captured', 'yes')


def screenshot_ready(dialog):
    expect(dialog.get_by_role('img', name='Screenshot of the page')).to_be_visible(timeout=15000)


def send(dialog):
    dialog.get_by_role('button', name='Send').click()
    try:
        expect(dialog).to_have_count(0)
    except AssertionError:
        raise AssertionError('Not sent: ' + dialog.locator('[aria-live=assertive]').inner_text()) from None


def reports(h, role='operator'):
    return h.get(role, '/api/feedback')['rows']


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_every_kind_is_sent_with_its_details_and_listed_in_my_feedback(browser, live_server, h, scheme):
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        page.emulate_media(color_scheme=scheme)
        page.goto(live_server + '/#home')
        expect(page.locator('#main[data-loaded-view=home]')).to_be_visible()
        # In-app navigation (the sidebar), not address changes, is what "Visited before" must follow.
        nav = page.get_by_role('navigation', name='Department pages', exact=True)
        for route in ('requests', 'people'):
            nav.locator(f'[data-route="{route}"]').click()
            expect(page.locator(f'#main[data-loaded-view={route}]')).to_be_visible()
        for kind, label, text in (('problem', 'Problem', 'The people list jumped to the top'),
                                  ('suggestion', 'Suggestion', 'Add a column for the phone extension'),
                                  ('confusing', 'Confusing', 'The word "eligibility" is unclear here'),
                                  ('question', 'Question', 'How do I add a second rank?')):
            dialog = open_with_shortcut(page)
            dialog.get_by_role('radio', name=label).click()
            expect(dialog.get_by_role('radio', name=label)).to_have_attribute('aria-checked', 'true')
            # Only a problem asks what was expected.
            expect(dialog.get_by_label('What did you expect? (optional)')).to_be_visible() if kind == 'problem' else \
                expect(dialog.get_by_label('What did you expect? (optional)')).to_be_hidden()
            dialog.get_by_role('textbox').first.fill(text)
            screenshot_ready(dialog)
            if kind == 'problem':
                details = dialog.locator('.fb-details')
                # Collapsed to one line by default; counts, not raw lists, once open.
                expect(details).not_to_have_attribute('open', '')
                expect(details.locator(':scope>summary')).to_contain_text('Page, view, version, browser')
                expect(details.locator(':scope>summary')).to_contain_text('request reference')
                shot(page, f'dialog-collapsed-{scheme}')
                details.locator(':scope>summary').click()
                refs_open = details.locator('dd .fb-count-list').last
                refs = details.locator('dd').filter(has=page.locator('.fb-count-list')).last
                expect(refs.locator('summary')).to_have_text(re.compile(r'^\d+ · Show$'))
                expect(refs.locator('.fb-ids')).to_be_hidden()
                expect(details.locator('dd', has_text='Chrome')).to_have_text(re.compile(r'^Chrome \d+ · (Windows|Linux|macOS)$'))
                refs_open.locator('summary').click()
                expect(refs_open.locator('.fb-ids')).to_be_visible()
                for label_text in ('Page', 'Visited before', 'View', 'Version', 'Environment', 'Roles', 'Screen', 'Browser', 'Recent errors', 'Request references'):
                    expect(details.locator('dt', has_text=label_text)).to_have_count(1)
                expect(details).to_contain_text('People (#people)')
                shot(page, f'dialog-problem-{scheme}')
            send(dialog)
            expect(page.get_by_role('status').filter(has_text='Your feedback was sent').first).to_be_visible()
        rows = reports(h, 'coordinator')
        assert sorted(r['kind'] for r in rows) == ['confusing', 'problem', 'question', 'suggestion']
        sent = {r['kind']: r for r in rows}
        assert sent['problem']['route'] == '/people' and sent['problem']['has_screenshot']
        assert sent['problem']['view_state']['active_view'] == 'people:people'
        # The pages visited before, in order, as the server stores them (no queries, no record identifiers);
        # the current page is the report's Page, not repeated here.
        assert sent['problem']['recent_routes'][-2:] == ['/home', '/requests']
        assert sent['problem']['request_ids'] and sent['problem']['theme'] == 'system'
        # "View my feedback" from the last confirmation opens the report.
        page.get_by_role('button', name='View my feedback').first.click()
        expect(page.locator('#main[data-loaded-view=feedback]')).to_be_visible()
        expect(page.locator('.dock-panel')).to_contain_text('How do I add a second rank?')
        expect(page.locator('.dock-panel .fb-img')).to_be_visible()
        shot(page, f'my-feedback-{scheme}')
    finally:
        context.close()


def test_feedback_sends_ten_previous_visits_without_query_changes_or_current_page(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'staff', viewport=PHONE)
    try:
        expect(page.locator('#main[data-loaded-view=home]')).to_be_visible()
        previous = page.evaluate("""async () => {
            const {clearFeedbackContext, recentRoutes} = await window.importBundle('feedback_context.js');
            const {FEEDBACK_LIMITS} = await window.importBundle('generated.js');
            clearFeedbackContext();
            const visits = Array.from({length: FEEDBACK_LIMITS.recent_routes + 2}, (_, i) =>
                i % 2 ? '#requests' : '#people');
            for (const route of visits) {
                history.pushState(null, '', route + '?private=first');
                history.replaceState(null, '', route + '?private=last');
            }
            history.replaceState(null, '', '#home');
            return {captured: recentRoutes(), expected: visits.slice(-FEEDBACK_LIMITS.recent_routes)};
        }""")
        assert previous['captured'] == [route + '?private=last' for route in previous['expected']] + ['#home']
        dialog = open_with_shortcut(page)
        dialog.get_by_role('textbox').first.fill('Synthetic bounded route history')
        screenshot_ready(dialog)
        send(dialog)
        assert reports(h, 'staff')[0]['recent_routes'] == [route.replace('#', '/') for route in previous['expected']]
    finally:
        context.close()


def test_report_this_problem_on_an_error_message_attaches_that_error(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'staff', viewport=DESKTOP)
    try:
        expect(page.locator('#main[data-loaded-view=home]')).to_be_visible()
        # The bell reads /api/notifications?limit=N (9141caab2): the route takes the query too.
        page.route(re.compile(r'.*/api/notifications(\?.*)?$'), lambda route: route.fulfill(status=500, json={}))
        # Home reads the notices too (f5 H-18) and the bell shares a read still on its way.
        restart_app(page)
        expect(page.locator('#main[data-loaded-view=home]')).to_be_visible()
        page.locator('.bell').click()
        toast = page.locator('.toast.error').filter(has_text='Couldn’t load the inbox.')
        expect(toast).to_be_visible()
        shot(page, 'error-toast')
        toast.get_by_role('button', name='Report this problem').click()
        dialog = page.get_by_role('dialog', name='Send feedback')
        expect(dialog.get_by_role('radio', name='Problem')).to_have_attribute('aria-checked', 'true')
        expect(dialog.locator('.fb-error')).to_contain_text('Couldn’t load the inbox.')
        page_copied(dialog)
        # The page was copied with the message still on it.
        assert 'Couldn’t load the inbox.' in page.evaluate('window.__fbSnapshot.root.textContent')
        screenshot_ready(dialog)
        shot(page, 'error-report-dialog')
        dialog.get_by_label('What went wrong?').fill('I pressed the bell')
        send(dialog)
        sent = reports(h, 'staff')[0]
        assert sent['kind'] == 'problem' and sent['recent_errors'][0]['message'] == '[free-text error redacted]'
    finally:
        context.close()


def test_private_fields_are_masked_and_mark_and_hide_reach_the_report(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'staff', viewport=DESKTOP)
    try:
        page.goto(live_server + '/#requests')
        expect(page.locator('#main[data-loaded-view=requests]')).to_be_visible()
        # A form with a private reason being typed: the feedback dialog opens over it.
        page.get_by_role('button', name='New request', exact=True).click()
        form = page.get_by_role('dialog', name='New request')
        form.get_by_role('radio', name=re.compile('^Leave')).check()
        reason = form.get_by_label(re.compile('reason', re.I)).first
        reason.fill('Private hospital appointment XYZZY')
        dialog = open_with_shortcut(page)
        masked = page.evaluate('''() => { const r = window.__fbSnapshot.root;
            return {text: r.textContent.includes('XYZZY') || [...r.querySelectorAll('textarea,input')].some(i => (i.getAttribute('value')||'').includes('XYZZY')),
                    marks: r.querySelectorAll('[data-fb-mask]').length}; }''')
        assert masked['text'] is False and masked['marks'] >= 1
        expect(dialog.locator('.fb-caption')).to_contain_text('hidden automatically')
        screenshot_ready(dialog)
        frame = dialog.locator('.fb-overlay')
        box = frame.bounding_box()
        dialog.get_by_role('button', name='Mark a spot').click()
        page.mouse.move(box['x'] + 40, box['y'] + 30)
        page.mouse.down()
        page.mouse.move(box['x'] + 200, box['y'] + 120, steps=4)
        page.mouse.up()
        dialog.get_by_role('button', name='Hide area').click()
        page.mouse.move(box['x'] + box['width'] - 200, box['y'] + 40)
        page.mouse.down()
        page.mouse.move(box['x'] + box['width'] - 40, box['y'] + 140, steps=4)
        page.mouse.up()
        expect(dialog.locator('.fb-area.mark')).to_have_count(1)
        expect(dialog.locator('.fb-area.hide')).to_have_count(1)
        expect(dialog.locator('.fb-caption')).to_contain_text('1 spot marked')
        # From the keyboard: a hidden area moves with the arrows and goes with Delete.
        dialog.get_by_role('button', name='Hide area').press('Enter')
        added = dialog.locator('.fb-area.hide').nth(1)
        expect(added).to_be_focused()
        before = added.bounding_box()
        page.keyboard.press('ArrowRight')
        assert added.bounding_box()['x'] > before['x']
        page.keyboard.press('Delete')
        expect(dialog.locator('.fb-area.hide')).to_have_count(1)
        shot(page, 'mark-and-hide')
        # With the mouse a drawn box moves and resizes by a corner; its button removes it; each step undoes and redoes.
        hidden = dialog.locator('.fb-area.hide')
        start = hidden.bounding_box()
        page.mouse.move(start['x'] + start['width'] / 2, start['y'] + start['height'] / 2 + 8)
        page.mouse.down()
        page.mouse.move(start['x'] + start['width'] / 2 - 60, start['y'] + start['height'] / 2 + 48, steps=4)
        page.mouse.up()
        moved = hidden.bounding_box()
        assert moved['x'] < start['x'] - 40 and moved['y'] > start['y'] + 30
        hidden.hover()
        corner = hidden.locator('.fb-area-handle.se').bounding_box()
        page.mouse.move(corner['x'] + corner['width'] / 2, corner['y'] + corner['height'] / 2)
        page.mouse.down()
        page.mouse.move(corner['x'] + 50, corner['y'] + 30, steps=4)
        page.mouse.up()
        assert hidden.bounding_box()['width'] > moved['width'] + 30
        dialog.get_by_role('button', name='Undo').click()
        assert abs(hidden.bounding_box()['width'] - moved['width']) < 2
        page.keyboard.press('Control+z')
        assert abs(hidden.bounding_box()['x'] - start['x']) < 2
        dialog.get_by_role('button', name='Redo').click()
        assert abs(hidden.bounding_box()['x'] - moved['x']) < 2
        hidden.hover()
        hidden.get_by_role('button', name='Remove hidden area').click()
        expect(hidden).to_have_count(0)
        page.keyboard.press('Control+z')
        expect(hidden).to_have_count(1)
        overlay, final = frame.bounding_box(), hidden.bounding_box()
        centre = ((final['x'] + final['width'] / 2 - overlay['x']) / overlay['width'],
                  (final['y'] + final['height'] / 2 - overlay['y']) / overlay['height'])
        shot(page, 'mark-and-hide')
        dialog.get_by_role('radio', name='Confusing').click()
        dialog.get_by_label('What is confusing?').fill('This reason field label is unclear')
        send(dialog)
        sent = reports(h, 'staff')[0]
        assert sent['kind'] == 'confusing' and sent['has_screenshot']
        mark = sent['mark']
        assert 0 <= mark['x'] < 0.3 and 0 < mark['w'] < 0.5
        image = h.client('staff').get(f"/api/feedback/{sent['id']}/screenshot")
        assert image.status_code == 200 and image.headers['content-type'] in ('image/webp', 'image/png')
        # The hidden area is painted into the picture itself, not laid over it.
        pixel = page.evaluate('''async ([id, x, y]) => { const b = await (await fetch(`/api/feedback/${id}/screenshot`)).blob();
            const i = await createImageBitmap(b), c = new OffscreenCanvas(i.width, i.height), g = c.getContext('2d');
            g.drawImage(i, 0, 0); return [...g.getImageData(Math.floor(x * i.width), Math.floor(y * i.height), 1, 1).data]; }''',
            [sent['id'], centre[0], centre[1]])
        assert all(abs(got - want) <= 12 for got, want in zip(pixel[:3], (0x64, 0x74, 0x8B))), pixel
    finally:
        context.close()


def send_as(h, role, message, route='#people', **extra):
    reply = h.client(role).post('/api/feedback', json={'route': route, 'kind': 'problem', 'message': message, **extra},
                                headers={'idempotency-key': str(uuid.uuid4())})
    assert reply.status_code == 200, reply.text
    return reply.json()['id']


def test_developer_triages_and_the_reporter_is_told_in_the_inbox(browser, live_server, h):
    send_as(h, 'staff', 'The week view shows the wrong day', route='#schedule/week/2026-10-05',
            view_state={'active_view': 'schedule', 'group_by': 'clinic'})
    context, page = sign_in(browser, live_server, developer(h), viewport=DESKTOP)
    try:
        page.goto(live_server + '/#system/feedback')
        expect(page.locator('#main[data-loaded-tab=feedback]')).to_be_visible()
        row = page.locator('.dt [data-row]').filter(has_text='The week view shows the wrong day')
        expect(row).to_be_visible()
        shot(page, 'admin-list')
        row.locator('.fb-row-msg').click()
        panel = page.locator('.dock-panel')
        expect(panel).to_contain_text('No screenshot was sent.')
        expect(panel.locator('dt', has_text='Reporter')).to_have_count(1)
        expect(row).not_to_contain_text('Unknown')
        expect(row).to_contain_text('1')  # occurrences: a single report reads 1
        # GitHub: the sanitised text is shown first; the message only when ticked.
        panel.get_by_role('button', name='Create GitHub issue…').click()
        issue = page.get_by_role('dialog', name='Create GitHub issue')
        expect(issue.locator('.fb-issue-body')).to_be_visible()
        expect(issue.locator('.fb-issue-body')).not_to_contain_text('The week view shows the wrong day')
        issue.get_by_text('Include the message').click()
        expect(issue.locator('.fb-issue-body')).to_contain_text('week view')
        expect(issue).to_contain_text('GitHub is not connected')
        expect(issue.get_by_role('link', name='connect GitHub in Configuration › Integrations')).to_have_attribute('href', '#configuration/integrations')
        shot(page, 'admin-github')
        issue.get_by_role('button', name='Close', exact=True).click()
        expect(issue).to_have_count(0)
        panel.get_by_label('Status').select_option('fixed')
        panel.get_by_label('Fixed in version').fill('0.8.1')
        panel.get_by_label('Note to the reporter').fill('Thanks, fixed in the next release.')
        shot(page, 'admin-triage')
        panel.get_by_role('button', name='Save').click()
        expect(page.get_by_role('status').filter(has_text='Saved. Fixed')).to_be_visible()
        expect(page.locator('.dock-panel .fb-said .badge')).to_contain_text(['Fixed'])
        first = next(r for r in h.get('operator', '/api/feedback')['rows'] if r['message'].startswith('The week view'))
        assert first['status'] == 'fixed' and first['resolved_in'] == '0.8.1'
        # A second report marked Same as the first through the shared picker; the first then lists it.
        second = send_as(h, 'coordinator', 'Week view day is off by one', route='#schedule/day/2026-10-05')
        page.goto(live_server + f'/#system/feedback?open={second}')
        panel = page.locator('.dock-panel')
        expect(panel).to_contain_text('Week view day is off by one')
        # F13 A-32: opening a report reads no list for Same as; choosing Duplicate reads it.
        listed = []
        page.on('request', lambda r: listed.append(r.url) if '/api/feedback?page=1&page_size=100' in r.url else None)
        expect(panel.get_by_label('Status')).to_be_visible()
        assert listed == []
        with page.expect_response(lambda r: '/api/feedback?page=1&page_size=100' in r.url):
            panel.get_by_label('Status').select_option('duplicate')
        same = panel.locator('.field', has=page.get_by_text('Same as', exact=True)).locator('select')
        expect(same.locator('option', has_text='The week view shows the wrong day')).to_have_count(1)
        same.select_option(label=same.locator('option', has_text='The week view shows the wrong day').inner_text())
        panel.get_by_role('button', name='Save').click()
        expect(page.locator('.dock-panel')).to_contain_text('Same as')
        page.locator('.dock-panel .fb-links').get_by_role('button', name=re.compile('The week view shows')).click()
        expect(page.locator('.dock-panel .fb-links')).to_contain_text('Linked reports (1)')
        shot(page, 'admin-linked')
    finally:
        context.close()
    process_one(h.app.state.engine)
    context, page = sign_in(browser, live_server, 'staff', viewport=PHONE)
    try:
        page.goto(live_server + '/#inbox')
        note = page.locator('.note-item').filter(has_text='Your feedback was updated')
        expect(note).to_be_visible()
        expect(note).to_contain_text('fixed in 0.8.1')
        shot(page, 'staff-inbox-phone')
        note.locator('.note-main').click()
        # A phone shows the report's panel as a full-width sheet over its list.
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'feedback')
        expect(page.locator('.dock-panel')).to_contain_text('Thanks, fixed in the next release.')
        shot(page, 'staff-my-feedback-phone')
    finally:
        context.close()


def test_open_where_it_happened_restores_the_page_and_its_list_view(browser, live_server, h):
    context, page = sign_in(browser, live_server, developer(h), viewport=DESKTOP)
    try:
        page.goto(live_server + '/#access/people')
        expect(page.locator('#main[data-loaded-tab=people]')).to_be_visible()
        toolbar = page.locator('.dt-toolbar').first
        toolbar.locator('.dt-tool', has_text='Group').click()
        page.locator('.popover [role=menuitemradio]').nth(1).click()
        group = toolbar.locator('.dt-tool').filter(has_text='Group:').inner_text()
        dialog = open_with_shortcut(page)
        dialog.get_by_role('radio', name='Suggestion').click()
        dialog.get_by_role('textbox').first.fill('Grouped people need counts')
        send(dialog)
        page.goto(live_server + '/#home')
        expect(page.locator('#main[data-loaded-view=home]')).to_be_visible()
        # Back to the default grouping, then reopen from the report.
        page.evaluate("for (const k of Object.keys(localStorage)) if (k.startsWith('rota.table.')) localStorage.removeItem(k)")
        page.goto(live_server + '/#system/feedback')
        page.locator('.dt [data-row]').filter(has_text='Grouped people need counts').click()
        page.locator('.dock-panel').get_by_role('button', name='Open where it happened').click()
        expect(page.locator('#main[data-loaded-tab=people]')).to_be_visible()
        expect(page.locator('.dt-toolbar').first.locator('.dt-tool').filter(has_text='Group:')).to_have_text(group)
    finally:
        context.close()


def test_staff_send_feedback_from_the_account_menu_on_a_phone(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'staff', viewport=PHONE)
    try:
        page.emulate_media(color_scheme='dark')
        expect(page.locator('#main[data-loaded-view=home]')).to_be_visible()
        page.locator('.mobile-account').click()
        page.get_by_role('menuitem', name='Send feedback').click()
        dialog = page.get_by_role('dialog', name='Send feedback')
        screenshot_ready(dialog)
        dialog.get_by_role('radio', name='Question').click()
        dialog.get_by_label('Your question').fill('Where do I see next month?')
        shot(page, 'phone-dialog-dark')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        dialog.get_by_role('button', name='Send').click()
        expect(page.get_by_role('status').filter(has_text='Your feedback was sent').first).to_be_visible()
        page.locator('.mobile-account').click()
        page.get_by_role('menuitem', name='My feedback').click()
        expect(page.locator('#main[data-loaded-view=feedback]')).to_be_visible()
        expect(page.locator('#main')).to_contain_text('Where do I see next month?')
        shot(page, 'phone-my-feedback-dark')
    finally:
        context.close()


@pytest.fixture
def department_scale_capture(browser, live_server, h):
    # 160 people, a month as one row per person (about 5,000 day cells) and today's sessions: the heaviest page.
    big_department(h, today_sessions=48)
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        page.goto(live_server + '/#schedule/people/2026-10-03?edit=none&range=month')
        expect(page.locator('#main[data-loaded-view=schedule]')).to_be_visible()
        expect(page.locator('#main')).to_contain_text('Person 159')
        page.wait_for_function('() => !!window.__fbWarm')
        # The key press only opens the dialog; the long page is copied on the next frame, off the key's handler.
        key = page.evaluate("""() => { const t = performance.now();
            document.dispatchEvent(new KeyboardEvent('keydown', {key: 'F', ctrlKey: true, shiftKey: true, bubbles: true}));
            const d = document.querySelector('.fb-dialog');
            return {ms: performance.now() - t, open: !!d?.open, copied: d?.dataset.captured === 'yes'}; }""")
        assert key['open'] and not key['copied'], key
        dialog = page.get_by_role('dialog', name='Send feedback')
        page_copied(dialog)
        screenshot_ready(dialog)
        took = int(dialog.get_attribute('data-capture-ms'))
        shot(page, 'schedule-capture')
        copied, total = int(dialog.get_attribute('data-copied')), page.evaluate("document.querySelectorAll('*').length")
        print(f'capture {took} ms; {copied} of {total} elements copied;', dialog.locator('canvas').get_attribute('data-timing'))
        print(f'shortcut handler {key["ms"]:.0f} ms')
        yield page, took, copied, total, key['ms']
    finally:
        context.close()


def test_capture_is_quick_on_a_department_scale_schedule(department_scale_capture):
    page, _, _, _, _ = department_scale_capture
    assert page.evaluate('window.__fbWarm') is True
    # The dialog opened first, but it is not part of its own picture.
    assert page.evaluate("window.__fbSnapshot.root.querySelector('.fb-dialog')") is None


def test_a_long_table_is_copied_row_by_row_only_where_it_is_in_view(browser, live_server, h):
    """AD-14: a table row out of view is one empty box of its height, not a copy of each of its cells."""
    from .test_browser_bulk_changes import bar
    from .test_browser import close_popover, navigate, toolbar
    alex, kind = h.seed['people'][0], h.get('admin', '/api/catalogues')['leave_types'][0]['id']
    h.command('coordinator', 'bulk.apply', {'item_kind': 'availability.record', 'reason': 'Test setup',
              'common': {'person_id': alex, 'kind': 'recorded_leave', 'leave_type_id': kind, 'private_reason': 'Long list'},
              'items': [{'payload': {'start': f'2027-{1 + n // 28:02d}-{1 + n % 28:02d}T08:00:00+03:00',
                                     'end': f'2027-{1 + n // 28:02d}-{1 + n % 28:02d}T09:00:00+03:00'}} for n in range(200)]})
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        navigate(page, 'people')
        page.locator('#main tr', has_text='Staff 01(test)').locator('.bulk-check').check()
        bar(page).get_by_role('button', name='Show their…').click()
        page.get_by_role('menu', name='Show their…').get_by_role('menuitem', name=re.compile('^Leave')).click()
        toolbar(page.locator('#main'), 'Filter').get_by_role('button', name='Clear all filters', exact=True).click()
        close_popover(page)
        rows = page.locator('#main tbody tr')
        expect(rows).to_have_count(200)  # the server's first page, all drawn (serverPaged); about 20 fit the screen
        cells = page.evaluate("document.querySelectorAll('#main tbody td, #main tbody td *').length")
        dialog = open_with_shortcut(page)
        screenshot_ready(dialog)
        copied, total = int(dialog.get_attribute('data-copied')), page.evaluate("document.querySelectorAll('*').length")
        assert copied < total - cells * 3 // 4, f'{copied} of {total} elements copied ({cells} in the table body)'
    finally:
        context.close()


@pytest.mark.wall_benchmark
def test_department_scale_capture_cpu_budget(department_scale_capture):
    _, took, copied, total, key_ms = department_scale_capture
    scale = 3 if os.getenv('CI') else 1
    assert took < 1500 * scale, f'capture took {took} ms ({copied} of {total} elements copied)'
    assert key_ms < 150 * scale, f'the shortcut held the key press for {key_ms:.0f} ms'
