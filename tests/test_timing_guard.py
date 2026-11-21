"""Flake guard: tests wait for a state (expect(...), wait_for, a polled condition), never for a fixed time.

A fixed sleep is either too short on a loaded CI runner (a flake) or longer than needed everywhere (slow). Existing
fixed waits are listed below with their count; a new one fails this test. When a fixed wait is truly the behaviour
under test (e.g. "nothing redraws during the next poll"), add it here with the count, in the same change. On every
event CI retries failed non-cpu_budget nodes once in the same partition, retains both attempts and reports FLAKY.
Skipped/missing retry results fail. Reviewed tests/quarantine.json entries still execute as expected failures;
expired entries stay active and are reported for triage.
"""
import re
from pathlib import Path

ALLOWED = {
    "test_browser.py": 3,  # live_server's readiness poll; PW_API_DELAY_MS (opt-in load simulation); a 70 ms settle per surface
    "test_browser_fairness_dashboard.py": 2,
    "test_browser_improvements.py": 2,
    "test_browser_operations.py": 2,
    "test_browser_operator_footer.py": 1,
    "test_browser_planner.py": 2,  # SI-01: "past the help delay, still no help card" (nothing happens)
    "test_browser_planner_phone.py": 2,
    "test_browser_qa_planner_a2.py": 2,
    "test_browser_qa_staff.py": 4,  # "a later check with nothing changed does not redraw" needs a full poll interval
    "test_browser_redesign.py": 1,
    "test_browser_recurring_sessions.py": 2,  # a Python-side poll of the API until saved; a deliberately slow check (held route) under test
    "test_browser_catalogue_hydration.py": 2,  # "the page/dialog stays on its skeleton while the lists are held" (nothing happens)
    "test_browser_qa_rules_rc12.py": 1,  # polls the worker queue from Python until the panel has queued the behaviour check
    "test_browser_reference.py": 1,
    "test_browser_requests.py": 1,  # a held bulk record must outlast an unclaimed change's 1.2 s Undo message
    "test_browser_release.py": 2,
    "test_browser_rules_in_planner.py": 3,
    "test_browser_uiux_polling.py": 4,  # asserts what does NOT happen during a poll
    "test_browser_uiux_sessions.py": 1,
    "test_browser_uiux_today.py": 2,
}
FIXED_WAIT = re.compile(r"wait_for_timeout\(|\bsleep\(")


def test_no_new_fixed_waits_in_tests():
    found = {p.name: len(FIXED_WAIT.findall(p.read_text(encoding="utf-8"))) for p in Path(__file__).parent.glob("test_*.py")
             if p.name != Path(__file__).name}
    over = {name: count for name, count in found.items() if count > ALLOWED.get(name, 0)}
    assert not over, f"New fixed waits (wait_for_timeout/sleep) in {over}; wait for a state with expect(...) instead"


# Playwright caches a function-form predicate after its first (CSP-exempt) evaluation; a bare expression is eval'd
# again on every poll, which the app's CSP (script-src 'self', no 'unsafe-eval') rejects with an EvalError.
BARE_PREDICATE = re.compile(r"""wait_for_function\(\s*[rbuf]?(?=(["']+))\1(?!\s*(async\s+)?(function\b|\([^()]*\)\s*=>|\w+\s*=>))""")


def test_polled_predicates_are_functions_under_csp():
    bare = [f"{p.name}:{text.count(chr(10), 0, m.start()) + 1}"
            for p in Path(__file__).parent.glob("test_*.py") if p.name != Path(__file__).name
            for text in [p.read_text(encoding="utf-8")] for m in BARE_PREDICATE.finditer(text)]
    assert not bare, f"wait_for_function with a bare expression at {bare}; pass a function, e.g. '() => window.ready'"
