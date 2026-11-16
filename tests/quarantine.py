"""Reviewed, expiring expected failures; quarantined tests still execute normally."""
from datetime import date, datetime, timezone
import json
from pathlib import Path

import pytest

NO_RETRY_MODULES = {"test_clock_guard.py", "test_version.py", "test_production_kit.py", "test_browser_seed_cache.py"}


def entries(path, today, *, current=True):
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("quarantine must be a JSON list")
    found = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"nodeid", "reason", "owner", "added", "expires"}:
            raise ValueError("quarantine entries require nodeid, reason, owner, added, expires")
        if any(not isinstance(value, str) or not value.strip() for value in row.values()):
            raise ValueError("quarantine fields must be nonempty strings")
        node = row["nodeid"]
        if node in found or "::" not in node or not node.startswith("tests/test_"):
            raise ValueError("invalid or duplicate quarantine nodeid: " + node)
        added, expires = date.fromisoformat(row["added"]), date.fromisoformat(row["expires"])
        if not added <= today < expires:
            if current:
                raise ValueError("expired or future quarantine: " + node + " (expires " + row["expires"] + ")")
            continue
        found[node] = row
    return found


def pytest_configure(config):
    try:
        config._quarantine_entries = entries(Path(__file__).with_name("quarantine.json"), datetime.now(timezone.utc).date(), current=False)  # real clock: quarantine expiry is wall-clock policy
    except (ValueError, OSError) as exc:
        config._quarantine_entries = {}
        config._quarantine_error = str(exc)  # The dedicated validity test fails without aborting unrelated sessions.


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    for item in items:
        module = Path(str(item.fspath)).name
        if item.get_closest_marker("cpu_budget") or module.startswith("test_ci_") or module in NO_RETRY_MODULES:
            item.user_properties.append(("fmuc_no_retry", "cpu_budget" if item.get_closest_marker("cpu_budget") else "deterministic_ci_guard"))
        entry = config._quarantine_entries.get(item.nodeid)
        if entry:
            reason = f"QUARANTINED ({entry['owner']}, expires {entry['expires']}): {entry['reason']}"
            # Do not add the legacy quarantine marker: existing selectors would deselect the test.
            item.add_marker(pytest.mark.xfail(reason=reason, strict=False))
            item.user_properties.append(("fmuc_quarantine", json.dumps(entry, sort_keys=True)))


def pytest_terminal_summary(terminalreporter):
    reports = [report for rows in terminalreporter.stats.values() for report in rows
               if getattr(report, "wasxfail", "").startswith("QUARANTINED")]
    if reports:
        terminalreporter.section("Reviewed quarantine (tests executed)")
        for report in reports:
            terminalreporter.write_line(f"{report.nodeid}: {report.outcome} - {report.wasxfail}")
