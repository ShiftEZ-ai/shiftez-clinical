from scripts import affected_tests as selector


def browser_files():
    return {test for test in selector.test_files() if selector.suite(test) == "browser"}


def test_a_shared_ui_module_selects_every_browser_test():
    tests = set(selector.test_files())
    for path in sorted(selector.SHARED_WEB_SOURCES | selector.WEB_ENTRY_SOURCES):
        owners = selector.direct_test_owners([path], tests)
        assert owners is not None and browser_files() <= owners, path
    # The collection toolbar tests never name table_view.ts; the shared rule still selects them.
    assert "tests/test_browser_collection_toolbars.py" in selector.direct_test_owners(
        ["apps/web/src/table_view.ts"], tests)


def test_a_shared_ui_module_does_not_widen_the_unit_selection():
    tests = set(selector.test_files())
    owners = selector.direct_test_owners(["apps/web/src/table_view.ts"], tests)
    unit = {test for test in tests if selector.suite(test) == "unit"}
    assert not unit <= owners


def test_a_page_module_keeps_its_direct_owners():
    tests = set(selector.test_files())
    owners = selector.direct_test_owners(["apps/web/src/planner_week.ts"], tests)
    assert owners is not None and not browser_files() <= owners


# Backend changes whose merge groups failed browser tests that their pull-request runs had not selected.
MISSED_BACKEND_CHANGES = [
    (["apps/api/services/bulk.py"], "tests/test_browser_perf.py"),
    (["apps/api/services/read_projection_warming.py", "apps/api/services/read_projections.py"],
     "tests/test_browser_improvements.py"),
    (["apps/api/services/departments.py", "apps/api/services/display_settings.py", "apps/api/settings.py",
      "packages/contracts/schemas.py"], "tests/test_browser_resolve_issues.py"),
    (["apps/api/commands.py", "apps/api/services/planning.py", "apps/api/snapshots.py",
      "packages/domain/rota_summary.py"], "tests/test_browser_attachments.py"),
]


def test_a_browser_driven_service_change_selects_every_browser_test():
    tests = set(selector.test_files())
    for changed, failed in MISSED_BACKEND_CHANGES:
        owners = selector.direct_test_owners(changed, tests)
        assert owners is not None and failed in owners and browser_files() <= owners, changed
    for path in ("apps/api/commands.py", "apps/api/snapshots.py", "apps/api/services/planning.py"):
        owners = selector.direct_test_owners([path], tests)
        unit = {test for test in tests if selector.suite(test) == "unit"}
        assert browser_files() <= owners and not unit <= owners, path


def test_other_backend_paths_keep_their_direct_owners():
    tests = set(selector.test_files())
    owners = selector.direct_test_owners(["packages/domain/conditions.py"], tests)
    assert owners is not None and not browser_files() <= owners
