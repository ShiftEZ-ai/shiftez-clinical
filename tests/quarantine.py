"""Run reviewed quarantine entries and report expiry for triage."""
import json
from pathlib import Path

import pytest

from ci.quarantine_registry import QUARANTINE_XFAIL_PREFIX, canonical_node, decode_registry, expired_entries, utc_today

NO_RETRY_MODULES = {"test_clock_guard.py", "test_version.py", "test_production_kit.py", "test_browser_seed_cache.py"}
QUARANTINE_FILE = Path(__file__).with_name("quarantine.json")


def entries(path, today):
    return decode_registry(path.read_text(encoding="utf-8"), today)


def pytest_configure(config):
    try:
        config._quarantine_entries = entries(QUARANTINE_FILE, utc_today())
    except (ValueError, OSError, RecursionError) as exc:
        raise pytest.UsageError("Invalid " + QUARANTINE_FILE.name + ": " + str(exc)) from None


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    registry = config._quarantine_entries
    parameterized = {node.partition("[")[0] for node in registry if "[" in node}
    # Hosted evidence retains raw outcomes for the trusted verdict.
    mark_expected = not config.pluginmanager.hasplugin("hosted_ci")
    for item in items:
        module = Path(str(item.fspath)).name
        if item.get_closest_marker("cpu_budget") or module.startswith("test_ci_") or module in NO_RETRY_MODULES:
            item.user_properties.append(("fmuc_no_retry", "cpu_budget" if item.get_closest_marker("cpu_budget") else "deterministic_ci_guard"))
        base, _, _ = item.nodeid.partition("[")
        node = canonical_node(item.nodeid) if base in parameterized else item.nodeid
        entry = registry.get(node)
        if entry:
            item.user_properties.append(("fmuc_quarantine", json.dumps(entry, sort_keys=True)))
        if entry and mark_expected:
            reason = f"{QUARANTINE_XFAIL_PREFIX}{entry['owner']}, expires {entry['expires']}): {entry['reason']}"
            # Do not add the legacy quarantine marker: existing selectors would deselect the test.
            item.add_marker(pytest.mark.xfail(reason=reason, strict=False))


def pytest_terminal_summary(terminalreporter):
    reports = [report for rows in terminalreporter.stats.values() for report in rows
               if getattr(report, "wasxfail", "").startswith(QUARANTINE_XFAIL_PREFIX)]
    expired = expired_entries(terminalreporter.config._quarantine_entries, utc_today())
    if expired:
        terminalreporter.section("Expired quarantine (still active)")
        for row in expired:
            terminalreporter.write_line(f"{row['nodeid']}: expired {row['expires']}, issue {row['issue']}")
    if reports:
        terminalreporter.section("Reviewed quarantine (tests executed)")
        for report in reports:
            terminalreporter.write_line(f"{report.nodeid}: {report.outcome} - {report.wasxfail}")
