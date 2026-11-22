"""The status footer: screen status on the left, general status on the right, on every screen, desktop and phone.
The window keeps scrolling (sticky headers work) and nothing at the bottom of the screen covers anything else."""
import re
import time

import pytest
from playwright.sync_api import expect
from .test_browser import route_fetch, browser, live_server, sign_in, navigate, pick_view, open_rota, wait_held  # noqa: F401

pytestmark = pytest.mark.browser
VIEWPORTS = [{"width": 1440, "height": 1000}, {"width": 390, "height": 844}]


def wait_for_held_route(page, held):
    # request fires before its route callback; pump Playwright until interception is actually held.
    deadline = time.monotonic() + 5
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(10)
    assert len(held) == 1


def footer_fits(page):
    """One footer, stuck to the bottom of the viewport, inside it, and no horizontal page scroll."""
    expect(page.locator('.app-footer')).to_have_count(1)
    assert page.evaluate("""() => {
      const f=document.querySelector('.app-footer').getBoundingClientRect();
      return f.left>=0 && f.right<=innerWidth+1 && Math.abs(f.bottom-innerHeight)<=1 && document.documentElement.scrollWidth<=innerWidth+1;
    }"""), page.evaluate("""() => {const f=document.querySelector('.app-footer').getBoundingClientRect();
      return {f:f.toJSON(),h:innerHeight,w:innerWidth,sw:document.documentElement.scrollWidth,
        wide:[...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().right>innerWidth+1).slice(0,5).map(e=>e.className||e.tagName)};}""")


def general_status(page, environment='Test environment'):
    footer = page.locator('.app-footer')
    for selector in ('#connection', '#save-state', '.env-badge'):
        expect(footer.locator(selector)).to_have_count(1)
        expect(page.locator('.topbar ' + selector)).to_have_count(0)
    expect(footer.locator('.env-badge')).to_contain_text(environment)


@pytest.mark.parametrize('viewport', VIEWPORTS, ids=['desktop', 'phone'])
def test_planner_rota_health_and_score_live_only_in_the_footer(browser, live_server, h, viewport):
    h.policy({'kind': 'max_hours', 'hours': 1, 'window': 'week'})
    w = h.workspace('Footer with a long session list')
    for day in range(4, 10):
        for hour in (8, 13):
            w = h.shift(w, start=f'2026-10-{day:02}T{hour:02}:00:00+03:00',
                        end=f'2026-10-{day:02}T{hour+4:02}:00:00+03:00')['workspace']
    w = h.lifecycle(w, 'validate')['workspace']
    must = h.get('coordinator', f"/api/workspaces/{w['id']}/fix-list")['summary']['must']
    assert must > 0
    phone = viewport['width'] < 500
    context, page = sign_in(browser, live_server, 'coordinator', viewport=viewport)
    try:
        navigate(page, 'planner')
        open_rota(page, w['id'])
        footer = page.locator('.app-footer')
        expect(footer.locator('.health-pills')).to_have_count(1)
        # The footer is the one place for rota health: nothing in the top bar repeats it.
        expect(page.locator('.topbar .health-pills, .topbar .workflow-blocked')).to_have_count(0)
        expect(page.locator('.topbar')).not_to_contain_text('blocking')
        # D9: the Fix list's must-fix causes, the server's one count.
        err = footer.get_by_role('button', name=f'{must} must fix · open the fix list', exact=True)
        expect(err).to_be_visible()
        if phone:  # one compact row: icons and numbers, words only in the accessible name
            assert err.locator('.label').evaluate('e => e.getBoundingClientRect().width') <= 1  # clipped, still read out
            expect(err).to_contain_text(str(must))
            assert footer.evaluate('f => f.getBoundingClientRect().height') < 48
        general_status(page)
        footer_fits(page)
        # Submit for review… cannot submit while causes must be fixed, and lists them as Fix list rows (read only).
        page.get_by_role('button', name='Submit for review…', exact=True).click()
        publish = page.get_by_role('dialog', name='Submit Footer with a long session list for review')
        expect(publish.locator('.fix-gate')).to_contain_text(re.compile(rf"{must} causes? must be fixed first"))
        expect(publish.locator('.fix-row')).to_have_count(must)
        expect(publish.get_by_role('button', name='Submit for review', exact=True)).to_be_disabled()
        publish.get_by_role('button', name='Close dialog', exact=True).hover()
        expect(page.get_by_role('tooltip')).to_have_text('Close dialog')
        page.keyboard.press('Escape')
        expect(publish).to_have_count(0)
        # The footer's own popover: the Fix list's head, opened above its button; Escape returns there.
        err.click()
        head = page.locator('.popover.fix-pop')
        expect(head).to_be_visible()
        expect(head.locator('.iss-row')).to_have_count(must)  # the one issue list (live check F6)
        # Owner, 7 Nov 2026: the score shows while anything must be fixed too (its own pill, capped).
        expect(head).not_to_contain_text('Score shows once')
        expect(page.locator('.app-footer .score-pill .score-blocking')).to_have_text('· capped')
        # Opened from the bottom of the screen, the popover opens above its button, fully in view.
        page.wait_for_function("""() => {const p=document.querySelector('.popover').getBoundingClientRect(),f=document.querySelector('.app-footer').getBoundingClientRect();
          return p.top>=0 && p.bottom<=f.top+1;}""")  # after its opening animation
        assert h.detail(w)['workspace']['status'] == 'draft'
        page.keyboard.press('Escape')
        expect(err).to_be_focused()
        expect(footer.locator('.score-pill .score-blocking')).to_have_text('· capped')
        # A selection bar sits above the footer and never covers the last row.
        pick_view(page, 'List')
        rows = page.locator('.dt-card .bulk-check' if phone else 'tbody .bulk-check')
        rows.last.check()
        bar = page.locator('#selection-bar')
        expect(bar).to_be_visible()
        rows.last.scroll_into_view_if_needed()
        assert page.evaluate("""() => {
          const f=document.querySelector('.app-footer').getBoundingClientRect(),b=document.querySelector('#selection-bar').getBoundingClientRect();
          return b.bottom<=f.top+1;
        }""")
        page.get_by_role('button', name='Clear selection', exact=True).click()
        # Any saved change shows in the general status (the rota checks itself, so here a saved view is the change).
        page.locator('.planner-toolbar').get_by_role('button', name='Saved views').click()
        page.get_by_role('menuitem', name='New view…').click()
        page.get_by_role('dialog', name='New view').get_by_label('View name', exact=True).fill('Footer check')
        with page.expect_response(lambda response: response.url.endswith('/api/commands')
                and response.request.method == 'POST'
                and response.request.post_data_json.get('kind') == 'table_view.create'
                and response.request.post_data_json.get('payload', {}).get('table_key') == 'planner'
                and response.request.post_data_json.get('payload', {}).get('name') == 'Footer check') as saved:
            page.get_by_role('dialog', name='New view').get_by_role('button', name='Save view', exact=True).click()
        assert saved.value.status == 200
        expect(footer.locator('#save-state')).to_contain_text('Saved')
        context.set_offline(True)
        page.evaluate("window.dispatchEvent(new Event('offline'))")
        expect(footer.locator('#connection')).to_have_attribute('aria-label', 'Offline · retrying')
        footer_fits(page)
        context.set_offline(False)
        expect(footer.locator('#connection')).to_have_attribute('aria-label', 'Connected', timeout=20000)
        page.reload(); navigate(page, 'planner')
        expect(footer.locator('.footer-total')).to_have_text('12 sessions in view')
        footer_fits(page)
    finally:
        context.close()


@pytest.mark.parametrize('viewport', VIEWPORTS, ids=['desktop', 'phone'])
def test_footer_does_not_present_stale_or_published_health_as_current(browser, live_server, h, viewport):
    # Master decision (10 Nov 2026, maintenance semantics): the approve/publish gate covers the safety rules plus every MANDATORY
    # rule the rota was approved against; ADVISORY rules stay out. An advisory rule activated after submission leaves
    # the submitted rota as it was; a mandatory non-safety one makes it need resubmitting.
    advisory = h.lifecycle(h.shift(h.workspace('Footer advisory change'))['workspace'], 'submit')['workspace']
    h.policy({'kind': 'max_hours', 'hours': 100, 'window': 'week'}, severity='advisory')
    w = h.shift(h.workspace('Footer independent review'))['workspace']
    w = h.lifecycle(w, 'submit')['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', viewport=viewport)
    try:
        navigate(page, 'planner')
        open_rota(page, advisory['id'])
        bar = page.locator('.topbar .planner-bar')
        expect(bar).to_contain_text('Waiting for approval')
        expect(bar).not_to_contain_text('Needs resubmitting')
        # A mandatory, non-safety rule (max_hours is no safety kind) activated after submission.
        h.policy({'kind': 'max_hours', 'hours': 100, 'window': 'week'}, severity='mandatory')
        open_rota(page, w['id'])
        expect(page.locator('.topbar .planner-bar')).to_contain_text('Needs resubmitting')
        expect(page.locator('.app-footer .score-pill')).to_have_count(0)
        expect(page.locator('.app-footer .health-pills')).to_have_count(0)
        if viewport['width'] > 500:
            expect(page.locator('.app-footer .footer-total')).to_have_text('1 session in view')
        footer_fits(page)
        w = h.lifecycle(w, 'reopen')['workspace']
        h.publish(w)
        page.reload(); navigate(page, 'schedule/week/2026-10-04')
        # Published: no draft to edit (it is the read-only published layer) and no score presented as current.
        expect(page.get_by_role('button', name='Choose a draft', exact=True)).to_be_visible()
        expect(page.locator('#schedule-view .pchip, #schedule-view .ag-item').first).to_be_visible()
        expect(page.locator('.app-footer .score-pill')).to_have_count(0)
        footer_fits(page)
    finally:
        context.close()


def test_a_late_planner_load_does_not_replace_the_next_pages_footer(browser, live_server, h):
    w = h.shift(h.workspace('Delayed Planner footer'))['workspace']
    h.lifecycle(w, 'validate')
    context, page = sign_in(browser, live_server, 'coordinator')
    held = []
    try:
        navigate(page, 'people')
        detail = '/api/workspaces/' + w['id'] + '?view=schedule'  # the draft: every Schedule load with it reads it (the sidebar counts never do)
        page.route('**' + detail, lambda route: held.append(route))
        with page.expect_request(lambda request: request.url.endswith(detail)):
            page.evaluate("location.hash='planner'")
        wait_held(page, held)
        navigate(page, 'people')
        # Leaving the page aborted its reads (api.ts leavePage); answering them late changes nothing on the new page.
        for route in held:
            try:
                route.fulfill(response=route_fetch(route))
            except Exception:
                pass  # the browser already dropped the aborted request
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'people')
        # People shows its own status (its list's count), never the Planner's rota health.
        expect(page.locator('.app-footer .page-status .health-pills')).to_have_count(0)
        expect(page.locator('.app-footer .page-status .dt-count')).to_have_text(re.compile(r'^\d+ (person|people)$'))
        expect(page.locator('.topbar .planner-bar')).to_have_count(0)
        footer_fits(page)
    finally:
        context.close()


def test_footer_is_neutral_for_empty_failed_and_private_staff_pages(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator', viewport=VIEWPORTS[1])
    try:
        navigate(page, 'planner')
        # Nothing planned: an empty timeline, and no rota health or score in the footer.
        expect(page.locator('#schedule-view')).to_contain_text('Nothing published this week')  # one empty state, not a fold of empty days
        expect(page.locator('.app-footer .page-status .health-pills, .app-footer .page-status .score-pill')).to_have_count(0)
        general_status(page)
        footer_fits(page)
        navigate(page, 'people')
        page.route('**/api/workspaces?limit=50&offset=0', lambda route: route.abort())
        page.evaluate("location.hash='planner'")
        page.reload()  # a reload: re-opening within the tab answers from its kept reply (api.ts keptReads), never this failure
        expect(page.locator('#main').get_by_role('alert')).to_contain_text('Couldn’t load the schedule.')  # DL-045 (c5a282a5d): the Planner is named "the schedule"
        expect(page.locator('.app-footer .page-status')).to_be_empty()
        footer_fits(page)
        page.unroute('**/api/workspaces?limit=50&offset=0')
        page.get_by_role('button', name='Retry', exact=True).click()
        expect(page.locator('#schedule-view')).to_contain_text('Nothing published this week')  # one empty state, not a fold of empty days
        page.get_by_role('button', name=re.compile('^Account menu,')).first.click()
        page.get_by_role('menuitem', name='Sign out', exact=True).click()
        expect(page.get_by_label('Username or email', exact=True)).to_be_visible()
        expect(page.locator('.app-footer')).to_have_count(0)
        assert page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--status-footer-height')") == ''
        assert page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--topbar-height')") == ''
    finally:
        context.close()
    h.shift(h.workspace("Another person's private draft"))
    context, page = sign_in(browser, live_server, 'staff', viewport=VIEWPORTS[1])
    try:
        navigate(page, 'published')
        expect(page.locator('.app-footer #connection')).to_have_attribute('aria-label', 'Connected')
        # Staff see only their own published duties: nothing from the draft, no rota health or score.
        expect(page.locator('.app-footer .score-pill, .app-footer .health-pills')).to_have_count(0)
        # The screen's first status item stays readable on a phone (not an empty footer with only "? +1").
        expect(page.locator('.app-footer .footer-total').first).to_have_text('0 duties in view')
        expect(page.locator('.app-footer .footer-total').first).to_be_visible()
        general_status(page)
        footer_fits(page)
    finally:
        context.close()


def test_a_single_list_shows_its_count_in_the_footer(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        count = page.locator('.app-footer .page-status .dt-count')
        expect(count).to_have_text(re.compile(r'^(\d+) people$'))
        total = int(count.inner_text().split()[0])
        expect(page.locator('#main .dt-toolbar .dt-count')).to_have_count(0)  # moved, not copied
        page.get_by_role('searchbox', name=re.compile('^Search people')).fill(h.get('coordinator', '/api/catalogues')['people'][0]['display_name'])
        expect(count).to_have_text(re.compile(rf'^\d+ of {total} people$'))
        navigate(page,'requests')  # lists in tabs: only the open tab's list exists, so its count moves to the footer
        expect(page.locator('.app-footer .page-status .dt-count')).to_have_count(1)
    finally:
        context.close()


def test_sticky_headers_and_window_scrolling_survive_the_footer(browser, live_server, h):
    """The page scrolls with the window, so side panels pinned under the top bar stay pinned while their section
    continues; the footer stays at the bottom. (With #main as a scroll container they scrolled away.)"""
    context, page = sign_in(browser, live_server, 'admin', viewport={'width': 1280, 'height': 360})
    try:
        stuck = 0
        for key, selector in (('setup', '.setup-aside'), ('configuration', '.config-nav')):
            navigate(page, key)
            expect(page.locator(selector)).to_be_visible()
            assert page.evaluate("getComputedStyle(document.querySelector('#main')).overflowY") == 'visible'
            page.evaluate("s => {const n=document.querySelector(s);window.scrollTo(0,n.getBoundingClientRect().top-parseFloat(getComputedStyle(n).top)+24);}", selector)
            state = page.evaluate("""s => {const n=document.querySelector(s),r=n.getBoundingClientRect(),offset=parseFloat(getComputedStyle(n).top);
              return {scrolled:document.scrollingElement.scrollTop,top:r.top,offset,room:n.parentElement.getBoundingClientRect().bottom-offset-r.height,
                bar:document.querySelector('.topbar').getBoundingClientRect().bottom};}""", selector)
            if state['room'] > 0:  # its section continues below: it must be pinned at its offset, under the top bar
                assert state['scrolled'] > 0 and abs(state['top'] - state['offset']) <= 1 and state['top'] >= state['bar'], f'{key} {state}'
                stuck += 1
            footer_fits(page)
            page.evaluate('window.scrollTo(0, 0)')
        assert stuck >= 1
    finally:
        context.close()


@pytest.fixture(params=[('dev', 'Test environment'), ('production', 'Production')], ids=['dev', 'production'])
def deployment(request, h):
    h.server_env = {'ROTA_DEPLOYMENT': request.param[0]}
    return request.param


def test_the_badge_names_the_deployment_not_rota_env(deployment, browser, live_server, h):
    """DEV runs ROTA_ENV=production for strict security yet is a test deployment: the badge follows ROTA_DEPLOYMENT."""
    name, label = deployment
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        assert page.request.get(live_server + '/auth/config').json()['deployment'] == name
        assert page.request.get(live_server + '/api/me').json()['deployment'] == name
        navigate(page, 'people')
        general_status(page, label)
        expect(page.locator('.app-footer .env-badge')).not_to_contain_text('Production' if name == 'dev' else 'Test')
    finally:
        context.close()
