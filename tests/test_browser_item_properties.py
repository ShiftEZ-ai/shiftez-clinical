"""The layout × property matrix (owner, 7 Nov 2026: "while on the monthly view of people, the properties cannot be
configured! … the A, M … are not configurable"). One item renderer (apps/web/src/schedule_item.ts) draws a session or a
person's duty in every Schedule layout. For every layout that draws items, View settings › Properties offers exactly
what that layout draws (LAYOUTS pins it), and switching each offered property changes what the schedule shows. A new
layout or property that is offered but not drawn, or drawn but not offered, fails here. Fictional names only."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, view_settings, close_popover, pick_view  # noqa: F401
from .test_browser_qa_planner import settled
from .test_browser_export_tag_replacement import tag_people

from .test_browser import DRAFT_CHIP, draft_chip  # noqa: F401
pytestmark = pytest.mark.browser
DESKTOP, PHONE = {'width': 1440, 'height': 900}, {'width': 390, 'height': 844}
CARD = {'time', 'lead', 'overtime', 'supervisor', 'open', 'rule', 'code', 'location', 'hours', 'tags'}  # Week: service is its group
CHIP = {'code', 'time', 'location', 'service', 'duty', 'hours', 'lead', 'overtime', 'tags'}
# name, viewport, saved planner settings, the properties View settings offers there
LAYOUTS = [
    ('week', DESKTOP, {'mode': 'week', 'range': 'week'}, CARD),
    ('week-docked', DESKTOP, {'mode': 'week', 'range': 'week'}, CARD),  # beside the docked session panel (L7)
    ('month', DESKTOP, {'mode': 'week', 'range': 'month'}, CARD | {'duty'}),
    ('day', DESKTOP, {'mode': 'week', 'range': 'day'}, {'supervisor', 'code', 'time', 'duty', 'location', 'service', 'hours', 'people', 'lead'}),
    ('people', DESKTOP, {'mode': 'person', 'range': '2weeks'}, CHIP),
    ('timeline-people-month', DESKTOP, {'mode': 'timeline', 'range': 'month'}, CHIP | {'leave'}),  # the owner's screen
    ('timeline-services', DESKTOP, {'mode': 'timeline', 'range': 'week', 'rows': 'service', 'group_by': 'none'}, CHIP - {'lead', 'overtime', 'tags'} | {'people'}),
    ('phone-agenda', PHONE, {'mode': 'week', 'range': 'week'}, {'supervisor', 'rule', 'code', 'time', 'duty', 'location', 'service', 'hours', 'people', 'lead'}),
]
# Each property is switched in turn and stays switched: People goes last, since the supervision teams and the TL in the
# line of names are drawn among the people (hidden people leave nothing for them to change).
LAST_PEOPLE = lambda keys: sorted(keys, key=lambda k: (k == 'people', k))
# What the schedule shows, outside its key: each element's tag, classes, accessible name and own words.
SHOWN = """() => {const v=document.querySelector('#schedule-view');if(!v)return '';
  return [...v.querySelectorAll('*')].filter(n=>!n.closest('.view-legend')).map(n=>n.tagName+'.'+n.className+'|'+(n.getAttribute('aria-label')||'')
    +'|'+[...n.childNodes].filter(c=>c.nodeType===3).map(c=>c.textContent).join('')).join('\\n');}"""


def matrix_rota(h):
    """One week with every fact a property can show: a team lead on overtime with a person tag, a supervised trainee, an
    open place, cover below its advisory target (a could-improve mark), recorded leave and a configured duty code."""
    a, b, _ = h.seed['people']
    tag_people(h, [a])
    trainee = h.configure('people', {'display_name': 'Dr Fictional Matrix Trainee'})['record']['id']
    h.command('coordinator', 'membership.create', {'person_id': trainee, 'starts_on': '2020-01-01', 'category_id': h.seed['trainee_category']})
    h.command('credentialer', 'eligibility.create', {'person_id': trainee, 'service_id': h.seed['services'][0], 'starts_on': '2020-01-01'})
    supervision = {'trainee_category_ids': [h.seed['trainee_category']], 'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 2}
    typ = h.configure('assignment_types', {'name': 'Fictional matrix clinic', 'short_code': 'FMX', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'target': 2}], 'supervision': supervision, 'maximum_duration_minutes': 960}})['record']['id']
    w = h.workspace('Fictional matrix rota')
    day = ('2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00')
    w = h.shift(w, *day, typ=typ, people=[{'person_id': a, 'start': day[0], 'end': day[1], 'team_lead': True, 'overtime': True}])['workspace']
    late = ('2026-10-04T12:00:00+03:00', '2026-10-04T16:00:00+03:00')
    w = h.shift(w, *late, typ=typ, people=[{'person_id': b, 'start': late[0], 'end': late[1], 'participation': 'supervisor'},
        {'person_id': trainee, 'start': late[0], 'end': late[1], 'participation': 'trainee', 'supervisor_person_id': b}])['workspace']
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', typ=typ, people=[])['workspace']  # an open place
    h.command('coordinator', 'availability.record', {'person_id': a, 'kind': 'recorded_leave', 'leave_type': 'Annual leave',
        'start': '2026-10-06T00:00:00+03:00', 'end': '2026-10-07T00:00:00+03:00', 'private_reason': 'Fictional leave'})
    return h.lifecycle(w, 'validate')['workspace']


@pytest.mark.parametrize('name,viewport,planner,offered', LAYOUTS, ids=[x[0] for x in LAYOUTS])
def test_every_offered_property_changes_the_layout(browser, live_server, h, name, viewport, planner, offered):
    matrix_rota(h)
    h.command('coordinator', 'table_view.create', {'table_key': 'planner', 'name': f'Fictional matrix {name}', 'definition': {'planner': planner}})
    context, page = sign_in(browser, live_server, 'coordinator', viewport=viewport)
    try:
        navigate(page, 'planner'); settled(page)
        pick_view(page, f'Fictional matrix {name}'); settled(page)
        expect(page.locator('#schedule-view :is([data-session],.sd-bar)').first).to_be_attached()
        if name.endswith('-docked'):  # a person's panel docked beside the grid narrows every day column
            page.locator(f'#schedule-view .wk-grid {DRAFT_CHIP}').first.click()
            expect(page.locator('.wk-panel')).to_be_visible()
        panel = view_settings(page.locator('.planner-toolbar'), 'Properties')
        rows = panel.locator('.vs-prop[data-part]')
        expect(rows.first).to_be_visible()
        assert set(rows.evaluate_all('rs => rs.map(r => r.dataset.part)')) == offered
        changed = []
        for key in LAST_PEOPLE(offered):
            before = page.evaluate(SHOWN)
            eye = panel.locator(f'.vs-prop[data-part="{key}"] .vs-eye')
            pressed = eye.get_attribute('aria-pressed')
            eye.click()
            expect(panel.locator(f'.vs-prop[data-part="{key}"] .vs-eye')).not_to_have_attribute('aria-pressed', pressed)
            try:
                page.wait_for_function(f'(b) => ({SHOWN})() !== b', arg=before, timeout=5000)
                changed.append(key)
            except Exception:
                pass
        assert sorted(changed) == sorted(offered), f'{name}: no visible change for {sorted(offered - set(changed))}'
        close_popover(page)
    finally:
        context.close()


def test_the_duty_code_is_the_configured_short_code_and_a_property(browser, live_server, h):
    """The codes ("A", "M") are the duty type's configured short code (0139_duty_short_code), and in the owner's screen
    (people rows by month) the code, time and other words are each the view's choice; switching words on widens the
    day columns instead of hiding them."""
    matrix_rota(h)
    h.command('coordinator', 'table_view.create', {'table_key': 'planner', 'name': 'Fictional month of codes',
        'definition': {'planner': {'mode': 'timeline', 'range': 'month'}}})
    h.command('coordinator', 'table_view.create', {'table_key': 'planner', 'name': 'Fictional month with times',
        'definition': {'planner': {'mode': 'timeline', 'range': 'month', 'properties': [
            {'key': 'code', 'on': False, 'form': 'chip'}, {'key': 'time', 'on': True, 'form': 'text'}]}}})
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        navigate(page, 'planner'); settled(page)
        pick_view(page, 'Fictional month of codes'); settled(page)
        chips = page.locator('#schedule-view .pg-chip[data-session]')
        expect(chips.first.locator('b')).to_have_text('FMX')
        pick_view(page, 'Fictional month with times'); settled(page)
        expect(chips.first.locator('b')).to_have_text(re.compile(r'^\d\d:\d\d–\d\d:\d\d$'))
        expect(page.locator('#schedule-view .pg-chip[data-session]', has_text='FMX')).to_have_count(0)
        cut = chips.evaluate_all('cs => cs.filter(c => c.scrollWidth > c.clientWidth + 1).length')
        assert cut == 0
    finally:
        context.close()



def test_grid_chips_say_a_service_or_location_short_code_and_keep_its_name(browser, live_server, h):
    """A grid chip says a service's or location's configured short code; its tip and the chip's accessible name keep the
    name, and a record with a blank code says its name."""
    matrix_rota(h)
    catalogues = h.get('admin', '/api/catalogues')
    service = next(r for r in catalogues['services'] if r['id'] == h.seed['services'][0])
    place = next(r for r in catalogues['locations'] if r['id'] == h.seed['locations'][0])
    assert place['short_code'] == ''
    h.command('admin', 'catalogue.bulk_update', {'catalogue': 'services', 'items': [{'id': service['id'], 'revision': service['revision']}],
                                                  'set': {'short_code': 'FSV'}, 'reason': 'Codes for dense grids'})
    h.command('coordinator', 'table_view.create', {'table_key': 'planner', 'name': 'Fictional month of service codes',
        'definition': {'planner': {'mode': 'timeline', 'range': 'month', 'properties': [
            {'key': 'code', 'on': False, 'form': 'chip'}, {'key': 'time', 'on': False, 'form': 'text'},
            {'key': 'service', 'on': True, 'form': 'text'}, {'key': 'location', 'on': True, 'form': 'text'}]}}})
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        navigate(page, 'planner'); settled(page)
        pick_view(page, 'Fictional month of service codes'); settled(page)
        chip = page.locator('#schedule-view .pg-chip[data-session]').first
        code = chip.locator('[data-part="service"]')
        expect(code).to_have_text('FSV')
        expect(code).to_have_attribute('data-tip', service['name'])
        expect(chip.locator('[data-part="location"]')).to_have_text(place['name'])
        expect(chip.locator('[data-part="location"]')).not_to_have_attribute('data-tip', re.compile('.'))
        label = chip.get_attribute('aria-label')
        assert service['name'] in label and place['name'] in label and 'FSV' not in label
    finally:
        context.close()

# Which of a pill's name forms is on screen, per pill: the label, its short form or its code (person_pill.ts, L7).
NAME_FORMS = """() => [...document.querySelectorAll('#schedule-view .wk-grid DRAFT_CHIP')].map(p => {
  const on = n => !!n && getComputedStyle(n).display !== 'none';
  return {form: on(p.querySelector(':scope>.pl-tight')) ? 'tight' : on(p.querySelector(':scope>.pl-narrow')) ? 'narrow' : 'label',
          alt: !!p.querySelector(':scope>.pchip-alt'), tip: p.dataset.tip || '', name: p.getAttribute('aria-label') || ''};})""".replace('DRAFT_CHIP', DRAFT_CHIP)


def test_week_names_give_way_to_short_forms_in_narrow_cells_and_keep_the_full_name(browser, live_server, h):
    """LEAD-UIUX live audit L7: beside a docked panel, Week chips shrank to "Dr. …". A name too wide for its cell shows
    the person's short form (initial and family name), then their code, chosen by the cell's own width (@container
    pill), never the window's; the full name stays the chip's tip and the start of its accessible name. Without the
    panel, the cells are wide enough for the view's label."""
    names = ['Dr. Fictional Saranoura Binqassimah', 'Dr. Fictional Abdulwahhab Alkhuzaymah']
    ids = [h.configure('people', {'display_name': n})['record']['id'] for n in names]
    typ = h.configure('assignment_types', {'name': 'Fictional narrow clinic', 'short_code': 'FNC', 'starts_on': '2020-01-01',
                                           'definition': {'requirements': [{'minimum': 1}]}})['record']['id']
    w = h.workspace('Fictional narrow names')
    for day in range(4, 11):
        start, end = f'2026-10-{day:02d}T08:00:00+03:00', f'2026-10-{day:02d}T12:00:00+03:00'
        w = h.shift(w, start, end, typ=typ, people=[{'person_id': p, 'start': start, 'end': end} for p in ids])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}"); settled(page)
        chips = page.locator(f'#schedule-view .wk-grid {DRAFT_CHIP}')
        expect(chips.first).to_be_visible()
        wide = page.evaluate(NAME_FORMS)
        assert any(c['form'] == 'label' for c in wide), wide
        chips.first.click()
        expect(page.locator('.wk-panel')).to_be_visible()
        page.wait_for_function(f"() => ({NAME_FORMS})().some(c => c.form !== 'label')")
        docked = page.evaluate(NAME_FORMS)
        for c in docked:
            full = next(n for n in names if c['name'].startswith(n))  # the accessible name starts with the full name
            if c['alt']:
                assert c['tip'] == full, c
        assert all(c['form'] != 'label' for c in docked if c['alt']), docked
    finally:
        context.close()



def test_published_chips_beside_a_docked_panel_take_the_short_forms_too(browser, live_server, h):
    """UX live verify row 4: beside a docked panel the draft's chips took their short forms, but the published grey chips
    around the draft kept the full label and ended in "…". They are the same person pill (.sd-greychip modifier), so they
    shrink the same way and keep the full name as their tip and the start of their accessible name."""
    names = ['Dr. Fictional Saranoura Binqassimah', 'Dr. Fictional Abdulwahhab Alkhuzaymah']
    ids = [h.configure('people', {'display_name': n})['record']['id'] for n in names]
    for p in ids:  # a rota is published only with people who may work its sessions
        h.command('coordinator', 'membership.create', {'person_id': p, 'starts_on': '2020-01-01', 'category_id': h.seed['category']})
        h.command('credentialer', 'eligibility.create', {'person_id': p, 'service_id': h.seed['services'][0], 'starts_on': '2020-01-01'})
    typ = h.configure('assignment_types', {'name': 'Fictional published clinic', 'short_code': 'FPC', 'starts_on': '2020-01-01',
                                           'definition': {'requirements': [{'minimum': 1}]}})['record']['id']

    def week(w, days):
        for day in days:
            start, end = f'2026-10-{day:02d}T08:00:00+03:00', f'2026-10-{day:02d}T12:00:00+03:00'
            w = h.shift(w, start, end, typ=typ, people=[{'person_id': p, 'start': start, 'end': end} for p in ids])['workspace']
        return w
    h.publish(week(h.workspace('Fictional published week', '2026-10-04', '2026-10-09'), range(4, 9)))
    d = week(h.workspace('Fictional draft after it', '2026-10-09', '2026-10-11'), range(9, 11))
    forms = NAME_FORMS.replace(DRAFT_CHIP, '.pchip.sd-greychip[data-person]')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={d['id']}"); settled(page)
        grey = page.locator('#schedule-view .wk-grid .pchip.sd-greychip')
        expect(grey.first).to_be_visible()
        assert any(c['form'] == 'label' for c in page.evaluate(forms))
        page.locator(f'#schedule-view .wk-grid {DRAFT_CHIP}').first.click()
        expect(page.locator('.wk-panel')).to_be_visible()
        page.wait_for_function(f"() => ({forms})().some(c => c.form !== 'label')")
        docked = page.evaluate(forms)
        assert all(c['alt'] for c in docked), docked  # every published name has its short forms
        for c in docked:
            full = next(n for n in names if c['name'].startswith(n))
            assert c['tip'] == full and c['form'] != 'label', c
    finally:
        context.close()

# The Coverage popover lists a cell's sessions as session items (planner_week sessionItems: the one item renderer, the
# phone agenda's), so Coverage offers what those items draw; there are no day heads there, so no rule marks.
COVERAGE = {'supervisor', 'code', 'time', 'duty', 'location', 'service', 'hours', 'people', 'lead'}
POPOVER = SHOWN.replace("document.querySelector('#schedule-view')", "document.querySelector('.cov-popover')")


def popovers(page):
    """What the Coverage popovers of 4 Oct show (the team lead's session and the supervised one), each opened in turn."""
    cells, shown = page.locator('#schedule-view button.cov-cell[aria-label*="4 Oct 2026"]'), []
    expect(cells).to_have_count(2)
    for i in range(2):
        cells.nth(i).click()
        expect(page.locator('.cov-popover .ag-item').first).to_be_visible()
        shown.append(page.evaluate(POPOVER))
        close_popover(page)
    return shown


def test_coverage_popover_draws_sessions_with_the_item_renderer_and_its_key(browser, live_server, h):
    """LEAD-UIUX C2 (7 Nov 2026): app.ts shiftCard, a second session card in the Coverage popover (own badges, no issue
    marker, a hue without a key entry), is gone. The popover's sessions follow each property Coverage offers, every mark
    in them has its entry in the popover's key (legend guard), and selecting one opens the one session panel."""
    from .legend_guard import assert_legend_matches
    matrix_rota(h)
    h.command('coordinator', 'table_view.create', {'table_key': 'planner', 'name': 'Fictional matrix coverage',
                                                  'definition': {'planner': {'mode': 'coverage', 'range': 'week'}}})
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        navigate(page, 'planner'); settled(page)
        pick_view(page, 'Fictional matrix coverage'); settled(page)
        panel = view_settings(page.locator('.planner-toolbar'), 'Properties')
        assert set(panel.locator('.vs-prop[data-part]').evaluate_all('rs => rs.map(r => r.dataset.part)')) == COVERAGE
        # The main page's Properties summary names only what is offered (live C2 F2: it said "Open places, Rule marks").
        summary = view_settings(page.locator('.planner-toolbar')).locator('.vs-row').filter(has_text=re.compile(r'^Properties'))
        expect(summary).to_contain_text('Time')
        expect(summary).not_to_contain_text('Rule marks')
        expect(summary).not_to_contain_text('Open places')
        close_popover(page)
        cells = page.locator('#schedule-view button.cov-cell[aria-label*="4 Oct 2026"]')
        for i in range(2):
            cells.nth(i).click()
            expect(page.locator('.cov-popover .ag-item').first).to_be_visible()
            names = assert_legend_matches(page, '.cov-popover')
            assert names, 'the popover draws its sessions without a key'
            close_popover(page)
        unchanged = []
        for key in LAST_PEOPLE(COVERAGE):
            before = popovers(page)
            panel = view_settings(page.locator('.planner-toolbar'), 'Properties')
            eye = panel.locator(f'.vs-prop[data-part="{key}"] .vs-eye')
            pressed = eye.get_attribute('aria-pressed')
            eye.click()
            expect(panel.locator(f'.vs-prop[data-part="{key}"] .vs-eye')).not_to_have_attribute('aria-pressed', pressed)
            close_popover(page)
            if popovers(page) == before:
                unchanged.append(key)
        assert not unchanged, f'Coverage popover: no visible change for {unchanged}'
        # The grids' selection rule (DESIGN › the selection panel): Ctrl+click adds the session to the selection, with
        # its ring, and opens no session panel; a plain click opens it.
        cells.first.click()
        page.locator('.cov-popover .ag-item').first.click(modifiers=['Control'])
        expect(page.locator('.rk-panel').get_by_role('heading', level=2)).to_contain_text('Fictional matrix clinic')
        expect(cells.first).to_have_class(re.compile(r'\brk-selected\b'))
        expect(page.locator('.wk-panel')).to_have_count(0)
        close_popover(page)
        page.keyboard.press('Escape')
        expect(page.locator('.rk-panel')).to_have_count(0)
        cells.first.click()
        page.locator('.cov-popover .ag-item').first.click()
        expect(page.locator('.cov-popover')).to_have_count(0)
        expect(page.locator('.wk-panel').get_by_role('heading', name='People', exact=True)).to_be_visible()
    finally:
        context.close()


def test_people_days_published_chip_opens_the_session_panel_not_a_second_card(browser, live_server, h):
    """C2: a published session around the draft in People × Days opens the same session dock as the Week grid's published
    chip (both call hooks.view → sessionDock). Its actions are the published-amendment route (DESIGN.md › Changes to a
    published rota: Replace…, Mark absent, Add a person, Close the session…), not a second session card or a popover."""
    from .test_browser_schedule import october, show
    _, d = october(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        dock = page.locator('.dock-panel')
        show(page, f"schedule/week/2026-10-12?edit={d['id']}")
        page.locator('#schedule-view .wk-grid .sd-greychip').first.click()
        expect(dock).to_be_visible()
        expect(dock).to_contain_text('Published')
        week = dock.get_by_role('heading').first.inner_text()
        page.keyboard.press('Escape')
        expect(dock).to_have_count(0)
        show(page, f"schedule/people/2026-10-12?edit={d['id']}")
        page.locator('#schedule-view .people-grid .pg-chip.sd-greychip').first.click()
        expect(dock).to_be_visible()
        expect(dock.get_by_role('heading').first).to_have_text(week)  # the same session, in the same dock
        expect(page.locator('.cov-popover')).to_have_count(0)
    finally:
        context.close()
