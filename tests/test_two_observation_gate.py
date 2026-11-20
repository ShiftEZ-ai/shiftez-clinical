"""Ordinary gate observations never grant an implicit committed allowance."""
from contextlib import chdir
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
STALE_SHA = "a" * 40


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
                    retry = maker.retry_run({"replicas": [1]})
                    self.assertEqual(maker.product_error_nodes(self.folder, allow_retry=retry), {NODE} if tag == "error" else set())
                    path = self.folder / "junit.xml"
                    report = ET.parse(path)
                    case = ET.SubElement(report.getroot(), "testcase",
                                         classname="tests.test_product", name="test_b")
                    ET.SubElement(case, "failure", message="ConnectionError: connection refused")
                    report.write(path)
                    with self.assertRaisesRegex(ValueError, "Infrastructure failure in multiple test nodes"):
                        maker.product_error_nodes(self.folder, allow_retry=retry)
                    with patch.object(maker, "recovered_nodes", return_value={NODE, "tests/test_product.py::test_b"}):
                        with self.assertRaisesRegex(ValueError, "Infrastructure failure in multiple test nodes"):
                            maker.product_error_nodes(self.folder, allow_retry=retry)

    def test_pr_recovery_cannot_hide_multiple_infrastructure_failures(self):
        self.report("error", "ConnectionError: connection refused")
        path = self.folder / "junit.xml"
        report = ET.parse(path)
        case = ET.SubElement(report.getroot(), "testcase", classname="tests.test_product", name="test_b")
        ET.SubElement(case, "failure", message="ConnectionError: connection refused")
        report.write(path)
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}):
            retry = maker.retry_run({"replicas": [1]})
            with patch.object(maker, "recovered_nodes", return_value={NODE, "tests/test_product.py::test_b"}):
                with self.assertRaisesRegex(ValueError, "Infrastructure failure in multiple test nodes"):
                    maker.product_error_nodes(self.folder, allow_retry=retry)

    def test_database_lock_is_infrastructure(self):
        self.report("error", "sqlalchemy.exc.OperationalError: database is locked")
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}):
            with self.assertRaisesRegex(ValueError, "Infrastructure"):
                maker.product_error_nodes(self.folder)
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "merge_group"}):
            retry = maker.retry_run({"replicas": [1]})
            classified = maker.product_error_nodes(self.folder, allow_retry=retry)
            self.assertEqual(classified, {NODE})
            runs, _ = known_skips.comparison_runs([{NODE: "error"}, {NODE: "passed"}], set(),
                                                  release=False, allowed_errors=classified)
            self.assertTrue(maker.evaluate(self.current, self.current, runs, product_errors=classified))
            with self.assertRaisesRegex(ValueError, "New errors.*test_a"):
                self.evaluate(["error", "error"], product_errors=classified)
            self.report("error", "error collecting tests/test_product.py")
            with self.assertRaisesRegex(ValueError, "Collection error"):
                maker.product_error_nodes(self.folder, allow_retry=retry)

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

    def test_single_replica_pr_refuses_and_reports_new_red_outcomes(self):
        self.assertTrue(self.evaluate(["passed"], expected_replicas=1))
        for outcome, reason in (("failed", "New failures"), ("error", "New errors")):
            with self.subTest(outcome=outcome):
                with self.assertRaisesRegex(ValueError, reason + ".*" + NODE) as refused:
                    self.evaluate([outcome], expected_replicas=1, product_errors={NODE})
                self.assertNotIn("complete replica", str(refused.exception))
                self.assertEqual(maker.report_outside(self.current, [{NODE: outcome}], self.folder), [])
                report = json.loads((self.folder / "outside-observations.json").read_text())
                self.assertEqual(report["observations"][0]["observations"], [outcome])
                self.assertEqual(report["observations"][0]["red_count"], 1)

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

    @patch.dict(os.environ, {"GITHUB_RUN_ID": "17"})
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
                    self.assertTrue(self.evaluate(["passed", "passed"], stale_output=stale_output, stale_sha=STALE_SHA))
                else:
                    with self.assertRaisesRegex(ValueError, "Stale passing"):
                        self.evaluate(["passed", "passed"], stale_output=stale_output)

        artifact = self.folder / "stale-report.json"
        with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}):
            report = {"run_id": "17", "sha": STALE_SHA, "nodes": [NODE]}
            output = {"stale_output": self.folder, "stale_sha": STALE_SHA}
            self.assertTrue(self.evaluate(["passed", "passed"], **output))
            self.assertEqual(json.loads(artifact.read_text()), report)
            self.assertIn("- " + NODE, summary.read_text())
            self.assertTrue(self.evaluate(["passed"], group=True, expected_replicas=1, **output))
            self.assertEqual(json.loads(artifact.read_text()), report)
            self.assertTrue(self.evaluate(["passed"], group=True, expected_replicas=1, recovered={NODE: "error"}, **output))
            self.assertEqual(json.loads(artifact.read_text()), {**report, "nodes": []})
            self.assertTrue(self.evaluate(["passed", "error"], **output))
            self.assertEqual(json.loads(artifact.read_text()), {**report, "nodes": []})
            down = "tests/test_product.py::test_unlisted"
            runs = [{NODE: "passed", down: "failed"}] * 2
            with self.assertRaisesRegex(ValueError, "New failures.*test_unlisted"):
                maker.evaluate(self.current, self.current, runs, **output)
            self.assertEqual(json.loads(artifact.read_text()), report)

    def test_plan_and_reader_use_event_replica_counts(self):
        import trusted_ci
        owner = "tests/test_quick_owner.py"
        owned = "apps/api/quick_fixture.py"
        shared = "apps/api/models.py"
        shared_owner = "tests/test_shared_owner.py"
        indirect_owner = "tests/test_indirect_owner.py"
        fixture = "tests/fixtures/test_sample.py"
        nested_conftest = "tests/fixtures/conftest.py"
        unknown = "apps/api/unknown_fixture.py"
        unknown_doc = "docs/unowned.md"
        unrelated = {"tests/test_other.py", "tests/test_browser_other.py", indirect_owner}
        guarded_tests = {fixture, "tests/test_product_helper.py", "tests/test_runner_hook.py",
                         "tests/test_existing_hook.py", "tests/test_legacy_encoding.py"}
        files = trusted_ci.PR_SMOKE | {owner, shared_owner} | unrelated | guarded_tests
        durations = {"suites": {suite: {"files": dict.fromkeys(files, 1)} for suite in ("unit", "browser")}}
        sources = dict.fromkeys(files, "def test_a(): pass\n")
        sources.update({owner: "from apps.api.quick_fixture import value\n", owned: "value = 1\n",
                        shared: "value = 1\n", shared_owner: "from apps.api.models import value\n",
                        "apps/api/consumer.py": "from apps.api.models import value\n",
                        indirect_owner: "from apps.api.consumer import value\n",
                        unknown: "value = 2\n", unknown_doc: "# Example\n",
                        "scripts/affected_tests.py": (ROOT / "scripts/affected_tests.py").read_text(encoding="utf-8")})
        for name, content in sources.items():
            target = self.folder / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        candidate, base, head = "a" * 40, "b" * 40, "c" * 40
        def source_git(*args):
            return "\n".join(sorted(files)) if args[0] == "ls-files" else candidate
        with patch.object(sys, "path", list(sys.path)), chdir(self.folder):
            added = "tests/test_new_product_case.py"
            with patch.object(trusted_ci, "TRUSTED", self.folder):
                self.assertEqual(trusted_ci.affected_files({shared, added}, files | {added}),
                                 trusted_ci.PR_SMOKE | {shared_owner, added})
            full_paths = ("scripts/tool.py", ".github/workflows/ci.yml", "ci/settings.json", "tools/control.py",
                          "requirements/test.lock", "package-lock.json", "package.json", "pyproject.toml",
                          "tests/conftest.py", nested_conftest, fixture, "apps/api/conftest.py",
                          "tests/fixtures/sample.json", "test_support/fixture.py",
                          "tests/test_product_helper.py", "tests/test_runner_hook.py", "tests/test_existing_hook.py",
                          "tests/test_legacy_encoding.py",
                          "migrations/env.py", "packages/contracts/schemas.py", "apps/web/src/generated.ts",
                          unknown, unknown_doc)
            cases = [(path, [], "dev", False, False) for path in full_paths]
            cases += [(owned, [], "dev", False, True), (shared, [], "dev", False, True), (owner, [], "dev", False, True),
                      (owned, ["full-ci"], "dev", False, False), (owned, [], "main", False, False),
                      (owned, [], "dev", True, True), (unknown, [], "dev", True, False), (owner, [], "dev", True, False)]
            events = [("pull_request", *case) for case in cases]
            events += [(event, owned, [], "dev", False, False)
                       for event in ("merge_group", "workflow_dispatch", "push", "schedule")]
            events.append(("pull_request", owned, [], "dev", False, False))
            events = [case for case in events if case[0] != "merge_group"]
            for index, (event, path, labels, branch, deleted, partial) in enumerate(events):
                if index == len(events) - 1:
                    # The real selector raises when reading this inventory entry as a file.
                    (self.folder / "tests/test_unreadable.py").mkdir()
                payload = {"pull_request": {"head": {"sha": head}, "base": {"sha": base, "ref": branch},
                                            "labels": [{"name": label} for label in labels]}}
                def event_git(*args):
                    if args[0] == "rev-list":
                        return f"{candidate} {base} {head}".encode()
                    self.assertEqual(args[-3:], (base, candidate, "--"))
                    changed = [path, owner] if path == shared else [path]
                    return ("\0".join(changed) + "\0").encode()
                def file_at(revision, name):
                    if deleted and revision == candidate:
                        return None
                    if name == "package.json":
                        return b'{"dependencies":{"example":"1"}}' if revision == base else b'{"dependencies":{"example":"2"}}'
                    if name == "tests/test_runner_hook.py" and revision == candidate:
                        return b"def pytest_configure(config): pass\n"
                    if name == "tests/test_existing_hook.py":
                        return b"def pytest_configure(config): pass\n" + (
                            b"x = 2\n" if revision == candidate else b"x = 1\n")
                    if name == "tests/test_legacy_encoding.py":
                        return b"# coding: latin-1\nvalue = '\xe9'\n"
                    return b"x = 1\n"
                with self.subTest(event=event, path=path, labels=labels, branch=branch, deleted=deleted, case=index), \
                     patch.dict(os.environ, {"GITHUB_EVENT_NAME": event, "GITHUB_SHA": candidate, "TRUSTED_BOOTSTRAP": "false"}), \
                     patch.object(maker, "at_revision", return_value=self.current), \
                     patch.object(trusted_ci, "TRUSTED", self.folder), \
                     patch.object(trusted_ci, "settings", return_value={"unit_shards": 3, "browser_shards": 2}), \
                     patch.object(trusted_ci.hosted, "git", side_effect=source_git), \
                     patch.object(trusted_ci.subprocess, "check_output", return_value=head), \
                     patch.object(trusted_ci.policy, "git", side_effect=event_git), \
                     patch.object(trusted_ci.policy, "event", return_value=payload), \
                     patch.object(trusted_ci.policy, "read_file", side_effect=file_at), \
                     patch.object(trusted_ci.policy, "control_inventory", return_value={
                         "helpers": {"tools/control.py"}, "product_imports": {shared, "tests/test_product_helper.py"}}), \
                     patch.object(trusted_ci.hosted, "load_durations", return_value=durations), \
                     patch.object(trusted_ci.canaries, "sha", return_value="b" * 64):
                    replicas = [1] if event == "pull_request" else [1, 2]
                    self.assertEqual(maker.context(base)["replicas"], replicas)
                    plan = trusted_ci.plan(base, False)
                    self.assertEqual(plan["sha"], candidate)
                    self.assertEqual(plan["replicas"], replicas)
                    actual = {file for row in plan["suites"].values() for shard in row["shards"] for file in shard["files"]}
                    if partial:
                        self.assertEqual(plan["selection"], {"reason": "affected"})
                        expected = {shared_owner, owner} if path == shared else {owner}
                        self.assertEqual(actual, trusted_ci.PR_SMOKE | expected)
                        self.assertEqual(trusted_ci.census_files(plan, files), actual)
                    else:
                        self.assertNotIn("selection", plan)
                        self.assertEqual(actual, files)

    def test_nightly_uses_the_same_pinned_dev_source_for_checks_and_verdict(self):
        import trusted_ci
        import trusted_verdict
        import yaml

        config = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(config["on"]["schedule"], [{"cron": "0 2 * * *"}])
        for producer in ("static", "result"):
            self.assertIn("${{ needs.checks.outputs.trusted_sha }}",
                          [step.get("with", {}).get("ref") for step in config["jobs"][producer]["steps"]])
        event = self.folder / "schedule.json"
        event.write_text("{}")
        pin, default_sha = "a" * 40, "b" * 40
        nightly = {"GITHUB_EVENT_NAME": "schedule", "GITHUB_EVENT_PATH": str(event),
                   "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": default_sha,
                   "GITHUB_OUTPUT": str(self.folder / "output")}
        with patch.dict(os.environ, nightly), \
             patch.object(trusted_ci.subprocess, "check_output", return_value=pin + "\n"), \
             patch.object(trusted_ci, "output") as selected:
            trusted_ci.select()
            self.assertEqual(selected.call_args.args[0]["candidate_sha"], pin)
        with patch.dict(os.environ, nightly), \
             patch.object(trusted_ci.policy, "event", return_value={}), \
             patch.object(trusted_ci.policy, "git", return_value=pin.encode()):
            trusted_ci.policy.ci_context()
            self.assertIn("base_sha=" + pin, Path(nightly["GITHUB_OUTPUT"]).read_text())
            self.assertIn("release=false", Path(nightly["GITHUB_OUTPUT"]).read_text())
        with patch.object(trusted_verdict, "git", return_value=pin.encode()):
            self.assertEqual(trusted_verdict.policy_context({}, "schedule", pin), ("dev", pin))
            with self.assertRaisesRegex(ValueError, "pinned dev"):
                trusted_verdict.policy_context({}, "schedule", default_sha)

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
