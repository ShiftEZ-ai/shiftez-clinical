"""one exception tree is presented consistently in the Rules list and rule page."""
import re

import pytest
from playwright.sync_api import Error as PlaywrightError, expect

from .test_browser import browser, choose, live_server, navigate, route_fetch, sign_in, wait_held
from .test_browser_rule_catalogue import flag
from .test_browser_rule_catalogue_v2 import line, v2_rule

pytestmark = pytest.mark.browser


def _who(field, value):
    return {'match': 'all', 'conditions': [{'field': field, 'values': [value]}]}


def _max(value, who=None, **more):
    return line({'max': {'value': value, 'level': 'mandatory'}},
                who=who, measure='duties', period={'kind': 'calendar_week'}, **more)


def _apply(who=None, **more):
    return line({'apply': {'value': None, 'level': 'mandatory'}},
                who=who, settings={'restriction': {}}, **more)


def _person_lines(h):
    first, second = h.seed['people'][:2]
    return first, second


def _workload_tree(h, *, rank=False, mixed=False):
    first, second = _person_lines(h)
    everyone = _max(5)
    if mixed:
        lines = [everyone, _apply(_who('person_ids', first))]
    elif rank:
        lines = [
            _max(5, _who('category_ids', h.seed['category'])),
            _max(3, _who('person_ids', first)),
            _max(4, _who('person_ids', second)),
        ]
    else:
        lines = [everyone, _max(3, _who('person_ids', first)), _max(4, _who('person_ids', second))]
    return v2_rule(h, 'workload_limit', lines), lines


def _list_item(page, title='Workload limit'):
    item = page.locator('.rt-item').filter(has=page.get_by_role('button', name=f'{title} lines', exact=True))
    expect(item).to_have_count(1)
    if not item.locator('.rt-lines').count():
        item.get_by_role('button', name=f'{title} lines', exact=True).click()
    expect(item.locator('.rt-lines')).to_be_visible()
    return item


def _ids(locator):
    return [locator.nth(i).get_attribute('data-line') for i in range(locator.count())]


def _line_rows(page, ident=None):
    return page.locator('[data-table=rule-line-db] tr[data-row]' + (f'[data-line="{ident}"]' if ident else ''))


def _server_replacements(page, replaces):
    """Vary only the server projection; the saved rules and their nesting remain real."""
    def rewrite(route):
        response = route_fetch(route)
        body = response.json()

        def project(target):
            if replaces is None:
                target.pop('line_replaces', None)
            else:
                target['line_replaces'] = replaces

        for entry in body.get('entries', []):
            if entry['id'] == 'workload_limit':
                project(entry)
        if body.get('entry', {}).get('id') == 'workload_limit':
            project(body['entry'])
        if isinstance(body.get('check'), dict):
            project(body['check'])
        if 'duplicates' in body:
            project(body)
        route.fulfill(response=response, json=body)

    page.route(re.compile(r'/api/rules/catalogue(?:\?|/workload_limit/(?:detail|check)(?:\?|$)|$)'), rewrite)


def _open_workload(h, browser, live_server, lines):
    rule = v2_rule(h, 'workload_limit', lines)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    navigate(page, 'rules')
    return rule, context, page


def test_rule_exceptions_anchor_and_exception_order_match_in_list_and_rule_page(browser, live_server, h):
    """Point 1: the broad line anchors its person exceptions, with person lines before a rank line."""
    flag(h, legacy=False)
    first, second = _person_lines(h)
    lines = [
        _max(5),
        _max(3, _who('person_ids', first)),
        _max(4, _who('person_ids', second)),
        _max(2, _who('category_ids', h.seed['category'])),
    ]
    _, context, page = _open_workload(h, browser, live_server, lines)
    try:
        item = _list_item(page)
        group = item.locator('ol.rt-lns').first
        assert _ids(group.locator(':scope > li.rt-ln')) == [lines[0]['id']]
        assert _ids(group.locator('ol.rt-exc > li.rt-ln')) == [lines[1]['id'], lines[2]['id'], lines[3]['id']]
        navigate(page, 'rules/workload_limit')
        rows = _line_rows(page)
        assert _ids(rows) == [line['id'] for line in lines]
        expect(rows.nth(0).locator('.rt-exc-who')).to_have_count(0)
        expect(rows.nth(1).locator('.rt-exc-who')).to_have_count(1)
        expect(rows.nth(2).locator('.rt-exc-who')).to_have_count(1)
        expect(rows.nth(3).locator('.rt-exc-who')).to_have_count(1)
    finally:
        context.close()


def test_rule_exceptions_instead_of_is_shown_only_for_certain_person_replacements(browser, live_server, h):
    """Point 2 positive case: same-bound person replacements say what they replace in both views."""
    flag(h, legacy=False)
    first, second = _person_lines(h)
    # The broad line sets only a maximum: with a minimum beside it the resolver keeps both maxima today, so a person
    # maximum does not replace it (no line_replaces); that resolver gap is tracked on its own, outside this page.
    anchor = _max(5)
    lines = [anchor, _max(3, _who('person_ids', first)), _max(4, _who('person_ids', second))]
    _, context, page = _open_workload(h, browser, live_server, lines)
    try:
        entry = next(e for e in h.get('coordinator', '/api/rules/catalogue?version=2')['entries'] if e['id'] == 'workload_limit')
        assert entry['line_replaces'] == {row['id']: [{'replaced_line_id': anchor['id'], 'numbers': ['max']}] for row in lines[1:]}
        item = _list_item(page)
        expect(item.locator('.rt-instead')).to_have_count(2)
        expect(item.locator('.rt-instead').first).to_have_text('(instead of ≤ 5 duties per calendar week)')
        expect(item.locator('.rt-instead').first).not_to_contain_text('2')
        expect(item.locator('.rt-instead').first).not_to_contain_text(re.compile(r'Must|Prefer|mandatory|medium'))
        navigate(page, 'rules/workload_limit')
        rows = _line_rows(page)
        expect(rows.locator('.rt-instead')).to_have_count(2)
        expect(rows.nth(1).locator('.rt-instead')).to_have_text('(instead of ≤ 5 duties per calendar week)')
        expect(rows.nth(1).locator('[data-col=who] .rt-instead')).to_have_text('(instead of ≤ 5 duties per calendar week)')
        assert _ids(rows) == [line['id'] for line in lines]
    finally:
        context.close()


@pytest.mark.parametrize('case', [
    'rank_anchor', 'rank_group', 'different_number', 'different_strength', 'different_measure_period',
    'different_days', 'not_in_force', 'same_numbers', 'duplicate_who', 'applies', 'mixed_safety',
])
def test_rule_exceptions_instead_of_is_absent_for_ambiguous_cases(browser, live_server, h, case):
    """Point 2 negatives: ``instead of`` is reserved for an unambiguous replacement."""
    flag(h, legacy=False)
    first, second = _person_lines(h)
    if case == 'rank_anchor':
        entry, lines = 'workload_limit', [_max(5, _who('category_ids', h.seed['category'])), _max(3, _who('person_ids', first))]
    elif case == 'rank_group':
        entry, lines = 'workload_limit', [_max(5), _max(4, _who('category_ids', h.seed['category'])), _max(3, _who('person_ids', first))]
    elif case == 'different_number':
        entry, lines = 'workload_limit', [_max(5), line({'min': {'value': 3, 'level': 'mandatory'}}, who=_who('person_ids', first),
                                                       measure='duties', period={'kind': 'calendar_week'})]
    elif case == 'different_strength':
        entry, lines = 'workload_limit', [_max(5), line({'max': {'value': 3, 'level': 'medium'}}, who=_who('person_ids', first),
                                                        measure='duties', period={'kind': 'calendar_week'})]
    elif case == 'different_measure_period':
        entry, lines = 'workload_limit', [_max(5), line({'max': {'value': 3, 'level': 'mandatory'}}, who=_who('person_ids', first),
                                                        measure='scheduled_hours', period={'kind': 'rolling', 'days': 7})]
    elif case == 'different_days':
        entry, lines = 'workload_limit', [_max(5), _max(3, _who('person_ids', first), weekdays=[0])]
    elif case == 'not_in_force':
        entry, lines = 'workload_limit', [_max(5), _max(3, _who('person_ids', first), starts_on='2026-10-10')]
    elif case == 'same_numbers':
        entry, lines = 'workload_limit', [_max(5), _max(5, _who('person_ids', first))]
    elif case == 'duplicate_who':
        # Two lines for one person; the second also sets a minimum, as the same line twice is refused (DUPLICATE_LINES).
        twin = _max(4, _who('person_ids', first))
        twin['numbers']['min'] = {'value': 1, 'level': 'mandatory'}
        entry, lines = 'workload_limit', [_max(5), _max(3, _who('person_ids', first)), twin]
    elif case == 'applies':
        entry, lines = 'workload_limit', [_max(5), _apply(_who('person_ids', first))]
    else:
        entry, lines = 'workload_limit', [_max(5), _apply(_who('person_ids', first))]
    rule = v2_rule(h, entry, lines)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        # Without server evidence even an old same-bound heuristic must stay quiet.
        _server_replacements(page, {})
        navigate(page, 'rules')
        item = _list_item(page)
        expect(item.locator('.rt-instead')).to_have_count(0)
        if case == 'mixed_safety':
            apply_line = item.locator(f'li[data-line="{lines[1]["id"]}"]')
            expect(apply_line.locator('.rt-ln-num')).to_contain_text('No duties')
            expect(apply_line.get_by_role('img', name='Safety line. Rota adjustments may only tighten protection. '
                                         'Reducing mandatory department protection needs independent approval.',
                                         exact=True)).to_have_count(1)
        if case == 'not_in_force':
            future_line = item.locator(f'li[data-line="{lines[1]["id"]}"]')
            muted = future_line.locator('.rt-ln-flags > .muted')
            expect(muted).to_have_text('Starts later')
            style = future_line.evaluate('''row => {const flag=row.querySelector('.rt-ln-flags > .muted'), when=row.querySelector('.rt-ln-when');
                const a=getComputedStyle(flag), b=getComputedStyle(when); return {size:a.fontSize, colour:a.color, whenSize:b.fontSize, whenColour:b.color};}''')
            assert style['size'] == style['whenSize'] == '12px' and style['colour'] == style['whenColour'], style
        navigate(page, 'rules/' + entry)
        expect(_line_rows(page).locator('.rt-instead')).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('evidence', ['no_map', 'no_line', 'two_broader', 'empty_numbers'])
def test_rule_replacement_text_requires_one_server_replacement_in_both_views(browser, live_server, h, evidence):
    flag(h, legacy=False)
    anchor, personal = _max(5), _max(3, _who('person_ids', h.seed['people'][0]))
    lines = [anchor, personal]
    if evidence == 'two_broader':
        lines.append(_max(4, _who('category_ids', h.seed['category'])))
    v2_rule(h, 'workload_limit', lines)
    replaces = None if evidence == 'no_map' else {} if evidence == 'no_line' else {
        personal['id']: [{'replaced_line_id': anchor['id'], 'numbers': [] if evidence == 'empty_numbers' else ['max']}]}
    if evidence == 'two_broader':
        replaces[personal['id']].append({'replaced_line_id': lines[2]['id'], 'numbers': ['max']})
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        _server_replacements(page, replaces)
        navigate(page, 'rules')
        item = _list_item(page)
        personal_row = item.locator(f'li.rt-ln[data-line="{personal["id"]}"]')
        expect(personal_row).to_be_visible()
        expect(personal_row.locator('.rt-instead')).to_have_count(0)
        # Removing evidence does not turn an exception back into a flat line.
        expect(item.locator(f'ol.rt-exc > li.rt-ln[data-line="{personal["id"]}"]')).to_have_count(1)
        navigate(page, 'rules/workload_limit')
        row = _line_rows(page, personal['id'])
        expect(row).to_be_visible()
        expect(row.locator('[data-col=who] .rt-instead')).to_have_count(0)
        expect(row.locator('.rt-exc-who .sr-only')).to_have_text('Exception: ')
    finally:
        context.close()


def test_changed_rule_replacement_waits_for_its_check_and_stays_absent_in_the_draft_list(browser, live_server, h):
    flag(h, legacy=False)
    anchor, personal = _max(5), _max(3, _who('person_ids', h.seed['people'][0]))
    v2_rule(h, 'workload_limit', [anchor, personal])
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    pending = []
    released = False
    changed_note = 'Synthetic draft change'

    def hold_changed_check(route):
        response = route_fetch(route)
        changed = any(row.get('note') == changed_note for row in route.request.post_data_json.get('lines', []))
        if changed and not released:
            pending.append((route, response))
        else:
            route.fulfill(response=response)

    try:
        page.route('**/api/rules/catalogue/workload_limit/check', hold_changed_check)
        navigate(page, 'rules')
        item = _list_item(page)
        expect(item.locator(f'li[data-line="{personal["id"]}"] .rt-instead')).to_have_text('(instead of ≤ 5 duties per calendar week)')
        navigate(page, 'rules/workload_limit/' + personal['id'])
        panel = page.locator('.rc-line-panel')
        expect(panel).to_be_visible()
        panel.get_by_label('Note', exact=True).fill(changed_note)
        panel.get_by_role('button', name='Keep change', exact=True).click()
        panel.get_by_role('button', name='Close panel', exact=True).click()
        wait_held(page, pending)
        row = _line_rows(page, personal['id'])
        expect(row.locator('[data-col=who] .rt-instead')).to_have_count(0)
        expect(page.locator('.app-footer')).to_contain_text('1 change not yet proposed')
        released = True
        while pending:
            route, response = pending.pop(0)
            body = response.json()
            try:
                assert body['line_replaces'][personal['id']] == [{'replaced_line_id': anchor['id'], 'numbers': ['max']}]
            finally:
                route.fulfill(response=response)
        # No second edit or reload: the check answer alone must invalidate the empty shape memo.
        expect(row.locator('[data-col=who] .rt-instead')).to_have_text('(instead of ≤ 5 duties per calendar week)')
        page.get_by_role('button', name='← Rules').click()  # within Rules the draft is kept; an address change asks first
        item = _list_item(page)
        personal_row = item.locator(f'li.rt-ln[data-line="{personal["id"]}"]')
        expect(personal_row.locator('.rt-ln-flags')).to_contain_text('Changed')
        expect(personal_row.locator('.rt-instead')).to_have_count(0)
    finally:
        released = True
        for route, response in pending:
            try:
                route.fulfill(response=response)
            except PlaywrightError:
                pass  # Cleanup must not replace the test's original failure.
        context.close()


@pytest.mark.parametrize('kind', ['people', 'ranks'])
def test_rule_exceptions_no_broad_line_keeps_a_flat_specific_first_list(browser, live_server, h, kind):
    """Point 3: without one broad anchor, lines stay flat; an explicit line sort stays flat too."""
    flag(h, legacy=False)
    first, second = _person_lines(h)
    who = ([_who('person_ids', first), _who('person_ids', second)] if kind == 'people'
           else [_who('category_ids', h.seed['category']), _who('category_ids', h.seed['trainee_category'])])
    lines = [_max(3, who[0]), _max(4, who[1])]
    _, context, page = _open_workload(h, browser, live_server, lines)
    try:
        item = _list_item(page)
        expect(item.locator('.rt-sub, ol.rt-exc')).to_have_count(0)
        assert _ids(item.locator('ol.rt-lns > li.rt-ln')) == [line['id'] for line in lines]
        # A chosen line sort deliberately uses its own flat order.
        choose(page, 'Sort', 'Sort by', 'Lines by numbers')
        expect(item.locator('.rt-sub, ol.rt-exc')).to_have_count(0)
    finally:
        context.close()


def test_rule_exceptions_safety_lines_stay_flat_and_keep_the_lock(browser, live_server, h):
    """Point 4: a pure safety catalogue entry never presents a weaker line as an exception."""
    flag(h, legacy=False)
    first = h.seed['people'][0]
    lines = [line({'min': {'value': 2, 'level': 'mandatory'}}),
             line({'min': {'value': 3, 'level': 'mandatory'}}, who=_who('person_ids', first))]
    rule = v2_rule(h, 'people_per_session', lines)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        navigate(page, 'rules')
        item = page.locator('.rt-item').filter(has=page.get_by_role('button', name='People per session lines', exact=True))
        item.get_by_role('button', name='People per session lines', exact=True).click()
        expect(item.locator('.rt-sub, ol.rt-exc, .rt-instead')).to_have_count(0)
        expect(item.locator('[aria-label^="Safety rule"]')).to_have_count(1)
        expect(item.locator('.rt-ln-num').first).to_contain_text('people')
    finally:
        context.close()


def test_rule_exceptions_exception_label_matches_only_indented_non_safety_lines(browser, live_server, h):
    """Every indented line is an exception, while the anchor and Always-applies safety line stay unlabeled."""
    flag(h, legacy=False)
    first, second = _person_lines(h)
    anchor = _max(5)
    mandatory = _max(3, _who('person_ids', first))
    advisory = line({'max': {'value': 4, 'level': 'medium'}}, who=_who('person_ids', second),
                    measure='duties', period={'kind': 'calendar_week'})
    safety = _apply()
    v2_rule(h, 'workload_limit', [anchor, mandatory, advisory, safety])
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        navigate(page, 'rules')
        item = _list_item(page)
        expect(item.locator('ol.rt-exc > li.rt-ln')).to_have_count(2)
        expect(item.locator('ol.rt-always > li.rt-ln')).to_have_count(1)
        expect(item.locator('ol.rt-exc > li.rt-ln').filter(has_text='Alex Morgan')).to_have_count(1)
        navigate(page, 'rules/workload_limit')
        for line_id in (mandatory['id'], advisory['id']):
            expect(_line_rows(page, line_id).locator('.rt-exc-who .sr-only')).to_have_text('Exception: ')
        for line_id in (anchor['id'], safety['id']):
            expect(_line_rows(page, line_id).locator('.rt-exc-who .sr-only')).to_have_count(0)
        expect(_line_rows(page, safety['id']).locator('.rt-always-word')).to_have_text('Always applies')
    finally:
        context.close()


def test_rule_exceptions_nested_exception_accessibility_uses_css_indent_and_rule_page_label(browser, live_server, h):
    """Point 5: the nested list is named, counted, and indented without inline styles or colour bars."""
    flag(h, legacy=False)
    _, context, page = _open_workload(h, browser, live_server,
                                      [_max(5), _max(3, _who('person_ids', h.seed['people'][0]))])
    try:
        item = _list_item(page)
        nested = item.locator('ol.rt-exc')
        expect(nested).to_have_accessible_name('1 exception')
        expect(nested.locator(':scope > li.rt-ln')).to_have_count(1)
        assert item.locator('.rt-lines [style]').count() == 0
        indent = nested.locator('.rt-ln-who').evaluate("e => ({padding:getComputedStyle(e).paddingLeft,border:getComputedStyle(e).borderLeftStyle})")
        assert float(indent['padding'].replace('px', '')) > 0 and indent['border'] == 'none', indent
        navigate(page, 'rules/workload_limit')
        expect(_line_rows(page).nth(1).locator('.rt-exc-who .sr-only')).to_have_text('Exception: ')
    finally:
        context.close()


def test_rule_exceptions_closed_row_meta_counts_do_not_change_when_lines_open(browser, live_server, h):
    """Point 6: opening a row does not alter its closed line/reach/where counts."""
    flag(h, legacy=False)
    _, context, page = _open_workload(h, browser, live_server,
                                      [_max(5), _max(3, _who('person_ids', h.seed['people'][0]))])
    try:
        item = page.locator('.rt-item').filter(has=page.get_by_role('button', name='Workload limit lines', exact=True))
        before = item.locator('.rt-meta').inner_text()
        expect(item.locator('.rt-lines')).to_have_count(0)
        item.get_by_role('button', name='Workload limit lines', exact=True).click()
        expect(item.locator('.rt-lines')).to_be_visible()
        item.get_by_role('button', name='Workload limit lines', exact=True).click()
        expect(item.locator('.rt-meta')).to_have_text(before)
        expect(item.locator('.rt-lines')).to_have_count(0)
    finally:
        context.close()


def test_rule_exceptions_list_and_rule_page_share_line_order_exception_ids_and_replacement_text(browser, live_server, h):
    """Point 7: both views expose the same tree output for the same where group."""
    flag(h, legacy=False)
    first, second = _person_lines(h)
    lines = [_max(5), _max(3, _who('person_ids', first)), _max(4, _who('person_ids', second))]
    _, context, page = _open_workload(h, browser, live_server, lines)
    try:
        item = _list_item(page)
        group = item.locator('ol.rt-lns').first
        list_ids = _ids(group.locator('li.rt-ln'))
        list_exceptions = _ids(group.locator('ol.rt-exc > li.rt-ln'))
        list_instead = {row.get_attribute('data-line'): row.locator('.rt-instead').inner_text()
                        for row in [group.locator(f'li[data-line="{line_id}"]').first for line_id in list_exceptions]}
        navigate(page, 'rules/workload_limit')
        rows = _line_rows(page)
        page_ids = _ids(rows)
        page_exceptions = _ids(rows.filter(has=page.locator('.rt-exc-who')))
        page_instead = {line_id: _line_rows(page, line_id).locator('.rt-instead').inner_text()
                        for line_id in page_exceptions}
        assert page_ids == list_ids
        assert page_exceptions == list_exceptions
        assert page_instead == list_instead
    finally:
        context.close()
