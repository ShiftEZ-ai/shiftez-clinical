"""People at the dev department's shape (FE-HANG, 2026-11-05): 150 people, 49 with aliases, ~780 rank memberships of
which 180 are one-week roster weeks, 450 clinic eligibilities, 400 leave records, 225 rotations and a published year of
6,000 duties. The People list, its Timeline layout and Where is everyone each draw, on the real /api/timeline, within
a bounded number of reads and a bounded main-thread time (no fixed sleeps: each waits for its drawing)."""
import json
import os
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from packages.domain.quotas import METRIC_VERSION
from .test_browser import browser, live_server, navigate, reconnect, set_layout, sign_in, toolbar  # noqa: F401
from .test_timeline_data import timeline_records

pytestmark = pytest.mark.browser

YEAR = date(2026, 11, 1)
PEOPLE, RANKS = 150, 6
EVERYONE = re.compile(r"^1[5-9]\d people")  # the board's count: the fixture's people and the department's own
# Main-thread work (long tasks) observed on the page while it draws.
LONG_TASKS = """window.__long=[];new PerformanceObserver(l=>{for(const e of l.getEntries())window.__long.push(e.duration);})
  .observe({type:'longtask',buffered:true});"""


def day(n):
    return YEAR + timedelta(days=n)


@pytest.fixture
def people_at_department_scale(browser, live_server, h):
    """Signs in once the data is there, opens People, its Timeline and Where is everyone, and measures each."""
    ids = timeline_records(h)
    dep, org = h.seed["department_id"], h.seed["organisation_id"]
    author = h.seed["identities"]["coordinator"]
    services = h.seed["services"]
    with transaction(h.app.state.engine, write=True) as db:
        ranks = [m.Category(department_id=dep, name=f"Scale rank {n}", starts_on=date(2020, 1, 1)) for n in range(RANKS)]
        db.add_all(ranks); db.flush()
        people = [{"id": str(uuid.uuid4()), "organisation_id": org, "display_name": f"Scale Person {k:03}", "short_code": f"S{k:03}",
                   "rota_aliases": [f"Alias {k:03}", f"Dr S{k:03}"] if k < 49 else [], "tags": ["night"] if k % 3 else []}
                  for k in range(PEOPLE)]
        db.execute(m.Person.__table__.insert(), people)
        pids = [p["id"] for p in people]
        memberships = []
        for k, pid in enumerate(pids):
            memberships.append({"person_id": pid, "category_id": ranks[k % RANKS].id, "starts_on": date(2024, 1, 1), "ends_on": None})
            # Earlier grades; then one-week roster memberships (the dev import's shape).
            memberships += [{"person_id": pid, "category_id": ranks[(k + n) % RANKS].id, "starts_on": date(2021 + n, 1, 1),
                             "ends_on": date(2021 + n, 7, 1)} for n in range(3)]
        for n in range(180):
            memberships.append({"person_id": pids[n % PEOPLE], "category_id": ranks[(n + 1) % RANKS].id,
                                "starts_on": day(7 * (n % 52)), "ends_on": day(7 * (n % 52) + 7)})
        db.execute(m.Membership.__table__.insert(), [{"id": m.uid(), "department_id": dep, **x} for x in memberships])
        db.execute(m.Eligibility.__table__.insert(), [{"id": m.uid(), "department_id": dep, "person_id": pid,
            "service_id": services[(k + n) % len(services)], "starts_on": date(2024, 1, 1)} for k, pid in enumerate(pids) for n in range(3)])
        db.execute(m.Availability.__table__.insert(), [{"id": m.uid(), "department_id": dep, "person_id": pids[n % PEOPLE],
            "start": datetime.combine(day((n * 11) % 350), datetime.min.time(), timezone.utc),
            "end": datetime.combine(day((n * 11) % 350 + 3), datetime.min.time(), timezone.utc),
            "kind": "recorded_leave", "leave_type": "Annual leave", "private_reason": "", "recorded_by": author} for n in range(400)])
        db.execute(m.Rotation.__table__.insert(), [{"id": m.uid(), "department_id": dep, "person_id": pids[n % PEOPLE],
            "cohort_id": ids["cohort"], "rotation_type_id": ids["type"], "starts_on": day(28 * (n % 12)), "ends_on": day(28 * (n % 12) + 28),
            "department_available": n % 2 == 0} for n in range(225)])
        workspace = m.Workspace(department_id=dep, name="Scale published year", starts_on=YEAR, ends_on=day(365),
                                status="draft", created_by=author, last_edited_by=author)
        db.add(workspace); db.flush()
        snapshot = record(db.get(m.AssignmentType, h.seed["type"]))
        shifts, assignments = [], []
        for n in range(6000):
            start = datetime.combine(day(n % 365), datetime.min.time(), timezone.utc) + timedelta(hours=8)
            sid = m.uid()
            shifts.append({"id": sid, "workspace_id": workspace.id, "assignment_type_id": h.seed["type"], "type_revision": snapshot["revision"],
                           "type_snapshot": snapshot, "location_id": h.seed["locations"][0], "start": start, "end": start + timedelta(hours=4)})
            assignments.append({"id": m.uid(), "shift_id": sid, "person_id": pids[n % PEOPLE], "start": start, "end": start + timedelta(hours=4)})
        db.execute(m.Shift.__table__.insert(), shifts)
        db.execute(m.ShiftService.__table__.insert(), [{"shift_id": s["id"], "service_id": services[0]} for s in shifts])
        db.execute(m.Assignment.__table__.insert(), assignments)
        workspace.status = "published"
        db.add(m.Publication(department_id=dep, workspace_id=workspace.id, sequence=1, starts_on=YEAR, ends_on=day(365),
            published_by=author, snapshot={"shifts": [], "services": {}, "locations": {}}, validation={"contracts": {"block_quota": METRIC_VERSION, "programmes": "programmes-v1"}},
            fingerprint="0" * 64))
    context, page = sign_in(browser, live_server, "coordinator")
    page.add_init_script(LONG_TASKS)
    reads = []
    page.on("request", lambda r: reads.append(r.url) if "/api/timeline?" in r.url and "limit=200" in r.url else None)
    board = page.get_by_role("region", name="People timeline")
    measured = {}
    try:
        page.reload()

        def table():
            navigate(page, "people")
            expect(page.locator("#main .dt-table tbody tr[data-row]").first).to_be_visible()
        measured["table"] = timed(page, table)

        def timeline():
            set_layout(page, "Timeline")
            # Rows are virtualised: the board counts everyone it laid out (the department's own people too).
            expect(board).to_contain_text(EVERYONE, timeout=60_000)
            expect(board.locator(".tl-row[data-row]").first).to_be_visible()
        measured["timeline"] = timed(page, timeline)
        measured["timeline"]["reads"] = len(reads)

        def where():
            toolbar(page, "Saved views").get_by_role("menuitemradio", name="Where is everyone").click()
            expect(board).to_contain_text(EVERYONE, timeout=60_000)
            expect(board.locator(".tl-bar").first).to_be_visible()
        measured["where_is_everyone"] = timed(page, where)
        measured["where_is_everyone"]["reads"] = len(reads) - measured["timeline"]["reads"]
        print("people at department scale", json.dumps(measured))
        if os.getenv("PEOPLE_SCALE_MEASUREMENTS"):
            with open(os.environ["PEOPLE_SCALE_MEASUREMENTS"], "w") as f:
                json.dump(measured, f, indent=2)
        yield page, measured, reads
    finally:
        context.close()


def timed(page, step):
    page.evaluate("window.__long=[]")
    started = time.perf_counter()
    step()
    return {"seconds": round(time.perf_counter() - started, 2), "long_task_ms": round(sum(page.evaluate("window.__long")))}


def test_people_timeline_and_where_is_everyone_draw_everyone_with_one_read_each(people_at_department_scale):
    _, measured, reads = people_at_department_scale
    # One year read per layout (every page of it in one, limit 200): the list's filters and groups redraw from it.
    assert measured["timeline"]["reads"] == 1 and measured["where_is_everyone"]["reads"] == 1, reads
    assert "bucket=month" in reads[-1] and "bucket" not in reads[0], reads


@pytest.mark.wall_benchmark
def test_people_at_department_scale_main_thread_budget(people_at_department_scale):
    _, measured, _ = people_at_department_scale
    # Measured on integration (FE-HANG, 5 Nov 2026, local): 144 / 406 / 277 ms of long tasks for the table, Timeline and
    # Where is everyone; the budget is about ten times that, for loaded runners (PW_CPU_THROTTLE=4 included).
    for step, budget in (("table", 2000), ("timeline", 4000), ("where_is_everyone", 4000)):
        assert measured[step]["long_task_ms"] < budget, measured


def test_a_refresh_keeps_the_timeline_drawn_and_shares_one_read(browser, live_server, h):
    """A refresh (a pulse, a save, coming back online) rebuilds People: the timeline keeps its drawing until the new
    answer lands, and refreshes while a read is on its way share one more read instead of each starting their own
    (each department year read can take the server tens of seconds; FE-HANG)."""
    context, page = sign_in(browser, live_server, "coordinator")
    held, seen, release = [], [], []
    year = lambda url: "/api/timeline?" in url and "limit=200" in url  # noqa: E731 (a page's read, not the one-day probe)
    page.on("request", lambda r: seen.append(r.url) if year(r.url) else None)

    def hold(route):
        route.continue_() if release else held.append(route)
    try:
        navigate(page, "people")
        set_layout(page, "Timeline")
        board = page.locator('#main .tl[role="region"][aria-label="People timeline"]')
        expect(board).to_be_visible()
        seen.clear()
        page.route(year, hold)
        for n in range(3):
            board.evaluate("(b, n) => b.dataset.before = n", n)
            with page.expect_request(lambda r: "/api/me" in r.url):
                reconnect(page)
            # The rebuilt page shows the drawing it had, not a skeleton, while its read is held.
            expect(page.locator('#main .tl[role="region"]:not([data-before])')).to_be_visible()
            expect(page.locator("#main .sk-timeline")).to_have_count(0)
        assert len(seen) == 1, seen  # one read on its way; the later refreshes wait for it
        release.append(True)
        with page.expect_request(lambda r: year(r.url)) as second:
            held.pop().continue_()  # then one more read, for both of them
        second.value.response()
        expect(board).to_be_visible()
        expect(page.locator("#main .sk-timeline")).to_have_count(0)
        assert len(seen) == 2, seen
    finally:
        context.close()
