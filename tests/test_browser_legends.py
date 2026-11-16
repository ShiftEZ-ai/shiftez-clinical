"""Every legend is the shared key (legend() in dom.ts) and lists only what the view draws now: colours from the active
"Colour by", marks such as TL = Team lead, codes when names are hidden; it follows filters and layouts and hides when empty."""
import re
import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate, pick_view, toolbar, close_popover, planner_filter, filter_pick, show_names, view_settings  # noqa: F401
from .test_governed_leadership import activate, proposal, line
from .test_browser_fairness_dashboard import published_month, october, section

from .test_browser import DRAFT_CHIP, draft_chip  # noqa: F401
pytestmark = pytest.mark.browser
DESKTOP, PHONE = None, {'width': 390, 'height': 844}


def two_duties(h, name='Legend rota'):
    """Clinical review (Alex) on Sunday and a led Night cover (Jordan as team lead) on Monday."""
    a, b, _ = h.seed['people']
    night = h.configure('assignment_types', {'name': 'Night cover', 'starts_on': '2020-01-01', 'definition': {
        'colour': 'violet', 'requirements': [{'minimum': 1, 'maximum': 2}]}})['record']['id']
    activate(h, proposal(h, [line(night, [b])]))
    h.configure('assignment_types', {'name': 'Unused duty', 'starts_on': '2020-01-01', 'definition': {'colour': 'amber'}})
    w = h.shift(h.workspace(name), people=[a])['workspace']
    start, end = '2026-10-05T20:00:00+03:00', '2026-10-06T06:00:00+03:00'
    return h.shift(w, start, end, typ=night, people=[{'person_id': b, 'start': start, 'end': end, 'team_lead': True}])['workspace']


def key(page):
    return page.locator('#main .view-legend:visible')


def colour_by(page, label):
    view_settings(page.locator('.planner-toolbar'), 'Colour by').get_by_role('radiogroup', name='Colour by', exact=True).get_by_role(
        'radio', name=label, exact=True).click()
    close_popover(page)


def duty_filter(page, label):
    filter_pick(planner_filter(page), 'Duty type', *([] if label == 'Any' else [label]), only=True)
    close_popover(page)


@pytest.mark.parametrize('viewport,scheme', [(DESKTOP, 'light'), (PHONE, 'dark')], ids=['desktop-light', 'phone-dark'])
def test_planner_legend_follows_filters_colour_by_names_and_layouts(browser, live_server, h, viewport, scheme):
    two_duties(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport)
    page.emulate_media(color_scheme=scheme)
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        navigate(page, 'planner')
        if viewport:  # phones open the day list: its key names the duty colours it paints
            expect(key(page)).to_contain_text('Clinical review session')
            expect(key(page)).to_contain_text('Night cover')
            expect(key(page)).not_to_contain_text('Unused duty')
            page.get_by_role('radiogroup', name='Calendar layout').get_by_role('radio', name='Grid', exact=True).click()
        legend = key(page)
        expect(legend).to_have_count(1)
        expect(legend).to_contain_text('Team lead')
        # A row name does not explain its colour: every drawn hue keeps its key entry.
        for text in ('Clinical review session', 'Night cover'):
            expect(legend).to_contain_text(text)
        expect(legend.locator('abbr.wk-tag.lead')).to_have_text('TL')  # the same tag as on the chip
        expect(page.locator('.wk-grid .pchip abbr.wk-tag.lead')).to_have_text('TL')
        for text in ('Unused duty', 'Overtime', 'Trainee', 'Breaks a rule'):
            expect(legend).not_to_contain_text(text)
        # Filter to one duty type: only its colour, and no team lead mark.
        duty_filter(page, 'Clinical review session')
        expect(legend).to_contain_text('filtered')
        expect(legend).not_to_contain_text('Team lead')
        # Weekend columns remain drawn even when no duty matches the filter.
        duty_filter(page, 'Unused duty')
        expect(legend).to_contain_text('Weekend')
        for text in ('Clinical review session', 'Night cover', 'Unused duty', 'Team lead'):
            expect(legend).not_to_contain_text(text)
        expect(legend.get_by_role('button', name='Change what is shown', exact=True)).to_be_visible()
        duty_filter(page, 'Any')
        expect(legend).to_contain_text('Team lead')
        # Colour by service, then rank: the key follows the drawn hue even when a group names it too.
        colour_by(page, 'Service')
        expect(legend).to_contain_text('Continuity clinic')
        expect(legend).not_to_contain_text('Clinical review session')
        colour_by(page, 'Rank')
        expect(legend).to_contain_text('Demo practitioners')
        colour_by(page, 'Duty type')
        # The owner removed person dictionaries; codes recover full names on hover/focus/open.
        expect(legend).not_to_contain_text('Alex Morgan')
        show_names(page, 'code')
        expect(legend.locator('.code-chip')).to_have_count(0)
        expect(legend).not_to_contain_text('Alex Morgan')
        expect(legend).not_to_contain_text('Jordan Lee')
        chip = page.locator(f'.wk-grid {draft_chip(h.seed["people"][0])}')
        chip.hover()
        expect(page.get_by_role('tooltip')).to_contain_text('Alex Morgan')
        page.mouse.move(-1, -1)
        chip.focus()
        expect(page.get_by_role('tooltip')).to_contain_text('Alex Morgan')
        chip.click()
        expect(page.locator('.wk-panel')).to_contain_text('Alex Morgan')
        page.keyboard.press('Escape')
        expect(legend).not_to_contain_text('Taylor Noor')
        show_names(page, 'full')
        expect(legend.locator('.code-chip')).to_have_count(0)
        # People: duty codes with their names; Coverage: the cell states drawn.
        pick_view(page, 'People')
        expect(key(page)).to_contain_text('Night cover')
        expect(key(page).locator('.pg-chip').filter(has_text='NC')).to_have_count(1)
        expect(key(page)).not_to_contain_text('Unused duty')
        pick_view(page, 'Coverage')
        expect(key(page)).to_contain_text('Clinical review session')
        expect(key(page)).to_contain_text('No session')
        duty_filter(page, 'Night cover')
        expect(key(page)).not_to_contain_text('Clinical review session')
        assert not errors
    finally:
        context.close()


def test_empty_planner_has_no_legend(browser, live_server, h):
    h.workspace('Nothing yet')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        expect(page.locator('.wk-grid')).to_be_visible()
        # An empty grid still draws weekend columns: only that encoding and the settings control belong in its key.
        legend = key(page)
        expect(legend).to_have_count(1)
        expect(legend).to_have_text('Weekend')
        expect(legend.get_by_role('button', name='Change what is shown', exact=True)).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize('viewport,scheme', [(DESKTOP, 'dark'), (PHONE, 'light')], ids=['desktop-dark', 'phone-light'])
def test_published_rota_and_team_calendar_keys_name_the_team_lead(browser, live_server, h, viewport, scheme):
    h.publish(two_duties(h, 'Published legend'))
    context, page = sign_in(browser, live_server, 'coordinator', viewport)
    page.emulate_media(color_scheme=scheme)
    try:
        # Published sessions on the schedule (no draft): the week's key.
        navigate(page, 'schedule/week/2026-10-04?edit=none')
        if viewport:
            expect(key(page)).to_contain_text('Night cover')
            page.get_by_role('radiogroup', name='Calendar layout').get_by_role('radio', name='Grid', exact=True).click()
        expect(key(page)).to_contain_text('Team lead')
        expect(key(page).locator('abbr.wk-tag.lead')).to_have_text('TL')
        expect(key(page)).to_contain_text('Clinical review session')
        show_names(page, 'code')
        chip = page.locator('.wk-grid button.pchip').filter(has=page.locator('.pchip-name')).filter(
            has_text=re.compile(r'.')).and_(page.get_by_role('button', name=re.compile('^Alex Morgan,')))
        chip.hover()
        expect(page.get_by_role('tooltip')).to_contain_text('Alex Morgan')
        page.mouse.move(-1, -1)
        chip.focus()
        expect(page.get_by_role('tooltip')).to_contain_text('Alex Morgan')
        chip.click()
        expect(page.locator('.dock-panel')).to_contain_text('Alex Morgan')
        expect(page.locator('.dock-panel').get_by_role('button', name='Edit session', exact=True)).to_have_count(0)
        page.keyboard.press('Escape')
        # The team calendar is the People view: its key names the duty codes drawn, not unused types.
        navigate(page, 'schedule/people/2026-10-05?edit=none')
        legend = key(page)
        expect(legend).to_contain_text('Night cover')
        expect(legend).to_contain_text('Clinical review session')
        expect(legend).not_to_contain_text('Unused duty')
    finally:
        context.close()


def test_fairness_keys_list_only_drawn_marks(browser, live_server, h):
    published_month(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page,'insights/fairness')
        october(page)
        score = section(page, 'score')
        expect(score.locator('.view-legend li')).to_have_count(score.locator('.fx-point').evaluate_all(
            "ps => new Set(ps.map(p => p.getAttribute('class'))).size"))
        dist = section(page, 'distribution')
        expect(dist.locator('.view-legend')).to_contain_text('Expected share')
        exposure = section(page, 'exposure')
        legend = exposure.locator('.view-legend')
        expect(legend).to_contain_text('Same person')
        expect(legend).to_contain_text(re.compile(r'\d+(–\d+)? sessions? together'))
        levels = exposure.locator('td[class^=heat-]:not(.heat-0)').evaluate_all("ts => new Set(ts.map(t => t.className)).size")
        expect(legend.locator('.lg-swatch')).to_have_count(levels)
    finally:
        context.close()


def test_score_trend_never_draws_a_score_in_the_running_blue(browser, live_server, h):
    """One status palette (audit I-25): blue means "running", so a score of 60–84 is plain (the score pill and the
    trend's point and key), and each drawn point is the colour its key says."""
    published_month(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'insights/fairness')
        october(page)
        score = section(page, 'score')
        expect(score.locator('.fx-point').first).to_be_attached()
        colours = score.evaluate("""async s => {
          const {scoreTone, scorePill} = await window.importBundle('score.js');
          const probe = document.createElement('i'); probe.style.setProperty('color', 'var(--info)'); s.append(probe);
          const blue = getComputedStyle(probe).color; probe.remove();
          const mid = {score: 72, uncapped_score: 72, publishable: true};
          const pill = scorePill('i25', mid, () => '', () => {}); s.append(pill);
          const pillColours = [getComputedStyle(pill).color, getComputedStyle(pill).backgroundColor]; pill.remove();
          const point = document.createElementNS('http://www.w3.org/2000/svg', 'circle'); point.setAttribute('class', 'fx-point ' + scoreTone(mid));
          s.querySelector('svg.fx-trend').append(point); const pointFill = getComputedStyle(point).fill; point.remove();
          const tone = n => [...n.classList].find(c => ['ok','warn','err','info','neutral'].includes(c)) || 'ok';
          const dots = Object.fromEntries([...s.querySelectorAll('.view-legend .lg-dot')].map(d => [tone(d), getComputedStyle(d).backgroundColor]));
          return {blue, midTone: scoreTone(mid), pillColours, pointFill,
                  points: [...s.querySelectorAll('.fx-point')].map(p => ({tone: tone(p), fill: getComputedStyle(p).fill, dot: dots[tone(p)]}))};
        }""")
        assert colours['midTone'] == 'neutral', colours
        assert colours['blue'] not in colours['pillColours'] and colours['pointFill'] != colours['blue'], colours
        for p in colours['points']:
            assert p['tone'] != 'info' and p['fill'] != colours['blue'], p
            assert p['fill'] == p['dot'], p  # the key shows the same colour as the point
    finally:
        context.close()
