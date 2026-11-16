"""The Schedule's dimensions are one list (schedule_dimensions.ts) matching the contract's axes, drawn by one grid
(schedule_grid.ts), and every axis is saved with the view."""
import re
from pathlib import Path

from .test_client_autoschedule_live import CLICKS, FAKE_DOM, node

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps" / "web" / "src"


def literal(name: str) -> list[str]:
    text = (ROOT / "packages" / "contracts" / "schemas.py").read_text(encoding="utf-8")
    return re.findall(r'"(\w+)"', re.search(rf"^{name} = Literal\[(.*?)\]$", text, re.M).group(1))


def test_dimensions_match_the_contract_axes():
    """One list with the server: the Schedule reads the generated dimensions and date axes, never a copy."""
    src = (WEB / "schedule_dimensions.ts").read_text(encoding="utf-8")
    assert "=PLANNER_DIMENSIONS;" in src and "=PLANNER_TIME_AXES" in src and "{key:'type',label:" not in src
    generated = (WEB / "generated.ts").read_text(encoding="utf-8")
    dims = re.findall(r'"key": "(\w+)"', re.search(r"^export const PLANNER_DIMENSIONS = (.*)$", generated, re.M).group(1))
    times = re.findall(r'\["(\w+)", "', re.search(r"^export const PLANNER_TIME_AXES = (.*)$", generated, re.M).group(1))
    columns = re.findall(r'"(\w+)"', re.search(r"columns: Literal\[(.*?)\]", (ROOT / "packages" / "contracts" / "schemas.py").read_text(encoding="utf-8")).group(1))
    assert sorted(times + dims) == sorted(columns)


def test_one_grid_draws_the_timeline_and_counts(client_modules):
    assert not (WEB / "schedule_timeline.ts").exists() and not (WEB / "schedule_counts.ts").exists()
    app = (WEB / "app.ts").read_text(encoding="utf-8")
    assert "scheduleGrid(" in app and "timelineGrid(" not in app and "countsGrid(" not in app
    grid = (WEB / "schedule_grid.ts").read_text(encoding="utf-8")
    # One delegated listener opens chips and "+N"; none per cell. Group heads are the shared groupToggle.
    assert set(re.findall(r"(\w+)\.addEventListener\('click'", grid)) == {"grid"}
    assert "groupToggle(" in grid
    # One click on a group toggle toggles exactly once (no double-bound handler).
    out = node(client_modules, "dom.js", FAKE_DOM + CLICKS + (
        "const seen=[],b=m.groupToggle('Group',3,true,open=>seen.push(open));"
        "b.click();const one={seen:[...seen],expanded:b.attrs['aria-expanded']};b.click();"
        "console.log(JSON.stringify({one,two:{seen,expanded:b.attrs['aria-expanded']}}));"))
    assert out == {"one": {"seen": [False], "expanded": "false"}, "two": {"seen": [False, True], "expanded": "true"}}


def test_every_axis_is_saved_with_the_view():
    app = (WEB / "app.ts").read_text(encoding="utf-8")
    definition = app[app.index("const plannerDefinition="):app.index("const sameView=")]
    for field in ("rows:", "columns:", "subgroup:", "cells:", "collapsed:"):
        assert field in definition, field
    apply = app[app.index("function applyPlannerView"):]
    apply = apply[:apply.index("\n}\n")]
    assert "gridRows=" in apply and "gridSubgroup=" in apply and "gridCells=" in apply
    assert "open as days until" not in apply  # the 3b29c8633 stop-gap is gone
