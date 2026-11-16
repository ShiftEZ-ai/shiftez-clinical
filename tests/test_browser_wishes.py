"""Staff wishes and work-pattern rules in the browser: staff add wishes within their points, planners see them in the
planner, and the rule form offers the new pattern rules."""
from packages.domain.time import now as clock_now
import re
from datetime import date, timedelta

import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate, open_config, save_dialog, pick_view, wait_held  # noqa: F401
from .test_browser import whole_list

pytestmark = pytest.mark.browser


def next_month():
    """The first day of next month: the wish calendar opens on this month, one click away."""
    today = clock_now().date()
    return date(today.year + today.month // 12, today.month % 12 + 1, 1)


def month_of(page):
    return page.locator('.wish-month').inner_text()


def test_staff_add_wishes_within_their_points(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page,'requests')
        page.get_by_role('button', name='Wish calendar', exact=True).click()
        panel = page.get_by_role('region', name='Wishes')
        expect(panel.get_by_text('0 of 10 points used', exact=False)).to_be_visible()
        # Quick add from a calendar day, the next month (always in the future).
        before = month_of(page)
        panel.get_by_role('button', name='Next month').click()
        expect(page.locator('.wish-month')).not_to_have_text(before)
        day = panel.locator('.wish-day:not(.other-month)').nth(9)
        label = day.get_attribute('aria-label')
        day.click()
        dialog = page.get_by_role('dialog', name='Add a wish')
        dialog.get_by_label('Points', exact=True).select_option('3')
        dialog.get_by_label('Note (optional, seen by planners)', exact=True).fill('Family event')
        dialog.get_by_role('button', name='Add wish', exact=True).click()
        expect(panel.get_by_text('3 of 10 points used', exact=False)).to_be_visible()
        expect(panel.get_by_role('button', name=re.compile(re.escape(label.split(' · ')[0]) + ' · Day off'))).to_be_visible()
        expect(panel.get_by_role('meter')).to_have_attribute('value', '3')
        # A duty-type wish for a range, then one that does not fit the budget is refused with the reason.
        panel.get_by_role('button', name='Add a wish', exact=True).click()
        dialog = page.get_by_role('dialog', name='Add a wish')
        dialog.get_by_label('I would like', exact=True).select_option('prefer_shift_type')
        expect(dialog.get_by_label('Duty type', exact=True)).to_be_visible()
        dialog.get_by_label('Duty type', exact=True).select_option(label=h.get('staff', '/api/wishes')['duty_types'][0]['name'])
        first = dialog.get_by_label('From', exact=True).evaluate('n => n.value')  # ISO; the field shows 'Sat 3 Oct 2026'
        dialog.get_by_label('From', exact=True).fill(first[:8] + '20')
        dialog.get_by_label('To (included)', exact=True).fill(first[:8] + '22')
        dialog.get_by_label('Points', exact=True).select_option('3')
        dialog.get_by_role('button', name='Add wish', exact=True).click()
        expect(panel.get_by_text('6 of 10 points used', exact=False)).to_be_visible()
        for n in (23, 24):
            panel.get_by_role('button', name='Add a wish', exact=True).click()
            dialog = page.get_by_role('dialog', name='Add a wish')
            dialog.get_by_label('From', exact=True).fill(first[:8] + str(n))
            dialog.get_by_label('To (included)', exact=True).fill(first[:8] + str(n))
            dialog.get_by_label('Points', exact=True).select_option('3')
            dialog.get_by_role('button', name='Add wish', exact=True).click()
        expect(dialog.get_by_text('1 of 10 left', exact=False)).to_be_visible()
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        mine = h.get('staff', '/api/wishes')['wishes']
        assert sorted(w['points'] for w in mine) == [3, 3, 3] and any(w['note'] == 'Family event' for w in mine)
        # The list shows each wish; withdrawing frees its points.
        expect(panel.get_by_text('Family event', exact=True)).to_be_visible()
    finally:
        context.close()


def test_large_points_budget_is_a_number_field_not_one_option_per_point(browser, live_server, h):
    from .test_staff_wishes import department_settings
    first = next_month() + timedelta(days=9)
    h.command('admin', 'department.settings', {**department_settings(h), 'generation_defaults': {'scoring': {'wish_budget_points': 2_000_000}}},
              h.seed['department_id'], h.get('admin', '/api/me')['organisation']['revision'])
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'requests')
        page.get_by_role('button', name='Wish calendar', exact=True).click()
        panel = page.get_by_role('region', name='Wishes')
        panel.get_by_role('button', name='Next month').click()
        # The old month's day buttons stay until the new month's wishes have loaded.
        expect(panel.locator('.wish-month')).to_have_text(first.strftime('%B %Y'))
        panel.locator('.wish-day:not(.other-month)').nth(9).click()
        dialog = page.get_by_role('dialog', name='Add a wish')
        points = dialog.get_by_role('spinbutton', name='Points', exact=True)
        expect(points).to_have_attribute('max', '2000000')
        points.fill('1500000')
        dialog.get_by_role('button', name='Add wish', exact=True).click()
        expect(panel.get_by_text('1500000 of 2000000 points used', exact=False)).to_be_visible()
        mine = h.get('staff', '/api/wishes')['wishes']
        assert [w['points'] for w in mine] == [1_500_000]
        assert [(w['first_day'], w['last_day']) for w in mine] == [(first.isoformat(), first.isoformat())]
    finally:
        context.close()

def test_admin_sets_wish_kinds_and_default_points_with_undo(browser, live_server, h):
    from .test_staff_wishes import department_settings
    pattern_settings = {'allowed': False, 'default_points': 4, 'may_repeat': False}
    h.command('admin', 'department.settings', {**department_settings(h), 'wish_settings': {'types': {'whole_weekends': pattern_settings}}},
              h.seed['department_id'], h.get('admin', '/api/me')['organisation']['revision'])
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, 'Auto-schedule & scoring')
        page.get_by_role('tab', name='Staff preferences', exact=True).click()
        # Every kind, the pattern kinds too; weekly repeat only for the dated kinds.
        expect(page.get_by_role('checkbox', name='Whole weekends', exact=True)).not_to_be_checked()
        expect(page.get_by_label('Default points for whole weekends', exact=True)).to_have_value('4')
        expect(page.get_by_role('checkbox', name='Whole weekends may repeat weekly', exact=True)).to_have_count(0)
        expect(page.get_by_role('checkbox', name='Day off may repeat weekly', exact=True)).to_be_checked()
        allowed = page.get_by_role('checkbox', name='Day off', exact=True)
        expect(allowed).to_be_checked()  # defaults keep every kind allowed at 1 point
        expect(page.get_by_label('Default points for day off', exact=True)).to_have_value('1')
        allowed.uncheck()
        expect(page.get_by_label('Default points for day off', exact=True)).to_be_disabled()
        page.get_by_label('Default points for day on duty', exact=True).fill('3')
        page.get_by_role('checkbox', name='Day on duty may repeat weekly', exact=True).uncheck()
        page.locator('.config-pane').get_by_role('button', name='Save defaults', exact=True).click()
        page.get_by_role('dialog').get_by_label('Reason', exact=True).fill('Day-off wishes go through leave')
        page.get_by_role('dialog').get_by_role('button', name='Save defaults', exact=True).click()
        expect(page.get_by_role('button', name='Undo', exact=True)).to_be_visible()
        types = h.get('admin', '/api/me')['department']['wish_settings']['types']
        assert not types['prefer_off']['allowed'] and types['prefer_on']['default_points'] == 3 and not types['prefer_on']['may_repeat']
        assert {key: types['whole_weekends'][key] for key in pattern_settings} == pattern_settings
        assert types['prefer_on']['approval'] == 'none'  # values the form does not show are kept
        page.reload()
        page.get_by_role('tab', name='Staff preferences', exact=True).click()
        expect(page.get_by_role('checkbox', name='Day off', exact=True)).not_to_be_checked()
        expect(page.get_by_label('Default points for day on duty', exact=True)).to_have_value('3')
    finally:
        context.close()
    # Staff: the kind is not offered, and a new wish starts at its kind's default points.
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'requests')
        page.get_by_role('button', name='Wish calendar', exact=True).click()
        panel = page.get_by_role('region', name='Wishes')
        panel.get_by_role('button', name='Add a wish', exact=True).click()
        dialog = page.get_by_role('dialog', name='Add a wish')
        kind = dialog.get_by_label('I would like', exact=True)
        expect(kind.locator('option[value="prefer_off"]')).to_have_count(0)
        expect(kind).to_have_value('prefer_on')
        expect(dialog.get_by_label('Points', exact=True)).to_have_value('3')
        kind.select_option('avoid_shift_type')
        expect(dialog.get_by_label('Points', exact=True)).to_have_value('1')
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, 'Auto-schedule & scoring')
        page.get_by_role('tab', name='Staff preferences', exact=True).click()
        page.get_by_label('Default points for day on duty', exact=True).fill('2')
        page.locator('.config-pane').get_by_role('button', name='Save defaults', exact=True).click()
        page.get_by_role('dialog').get_by_label('Reason', exact=True).fill('Two points by default')
        page.get_by_role('dialog').get_by_role('button', name='Save defaults', exact=True).click()
        page.get_by_role('button', name='Undo', exact=True).click()
        expect(page.get_by_label('Default points for day on duty', exact=True)).to_have_value('3')
        assert h.get('admin', '/api/me')['department']['wish_settings']['types']['prefer_on']['default_points'] == 3
    finally:
        context.close()

def test_coordinator_sees_wishes_in_the_planner(browser, live_server, h):
    person = h.seed['people'][0]
    w = h.shift(h.workspace('Wish rota', '2026-10-04', '2026-10-11'), people=[person])['workspace']
    h.command('staff', 'wish.create', {'person_id': person, 'first_day': '2026-10-06', 'last_day': '2026-10-06', 'kind': 'prefer_off', 'points': 2})
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        page.evaluate("window.location.hash='#planner'")
        expect(page.locator('.wk-day .wk-wish')).to_have_count(1, timeout=20000)  # the key repeats the mark
        expect(page.locator('.wk-day').filter(has=page.locator('.wk-wish'))).to_contain_text('6')
        expect(page.locator('.view-legend')).to_contain_text('Staff wishes on the day')
        # People view: a mark on that person's day, with the wish in words.
        pick_view(page, 'People')
        mark = page.locator('.pg-wish')
        expect(mark).to_have_count(1)
        expect(mark).to_have_attribute('aria-label', 'Wish: day off (2 pts)')
        # Availability lists everyone's wishes for planners.
        navigate(page,'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        page.get_by_role('button', name='Wish calendar', exact=True).click()
        panel = page.get_by_role('region', name='Wishes')
        panel.get_by_label('Whose wishes', exact=True).select_option('')
        expect(panel.locator('.dt')).to_contain_text('Day off')
    finally:
        context.close()
    assert w



def test_planner_chip_marks_a_wish_it_goes_against(browser, live_server, h):
    """The server's wish_effects reach the chip: a struck-through star, words in its label and a key entry."""
    person = h.seed['people'][0]
    h.shift(h.workspace('Wish mark rota', '2026-10-04', '2026-10-11'), people=[person])
    h.command('staff', 'wish.create', {'person_id': person, 'first_day': '2026-10-04', 'last_day': '2026-10-04', 'kind': 'prefer_off', 'points': 1})
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        chip = page.locator('.pchip:not(.gap)').first
        expect(chip.locator('.wk-wish-mark.against svg')).to_have_count(1, timeout=20000)
        expect(chip).to_have_attribute('aria-label', re.compile('goes against their wish'))
        expect(page.locator('.view-legend')).to_contain_text('Goes against their wish')
    finally:
        context.close()

@pytest.mark.parametrize('card,fill,expected', [
    ('Run length', lambda f: f.get_by_label('Longest run (days in a row)', exact=True).fill('5'), {'kind': 'run_length', 'minimum': 2, 'maximum': 5}),
    ('Days off together', lambda f: None, {'kind': 'days_off_run', 'minimum': 2}),
    ('Weekend shape', lambda f: f.get_by_label('Shape of the weekend', exact=True).select_option('identical_types'), {'kind': 'weekend_shape', 'shape': 'identical_types'}),
])
def test_rule_form_offers_work_pattern_rules(browser, live_server, h, card, fill, expected):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        page.get_by_role('button', name='Add rule', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_role('radio', name=card, exact=True).check()
        expect(form.get_by_label('Classification', exact=True)).to_be_disabled()
        expect(form.get_by_label('Classification', exact=True)).to_have_value('advisory')
        fill(form)
        form.get_by_label('Source or departmental decision', exact=True).fill('Constructed browser test')
        save_dialog(form, 'Propose rule')
        expect(page.get_by_text('Rule proposed', exact=False).last).to_be_visible()
        rule = next(p for p in h.get('coordinator', '/api/catalogues')['policies'] if p['definition']['rule']['kind'] == expected['kind'])
        assert {k: rule['definition']['rule'][k] for k in expected} == expected and rule['definition']['severity'] == 'advisory'
    finally:
        context.close()


def test_rule_form_shift_order_and_together_apart(browser, live_server, h):
    late = h.configure('assignment_types', {'name': 'Late', 'starts_on': '2020-01-01', 'definition': {'requirements': [{'minimum': 1}]}})['record']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        page.get_by_role('button', name='Add rule', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_role('radio', name='Shift order', exact=True).check()
        form.get_by_label('Earliest duty', exact=True).select_option(h.seed['type'])
        form.get_by_label('Next duty', exact=True).first.select_option(late['id'])
        form.get_by_label('Source or departmental decision', exact=True).fill('Constructed browser test')
        save_dialog(form, 'Propose rule')
        expect(page.get_by_text('Rule proposed', exact=False).last).to_be_visible()
        rule = next(p for p in h.get('coordinator', '/api/catalogues')['policies'] if p['definition']['rule']['kind'] == 'shift_succession')
        assert rule['definition']['rule']['type_order'] == [h.seed['type'], late['id']]
        page.get_by_role('button', name='Add rule', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_role('radio', name='Together or apart', exact=True).check()
        expect(form.get_by_text('the people named are never told', exact=False)).to_be_visible()
        people = form.locator('fieldset').filter(has_text='People (2 to 10)')
        for box in people.get_by_role('checkbox').all()[:2]:
            box.check()
        form.get_by_label('Source or departmental decision', exact=True).fill('Constructed browser test')
        save_dialog(form, 'Propose rule')
        expect(page.get_by_text('Rule proposed', exact=False).last).to_be_visible()
        rule = next(p for p in h.get('coordinator', '/api/catalogues')['policies'] if p['definition']['rule']['kind'] == 'together_apart')
        assert rule['definition']['rule']['mode'] == 'apart' and len(rule['definition']['rule']['person_ids']) == 2
    finally:
        context.close()


def test_withdraw_has_undo_settled_wishes_are_read_only_and_approvers_only_read(browser, live_server, h):
    person, first = h.seed['people'][0], next_month()
    day = lambda n: (first + timedelta(days=n)).isoformat()  # noqa: E731
    settled = h.command('staff', 'wish.create', {'person_id': person, 'first_day': day(5), 'last_day': day(5), 'kind': 'prefer_off', 'points': 2})['wish']
    h.command('staff', 'wish.create', {'person_id': person, 'first_day': day(19), 'last_day': day(19), 'kind': 'prefer_on', 'points': 1, 'note': 'Swap day'})
    w = h.workspace(start=day(3), end=day(10))
    h.lifecycle(h.shift(w, start=day(4) + 'T08:00:00+03:00', end=day(4) + 'T12:00:00+03:00')['workspace'], 'submit')
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page,'requests')
        page.get_by_role('button', name='Wish calendar', exact=True).click()
        panel = page.get_by_role('region', name='Wishes')
        panel.get_by_role('button', name='Next month').click()
        expect(page.locator('.wish-month')).to_have_text(first.strftime('%B %Y'))
        expect(panel.get_by_text('3 of 10 points used', exact=False)).to_be_visible()
        # Settled by the submitted rota: no Change, and its calendar day cannot be opened.
        rows = panel.locator('tr.dt-open, tbody tr')
        expect(rows.filter(has_text='Settled · rota submitted')).to_have_count(1)
        expect(rows.filter(has_text='Settled · rota submitted').get_by_role('button', name='Change')).to_have_count(0)
        expect(panel.get_by_role('button', name=re.compile(r'Day off \(settled\)'))).to_be_disabled()
        # Withdraw at once, then Undo puts it back.
        row = rows.filter(has_text='Swap day')
        row.get_by_role('button', name=re.compile('More actions|Actions')).click()
        page.get_by_role('menuitem', name='Withdraw wish').click()
        expect(panel.get_by_text('2 of 10 points used', exact=False)).to_be_visible()
        page.get_by_role('button', name='Undo', exact=True).click()
        expect(panel.get_by_text('Swap day', exact=True)).to_be_visible()
        expect(panel.get_by_text('3 of 10 points used', exact=False)).to_be_visible()
    finally:
        context.close()
    assert settled
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page,'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        page.get_by_role('button', name='Wish calendar', exact=True).click()
        panel = page.get_by_role('region', name='Wishes')
        panel.get_by_role('button', name='Next month').click()
        expect(panel.get_by_text('Swap day', exact=True)).to_be_visible()
        expect(panel.get_by_role('button', name='Add a wish')).to_have_count(0)
        expect(panel.get_by_role('button', name='Change')).to_have_count(0)
        expect(panel.locator('.wish-day:not([disabled])')).to_have_count(0)
    finally:
        context.close()


def test_coordinator_adds_a_wish_for_someone_from_everyone(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page,'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        page.get_by_role('button', name='Wish calendar', exact=True).click()
        panel = page.get_by_role('region', name='Wishes')
        panel.get_by_label('Whose wishes', exact=True).select_option('')
        before = month_of(page)
        panel.get_by_role('button', name='Next month').click()
        # The month redraws when its wishes arrive: a day clicked before then is in the month left behind.
        expect(page.locator('.wish-month')).not_to_have_text(before)
        panel.locator('.wish-day:not(.other-month)').nth(12).click()
        dialog = page.get_by_role('dialog', name='Add a wish')
        dialog.get_by_label('Person', exact=True).select_option(h.seed['people'][1])
        dialog.get_by_role('button', name='Add wish', exact=True).click()
        expect(panel.locator('.wish-chip')).to_have_count(1)
        assert [w['person_id'] for w in h.get('coordinator', '/api/wishes')['wishes']] == [h.seed['people'][1]]
    finally:
        context.close()


def test_a_late_answer_for_the_old_month_does_not_replace_the_new_month(browser, live_server, h):
    """Regression (flaky in CI): Everyone then Next month at once sends two reads; when the first answers last it
    drew the old month, so the wish added next landed in a month the calendar then moved away from."""
    context, page = sign_in(browser, live_server, 'coordinator')
    held = []
    try:
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        page.get_by_role('button', name='Wish calendar', exact=True).click()
        panel = page.get_by_role('region', name='Wishes')
        this_month = page.locator('.wish-month').inner_text()
        page.route('**/api/wishes?**', lambda route: held.append(route) if not held else route.continue_())
        panel.get_by_label('Whose wishes', exact=True).select_option('')
        wait_held(page, held)
        panel.get_by_role('button', name='Next month').click()
        expect(page.locator('.wish-month')).not_to_have_text(this_month)
        next_month = page.locator('.wish-month').inner_text()
        with page.expect_response(lambda r: r.request == held[0].request):
            held[0].continue_()
        page.evaluate("() => new Promise(r => requestAnimationFrame(() => setTimeout(r)))")
        expect(page.locator('.wish-month')).to_have_text(next_month)
    finally:
        context.close()


def test_wish_calendar_opens_on_the_department_month_not_the_utc_month(browser, live_server, h):
    """Regression: just after midnight in the department (UTC+3) it is still the previous month in UTC; the
    calendar must open on the department's month, and mark the department's today."""
    assert h.get('staff', '/api/me')['organisation']['timezone'] == 'Asia/Riyadh'
    context, page = sign_in(browser, live_server, 'staff')
    try:
        # 1 Nov 2026 00:30 in department-local is 31 Oct 21:30 UTC.
        page.clock.set_fixed_time('2026-10-31T21:30:00Z')
        navigate(page,'requests')
        page.get_by_role('button', name='Wish calendar', exact=True).click()
        expect(page.locator('.wish-month')).to_have_text('November 2026')
        expect(page.locator('.wish-day.today')).to_have_attribute('aria-label', re.compile('1 Nov'))
    finally:
        context.close()


def test_wish_calendar_shows_one_points_meter_and_shades_the_department_weekend(browser, live_server, h):
    """Open, the calendar's meter (for the month it shows) is the only one: the toolbar's summary steps aside and comes
    back when the calendar closes. Weekend days follow Department settings (here Thursday and Friday)."""
    me = h.get('admin', '/api/me')
    org, dept = me['organisation'], me['department']
    h.command('admin', 'department.settings', {'department_name': dept['name'], 'department_revision': dept['revision'],
              'organisation_name': org['name'], 'timezone': org['timezone'], 'week_start': org['week_start'],
              'weekend_days': [3, 4], 'reason': 'Synthetic Thursday and Friday weekend'}, dept['id'], org['revision'])
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'requests')
        strip = page.locator('#main').get_by_role('group', name='Requests', exact=True).get_by_role('group', name='Wish points', exact=True)
        expect(strip.get_by_role('meter')).to_be_visible()
        strip.get_by_role('button', name='Wish calendar', exact=True).click()
        panel = page.get_by_role('region', name='Wishes')
        expect(panel.get_by_role('meter')).to_be_visible()
        expect(page.locator('#main').get_by_role('meter')).to_have_count(1)
        days = panel.locator('.wish-day:not(.other-month)')
        expect(days.first).to_be_visible()
        shaded = days.evaluate_all("ds => ds.filter(d => d.classList.contains('weekend')).map(d => d.getAttribute('aria-label').split(' ')[0])")
        assert shaded == ['1', '2', '8', '9', '15', '16', '22', '23', '29', '30'], shaded  # October 2026's Thursdays and Fridays
        strip.get_by_role('button', name='Wish calendar', exact=True).click()
        expect(strip.get_by_role('meter')).to_be_visible()
        expect(page.locator('#main').get_by_role('meter')).to_have_count(1)
    finally:
        context.close()


@pytest.mark.parametrize('pattern', [False, True], ids=['standing', 'pattern'])
def test_standing_preference_is_withdrawn_not_changed_with_the_one_off_editor(browser, live_server, h, pattern):
    """The one-off editor has no weekly repeat: saving a standing preference with it would turn it into one day (maintenance)."""
    person, first = h.seed['people'][0], next_month()
    starts = (first + timedelta(days=(3 - first.weekday()) % 7)).isoformat()  # Thursday anchor, Monday repeats
    fields = {'kind': 'max_days_in_row', 'limit': 2, 'first_day': first.isoformat(), 'last_day': ((first + timedelta(days=32)).replace(day=1) - timedelta(days=1)).isoformat()} if pattern else {
        'kind': 'prefer_off', 'repeat': {'weekdays': [0], 'starts_on': starts, 'ends_on': None, 'number': None}}
    standing = h.command('staff', 'wish.create', {'person_id': person, 'points': 1, 'note': 'Every Monday', **fields})['wish']
    one_off = (first + timedelta(days=20)).isoformat()
    h.command('staff', 'wish.create', {'person_id': person, 'first_day': one_off, 'last_day': one_off, 'kind': 'prefer_on', 'points': 1, 'note': 'One day'})
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'requests')
        rows = page.locator('tbody tr')
        expect(rows.filter(has_text='One day').get_by_role('button', name='Change')).to_have_count(1)
        expect(rows.filter(has_text='Every Monday')).to_have_count(1)
        if not pattern:
            expect(rows.filter(has_text='Every Monday').locator('[data-col="when"]')).to_contain_text('Every Monday')
        expect(rows.filter(has_text='Every Monday').get_by_role('button', name='Change')).to_have_count(0)
        page.get_by_role('button', name='Wish calendar', exact=True).click()
        panel = page.get_by_role('region', name='Wishes')
        panel.get_by_role('button', name='Next month').click()
        expect(page.locator('.wish-month')).to_have_text(first.strftime('%B %Y'))
        listed = panel.locator('tbody tr')
        expect(listed.filter(has_text='One day').get_by_role('button', name='Change')).to_have_count(1)
        expect(listed.filter(has_text='Every Monday').get_by_role('button', name='Change')).to_have_count(0)
        if pattern:
            expect(listed.filter(has_text='Every Monday')).to_contain_text('Most days in a row (2)')
            panel.get_by_role('button', name=re.compile('Day on duty')).click()
            dialog = page.get_by_role('dialog', name='Change wish')
            dialog.get_by_label('Note (optional, seen by planners)', exact=True).fill('One day changed')
            dialog.get_by_role('button', name='Save wish', exact=True).click()
            expect(listed.filter(has_text='One day changed')).to_have_count(1)
            panel.get_by_role('button', name=re.compile('Most days in a row.*add a wish')).first.click()
            dialog = page.get_by_role('dialog', name='Add a wish')
            dialog.get_by_label('Note (optional, seen by planners)', exact=True).fill('Beside the pattern')
            dialog.get_by_role('button', name='Add wish', exact=True).click()
            expect(listed.filter(has_text='Beside the pattern')).to_have_count(1)
        else:
            expect(listed.filter(has_text='Every Monday').locator('[data-col="when"]')).to_contain_text('Every Monday')
            # Definitions are not dated wishes: neither the Thursday anchor nor Mondays get invented chips.
            expect(panel.locator('.wish-day.has-wish')).to_have_count(1)
            expect(panel.locator('.wish-chip')).to_have_text('On 1')
        # Withdraw stays: that is how a standing preference ends until its own editor ships.
        listed.filter(has_text='Every Monday').get_by_role('button', name=re.compile('More actions|Actions')).click()
        expect(page.get_by_role('menuitem', name='Withdraw wish')).to_be_visible()
        page.keyboard.press('Escape')
        page.reload()
        expect(page.locator('tbody tr').filter(has_text='Every Monday')).to_have_count(1)
    finally:
        context.close()
    saved = h.get('staff', f"/api/wishes?start={first.isoformat()}&end={(first + timedelta(days=40)).isoformat()}&person_id={person}")
    kept = next(w for w in saved['wishes'] if w['id'] == standing['id'])
    assert {key: kept[key] for key in standing} == standing


def test_scoring_defaults_save_with_a_points_budget_above_one_hundred(browser, live_server, h):
    """The scoring form takes the same points ceiling as the API: a large stored budget never blocks an unrelated edit."""
    from .test_staff_wishes import department_settings
    h.command('admin', 'department.settings', {**department_settings(h), 'generation_defaults': {'scoring': {'wish_budget_points': 2_000_000}}},
              h.seed['department_id'], h.get('admin', '/api/me')['organisation']['revision'])
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, 'Auto-schedule & scoring')
        pane = page.locator('.config-pane')
        pane.get_by_role('tab', name='Staff preferences', exact=True).click()
        expect(pane.get_by_label('Wish points per person (0 = wishes off)', exact=True)).to_have_value('2000000')
        pane.get_by_role('tab', name='How it decides', exact=True).click()
        pane.get_by_label('Fairness weight', exact=True).fill('30')  # an unrelated default
        pane.get_by_role('button', name='Save defaults', exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Reason', exact=True).fill('Fairness up, budget kept')
        dialog.get_by_role('button', name='Save defaults', exact=True).click()
        expect(dialog).to_have_count(0)
        page.reload()
        open_config(page, 'Auto-schedule & scoring')
        pane = page.locator('.config-pane')
        pane.get_by_role('tab', name='Staff preferences', exact=True).click()
        expect(pane.get_by_label('Wish points per person (0 = wishes off)', exact=True)).to_have_value('2000000')
        pane.get_by_role('tab', name='How it decides', exact=True).click()
        expect(pane.get_by_label('Fairness weight', exact=True)).to_have_value('30')
    finally:
        context.close()
    scoring = h.get('admin', '/api/me')['department']['generation_defaults']['scoring']
    assert scoring['wish_budget_points'] == 2_000_000 and scoring['weights']['fairness'] == 30
