"""Non-issuable synthetic credentials stay outside feedback copies and local storage."""
import json

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401
from .test_browser_feedback import open_with_shortcut, screenshot_ready
from .test_feedback_api import submit, detail

pytestmark = [pytest.mark.browser, pytest.mark.parametrize('h', [pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)]
SENTINEL = 'NON-ISSUABLE-SYNTHETIC-CC01'


def assert_capture_private(page):
    captured = page.evaluate('''() => {const r=window.__fbSnapshot.root;return {
        html:r.outerHTML,masked:r.querySelectorAll('[data-fb-mask]').length,
        publicValue:r.querySelector('#ordinary-field')?.getAttribute('value'),
        links:[...r.querySelectorAll('a')].map(a=>a.getAttribute('href'))};}''')
    assert SENTINEL not in captured['html']
    assert captured['masked'] >= 1
    assert not any(captured['links'])
    return captured


@pytest.mark.parametrize('purpose', ['invite', 'reset'])
def test_native_one_time_link_dialog_masks_owner_marked_output(browser, live_server, h, purpose):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        # Existing component fixture: no account invitation or reset credential is ever issued.
        page.evaluate('''async ([purpose,sentinel])=>{
            const {linkDialog}=await window.importBundle('accounts.js');
            linkDialog(purpose==='invite'?'Invitation link':'Password reset link','Fictional Example (test)',{
                one_time_link:location.origin+'/'+purpose+'/'+sentinel,link:{expires_at:'2026-10-05T00:00:00Z'}});
        }''', [purpose, SENTINEL])
        output = page.get_by_label('One-time link', exact=True)
        expect(output).to_be_visible()
        assert output.evaluate("n=>!!n.closest('[data-private]')")
        feedback = open_with_shortcut(page)
        assert_capture_private(page)
        screenshot_ready(feedback)  # normal snapshot -> render -> screenshot preview
        page.keyboard.press('Escape')
        expect(feedback).to_have_count(0)
        expect(output).to_be_visible()
    finally:
        context.close()


def test_calendar_links_text_fields_and_context_stay_private_without_disabling_hide_undo(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'staff', viewport={'width': 1440, 'height': 1000})
    try:
        active = False
        def command(route):
            nonlocal active
            if route.request.post_data_json['kind'] == 'calendar_feed.create':
                active = True
                route.fulfill(json={'path': '/calendar/' + SENTINEL + '.ics'})
            else:
                route.continue_()
        page.route('**/api/commands', command)
        page.route('**/api/calendar-feed', lambda route: route.fulfill(json={'active': active, 'last_used_at': None}))
        navigate(page, 'insights/exports')
        page.get_by_role('button', name='Subscribe in my calendar', exact=True).click()
        output = page.get_by_label('Your private calendar link', exact=True)
        expect(output).to_be_visible()
        expect(output).to_have_attribute('data-private', '')
        expect(page.locator('.subscription-apps a')).to_have_count(3)
        # Include unmarked pasted fields and text as well as the real calendar app anchors above.
        page.evaluate('''async sentinel=>{
            const {el}=await window.importBundle('dom.js');
            const box=el('section',{id:'privacy-fixture'},
                el('input',{id:'ordinary-field','aria-label':'Ordinary field',value:'Fictional public value'}),
                el('input',{'aria-label':'Pasted invitation',value:location.origin+'/invite/'+sentinel}),
                el('textarea',{'aria-label':'Pasted reset'},location.origin+'/reset/'+sentinel),
                el('p',{},'Copied reset '+location.origin+'/reset/'+sentinel),
                el('a',{href:'https://example.invalid/?next='+encodeURIComponent(location.origin+'/calendar/'+sentinel+'.ics')},'Related address'));
            document.querySelector('#main').prepend(box);
            const c=await window.importBundle('feedback_context.js');
            c.recordError('Failed to open /reset/'+sentinel,'/invite/'+sentinel);
            history.replaceState(null,'','#insights/exports?next=/invite/'+sentinel);
        }''', SENTINEL)
        dialog = open_with_shortcut(page)
        capture = assert_capture_private(page)
        assert capture['masked'] >= 3
        assert capture['publicValue'] == 'Fictional public value'
        assert '/reset/[redacted]' in capture['html']
        screenshot_ready(dialog)
        frame = dialog.locator('.fb-overlay')
        box = frame.bounding_box()
        dialog.get_by_role('button', name='Hide area', exact=True).click()
        page.mouse.move(box['x'] + 25, box['y'] + 25)
        page.mouse.down()
        page.mouse.move(box['x'] + 125, box['y'] + 85, steps=4)
        page.mouse.up()
        expect(dialog.locator('.fb-area.hide')).to_have_count(1)
        dialog.get_by_role('button', name='Undo', exact=True).click()
        expect(dialog.locator('.fb-area.hide')).to_have_count(0)
        dialog.get_by_role('button', name='Redo', exact=True).click()
        expect(dialog.locator('.fb-area.hide')).to_have_count(1)
        # Intercept the local send to inspect the actual prepared payload without forwarding feedback.
        prepared = []
        page.route('**/api/feedback', lambda route: (prepared.append(route.request.post_data_json),
                   route.fulfill(json={'id': '00000000-0000-4000-8000-000000000001'})))
        dialog.get_by_label('What went wrong?', exact=True).fill('Synthetic privacy regression')
        dialog.get_by_role('button', name='Send', exact=True).click()
        expect(dialog).to_have_count(0)
        assert len(prepared) == 1
        assert SENTINEL not in json.dumps(prepared[0])
        assert prepared[0]['recent_errors']
        assert '[redacted]' in prepared[0]['route']
        # Canonical API helpers exercise the storage defense independently of client-side masking.
        report = submit(h, route='#home?next=/invite/' + SENTINEL,
                        recent_errors=[{'at': '2026-10-03T06:00:00Z', 'message': '/reset/' + SENTINEL,
                                        'source': '/calendar/' + SENTINEL + '.ics'}])
        assert SENTINEL not in json.dumps(detail(h, report))
        assert h.get('staff', '/api/calendar-feed')['active'] is False
    finally:
        context.close()
