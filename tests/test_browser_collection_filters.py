"""One Filter, Group and Sort for every collection : the shared condition builder in the
shared collection toolbar. Each migrated collection takes a filter, an Or group inside an And tree, is saved as a view
(or kept, where the collection has no saved views), and reads the same after a reload. Fixtures are fictional."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import add_filter, browser, close_popover, condition, live_server, navigate, sign_in, sort_direction, toolbar  # noqa: F401
from .test_browser_rule_catalogue import flag, weekly
from .test_browser_rotation_board import residency
from .test_browser_operator_role_coverage import all_roles  # noqa: F401
from .collection_fixture import mount
from .rules_support import rules_by_family

pytestmark = pytest.mark.browser


def or_group(pop, field, first, second):
    """+ Add filter rule ▾ › Add filter group, then "<field> is <first> Or <field> is <second>" inside it."""
    add_filter(pop, group=True)
    group = pop.locator('.cond-group').last
    condition(group, field, [first])
    condition(group, field, [second])
    group.locator('.cond-join select').first.select_option('any')
    expect(group.locator('.cond-join select').first).to_have_value('any')


def save_view(scope, name):
    toolbar(scope, 'Saved views').get_by_role('menuitem', name='New view…', exact=True).click()
    dialog = scope.page.get_by_role('dialog', name='New view', exact=True)
    dialog.get_by_label('View name', exact=True).fill(name)
    dialog.get_by_role('button', name='Save view', exact=True).click()
    expect(dialog).to_have_count(0)


def tree_has_or_group(tree):
    return any('conditions' in n and n['match'] == 'any' for n in tree['conditions'])


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
def test_rules_filter_is_the_shared_builder_with_groups_saved_as_a_view(browser, live_server, h, width, scheme):
    flag(h)
    weekly(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 900})
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        page.emulate_media(color_scheme=scheme)
        rules_by_family(h, page)  # this test's groups are the catalogue families
        navigate(page, 'rules')
        main = page.locator('#main')
        families = main.locator('tr.rt-family strong')
        names = families.all_inner_texts()
        assert len(names) >= 3, names
        pop = toolbar(main, 'Filter')
        # No bespoke controls: the popover is the condition builder alone (no "Any" dropdown per property).
        expect(pop.locator('.cond-root')).to_have_count(1)
        assert pop.locator('.pick').count() == pop.locator('.cond-root .pick').count()
        expect(pop.locator(':scope > .dt-pop-label')).to_have_count(0)
        if width < 768:  # a bottom sheet on phones
            pop.evaluate('e => Promise.allSettled(e.getAnimations({subtree: true}).map(a => a.finished))')
            box, view = pop.bounding_box(), page.viewport_size
            assert box['x'] <= 1 and abs(box['x'] + box['width'] - view['width']) <= 1, box
        # Where Family is not the third And (Family is the first Or the second).
        condition(pop, 'Family', [names[2]], op='none')
        or_group(pop, 'Family', names[0], names[1])
        close_popover(page)
        expect(families).to_have_text(names[:2])
        rules = main.locator('tr.rt-rule').all_inner_texts()
        expect(main.get_by_role('button', name=re.compile(r'^Filter: .*\(3 active\)$'))).to_be_visible()
        save_view(main, 'Two families')
        saved = next(v for v in h.get('coordinator', '/api/table-views?table=rule-tree')['views'] if v['name'] == 'Two families')
        assert tree_has_or_group(saved['definition']['conditions']), saved['definition']
        if width < 768:
            for index in range(18):
                h.command('coordinator', 'table_view.create', {'table_key': 'rule-tree', 'name': f'Extra rules {index}',
                                                               'definition': saved['definition']})
        catalogue = []
        pattern = re.compile(r'.*/api/table-views\?table=rule-tree$')
        page.route(pattern, lambda route: catalogue.append(route))
        with page.expect_request(pattern):
            page.reload()
        navigate(page, 'rules')
        menu = toolbar(main, 'Saved views')
        item = menu.get_by_role('menuitemradio', name='Two families', exact=True)
        expect(item).to_have_count(0)
        menu.get_by_role('menuitemradio', name=re.compile(r'^All rules\b')).press('End')
        # 176a12f82: while the catalogue loads, the view shown is named by its id, so both defaults are offered.
        department_default = menu.get_by_role('menuitem', name='Set as department default', exact=True)
        expect(department_default).to_be_focused()
        # The personal view removes the department action when loaded; keep focus on the surviving personal action.
        department_default.press('ArrowUp')
        my_default = menu.get_by_role('menuitem', name='Set as my default', exact=True)
        expect(my_default).to_be_focused()
        outside = main.get_by_role('searchbox', name='Search rules', exact=True) if width >= 768 else None
        if outside:
            outside.focus()
        assert catalogue, 'the rules saved-view catalogue was held after reload'
        for route in catalogue:
            route.continue_()
        expect(item).to_be_visible()
        expect(department_default).to_have_count(0)  # a personal view is never the department default
        expect(outside if outside else my_default).to_be_focused()
        expect(menu.locator('[role^=menuitem][tabindex="0"]')).to_have_count(1)
        if width < 768:
            expect(my_default).to_be_in_viewport(ratio=1)
        close_popover(page)
        expect(main.get_by_role('button', name='Saved views: Two families', exact=True)).to_be_visible()
        expect(families).to_have_text(names[:2])
        assert main.locator('tr.rt-rule').all_inner_texts() == rules
        # Clear all brings every family back; the view is then marked changed.
        toolbar(main, 'Filter').get_by_role('button', name='Clear all filters', exact=True).click()
        close_popover(page)
        expect(families).to_have_count(len(names))
        expect(main.get_by_role('button', name='Saved views: Two families (changed)', exact=True)).to_be_visible()
    finally:
        context.close()
    assert not errors


def test_a_shared_table_filters_with_an_or_group_sorts_by_two_keys_and_keeps_both_in_a_view(browser, live_server, h):
    people = h.get('admin', '/api/catalogues')['people']
    tags = {'Alex Morgan': ['north', 'east'], 'Jordan Lee': ['south'], 'Taylor Noor': ['north']}
    for p in people:
        if p['display_name'] in tags:
            h.command('admin', 'catalogue.update', {'catalogue': 'people', 'value': {**{k: p[k] for k in ('display_name', 'active')}, 'tags': tags[p['display_name']]},
                                                    'reason': 'Test setup'}, p['id'], p['revision'])
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        navigate(page, 'people')
        main = page.locator('#main')
        shown = lambda: [next(n for n in tags if n in t) for t in main.locator('tbody tr[data-row]').all_inner_texts() if any(n in t for n in tags)]  # noqa: E731
        first = main.locator('thead th[data-col]').first.inner_text().strip()
        # Sort: Current rank (all the same), then the name descending: + Add sort, its property and direction.
        sort = toolbar(main, 'Sort')
        if sort.locator('.dt-sort-row').count():
            sort.locator('.dt-sort-row').first.get_by_label('Sort by', exact=True).select_option(label='Current rank')
        else:
            sort.get_by_role('menu', name='Sort by', exact=True).get_by_role('menuitemradio', name='Current rank', exact=True).click()
        sort.get_by_role('button', name='+ Add sort', exact=True).click()
        sort.locator('.dt-sort-row').nth(1).get_by_label('Then by', exact=True).select_option(label=first)
        sort_direction(main, 'Descending', row=1)
        expect(main.get_by_role('button', name=re.compile(r'^Sort: Current rank, ascending, then .+, descending'))).to_be_visible()
        close_popover(page)
        assert shown() == ['Taylor Noor', 'Jordan Lee', 'Alex Morgan']
        # Where Tags is not east And (Tags is north Or Tags is south).
        pop = toolbar(main, 'Filter')
        condition(pop, 'Tags', ['east'], op='none')
        or_group(pop, 'Tags', 'north', 'south')
        close_popover(page)
        expect(main.locator('tbody tr[data-row]')).to_have_count(2)
        assert shown() == ['Taylor Noor', 'Jordan Lee']
        save_view(main, 'North or south')
        saved = next(v for v in h.get('coordinator', '/api/table-views?table=people')['views'] if v['name'] == 'North or south')['definition']
        assert saved['sort'] == ['rank', 'asc'] and len(saved['then_sort']) == 1 and saved['then_sort'][0][1] == 'desc', saved
        assert tree_has_or_group(saved['conditions']), saved
        page.reload()
        navigate(page, 'people')
        expect(main.get_by_role('button', name='Saved views: North or south', exact=True)).to_be_visible()
        expect(main.locator('tbody tr[data-row]')).to_have_count(2)
        assert shown() == ['Taylor Noor', 'Jordan Lee']
        # View settings › Shown properties: a property moves down, and the header follows.
        view = toolbar(main, 'View settings')
        headers = main.locator('thead th[data-col]').all_inner_texts()
        second = headers[1].strip()
        view.get_by_role('button', name=f'Move {second} down', exact=True).click()
        expect(main.locator('thead th[data-col]').nth(2)).to_have_text(second)
        expect(view.get_by_role('button', name=f'Move {second} down', exact=True)).to_be_focused()
        close_popover(page)
    finally:
        context.close()
    assert not errors


def test_rotation_board_filters_with_the_shared_builder_and_keeps_it(browser, live_server, all_roles):
    h = all_roles
    p, cohort, types, people, planned = residency(h)
    context, page = sign_in(browser, live_server, 'programme_lead', {'width': 1440, 'height': 900})
    try:
        navigate(page, 'programmes/board')
        board = page.get_by_role('region', name='Rotation timeline')
        expect(board.locator('.tl-row')).to_have_count(3)
        page.get_by_role('button', name='Filter', exact=True).click()
        pop = page.locator('.dt-pop')
        # Where Trainees is local And (Rotation type is Emergency Or Clinic).
        condition(pop, 'Trainees', ['Local trainees'])
        or_group(pop, 'Rotation type', types[0]['name'], types[2]['name'])
        close_popover(page)
        expect(board.locator('.tl-row')).to_have_count(2)
        expect(page.get_by_role('button', name=re.compile(r'^Filter: .*\(3 active\)$'))).to_be_visible()
        page.reload()
        navigate(page, 'programmes/board')
        expect(board.locator('.tl-row')).to_have_count(2)
    finally:
        context.close()


@pytest.mark.parametrize('width', [1440, 390])
def test_nothing_matching_says_so_with_the_one_show_all_in_the_toolbar_strip(browser, live_server, h, width):
    """C5 (supersedes X-35's placement): a search with no match says so in the empty state, and its one way back is the
    toolbar's "Filtered: 0 of …" strip with Show all, the same place every collection toolbar has it."""
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 900})
    try:
        navigate(page, 'people')
        fixture = mount(page, 'fixture-filtered-empty')
        fixture.get_by_role('searchbox').fill('zzzz')
        state = fixture.locator('.empty-state')
        expect(state.get_by_role('heading')).to_have_text('No matching rows')
        expect(state.get_by_role('button', name='Show all')).to_have_count(0)
        strip = fixture.locator('.dt-filtered')
        expect(strip).to_contain_text('Filtered: 0 of')
        expect(fixture.get_by_role('button', name='Show all', exact=True)).to_have_count(1)
        strip.get_by_role('button', name='Show all', exact=True).click()
        expect(strip).to_be_hidden()
        expect(fixture.get_by_role('searchbox')).to_have_value('')
        expect(state).to_have_count(0)
        # While some rows show, the strip keeps its one way back.
        fixture.get_by_role('searchbox').fill('row 001')
        expect(fixture.locator('.dt-filtered')).to_be_visible()
    finally:
        context.close()


def test_cards_list_only_what_a_record_has(browser, live_server, h):
    """X-04 / P-13: a card has no "Tag —" line for a record without a tag; one with a tag lists it."""
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 390, 'height': 844})
    try:
        navigate(page, 'people')
        fixture = mount(page, 'fixture-cards', defaults={'layout': 'cards'})
        tagged, plain = fixture.locator('.dt-card').nth(0), fixture.locator('.dt-card').nth(1)
        expect(tagged.locator('dt', has_text='Tag')).to_have_count(1)
        expect(plain.locator('dt', has_text='Tag')).to_have_count(0)
        expect(fixture.locator('.dt-card-props dd', has_text=re.compile('^—$'))).to_have_count(0)
    finally:
        context.close()


def test_the_joiner_keeps_its_word_on_a_phone(browser, live_server, h):
    """X-20: inside a filter group on a 390 phone, the joiner reads "And" / "Or" whole, not "A…"."""
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 390, 'height': 844})
    try:
        navigate(page, 'people')
        pop = toolbar(mount(page, 'fixture-joiner'), 'Filter')
        add_filter(pop, group=True)
        group = pop.locator('.cond-group').last
        condition(group, 'Status', ['Active'])
        condition(group, 'Status', ['Inactive'])
        join = group.locator('.cond-join select').first
        expect(join).to_be_visible()
        assert join.evaluate('s => s.scrollWidth <= s.clientWidth')
        width = join.evaluate("""s => {const c = document.createElement('canvas').getContext('2d'), st = getComputedStyle(s);
          c.font = `${st.fontWeight} ${st.fontSize} ${st.fontFamily}`;
          return {text: c.measureText(s.selectedOptions[0].textContent).width, room: s.clientWidth - parseFloat(st.paddingLeft) - parseFloat(st.paddingRight)};}""")
        assert width['text'] <= width['room'] + 0.5, width
    finally:
        context.close()
