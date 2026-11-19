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
