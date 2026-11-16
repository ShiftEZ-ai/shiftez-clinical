"""Grouped cards on a phone (shared dataTable): a group's box, chevron and long name sit on one row, the name
left-aligned and wrapping beside the box, not centred on a line of its own."""
import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401
from .test_browser_resolve_issues import conflicted, issues_page

pytestmark = pytest.mark.browser


def test_a_long_group_name_wraps_beside_its_box_on_a_phone(browser, live_server, h):
    w, _ = conflicted(h)
    # An open place of its own (a leave's cover gap is said inside its one leave issue): the long-named group.
    w = h.shift(w, '2026-10-07T08:00:00+03:00', '2026-10-07T12:00:00+03:00', people=[])['workspace']
    w = h.lifecycle(w, 'validate')['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 390, 'height': 844})
    try:
        navigate(page, f'schedule/issues?draft={w["id"]}&view=checks')
        main = issues_page(page)
        head = main.locator('.dt-card-group').filter(has_text='Fewer people than the minimum')
        expect(head).to_be_visible()
        box, toggle = head.get_by_role('checkbox').bounding_box(), head.locator('.dt-group-toggle').bounding_box()
        assert box['y'] >= toggle['y'] - 2 and box['y'] + box['height'] <= toggle['y'] + toggle['height'] + 2, (box, toggle)
        assert toggle['x'] > box['x'] + box['width'] - 1, 'the name is beside the box'
        assert head.locator('.dt-group-toggle').evaluate('b => getComputedStyle(b).textAlign') in ('left', 'start')
    finally:
        context.close()
