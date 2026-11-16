"""Every collection toolbar spans exactly its list's width and sits the same gap above it; page actions in the top bar
sit on the right with the main action last, next to the bell."""
import pytest

from .test_browser import browser, live_server, sign_in, navigate, expect_loaded  # noqa: F401
from .test_browser_requests import leave
from .test_browser_rule_catalogue import flag

pytestmark = pytest.mark.browser

GEOMETRY = '''([bar, list]) => {
  const b = document.querySelector(bar).getBoundingClientRect(), l = document.querySelector(list).getBoundingClientRect();
  const controls = [...document.querySelector(bar).children].filter(n => n.getBoundingClientRect().height)
    .map(n => n.getBoundingClientRect().bottom);
  return {left: l.left - b.left, right: l.right - b.right, gap: l.top - Math.max(...controls)};
}'''
TOP_BAR = '''() => {
  const bell = document.querySelector('.topbar .bell').getBoundingClientRect();
  const acts = [...document.querySelectorAll('.topbar .page-actions > *, .topbar .planner-bar > :not(.grow)')]
    .filter(n => n.getBoundingClientRect().width && n.getBoundingClientRect().left > innerWidth / 2);
  const last = acts.at(-1);
  return {room: bell.left - last.getBoundingClientRect().right, label: last.getAttribute('aria-label') || last.textContent.trim(),
          primaryLast: !acts.some(n => n.matches('.primary')) || last.matches('.primary')};
}'''


@pytest.mark.parametrize('route,bar,listed', [
    ('people', '#main .dt-toolbar', '#main .dt-body'),
    ('requests', '#main .dt-toolbar', '#main .dt-body'),
    ('rules', '#main .dt-toolbar', '#main .rt-body'),
    ('calendar', '#main .planner-toolbar', '#main #schedule-view'),
])
def test_toolbar_spans_its_list_with_one_gap(browser, live_server, h, route, bar, listed):
    leave(h, 'coordinator')
    h.shift(h.workspace('Toolbar layout rota'))
    if route == 'rules':
        flag(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        navigate(page, route)
        page.locator(listed).first.wait_for()
        g = page.evaluate(GEOMETRY, [bar, listed])
        assert abs(g['left']) <= 1 and abs(g['right']) <= 1, g  # same side gutters as the list
        assert 12 <= g['gap'] <= 16, g  # ~14px between the controls and the list
        top = page.evaluate(TOP_BAR)
        assert 0 <= top['room'] <= 24 and top['primaryLast'], top  # on the right; the main action last, by the bell
    finally:
        context.close()
