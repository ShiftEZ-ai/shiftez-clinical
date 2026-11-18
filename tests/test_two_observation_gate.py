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
                for event in ("pull_request", "push"):
                    with patch.dict(os.environ, {"GITHUB_EVENT_NAME": event}):
                        with self.assertRaisesRegex(ValueError, "Infrastructure"):
                            maker.product_error_nodes(self.folder)
                with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "merge_group"}):
                    self.assertEqual(maker.product_error_nodes(self.folder), {NODE} if tag == "error" else set())
                    path = self.folder / "junit.xml"
                    report = ET.parse(path)
                    case = ET.SubElement(report.getroot(), "testcase",
                                         classname="tests.test_product", name="test_b")
                    ET.SubElement(case, "failure", message="ConnectionError: connection refused")
                    report.write(path)
                    with self.assertRaisesRegex(ValueError, "Infrastructure failure in multiple test nodes"):
                        maker.product_error_nodes(self.folder)

    def test_database_lock_is_infrastructure(self):
        self.report("error", "sqlalchemy.exc.OperationalError: database is locked")
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}):
            with self.assertRaisesRegex(ValueError, "Infrastructure"):
                maker.product_error_nodes(self.folder)
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "merge_group"}):
            classified = maker.product_error_nodes(self.folder)
            self.assertEqual(classified, {NODE})
            runs, _ = known_skips.comparison_runs([{NODE: "error"}, {NODE: "passed"}], set(),
                                                  release=False, allowed_errors=classified)
            self.assertTrue(maker.evaluate(self.current, self.current, runs, product_errors=classified))
            with self.assertRaisesRegex(ValueError, "New errors.*test_a"):
                self.evaluate(["error", "error"], product_errors=classified)
            self.report("error", "error collecting tests/test_product.py")
            with self.assertRaisesRegex(ValueError, "Collection error"):
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

        summary = self.folder / "summary.md"
        for event in ("pull_request", "workflow_dispatch", "merge_group", "push"):
            with self.subTest(event=event), patch.dict(os.environ, {"GITHUB_EVENT_NAME": event}):
                stale_output = maker.stale_report_dir(self.folder)
                if event in {"merge_group", "push"}:
                    self.assertTrue(self.evaluate(["passed", "passed"], stale_output=stale_output))
                else:
                    with self.assertRaisesRegex(ValueError, "Stale passing"):
                        self.evaluate(["passed", "passed"], stale_output=stale_output)

        artifact = self.folder / "stale-report.json"
        with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}):
            self.assertTrue(self.evaluate(["passed", "passed"], stale_output=self.folder))
            self.assertEqual(json.loads(artifact.read_text()), [NODE])
            self.assertIn("- " + NODE, summary.read_text())
            self.assertTrue(self.evaluate(["passed", "error"], stale_output=self.folder))
            self.assertEqual(json.loads(artifact.read_text()), [])
            down = "tests/test_product.py::test_unlisted"
            runs = [{NODE: "passed", down: "failed"}] * 2
            with self.assertRaisesRegex(ValueError, "New failures.*test_unlisted"):
                maker.evaluate(self.current, self.current, runs, stale_output=self.folder)
            self.assertEqual(json.loads(artifact.read_text()), [NODE])

    def test_plan_and_reader_use_two_replicas_on_every_event(self):
        import trusted_ci
        durations = {"suites": {suite: {"files": {"tests/test_unit.py": 1}} for suite in ("unit", "browser")}}
        for name in ("test_unit.py", "test_browser_unit.py"):
            target = self.folder / "tests" / name
            target.parent.mkdir(exist_ok=True)
            target.write_text("def test_a(): pass\n")
        def source_git(*args):
            return "tests/test_unit.py\ntests/test_browser_unit.py" if args[0] == "ls-files" else "a" * 40
        previous = Path.cwd()
        try:
            os.chdir(self.folder)
            for event in ("pull_request", "merge_group", "workflow_dispatch", "push"):
                with self.subTest(event=event), patch.dict(os.environ, {"GITHUB_EVENT_NAME": event}), \
                     patch.object(maker, "at_revision", return_value=self.current), \
                     patch.object(trusted_ci, "TRUSTED", self.folder), \
                     patch.object(trusted_ci, "settings", return_value={"unit_shards": 1, "browser_shards": 1}), \
                     patch.object(trusted_ci.hosted, "git", side_effect=source_git), \
                     patch.object(trusted_ci.hosted, "load_durations", return_value=durations), \
                     patch.object(trusted_ci.canaries, "sha", return_value="b" * 64):
                    self.assertEqual(maker.context("a" * 40)["replicas"], [1, 2])
                    self.assertEqual(trusted_ci.plan("a" * 40, False)["replicas"], [1, 2])
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
