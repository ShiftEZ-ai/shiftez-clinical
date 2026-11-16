"""Auto-schedule live view on the Calendar's actual renderers (P0, 25 Oct 2026): the Schedule grid (Timeline / Duty
codes, Counts, any rows × columns) and the Week's busy cells follow the solver's events; a grid redrawn just before Done still gets
the final result; and the changes of one better schedule play as a steady stream, not all in one frame. Reuses the
running-job stream of test_browser_autoschedule_live."""
import re

import pytest
from playwright.sync_api import expect

from apps.worker.main import process_one
from .test_browser import browser, choose, choose_radio, close_popover, live_server, view_settings  # noqa: F401
from .test_browser_cp_sat import cp_server  # noqa: F401
from .test_browser_autoschedule_live import seen_done, start_run

pytestmark = pytest.mark.browser
IMPROVING = {'stage': 'improving', 'elapsed_seconds': 9, 'search_started_seconds': 3, 'time_limit_seconds': 120}


def show(page, *radios):
    """View settings: each (group, choice) in turn."""
    panel = view_settings(page.locator('.planner-toolbar'))
    for group, name in radios:
        choose_radio(panel, group, name)
    close_popover(page)


def row(grid, person):
    return grid.locator(f'.pg-row[data-row-person="{person}"]')


def day(grid, person, d):
    return row(grid, person).locator(f'.pg-cell[data-col="2026-10-{d:02d}"]')


def done(page, stream, h):
    stream.on = False
    process_one(h.app.state.engine)
    assert seen_done(page)['step'] == '5/5 Done'


def test_timeline_duty_codes_follow_the_run(browser, cp_server, h):
    """People rows showing Duty codes: a person's first duty adds their row, a swap flies, a second duty of the same
    code that day reads "2×" and the hours follow."""
    a, b, c = h.seed['people'][:3]
    context, page, stream, w, (s4m, s4a, s5m, s5a, s5e) = start_run(browser, cp_server, h, [
        (4, 8, [a]), (4, 13, [b]), (5, 8, []), (5, 13, [a]), (5, 18, [])])
    try:
        show(page, ('Cells show', 'Duty codes'))
        grid = page.locator('#schedule-view .tl-grid')
        expect(grid).to_be_visible()
        expect(row(grid, c)).to_have_count(0)  # no duty yet, no row
        stream.progress = IMPROVING
        stream.event(adds=[(s5m, c)])
        expect(day(grid, c, 5).locator(f'.pg-chip[data-session="{s5m}"]')).to_be_visible()
        expect(row(grid, c).locator('.pg-hours')).to_have_text('4 h')

        stream.event(adds=[(s4m, b), (s4a, a)], removes=[(s4m, a), (s4a, b)])  # a swap: both chips fly
        expect(day(grid, a, 4).locator(f'.pg-chip[data-session="{s4a}"]')).to_have_count(1)
        expect(day(grid, b, 4).locator(f'.pg-chip[data-session="{s4m}"]')).to_have_count(1)
        page.wait_for_function('() => window.__flew > 0')

        stream.event(adds=[(s5e, a)])  # a's second duty on the 5th, same code: one chip "2×", as the grid draws it
        expect(day(grid, a, 5).locator('.pg-chip')).to_have_text(re.compile(r'^2×'))
        expect(day(grid, a, 5).locator('.pg-chip')).to_have_attribute('data-session', s5a)
        expect(row(grid, a).locator('.pg-hours')).to_have_text('12 h')

        stream.event(adds=[(s5e, c)], removes=[(s5e, a)])  # the evening goes to c, behind c's morning
        expect(day(grid, a, 5).locator('.pg-chip')).not_to_have_text(re.compile('×'))
        expect(day(grid, c, 5).locator('.pg-chip')).to_have_text(re.compile(r'^2×'))
        expect(day(grid, c, 5).locator('.pg-chip')).to_have_attribute('data-session', s5m)
        anims = page.evaluate('window.__anim.filter(a=>a.live)')
        assert anims and all(set(a['props']) <= {'transform', 'opacity', 'offset', 'easing', 'composite'} for a in anims), anims

        done(page, stream, h)
        expect(grid).to_have_attribute('aria-busy', 'false')
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_counts_follow_the_run(browser, cp_server, h):
    """Counts by person (weeks) count each person's duties and hours live, a person's first duty adding their row;
    Counts by duty (days) follow the places filled."""
    a, b, c = h.seed['people'][:3]
    context, page, stream, w, (s4, s5m, s5a, s6) = start_run(browser, cp_server, h, [
        (4, 8, [a]), (5, 8, []), (5, 13, [a]), (6, 8, [])])
    try:
        show(page, ('Columns', 'Weeks'))
        grid = page.locator('#schedule-view .ct-grid')
        expect(grid).to_be_visible()
        expect(row(grid, a).locator('.pg-cell.sg-count').filter(has_text=re.compile(r'\d'))).to_have_text('2')
        expect(row(grid, c)).to_have_count(0)
        stream.progress = IMPROVING
        stream.event(adds=[(s5m, c)])
        expect(row(grid, c).locator('.pg-cell.sg-count').filter(has_text=re.compile(r'\d'))).to_have_text('1')
        expect(row(grid, c).locator('.pg-hours')).to_have_text('4 h')
        stream.event(adds=[(s5m, a)])
        a_count = row(grid, a).locator('.pg-cell.sg-count').filter(has_text=re.compile(r'\d'))
        expect(a_count).to_have_text('3')
        expect(a_count).to_have_attribute('aria-label', re.compile(r': 3 duties$'))
        expect(row(grid, a).locator('.pg-hours')).to_have_text('12 h')

        show(page, ('Rows', 'Duties'), ('Columns', 'Days'), ('Cells show', 'Counts'))
        places = page.locator(f'#schedule-view .ct-grid .pg-cell[data-count][data-sessions~="{s6}"]')
        expect(places).to_have_text(re.compile(r'^\d+/\d+$'))
        before = places.inner_text()
        stream.event(adds=[(s6, b)])
        expect(places).not_to_have_text(before)
        expect(places).to_have_attribute('aria-label', re.compile(r'\d+ of \d+ places filled$'))
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_rows_by_category_columns_follow_the_run(browser, cp_server, h):
    """Any rows × columns: people by duty type (a category as the columns), counted, follow the run like days do."""
    a, b, c = h.seed['people'][:3]
    context, page, stream, w, (s4, s5) = start_run(browser, cp_server, h, [(4, 8, [a]), (5, 8, [])])
    try:
        show(page, ('Rows', 'People'), ('Columns', 'Duty types'), ('Cells show', 'Counts'))
        grid = page.locator('#schedule-view .ct-grid.sg-cat')
        expect(grid).to_be_visible()
        count = row(grid, a).locator('.pg-cell.sg-count[data-units]').first
        expect(count).to_have_text('1')
        stream.progress = IMPROVING
        stream.event(adds=[(s5, a)])
        expect(count).to_have_text('2')
        expect(row(grid, a).locator('.pg-hours')).to_have_text('8 h')
        done(page, stream, h)
        expect(grid).to_have_attribute('aria-busy', 'false')
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()

def test_a_duty_type_nobody_holds_has_its_column_and_fills_live(browser, cp_server, h):
    """People × Duty types: a type with only open sessions has its column, so a run filling it shows there (count,
    hours) instead of waiting for Apply."""
    a, b, c = h.seed['people'][:3]
    typ = h.configure('assignment_types', {'name': 'Live open clinic', 'starts_on': '2020-01-01',
                                           'definition': {'requirements': [{'minimum': 1}]}})['record']['id']
    context, page, stream, w, (s4, s5) = start_run(browser, cp_server, h, [(4, 8, [a]), (5, 8, [], typ)])
    try:
        show(page, ('Rows', 'People'), ('Columns', 'Duty types'), ('Cells show', 'Counts'))
        grid = page.locator('#schedule-view .ct-grid.sg-cat')
        expect(grid.locator('.pg-day', has_text='Live open clinic')).to_have_count(1)
        expect(row(grid, c)).to_have_count(0)
        stream.progress = IMPROVING
        stream.event(adds=[(s5, c)])
        expect(row(grid, c).locator('.pg-cell.sg-count[data-units]').filter(has_text=re.compile(r'\d'))).to_have_text('1')
        expect(row(grid, c).locator('.pg-hours')).to_have_text('4 h')
        done(page, stream, h)
        expect(grid).to_have_attribute('aria-busy', 'false')
        expect(grid.locator('.asl-changed')).not_to_have_count(0)
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()

def test_grouped_by_service_an_all_open_service_has_its_heading_and_counts_follow(browser, cp_server, h):
    """Timeline grouped by Service: a service with only open sessions has its heading and Staffed / needed; a run
    filling it adds the person under it, and each heading's people count follows."""
    a, b, c = h.seed['people'][:3]
    names = {s['id']: s['name'] for s in h.get('coordinator', '/api/catalogues')['services']}
    held, empty = names[h.seed['services'][0]], names[h.seed['services'][1]]
    context, page, stream, w, (s4, s5) = start_run(browser, cp_server, h, [(4, 8, [a]), (5, 8, [], None, 1)])
    try:
        show(page, ('Rows', 'People'), ('Columns', 'Days'), ('Cells show', 'Duty codes'))
        expect(page.locator('#schedule-view')).to_have_attribute('aria-label', 'Timeline view')
        # Service is the Timeline's default grouping (its menu entry carries the "default" hint): nothing to choose.
        expect(page.locator('.planner-toolbar').get_by_role('button', name='Group: Service', exact=True)).to_be_visible()
        grid = page.locator('#schedule-view .tl-grid')
        count = lambda name: grid.locator(f'.pg-group[data-group="{name}"] .gt-count')  # groupToggle's count chip
        expect(count(empty)).to_have_text('0 people')
        expect(count(held)).to_have_text('1 person')
        expect(grid.locator(f'.tl-cover .pg-cell[data-count][data-sessions~="{s5}"]')).to_have_count(1)
        stream.progress = IMPROVING
        stream.event(adds=[(s5, c)])
        expect(grid.locator(f'.pg-row[data-group="{empty}"][data-row-person="{c}"] .pg-chip[data-session="{s5}"]')).to_be_visible()
        expect(count(empty)).to_have_text('1 person')
        stream.event(adds=[(s4, b)])
        expect(count(held)).to_have_text('2 people')
        done(page, stream, h)
        expect(grid).to_have_attribute('aria-busy', 'false')
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()

def test_an_all_open_service_has_no_empty_heading_over_rows_nobody_can_fill(browser, cp_server, h):
    """Duty-type rows grouped by Service, then by Rank (a person's value): an all-open service has no rows a run could
    add, so it gets no (empty) heading."""
    a = h.seed['people'][0]
    names = {s['id']: s['name'] for s in h.get('coordinator', '/api/catalogues')['services']}
    held, empty = names[h.seed['services'][0]], names[h.seed['services'][1]]
    context, page, stream, w, ids = start_run(browser, cp_server, h, [(4, 8, [a]), (5, 8, [], None, 1)])
    try:
        show(page, ('Rows', 'Duty types'), ('Columns', 'Days'), ('Cells show', 'Duty codes'))
        expect(page.locator('#schedule-view')).to_have_attribute('aria-label', 'Timeline view')
        bar = page.locator('.planner-toolbar')
        expect(bar.get_by_role('button', name='Group: Service', exact=True)).to_be_visible()  # the default grouping
        choose(bar, 'Group', 'Then by', 'Rank')
        grid = page.locator('#schedule-view .tl-grid')
        expect(grid.locator(f'.pg-group[data-group="{held}"]')).to_have_count(1)
        expect(grid.locator(f'.pg-group[data-group="{empty}"]')).to_have_count(0)
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()

def test_week_busy_cell_keeps_its_first_chips_as_people_move(browser, cp_server, h):
    """Week shows a busy cell's first 8 chips and "+N": a hidden person moving to another session shows there, and
    the cell they left shows the right chips and count."""
    crowd = h.seed['people'][:3] + [h.configure('people', {'display_name': f'Live crowd {i}'})['record']['id'] for i in range(7)]
    context, page, stream, w, (s1, s2) = start_run(browser, cp_server, h, [(4, 8, crowd), (4, 13, [])], view='Week')
    try:
        grid = page.locator('#schedule-view .wk-grid')
        hidden = grid.locator(f'.pchip[data-shift="{s1}"][hidden]')
        more = grid.locator(f'.wk-cell[data-session~="{s1}"] .pchip.more')
        expect(hidden).to_have_count(2)
        expect(more).to_have_text('+2')  # +N everywhere (DESIGN D8); its name says it in full
        expect(more).to_have_accessible_name('2 more people: show all')
        person = hidden.first.get_attribute('data-person')
        stream.progress = IMPROVING
        stream.event(adds=[(s2, person)], removes=[(s1, person)])
        expect(grid.locator(f'.pchip[data-shift="{s2}"][data-person="{person}"]')).to_be_visible()
        expect(hidden).to_have_count(1)
        expect(more).to_have_text('+1')
        expect(more).to_have_accessible_name('1 more person: show all')
        more.click()  # the live copies show too
        expect(grid.locator(f'.wk-cell[data-session~="{s1}"] .pchip[hidden]')).to_have_count(0)
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


# Before the page sees the run end, the Timeline is drawn again (collapse and expand a group, synchronously): Done must
# still put the result on the new grid, not on the one it replaced.
REDRAW_BEFORE_DONE = """(() => {const f0=window.fetch;window.fetch=async(...a)=>{const r=await f0(...a);
  try{const url=String(a[0]?.url||a[0]);
    if(window.__redrawBeforeDone&&/\\/api\\/jobs\\/[^/?]+$/.test(url)&&(await r.clone().json())?.job?.status==='succeeded'){
      window.__redrawBeforeDone=false;const t=()=>document.querySelector('#schedule-view .tl-grid .wk-toggle');
      const old=document.querySelector('#schedule-view .tl-grid');t().click();t().click();
      window.__redrawn=old!==document.querySelector('#schedule-view .tl-grid');}}catch(e){}
  return r;};})()"""


def test_a_grid_redrawn_just_before_done_gets_the_result(browser, cp_server, h):
    context, page, stream, w, ids = start_run(browser, cp_server, h, [(4, 8, []), (5, 8, []), (6, 8, [])])
    try:
        page.evaluate(REDRAW_BEFORE_DONE)
        show(page, ('Cells show', 'Duty codes'))
        grid = page.locator('#schedule-view .tl-grid')
        expect(grid).to_be_visible()
        page.evaluate('window.__redrawBeforeDone=true')
        done(page, stream, h)
        assert page.evaluate('window.__redrawn') is True
        job = h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs'][0]
        added = h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']['additions']
        assert added
        for a in added:
            expect(grid.locator(f'.pg-chip[data-session="{a["shift_id"]}"][data-person="{a["participant"]["person_id"]}"]')).to_have_count(1)
        expect(grid).to_have_attribute('aria-busy', 'false')
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


# When each chip of the People grid starts to move (performance.now()).
CADENCE = """(() => {window.__starts=[];const animate=Element.prototype.animate;
  Element.prototype.animate=function(frames,options){
    if(this.matches?.('#schedule-view .pg-chip[data-session]'))window.__starts.push(performance.now());
    return animate.call(this,frames,options);};})()"""


def test_one_better_schedule_plays_as_a_stream_not_a_jump(browser, cp_server, h):
    """Four places filled by one better schedule start one after another, spread over the time until the next update
    (at least 1/3 s at the default 3 updates a second), not in one frame."""
    a, b, c = h.seed['people'][:3]
    context, page, stream, w, (s4, s5, s6, s7) = start_run(browser, cp_server, h, [(4, 8, []), (5, 8, []), (6, 8, []), (7, 8, [])])
    try:
        page.evaluate(CADENCE)
        stream.progress = IMPROVING
        stream.event(adds=[(s4, a), (s5, b), (s6, c), (s7, a)])
        for s, p in ((s4, a), (s5, b), (s6, c), (s7, a)):
            expect(page.locator(f'#schedule-view .pg-chip[data-session="{s}"][data-person="{p}"]')).to_be_visible()
        page.wait_for_function('() => window.__starts.length >= 4')
        starts = sorted(page.evaluate('window.__starts'))
        assert starts[-1] - starts[0] >= 150, starts  # spread out (one frame would be under 20 ms)
        assert len({round(t / 16) for t in starts}) >= 3, starts  # in several frames
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()
