"""mixed-safety lines, strength placement and field-based pills."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in
from .test_browser_rule_catalogue import flag
from .test_browser_rule_catalogue_v2 import line, v2_rule

pytestmark = pytest.mark.browser


def _who(field, values):
    return {'match': 'all', 'conditions': [{'field': field, 'values': list(values)}]}


def _count(value, who=None, *, level='mandatory', measure='duties', period=None, where=None, **more):
    return line({'max': {'value': value, 'level': level}}, who=who, where=where,
                measure=measure, period=period or {'kind': 'calendar_week'}, **more)


def _mixed_fixture(h):
    flag(h, legacy=False)
    second_rank = h.configure('categories', {'name': 'Exception second rank', 'starts_on': '2020-01-01'})['record']['id']
    programme = h.configure('programmes', {'name': 'Exception programme', 'starts_on': '2020-01-01'})['record']['id']
    capability = h.configure('capabilities', {'name': 'Exception capability', 'starts_on': '2020-01-01'})['record']['id']
    tag = h.configure('tags', {'name': 'Exception mixed tag', 'labels': ['people']})['record']['id']
    services = h.seed['services'][:2]
    replacement_service = h.configure('services', {'name': 'Exception replacement clinic', 'starts_on': '2020-01-01'})['record']['id']
    duty_type = h.seed['type']
    people = h.seed['people']
    lines = []
    for i in range(40):
        mask = i + 1
        weekdays = [day for day in range(7) if mask & (1 << day)]
        where = {'conditions': [{'field': 'service_ids', 'values': [services[i % 2]]}]}
        if i == 7:
            where = {'conditions': [{'field': 'assignment_type_ids', 'values': [duty_type]}]}
        if i == 8:
            where = {'conditions': [{'field': 'location_ids', 'values': [h.seed['locations'][0]]}]}
        if i == 9:
            where = {'conditions': [{'field': 'usual_assignee_ids', 'values': [people[1]]},
                                    {'field': 'service_ids', 'values': [services[i % 2]]}]}
        if i == 0:
            who = _who('category_ids', [second_rank])
        elif i == 1:
            who = None
        elif i == 2:
            who = _who('category_ids', [h.seed['category'], second_rank])
        elif i == 3:
            who = _who('person_ids', [people[0]])
        elif i == 4:
            who = _who('programme_ids', [programme])
        elif i == 5:
            who = _who('tags', [tag])
        elif i == 6:
            who = _who('capability_ids', [capability])
        elif i == 9:
            who = _who('person_ids', [people[1]])
        elif i == 10:
            who = _who('person_ids', [people[2]])
        else:
            who = _who('person_ids', [people[i % len(people)]]) if i % 2 else None
        level = 'medium' if i == 4 else None if i == 5 else 'mandatory'
        lines.append(_count(3 + i % 4, who, level=level,
                            period={'kind': 'calendar_month'} if i == 11 else {'kind': 'calendar_week'},
                            where=where,
                            weekdays=weekdays,
                            ends_on='2026-10-08' if i == 0 else None,
                            starts_on='2026-10-10' if i == 1 else None,
                            note=f'Exception count {i}'))
    apply_line = line({'apply': {'value': None, 'level': 'mandatory'}}, who=_who('person_ids', [people[2]]),
                       where={'conditions': [{'field': 'service_ids', 'values': [services[0]]}]},
                       settings={'restriction': {}}, note='Exception no duties')
    lines.append(apply_line)
    replacement_anchor = _count(20, where={'conditions': [{'field': 'service_ids', 'values': [replacement_service]}]},
                                note='Exception ' + ('long note ' * 20))
    replacement_exception = _count(18, _who('person_ids', [people[0]]),
                                   where={'conditions': [{'field': 'service_ids', 'values': [replacement_service]}]},
                                   note='Exception replacement exception')
    lines.extend([replacement_anchor, replacement_exception])
    workload = v2_rule(h, 'workload_limit', lines)
    safety = v2_rule(h, 'duty_spacing', [line({'min': {'value': 11, 'level': 'mandatory'}}),
                                         line({'min': {'value': 12, 'level': 'mandatory'}}, who=_who('person_ids', [people[0]]))])
    return workload, safety, lines, apply_line, (replacement_anchor, replacement_exception)


def _open(page, rule='Workload limit'):
    item = page.locator('.rt-item').filter(has=page.get_by_role('button', name=f'{rule} lines', exact=True))
    expect(item).to_have_count(1)
    item.get_by_role('button', name=f'{rule} lines', exact=True).click()
    expect(item.locator('.rt-lines')).to_be_visible()
    return item


@pytest.mark.parametrize('width', [1440, 390], ids=['desktop-1440', 'phone-390'])
def test_rules_page_flags_fit_and_mixed_safety_lines_stay_apart(browser, live_server, h, width):
    """Items 10, 12 and C1: flags fit, no-duties lines always apply, and count lines still form a tree."""
    _, _, lines, apply_line, pair = _mixed_fixture(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 900})
    try:
        navigate(page, 'rules')
        item = _open(page)
        if width == 1440:
            scroll = item.locator('.rt-lines')
            size = scroll.evaluate('e=>({width:e.clientWidth,scroll:e.scrollWidth})')
            assert size['scroll'] <= size['width'] + 1, size
            for flags in item.locator('.rt-ln-flags').all():
                assert flags.bounding_box()['x'] + flags.bounding_box()['width'] <= scroll.bounding_box()['x'] + scroll.bounding_box()['width'] + 1
        expect(item).to_contain_text('⚠ Ends in 4 days')
        expect(item).to_contain_text('⚠ No one')
        apply_row = item.locator(f'li[data-line="{apply_line["id"]}"]')
        expect(apply_row).to_have_count(1)
        expect(apply_row.locator('.rt-ln-num .rule-num')).to_have_text(re.compile(r'^No duties$'))
        expect(apply_row.locator('.rk-lockmark[aria-label^="Safety line"]')).to_have_count(1)
        expect(item.locator('ol.rt-always[aria-label^="Always applies"]')).to_have_count(1)
        expect(item.locator('.rt-always > li.rt-ln')).to_have_count(1)
        # Two where groups have one Everyone line to anchor their exceptions: Review clinic (line 2) and the replacement clinic.
        expect(item.locator('.rt-exc')).to_have_count(2)
        # The server's one replacement in this fixture (as the rule page shows it); no safety line ever replaces.
        expect(item.locator('.rt-instead')).to_have_count(1)
        expect(item.locator(f'li[data-line="{pair[1]["id"]}"] .rt-instead')).to_contain_text('instead of')
        if width == 390:
            # A line's flags may wrap, and stay inside the line (two that fit side by side need not break).
            several = [flags for flags in item.locator('.rt-ln-flags').all() if flags.locator('.badge').count() > 1]
            assert several, 'the fixture has a line with two flags'
            for flags in several:
                assert flags.evaluate('e => getComputedStyle(e).flexWrap') == 'wrap'
                box, line_box = flags.bounding_box(), flags.locator('xpath=ancestor::li[contains(@class,"rt-ln")][1]').bounding_box()
                assert box['x'] + box['width'] <= line_box['x'] + line_box['width'] + 1, (box, line_box)
        navigate(page, 'rules/workload_limit')
        # The row itself carries data-line: a table row at 1440, a card (its facts in dd) on a phone.
        apply_row = page.locator(f'[data-table=rule-line-db] [data-row][data-line="{apply_line["id"]}"]')
        expect(apply_row.locator('[data-col="n_apply"] .rule-num' if width == 1440 else 'dd .rule-num')).to_contain_text('No duties')
        expect(apply_row.locator('.rk-lockmark[aria-label^="Safety line"]')).to_have_count(1)
        expect(apply_row.locator('.rt-always-word')).to_have_text('Always applies')
    finally:
        context.close()


def test_rules_page_strength_is_after_measure_and_inherited_is_silent(browser, live_server, h):
    """Item 11: one strength is on the rule, mixed strength follows the measured number, and inheritance stays silent."""
    _, _, lines, _, _ = _mixed_fixture(h)
    inherited = lines[5]
    mixed = lines[4]
    month = lines[11]
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        navigate(page, 'rules')
        item = _open(page)
        mixed_row = item.locator(f'li[data-line="{mixed["id"]}"]')
        expect(mixed_row.locator('.rt-ln-after .rc-level')).to_have_count(1)
        assert mixed_row.locator('.rt-ln-after').bounding_box()['x'] >= mixed_row.locator('.rt-ln-num .rule-num').bounding_box()['x']
        expect(item.locator(f'li[data-line="{month["id"]}"] .rt-ln-num .rule-num')).to_have_text(re.compile(r'^\S.* duties per calendar month$'))
        inherited_row = item.locator(f'li[data-line="{inherited["id"]}"]')
        expect(inherited_row.locator('.rt-ln-after .rc-level')).to_have_count(0)
        navigate(page, 'rules')  # the rule's strength badges are on the Rules list, not its own page
        safety_item = _open(page, 'Rest between duties')
        expect(safety_item.locator('.rt-ln-num .rc-level')).to_have_count(0)
        expect(safety_item.locator('.rt-levels .rc-level')).to_have_count(1)
        expect(page.locator('body')).not_to_contain_text('Inherited strength')
        white_space = month and item.locator(f'li[data-line="{month["id"]}"] .rt-ln-num > .rule-num').evaluate('e=>getComputedStyle(e).whiteSpace')
        assert white_space == 'nowrap'
        navigate(page, f'rules/workload_limit/{inherited["id"]}')
        expect(page.locator('body')).not_to_contain_text('Inherited strength')
        expect(page.locator('.rc-line-panel')).not_to_contain_text('Inherited strength')
    finally:
        context.close()


def test_rules_page_safety_rule_is_flat_and_rule_page_has_no_rule_overlap_badge(browser, live_server, h):
    """Items C1(v/vi) and 14: pure safety stays flat and rule-level Overlaps text is absent on the page."""
    _, safety, _, _, _ = _mixed_fixture(h)
    v2_rule(h, 'people_per_session', [line({'min': {'value': 1, 'level': 'mandatory'}}),
                                      line({'min': {'value': 2, 'level': 'mandatory'}},
                                           who=_who('person_ids', [h.seed['people'][0]]))])
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        navigate(page, 'rules')
        item = _open(page, 'People per session')
        expect(item.locator('.rt-exc')).to_have_count(0)
        navigate(page, 'rules/workload_limit')
        overlap_text = page.locator('tr[data-row] td:not([data-col="check"])').evaluate_all(
            "cells => cells.filter(e => e.textContent?.includes('Overlaps')).length"
        )
        assert overlap_text == 0
        assert safety['definition']['rule']['id'] == 'duty_spacing'
    finally:
        context.close()


def test_rules_page_rule_page_fits_at_1440_with_checks_and_exception_text(browser, live_server, h):
    """the dense rule page keeps its rail, pinned navigation and check column inside the 1440 viewport."""
    _, _, _, _, pair = _mixed_fixture(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        navigate(page, 'rules/workload_limit')
        for nav in ('rail', 'pinned'):
            shell = page.locator('.shell')
            if shell.get_attribute('data-nav') != nav:
                page.locator('.pin-button').evaluate('b => b.click()')  # hidden in the rail until the sidebar peeks
            expect(shell).to_have_attribute('data-nav', nav)
            navigate(page, 'rules/workload_limit')
            table_scroll = page.locator('[data-table=rule-line-db] .table-scroll')
            for label in ('Who', 'Where', 'Measure', 'Days', 'Valid', 'Note', 'Check'):
                header = table_scroll.locator('thead').get_by_role('button', name=label, exact=True)
                if header.count():
                    header.click()
                    wrap = page.get_by_role('menuitemcheckbox', name='Wrap this column', exact=True)
                    # A switch saved with the view: the second pass finds it on and must leave it on.
                    if wrap.get_attribute('aria-checked') != 'true':
                        wrap.click()
                    else:
                        page.keyboard.press('Escape')
                    expect(table_scroll.locator(f'td[data-col="{label.lower()}"].dt-wrap').first).to_be_attached()
            size = table_scroll.evaluate('e=>({width:e.clientWidth,scroll:e.scrollWidth})')
            assert size['scroll'] <= size['width'] + 1, (nav, size)
            for badge in table_scroll.locator('td[data-col=check] .badge').all():
                rect, outer = badge.bounding_box(), table_scroll.bounding_box()
                assert rect['x'] + rect['width'] <= outer['x'] + outer['width'] + 1
            for col in ('who', 'where', 'measure', 'days', 'valid', 'note', 'check'):
                cells = table_scroll.locator(f'td[data-col="{col}"]')
                if cells.count():
                    assert cells.first.evaluate('e=>getComputedStyle(e).whiteSpace') == 'normal'
            assert table_scroll.locator('td[data-col^="n_"] .rule-num').count() > 0
            assert table_scroll.locator('td[data-col^="n_"] .rule-num').first.evaluate('e=>getComputedStyle(e).whiteSpace') == 'nowrap'
            expect(table_scroll.locator('tbody')).to_contain_text('Duties per calendar week')  # one Measure column, its cells one phrase
            expect(table_scroll.locator('thead')).not_to_contain_text('Period')
            pair_row = table_scroll.locator(f'tr[data-row][data-line="{pair[1]["id"]}"]')
            expect(pair_row.locator('.rt-instead')).to_contain_text('instead of')
            expect(table_scroll.locator('.rt-always-word')).to_have_count(1)
    finally:
        context.close()


def test_rules_page_pills_follow_condition_fields(browser, live_server, h):
    """Item 13: field semantics choose pill kinds and rank/duty-type swatches."""
    flag(h, legacy=False)
    programme = h.configure('programmes', {'name': 'Exception pill programme', 'starts_on': '2020-01-01'})['record']['id']
    capability = h.configure('capabilities', {'name': 'Exception pill capability', 'starts_on': '2020-01-01'})['record']['id']
    tag = h.configure('tags', {'name': 'Exception pill tag', 'labels': ['people']})['record']['id']
    second_rank = h.configure('categories', {'name': 'Exception pill second rank', 'starts_on': '2020-01-01'})['record']['id']
    service = h.seed['services'][0]
    who_fields = [
        ('category_ids', [h.seed['category'], second_rank], 'dotted'),
        ('programme_ids', [programme], 'group'), ('tags', [tag], 'tag'),
        ('capability_ids', [capability], 'group'), ('person_ids', [h.seed['people'][0]], 'person'),
        (None, [], 'all'),
    ]
    lines = []
    for i, (field, values, kind) in enumerate(who_fields):
        who = ({'conditions': [{'field': field, 'op': 'all', 'values': values}]}
               if field == 'category_ids' else _who(field, values) if field else None)
        lines.append(line({'max': {'value': 3 + i, 'level': 'mandatory'}},
                                          who=who,
                                          where={'conditions': [{'field': 'service_ids', 'values': [service]}]},
                                          weekdays=[day for day in range(7) if (i + 1) & (1 << day)],
                                          measure='duties', period={'kind': 'calendar_week'}, note=f'pill {kind}'))
    single_rank_line = line({'max': {'value': 18, 'level': 'mandatory'}},
                            who=_who('category_ids', [h.seed['category']]),
                            where={'conditions': [{'field': 'service_ids', 'values': [service]}]},
                            weekdays=[0], measure='duties', period={'kind': 'calendar_week'}, note='pill single rank')
    lines.append(single_rank_line)
    duty_line = line({'max': {'value': 12, 'level': 'mandatory'}},
                      who=None, where={'conditions': [{'field': 'assignment_type_ids', 'values': [h.seed['type']]}]},
                      weekdays=[6], measure='duties', period={'kind': 'calendar_week'}, note='pill duty type')
    usual_line = line({'max': {'value': 14, 'level': 'mandatory'}}, who=None,
                      where={'conditions': [{'field': 'usual_assignee_ids', 'values': [h.seed['people'][1]]}]},
                      weekdays=[5], measure='duties', period={'kind': 'calendar_week'}, note='pill usual assignee')
    lines.extend([duty_line, usual_line])
    duty_name = next(row['name'] for row in h.get('coordinator', '/api/catalogues')['assignment_types']
                     if row['id'] == h.seed['type'])
    v2_rule(h, 'workload_limit', lines)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        navigate(page, 'rules')
        item = _open(page)
        rank_pill = item.locator(f'li[data-line="{single_rank_line["id"]}"] .rt-ln-who .rp').first
        expect(rank_pill).to_have_class(re.compile(r'\brp duty-rank\d+\b'))
        expect(rank_pill).to_have_attribute('data-lg-self', '')
        assert not re.search(r'\brp (?:place|group|all)\b', rank_pill.get_attribute('class') or '')
        expect(item.locator('.rt-ln-who').filter(has_text='Exception pill programme').locator('.rp.group')).to_have_count(1)
        expect(item.locator('.rt-ln-who').filter(has_text='Exception pill capability').locator('.rp.group')).to_have_count(1)
        expect(item.locator('.rt-ln-who').filter(has_text='Alex Morgan').locator('.rp.person')).to_have_count(1)
        assert item.locator('.rt-ln-who .rp.all').count() >= 1
        expect(item.locator('.rt-ln-who').filter(has_text='#Exception pill tag').locator('.tag-ref')).to_have_count(1)
        duty_pill = item.locator('.rt-lines .rt-wh .rp.dotted')
        expect(duty_pill).to_have_count(1)
        expect(duty_pill).to_contain_text(duty_name)
        expect(duty_pill.locator('.lg-swatch[class*="duty-"]')).to_have_count(1)
        assert 'brown' not in (duty_pill.get_attribute('class') or '')
        usual_pill = item.locator('.rt-lines .rt-wh .rp.person').filter(has_text='Jordan Lee')
        expect(usual_pill).to_have_class(re.compile(r'\brp person\b'))
        expect(page.locator('.view-legend')).not_to_contain_text('Duty type')
    finally:
        context.close()


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_rules_page_opened_lines_use_the_applied_seven_track_grid(browser, live_server, h, theme):
    """opened rows use the shared grid and subgrid, so every number cell aligns."""
    flag(h, legacy=False)
    first = h.seed['people'][0]
    lines = [line({'max': {'value': 5, 'level': 'mandatory'}}),
             line({'max': {'value': 3, 'level': 'mandatory'}}, who=_who('person_ids', [first])),
             line({'apply': {'value': None, 'level': 'mandatory'}}, settings={'restriction': {}})]
    v2_rule(h, 'workload_limit', lines)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        page.emulate_media(color_scheme=theme)
        navigate(page, 'rules')
        page.evaluate('t => {document.documentElement.dataset.theme=t;}', theme)
        expect(page.locator('html')).to_have_attribute('data-theme', theme)
        item = _open(page)
        grid = item.locator('.rt-lines')
        info = grid.evaluate('''e => {
            const columns=getComputedStyle(e).gridTemplateColumns;
            let depth=0,tracks=columns==='none'?0:1;
            for(const char of columns){if(char==='(')depth++;else if(char===')')depth--;else if(char===' '&&depth===0)tracks++;}
            return {
            columns,tracks,
            rows:[...e.querySelectorAll('li.rt-ln')].map(r => ({
                grid:getComputedStyle(r).gridTemplateColumns,
                left:r.querySelector('.rt-n')?.getBoundingClientRect().left,
                starts:['.bulk-check','.rt-n','.rt-ln-who','.rt-ln-when','.rt-ln-num','.rt-ln-flags','.rt-ln-open']
                    .map(s=>r.querySelector(s)?.getBoundingClientRect().left)
            }))
        }}''')
        assert info['columns'] != 'none' and info['tracks'] == 7, info
        # Chromium reads a subgrid back with its line names: "subgrid [] [] …".
        assert info['rows'] and all(row['grid'].split()[0] == 'subgrid' for row in info['rows']), info
        lefts = [row['left'] for row in info['rows'] if row['left'] is not None]
        assert max(lefts) - min(lefts) <= 1, info
        for column in range(7):
            starts = [row['starts'][column] for row in info['rows']]
            assert all(start is not None for start in starts) and max(starts) - min(starts) <= 1, (column, info)
    finally:
        context.close()


@pytest.mark.parametrize('variant', ['weekday_leave', 'hours', 'leave_one', 'empty'])
def test_rules_page_restriction_words_match_between_list_and_rule_page(browser, live_server, h, variant):
    """No duties condition words are visible in both presentations."""
    flag(h, legacy=False)
    if variant == 'weekday_leave':
        restriction = {'weekdays': [3, 6], 'leave_buffer_days': 3}
        expected = 'Sun, Thu · within 3 days of leave'
    elif variant == 'hours':
        restriction = {'start_time': '08:00', 'end_time': '16:00'}
        expected = 'Sun–Sat 08:00–16:00'
    elif variant == 'leave_one':
        restriction = {'leave_buffer_days': 1}
        expected = 'within 1 day of leave'
    else:
        restriction = {}
        expected = ''
    no_duties = line({'apply': {'value': None, 'level': 'mandatory'}}, weekdays=list(range(7)),
                      settings={'restriction': restriction})
    v2_rule(h, 'workload_limit', [no_duties])
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        navigate(page, 'rules')
        item = _open(page)
        list_text = item.locator(f'li[data-line="{no_duties["id"]}"] .rt-ln-when').inner_text().strip()
        navigate(page, 'rules/workload_limit')
        row = page.locator(f'tr[data-row][data-line="{no_duties["id"]}"]')
        page_text = row.locator('[data-col="days"]').inner_text().strip()
        if expected:
            assert list_text == expected and page_text == expected, (variant, list_text, page_text)
        else:
            assert list_text == '' and 'Conditions apply' not in page_text and 'within ' not in page_text, (list_text, page_text)
        expect(row.locator('[data-col="n_apply"] .rule-num')).to_contain_text('No duties')
    finally:
        context.close()


def test_rules_page_rules_list_scroll_reaches_last_workload_line(browser, live_server, h):
    """At 1440 the Rules collection owns the vertical scroll and keeps the last open line reachable."""
    _mixed_fixture(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        navigate(page, 'rules')
        item = _open(page)
        scroll = page.locator('#main .rt-scroll')
        size = scroll.evaluate('e=>({height:e.clientHeight,total:e.scrollHeight})')
        assert size['total'] > size['height'], size
        last = item.locator('.rt-ln').last
        scroll.evaluate('e=>e.scrollTop=e.scrollHeight')
        expect(last).to_be_in_viewport()
        assert last.evaluate('e=>{const r=e.getBoundingClientRect(),s=e.closest(".rt-scroll").getBoundingClientRect();return r.bottom<=s.bottom+1}')
    finally:
        context.close()
