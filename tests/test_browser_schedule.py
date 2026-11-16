"""The one Schedule page (docs/design/schedule-unification.md §3–§5, §8, §14): its seven views over one continuous
timeline, the layer modes, staff's published-only view, the old routes that land on it, every role on desktop and phone
in light and dark, and its states (empty, filtered to nothing, slow, failing, read-only, department scale).
Real Chromium, real API, migrated database; data is set up through the API (or inserted directly for scale)."""
from packages.domain.time import now as clock_now
from packages.contracts.schemas import ROLE_NAMES
import re
import time
from datetime import date
from random import Random
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect

from .governance_support import identity
from .test_browser import browser, live_server, sign_in, toolbar, close_popover, wait_held, view_settings, choose_radio, choose_layout  # noqa: F401

pytestmark = pytest.mark.browser


# ---- helpers shared with test_browser_schedule_flows.py ----
def at(day, start='08', end='12', month=10):
    """One session's start and end on a day of October 2026 (Riyadh time)."""
    return f'2026-{month:02}-{day:02}T{start}:00:00+03:00', f'2026-{month:02}-{day:02}T{end}:00:00+03:00'


def checked(h, w):
    """Store a current rota check, so the page does not re-check (and redraw) while a test reads it."""
    w = h.detail(w)['workspace']
    return h.lifecycle(w, 'validate')['workspace']


def october(h):
    """A published October rota, 4–13 Oct (Alex on 4 Oct; Alex and Jordan on 12 Oct), and a checked draft 14–20 Oct
    (Alex on 14 Oct, nobody on 15 Oct). Returns (publication id, draft)."""
    a, b, _ = h.seed['people']
    w = h.shift(h.workspace('October rota', '2026-10-04', '2026-10-14'), *at(4), people=[a])['workspace']
    w = h.shift(w, *at(12), people=[a, b])['workspace']
    pid = h.publish(w)['publication_id']
    d = h.shift(h.workspace('Mid-October draft', '2026-10-14', '2026-10-21'), *at(14), people=[a])['workspace']
    d = h.shift(d, *at(15), people=[])['workspace']
    return pid, checked(h, d)


def overlapping(h):
    """A draft over published days, 11–17 Oct: the 12 Oct session with Alex only (changed), a new 13:00 session with
    Jordan, and Alex on 16 Oct."""
    a, b, _ = h.seed['people']
    d = h.shift(h.workspace('Overlap draft', '2026-10-11', '2026-10-18'), *at(12), people=[a])['workspace']
    d = h.shift(d, *at(12, '13', '17'), people=[b])['workspace']
    d = h.shift(d, *at(16), people=[a])['workspace']
    return checked(h, d)


def watch(page):
    """Script errors, console errors and refused requests of a page."""
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('console', lambda m: errors.append(m.text) if m.type == 'error' and not m.text.startswith('Failed to load resource') else None)
    page.on('response', lambda r: errors.append(f'{r.status} {r.url}') if r.status >= 400 and '/api/' in r.url else None)
    return errors


def settled(page):
    # Generous: a loaded CI host (or several browser suites at once) can take many seconds per render.
    expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule', timeout=45000)
    expect(page.locator('#schedule-view .loading')).to_have_count(0, timeout=45000)


def show(page, route):
    """Open a Schedule address and wait for that render (an address already shown is not rendered again)."""
    page.evaluate("r => {if (location.hash !== '#' + r) {document.querySelector('#main').removeAttribute('data-loaded-view'); location.hash = r;}}", route)
    settled(page)
    # A redraw already under way (e.g. live coverage arriving) can finish first: wait for the view this address names.
    parts = route.split('?')[0].split('/')
    if parts[0] == 'schedule' and len(parts) > 1 and parts[1] in VIEWS:
        expect(view_button(page)).to_have_accessible_name(re.compile(f'^View settings: {parts[1].capitalize()}'), timeout=45000)
        settled(page)


VIEWS = ('year', 'month', 'week', 'day', 'people', 'coverage', 'list')


def view_button(page):
    return page.get_by_role('button', name=re.compile(r'^View settings: '))


def hash_of(page):
    return page.evaluate('location.hash')


def today():
    return clock_now().astimezone(ZoneInfo('Asia/Riyadh')).date().isoformat()


# ---- 1. Views ----
def test_year_dots_show_state_and_the_day_panel_opens_week_at_that_date(browser, live_server, h):
    october(h)
    d2 = overlapping(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    errors = watch(page)
    try:
        show(page, f"schedule/year/2026-10-12?edit={d2['id']}")
        expect(view_button(page)).to_have_accessible_name('View settings: Year')
        year = page.get_by_role('region', name='Year view')
        published = year.get_by_role('button', name='4 Oct 2026: October rota, published, 1 session', exact=True)
        expect(published.locator('.sd-dot')).to_have_class(re.compile(r'^sd-dot\s*$'))  # solid: published
        draft_only = year.get_by_role('button', name=re.compile(r'^16 Oct 2026: Overlap draft, draft, 1 session'))
        expect(draft_only.locator('.sd-dot')).to_have_class(re.compile(r'\bdraft\b'))  # hollow: draft
        both = year.get_by_role('button', name=re.compile(r'^12 Oct 2026: October rota, published, 1 session.*; Overlap draft, draft, 2 sessions'))
        expect(both.locator('.sd-dot')).to_have_class(re.compile(r'\bboth\b'))  # half: two rotas on one day
        expect(year.get_by_role('button', name='13 Oct 2026: not planned', exact=True)).to_be_visible()
        expect(year.get_by_role('region', name='October').locator('.sd-flag')).to_have_text('Draft starts 11')
        key = page.get_by_role('list', name='Published and draft days')  # Year's key is labelled (SR-08 wave)
        for item in ('Published', 'Draft', 'Two rotas on one day'):
            expect(key.get_by_role('listitem').filter(has_text=re.compile(f'^{item}$'))).to_have_count(1)
        # One tab stop; arrows move by day; the panel follows the chosen day.
        draft_only.click()
        expect(draft_only).to_have_attribute('aria-pressed', 'true')
        page.keyboard.press('ArrowLeft')
        expect(year.get_by_role('button', name=re.compile(r'^15 Oct 2026'))).to_be_focused()
        page.keyboard.press('ArrowRight')
        panel = page.locator('.sd-panel')
        expect(panel.get_by_role('heading', level=2)).to_have_text('Fri 16 Oct 2026')
        expect(panel).to_contain_text('Overlap draft · Draft')
        panel.get_by_role('button', name='Open in Week').click()
        settled(page)
        expect(view_button(page)).to_have_accessible_name('View settings: Week')
        assert hash_of(page) == f"#schedule/week/2026-10-16?edit={d2['id']}"
        expect(page.get_by_role('region', name='Week view').get_by_role('columnheader', name=re.compile(r'^16 Oct 2026'))).to_be_visible()
        assert not errors, errors
    finally:
        context.close()


def test_month_bands_show_published_days_open_places_and_where_the_draft_starts(browser, live_server, h):
    _, d = october(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    errors = watch(page)
    try:
        show(page, f"schedule/month/2026-10-15?edit={d['id']}")
        month = page.get_by_role('region', name='Month view').locator('.wk-grid.mo')
        # The week grid once per week: published days around the draft are grey and read-only.
        expect(month.get_by_role('gridcell', name=re.compile(r'^4 Oct 2026 · ')).locator('.sd-greychip')).to_have_count(1)
        expect(month.get_by_role('columnheader', name=re.compile(r'^4 Oct 2026 · published, read-only'))).to_be_visible()
        # The day with an open place is marked short; the draft's first day carries the edge.
        expect(month.get_by_role('columnheader', name=re.compile(r'^15 Oct 2026 · '))).to_have_class(re.compile(r'\bshort\b'))
        expect(month.get_by_role('columnheader', name=re.compile(r'^14 Oct 2026 · .*the draft starts'))).to_have_class(re.compile(r'\bsd-edge\b'))
        expect(month.get_by_role('gridcell', name=re.compile(r'^15 Oct 2026 · ')).locator('.pchip.gap')).to_have_count(1)
        assert not errors, errors
    finally:
        context.close()


def test_week_shows_published_days_grey_and_read_only_around_the_draft(browser, live_server, h):
    _, d = october(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    errors = watch(page)
    try:
        show(page, f"schedule/week/2026-10-14?edit={d['id']}")
        grid = page.get_by_role('region', name='Week view').get_by_role('grid')
        for day in (11, 12, 13):
            head = grid.get_by_role('columnheader', name=f'{day} Oct 2026 · published, read-only', exact=True)
            expect(head).not_to_contain_text('Published')  # grey and named, not a word on each day (S-16)
            expect(head).to_have_class(re.compile(r'\bsd-ctx\b'))
        start = grid.get_by_role('columnheader', name=re.compile(r'^14 Oct 2026 · .* · the draft starts$'))
        expect(start).to_have_class(re.compile(r'\bsd-edge\b'))  # the day the draft starts (grey context ends here)
        expect(grid.get_by_role('columnheader', name=re.compile(r'^15 Oct 2026')).locator('.wk-cov')).not_to_have_text('Published')
        chip = grid.get_by_role('button', name=re.compile(r'^Jordan Lee, Clinical review session, .+, 12 Oct 2026 08:00.12:00, published, read-only$'))
        expect(chip).to_have_class(re.compile(r'\bsd-greychip\b'))
        expect(page.get_by_role('list', name='Key')).to_contain_text('Published, read-only')
        # A published chip opens the session read-only in a side panel (S-22), never the planner's editing panel.
        chip.click()
        dialog = page.locator('.dock-panel')
        expect(dialog.get_by_role('heading', name='Mon 12 Oct · Clinical review session', exact=True)).to_be_visible()
        expect(dialog).to_contain_text('Published')
        expect(dialog.get_by_role('button', name=re.compile('^(Save|Edit session|Add participant|Remove)'))).to_have_count(0)
        expect(page.locator('.wk-panel')).to_have_count(0)
        expect(page.get_by_role('dialog', name='Clinical review session')).to_have_count(0)  # not a modal
        dialog.get_by_role('button', name='Close panel').click()
        expect(dialog).to_have_count(0)
        # The draft's own chip on 14 Oct is editable (not grey).
        own = grid.get_by_role('button', name=re.compile(r'^Alex Morgan, Clinical review session, .+, 14 Oct 2026'))
        expect(own).not_to_have_class(re.compile(r'\bsd-greychip\b'))
        assert not errors, errors
    finally:
        context.close()


def test_day_overlay_shows_published_and_draft_with_changed_and_new(browser, live_server, h):
    october(h)
    d2 = overlapping(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    errors = watch(page)
    try:
        # Edit one: on the draft's days its sessions replace the published ones.
        show(page, f"schedule/day/2026-10-12?edit={d2['id']}")
        table = page.get_by_role('region', name='Day view').get_by_role('table', name=re.compile('^Sessions on 12 Oct 2026'))
        expect(table.get_by_role('button', name=re.compile('^Draft, '))).to_have_count(2)
        expect(table.get_by_role('button', name=re.compile('^Published, '))).to_have_count(0)
        # Overlay: both layers, with what the draft changes.
        show(page, f"schedule/day/2026-10-12?edit={d2['id']}&mode=overlay")
        expect(view_button(page)).to_have_accessible_name('View settings: Day · Overlay')
        expect(table.get_by_role('button', name=re.compile(r'^Published, Clinical review session, 08:00 to 12:00, .*Jordan Lee'))).to_have_count(1)
        changed = table.get_by_role('button', name=re.compile(r'^Draft, Clinical review session, 08:00 to 12:00, .*, changed$'))
        expect(changed.get_by_text('Changed', exact=True)).to_be_visible()
        new = table.get_by_role('button', name=re.compile(r'^Draft, Clinical review session, 13:00 to 17:00, .*, new$'))
        expect(new.get_by_text('New', exact=True)).to_be_visible()
        expect(page.locator('footer')).to_contain_text('2 changes from published')
        assert not errors, errors
    finally:
        context.close()


def test_view_picker_and_keys_open_every_view_on_the_same_day(browser, live_server, h):
    _, d = october(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    errors = watch(page)
    try:
        show(page, f"schedule/week/2026-10-15?edit={d['id']}")
        region = {'People': ('grid', 'Duties by person and day'), 'Coverage': ('table', 'Coverage by duty type and day'),
                  'List': ('table', None), 'Year': ('region', 'October'), 'Month': ('grid', re.compile('^Sessions grouped by')),
                  'Day': ('table', re.compile('^Sessions on 15 Oct 2026')), 'Week': ('grid', re.compile('^Sessions grouped by'))}
        for letter, name in (('p', 'People'), ('c', 'Coverage'), ('l', 'List'), ('y', 'Year'), ('m', 'Month'), ('d', 'Day'), ('w', 'Week')):
            page.locator('body').press(letter)
            expect(view_button(page)).to_have_accessible_name(f'View settings: {name}')
            settled(page)
            role, label = region[name]
            view = page.get_by_role('region', name=f'{name} view')
            expect(view.get_by_role(role, name=label).first if label else view.get_by_role(role).first).to_be_visible()
            assert re.match(rf"#schedule/{name.lower()}/2026-10-1\d\?edit={d['id']}$", hash_of(page)), hash_of(page)
        expect(page.get_by_role('region', name='Week view').get_by_role('table').or_(
            page.get_by_role('region', name='Week view').get_by_role('grid'))).to_be_visible()
        # The List view lists the draft's sessions; View settings (layout and range) is the same choice as the keys.
        panel = view_settings(page)
        choose_layout(panel, 'List')
        choose_radio(panel, 'Range', 'Month')
        settled(page)
        expect(view_button(page)).to_have_accessible_name('View settings: List')
        rows = page.get_by_role('region', name='List view').get_by_role('row').filter(has_text='Clinical review session')
        expect(rows).to_have_count(2)
        panel = view_settings(page)
        expect(panel.get_by_role('radiogroup', name='Layout').get_by_role('radio', name='List')).to_have_attribute('aria-checked', 'true')
        close_popover(page)
        assert not errors, errors
    finally:
        context.close()


def test_days_outside_the_draft_say_published_only_when_a_publication_is_in_force(browser, live_server, h):
    """Nothing published before or after a draft: its neighbouring days are the same out-of-range grey as published ones,
    with no day numbers and the key's "Outside this rota" (QA-055), and never labelled "Published"."""
    d = checked(h, h.shift(h.workspace('Lone draft', '2026-10-14', '2026-10-16'), *at(14))['workspace'])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        for route in ('week', 'month'):
            show(page, f"schedule/{route}/2026-10-14?edit={d['id']}")
            head = page.locator('#schedule-view').get_by_role('columnheader', name='12 Oct 2026 · outside this rota', exact=True)
            expect(head).to_have_class(re.compile(r'\bsd-ctx\b'))
            expect(head.locator('.wk-cov')).to_have_count(0)
            expect(page.get_by_role('list', name='Key')).to_contain_text('Outside this rota')
            expect(page.locator('#schedule-view .wk-cov', has_text='Published')).to_have_count(0)
            expect(page.get_by_role('list', name='Key')).not_to_contain_text('Published')
            # A day with nothing planned has an empty coverage bar, not a full green one.
            assert head.locator('.wk-bar>span').evaluate('n => n.style.width') == '0%'
    finally:
        context.close()


def test_duty_types_get_distinct_colours_the_same_for_planners_and_staff(browser, live_server, h):
    """Four duty types without a chosen colour get four different hues (one SSOT assignment), and staff, who read no
    catalogues, see each duty in the same hue as the coordinator."""
    a = h.seed['people'][0]
    names = ('Distinguished Clinic', 'Urgent Care Walk-in', 'Teleconsultation', 'Otorhinolaryngology')
    w = h.workspace('Colourful week', '2026-10-11', '2026-10-18')
    for i, name in enumerate(names):
        typ = h.configure('assignment_types', {'name': name, 'starts_on': '2020-01-01', 'definition': {}})['record']['id']
        w = h.shift(w, *at(12 + i), people=[a], typ=typ)['workspace']
    h.publish(w)

    def hues(role, viewport=None):
        context, page = sign_in(browser, live_server, role, viewport)
        try:
            # Month: Week's rows already name each duty type, so its key does not repeat them (SI-36).
            show(page, 'schedule/month/2026-10-12?edit=none')
            key = page.get_by_role('list', name='Key')
            expect(key.locator('li', has_text='Teleconsultation')).to_be_visible()
            return {n: key.locator('li', has_text=n).locator('.lg-swatch').evaluate("s => [...s.classList].find(c => c.startsWith('duty-'))") for n in names}
        finally:
            context.close()
    planner = hues('coordinator')
    assert len(set(planner.values())) == 4, planner
    assert hues('staff') == planner
    # The light list every role gets says the same (no catalogue needed).
    assert {t['id'] for t in h.get('staff', '/api/me')['duty_types']} >= {t['id'] for t in h.get('admin', '/api/catalogues')['assignment_types']}


def test_period_moves_across_rota_boundaries_and_today_returns(browser, live_server, h):
    _, d = october(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    errors = watch(page)
    try:
        show(page, f"schedule/week/2026-10-14?edit={d['id']}")
        label = page.get_by_role('group', name='Schedule').locator('strong').first
        expect(label).to_have_text('Sun 11 Oct – Sat 17 Oct')
        page.get_by_role('button', name='Next week', exact=True).click()
        expect(label).to_have_text('Sun 18 Oct – Sat 24 Oct')
        settled(page)
        assert hash_of(page).startswith('#schedule/week/2026-10-21?')
        for _ in range(2):
            page.get_by_role('button', name='Previous week', exact=True).click()
            settled(page)
        expect(label).to_have_text('Sun 4 Oct – Sat 10 Oct')
        # A week before the draft: all published, read-only, with the published sessions as grey chips.
        grid = page.get_by_role('region', name='Week view').get_by_role('grid')
        expect(grid.get_by_role('columnheader', name=re.compile('published, read-only$'))).to_have_count(7)
        expect(grid.get_by_role('button', name=re.compile(r'^Alex Morgan, Clinical review session, .+, 4 Oct 2026 .*published, read-only$'))).to_be_visible()
        page.get_by_role('button', name='Go to today', exact=True).click()
        settled(page)
        assert today() in hash_of(page), hash_of(page)
        expect(page.get_by_role('button', name='Go to today', exact=True)).to_be_disabled()
        assert not errors, errors
    finally:
        context.close()


def test_moving_back_to_a_week_already_seen_reads_nothing_again(browser, live_server, h):
    _, d = october(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    errors = watch(page)
    try:
        show(page, f"schedule/week/2026-10-14?edit={d['id']}")
        page.get_by_role('button', name='Next week', exact=True).click()
        expect(page.get_by_role('group', name='Schedule').locator('strong').first).to_have_text('Sun 18 Oct – Sat 24 Oct')
        settled(page)
        reads = []
        page.on('request', lambda r: reads.append(r.url) if '/api/' in r.url and '/api/events' not in r.url else None)
        page.get_by_role('button', name='Previous week', exact=True).click()
        expect(page.get_by_role('group', name='Schedule').locator('strong').first).to_have_text('Sun 11 Oct – Sat 17 Oct')
        settled(page)
        expect(page.get_by_role('button', name=re.compile(r'^Jordan Lee, .*12 Oct 2026.*published, read-only$'))).to_be_visible()
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')  # the redraw and anything it started have run
        assert reads == [], reads  # the draft and the published window are kept for the visit
        assert not errors, errors
    finally:
        context.close()


# ---- 2. Layers ----
def test_layer_modes_side_by_side_overlay_leave_and_no_draft(browser, live_server, h):
    _, d = october(h)
    d2 = overlapping(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    errors = watch(page)
    try:
        show(page, f"schedule/week/2026-10-15?edit={d['id']}")

        def mode(name):
            view_settings(page, 'Layers').get_by_role('radiogroup', name='Show drafts').get_by_role('radio', name=name, exact=True).click()
            settled(page)

        mode('Side by side')
        expect(view_button(page)).to_have_accessible_name('View settings: Week · Side by side')
        assert '&mode=side' in hash_of(page)
        compare = page.locator('#schedule-view').get_by_label('Compare with', exact=True)
        expect(compare).to_be_visible()
        compare.select_option(d2['id'])
        expect(page.locator('#schedule-view')).to_contain_text('Overlap draft')
        expect(page.get_by_role('button', name='Add session', exact=True)).to_have_count(0)  # no primary while comparing
        mode('Overlay')
        expect(view_button(page)).to_have_accessible_name('View settings: Week · Overlay')
        assert '&mode=overlay' in hash_of(page)
        mode('Edit one')
        expect(view_button(page)).to_have_accessible_name('View settings: Week')
        assert 'mode=' not in hash_of(page)
        # Leave and wishes is People's layer (D80): the Week draws none, so its Layers do not offer it.
        expect(view_settings(page, 'Layers').get_by_label('Leave and wishes', exact=True)).to_have_count(0)
        # No draft: the published layer only; New draft becomes the primary.
        view_settings(page, 'Layers').get_by_role('radio', name=re.compile('^No draft')).check()
        settled(page)
        close_popover(page)
        assert hash_of(page).endswith('?edit=none'), hash_of(page)
        expect(page.locator('.topbar h1')).to_have_text('Schedule')
        expect(page.get_by_role('button', name='New rota', exact=True)).to_be_visible()
        page.get_by_role('button', name='Choose a draft').click()
        menu = page.get_by_role('listbox', name='Drafts')  # the shared picker
        expect(menu.get_by_role('option', name=re.compile('^Published only'))).to_have_attribute('aria-selected', 'true')
        menu.get_by_role('option', name=re.compile('^Mid-October draft')).click()
        settled(page)
        expect(page.locator('.topbar h1')).to_have_text('Mid-October draft')
        assert not errors, errors
    finally:
        context.close()


# ---- 5. Staff ----
@pytest.mark.parametrize('phone', [False, True], ids=['desktop', 'phone'])
def test_staff_see_their_published_duties_only_on_the_same_page(browser, live_server, h, phone):
    a, b, _ = h.seed['people']
    w = h.shift(h.workspace('October rota', '2026-10-04', '2026-10-14'), *at(4), people=[a])['workspace']
    w = h.shift(w, *at(6), people=[b])['workspace']
    h.publish(w)
    d = h.shift(h.workspace('Secret draft', '2026-10-14', '2026-10-21'), *at(15), people=[a])['workspace']
    context, page = sign_in(browser, live_server, 'staff', {'width': 390, 'height': 844} if phone else None)
    errors = watch(page)
    try:
        show(page, 'schedule/month/2026-10-04')
        expect(page.locator('.topbar h1')).to_have_text('Schedule')
        expect(page.get_by_role('button', name=re.compile('^(Switch draft|Choose a draft)'))).to_have_count(0)
        # QA-037: staff only ever read their own duties, so no My duties look-alike toggle that does nothing.
        expect(page.locator('#main .planner-toolbar')).not_to_contain_text('My duties')
        for absent in ('Add session', 'New rota', 'Publish…', 'Filter'):
            expect(page.get_by_role('button', name=absent, exact=True)).to_have_count(0)
        month = page.get_by_role('region', name='Month view')
        if phone:  # the month as a day list
            mine = month.get_by_role('region', name=re.compile('^4 Oct 2026')).get_by_role('button', name=re.compile('Clinical review session'))
            # 6 Oct (a colleague's duty) and 15 Oct (a draft) are empty for staff: one folded run of empty days.
            expect(month.locator('.ag-gap').filter(has_text='Mon 5 Oct – Sat 31 Oct')).to_contain_text('27 days without sessions')
        else:  # the week grid once per week
            mine = month.get_by_role('gridcell', name=re.compile(r'^4 Oct 2026 · ')).locator('.pchip')
            expect(mine).to_have_count(1)
            expect(month.get_by_role('gridcell', name=re.compile(r'^6 Oct 2026 · ')).locator('.pchip')).to_have_count(0)
            expect(month.get_by_role('gridcell', name=re.compile(r'^15 Oct 2026 · ')).locator('.pchip')).to_have_count(0)
        mine.click()
        expect(page.locator('.dock-panel')).to_contain_text('Published')  # docked, read-only (S-22)
        page.locator('.dock-panel').get_by_role('button', name='Close panel').click()
        # Planning addresses are restricted for staff, even when a draft id is supplied directly (SW3-C-12).
        page.evaluate("r => { location.hash = '#' + r; }", f"schedule/week/2026-10-12?edit={d['id']}")
        expect(page.locator('#main[data-loaded-view=restricted]')).to_contain_text('Restricted page')
        expect(page.get_by_text('Secret draft')).to_have_count(0)
        errors[:] = [e for e in errors if not e.startswith('403 ') or f"/api/workspaces/{d['id']}" not in e]  # the refused draft read
        expect(page.get_by_role('button', name=re.compile('Jordan Lee'))).to_have_count(0)
        if phone:
            expect(page.get_by_role('button', name='Menu', exact=True)).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 2')
        else:
            expect(page.get_by_role('button', name='Menu', exact=True)).to_be_hidden()
        assert not errors, errors
    finally:
        context.close()


# ---- 6. Old routes ----
def test_old_routes_land_on_the_schedule_with_the_right_view(browser, live_server, h):
    _, d = october(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    errors = watch(page)
    try:
        cases = [('planner', 'Week', r'#schedule/week/[\d-]+\?edit=' + d['id']),
                 (f"planner/{d['id']}/person.rota", 'People', r'#schedule/people/[\d-]+\?edit=' + d['id']),
                 (f"planner/{d['id']}/week.month", 'Month', r'#schedule/month/[\d-]+\?edit=' + d['id']),
                 ('timeline', 'Year', rf'#schedule/year/{today()}(\?|$)'),
                 ('calendar', 'People', rf'#schedule/people/{today()}(\?|$)'),
                 ('published', 'Month', rf'#schedule/month/{today()}(\?|$)')]
        for old, name, address in cases:
            show(page, old)
            expect(view_button(page)).to_have_accessible_name(f'View settings: {name}')
            assert re.match(address, hash_of(page)), (old, hash_of(page))
            show(page, 'schedule/list/2026-01-01')  # move away, so the next old address is a change
        assert not errors, errors
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'staff')
    try:
        show(page, 'published')
        expect(view_button(page)).to_have_accessible_name('View settings: Week')
        assert hash_of(page).startswith(f'#schedule/week/{today()}')
    finally:
        context.close()


# ---- 7. Every role ----
@pytest.mark.parametrize('role', ROLE_NAMES)
@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_every_role_opens_the_schedule_on_desktop_and_phone_in_light_and_dark(browser, live_server, h, role, scheme):
    if role == 'programme_lead':
        identity(h, 'programme_lead', ['programme_lead'])
    _, d = october(h)
    for viewport in ({'width': 1440, 'height': 1000}, {'width': 390, 'height': 844}):
        context, page = sign_in(browser, live_server, role, viewport)
        errors = watch(page)
        try:
            page.emulate_media(color_scheme=scheme)
            nav = page.locator('#department-nav [data-route=schedule]')
            if role in ('credentialer', 'operator'):
                expect(nav).to_have_count(0)
                page.evaluate("() => {location.hash = 'schedule'}")
                expect(page.get_by_role('heading', name='Restricted page')).to_be_visible()
                assert not errors, (role, scheme, errors)
                continue
            for route, name in (('schedule/week/2026-10-12', 'Week'), ('schedule/year/2026-10-12', 'Year'), ('schedule/people/2026-10-12', 'People')):
                show(page, route)
                expect(view_button(page)).to_have_accessible_name(re.compile(f'^View settings: {name}'))
                expect(page.locator('#main [role=alert]')).to_have_count(0)
            expect(nav).to_have_attribute('aria-current', 'page')
            # Only coordinators change a draft; every other role reads it.
            expect(page.get_by_role('button', name='Add session', exact=True)).to_have_count(1 if role in ('coordinator', 'admin') else 0)
            if role != 'staff':
                expect(page.locator('.topbar h1')).to_have_text('Mid-October draft')
            if viewport['width'] == 390:
                expect(page.get_by_role('button', name='Menu', exact=True)).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 2')
            assert page.evaluate("getComputedStyle(document.body).backgroundColor") != ''
            assert not errors, (role, scheme, errors)
        finally:
            context.close()


# ---- 8. States ----
def test_an_empty_department_offers_new_draft_and_empty_views(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator')
    errors = watch(page)
    try:
        show(page, 'schedule/week/2026-10-12')
        expect(page.locator('.planner-toolbar').get_by_role('button', name='New rota', exact=True)).to_have_count(0)
        expect(page.get_by_role('region', name='Week view').get_by_role('heading', name='No sessions this week')).to_be_visible()
        show(page, 'schedule/year/2026-10-12')
        year = page.get_by_role('region', name='Year view')
        expect(year.get_by_role('button', name=re.compile(': not planned'))).to_have_count(365)
        show(page, 'schedule/week/2026-10-12')
        page.get_by_role('region', name='Week view').get_by_role('button', name='New rota', exact=True).click()
        dialog = page.get_by_role('dialog', name='New rota')
        expect(dialog.get_by_label('Rota name', exact=True)).to_be_visible()
        # Nothing published: no overlap choice (the draft baseline's own choice, f9a3963f6, is always there: since
        # c68caff45 the department baseline by default, a custom scope under a closed Advanced).
        expect(dialog.get_by_text('Advanced', exact=True)).to_have_count(1)
        expect(dialog.get_by_role('radiogroup', name='How should the draft fit?', exact=True)).to_have_count(0)
        assert not errors, errors
    finally:
        context.close()


def test_a_search_matching_nothing_shows_the_empty_state(browser, live_server, h):
    _, d = october(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    errors = watch(page)
    try:
        show(page, f"schedule/list/2026-10-15?edit={d['id']}")
        expect(page.get_by_role('region', name='List view').get_by_role('row').filter(has_text='Clinical review session')).to_have_count(2)
        page.get_by_role('searchbox', name='Search sessions').fill('nothing-matches-this')
        expect(page.get_by_role('region', name='List view').get_by_role('heading', name='Hidden by filters')).to_be_visible(timeout=10000)
        page.get_by_role('searchbox', name='Search sessions').fill('')
        expect(page.get_by_role('region', name='List view').get_by_role('row').filter(has_text='Clinical review session')).to_have_count(2, timeout=10000)
        assert not errors, errors
    finally:
        context.close()


def test_slow_published_sessions_keep_the_page_working(browser, live_server, h):
    _, d = october(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    errors = watch(page)
    try:
        show(page, f"schedule/year/2026-10-12?edit={d['id']}")

        held = []
        page.route('**/api/schedule/sessions*', lambda route: held.append(route))
        # 4 Oct: a week not read yet this visit (signing in showed the draft's week, 11–17 Oct).
        page.get_by_role('region', name='Year view').get_by_role('button', name=re.compile(r'^4 Oct 2026')).click()
        page.locator('.sd-panel').get_by_role('button', name='Open in Week').click()
        wait_held(page, held)
        # While the published sessions are still on their way, the page stays usable (its toolbar answers).
        expect(page.get_by_role('group', name='Schedule')).to_be_visible()
        for route in held:
            route.continue_()
        # The week arrives once the slow read answers, with the published days filled in.
        expect(view_button(page)).to_have_accessible_name('View settings: Week', timeout=20000)
        settled(page)
        expect(page.get_by_role('button', name=re.compile(r'^Alex Morgan, .*4 Oct 2026.*published, read-only$'))).to_be_visible()
        page.unroute('**/api/schedule/sessions*')
        page.locator('body').press('m')
        expect(view_button(page)).to_have_accessible_name('View settings: Month')
        settled(page)
        assert not errors, errors
    finally:
        context.close()


def test_a_failing_day_summary_says_so_and_retry_recovers(browser, live_server, h):
    october(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    errors = watch(page)
    try:
        page.route('**/api/schedule/days*', lambda route: route.fulfill(status=500, content_type='application/json',
                                                                       body='{"detail":"Synthetic failure"}'))
        show(page, 'schedule/year/2026-10-12?edit=none')
        view = page.get_by_role('region', name='Year view')
        expect(view).to_contain_text('Couldn’t load the calendar.')
        retry = view.get_by_role('button', name='Retry', exact=True)
        expect(retry).to_be_visible()
        page.unroute('**/api/schedule/days*')
        retry.click()
        settled(page)
        expect(page.get_by_role('region', name='Year view').get_by_role('button', name='4 Oct 2026: October rota, published, 1 session', exact=True)).to_be_visible()
        expect(page.get_by_role('region', name='Year view')).not_to_contain_text('Couldn’t load')
        assert all('500' in e for e in errors), errors  # only the refused request itself
    finally:
        context.close()


def test_published_day_label_is_never_cut_and_the_people_key_uses_the_real_code(browser, live_server, h):
    """With the Auto-schedule panel open the day columns narrow: "Published" is dropped rather than cut ("Publishe"); the
    grey day and the key still say it. The People key shows a published chip with its real duty code (not "DS")."""
    from .test_browser import open_scheduling
    _, d = october(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        show(page, f"schedule/week/2026-10-12?edit={d['id']}")
        open_scheduling(page)
        grid = page.get_by_role('region', name='Week view')
        head = grid.get_by_role('columnheader', name='12 Oct 2026 · published, read-only', exact=True)
        expect(head).to_be_visible()
        assert grid.locator('.wk-cov').evaluate_all('cs => cs.filter(c => c.getClientRects().length && c.getBoundingClientRect().right > c.parentElement.getBoundingClientRect().right + 1).length') == 0
        show(page, f"schedule/people/2026-10-12?edit={d['id']}")
        key = page.get_by_role('list', name='Key')
        expect(key.locator('li', has_text='Published, read-only').locator('.sd-greychip')).to_have_text('CRS')
    finally:
        context.close()


@pytest.mark.parametrize('view', ['week', 'people'])
def test_failing_published_sessions_say_so_in_the_view_and_retry_recovers(browser, live_server, h, view):
    """Without a draft the published sessions are the whole view: a failed read says so there, with Retry, instead of an
    empty "No sessions this week" (and a toast per redraw)."""
    october(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.route('**/api/schedule/sessions*', lambda route: route.fulfill(status=500, content_type='application/json',
                                                                           body='{"detail":"Synthetic failure"}'))
        show(page, f'schedule/{view}/2026-10-12?edit=none')
        region = page.locator('#schedule-view')
        expect(region).to_contain_text('Couldn’t load the published sessions.')
        expect(region).not_to_contain_text('No sessions this week')
        expect(page.locator('#toast-tray .toast')).to_have_count(0)
        page.unroute('**/api/schedule/sessions*')
        region.get_by_role('button', name='Retry', exact=True).click()
        settled(page)
        expect(page.locator('#schedule-view')).not_to_contain_text('Couldn’t load')
        expect(page.locator('#schedule-view').locator('.pchip, .pg-chip').first).to_be_visible()  # the published sessions are back
    finally:
        context.close()


def _department_scale(h):
    """150 people in the department and a published October with ~500 sessions (16 a day), inserted directly."""
    from sqlalchemy import select
    from apps.api import models as m
    from apps.api.db import transaction
    from apps.api.snapshots import workspace_shifts
    from packages.domain.validation import evaluate
    from .test_schedule_window import _big_month
    w = h.workspace('Big October', '2026-10-01', '2026-11-01')
    contracts = evaluate(h.snapshot(w))['contracts']
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, w['id'])
        org_id = db.get(m.Department, row.department_id).organisation_id
        category = db.scalar(select(m.Membership.category_id).where(m.Membership.person_id == h.seed['people'][0]))
        extra = [m.Person(organisation_id=org_id, display_name=f'Synthetic person {i:03}') for i in range(147)]
        db.add_all(extra)
        db.flush()
        db.add_all([m.Membership(person_id=p.id, category_id=category, department_id=row.department_id, starts_on=date(2020, 1, 1)) for p in extra])
        people = list(h.seed['people']) + [p.id for p in extra]
        typ = db.get(m.AssignmentType, h.seed['type'])
        check = _big_month(db, row.id, people, typ, h.seed['locations'], h.seed['services'], date(2026, 10, 1), Random(7), '2026-09-30T00:00:00+00:00')
        db.flush()
        row.status = 'published'
        # The sessions in the snapshot too: the Team calendar read (People view's academic band) reads snapshots.
        pub = m.Publication(workspace_id=row.id, department_id=row.department_id, sequence=1, starts_on=row.starts_on, ends_on=row.ends_on,
                            current=True, published_by=row.created_by, snapshot={'shifts': workspace_shifts(db, row.id)},
                            validation={**check, 'contracts': contracts}, fingerprint='0' * 64)
        db.add(pub)
        db.flush()
        on = {a.person_id for a in db.scalars(select(m.Assignment).join(m.Shift).where(m.Shift.workspace_id == row.id))}
        db.add_all([m.PublicationPerson(publication_id=pub.id, person_id=p) for p in on])
    return len(people)


@pytest.fixture
def department_scale_schedule_views(browser, live_server, h):
    count = _department_scale(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    errors = watch(page)
    try:
        timings = {}
        for name, route, ready in (
                ('Week', 'schedule/week/2026-10-14?edit=none', lambda: expect(page.get_by_role('region', name='Week view').get_by_role('grid')).to_be_visible(timeout=30000)),
                # Month draws a month of sessions (about 500 at department scale) as week bands.
                ('Month', 'schedule/month/2026-10-14?edit=none', lambda: expect(page.get_by_role('region', name='Month view').locator('.wk-grid.mo .mo-wk')).not_to_have_count(0, timeout=30000)),
                ('People', 'schedule/people/2026-10-14?edit=none', lambda: expect(page.get_by_role('columnheader', name=f'{count} people', exact=True)).to_be_visible(timeout=30000)),
                ('Year', 'schedule/year/2026-10-14?edit=none', lambda: expect(page.get_by_role('button', name=re.compile(r'^14 Oct 2026: Big October, published, 16 sessions'))).to_be_visible(timeout=30000))):
            began = time.perf_counter()
            show(page, route)
            ready()
            timings[name] = time.perf_counter() - began
            expect(view_button(page)).to_have_accessible_name(f'View settings: {name}')
        print('schedule page timings: ' + ', '.join(f'{k} {v * 1000:.0f} ms' for k, v in timings.items()))
        yield page, count, timings, errors
    finally:
        context.close()


def test_department_scale_week_month_people_and_year_open_quickly(department_scale_schedule_views):
    page, count, _, errors = department_scale_schedule_views
    # Every person in the department has a row in the People view (or is reachable by scrolling it).
    show(page, 'schedule/people/2026-10-21?edit=none')
    grid = page.get_by_role('region', name='People view').get_by_role('grid', name='Duties by person and day')
    expect(grid.locator('.pg-row[data-row-person] > [role=rowheader]')).to_have_count(count, timeout=30000)
    expect(grid.get_by_role('button', name=re.compile(r'Clinical review session · .*21 Oct 2026'))).not_to_have_count(0)
    assert not errors, errors


def test_department_scale_month_builds_only_the_chips_it_shows(department_scale_schedule_views):
    """Perf SC-03 / PL-06: a busy Month builds the 2-3 chips a cell shows and counts the rest for "+N more" (it built
    every chip of the month, ~1,900, to show ~180); the count stays exact."""
    page, _, _, errors = department_scale_schedule_views
    show(page, 'schedule/month/2026-10-14?edit=none')
    view = page.get_by_role('region', name='Month view')
    cells = view.locator('.wk-cell:not(.mo-out)')
    expect(cells.first).to_be_visible()
    chips = view.locator('.wk-cell .pchip:not(.more)').count()
    assert chips <= 3 * cells.count(), (chips, cells.count())
    more = view.locator('.wk-cell button.pchip.more').first
    cell = more.locator('xpath=..')
    hidden = int(re.search(r'\+(\d+)', more.inner_text()).group(1))
    shown = cell.locator('.pchip:not(.more)').count()
    more.click()
    expect(page.locator('.mo-more .pchip')).to_have_count(hidden + shown)
    assert not errors, errors


@pytest.mark.wall_benchmark
def test_department_scale_month_opens_without_a_long_task_cpu_budget(department_scale_schedule_views):
    """Perf SC-03 / PL-06: Week → Month at department scale has no main-thread task over 200 ms (it was 225-387 ms,
    most of it building chips the cells never show)."""
    from .test_schedule_window import _budget
    page, _, _, _ = department_scale_schedule_views
    show(page, 'schedule/week/2026-10-14?edit=none')
    page.evaluate("""() => {window.__long = []; new PerformanceObserver(l => window.__long.push(...l.getEntries().map(e => e.duration)))
        .observe({type: 'longtask'});}""")
    show(page, 'schedule/month/2026-10-14?edit=none')
    longest = max(page.evaluate('window.__long') or [0])
    assert longest < _budget(0.2) * 1000, longest


@pytest.mark.wall_benchmark
def test_department_scale_schedule_views_cpu_budget(department_scale_schedule_views):
    from .test_schedule_window import _budget
    _, _, timings, _ = department_scale_schedule_views
    for name, elapsed in timings.items():
        assert elapsed < _budget(8.0), (name, elapsed)


@pytest.mark.parametrize('width', [1440, 1280, 1024, 390])
def test_the_toolbar_fits_its_space_with_the_auto_schedule_panel_open(browser, live_server, h, width):
    """With the Auto-schedule panel open (a third of the screen) the toolbar adapts: Filter, Group and Sort become named
    icons, then Add session, then the search takes its own row. No control is clipped, Add session stays whole and the
    search stays usable."""
    from .test_browser import open_scheduling
    checked(h, h.shift(h.workspace('Toolbar fit rota', '2026-10-11', '2026-10-18'), *at(12))['workspace'])
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900})
    try:
        show(page, 'schedule/week/2026-10-12')
        if width >= 1280:  # room to spare: Auto-schedule keeps its words (the label is not clipped)
            label = page.get_by_role('button', name='Auto-schedule', exact=True).locator('.label')
            assert label.evaluate('e => e.getBoundingClientRect().width') > 1
        if width > 720:
            open_scheduling(page)
        bar = page.locator('.planner-toolbar')
        expect(bar.get_by_role('button', name='Add session', exact=True)).to_be_visible()
        for name in ('Filter', 'Sort') if width > 720 else ('Filter',):  # the phone's day agenda has no sort
            expect(bar.get_by_role('button', name=name, exact=True)).to_be_visible()
        clipped = bar.evaluate("""t => {const b = t.getBoundingClientRect();
            return [...t.querySelectorAll('button, input')].filter(n => n.getClientRects().length).filter(n => {
                const r = n.getBoundingClientRect();
                return r.left < b.left - 1 || r.right > b.right + 1 || n.scrollWidth > n.clientWidth + 1;
            }).map(n => n.getAttribute('aria-label') || n.textContent)}""")
        assert clipped == [], clipped
        assert bar.locator('.dt-search-wrap').evaluate('n => n.getBoundingClientRect().width') >= 140
    finally:
        context.close()
