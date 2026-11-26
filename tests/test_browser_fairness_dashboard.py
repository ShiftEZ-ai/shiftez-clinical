"""The fairness dashboard (v0.8 W3): score and goals, distribution with drill-down, trends, the exposure heatmap,
saved views, staff's own view, phone width, and Compare drafts in the planner."""
import re
import pytest
from playwright.sync_api import expect

from .legend_guard import assert_legend_matches
from apps.api import models as m
from apps.api.db import transaction
from .test_browser import browser, live_server, sign_in, navigate, condition, wait_held, planner_tool, route_fetch  # noqa: F401
from .test_fairness import goal, session
from .test_overtime_offered_api import offer

pytestmark = pytest.mark.browser
OCTOBER = ('2026-10-01', '2026-11-01')


def published_month(h):
    """Two published weeks in October: Alex works three duties (one overtime), Jordan two, Taylor one."""
    a, b, c = h.seed['people']
    goal(h, metric='duties', period='month')
    w = h.workspace('October week 1', '2026-10-04', '2026-10-11')
    w = session(h, w, 4, [a, b], overtime=[a])
    w = session(h, w, 5, [a, c])
    h.publish(w)
    w = h.workspace('October week 2', '2026-10-11', '2026-10-18')
    w = session(h, w, 11, [a, b])
    h.publish(w)
    return a, b, c


def october(page):
    """Custom dates keep the test independent of today's date."""
    page.get_by_label('Period', exact=True).select_option('custom')
    page.get_by_label('From', exact=True).fill(OCTOBER[0])
    page.get_by_label('Until (included)', exact=True).fill('2026-10-31')  # stored end OCTOBER[1]
    page.get_by_label('Until (included)', exact=True).blur()  # leaving the field applies the dates at once
    expect(page.get_by_label('From', exact=True)).to_have_value('Thu 1 Oct 2026')  # the date picker shows the day in full


def group_by(page, label):
    """The collection toolbar's Group menu."""
    page.get_by_role('button', name=re.compile('^Group')).click()
    page.get_by_role('menuitemradio', name=label, exact=True).click()


def section(page, key):
    """One section once the whole dashboard has drawn: sections answer in any order, and one drawn above this one
    later moves it (a menu opened from it then lands off screen)."""
    s = page.locator(f'[data-section={key}]')
    expect(page.locator('.fx-page')).to_have_attribute('data-ready', 'true', timeout=60_000)
    expect(s).not_to_have_attribute('aria-busy', 'true', timeout=60_000)
    return s


def test_dashboard_draws_every_section_and_drills_down(browser, live_server, h):
    published_month(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        navigate(page,'insights/fairness')
        october(page)
        score = section(page, 'score')
        expect(score).to_contain_text('Schedule score')
        expect(score.locator('.fx-point')).to_have_count(2)  # one point per published rota
        expect(page.locator('.app-footer .page-status')).to_contain_text('Schedule score')  # the headline number, in the footer
        goals = section(page, 'goals')
        expect(goals.locator('.fx-goal')).to_have_count(2)  # authored duties goal plus seeded workload goal
        dist = section(page, 'distribution')
        alex = dist.get_by_role('button', name=re.compile('^Staff 01\\(test\\): 3 duties'))
        expect(alex).to_be_visible()
        # Sort (the toolbar's) orders the list at once, without reloading the dashboard.
        page.get_by_role('button', name='Sort', exact=True).click()
        page.get_by_role('menuitemradio', name='Name', exact=True).click()
        expect(page.get_by_role('button', name=re.compile('^Sort: Name'))).to_be_visible()
        names = dist.locator('.fx-drow .fx-name').all_text_contents()
        assert names == sorted(names), names
        # The toolbar's people search narrows the list at once.
        page.get_by_label('Find a person', exact=True).fill('staff 01')
        expect(dist.locator('.fx-drow')).to_have_count(1)
        page.get_by_label('Find a person', exact=True).fill('')
        expect(dist.locator('.fx-drow')).to_have_count(len(names))
        alex.click()
        drill = page.get_by_role('region', name='Duties · Staff 01(test)')
        expect(drill.locator('section.fx-drill')).to_have_count(2)
        expect(drill.locator('tbody tr')).to_have_count(3)
        drill.get_by_role('button', name='Close panel').click()
        # Any measure, same chart: hours, then overtime (only Alex).
        page.get_by_label('Measure', exact=True).select_option('hours')
        expect(section(page, 'distribution').get_by_role('button', name=re.compile('^Staff 01\\(test\\): 12 h'))).to_be_visible()
        trends = section(page, 'trends')
        trends.get_by_role('button', name=re.compile('^Oct 2026: 1 overtime duties')).click()
        who = page.get_by_role('region', name=re.compile(r'^Overtime duties · 2026-10'))  # a docked panel (C26, bc24d200e)
        who.get_by_role('button', name=re.compile('Staff 01\\(test\\)')).click()
        expect(page.get_by_role('region', name='Overtime duties · Staff 01(test)').locator('tbody tr')).to_have_count(1)
        page.keyboard.press('Escape')
        # Exposure: numbers in the cells; arrow keys move; Enter opens the sessions together.
        heat = section(page, 'exposure').locator('table.fx-heat')
        cell = heat.get_by_role('button', name='Staff 01(test) with Staff 02(test): 2')
        expect(cell).to_have_text('2')
        heat.locator('td[tabindex="0"]').focus()
        page.keyboard.press('ArrowRight')
        expect(heat.locator('td[data-i="0"][data-j="1"]')).to_be_focused()
        cell.click()
        expect(page.get_by_role('region', name='Duties shared with a partner · Staff 01(test) with Staff 02(test)').locator('tbody tr')).to_have_count(2)
        page.keyboard.press('Escape')
        # Filter people with the shared condition builder.
        page.get_by_role('button', name='Filter', exact=True).click()
        pop = page.get_by_role('dialog', name='Filter')
        condition(pop, 'Person', ['Staff 02(test)'])
        # Live, like every collection's Filter: no Apply button; the dashboard follows while the popover stays open.
        expect(pop.get_by_role('button', name='Apply')).to_have_count(0)
        expect(section(page, 'distribution').locator('.fx-drow')).to_have_count(1)
        expect(page.locator('.fx-scope')).to_contain_text('Staff 02(test)')
        expect(pop).to_be_visible()
        page.keyboard.press('Escape')
        expect(page.get_by_role('button', name='Filter (1 active)', exact=True)).to_be_visible()
        assert not errors, errors
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_workload_goal_person_opens_its_counted_sessions(browser, live_server, h, scheme):
    from urllib.parse import parse_qs, urlsplit

    a = h.seed['people'][0]
    w = h.workspace()
    for day in (4, 5, 6, 7):
        w = session(h, w, day, [a])
    publication = h.publish(w)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'insights/fairness')
        october(page)
        workload = section(page, 'goals').locator('.fx-goal').filter(has_text='Workload')
        expect(workload).to_contain_text('outside the band')
        with page.expect_response(lambda r: '/api/fairness/sessions?' in r.url and 'metric=workload' in r.url) as response:
            workload.get_by_role('button', name=re.compile('^Staff 01\\(test\\)')).click()
        assert response.value.status == 200, response.value.text()
        query = parse_qs(urlsplit(response.value.url).query)
        assert query['workspace_id'] == [w['id']] and query['goal_id']
        assert query['starts_on'] == [w['starts_on']] and query['ends_on'] == [w['ends_on']]
        assert not {'assignment_type_ids', 'service_ids', 'location_ids'} & query.keys()
        result = response.value.json()
        assert result['metric'] == 'hours' and result['unit'] == 'h'
        [pub] = result['publications']
        assert pub['publication_id'] == publication['publication_id'] and pub['value'] == 16
        drill = page.get_by_role('region', name='Workload · Staff 01(test)')
        expect(drill.locator('tbody tr')).to_have_count(4)
        expect(drill).to_contain_text('16 h')
    finally:
        context.close()


def test_person_trend_lines_work_groups_and_locations(browser, live_server, h):
    """Trend lines per person (the furthest from share, or people chosen with the condition builder), each with a colour and
    a dash, a legend that opens the sessions, and a table; grouping by clinic counts only a clinic on click; a location
    filter counts the locations inside it."""
    a, b, c = published_month(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        navigate(page,'insights/fairness')
        october(page)
        people = section(page, 'people')
        expect(people.get_by_role('heading', name='Duties per person by month')).to_be_visible()
        expect(people.locator('.fx-series')).to_have_count(3)
        chart = people.get_by_role('img', name=re.compile('^Duties per month for 3 people'))
        expect(chart).to_be_visible()
        dashes = people.locator('.fx-pline').evaluate_all('ls => ls.map(l => getComputedStyle(l).strokeDasharray)')
        assert len(set(dashes)) == 3, dashes  # told apart without colour
        # The people are the shared key's entries (dom.ts legend, LegendUse): each line stamped, each entry usable.
        names = assert_legend_matches(page, root='[data-section=people]')
        assert sum(n.startswith('p:') for n in names) == 3, names
        legend = people.locator('.view-legend').get_by_role('button', name=re.compile('^Staff 01\\(test\\): 3 duties'))
        legend.hover()
        expect(people.locator('svg.fx-focus')).to_have_count(1)
        legend.click()
        expect(page.get_by_role('region', name='Duties · Staff 01(test)').locator('tbody tr')).to_have_count(3)
        page.keyboard.press('Escape')
        people.locator('summary', has_text='as a table').click()
        expect(people.locator('.fx-table tbody tr')).to_have_count(3)
        # Choose whose lines: the shared condition builder.
        people.get_by_role('button', name=re.compile('^People: furthest from share')).click()
        pop = page.get_by_role('dialog', name='People to draw')
        condition(pop, 'Person', ['Staff 01(test)', 'Staff 02(test)'])
        pop.get_by_role('button', name='Apply').click()
        people = section(page, 'people')
        expect(people.locator('.fx-series')).to_have_count(2)
        expect(people.get_by_role('button', name=re.compile('^People: Person is any of'))).to_be_visible()
        # Group by what the work was: clinics, then count only one of them.
        group_by(page, 'Clinic')
        groups = section(page, 'distribution').locator('.fx-groups')
        clinic = groups.get_by_role('button', name=re.compile('^Continuity clinic: 6 duties by 3 people'))
        clinic.click()
        expect(page.locator('.fx-scope')).to_contain_text('Clinics: Continuity clinic')
        expect(section(page, 'distribution').locator('.fx-groups [aria-pressed=true]')).to_have_count(1)
        # A clinic group with a location filter cannot be cut: the page says how to fix it instead of failing.
        page.get_by_role('button', name=re.compile('^Filter')).click()
        pop = page.get_by_role('dialog', name='Filter')
        pop.get_by_role('button', name='Clear all filters').click()  # the popover stays open, emptied
        expect(page.get_by_role('button', name='Filter', exact=True)).to_be_visible()
        expect(page.locator('.fx-scope')).to_be_hidden()
        pop.locator('.field', has_text='or locations').locator('.pick-button').click()
        pop.locator('.picker').get_by_role('option', name='North consulting room', exact=True).check()
        page.keyboard.press('Escape')
        expect(pop.locator('.picker')).to_have_count(0)
        expect(pop.get_by_role('checkbox', name='Include locations inside')).to_be_checked()
        expect(page.locator('.fx-scope')).to_contain_text('Locations: North consulting room and inside')
        expect(section(page, 'distribution').locator('.fx-groups')).to_contain_text('To group by clinic')
        group_by(page, 'Location')
        expect(section(page, 'distribution').locator('.fx-groups')).to_contain_text('North consulting room')
        assert not errors, errors
    finally:
        context.close()


def test_custom_dates_reload_once_they_rest(browser, live_server, h):
    """Typing a date fires input per keystroke; the dashboard reloads once, after the dates rest, and keeps focus."""
    published_month(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page,'insights/fairness')
        october(page)
        expect(page.locator('.fx-page[data-ready=true]')).to_have_count(1)
        reads = []
        page.on('request', lambda r: '/api/fairness/goals' in r.url and reads.append(r.url))
        until = page.get_by_label('Until (included)', exact=True)
        until.select_text()
        page.keyboard.type('31/10/2028')  # quick keystrokes, each an input
        expect(page.locator('.fx-page[data-ready=true]')).to_have_count(1)
        page.wait_for_timeout(1000)
        assert len(reads) == 1, reads
        expect(until).to_be_focused()
        assert 'ends_on=2028-11-01' in reads[0], reads
        expect(page.locator('.fx-page [role=alert]')).to_have_count(0)
        # Leaving the field applies at once.
        until.select_text()
        page.keyboard.type('31/10/2029')
        page.get_by_label('Measure', exact=True).focus()
        expect(page.locator('.fx-page[data-ready=true]')).to_have_count(1)
        expect(until).to_have_value('Wed 31 Oct 2029')
        assert len(reads) == 2 and 'ends_on=2029-11-01' in reads[1], reads
    finally:
        context.close()


def test_saved_views_group_by_and_density(browser, live_server, h):
    a, b, c = published_month(h)
    goal(h, metric='pair_exposure', aim='min_per_pair', amount=2, period='month',
         partners={'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': [b, c]}]})
    # The nearest rota is week 1: Alex has met both partners; Jordan and Taylor have not met.
    report = h.get('coordinator', '/api/fairness/goals?starts_on=2026-10-01&ends_on=2026-11-01')
    assert report['workspace']['starts_on'] == '2026-10-04'
    pair_goal, = [g for g in report['goals'] if g['metric'] == 'pair_exposure']
    assert {(r['person_id'], r['partner_id']) for r in pair_goal['pairs'] if r['status'] != 'within'} == {(b, c), (c, b)}
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page,'insights/fairness')
        october(page)
        expect(section(page, 'goals')).to_contain_text('2 pairs outside the band')  # a pair goal counts pairs, not people
        page.get_by_label('Measure', exact=True).select_option('overtime_duties')
        group_by(page, 'Rank')
        groups = section(page, 'distribution').locator('.fx-groups')
        expect(groups).to_contain_text('Demo practitioners')
        page.get_by_role('button', name=re.compile('^View settings')).click()
        page.get_by_role('radio', name='Compact').click()
        page.get_by_role('checkbox', name='Exposure').uncheck()
        page.keyboard.press('Escape')
        expect(page.locator('.fx-page.compact')).to_be_visible()
        expect(page.locator('[data-section=exposure]')).to_have_count(0)
        expect(page.get_by_role('button', name=re.compile('^View settings'))).to_have_class(re.compile(r'\bon\b'))  # not the default view
        page.get_by_role('button', name=re.compile('^Saved views')).click()
        page.get_by_role('menuitem', name='New view…').click()
        dialog = page.get_by_role('dialog', name='New view')
        dialog.get_by_label('View name', exact=True).fill('Overtime by rank')
        dialog.get_by_role('button', name='Save view').click()
        expect(page.get_by_role('button', name='Saved views: Overtime by rank')).to_be_visible()
        view = next(v for v in h.get('coordinator', '/api/table-views?table=fairness')['views'] if v['name'] == 'Overtime by rank')
        assert view['definition']['fairness']['metric'] == 'overtime_duties' and view['definition']['fairness']['density'] == 'compact'
        # Back to the default view, then the saved one again.
        page.get_by_role('button', name=re.compile('^Saved views')).click()
        page.get_by_role('menuitemradio', name='Default view').click()
        expect(page.get_by_label('Measure', exact=True)).to_have_value('duties')
        page.get_by_role('button', name=re.compile('^Saved views')).click()
        page.get_by_role('menuitemradio', name='Overtime by rank').click()
        expect(page.get_by_label('Measure', exact=True)).to_have_value('overtime_duties')
        expect(page.locator('[data-section=exposure]')).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (1440, 'dark'), (390, 'light'), (390, 'dark')])
def test_staff_see_only_their_own_numbers_and_phone_fits(browser, live_server, h, width, scheme):
    long_name = 'Alexandra Fictional Department A Training Registrar Morgan'
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, h.seed['people'][0]).display_name = long_name
    published_month(h)
    context, page = sign_in(browser, live_server, 'staff', viewport={'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page,'insights/fairness')
        october(page)
        expect(page.locator('[data-section=score]')).to_have_count(0)
        dist = section(page, 'distribution')
        expect(dist.locator('.fx-drow')).to_have_count(1)
        expect(dist).to_contain_text(long_name)
        expect(section(page, 'exposure').locator('tbody tr')).to_have_count(1)
        goals = section(page, 'goals')
        expect(goals).not_to_contain_text('No trend yet')  # staff never get the goal score or trend
        # Planner-only controls are not offered: grouping, people conditions, the rows of the matrix.
        expect(page.get_by_role('button', name=re.compile('^Group'))).to_have_count(0)
        expect(page.get_by_role('button', name=re.compile('^Sort'))).to_have_count(0)  # one row of their own
        expect(page.get_by_label('Find a person', exact=True)).to_have_count(0)
        expect(page.locator('.app-footer .page-status')).not_to_contain_text('Schedule score')
        expect(page.get_by_role('button', name=re.compile('^Rows:'))).to_have_count(0)
        # Staff keep four sidebar pages; Insights is a link on Home (and Ctrl K).
        expect(page.locator('#department-nav [data-route=insights]')).to_have_count(0)
        assert page.evaluate('document.scrollingElement.scrollWidth <= innerWidth + 1')
        own = dist.get_by_role('button', name=re.compile('^' + re.escape(long_name) + ': 3 duties'))
        own.focus()
        expect(own).to_be_focused()
        own.press('Enter')
        drill = page.get_by_role('region', name='Duties · ' + long_name, exact=True)
        expect(drill.locator('tbody tr')).to_have_count(3)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        drill.get_by_role('button', name='Close panel', exact=True).focus()
        page.keyboard.press('Escape')
        expect(drill).to_have_count(0)
        expect(own).to_be_focused()
    finally:
        context.close()


def test_empty_department_shows_one_published_only_empty_state(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page,'insights/fairness')
        expect(page.get_by_role('heading', name='Nothing published yet', exact=True)).to_be_visible()
        expect(page.locator('.fx-page .empty-state')).to_have_count(1)
        expect(page.locator('.fx-section, .fx-drow, table.fx-heat')).to_have_count(0)
        # Nothing to filter, group or sort yet: no toolbar, one way forward (I-01, I-02).
        expect(page.get_by_label('Period', exact=True)).to_have_count(0)
        expect(page.get_by_role('button', name=re.compile('^View settings'))).to_have_count(0)
        page.locator('.fx-page').get_by_role('button', name='Open Schedule', exact=True).click()
        expect(page).to_have_url(re.compile(r'#schedule'))
    finally:
        context.close()


@pytest.mark.parametrize('select_empty', [False, True], ids=['default', 'selected-empty'])
def test_fairness_reaches_the_next_published_rota(browser, live_server, h, select_empty):
    goal(h, metric='duties', period='rota')
    for name, month in [('Later published rota', '2027-04'), ('Next published rota', '2027-03')]:
        rota = h.workspace(name, month + '-01', month + '-08')
        rota = h.shift(rota, start=month + '-02T08:00:00+03:00', end=month + '-02T12:00:00+03:00')['workspace']
        publication = h.publish(rota)['publication_id']
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        navigate(page, 'insights/fairness')
        period = page.get_by_label('Period', exact=True)
        if select_empty:
            period.select_option('next_3_months')
            score = section(page, 'score')
            goals = section(page, 'goals')
            expect(score).to_contain_text('No published rota in this period.')
            expect(goals).to_contain_text('No published rota in this period.')
            expect(goals.locator('.fx-goal')).to_have_count(0)
            expect(period).to_have_value('next_3_months')
            expect(page.get_by_role('button', name='Next month', exact=True)).to_be_visible()
            score.get_by_role('button', name='Show upcoming published rota', exact=True).press('Enter')

        expect(period).to_have_value('custom')
        expect(section(page, 'goals').locator('.fx-goal').first).to_be_visible()
        section(page, 'score').get_by_role('button', name='Next published rota', exact=True).click()
        expect(page).to_have_url(re.compile(r'[?&]source=' + re.escape(publication)))
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
    finally:
        context.close()


@pytest.mark.parametrize('role,width,scheme', [('coordinator', 1440, 'light'), ('staff', 390, 'dark')])
def test_checked_draft_and_modern_fair_share_appear_only_after_publication(browser, live_server, h, role, width, scheme):
    from .test_browser_rule_catalogue import catalogue_rule, line
    policy = catalogue_rule(h, 'fair_share', [line({'apply': {'value': None, 'level': 'medium'}})],
                            settings={'balance_goal': {'metric': 'duties', 'period': 'rota'}})
    w = session(h, h.workspace('Checked October draft'), 4, [h.seed['people'][0]])
    w = h.lifecycle(w, 'validate')['workspace']
    draft = h.get('coordinator', '/api/fairness/goals?workspace_id=' + w['id'])
    assert draft['schedule'] is not None and len(draft['goals']) == 1
    assert draft['goals'][0]['policy_id'] == policy['id']
    assert h.get(role, '/api/fairness/goals')['workspace'] is None
    context, page = sign_in(browser, live_server, role, viewport={'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'insights/fairness')
        dashboard = page.locator('.fx-page')
        expect(dashboard.get_by_role('heading', name='Nothing published yet', exact=True)).to_be_visible()
        expect(dashboard).to_contain_text('once a rota is published')
        # Open Schedule for every role that can open it, staff included (LIVE-11; it replaced review #556's planners-only).
        expect(dashboard.get_by_role('button', name='Open Schedule', exact=True)).to_have_count(1)
        expect(dashboard).not_to_contain_text('No balance goals')
        expect(dashboard).not_to_contain_text('No score yet')
        expect(dashboard.locator('.fx-section')).to_have_count(0)
        expect(page.locator('.app-footer .page-status')).not_to_contain_text('Schedule score')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        h.publish(w)
        page.reload()
        october(page)
        expect(dashboard.get_by_role('heading', name='Nothing published yet', exact=True)).to_have_count(0)
        expect(section(page, 'goals').locator('.fx-goal')).to_have_count(1)
        expect(section(page, 'distribution').get_by_role('button', name=re.compile('^Staff 01\\(test\\): 1 dut'))).to_be_visible()
        if role == 'coordinator':
            # One published rota: no one-dot chart and no key for it (QA-043), its score and band in words instead.
            score = section(page, 'score')
            expect(score.locator('.fx-point, .fx-trend')).to_have_count(0)
            expect(score.locator('.fx-one')).to_contain_text(re.compile(r'scored \d+ of 100: (85 or more|60 to 84|below 60|not publishable)'))
            expect(score.locator('.view-legend:visible')).to_have_count(0)
        else:
            expect(dashboard.locator('[data-section=score]')).to_have_count(0)
            expect(section(page, 'distribution').locator('.fx-drow')).to_have_count(1)
            expect(section(page, 'goals')).not_to_contain_text('Staff 02(test)')
    finally:
        context.close()


@pytest.mark.parametrize('role', ['coordinator', 'staff'])
def test_hidden_goals_still_wait_for_the_publication_source_and_offer_retry(browser, live_server, h, role):
    context, page = sign_in(browser, live_server, role)
    held, period = [], []  # every held goals read; those asked for the new period
    hold = lambda route: (held.append(route), period.append(route) if route.request.url != held[0].request.url else None)
    pattern = '**/api/fairness/goals**'
    try:
        # The toolbar shows while the publication source is read (with nothing published it then goes, I-01).
        page.route(pattern, hold)
        page.evaluate("location.hash = 'insights/fairness'")
        wait_held(page, held)
        if role == 'coordinator':  # staff have no View settings (I-19): their goals section stays shown
            page.get_by_role('button', name=re.compile('^View settings')).click()
            page.get_by_role('checkbox', name='Schedule score', exact=True).uncheck()
            page.get_by_role('checkbox', name='Balance goals', exact=True).uncheck()
            page.keyboard.press('Escape')
        page.get_by_label('Period', exact=True).select_option('next_3_months')
        wait_held(page, period)  # the redraw asks again, for the new period
        for route in held:
            if route is not period[-1]:
                route.fulfill(status=500, content_type='application/json', body='{"error":{"code":"INTERNAL","message":"Superseded."}}')
        held[:] = [period[-1]]
        # Other sections settle, but this required source still owns the final report state.
        expect(page.locator('.fx-section[aria-busy=true]')).to_have_count(0 if role == 'coordinator' else 1)  # staff: the goals section waits
        expect(page.locator('.fx-section')).to_have_count(4 if role == 'coordinator' else 5)
        expect(page.locator('.fx-page')).not_to_have_attribute('data-ready', 'true')
        held.pop().fulfill(status=500, content_type='application/json',
                           body='{"error":{"code":"INTERNAL","message":"The server is busy."}}')
        expect(page.locator('.fx-page').get_by_role('alert')).to_contain_text(('Couldn’t load the fairness report.' if role == 'coordinator' else 'Couldn’t load balance goals.') + ' The server is busy.')
        expect(page.locator('.fx-page')).to_have_attribute('data-ready', 'true')
        page.unroute(pattern, hold)
        page.locator('.fx-page').get_by_role('button', name='Retry' if role == 'coordinator' else 'Retry loading balance goals', exact=True).click()
        expect(page.get_by_role('heading', name='Nothing published yet', exact=True)).to_be_visible()
        expect(page.locator('.fx-section, .fx-page [role=alert]')).to_have_count(0)
    finally:
        context.close()


def test_a_section_that_did_not_load_keeps_its_heading_and_retries(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator')
    failing = {'on': True}
    page.route('**/api/fairness/**', lambda route: route.fulfill(status=500, content_type='application/json',
                                                                 body='{"error":{"code":"INTERNAL","message":"The server is busy."}}')
               if failing['on'] else route.continue_())
    try:
        page.evaluate("location.hash = 'insights/fairness'")  # navigate() would refuse the error notes this test wants
        exposure = section(page, 'exposure')
        expect(exposure.get_by_role('heading', name='Exposure')).to_be_visible()  # which section failed is still said
        expect(exposure).to_contain_text('Couldn’t load exposure. The server is busy.')
        failing['on'] = False
        exposure.get_by_role('button', name='Retry loading exposure', exact=True).click()
        expect(page.get_by_role('heading', name='Nothing published yet', exact=True)).to_be_visible()
        expect(page.locator('.fx-section, .fx-page .notice')).to_have_count(0)  # the successful source replaces all failed sections
    finally:
        context.close()


def test_compare_drafts_shows_parts_goals_and_people(browser, live_server, h):
    a, b, c = h.seed['people']
    goal(h, metric='duties', period='rota')
    first = session(h, h.workspace('Draft A, the long-named October option'), 4, [a, b])
    second = session(h, h.workspace('Draft B, the long-named October option'), 4, [a])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        h.get('coordinator', f"/api/workspaces/{first['id']}/compare?other_id={second['id']}")
        navigate(page, 'planner')
        planner_tool(page, 'Compare side by side')
        dialog = page.locator('#schedule-view')  # Side by side: the comparison replaces the view, inline
        other = dialog.locator('select option', has_text='Draft').first.get_attribute('value')
        dialog.get_by_label('Compare with', exact=True).select_option(other)
        compare = dialog.locator('.fx-compare')
        expect(compare).to_contain_text('Score parts')
        expect(compare).to_contain_text('Coverage')
        expect(compare).to_contain_text('Balance goals')
        # Every column, the change included, shows without scrolling the tables sideways.
        assert compare.locator('.table-scroll').evaluate_all('ts => ts.every(t => t.scrollWidth <= t.clientWidth + 1)')
        page.set_viewport_size({'width': 390, 'height': 844})  # phone: nothing wider than the sheet (tables scroll inside)
        page.wait_for_timeout(100)
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1'), 'compare overflows at 390px'
    finally:
        context.close()


@pytest.fixture
def big_department_dashboard(browser, live_server, h):
    from .test_fairness_dashboard import big_department
    big_department(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page,'insights/fairness')
        page.get_by_label('Period', exact=True).select_option('custom')
        page.get_by_label('From', exact=True).fill('2025-10-01')
        page.get_by_label('Until (included)', exact=True).fill('2026-09-30')
        page.get_by_label('Until (included)', exact=True).blur()  # leaving the field applies the dates at once
        expect(page.locator('.fx-page[data-ready=true]')).to_have_count(1, timeout=120_000)
        built = int(page.locator('.fx-page').get_attribute('data-render-ms'))
        print('fairness dashboard build ms', built)
        yield page, built
    finally:
        context.close()


def test_big_department_renders_quickly(big_department_dashboard):
    """150 people and a year of publications: every section draws; the heatmap shows the 30 x 30 people with the most
    sessions together (not 153 x 153 cells) and says so."""
    page, _ = big_department_dashboard
    expect(page.locator('.fx-page [role=alert]')).to_have_count(0)
    expect(page.locator('.fx-drow')).to_have_count(153)
    expect(page.locator('table.fx-heat tbody tr')).to_have_count(30)
    expect(page.locator('table.fx-heat thead th.fx-col:not(.fx-total)')).to_have_count(30)
    expect(page.locator('[data-section=exposure] .fx-legend')).to_contain_text('of 153 × 153')
    expect(page.locator('[data-section=score] .fx-point')).to_have_count(12)
    # Long names end in … inside each goal card; the counts beside them stay whole.
    spill = page.locator('.fx-goal').evaluate_all('''cards => cards.filter(c => { const r = c.getBoundingClientRect().right;
        return [...c.querySelectorAll('.fx-link')].some(b => b.getBoundingClientRect().right > r + 1
            || b.querySelector('small').getBoundingClientRect().right > b.getBoundingClientRect().right + 1); }).length''')
    assert spill == 0, spill


@pytest.mark.wall_benchmark
def test_big_department_dashboard_cpu_budget(big_department_dashboard):
    # The page's own build time excludes network delay on a busy CI runner.
    _, built = big_department_dashboard
    assert built < 5000, built


def test_staff_empty_goals_do_not_send_them_to_rules(browser, live_server, h):
    [seeded] = h.get('admin', '/api/policies?status=active')['policies']
    assert seeded['definition']['rule']['id'] == 'fair_share'
    h.command('approver', 'policy.retire', {'reason': 'Exercise fairness without goals'}, seeded['id'], seeded['revision'])
    h.publish(session(h, h.workspace(), 4, [h.seed['people'][0]]))
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page,'insights/fairness')
        goals = section(page, 'goals')
        expect(goals).to_contain_text('Your coordinator has not set any goals')
        expect(goals).not_to_contain_text('under Rules')
    finally:
        context.close()


NODE_BUDGET = 4_500  # 153 people, a year: 4,866 nodes with eager tables, 3,732 lazy (PI-4)


CONTRAST = """td => {
  const rgb = c => { const n = c.match(/[\\d.]+/g).map(Number); return c.startsWith('color(') ? n.slice(0, 3).map(v => v * 255) : n.slice(0, 3); };
  const lum = c => { const [r, g, b] = rgb(c).map(v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; }); return .2126 * r + .7152 * g + .0722 * b; };
  return [1, 2, 3, 4, 5].map(k => { td.className = 'heat-' + k; const s = getComputedStyle(td);
    const [a, b] = [lum(s.color), lum(s.backgroundColor)].sort((x, y) => y - x); return Math.round((a + .05) / (b + .05) * 100) / 100; });
}"""


def test_heatmap_numbers_are_legible_in_both_themes(browser, live_server, h):
    """Every heat level keeps its number at 4.5:1 or better, light and dark."""
    published_month(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page,'insights/fairness')
        october(page)
        cell = section(page, 'exposure').get_by_role('button', name='Staff 01(test) with Staff 02(test): 2')
        for scheme in ('light', 'dark'):
            page.emulate_media(color_scheme=scheme)
            ratios = cell.evaluate(CONTRAST)
            assert min(ratios) >= 4.5, (scheme, ratios)
    finally:
        context.close()


@pytest.mark.parametrize('role, viewport', [('auditor', None), ('programme_lead', {'width': 390, 'height': 844})])
def test_auditors_and_programme_leads_read_everyone_and_the_wishes(browser, live_server, h, role, viewport):
    """Read-only: every person's numbers, the score, the wishes, and no way to add a goal or a wish. A programme lead
    has no published-rota page, so the drill-down offers no link there."""
    from .governance_support import identity
    if role == 'programme_lead':
        identity(h, role, ['programme_lead'])
    published_month(h)
    context, page = sign_in(browser, live_server, role, viewport=viewport)
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        navigate(page,'insights/fairness')
        october(page)
        expect(section(page, 'score')).to_contain_text('Schedule score')
        expect(section(page, 'goals').locator('.fx-goal')).to_have_count(2)  # authored duties goal plus seeded workload goal
        dist = section(page, 'distribution')
        expect(dist.locator('.fx-drow')).to_have_count(3)
        dist.get_by_role('button', name=re.compile('^Staff 01\\(test\\): 3 duties')).click()
        drill = page.get_by_role('region', name='Duties · Staff 01(test)')
        expect(drill.locator('tbody tr')).to_have_count(3)
        # Both read the Schedule, so both can open the published rota behind a duty.
        expect(drill.get_by_role('button', name='Open rota')).to_have_count(2)
        page.keyboard.press('Escape')
        wishes = page.get_by_role('region', name='Wishes')
        expect(wishes.get_by_label('Whose wishes', exact=True)).to_be_visible()
        expect(wishes.get_by_role('button', name='Add a wish')).to_have_count(0)
        expect(page.get_by_role('button', name='Open rules')).to_have_count(0)
        assert page.evaluate('document.scrollingElement.scrollWidth <= innerWidth + 1')
        assert not errors, errors
    finally:
        context.close()


def test_overtime_shares_and_workload_show_the_hours_each_person_offered(browser, live_server, h):
    a, b, _ = h.seed['people']
    offer(h, a, '2026-10-04', '2026-10-05', '08:00', '12:00')
    offer(h, b, '2026-10-04', '2026-10-05', '08:00', '16:00')
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    w = h.shift(h.workspace('October week 1', '2026-10-04', '2026-10-11'), start, end,
        people=[{'person_id': p, 'start': start, 'end': end, 'overtime': True} for p in (a, b)])['workspace']
    h.publish(w)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'insights/fairness')
        october(page)
        page.get_by_label('Measure', exact=True).select_option('overtime_hours')
        table = section(page, 'distribution').locator('details.fx-table')
        table.locator('summary').click()
        expect(table.locator('th')).to_contain_text(['Person', 'Overtime offered (h)'])
        expect(table.locator('tr').filter(has_text='Staff 01(test)').locator('td').nth(1)).to_have_text('4')
        expect(table.locator('tr').filter(has_text='Staff 02(test)').locator('td').nth(1)).to_have_text('8')
        page.get_by_label('Measure', exact=True).select_option('duties')
        table = section(page, 'distribution').locator('details.fx-table')
        table.locator('summary').click()  # built when opened (perf PI-4)
        expect(table.locator('th')).to_contain_text(['Person', 'Days available'])
        navigate(page, 'insights/workload')
        workload = page.locator('#main table')
        expect(workload.get_by_role('columnheader', name=re.compile('Overtime offered'))).to_be_visible()
        expect(workload.locator('tr').filter(has_text='Staff 02(test)')).to_contain_text('8')
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'staff')  # Alex: their own line, not a table
    try:
        navigate(page, 'insights/workload')
        expect(page.locator('.workload-mine')).to_contain_text('4 h overtime offered')
    finally:
        context.close()


FONT_ON_SCREEN = "n => n.getScreenCTM().a * parseFloat(getComputedStyle(n).fontSize)"


def test_charts_and_rows_stay_legible_beside_the_drill_panel(browser, live_server, h):
    """I-06, I-10, I-18: chart ticks keep at least 11px on screen while the drill-down panel narrows the page; the
    drill table shows day, time and duty; the expected band is drawn in both themes."""
    published_month(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page,'insights/fairness')
        october(page)
        dist = section(page, 'distribution')
        dist.get_by_role('button', name=re.compile('^Staff 01\\(test\\): 3 duties')).click()
        drill = page.get_by_role('region', name='Duties · Staff 01(test)')
        expect(drill.locator('tbody tr')).to_have_count(3)
        expect(drill.locator('section.fx-drill').first.locator('thead th')).to_have_text(['Day', 'Time', 'Duty'])
        expect(drill.locator('tbody tr').first.locator('td').nth(1)).to_have_text(re.compile(r'^\d\d:\d\d–\d\d:\d\d$'))
        ticks = page.locator('.fx-page .fx-tick')
        expect(ticks.first).to_be_visible()
        sizes = ticks.evaluate_all('nodes => nodes.map(' + FONT_ON_SCREEN + ')')
        assert sizes and min(sizes) >= 10.9, sizes
        drill.get_by_role('button', name='Close panel').click()
        band = dist.locator('.fx-drow .fx-band').first
        for scheme in ('light', 'dark'):
            page.emulate_media(color_scheme=scheme)
            assert band.evaluate('n => getComputedStyle(n).boxShadow') != 'none'
    finally:
        context.close()


def test_phone_rows_keep_their_status_inside_the_row(browser, live_server, h):
    """I-27: at 390px every distribution row shows its status mark inside the row, not cut off."""
    published_month(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 390, 'height': 844})
    try:
        navigate(page,'insights/fairness')
        october(page)
        rows = section(page, 'distribution').locator('.fx-drow')
        expect(rows.first).to_be_visible()
        outside = rows.evaluate_all("""rows => rows.filter(r => { const s = r.querySelector('.fx-status'); if (!s || !s.textContent && !s.firstElementChild) return false;
          const a = r.getBoundingClientRect(), b = s.getBoundingClientRect(); return b.width === 0 || b.right > a.right + 1 || b.left < a.left - 1; }).length""")
        assert outside == 0
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        context.close()


def test_find_reuses_the_drawn_rows(browser, live_server, h):
    """Perf PI-4: searching and clearing the search shows the same row nodes again instead of building new ones."""
    published_month(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page,'insights/fairness')
        october(page)
        dist = section(page, 'distribution')
        expect(dist.locator('.fx-drow')).to_have_count(3)
        dist.locator('.fx-drow').evaluate_all('rows => rows.forEach(r => { r.__kept = true; })')
        page.get_by_label('Find a person', exact=True).fill('staff 01')
        expect(dist.locator('.fx-drow')).to_have_count(1)
        page.get_by_label('Find a person', exact=True).fill('')
        expect(dist.locator('.fx-drow')).to_have_count(3)
        assert dist.locator('.fx-drow').evaluate_all('rows => rows.every(r => r.__kept === true)')
    finally:
        context.close()


def test_big_department_builds_tables_only_when_opened(big_department_dashboard):
    """Perf PI-4 budget: the "as a table" views stay unbuilt until opened, which keeps the page's DOM small."""
    page, _ = big_department_dashboard
    expect(page.locator('details.fx-table')).not_to_have_count(0)
    expect(page.locator('details.fx-table table')).to_have_count(0)
    nodes = page.evaluate("document.querySelectorAll('.fx-page *').length")
    print('fairness dashboard nodes', nodes)
    assert nodes < NODE_BUDGET, nodes
    table = page.locator('details.fx-table').first
    table.locator('summary').click()
    expect(table.locator('table')).to_have_count(1)


@pytest.mark.parametrize('width', [1440, 390])
def test_long_measure_headers_fit_their_columns(browser, live_server, h, width):
    """I-27 / I-29 (review #556): a long measure name heading the value column (Overtime duties, Overtime hours) stays
    inside its column: it never runs into Expected, and both headers stay over their numbers."""
    published_month(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900})
    try:
        navigate(page,'insights/fairness')
        october(page)
        for measure in ('overtime_duties', 'overtime_hours'):  # both counted for Alex (published_month)
            page.get_by_label('Measure', exact=True).select_option(measure)
            dist = section(page, 'distribution')
            head = dist.locator('.fx-dhead')
            expect(head).to_be_visible()
            boxes = head.evaluate("""h => [...h.children].map(s => { const r = s.getBoundingClientRect(), c = document.createRange();
              c.selectNodeContents(s); const t = c.getBoundingClientRect(); return {left: r.left, right: r.right, textLeft: t.left, textRight: t.right, shown: r.width > 0}; })""")
            value, expected = boxes[2], boxes[3]
            # The text stays inside its own cell, so the two headers never touch.
            assert value['textRight'] <= value['right'] + 1 and value['textLeft'] >= value['left'] - 1, (measure, value)
            assert expected['textLeft'] >= expected['left'] - 1 and value['textRight'] < expected['textLeft'], (measure, value, expected)
            row = dist.locator('.fx-drow').first
            if row.count():
                cells = row.evaluate("r => ['.fx-val', '.fx-expv'].map(s => r.querySelector(s).getBoundingClientRect().right)")
                assert abs(cells[0] - value['right']) <= 2 and abs(cells[1] - expected['right']) <= 2, (measure, cells, value, expected)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        context.close()


def test_goal_cards_are_named_once_with_their_period(browser, live_server, h):
    """LIVE-42: one rule's per-week targets read "<rule> · <week>", never "<rule> target target" five times alike."""
    import json
    published_month(h)
    context, page = sign_in(browser, live_server, 'coordinator')

    def weekly(route):
        reply = route_fetch(route)
        data = reply.json()
        if data.get('goals'):
            template = data['goals'][0]
            data['goals'] = [{**template, 'goal_id': f'{template["goal_id"]}:{n}', 'name': 'Weekly clinical target target',
                              'starts_on': start, 'ends_on': end} for n, (start, end) in enumerate([('2026-10-04', '2026-10-11'), ('2026-10-11', '2026-10-18')])]
        route.fulfill(response=reply, body=json.dumps(data))

    try:
        page.route(re.compile(r'/api/fairness/goals\?'), weekly)
        navigate(page, 'insights/fairness')
        october(page)
        names = page.locator('.fx-goal-name h3')
        expect(names).to_have_count(2)
        assert names.all_text_contents() == ['Weekly clinical target · 4 Oct 2026 – 10 Oct 2026', 'Weekly clinical target · 11 Oct 2026 – 17 Oct 2026']
    finally:
        context.close()


def test_all_people_outside_a_goal_band_open_in_the_dock(browser, live_server, h):
    """C26: "All N" under a Balance goal docks the list beside the page (never a modal); a long list has the panel
    search; Esc clears the search first, then closes the panel, and focus goes back to "All N"."""
    import json
    published_month(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    names = ['Avery Stone', 'Blake Rowe', 'Casey Hart', 'Drew Lane', 'Emery Fox', 'Finley Moss', 'Gray Pike', 'Harper Vale', 'Quinn Reed']

    def many(route):
        reply = route_fetch(route)
        data = reply.json()
        for g in data.get('goals') or []:
            g['people'] = [{'person_id': f'invented-{n}', 'name': name, 'actual': 9, 'expected': 3, 'status': 'above'} for n, name in enumerate(names)]
            g['pairs'] = []
        route.fulfill(response=reply, body=json.dumps(data))

    try:
        page.route(re.compile(r'/api/fairness/goals\?'), many)
        navigate(page, 'insights/fairness')
        october(page)
        opener = section(page, 'goals').get_by_role('button', name=f'All {len(names)}').first
        opener.click()
        expect(page.locator('dialog[open]')).to_have_count(0)
        dock = page.locator('.side-panel.dock-panel')
        expect(dock).to_be_visible()
        expect(dock).to_contain_text(f'{len(names)} outside the band')
        find = dock.get_by_role('searchbox', name='Find a person')
        expect(find).to_be_focused()
        items = dock.locator('.fx-list > li')
        expect(items).to_have_count(len(names))
        find.fill('quinn')
        expect(items.filter(visible=True)).to_have_count(1)
        expect(items.filter(visible=True)).to_contain_text('Quinn Reed')
        find.fill('nobody here')
        expect(dock.get_by_text('No one matches')).to_be_visible()
        page.keyboard.press('Escape')
        expect(find).to_have_value('')
        expect(items.filter(visible=True)).to_have_count(len(names))
        expect(dock).to_be_visible()
        page.keyboard.press('Escape')
        expect(dock).to_have_count(0)
        expect(opener).to_be_focused()
    finally:
        context.close()

def test_insights_asks_once_per_distribution_url_across_rebuilds_without_console_errors(browser, live_server, h):
    """QA-115: the page rebuilt while its slow reads are on their way (Back, a live update) joins them, and once they
    have answered a revisit asks nothing again: one request per distinct distribution URL. QA-116: its charts redraw
    for their width without a ResizeObserver loop error (or any other console error)."""
    published_month(h)
    context, page = sign_in(browser, live_server, 'auditor')
    asked, errors, held, holding = [], [], [], [True]
    page.on('request', lambda r: asked.append(r.url) if '/api/fairness/distribution?' in r.url else None)
    page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.route('**/api/fairness/distribution?**', lambda route: held.append(route) if holding[0] else route.continue_())
    try:
        page.evaluate("location.hash = 'insights/fairness'")
        wait_held(page, held)
        for key in ('home', 'insights/fairness', 'home', 'insights/fairness'):
            navigate(page, key)
        holding[0] = False
        for route in held:
            route.continue_()
        section(page, 'distribution')
        october(page)  # new dates: new URLs, each asked once, and charts drawn from data
        section(page, 'people')
        expect(page.locator('.fx-fit svg').first).to_be_visible()  # a chart drawn for its box (boxChart)
        navigate(page, 'home')
        navigate(page, 'insights/fairness')
        section(page, 'distribution')
        assert asked and len(asked) == len(set(asked)), asked
        assert errors == []
    finally:
        context.close()
