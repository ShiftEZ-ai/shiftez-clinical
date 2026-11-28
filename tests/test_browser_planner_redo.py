"""SW3-A-06: the planner's Redo, beside Undo and in the Undo message, by button and keyboard."""
import contextlib

import pytest
from playwright.sync_api import Error as PlaywrightError, expect

from .browser_acceptance_support import shot
from .test_advisory_undo_fingerprint import advisory_rota
from .test_browser import browser, live_server, sign_in, open_rota  # noqa: F401 (fixtures)
from .test_browser_autoschedule_review import DESKTOP, TWO
from .test_planner_redo import move_to, people

pytestmark = pytest.mark.browser


def edited_draft(h):
    first, second = h.seed['people'][:2]
    w = move_to(h, h.shift(h.workspace('Redo journey'), people=[first])['workspace'], second)
    return w, first, second


def command(page, kind):
    return page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.method == 'POST'
                                and r.request.post_data_json.get('kind') == kind)


@pytest.mark.parametrize('view,scheme', TWO)
def test_undo_then_redo_from_the_bar_and_the_message(browser, live_server, h, view, scheme):
    w, first, second = edited_draft(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport=view, color_scheme=scheme)
    try:
        open_rota(page, w['id'])
        bar = page.locator('.planner-bar')
        expect(bar.get_by_role('button', name='Undo last edit', exact=True)).to_be_visible()
        expect(bar.get_by_role('button', name='Redo', exact=True)).to_have_count(0)  # nothing undone yet
        with command(page, 'workspace.undo') as undone:
            bar.get_by_role('button', name='Undo last edit', exact=True).click()
        assert undone.value.ok, undone.value.text()
        assert people(h, w) == [[first]]
        undo = bar.get_by_role('button', name='Undo last edit', exact=True)
        expect(undo).to_be_enabled()
        toast = page.locator('.toast').filter(has_text='Undone')
        expect(toast.get_by_role('button', name='Redo', exact=True)).to_be_visible()
        # The server says this Undo is the viewer's to redo: Redo sits beside Undo too.
        expect(bar.get_by_role('button', name='Redo', exact=True)).to_be_visible()
        shot(page, f"planner-redo-{view['width']}-{scheme}", full_page=False)
        with command(page, 'workspace.redo') as redone:
            toast.get_by_role('button', name='Redo', exact=True).click()
        assert redone.value.ok, redone.value.text()
        assert people(h, w) == [[second]]
        expect(undo).to_be_enabled()
        # A Redo is an edit: its message offers Undo again, and Redo leaves the bar.
        expect(page.locator('.toast').filter(has_text='Redone').get_by_role('button', name='Undo', exact=True)).to_be_visible()
        expect(page.locator('.planner-bar').get_by_role('button', name='Redo', exact=True)).to_have_count(0)
        with command(page, 'workspace.undo'):
            page.locator('.planner-bar').get_by_role('button', name='Undo last edit', exact=True).click()
        with command(page, 'workspace.redo') as again:
            page.locator('.planner-bar').get_by_role('button', name='Redo', exact=True).click()
        assert again.value.ok and people(h, w) == [[second]]
    finally:
        context.close()


def test_consecutive_undo_stops_when_the_edit_history_is_empty(browser, live_server, h):
    w, first, _ = edited_draft(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        compact_read = f"**/api/workspaces/{w['id']}?view=compact"

        def without_availability(route):
            reply = route.fetch()
            data = reply.json()
            del data['workspace']['can_undo']
            route.fulfill(response=reply, json=data)

        page.route(compact_read, without_availability)
        open_rota(page, w['id'])
        undo = page.locator('.planner-bar').get_by_role('button', name='Undo last edit', exact=True)
        expect(undo).to_have_attribute('aria-disabled', 'true')
        expect(undo).to_have_attribute('data-tip', 'Undo availability is unknown')
        page.unroute(compact_read, without_availability)
        page.reload()
        expect(undo).to_be_enabled()
        with command(page, 'workspace.undo') as latest:
            undo.click()
        assert latest.value.ok and people(h, w) == [[first]]
        with command(page, 'workspace.undo') as earlier:
            undo.click()
        assert earlier.value.ok and people(h, w) == []
        expect(undo).to_have_attribute('aria-disabled', 'true')
        expect(undo).to_have_attribute('data-tip', 'Nothing left to undo')
    finally:
        context.close()


def test_old_toast_cannot_undo_an_earlier_edit_after_toolbar_undo(browser, live_server, h):
    workspace, keys = advisory_rota(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_rota(page, workspace['id'])
        page.evaluate("""async ({id, revision, keys}) => {
            const app = document.querySelector('script[type=module][src$="/app.js"]').src;
            const {command} = await import(new URL('api.js', app));
            const {notify} = await import(new URL('dom.js', app));
            await command('issues.resolve', {workspace_id: id, action: 'accept', keys, reason: 'Accept advisory'},
                undefined, revision);
            notify('Issue accepted.');
        }""", {'id': workspace['id'], 'revision': workspace['revision'], 'keys': keys})
        toast = page.locator('.toast').filter(has_text='Issue accepted')
        saved_undo = toast.get_by_role('button', name='Undo', exact=True)
        expect(saved_undo).to_be_visible()
        toolbar_undo = page.locator('.planner-bar').get_by_role('button', name='Undo last edit', exact=True)
        with command(page, 'workspace.undo') as undone:
            toolbar_undo.click()
        assert undone.value.ok, undone.value.text()
        revision = undone.value.json()['workspace']['revision']
        with command(page, 'workspace.undo') as stale:
            saved_undo.click()
        assert stale.value.status == 409
        assert stale.value.json()['error']['code'] == 'STALE_UNDO'
        expect(page.get_by_role('alert').filter(has_text='Nothing was undone.')).to_be_visible()
        assert h.detail(workspace)['workspace']['revision'] == revision
    finally:
        context.close()


def test_keyboard_undo_and_redo_on_the_planner(browser, live_server, h):
    w, first, second = edited_draft(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP, color_scheme='dark')
    try:
        open_rota(page, w['id'])
        page.locator('.planner-bar').get_by_role('button', name='Undo last edit', exact=True).click()
        expect(page.locator('.toast').filter(has_text='Undone')).to_be_visible()
        assert people(h, w) == [[first]]
        page.locator('body').click(position={'x': 5, 'y': 5})
        with command(page, 'workspace.redo') as redone:
            page.keyboard.press('ControlOrMeta+Shift+KeyZ')  # the message's Redo
        assert redone.value.ok and people(h, w) == [[second]]
        expect(page.locator('.toast').filter(has_text='Redone')).to_be_visible()
        with command(page, 'workspace.undo'):
            page.keyboard.press('ControlOrMeta+KeyZ')  # the "Redone." message's Undo
        expect(page.locator('.planner-bar').get_by_role('button', name='Redo', exact=True)).to_be_visible()
        # With no message showing, Ctrl Y presses the bar's Redo.
        for close in page.locator('.toast .toast-close').all():
            close.click()
        expect(page.locator('.toast')).to_have_count(0)
        with command(page, 'workspace.redo') as again:
            page.keyboard.press('Control+KeyY')
        assert again.value.ok and people(h, w) == [[second]]
    finally:
        context.close()


def test_redo_shows_beside_undo_from_the_undo_reply(browser, live_server, h):
    """H-11: Redo sits beside Undo as soon as the Undo answers, not only once the draft is read again (a minute under
    load); both buttons name their shortcut. The re-read is held back here to prove it."""
    w, first, second = edited_draft(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP, color_scheme='light')
    held = []
    try:
        open_rota(page, w['id'])
        bar = page.locator('.planner-bar')
        undo = bar.get_by_role('button', name='Undo last edit', exact=True)
        expect(undo).to_have_attribute('data-tip', 'Undo (Ctrl Z)')
        page.route(f"**/api/workspaces/{w['id']}*", lambda route: held.append(route) if route.request.method == 'GET' else route.continue_())
        with command(page, 'workspace.undo') as undone:
            undo.click()
        assert undone.value.ok, undone.value.text()
        redo = page.locator('.planner-bar').get_by_role('button', name='Redo', exact=True)
        expect(redo).to_be_visible()
        expect(redo).to_have_attribute('data-tip', 'Redo (Ctrl Shift Z)')
        page.unroute(f"**/api/workspaces/{w['id']}*")
        for route in held:
            with contextlib.suppress(PlaywrightError):  # a read the page has since dropped (aborted) is already handled
                route.continue_()
        with command(page, 'workspace.redo') as redone:
            page.locator('.planner-bar').get_by_role('button', name='Redo', exact=True).click()
        assert redone.value.ok and people(h, w) == [[second]]
    finally:
        context.close()
