"""Rendered dates stay whole in narrow cells; stored dates and visible wording keep their meaning."""
import os
from pathlib import Path

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401
from .test_browser_people_merged import row
from .test_browser_rule_catalogue import flag, rules_action
from .test_rule_templates import activate, propose

pytestmark = pytest.mark.browser


def stored_theme(page, theme):
    page.emulate_media(color_scheme=theme)
    page.evaluate('''theme => {
        const prefs = JSON.parse(localStorage.getItem('rota.prefs') || '{}');
        localStorage.setItem('rota.prefs', JSON.stringify({...prefs, theme}));
    }''', theme)
    page.reload()
    expect(page.locator('html')).to_have_attribute('data-theme', theme)
    expect(page.locator('#main[data-loaded-view]')).to_have_count(1)


def whole_units(holder, parts):
    """Check actual date text, line boxes and cell containment, not just the styling hook."""
    expect(holder).to_have_text(' '.join(parts))
    units = holder.locator('.date-units')
    expect(units).to_have_count(1)
    children = units.locator(':scope > span')
    expect(children).to_have_text(parts)
    assert children.evaluate_all('''nodes => nodes.every(n => {
        const range = document.createRange(); range.selectNodeContents(n);
        const rects = [...range.getClientRects()];
        const unit = n.getBoundingClientRect();
        return rects.length === 1 && rects[0].height > 0 &&
            rects[0].left >= unit.left - 1 && rects[0].right <= unit.right + 1;
    })'''), parts


def contained_date_fragments(holder, parts):
    """Each rendered date/time unit is one text line inside its actual cell/card content box."""
    expect(holder.locator('.date-units > span')).to_have_text(parts)
    assert holder.evaluate('''box => {
        const bounds = box.getBoundingClientRect();
        return [...box.querySelectorAll('.date-units > span')].every(unit => {
            const range = document.createRange(); range.selectNodeContents(unit);
            const rects = [...range.getClientRects()];
            return rects.length === 1 && rects[0].height > 0 &&
                rects[0].left >= bounds.left - 1 && rects[0].right <= bounds.right + 1 &&
                rects[0].top >= bounds.top - 1 && rects[0].bottom <= bounds.bottom + 1;
        });
    }'''), parts


def screenshot(page, name):
    folder = os.getenv('PW_COVERAGE_ARTIFACT_DIR')
    if folder:
        output = Path(folder) / 'date-units'
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / (name + '.png')), full_page=True)


@pytest.mark.parametrize('role,width,theme', [
    ('credentialer', 1440, 'light'), ('credentialer', 390, 'dark'),
    ('auditor', 1440, 'dark'), ('coordinator', 390, 'light'),
])
def test_people_four_history_kinds_keep_finite_and_open_dates_whole_after_reload(browser, live_server, h, role, width, theme):
    person = h.configure('people', {'display_name': 'Date history colleague'})['record']
    definitions = [
        ('services', 'service_id', 'eligibility.create', 'credentialer', 'Clinical continuity and urgent care eligibility'),
        ('capabilities', 'capability_id', 'grant.create', 'credentialer', 'Comprehensive clinical assessment capability'),
        ('categories', 'category_id', 'membership.create', 'coordinator', 'Senior clinical practice rank'),
        ('programmes', 'programme_id', None, 'coordinator', 'Family medicine longitudinal programme'),
    ]
    expected = []
    for catalogue, key, command, writer, name in definitions:
        reference = h.configure(catalogue, {'name': name, 'starts_on': '2020-01-01'})['record']
        ranges = [
            ('2026-12-05', '2026-12-19', ['5 Dec 2026', '– 18 Dec 2026']),
            ('2027-01-02', '2028-01-03' if catalogue == 'programmes' else None,
             ['2 Jan 2027', '– 2 Jan 2028' if catalogue == 'programmes' else '– no end']),
        ]
        for start, end, words in ranges:
            payload = {'person_id': person['id'], key: reference['id'], 'starts_on': start, 'ends_on': end}
            if catalogue == 'capabilities':
                payload['evidence'] = 'Synthetic independently recorded qualification evidence'
            if command:
                h.command(writer, command, payload)
            else:
                h.configure('programme_memberships', payload, role=writer)
            expected.append((catalogue, name, words))
    # The browser reads persisted records, including the exclusive end boundary.
    before = h.get('credentialer', '/api/catalogues')
    owned = {kind: [r for r in before[kind] if r['person_id'] == person['id']]
             for kind in ('eligibilities', 'grants', 'memberships', 'programme_memberships')}
    assert all(len(records) == 2 for records in owned.values())
    context, page = sign_in(browser, live_server, role, viewport={'width': width, 'height': 844})
    try:
        stored_theme(page, theme)
        navigate(page, 'people')
        page.reload()
        navigate(page, 'people')
        row(page, person['display_name']).locator('strong').first.click()
        panel = page.get_by_role('region', name=person['display_name'], exact=True)
        expect(panel.get_by_role('tab', name='Overview', exact=True)).to_have_attribute('aria-selected', 'true')
        for catalogue, name, parts in expected:
            record = panel.locator('.ph-row').filter(has_text=name).filter(has_text=' '.join(parts))
            expect(record).to_have_count(1)
            cell = record.locator('.ph-when')
            cell.scroll_into_view_if_needed()
            whole_units(cell, parts)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        screenshot(page, f'people-{role}-{width}-{theme}')
        after = h.get('credentialer', '/api/catalogues')
        assert {kind: [r for r in after[kind] if r['person_id'] == person['id']] for kind in owned} == owned
    finally:
        context.close()


@pytest.mark.parametrize('width,theme', [(1440, 'dark'), (390, 'light')])
def test_rule_context_and_details_keep_finite_and_open_date_wording(browser, live_server, h, width, theme):
    flag(h)
    rules = [
        (activate(h, propose(h, {'kind': 'min_rest', 'hours': 11}, name='Finite date-unit rule',
                            starts_on='2026-12-05', ends_on='2026-12-19')),
         ['5 Dec 2026', '– 18 Dec 2026'], ['5 Dec 2026', '– 18 Dec 2026']),
        (activate(h, propose(h, {'kind': 'min_rest', 'hours': 12}, name='Open date-unit rule', starts_on='2027-01-02')),
         ['2 Jan 2027', '– Ongoing'], ['2 Jan 2027', '– no end']),
    ]
    context, page = sign_in(browser, live_server, 'auditor', viewport={'width': width, 'height': 844})
    try:
        stored_theme(page, theme)
        navigate(page, 'rules')
        rules_action(page, 'Other department rules (2)')
        panel = page.get_by_role('region', name='Other department rules', exact=True)
        for rule, table_parts, detail_parts in rules:
            panel.get_by_role('searchbox', name='Search other department rules', exact=True).fill(rule['name'])
            record = panel.locator(f'[data-row="{rule["id"]}"]')
            expect(record).to_have_count(1)
            # Table cells and the phone's labelled card properties share the same renderer.
            date_cell = record.locator('.date-units').locator('..')
            date_cell.scroll_into_view_if_needed()
            whole_units(date_cell, table_parts)
            record.get_by_role('button', name=rule['name'], exact=True).click()
            detail = page.locator('.dock-panel.rule-sheet')
            expect(detail.locator('.rl-meta')).to_contain_text('Effective from ' + detail_parts[0])
            effective = detail.locator('.rule-facts .rule-valid').locator('..')
            effective.scroll_into_view_if_needed()
            whole_units(effective, detail_parts)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            screenshot(page, f'rule-{width}-{theme}-' + rule['name'].split()[0].lower())
            detail.get_by_role('button', name='Back to Other department rules', exact=True).click()
        expect(panel.get_by_role('searchbox', name='Search other department rules', exact=True)).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (390, 'dark')])
def test_request_dates_keep_all_day_timed_and_weekly_wording(browser, live_server, h, width, theme):
    person = h.seed['people'][0]
    cases = []
    for start, end, parts in [
        ('2026-12-05T00:00:00+03:00', '2026-12-06T00:00:00+03:00', ['5 Dec 2026', '· all day']),
        ('2026-12-07T00:00:00+03:00', '2026-12-19T00:00:00+03:00', ['7 Dec 2026', '– 18 Dec 2026', '· all day']),
        ('2026-12-20T08:00:00+03:00', '2026-12-20T12:00:00+03:00', ['20 Dec 2026', '· 08:00–12:00']),
        ('2026-12-21T22:00:00+03:00', '2026-12-22T02:00:00+03:00', ['21 Dec 2026', '22:00', '– 22 Dec 2026', '02:00']),
    ]:
        request = h.command('staff', 'request.create', {'person_id': person, 'start': start, 'end': end,
                            'private_reason': 'Date formatting fixture', 'kind': 'leave'})['record']
        cases.append((request['id'], parts))
    for start, end, parts in [('2027-01-02', None, ['From 2 Jan 2027']),
                               ('2026-12-05', '2026-12-19', ['From 5 Dec 2026', 'to 18 Dec 2026'])]:
        pattern = h.command('staff', 'availability_pattern.propose', {'person_id': person, 'weekdays': [1],
                            'start_time': '13:00', 'end_time': '17:00', 'starts_on': start, 'ends_on': end})['pattern']
        cases.append((pattern['id'], parts))
    summary = h.get('staff', '/api/home')['my_requests']
    expected_by_id = dict(cases)
    assert len(summary) == 5 < len(cases)  # Home deliberately shows only the first five own requests.
    assert all(r['person_id'] == person and r['id'] in expected_by_id and r['stage'] == 'waiting' for r in summary)
    assert {cases[3][0], cases[5][0]} <= {r['id'] for r in summary}  # The observed overnight and finite weekly regressions.
    context, page = sign_in(browser, live_server, 'staff', viewport={'width': width, 'height': 844})
    try:
        stored_theme(page, theme)
        navigate(page, 'home')
        # Home › My requests: their own waiting requests (status, not Needs you); a full first page reads "5+ waiting".
        mine = page.get_by_role('region', name='My requests')
        expect(mine).to_contain_text('5+ waiting')
        expect(mine.locator('.home-request-list li')).to_have_count(3)
        expect(page.locator('#main .home-needs-list li').filter(has_text='request')).to_have_count(0)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        screenshot(page, f'home-requests-{width}-{theme}')
        navigate(page, 'requests')
        for record_id, parts in cases:
            record = page.locator(f'#main [data-row="{record_id}"]')
            expect(record).to_have_count(1)
            dates = record.locator('.request-when')
            dates.scroll_into_view_if_needed()
            whole_units(dates, parts)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        screenshot(page, f'requests-{width}-{theme}')
    finally:
        context.close()


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (390, 'dark')])
def test_resolve_dates_keep_session_leave_and_finding_fallback_whole(browser, live_server, h, width, theme):
    from .test_leave_disclosure import setting

    person = h.seed['people'][0]
    quota = h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
                      'metric': 'duties', 'comparison': 'minimum', 'amount': 3}, {'person_id': person})
    workspace = h.shift(h.workspace('Resolve whole dates'))['workspace']
    note = 'Family medicine congress with a long recorded explanation that remains readable and wraps between words'
    h.command('coordinator', 'availability.record', {'person_id': person, 'kind': 'recorded_leave',
              'leave_type': 'Annual leave', 'start': '2026-09-28T00:00:00+03:00',
              'end': '2026-10-07T00:00:00+03:00', 'private_reason': note})
    setting(h, 'details')
    workspace = h.lifecycle(h.detail(workspace)['workspace'], 'validate')['workspace']
    findings = h.detail(workspace)['workspace']['validation']['findings']
    leave = next(f for f in findings if f['code'] == 'UNAVAILABLE')
    fallback = next(f for f in findings if f['rule_id'] == quota['id'])
    assert leave['shift_id'] and leave['leave']['starts_on'] == '2026-09-28'
    assert leave['leave']['ends_on'] == '2026-10-06' and leave['leave']['note'] == note
    assert not fallback['shift_id'] and fallback['start'], 'Exercise the explicit finding-date fallback'
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 844})
    try:
        stored_theme(page, theme)
        navigate(page, f'schedule/issues?draft={workspace["id"]}&view=checks')
        main = page.locator('#main')
        records = main.locator('.dt-card' if width < 720 else 'tr[data-row]')
        unavailable = records.filter(has_text='Annual').first
        expect(unavailable).to_be_visible()
        expect(unavailable).to_contain_text('28 Sep – 6 Oct · ' + note)
        expect(unavailable).to_contain_text('Sun 4 Oct 2026 · 08:00–12:00')
        if width < 720:
            # A card's title is its session (duty · clinic · day · time); its line holds the leave range and note.
            contained_date_fragments(unavailable.locator('.dt-card-title'), ['Sun 4 Oct 2026', '08:00–12:00'])
            contained_date_fragments(unavailable.locator('.dt-card-line'), ['28 Sep – 6 Oct'])
        else:
            contained_date_fragments(unavailable.locator('td[data-col="reason"]'), ['28 Sep – 6 Oct'])
            contained_date_fragments(unavailable.locator('td[data-col="session"]'), ['Sun 4 Oct 2026', '08:00–12:00'])
        # Plain session wording is still indexed, including the no-session finding's dates: since  (F5) a
        # period issue names the whole days it counts over, 4 to 10 Oct (its end is the exclusive 11 Oct midnight).
        search = main.get_by_role('searchbox', name='Search issues', exact=True)
        search.fill('4 Oct 2026')
        expect(records.filter(has_text='Annual')).to_have_count(1)
        key = '|'.join(str(fallback.get(k) or '') for k in ('code', 'rule_id', 'shift_id', 'person_id', 'start'))
        quota_row = main.locator(f'[data-row="{key}"]')
        expect(quota_row).to_have_count(1)  # a period is also found by its first whole date
        period = '4–10 Oct 2026'
        search.fill(period)
        expect(quota_row).to_have_count(1)
        expect(quota_row).to_have_attribute('data-row', key)
        holder = quota_row.locator('.dt-card-title' if width < 720 else 'td[data-col="session"]')  # the finding's dates are its card title
        contained_date_fragments(holder, [period])
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        screenshot(page, f'resolve-dates-{width}-{theme}')
    finally:
        context.close()
