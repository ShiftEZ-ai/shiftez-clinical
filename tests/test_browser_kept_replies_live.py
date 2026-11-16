"""maintenance/maintenance (maintenance review): a page opened again answers from the replies this tab holds (api.ts keptReads). A reply carries
more than the page that read it shows (/api/catalogues holds people and settings), so ANY live change forgets every kept
reply, including one on a page that shows nothing of it (Home and a people change), and a pulse held under an open
dialog forgets when it is applied, not before (maintenance's rule). The probe asks through the app's own api.js (the same module
instance) and says whether the answer came from memory (resolved before any network turn) or from the server."""
import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from .test_browser import PAGE_LOAD_MS, browser, live_server, sign_in, navigate  # noqa: F401
from .test_browser_live_scopes import RECORD_PULSES, command
from .governance_support import identity

pytestmark = pytest.mark.browser

# Branding is read once at boot and by no page's refresh: only the forgetting can send it to the server again.
KEPT = '/api/branding'
FROM_MEMORY = """async path => {const api = await window.importBundle('api.js'); let done = false;
  const opened = api.keptReads(() => api.get(path).then(() => {done = true;}), () => {});
  for (let i = 0; i < 5; i++) await Promise.resolve();
  const memory = done; await opened; return memory;}"""


def home_live():
    """Home's live scopes, read from the one definition (app.ts paths.home.live), never copied here."""
    source = (Path(__file__).resolve().parents[1] / 'apps/web/src/app.ts').read_text(encoding='utf-8')
    found = re.search(r"^\s*home:\{[^\n]*?live:(\[[^\]]*\])", source, re.M)
    assert found, 'paths.home.live not found in app.ts'
    return set(json.loads(found.group(1).replace("'", '"')))


def someone_adds_a_person(planner, page, name):
    """Another tab's save in the people scope; this tab's stream announces it. Its pulse also names `catalogue`, which
    Home shows: Home must apply it, so an open dialog holds it (the held-pulse test needs exactly that)."""
    since = page.evaluate('window.livePulses.length')
    command(planner, 'catalogue.create', {'catalogue': 'people', 'value': {'display_name': name}, 'reason': 'Fictional'})
    page.wait_for_function("""since => window.livePulses.slice(since).some(p => (p.changes || []).some(c => c.scope === 'people'))""",
                           arg=since, timeout=15000)
    page.clock.run_for(400)  # the pushed pulse's burst window (app.ts queuePulse)


def home_with_a_kept_reply(browser, live_server):
    planner = sign_in(browser, live_server, 'coordinator')[1]
    context, page = sign_in(browser, live_server, 'coordinator')
    context.add_init_script(RECORD_PULSES)
    page.reload()
    expect(page.locator('#main[data-loaded-view]')).to_have_count(1, timeout=PAGE_LOAD_MS)
    navigate(page, 'home')
    page.wait_for_function('() => window.livePulses.length > 0', timeout=15000)  # the stream's starting point
    # Read on Home (outside an opening, so from the server): the reply is kept as Home read it.
    page.evaluate("async path => {await (await window.importBundle('api.js')).get(path);}", KEPT)
    assert page.evaluate(FROM_MEMORY, KEPT) is True  # kept: an opening answers from memory
    return planner, context, page


def test_a_people_change_on_home_forgets_every_kept_reply(browser, live_server, h):
    """A trainee note's pulse names only `people`, which Home does not show: Home reads nothing for it, so only the
    forgetting of every kept reply sends the probe to the server."""
    programme = h.configure('programmes', {'name': 'Fictional kept-reply programme', 'starts_on': '2026-01-01'})['record']
    trainee = h.configure('people', {'display_name': 'Fictional kept-reply trainee'})['record']['id']
    lead = h.configure('people', {'display_name': 'Fictional kept-reply lead'})['record']['id']
    identity(h, 'programme_lead', ['programme_lead'], lead)  # the harness seeds no programme lead sign-in
    for person in (lead, trainee):  # the lead leads the trainee's programme
        h.configure('programme_memberships', {'person_id': person, 'programme_id': programme['id'],
                    'starts_on': '2026-01-01', 'ends_on': '2027-11-01'}, role='coordinator')
    planner, context, page = home_with_a_kept_reply(browser, live_server)
    lead_context, lead_page = sign_in(browser, live_server, 'programme_lead')
    try:
        since = page.evaluate('window.livePulses.length')
        # Through the live server (another tab), so its stream announces it: a harness write stays in the test process.
        command(lead_page, 'trainee_note.create', {'person_id': trainee, 'body': 'Fictional progress note'})
        page.wait_for_function("""since => window.livePulses.slice(since).some(p => (p.changes || []).some(c => c.scope === 'people'))""",
                               arg=since, timeout=15000)
        scopes = {c['scope'] for p in page.evaluate('since => window.livePulses.slice(since)', since) for c in p.get('changes') or []}
        assert 'people' in scopes and not scopes & home_live(), scopes  # nothing Home shows: Home itself reads nothing
        page.clock.run_for(400)  # the pushed pulse's burst window (app.ts queuePulse)
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'home')
        assert page.evaluate(FROM_MEMORY, KEPT) is False  # asked again (conditional when it has an ETag)
        assert page.evaluate(FROM_MEMORY, KEPT) is True  # and kept again
    finally:
        lead_context.close()
        context.close()
        planner.context.close()


def test_a_held_pulse_forgets_kept_replies_when_it_is_applied(browser, live_server, h):
    planner, context, page = home_with_a_kept_reply(browser, live_server)
    try:
        page.keyboard.press('Control+k')
        menu = page.get_by_role('dialog', name='Command menu')
        expect(menu).to_be_visible()
        someone_adds_a_person(planner, page, 'Held pulse person')
        assert page.evaluate(FROM_MEMORY, KEPT) is True  # held under the open dialog: nothing forgotten yet
        page.keyboard.press('Escape')
        expect(menu).to_be_hidden()
        page.clock.run_for(11_000)  # the 10 s tick applies the held pulse
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'home')
        assert page.evaluate(FROM_MEMORY, KEPT) is False
    finally:
        context.close()
        planner.context.close()
