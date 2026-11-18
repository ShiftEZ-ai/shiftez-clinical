"""Ordinary gate observations never grant an implicit committed allowance."""
from datetime import date, timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "ci"), str(ROOT / ".github/scripts"), str(ROOT / "tests")]
import make_known_failures as maker
import known_skips

NODE = "tests/test_product.py::test_a"


class TwoObservationGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ["RUNNER_TEMP"])
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        passed = {NODE: "passed"}
        self.current = maker.candidate_list([passed, passed], set(), "a" * 40, 1,
            (date.today() + timedelta(days=30)).isoformat(), 80, cleanup_cap=0,
            root_evidence_sha256="b" * 64,
            root_runs=[{"run_id": "1", "run_attempt": "1", "replica": r} for r in (1, 2)])

    def evaluate(self, values, **kwargs):
        return maker.evaluate(self.current, self.current, [{NODE: v} for v in values], **kwargs)

    def report(self, tag, message, trace="tests/test_product.py:9: fixture"):
        suite = ET.Element("testsuite")
        case = ET.SubElement(suite, "testcase", classname="tests.test_product", name="test_a")
        ET.SubElement(case, tag, message=message).text = trace
        ET.ElementTree(suite).write(self.folder / "junit.xml")
        (self.folder / "shard-receipt.json").write_text(json.dumps({"reports": [{"file": "junit.xml"}]}))

    def test_assertion_once_is_unstable_without_rewriting_list(self):
        self.assertTrue(self.evaluate(["failed", "passed"]))
        before = maker.encoded(self.current)
        summary = self.folder / "summary.md"
        with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}):
            rows = maker.report_outside(self.current, [{NODE: "failed"}, {NODE: "passed"}], self.folder)
        self.assertIn("UNSTABLE outside-list nodes: 1", summary.read_text())
        self.assertIn(NODE, summary.read_text())
        self.assertEqual(json.loads((self.folder / "outside-observations.json").read_text())["unstable_count"], 1)
        self.assertEqual(rows[0]["test_file"], "tests/test_product.py")
        self.assertEqual(rows[0]["status"], "PROPOSED")
        self.assertEqual(maker.encoded(self.current), before)
        self.assertFalse((self.folder / "known-failures.json").exists())

    def test_assertion_twice_blocks(self):
        with self.assertRaisesRegex(ValueError, "New failures"):
            self.evaluate(["failed", "failed"])

    def test_product_error_once_is_unstable_after_classification(self):
        self.report("error", "TypeError: invalid product collection")
        reviewed = maker.product_error_nodes(self.folder)
        self.assertEqual(reviewed, {NODE})
        self.assertTrue(self.evaluate(["error", "passed"], product_errors=reviewed))

    def test_product_error_twice_blocks(self):
        with self.assertRaisesRegex(ValueError, "New errors"):
            self.evaluate(["error", "error"], product_errors={NODE})

    def test_mixed_error_and_failure_is_red_twice(self):
        with self.assertRaises(ValueError):
            self.evaluate(["error", "failed"], product_errors={NODE})

    def test_unreviewed_error_cannot_be_softened(self):
        with self.assertRaisesRegex(ValueError, "New errors"):
            self.evaluate(["error", "passed"])

    def test_service_error_blocks_on_either_report_tag(self):
        for tag in ("failure", "error"):
            with self.subTest(tag=tag):
                self.report(tag, "ConnectionError: connection refused")
                with self.assertRaisesRegex(ValueError, "Infrastructure"):
                    maker.product_error_nodes(self.folder)

    def test_database_lock_is_infrastructure(self):
        self.report("error", "sqlalchemy.exc.OperationalError: database is locked")
        with self.assertRaisesRegex(ValueError, "Infrastructure"):
            maker.product_error_nodes(self.folder)

    def test_schema_error_with_product_frame_is_product(self):
        self.report("error", "sqlalchemy.exc.OperationalError: no such table: example")
        self.assertEqual(maker.product_error_nodes(self.folder), {NODE})

    def test_unknown_error_and_third_party_only_trace_require_review(self):
        for message, trace in (("RuntimeError: unexplained", "tests/test_product.py:9"),
                               ("TypeError: invalid", "site-packages/example/lib.py:9")):
            with self.subTest(message=message):
                self.report("error", message, trace)
                with self.assertRaisesRegex(ValueError, "Unclassified"):
                    maker.product_error_nodes(self.folder)

    def test_single_or_incomplete_observation_does_not_grant_a_pass(self):
        for runs in ([{NODE: "failed"}], [{NODE: "failed"}, {}], [{NODE: "failed"}, {NODE: "skipped"}]):
            with self.subTest(runs=runs), self.assertRaises(ValueError):
                maker.evaluate(self.current, self.current, runs)

    def test_main_release_remains_strict_for_single_sighting(self):
        for outcome in ("failed", "error"):
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                known_skips.comparison_runs([{NODE: outcome}, {NODE: "passed"}], set(), release=True, allowed_errors={NODE})

    def test_listed_nodes_are_exempt_from_both_classifiers(self):
        for tag in ("failure", "error"):
            for message in ("OperationalError: database is locked", "RuntimeError: unexplained",
                            "FileNotFoundError: absent", "PermissionError: permission denied"):
                with self.subTest(tag=tag, message=message):
                    self.report(tag, message)
                    self.assertEqual(maker.product_error_nodes(self.folder, listed={NODE}), set())
        self.report("error", "error collecting tests/test_product.py")
        with self.assertRaisesRegex(ValueError, "Collection"):
            maker.product_error_nodes(self.folder, listed={NODE})

    def test_listed_mixed_red_is_reported_and_proposed_for_remark(self):
        self.current["fails"] = [{"node": NODE, "class": "baseline"}]
        before = maker.encoded(self.current)
        for outcome in ("failed", "error"):
            for values in ([outcome, "passed"], ["passed", outcome]):
                with self.subTest(values=values):
                    runs = [{NODE: value} for value in values]
                    self.assertTrue(maker.evaluate(self.current, self.current, runs))
                    maker.report_outside(self.current, runs, self.folder)
                    report = json.loads((self.folder / "outside-observations.json").read_text())
                    self.assertEqual(report["listed_unstable_count"], 1)
                    self.assertEqual(report["listed_unstable"][0]["status"], "PROPOSED_REMARK")
                    self.assertEqual(report["listed_unstable"][0]["observations"], values)
                    self.assertEqual(report["listed_unstable"][0]["test_file"], "tests/test_product.py")
        self.assertEqual(maker.encoded(self.current), before)

    def test_listed_green_twice_is_stale_and_red_twice_is_allowed(self):
        for kind, red in (("baseline", "failed"), ("baseline-error", "error")):
            self.current["fails"] = [{"node": NODE, "class": kind}]
            with self.subTest(kind=kind):
                self.assertTrue(self.evaluate([red, red]))
                self.assertTrue(self.evaluate([red, "passed"]))
                with self.assertRaisesRegex(ValueError, "Stale passing"):
                    self.evaluate(["passed", "passed"])

    def test_plan_and_reader_use_two_replicas_on_every_event(self):
        import trusted_ci
        changed_test = "tests/test_changed.py"
        direct_owner = "tests/test_direct.py"
        unrelated = {"tests/test_other.py", "tests/test_browser_other.py"}
        files = trusted_ci.PR_SMOKE | {changed_test, direct_owner} | unrelated
        durations = {"suites": {suite: {"files": dict.fromkeys(files, 1)} for suite in ("unit", "browser")}}
        for name in files:
            target = self.folder / name
            target.parent.mkdir(exist_ok=True)
            target.write_text("def test_a(): pass\n")
        head, base = "a" * 40, "b" * 40
        def source_git(*args):
            return "\n".join(sorted(files)) if args[0] == "ls-files" else head
        previous = Path.cwd()
        try:
            os.chdir(self.folder)
            cases = [("pull_request", "apps/api/settings_extra.py", [], "affected"),
                     ("pull_request", "apps/api/settings_extra.py", ["full-ci"], None),
                     ("pull_request", "scripts/tool.py", [], None),
                     ("pull_request", "test_support/plugin.py", [], None),
                     ("pull_request", "apps/api/broken.py", [], "changed-tests-and-smoke"),
                     *((event, "", [], None) for event in ("merge_group", "workflow_dispatch", "push"))]
            for event, path, labels, reason in cases:
                payload = {"pull_request": {"head": {"sha": head}, "base": {"sha": base, "ref": "dev"},
                                            "labels": [{"name": label} for label in labels]}}
                def event_git(*args):
                    if args[0] == "merge-base":
                        return base.encode()
                    return b"" if "--diff-filter=D" in args else (path + "\0" + changed_test + "\0").encode()
                def selector(root, paths, inventory):
                    self.assertEqual((root, paths, inventory), (self.folder, sorted([path, changed_test]), files))
                    if path.endswith("broken.py"):
                        raise RuntimeError("selector failed")
                    return {direct_owner}
                with self.subTest(event=event, path=path, labels=labels), \
                     patch.dict(os.environ, {"GITHUB_EVENT_NAME": event, "TRUSTED_BOOTSTRAP": "false"}), \
                     patch.object(maker, "at_revision", return_value=self.current), \
                     patch.object(trusted_ci, "TRUSTED", self.folder), \
                     patch.object(trusted_ci, "settings", return_value={"unit_shards": 3, "browser_shards": 2}), \
                     patch.object(trusted_ci.hosted, "git", side_effect=source_git), \
                     patch.object(trusted_ci.subprocess, "check_output", return_value=head), \
                     patch.object(trusted_ci.policy, "git", side_effect=event_git), \
                     patch.object(trusted_ci.policy, "event", return_value=payload), \
                     patch.object(trusted_ci.policy, "read_file", return_value=b"x = 1\n"), \
                     patch.object(trusted_ci.policy, "control_inventory", return_value={"helpers": {"test_support/plugin.py"}, "product_imports": set()}), \
                     patch.object(trusted_ci.runpy, "run_path", return_value={"direct_test_owners": selector}) as load_selector, \
                     patch.object(trusted_ci.hosted, "load_durations", return_value=durations), \
                     patch.object(trusted_ci.canaries, "sha", return_value="b" * 64):
                    self.assertEqual(maker.context(base)["replicas"], [1, 2])
                    plan = trusted_ci.plan(base, False)
                    self.assertEqual(plan["replicas"], [1, 2])
                    actual = {file for row in plan["suites"].values() for shard in row["shards"] for file in shard["files"]}
                    if reason is None:
                        self.assertNotIn("selection", plan)
                        self.assertEqual(actual, files)
                        load_selector.assert_not_called()
                    else:
                        self.assertEqual(plan["selection"], {"reason": reason, "deleted_test_files": []})
                        expected = trusted_ci.PR_SMOKE | {changed_test}
                        if reason == "affected":
                            expected.add(direct_owner)
                        self.assertEqual(actual, expected)
                        self.assertEqual(trusted_ci.census_files(plan, files), expected)
        finally:
            os.chdir(previous)

    def test_dispatch_and_push_fetch_the_measured_pr_before_reader(self):
        import test_working_policy as fixtures
        from unittest.mock import call
        for event in ("workflow_dispatch", "push"):
            with self.subTest(event=event):
                fixture = fixtures.PolicyTests()
                fixture.setUp()
                fetched = fixture.exercise_verdict_materialization("dev", event=event)
                self.assertIn(call("e" * 40, "e" * 40), fetched)


if __name__ == "__main__":
    unittest.main()
