"""D78 budget: drawing every person as the one pill, teams as a tree and every issue as the shared marker (with the row
and day roll-ups) must not slow the Week grid's first paint against the same grid without them, at department scale
(test_backend_scale.scale_department: 163 people, ~768 sessions in four weeks).

The baseline is measured in the same run, on the same data and runner: the grid drawn by a saved view with Supervision
teams and Rule marks off. So the budget is a ratio, not a stored number, and holds on any runner. Each mode is opened
the same way (from another page, as the sidebar does) and timed until its sessions are drawn and painted (two frames);
the fastest of a few opens counts, so one slow frame does not decide it. No fixed waits.

shortcut: the margin (RATIO, SLACK_MS) is generous because it could not be measured on the owner's Mac (testing there is
paused, 7 Nov 2026); tighten it from the first CI run's printed timings (see the test's output)."""
import os

import pytest

from .test_backend_scale import scale_department
from .test_browser import browser, live_server, navigate, pick_view, sign_in  # noqa: F401
from .test_browser_frontend_budget import timed_open

pytestmark = pytest.mark.browser

SLOWER = (2 if os.getenv('CI') else 1) * float(os.getenv('PW_CPU_THROTTLE') or 1)
RATIO = 1.5            # the new drawing may cost at most half again the plain grid's first paint
SLACK_MS = 150 * SLOWER  # plus a small absolute slack, for a fast baseline
OPENS = 3


def saved_view(h, name, properties):
    return h.command('coordinator', 'table_view.create', {'table_key': 'planner', 'name': name,
        'definition': {'planner': {'mode': 'week', 'range': 'week', 'properties': properties}}})['view']


@pytest.mark.wall_benchmark
def test_week_first_paint_with_pills_teams_and_markers_stays_within_the_plain_grid_budget(browser, live_server, h):
    w, *_ = scale_department(h)
    w = h.lifecycle(w, 'validate')['workspace']
    plain = saved_view(h, 'Fictional plain grid', [{'key': 'supervisor', 'on': False}, {'key': 'rule', 'on': False}])
    drawn = saved_view(h, 'Fictional D78 grid', [{'key': 'supervisor', 'on': True, 'form': 'tree'}, {'key': 'rule', 'on': True}])
    planner = f"schedule/week/{w['starts_on']}?edit={w['id']}"
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        times = {}
        for view in (plain, drawn):
            navigate(page, planner)
            pick_view(page, view['name'])
            runs = []
            for _ in range(OPENS):
                navigate(page, 'people')
                runs.append(timed_open(page, '#' + planner, 'schedule', '.wk-grid [data-session]'))
            times[view['name']] = min(runs)
        base, new = times[plain['name']], times[drawn['name']]
        print(f'D78 Week first paint: plain {base:.0f} ms, pills/teams/markers {new:.0f} ms (budget {base * RATIO + SLACK_MS:.0f} ms)')
        assert new <= base * RATIO + SLACK_MS, times
    finally:
        context.close()
