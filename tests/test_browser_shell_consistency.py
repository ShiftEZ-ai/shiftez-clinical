"""One way to do each thing: the collection toolbar, dropdowns, tab keyboard activation and the status colours."""
import re

import pytest
from playwright.sync_api import expect
from .test_browser import add_filter, browser, live_server, sign_in, navigate, pick_view, toolbar, close_popover, sort_direction  # noqa: F401

pytestmark = pytest.mark.browser
ORDER = ['Saved views', 'search', 'Filter', 'Group', 'Sort', 'View settings']  #


def toolbar_order(scope):
    """The collection controls in document order: the search box, then each toolbar button by its role in the order."""
    names = scope.locator('.dt-toolbar').first.evaluate("""bar => [...bar.querySelectorAll('input[type=search], .dt-tool')].map(n =>
      n.matches('input') ? 'search' : n.classList.contains('dt-saved') ? 'Saved views' : /^View settings(:|$)/.test(n.getAttribute('aria-label')) ? 'View settings' : n.querySelector('.dt-tool-label').textContent.split(':')[0])""")
    return [n for n in names if n in ORDER]


def test_people_list_and_planner_share_one_toolbar_in_one_order(browser, live_server, h):
    h.shift(h.workspace('Toolbar order'))
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        assert toolbar_order(page.locator('#main')) == ORDER
        bar = page.locator('#main .dt-toolbar').first
        # Group: one choice, announced as checked; the button shows it is on.
        bar.get_by_role('button', name='Group', exact=True).click()
        menu = page.get_by_role('menu', name='Group by')
        choice = menu.get_by_role('menuitemradio').nth(1)
        label = choice.inner_text().strip()
        choice.click()
        group = bar.get_by_role('button', name=f'Group: {label}', exact=True)
        expect(group).to_have_class(re.compile(r'\bon\b'))
        editor = page.get_by_role('dialog', name='Group', exact=True)
        expect(editor.get_by_label('Group by', exact=True).locator('option:checked')).to_have_text(label)
        editor.get_by_role('button', name='Remove grouping', exact=True).click()
        close_popover(page)
        expect(bar.get_by_role('button', name='Group', exact=True)).not_to_have_class(re.compile(r'\bon\b'))
        # Sort: property, then direction, both in one popover.
        bar.get_by_role('button', name='Sort', exact=True).click()
        page.get_by_role('menu', name='Sort by').get_by_role('menuitemradio').nth(1).click()
        sort = bar.get_by_role('button', name=re.compile(r'^Sort: .+, ascending$'))
        expect(sort).to_be_visible()
        sort_direction(bar, 'Descending')  # the popover stays open on the sort list
        expect(bar.get_by_role('button', name=re.compile(r'^Sort: .+, descending$'))).to_be_visible()
        # View settings: the layout choice keeps its popover open; properties live there too.
        bar.get_by_role('button', name=re.compile(r'^View settings')).click()
        view = page.get_by_role('dialog', name='View settings')
        view.get_by_role('radio', name='Cards', exact=True).click()
        expect(view).to_be_visible()
        expect(page.locator('#main .dt-card').first).to_be_visible()
        expect(view.get_by_text('Shown properties')).to_be_visible()
        page.keyboard.press('Escape')

        navigate(page, 'planner')
        tools = page.locator('#main .planner-toolbar')
        assert toolbar_order(page.locator('#main')) == ORDER
        # One filter system for every layout: the List view has no second toolbar.
        pick_view(page, 'List')
        expect(page.locator('#main .dt-toolbar')).to_have_count(1)
        expect(page.locator('#main').get_by_role('button', name=re.compile(r'^Filter'))).to_have_count(1)
        tools.get_by_role('button', name='Filter', exact=True).click()
        pop = page.get_by_role('dialog', name='Filter')
        pop.get_by_label('Only sessions that break a rule', exact=True).check()
        # The planner redraws behind the popover; the popover stays open and its button shows one active filter.
        expect(tools.get_by_role('button', name=re.compile(r'^Filter: .+ \(1 active\)$'))).to_be_visible()
        expect(pop).to_be_visible()
        pop.get_by_role('button', name='Clear all filters', exact=True).click()
        expect(page.locator('#main .planner-toolbar').get_by_role('button', name='Filter', exact=True)).to_be_visible()
        page.keyboard.press('Escape')
    finally:
        context.close()


def test_long_dropdowns_search_short_ones_do_not_and_both_stay_in_view(browser, live_server, h):
    for k in range(8):
        h.configure('people', {'display_name': f'Fictional picker person {k}'})
    h.shift(h.workspace('Dropdown behaviour'))
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1280, 'height': 640})
    try:
        navigate(page, 'planner')
        page.locator('#main .planner-toolbar').get_by_role('button', name='Filter', exact=True).click()
        pop = page.get_by_role('dialog', name='Filter')
        pop.get_by_role('button', name=re.compile('^Person:')).click()
        picker = page.locator('.picker')
        search = picker.get_by_role('combobox')
        expect(search).to_be_visible()  # more than 7 choices: a search box
        search.fill('picker person 3')
        expect(picker.get_by_role('option', name=re.compile('Fictional picker person 3'))).to_be_visible()
        expect(picker.get_by_role('option', name=re.compile('Fictional picker person 4'))).to_have_count(0)
        assert picker.evaluate('p => {const r=p.getBoundingClientRect();return r.left>=0&&r.top>=0&&r.right<=innerWidth&&r.bottom<=innerHeight;}')
        picker.get_by_role('option', name=re.compile('Fictional picker person 3')).check()
        page.keyboard.press('Escape')
        expect(search).to_have_value('')  # First Escape clears the query; the next closes this picker.
        page.keyboard.press('Escape')
        expect(page.locator('#main .planner-toolbar').get_by_role('button', name=re.compile(r'^Filter: .+ \(1 active\)$'))).to_be_visible()
        # A short list has its search box too (every list does), focused.
        # SR-06: the Filter is the shared condition builder; a Location rule opens its list at once.
        add_filter(pop)
        pop.locator('.cond-line').last.get_by_label('Filter on', exact=True).select_option(label='Location')
        expect(page.locator('.picker').get_by_role('combobox')).to_be_focused()
        page.keyboard.press('Escape')
        pop.get_by_role('button', name='Clear all filters', exact=True).click()
        expect(page.locator('#main .planner-toolbar').get_by_role('button', name='Filter', exact=True)).to_be_visible()
    finally:
        context.close()


def test_expensive_tabs_wait_for_enter_and_cheap_radios_follow_the_arrow(browser, live_server, h):
    h.shift(h.workspace('Keyboard activation'))
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        # Ranges redraw the schedule: in View settings, arrows only move focus; Enter chooses.
        ranges = toolbar(page.locator('.planner-toolbar'), 'View settings').get_by_role('radiogroup', name='Range', exact=True)
        week, two = ranges.get_by_role('radio', name='Week', exact=True), ranges.get_by_role('radio', name='2 weeks', exact=True)
        before = page.locator('#schedule-view').element_handle()
        week.focus(); page.keyboard.press('ArrowRight')
        expect(two).to_be_focused()
        expect(week).to_have_attribute('aria-checked', 'true')  # moving focus does not redraw the schedule
        assert before.evaluate('view => view.isConnected')
        page.keyboard.press('Enter')
        expect(two).to_have_attribute('aria-checked', 'true')
        expect(page.locator('.wk-grid .wk-head .wk-day')).to_have_count(14)
        close_popover(page)
        # The theme is a choice in the account menu: arrows move through the menu, Enter chooses (N-16).
        page.get_by_role('button', name=re.compile(r'^Account menu, ')).first.click()
        themes = page.get_by_role('group', name='Appearance')
        themes.get_by_role('menuitemradio', name='Light', exact=True).click()
        expect(page.locator('html')).to_have_attribute('data-theme', 'light')
        themes.get_by_role('menuitemradio', name='Light', exact=True).focus()
        page.keyboard.press('ArrowDown')
        expect(themes.get_by_role('menuitemradio', name='Dark', exact=True)).to_be_focused()
        expect(page.locator('html')).to_have_attribute('data-theme', 'light')
        page.keyboard.press('Enter')
        expect(themes.get_by_role('menuitemradio', name='Dark', exact=True)).to_have_attribute('aria-checked', 'true')
        expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
    finally:
        context.close()


CONTRAST = """([cls, tag]) => {
  const rgb = c => c.match(/[\\d.]+/g).map(Number).slice(0, 3);
  const lum = c => { const [r, g, b] = rgb(c).map(v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; }); return .2126 * r + .7152 * g + .0722 * b; };
  const n = document.createElement(tag); n.className = cls; n.textContent = 'Confirm?'; document.body.append(n);
  const s = getComputedStyle(n), [a, b] = [lum(s.color), lum(s.backgroundColor)].sort((x, y) => y - x);
  const out = {ratio: Math.round((a + .05) / (b + .05) * 100) / 100, background: s.backgroundColor, color: s.color}; n.remove(); return out;
}"""


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_confirm_error_and_environment_colours_are_solid_and_legible(browser, live_server, h, theme):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.emulate_media(color_scheme=theme)
        navigate(page, 'people')
        red = None
        for cls, tag in (('button secondary armed', 'button'), ('toast error', 'div'), ('env-badge', 'span'), ('badge warn', 'span')):
            c = page.evaluate(CONTRAST, [cls, tag])
            assert c['ratio'] >= 4.5, (theme, cls, c)
            if cls != 'badge warn' and cls != 'env-badge':
                red = red or c['background']
                assert c['background'] == red, (theme, cls, c)  # the armed button and the error toast: one solid red
        # One tone vocabulary (dom.ts Tone): a state name or a synonym is not a second way to colour a badge; states go
        # through statusTone, so 'badge warning' or 'badge submitted' is a plain badge, not a second gold.
        assert page.evaluate("""() => {const look=c=>{const n=document.createElement('span');n.className=c;document.body.append(n);
          const s=getComputedStyle(n),v=s.color+s.backgroundColor;n.remove();return v;};
          return ['badge warning','badge submitted','badge queued'].every(c=>look(c)===look('badge'))&&look('badge warn')!==look('badge');}""")
    finally:
        context.close()


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_rotation_without_a_type_shows_the_notice_warning_not_information(browser, live_server, h, theme):
    """A notice has its own tones (dom.ts NoticeTone): 'warn' is a badge tone, so passing it to note() drew the
    missing-rotation-type warning as the blue information notice. It must look like every other notice warning."""
    programme = h.configure('programmes', {'name': 'Untyped programme', 'starts_on': '2026-01-01'})['record']
    cohort = h.configure('cohorts', {'name': 'Untyped cohort', 'programme_id': programme['id'],
                                     'starts_on': '2026-01-01', 'ends_on': '2027-01-01'})['record']
    person = h.seed['people'][0]
    h.configure('programme_memberships', {'person_id': person, 'programme_id': programme['id'],
                                          'starts_on': '2026-01-01', 'ends_on': '2027-01-01'})
    rotation = h.configure('rotations', {'person_id': person, 'cohort_id': cohort['id'], 'service_id': h.seed['services'][0],
                                         'starts_on': '2026-10-01', 'ends_on': '2026-11-01'})['record']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.emulate_media(color_scheme=theme)
        navigate(page, 'programmes/rotations')
        notice = page.locator(f'#main [data-row="{rotation["id"]}"] .notice', has_text='No rotation type selected')
        expect(notice).to_have_class(re.compile(r'\bwarning\b'))
        looks = notice.evaluate("""n => {const look=c=>{const p=document.createElement('div');p.className=c;n.parentElement.append(p);
          const v=getComputedStyle(p).backgroundColor;p.remove();return v;};
          return {own: getComputedStyle(n).backgroundColor, warning: look('notice warning'), info: look('notice')};}""")
        assert looks['own'] == looks['warning'] != looks['info'], looks
    finally:
        context.close()
