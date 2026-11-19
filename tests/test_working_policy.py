"""Synthetic policy regressions, reusable as tests/test_working_policy.py in PR #1."""
import contextlib
import copy
from fnmatch import fnmatchcase
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess
import types
import unittest
from unittest.mock import patch

HELPERS = Path(__file__).resolve().parents[1] / ".github/scripts"
if not HELPERS.is_dir():
    HELPERS = Path(__file__).resolve().parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(HELPERS))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ci"))
import working_policy as policy
import trusted_verdict as verifier
import node_census
import known_skips
import canary_evidence as canaries
import re
import xml.etree.ElementTree as ET


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.inventory = {"helpers": {"scripts/build.py"}, "product_imports": {"packages/example.py"}}
        self.before = {"schema": 2, "tree": "a" * 40, "root_evidence_sha256": "b" * 64,
                       "root_runs": [{"run_id": "1", "run_attempt": "1", "replica": 1},
                                     {"run_id": "1", "run_attempt": "1", "replica": 2}],
                       "cleanup_cap": 1, "fails": [{"node": "a", "class": "baseline"},
                       {"node": "b", "class": "cleanup", "issue": 1}],
                       "unstable": [{"node": "c", "class": "unstable", "issue": 2, "expires_on": "2099-01-01"}]}

    def removal(self, after):
        return policy.removal_only(json.dumps(self.before).encode(), json.dumps(after).encode())

    def test_removal_only_both_schemas(self):
        after = copy.deepcopy(self.before)
        after["fails"].pop()
        self.assertTrue(self.removal(after))
        after["unstable"] = []
        self.assertTrue(self.removal(after))
        old = {"schema": 1, "tree": "a" * 40, "fails": ["a", "b"], "unstable": []}
        self.assertTrue(policy.removal_only(json.dumps(old).encode(), json.dumps({**old, "fails": ["a"]}).encode()))

    def test_add_alter_move_reorder_metadata_or_delete_refused(self):
        variants = []
        for key, value in [("tree", "c" * 40), ("cleanup_cap", 0), ("cleanup_cap", True), ("schema", True)]:
            after = copy.deepcopy(self.before)
            after["fails"].pop()
            after[key] = value
            variants.append(after)
        for change in ("add", "issue", "class", "expiry", "reorder"):
            after = copy.deepcopy(self.before)
            if change == "add": after["fails"].append({"node": "new", "class": "baseline"})
            if change == "issue": after["fails"][1]["issue"] = 3
            if change == "class": after["fails"][1]["class"] = "baseline"
            if change == "expiry": after["unstable"][0]["expires_on"] = "2099-01-02"
            if change == "reorder": after["fails"].reverse()
            variants.append(after)
        for after in variants:
            with self.subTest(after=after): self.assertFalse(self.removal(after))
        self.assertFalse(self.removal(self.before))
        self.assertFalse(policy.removal_only(None, b"{}"))
        self.assertFalse(policy.removal_only(b"{}", None))
        with self.assertRaises(ValueError): policy.removal_only(b"{", b"{}")
        with self.assertRaises(ValueError): policy.decode(b'{"schema":2,"schema":1}')

    def test_auditor_quarantine_hook_attack_is_tier1(self):
        attack = b'''import pytest
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.failed and "test_new_regression" in item.nodeid:
        report.outcome = "passed"
'''
        self.assertEqual(policy.tier("tests/quarantine.py", b"import pytest\n", attack, self.inventory)[0], 1)
        self.assertEqual(policy.tier("packages/unusual.py", b"x=1\n", attack, self.inventory)[0], 1)

    def test_all_named_hooks_are_content_protected(self):
        for name in policy.HOOKS:
            with self.subTest(hook=name):
                raw = ("def " + name + "(*args, **kwargs):\n    pass\n").encode()
                self.assertEqual(policy.tier("packages/unrelated.py", b"x=1\n", raw, self.inventory)[0], 1)
        named = b'@pytest.hookimpl(specname="pytest_runtest_makereport")\ndef innocent(*args):\n    return None\n'
        self.assertEqual(policy.tier("apps/renamed.py", None, named, self.inventory)[0], 1)
        alias = b'@decorate(specname="pytest_runtest_makereport")\ndef innocent(*args):\n    return None\n'
        self.assertEqual(policy.tier("apps/renamed.py", None, alias, self.inventory)[0], 1)
        for code in (b'pytest_runtest_makereport = lambda *a: None\n',
                     b'monkeypatch.setattr(module,"pytest_runtest_call",replacement)\n'):
            self.assertEqual(policy.tier("apps/other.py", None, code, self.inventory)[0], 1)

    def test_plugins_conftest_and_registration(self):
        for name in ("arbitrary/conftest.py", "arbitrary/new_plugin.py"):
            self.assertEqual(policy.tier(name, None, b"x=1\n", self.inventory)[0], 1)
        for code in (b'pytest_plugins=["tests.other"]\n', b'config.pluginmanager.register(Thing())\n',
                     b'globals()["pytest_plugins"]=["tests.other"]\n'):
            self.assertEqual(policy.tier("tests/conftest.py", b"x=1\n", code, self.inventory)[0], 1)
        self.assertEqual(policy.tier("tests/test_doc.py", b"x=1\n", b'x="pytest_plugins"\n', self.inventory)[0], 2)

    def test_config_sections_and_scripts(self):
        for name in ("pytest.ini", "nested/setup.cfg", "tox.ini", "apps/web/tsconfig.json", "alembic.ini", "requirements/ci-runtimes.json"):
            self.assertEqual(policy.tier(name, b"", b"", self.inventory)[0], 1)
        self.assertEqual(policy.tier("pyproject.toml", b'[tool.pytest.ini_options]\naddopts="-q"\n',
                                     b'[tool.pytest.ini_options]\naddopts="--ignore=tests"\n', self.inventory)[0], 1)
        self.assertEqual(policy.tier("package.json", b'{"scripts":{"test":"pytest"}}',
                                     b'{"scripts":{"test":"true"}}', self.inventory)[0], 1)

    def test_tier2_lock_dependency_and_imported_product(self):
        for name, old, new in [("requirements/test.lock", b"old", b"new"), ("package-lock.json", b"{}", b"{}"),
                              ("packages/example.py", b"x=1\n", b"x=2\n"),
                              ("package.json", b'{"scripts":{},"dependencies":{}}', b'{"scripts":{},"dependencies":{"x":"1"}}'),
                              ("pyproject.toml", b'[project]\nversion="1"\n', b'[project]\nversion="2"\n')]:
            with self.subTest(path=name):
                self.assertEqual(policy.tier(name, old, new, self.inventory), (2, "changes what the checks run on"))

    def test_mechanical_helpers_and_transitive_product_imports(self):
        files = {".github/workflows/ci.yml": b'run: python scripts/check.py\n',
                 "scripts/check.py": b'from scripts.helper import run\nfrom packages.example import value\n',
                 "scripts/helper.py": b'def run(): pass\n', "packages/example.py": b'value=1\n'}
        with patch.object(policy, "git", return_value=b"\0".join(k.encode() for k in files) + b"\0"), \
             patch.object(policy, "read_file", side_effect=lambda rev, name: files.get(name)):
            result = policy.control_inventory("a" * 40)
        self.assertEqual(result["direct_helpers"], {"scripts/check.py"})
        self.assertEqual(result["helpers"], {"scripts/check.py", "scripts/helper.py"})
        self.assertEqual(result["product_imports"], {"packages/example.py"})

    def test_executable_helpers_are_regular_but_lists_are_not_executable(self):
        def metadata(*args):
            if args[0] == "ls-tree": return b"100755 blob " + b"a" * 40 + b"\t" + args[-1].encode() + b"\0"
            return b"print('safe metadata read')\n"
        with patch.object(policy, "git", side_effect=metadata):
            self.assertIn(b"metadata", policy.read_file("a" * 40, "scripts/build.py"))
            with self.assertRaises(ValueError): policy.read_file("a" * 40, policy.KNOWN)

    def test_nonstandard_named_helper_and_nonliteral_helper(self):
        files = {".github/workflows/ci.yml": b'run: python tools/custom.py\n', "tools/custom.py": b'x=1\n'}
        with patch.object(policy, "git", return_value=b"\0".join(k.encode() for k in files) + b"\0"), \
             patch.object(policy, "read_file", side_effect=lambda rev, name: files.get(name)):
            self.assertIn("tools/custom.py", policy.control_inventory("a" * 40)["helpers"])
            files[".github/workflows/ci.yml"] = b'run: python "$CUSTOM_HELPER"\n'
            with self.assertRaises(ValueError): policy.control_inventory("a" * 40)

    def test_migration_floor_uses_its_target(self):
        before = {"migrations/versions/0100_example.py": "blob-a"}
        accepted_dev = {**before, "migrations/versions/0101_next.py": "blob-b", "migrations/versions/0147_last.py": "blob-c"}
        policy.migration_floor(before, accepted_dev)
        for after in ({}, {**before, "migrations/versions/0099_older.py": "x"},
                      {**before, "migrations/versions/0100_example.py": "modified"}):
            with self.assertRaises(ValueError): policy.migration_floor(before, after)

    def test_release_uses_tree_equality_and_marker(self):
        expected_tree, dev = "c" * 40, "d" * 40
        with patch.object(policy, "git", return_value=(dev + ":" + expected_tree + "\n").encode()), \
             patch.object(policy, "read_file", return_value=json.dumps(policy.CONTRACT).encode()):
            self.assertEqual(policy.accepted_dev_commit(expected_tree, dev), dev)
            with self.assertRaises(ValueError): policy.accepted_dev_commit("e" * 40, dev)
        with patch.object(policy, "git", return_value=(dev + ":" + expected_tree + "\n").encode()), \
             patch.object(policy, "read_file", return_value=None):
            with self.assertRaises(ValueError): policy.accepted_dev_commit(expected_tree, dev)

    def run_guard(self, changed, branch="dev", after_known=None, merge_tree=None, preview_history=False,
                  kind="pull_request", after_removed=None, after_python=None, entry=None, preview_probe=None):
        base, head, candidate, source = (c * 40 for c in "abcd")
        data = {"number": 1, "pull_request": {"base": {"ref": branch, "sha": base, "repo": {"id": policy.REPOSITORY}},
                 "head": {"ref": "feature", "sha": head, "repo": {"id": policy.REPOSITORY}}, "merge_commit_sha": candidate}}
        data["pull_request"]["merge_commit_sha"] = None
        data["merge_group"] = {"base_ref": "refs/heads/" + branch, "base_sha": base, "head_sha": head}
        report = {}
        preview_ref = "refs/heads/preview"
        fetch = policy.fetch
        def fake_fetch(ref, expected=None, **options):
            if ref == preview_ref and preview_probe is not None:
                return fetch(ref, expected, **options)
            return expected or source
        def fake_git(*args):
            if args == ("fetch", "--no-tags", "origin", preview_ref):
                raise subprocess.CalledProcessError(128, args)
            if args == ("ls-remote", "--exit-code", "--refs", "origin", preview_ref):
                if preview_probe:
                    raise subprocess.CalledProcessError(preview_probe, args)
                return (source + "\t" + preview_ref + "\n").encode()
            if args[0]=='ls-tree':return b''
            if args[0] == "rev-parse": return source.encode()
            if args[0] == "diff": return changed.encode() + b"\0"
            if args[0] == "rev-list": return b"preview-merge\n" if preview_history else b""
            raise AssertionError(args)
        def record(branch, base, head, candidate, rows, source, candidate_sha=None):
            report.update({"branch": branch, "target_sha": base, "candidate_tree": candidate,
                           "tier1": [r for r in rows if r["tier"] == 1], "tier2": [r for r in rows if r["tier"] == 2]})
            return report
        def read(rev, name):
            if name=='ci/removed-tests.json':
                return json.dumps(after_removed if rev==candidate and after_removed is not None else {'schema':2,'removed':[],'removed_nodes':[]}).encode()
            if name == policy.KNOWN:
                return json.dumps(after_known if rev == candidate and after_known is not None else self.before).encode()
            if name.endswith(".py"):
                return after_python if rev == candidate and after_python is not None else b"x=1\n"
            return b"{}"
        with contextlib.ExitStack() as stack:
            for name, value in {"event": lambda: data, "git": fake_git, "fetch": fake_fetch,
                                "merge_tree": lambda *a: candidate,
                                "tree": lambda rev: ((merge_tree or candidate) if rev == head else candidate),
                                "accepted_dev_commit": lambda candidate_tree, dev_tip: source,
                                "control_inventory": lambda revision: self.inventory,
                                "migrations": lambda revision: {}, "read_file": read, "findings_report": record}.items():
                stack.enter_context(patch.object(policy, name, value))
            stack.enter_context(patch.dict(policy.os.environ, {"GITHUB_EVENT_NAME": kind, "TRUSTED_BOOTSTRAP": "false"}))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            (entry or policy.guard)()
        return report

    def checks_guard(self, summary):
        import yaml
        workflows = HELPERS if (HELPERS / "ci.yml").is_file() else HELPERS.parent / "workflows"
        jobs = yaml.load((workflows / "ci.yml").read_bytes(), Loader=yaml.BaseLoader)["jobs"]
        steps = jobs["checks"]["steps"]
        guard = next(step for step in steps if step.get("name") == "Enforce working policy before test fan-out")
        self.assertEqual(guard["working-directory"], ".trusted-controls")
        self.assertEqual(guard["if"], "github.event_name == 'pull_request' || github.event_name == 'merge_group'")
        self.assertEqual(guard["env"], {"POLICY_WORKFLOW_SHA": "${{ github.workflow_sha }}"})
        for i, step in enumerate(steps):
            if step.get("id") == "plan" or step.get("uses", "").startswith("actions/upload-artifact@"):
                self.assertLess(steps.index(guard), i)
        for job in ("unit", "browser", "static"):
            self.assertEqual(jobs[job]["needs"], "checks")
            self.assertIn("needs.checks.result == 'success'", jobs[job]["if"])
        standalone = yaml.load((workflows / "working-policy.yml").read_bytes(), Loader=yaml.BaseLoader)
        standalone_steps = standalone["jobs"]["policy"]["steps"]
        self.assertIn("python -I .github/scripts/working_policy.py guard", [step.get("run") for step in standalone_steps])
        source = guard["run"].split("\n", 1)[1].rsplit("\nPY", 1)[0]

        def entry():
            with patch.object(sys, "path", list(sys.path)), patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}):
                exec(compile(source, "Checks policy step", "exec"), {})

        return entry

    def test_checks_stops_on_other_policy_refusals_before_fan_out(self):
        addition = copy.deepcopy(self.before)
        addition["fails"].append({**addition["fails"][0], "node": "tests/test_new.py::test_case"})
        with tempfile.TemporaryDirectory() as folder:
            summary = Path(folder) / "summary.md"
            entry = self.checks_guard(summary)
            cases = [
                (policy.KNOWN, {"after_known": addition}, "Known-failures changes"),
                ("AGENTS.md", {}, "private agent instructions"),
                ("apps/example.py", {"after_python": b"x = ("}, "syntax error"),
                (".github/workflows/ci.yml", {"preview_history": True}, "Preview merge history"),
            ]
            for path, options, message in cases:
                with self.subTest(path=path), self.assertRaisesRegex(SystemExit, message):
                    self.run_guard(path, entry=entry, **options)
            self.assertFalse(summary.exists())

    def test_checks_continues_for_audited_route_and_standalone_still_refuses(self):
        refusal = "Tier 1 control changes require the audited maintenance route"
        with tempfile.TemporaryDirectory() as folder:
            summary = Path(folder) / "summary.md"
            entry = self.checks_guard(summary)
            clean = self.run_guard("apps/example.py", entry=entry)
            self.assertEqual(clean["tier1"], [])
            self.assertFalse(summary.exists())
            for kind in ("pull_request", "merge_group"):
                with self.subTest(kind=kind):
                    with self.assertRaisesRegex(ValueError, refusal):
                        self.run_guard(".github/workflows/ci.yml", kind=kind)
                    report = self.run_guard(".github/workflows/ci.yml", kind=kind, entry=entry)
                    self.assertEqual([row["path"] for row in report["tier1"]], [".github/workflows/ci.yml"])
            self.assertEqual(summary.read_text(encoding="utf-8").count(refusal), 2)

    def test_review_required_patterns_cover_tracked_or_reserved_paths(self):
        root = HELPERS.parents[1]
        paths = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"]).decode("utf-8").split("\0")[:-1]
        rules = (root / "ci/review_required_paths.txt").read_text(encoding="utf-8")
        for line in rules.splitlines():
            pattern, _, note = line.partition("#")
            if pattern := pattern.strip():
                with self.subTest(pattern=pattern):
                    self.assertEqual(any(fnmatchcase(path, pattern) for path in paths),
                                     note.strip() != "must never exist")
        for path in ("apps/api/auth.py", "apps/api/commands.py", "apps/api/services/access.py", "apps/api/data_crypto.py",
                     "packages/domain/rule_lines.py", "migrations/versions/new.py", ".github/workflows/ci.yml",
                     "ci/review_required_paths.txt", "infra/Dockerfile", "DESIGN.md"):
            with self.subTest(path=path):
                self.assertTrue(policy.review_required(["tests/test_example.py", path]))
        self.assertFalse(policy.review_required(["tests/test_example.py", "apps/web/src/branding.ts"]))
        self.assertFalse(policy.review_required([]))

    def test_guard_refuses_previous_event(self):
        with self.assertRaisesRegex(ValueError,'Unsupported policy event'):
            self.run_guard('apps/example.py',kind='pull_request_target')

    def test_guard_tier1_attack_refused_tier2_flagged(self):
        with self.assertRaises(ValueError): self.run_guard("tests/quarantine.py")
        report = self.run_guard("requirements/test.lock")
        self.assertEqual(report["tier2"][0]["path"], "requirements/test.lock")
        self.assertEqual(report["tier1"], [])

    def test_guard_removal_repair_and_promotion(self):
        after = copy.deepcopy(self.before)
        after["fails"].pop()
        self.assertEqual(self.run_guard(policy.KNOWN, after_known=after)["tier1"], [])
        self.run_guard(".github/workflows/ci.yml", branch="main")
        with self.assertRaises(ValueError): self.run_guard("apps/example.py", branch="main", merge_tree="u" * 40)
        with self.assertRaises(ValueError): self.run_guard("apps/example.py", preview_history=True)

    def test_guard_allows_an_absent_preview_ref_but_propagates_fetch_failures(self):
        for kind in ("pull_request", "merge_group"):
            with self.subTest(kind=kind):
                self.assertEqual(self.run_guard("apps/example.py", kind=kind, preview_probe=2)["tier1"], [])
                for code in (0, 128):
                    with self.assertRaises(subprocess.CalledProcessError):
                        self.run_guard("apps/example.py", kind=kind, preview_probe=code)

    def test_same_pr_node_ledger_structural_addition_is_flagged_tier2(self):
        after={'schema':2,'removed':[],'removed_nodes':[{'node':'tests/test_a.py::test_old','owner':'test-owner','reason':'Reviewed redundant assertion'}]}
        report=self.run_guard('ci/removed-tests.json',after_removed=after)
        self.assertEqual(report['tier1'],[])
        self.assertEqual(report['tier2'][0]['path'],'ci/removed-tests.json')
        after['removed']=[{'file':'extra'}]
        with self.assertRaisesRegex(ValueError,'Tier 1'):self.run_guard('ci/removed-tests.json',after_removed=after)

    def test_trusted_verifier_rejects_skipped_missing_producers(self):
        good = {name: {"result": "success"} for name in ("checks", "static", "unit", "browser")}
        verifier.required_producers(json.dumps(good))
        for producer in good:
            for status in ("skipped", "cancelled", "failure", "unknown"):
                bad = copy.deepcopy(good)
                bad[producer]["result"] = status
                with self.assertRaises(ValueError): verifier.required_producers(json.dumps(bad))
        del good["checks"]
        with self.assertRaises(ValueError): verifier.required_producers(json.dumps(good))

    def test_trusted_candidate_inventory_cannot_shrink(self):
        plan = {"smoke": False, "suites": {"unit": {"shards": [{"files": ["tests/test_a.py"]}]},
                                           "browser": {"shards": [{"files": ["tests/test_browser_a.py"]}]}}}
        rows = b"100644 blob " + b"a" * 40 + b"\ttests/test_a.py\0" + b"100644 blob " + b"b" * 40 + b"\ttests/test_browser_a.py\0"
        verifier.assert_inventory(plan, rows, {"unit": 1, "browser": 1})
        omitted = copy.deepcopy(plan)
        omitted["suites"]["unit"]["shards"][0]["files"] = []
        with self.assertRaises(ValueError): verifier.assert_inventory(omitted, rows, {"unit": 1, "browser": 1})
        with self.assertRaises(ValueError): verifier.assert_inventory(plan, rows.replace(b"100644", b"120000", 1), {"unit": 1, "browser": 1})
        partial = {**plan, "replicas": [1], "selection": {"reason": "affected"}}
        rows += b"100644 blob " + b"c" * 40 + b"\ttests/test_unobserved.py\0"
        with patch.object(verifier, "PR_SMOKE", {"tests/test_browser_a.py"}):
            self.assertEqual(verifier.assert_inventory(partial, rows, {"unit": 2, "browser": 2}, {"tests/test_a.py"}),
                             {"tests/test_a.py", "tests/test_browser_a.py"})
            with self.assertRaisesRegex(ValueError, "ordinary dev pull request"):
                verifier.assert_inventory(partial, rows, {"unit": 2, "browser": 2})
            with self.assertRaisesRegex(ValueError, "omits required test files"):
                verifier.assert_inventory(partial, rows, {"unit": 2, "browser": 2}, {"tests/test_unobserved.py"})
            with self.assertRaisesRegex(ValueError, "Partial selection disagrees"):
                verifier.assert_inventory({**partial, "selection": {"reason": "selector-error"}}, rows,
                                          {"unit": 2, "browser": 2}, {"tests/test_a.py"})

    def test_computed_conftest_attack_refused(self):
        attack = b'''import sys
def forged(item, call):
    return None
setattr(sys.modules[__name__], "pytest_" + "runtest_makereport", forged)
'''
        self.assertEqual(policy.tier("tests/conftest.py", b"x=1\n", attack, self.inventory)[0], 1)
        self.assertEqual(policy.tier("packages/odd.py", b"x=1\n", attack, self.inventory)[0], 1)

    def test_startup_attacks_refused(self):
        for name in ("sitecustomize.py", "nested/usercustomize.py", "nested/agent.pth"):
            self.assertEqual(policy.tier(name, None, b"x=1\n", self.inventory)[0], 1)

    def test_plugin_mechanisms_and_aliases(self):
        for value in (b"pm = config.pluginmanager\npm.import_plugin('x')\n",
                      b"pm = config.pluginmanager\npm.register(Thing())\n",
                      b"os.environ['PYTEST_PLUGINS']='x'\n", b"os.environ['PYTEST_ADDOPTS']='-p hidden'\n"):
            self.assertEqual(policy.tier("packages/example.py", b"x=1\n", value, self.inventory)[0], 1)
        self.assertEqual(policy.tier("config.env", b"", b"PYTEST_ADDOPTS=-p hidden", self.inventory)[0], 1)

    def test_conftest_reporter_assignment_refused_other_support_flagged(self):
        self.assertEqual(policy.tier("tests/conftest.py", b"x=1\n", b"config.terminalreporter = fake\n", self.inventory)[0], 1)
        for name in ("tests/conftest.py", "tests/helpers.py", "tests/test_example.py"):
            self.assertEqual(policy.tier(name, b"x=1\n", b"x=2\n", self.inventory)[0], 2)

    def test_conftest_only_imports_are_tier2(self):
        four = ("manage", "initial_seed", "golden_scenarios", "system_command")
        files = {".github/workflows/ci.yml": b"run: python scripts/check.py\n", "scripts/check.py": b"x=1\n",
                 "tests/conftest.py": ("\n".join("import scripts." + n for n in four)).encode(),
                 **{"scripts/" + n + ".py": b"x=1\n" for n in four}}
        with patch.object(policy, "git", return_value=b"\0".join(k.encode() for k in files) + b"\0"), \
             patch.object(policy, "read_file", side_effect=lambda rev, name: files.get(name)):
            inventory = policy.control_inventory("a" * 40)
        self.assertTrue(all("scripts/" + n + ".py" in inventory["product_imports"] for n in four))
        self.assertFalse(any("scripts/" + n + ".py" in inventory["helpers"] for n in four))

    def test_merge_tree_ignores_null_or_stale_event_sha(self):
        with patch.object(policy, "git", return_value=("a" * 40 + "\n").encode()) as git:
            self.assertEqual(policy.merge_tree("b" * 40, "c" * 40), "a" * 40)
            git.assert_called_once_with("merge-tree", "--write-tree", "b" * 40, "c" * 40)
        self.run_guard("apps/ordinary.py")

    def test_merge_tree_conflicts_refused(self):
        with patch.object(policy, "git", side_effect=policy.subprocess.CalledProcessError(1, ["git"])):
            with self.assertRaises(policy.subprocess.CalledProcessError): policy.merge_tree("a" * 40, "b" * 40)

    def test_main_group_checks_actual_base_after_retarget(self):
        self.run_guard("apps/ordinary.py", branch="dev", kind="merge_group")
        with patch.object(policy, "accepted_dev_commit", side_effect=ValueError("Not reachable dev tree")):
            # Directly prove main groups call the dev-tree predicate; the group target is the authority.
            data = {"merge_group":{"base_ref":"refs/heads/main", "base_sha":"a"*40,"head_sha":"b"*40}}
            with patch.object(policy, "event", return_value=data), patch.object(policy, "fetch", side_effect=lambda ref, expected=None: expected or "d"*40), \
                 patch.object(policy, "tree", return_value="c"*40), patch.object(policy, "migrations", return_value={}), patch.object(policy,'assert_public_tree'), \
                 patch.dict(os.environ, {"GITHUB_EVENT_NAME":"merge_group", "TRUSTED_BOOTSTRAP":"false"}):
                with self.assertRaisesRegex(ValueError, "Not reachable"): policy.guard()

    def test_detached_forged_dev_commit_is_refused(self):
        dev, accepted_tree, forged_tree = (x * 40 for x in "abc")
        def history(*args):
            self.assertEqual(args, ("log", "--first-parent", "--format=%H:%T", dev))
            return (dev + ":" + accepted_tree + "\n").encode()
        with patch.object(policy, "git", side_effect=history):
            with self.assertRaises(ValueError): policy.accepted_dev_commit(forged_tree, dev)

    def test_known_skips_review_binding_and_new_skip_refusal(self):
        evidence = {"sha":"a"*40,"tree":"b"*40,"runs":[{"run_id":"1"},{"run_id":"2"}]}
        runs = [{"tests/test_a.py::test_a":"skipped", "tests/test_b.py::test_b":"passed"}] * 2
        reviews = {"tests/test_a.py::test_a":{"reason":"Reviewed runner limitation", "review":"sha256:"+"c"*64}}
        result = known_skips.build_list(runs, evidence, reviews, re.compile(r"tests/.+::test_.+").fullmatch)
        self.assertEqual(result["skips"][0]["observed_in"], [1,2])
        compared, observed = known_skips.comparison_runs(runs, set(reviews), release=True)
        self.assertEqual(observed, set(reviews))
        self.assertNotIn("tests/test_a.py::test_a", compared[0])
        with self.assertRaises(ValueError): known_skips.comparison_runs(runs, set(), release=False)
        with self.assertRaises(ValueError): known_skips.build_list(runs, evidence, {}, lambda n: True)
        bad = copy.deepcopy(result); bad["root_tree"] = "d"*40
        with self.assertRaises(ValueError): known_skips.decode_list(json.dumps(bad), evidence, lambda n: True)

    def test_main_refuses_fail_error_but_dev_retains_failure_ratchet(self):
        self.assertEqual(known_skips.comparison_runs([{"n":"failed"}], set(), release=False)[0], [{"n":"failed"}])
        for outcome in ("failed", "error"):
            with self.assertRaises(ValueError): known_skips.comparison_runs([{"n":outcome}], set(), release=True)

    def test_canaries_exact_outcomes_and_node_set(self):
        manifest = {kind:"test_random_" + kind for kind in ("passed","failed","skipped","error")}
        def case(name, outcome):
            row = ET.Element("testcase", {"name":name})
            if outcome != "passed": ET.SubElement(row, {"failed":"failure", "error":"error", "skipped":"skipped"}[outcome])
            return row
        cases = [case(token, kind) for kind, token in manifest.items()] + [case("ordinary", "passed")]
        collection = [*manifest.values(), "ordinary"]
        self.assertEqual(canaries.verify_cases(cases, manifest, collection), ["ordinary"])
        with self.assertRaises(ValueError): canaries.verify_cases(cases[:-1], manifest, collection)
        with self.assertRaises(ValueError): canaries.verify_cases(cases[1:], manifest, collection)
        forged = copy.deepcopy(cases); forged[1].remove(forged[1][0])
        with self.assertRaises(ValueError): canaries.verify_cases(forged, manifest, collection)
        with self.assertRaises(ValueError): canaries.verify_cases(cases, manifest, collection + ["hidden"])

    def test_trusted_verdict_materializes_only_target_json_and_candidate_subtraction(self):
        self.exercise_verdict_materialization("dev")

    def test_trusted_verdict_rejects_a_plan_with_the_wrong_replica_count(self):
        with self.assertRaisesRegex(ValueError, "Replica count differs from the trusted reader context"):
            self.exercise_verdict_materialization("dev", replicas=(1, 2))

    def test_old_base_pr_evaluates_merge_with_29_inherited_allowances_removed(self):
        self.exercise_verdict_materialization("dev", old_base_pr=True)

    def test_first_main_release_reads_accepted_dev_lists_when_main_has_none(self):
        self.exercise_verdict_materialization("main")

    def test_isolated_guard_imports_siblings_from_another_directory(self):
        with tempfile.TemporaryDirectory(dir=os.environ['RUNNER_TEMP']) as folder:
            event=Path(folder)/'event.json';event.write_text('{}')
            result=subprocess.run([sys.executable,'-I',str(HELPERS/'working_policy.py'),'guard'],cwd=folder,
                env={**os.environ,'GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_EVENT_PATH':str(event)},capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,1)
            self.assertIn('Working policy refused:',result.stderr)
            self.assertNotIn('ModuleNotFoundError',result.stderr)

    def test_fixed_test_with_removed_entry_reads_candidate_and_passes(self):
        self.exercise_verdict_materialization('dev',real_reader=True)

    def test_removed_entry_with_still_failing_test_is_refused(self):
        with self.assertRaisesRegex(ValueError,'New failures outside'):
            self.exercise_verdict_materialization('dev',real_reader=True,still_fails=True)

    def exercise_verdict_materialization(self, branch, *, real_reader=False, still_fails=False, event="pull_request",
                                         old_base_pr=False, replicas=(1,)):
        base, candidate, trusted, tree = (c * 40 for c in "abcd")
        if event == "workflow_dispatch": base = candidate
        head = "7" * 40
        target = base if branch == "dev" else "9" * 40
        payload = {"before": base, "pull_request": {"base": {"ref": branch, "sha": target}, "head": {"sha": head}}}
        plan = {"sha": candidate, "tree": tree, "base_sha": base, "smoke": False, "replicas": list(replicas),
                "suites": {"unit": {"shards": [{"files": ["tests/test_a.py"]}]},
                           "browser": {"shards": [{"files": ["tests/test_browser_a.py"]}]}}}
        inventory = (b"100644 blob " + b"e" * 40 + b"\ttests/test_a.py\0"
                     + b"100644 blob " + b"f" * 40 + b"\ttests/test_browser_a.py\0")
        after = copy.deepcopy(self.before)
        after["fails"].pop()
        after["schema"]=3
        before = {**copy.deepcopy(self.before),"schema":3}
        if real_reader:
            before={**before,'classifications_sha256':'a'*64,'fails':[{'node':'tests/test_a.py::test_a','class':'baseline','owner':'test-owner','reason':'Measured failure'}],'unstable':[]}
            after={**before,'fails':[]}
        if old_base_pr:
            after = before
            inherited = [{"node": f"tests/test_previous.py::test_previous_{i:02d}", "class": "baseline"} for i in range(29)]
            stale = json.dumps({**before, "fails": before["fails"] + inherited}).encode()
            self.assertFalse(policy.removal_only(json.dumps(before).encode(), stale))
        reads = []
        def object_read(revision, name):
            if revision == "9"*40:
                raise AssertionError("Old main has no lists; it must never be read")
            reads.append((revision, name))
            if name == policy.KNOWN:
                if old_base_pr and revision == head:
                    return stale
                return json.dumps(after if revision == candidate else before).encode()
            if real_reader and name == "ci/frozen-baseline-failures.json": return b"[]\n"
            if name == "ci/root-run-evidence.json": return json.dumps({"sha":"e"*40,"tree":"f"*40,
                "runs":[{"run_id":"1","run_attempt":"1","replica":i} for i in (1,2)],
                "measurements":[{"run_id":"1","run_attempt":"1","replica":i,"sha":"e"*40,"tree":"f"*40,"trusted_sha":"d"*40,"receipts_sha256":"a"*64,"measurement_receipt_sha256":"b"*64} for i in (1,2)]}).encode()
            if name == "ci/known-skips.json": return json.dumps({"schema":1,"root_sha":"e"*40,"root_tree":"f"*40,"root_runs":[{"run_id":"1","run_attempt":"1","replica":i} for i in (1,2)],"skips":[]}).encode()
            if name == 'ci/removed-tests.json':return b'{"schema":2,"removed":[],"removed_nodes":[]}'
            return json.dumps({"origin": revision}).encode()
        def fake_git(*args):
            if args[0] == "ls-tree": return inventory
            if args[0] == "show": return json.dumps(after if args[1] == candidate+":"+policy.KNOWN else before).encode()
            if args[:4] == ("rev-list", "--parents", "-n", "1"):
                return (f"{candidate} {target} {head}" if args[4] == candidate else f"{head} {'6' * 40}").encode()
            if args == ("rev-parse", "HEAD"): return trusted.encode()
            if args[0] == "rev-parse": return tree.encode()
            raise AssertionError(args)
        scratch_root = Path(os.getenv("RUNNER_TEMP", tempfile.gettempdir())).resolve()
        with tempfile.TemporaryDirectory(prefix="p2-v3-verifier-test-", dir=scratch_root) as temporary:
            folder = Path(temporary).resolve()
            trusted_root = folder / "trusted"
            (trusted_root / "ci").mkdir(parents=True)
            config = {"unit_shards": 1, "browser_shards": 1, "unstable_limit": 80, "unstable_issue": 1,
                      "unstable_expires_on": "2099-01-01", "cleanup_issue": 1}
            self.assertTrue(folder.is_relative_to(scratch_root))
            plan_file, reports, output = folder / "plan.json", folder / "reports", folder / "output"
            reports.mkdir()
            plan_file.write_text(json.dumps(plan))
            for suite in plan["suites"]:
                (reports / f"ci-test-1-{suite}-1-1").mkdir(parents=True)
            (reports / "ci-test-1-unit-1-1/conftest.py").write_text("raise RuntimeError('candidate code must never execute')\n")
            maker = types.ModuleType("make_known_failures")
            maker.canonical_node=lambda n:n
            maker.SAFE_NODE = re.compile(r"tests/.+::test_.+")
            maker.collect_reports = lambda *a: [{"tests/test_a.py::test_a":"passed", "tests/test_browser_a.py::test_a":"passed"}]
            maker.product_error_nodes = lambda *a, **kw: set()
            maker.mixed_listed_nodes = lambda *a: set()
            maker.outside_observations = lambda *a: []
            if real_reader:
                import make_known_failures as actual_reader
                def reader_context(actual_base):
                    self.assertEqual(actual_base,base)
                    self.assertEqual(actual_reader.load_frozen_baseline(Path('ci/frozen-baseline-failures.json'))[0],set())
                    current=actual_reader.decode_list(maker.git('show','HEAD:'+policy.KNOWN))
                    actual_reader.evaluate(current,before,[{'tests/test_a.py::test_a':'failed' if still_fails else 'passed'}],
                                           expected_replicas=1)
                    return {'replicas':[1]}
                maker.context=reader_context
            else:
                maker.context = lambda actual_base: {"replicas": [1]} if actual_base == base else self.fail("wrong target")
            summaries = []
            maker.write_summary = lambda: summaries.append(True)
            def compare():
                self.assertEqual(json.loads(Path(policy.KNOWN).read_text()), after)
                self.assertEqual(json.loads(Path("ci/frozen-baseline-failures.json").read_text()), [] if real_reader else {"origin": base})
                self.assertNotIn(str(reports), sys.path)
                self.assertEqual(sys.argv[sys.argv.index("--base-sha") + 1], base)
                output.mkdir()
                (output / "verdict.json").write_text('{"passed":true}')
            maker.main = compare
            argv = ["trusted_verdict.py", "--plan", str(plan_file), "--reports", str(reports), "--output", str(output)]
            event_file = folder / "event.json"
            event_file.write_text(json.dumps(payload))
            env = {"NEEDS_JSON": json.dumps({k: {"result": "success"} for k in ("checks", "static", "unit", "browser")}),
                   "CANDIDATE_SHA": candidate, "GITHUB_SHA": candidate, "GITHUB_EVENT_NAME": event, "GITHUB_EVENT_PATH": str(event_file),
                   "GITHUB_REF": "refs/pull/1/merge" if event == "pull_request" else "refs/heads/" + branch,
                   "TRUSTED_BOOTSTRAP": "false", "RUNNER_TEMP": str(folder),
                   "UNIT_COUNT": "1", "BROWSER_COUNT": "1", "GITHUB_RUN_ID":"1", "GITHUB_RUN_ATTEMPT":"1"}
            with patch.object(node_census,"base_nodes",return_value={"tests/test_a.py::test_a","tests/test_browser_a.py::test_a"}), patch.object(node_census,"removed_nodes",return_value=set()), patch.object(node_census,"emit"), patch.object(verifier, "TRUSTED", trusted_root), patch.object(canaries, "verify_directory"), patch.object(sys, "argv", argv), patch.object(sys, "path", list(sys.path)), patch.dict(os.environ, env), \
                 patch.object(verifier, "settings", return_value=config), \
                 patch.dict(sys.modules, {"make_known_failures": maker}), patch.object(verifier, "git", side_effect=fake_git), \
                 patch.object(policy, "git", fake_git), patch.object(policy, "fetch", side_effect=lambda ref, expected=None: expected or trusted) as fetched, \
                 patch.object(policy, "accepted_dev_commit", return_value=base), \
                 patch.object(policy, "event", return_value=payload), \
                 patch.object(policy, "read_file", side_effect=object_read), contextlib.redirect_stdout(io.StringIO()):
                if old_base_pr:
                    import trusted_ci
                    with patch.object(trusted_ci, "settings", return_value=config), \
                         patch.object(trusted_ci.subprocess, "check_output", return_value=trusted), \
                         patch.object(trusted_ci, "output") as selected:
                        trusted_ci.select()
                    with patch.dict(os.environ, {"CANDIDATE_SHA": selected.call_args.args[0]["candidate_sha"]}):
                        verifier.main()
                    with patch.dict(os.environ, {"CANDIDATE_SHA": head, "GITHUB_SHA": head}):
                        with self.assertRaisesRegex(ValueError, "PR candidate must be the event merge"):
                            verifier.main()
                else:
                    verifier.main()
            self.assertEqual(summaries, [True])
            self.assertTrue(all(revision == base for revision, name in reads if name not in {policy.KNOWN,'ci/removed-tests.json'}))
            self.assertTrue(all(name in policy.LISTS for _, name in reads))
            return fetched.call_args_list



class BootstrapControlTests(unittest.TestCase):
    def test_workflow_fallback_is_only_for_missing_controls(self):
        import shutil
        import yaml
        workflows = HELPERS if (HELPERS / 'ci.yml').is_file() else HELPERS.parent / 'workflows'
        bash = shutil.which('bash')
        if os.name == 'nt':
            bash = 'C:/Program Files/Git/bin/bash.exe'
        count = 0
        for name in ('ci.yml', 'ci-tests.yml', 'working-policy.yml'):
            doc = yaml.load((workflows / name).read_text(), Loader=yaml.BaseLoader)
            self.assertNotIn('pull_request_target', (workflows / name).read_text())
            if name == 'ci.yml':
                checks = doc['jobs']['checks']['steps']
                merge_ref = "${{ github.event_name == 'pull_request' && steps.bootstrap.outputs.bootstrap == 'false' && github.sha || steps.select.outputs.candidate_sha }}"
                for job in ('checks', 'static'):
                    checkout = next(step for step in doc['jobs'][job]['steps'] if step.get('with', {}).get('path') == 'candidate')
                    self.assertEqual(checkout['with']['ref'], merge_ref)
                self.assertEqual(doc['jobs']['checks']['outputs']['candidate_sha'], '${{ steps.candidate.outputs.commit }}')
                context_index = next(i for i, step in enumerate(checks) if step.get('id') == 'context')
                plan, upload = checks[context_index + 1:context_index + 3]
                self.assertEqual(plan['id'], 'plan')
                cache_only_push = "(github.event_name != 'push' || github.ref != 'refs/heads/dev')"
                self.assertEqual(plan['if'], cache_only_push)
                self.assertEqual(upload['if'], cache_only_push)
                self.assertTrue(plan['run'].startswith('python3 -I -S "$GITHUB_WORKSPACE/.trusted-controls/'))
                self.assertEqual(upload['with']['name'], 'ci-plan-${{ github.run_attempt }}')
                for name_after_plan in ('Install the committed runtime pins', 'Install hash-locked dependencies',
                                        'Build web assets and compiled client modules'):
                    self.assertIn(name_after_plan, [step.get('name') for step in checks[context_index + 3:]])
            for job in doc['jobs'].values():
                steps = job.get('steps', [])
                for index, step in enumerate(steps):
                    if step.get('id') != 'bootstrap': continue
                    count += 1
                    fallback = steps[index + 1]
                    self.assertEqual(fallback['if'], "steps.bootstrap.outputs.bootstrap == 'true'")
                    self.assertEqual(fallback['with']['ref'], '${{ github.event.pull_request.head.sha }}')
                    self.assertEqual(fallback['with'].get('path'), steps[index - 1]['with'].get('path'))
                    self.assertEqual(fallback['with']['persist-credentials'], 'false')
                    script = ('.trusted-controls/' if fallback['with'].get('path') else '') + '.github/scripts/'
                    script += 'working_policy.py' if name == 'working-policy.yml' else 'trusted_ci.py'
                    for present in (False, True):
                        with tempfile.TemporaryDirectory(dir=os.environ['RUNNER_TEMP']) as temporary:
                            folder = Path(temporary)
                            if present:
                                target = folder / script; target.parent.mkdir(parents=True); target.write_text('trusted')
                            env = {**os.environ, 'GITHUB_OUTPUT': str(folder / 'output'),
                                   'GITHUB_ENV': str(folder / 'env'), 'EVENT_NAME': 'pull_request'}
                            result = subprocess.run([bash, '-e', '-c', step['run']], cwd=folder, env=env,
                                                    capture_output=True, text=True, timeout=20)
                            self.assertEqual(result.returncode, 0, result.stderr)
                            self.assertEqual((folder / 'output').read_text().strip(), 'bootstrap=' + str(not present).lower())
                            self.assertEqual('BOOTSTRAP:' in result.stdout, not present)
                            self.assertEqual((folder / 'env').exists(), not present)
                    with tempfile.TemporaryDirectory(dir=os.environ['RUNNER_TEMP']) as temporary:
                        folder = Path(temporary)
                        result = subprocess.run([bash, '-e', '-c', step['run']], cwd=folder,
                            env={**os.environ,'GITHUB_OUTPUT':str(folder/'output'),'GITHUB_ENV':str(folder/'env'),
                                 'EVENT_NAME':'merge_group'}, capture_output=True, text=True, timeout=20)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertFalse((folder / 'env').exists())
        self.assertEqual(count, 4)

    def test_bootstrap_binding_refuses_noninitial_events_and_sources(self):
        base, head = 'a' * 40, 'b' * 40
        data = {'pull_request': {'base': {'ref':'dev','repo':{'id':policy.REPOSITORY}},
                                'head': {'sha':head,'repo':{'id':policy.REPOSITORY}}}}
        for change in ('valid', 'unflagged', 'merge_group', 'attempt', 'parents', 'controls', 'known', 'head', 'fork', 'target'):
            event = copy.deepcopy(data)
            if change == 'fork': event['pull_request']['head']['repo']['id'] = 1
            if change == 'target': event['pull_request']['base']['ref'] = 'main'
            def git(*args):
                if args[0] == 'rev-list': return (base + (' ' + head if change == 'parents' else '')).encode()
                return ('c' * 40 if change == 'head' else head).encode()
            def read(revision, path):
                return b'present' if (change == 'controls' and path.endswith('trusted_ci.py') or change == 'known' and path == policy.KNOWN) else None
            with patch.dict(os.environ, {'TRUSTED_BOOTSTRAP':'false' if change == 'unflagged' else 'true',
                                        'GITHUB_RUN_ATTEMPT':'2' if change == 'attempt' else '1'}), \
                 patch.object(policy, 'git', side_effect=git), patch.object(policy, 'read_file', side_effect=read):
                if change in {'valid','unflagged'}:
                    self.assertEqual(policy.bootstrap(event, 'pull_request', base), change == 'valid')
                else:
                    with self.assertRaises(ValueError): policy.bootstrap(event, 'merge_group' if change == 'merge_group' else 'pull_request', base)

    def test_bootstrap_verdict_never_reads_missing_baseline(self):
        maker = types.SimpleNamespace(collect_reports=lambda *a:[{'tests/test_a.py::test_a':'failed'}] * 2)
        with tempfile.TemporaryDirectory(dir=os.environ['RUNNER_TEMP']) as temporary:
            folder = Path(temporary)
            def read(revision, path):
                self.assertEqual(path, policy.KNOWN)
                return None
            with patch.dict(os.environ, {'GITHUB_RUN_ID':'1','GITHUB_RUN_ATTEMPT':'1'}), \
                 patch.object(policy, 'read_file', side_effect=read):
                verifier.bootstrap_verdict({'replicas':[1,2],'tree':'c'*40}, folder, folder/'out', 'b'*40,
                                           'a'*40, 'a'*40, maker, {'canaries':False,'node_census':False})
            self.assertTrue(json.loads((folder/'out/verdict.json').read_text())['passed'])
            self.assertTrue(json.loads((folder/'out/bootstrap-measurement.json').read_text())['bootstrap'])

    def test_bootstrap_verdict_refuses_single_replica_errors_and_skips(self):
        for replicas, outcome in (([1],'passed'),([1,2],'error'),([1,2],'skipped')):
            maker = types.SimpleNamespace(collect_reports=lambda *a:[{'n':outcome}] * len(replicas))
            with patch.dict(os.environ, {'GITHUB_RUN_ID':'1','GITHUB_RUN_ATTEMPT':'1'}), \
                 patch.object(policy, 'read_file', return_value=None):
                with self.assertRaises(ValueError):
                    verifier.bootstrap_verdict({'replicas':replicas}, Path('.'), Path('.'), 'b'*40,
                                               'a'*40, 'a'*40, maker, {'canaries':False,'node_census':False})


from hardening_profiles import apply_profile
apply_profile(globals(),{'test_auditor_quarantine_hook_attack_is_tier1': ['content_classifier'], 'test_tier2_lock_dependency_and_imported_product': ['content_classifier'], 'test_all_named_hooks_are_content_protected': ['content_classifier'], 'test_plugins_conftest_and_registration': ['content_classifier'], 'test_config_sections_and_scripts': ['content_classifier'], 'test_computed_conftest_attack_refused': ['content_classifier'], 'test_plugin_mechanisms_and_aliases': ['content_classifier'], 'test_conftest_reporter_assignment_refused_other_support_flagged': ['content_classifier'], 'test_canaries_exact_outcomes_and_node_set': ['canaries'], 'test_same_pr_node_ledger_structural_addition_is_flagged_tier2': ['node_census']})

if __name__ == "__main__":
    unittest.main()
