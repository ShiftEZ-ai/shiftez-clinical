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
from unittest.mock import Mock, patch
from urllib.error import HTTPError

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

# The refusal only the pre-code-owner guard had; it must not come back.
LEGACY_ROUTE = "Tier 1 control changes require the audited maintenance route"

import pytest


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
        files = {".github/workflows/ci.yml": b'jobs: {check: {steps: [{run: python scripts/check.py}]}}',
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
        workflow = ".github/workflows/ci.yml"
        files = {workflow: b'jobs: {check: {steps: [{run: python tools/custom.py}]}}', "tools/custom.py": b'x=1\n'}
        with patch.object(policy, "git", return_value=b"\0".join(k.encode() for k in files) + b"\0"), \
             patch.object(policy, "read_file", side_effect=lambda rev, name: files.get(name)):
            self.assertIn("tools/custom.py", policy.control_inventory("a" * 40)["helpers"])
            files[workflow] = b'jobs: {check: {steps: [{run: python "$CUSTOM_HELPER"}]}}'
            with self.assertRaises(ValueError): policy.control_inventory("a" * 40)

    def test_inventory_only_accepts_allowlisted_python_modules(self):
        workflow = ".github/workflows/ci.yml"
        with patch.object(policy, "git", return_value=workflow.encode() + b"\0"), \
             patch.object(policy, "read_file") as read:
            for command in ("python -m local_helper", "python3 -I -m local_helper", "python -X dev -W ignore -m local_helper",
                            '"/venv/bin/python3.13" -Im local_helper', 'python -m "$MODULE"'):
                with self.subTest(command=command), self.assertRaisesRegex(ValueError, "python -m module"):
                    read.return_value = f"jobs: {{check: {{steps: [{{run: {json.dumps(command)}}}]}}}}".encode()
                    policy.control_inventory("a" * 40)
            for module in ("pytest", "playwright", "ruff", "venv", "pip"):
                with self.subTest(module=module):
                    read.return_value = f"jobs: {{check: {{steps: [{{run: python3 -I -m {module}}}]}}}}".encode()
                    self.assertEqual(policy.control_inventory("a" * 40)["helpers"], set())

    def test_inventory_refuses_unlisted_python_modules(self):
        workflow = ".github/workflows/ci.yml"
        with patch.object(policy, "git", return_value=workflow.encode() + b"\0"), \
             patch.object(policy, "read_file") as read:
            for command in ("pypy -m tools.check", "uv run alternate -m tools.check",
                            "bash -lc 'alternate -m tools.check'", "$PY -m tools.check"):
                with self.subTest(command=command), self.assertRaisesRegex(ValueError, "python -m module"):
                    read.return_value = json.dumps({"jobs": {"check": {"steps": [{"run": command}]}}}).encode()
                    policy.control_inventory("a" * 40)

    def test_inventory_reports_forms_it_cannot_resolve_for_the_workflow_reviewer(self):
        # Design (19 Nov): workflow files are owned, so these forms are reported for the owner's audit, not refused.
        workflow = ".github/workflows/ci.yml"
        cases = {
            "alternate -p tools.check": "plugin option -p tools.check",
            "$PY -m pytest": "nonliteral interpreter $PY",
            'if env MODE=check "$PY" -m pytest; then true; fi': "nonliteral interpreter $PY",
            '"${PYTHON}" -I -m pytest': "nonliteral interpreter ${PYTHON}",
            '"${{ steps.setup.outputs.python }}" -I -m pip': "nonliteral interpreter ${WORKFLOW_EXPRESSION}",
            "python -c 'print(1)'": "inline Python (-c or stdin)",
            "python3 -Ic'print(1)'": "inline Python (-c or stdin)",
            "python -": "inline Python (-c or stdin)",
            "python3 -I - <<'PY'\nprint(1)\nPY": "inline Python (-c or stdin)",
            "pytest -p no:cacheprovider": "pytest plugin option",
            "python -m pytest -ptools.check": "pytest plugin option",
            "python -m pytest -p tools.check": "plugin option -p tools.check",
            "pip install -e .": "editable pip install",
            "pip3 install --editable=.": "editable pip install",
            "python -m pip install -e.": "editable pip install",
        }
        with patch.object(policy, "git", return_value=workflow.encode() + b"\0"), \
             patch.object(policy, "read_file") as read:
            for command, reference in cases.items():
                with self.subTest(command=command):
                    read.return_value = json.dumps({"jobs": {"check": {"steps": [{"run": command}]}}}).encode()
                    unresolved = policy.control_inventory("a" * 40)["unresolved"]
                    self.assertIn({"workflow": workflow, "job": "check", "step": "step 1",
                                   "run_sha256": policy.hashlib.sha256(command.encode()).hexdigest(),
                                   "reference": reference}, unresolved)
            read.return_value = json.dumps({"jobs": {"check": {"steps": [{"run": "python -m pytest -q"}]}}}).encode()
            self.assertEqual(policy.control_inventory("a" * 40)["unresolved"], [])

    def test_unresolved_entries_are_kept_per_step_and_marked_new_against_the_base(self):
        workflow = ".github/workflows/ci.yml"
        steps = [{"name": "First", "run": "python -c 'print(0)'"}, {"id": "second", "run": "python -c 'print(1)'"},
                 {"run": "python -c 'print(2)'"}]
        with patch.object(policy, "git", return_value=workflow.encode() + b"\0"), \
             patch.object(policy, "read_file",
                          return_value=json.dumps({"jobs": {"check": {"steps": steps}}}).encode()):
            entries = policy.control_inventory("a" * 40)["unresolved"]
        self.assertEqual(sorted(e["step"] for e in entries), ["First", "second", "step 3"])
        self.assertEqual(len({e["run_sha256"] for e in entries}), 3)
        changed = {**entries[0], "run_sha256": "0" * 64}
        marked = policy.mark_new([changed, *entries[1:]], entries)
        self.assertEqual([(e["step"], e["new"]) for e in marked],
                         [(changed["step"], True)] + [(e["step"], False) for e in entries[1:]])

    def test_guard_reports_a_new_inline_python_step_in_findings_and_summary(self):
        from workflow_privacy import HOSTED_STEP

        workflow = ".github/workflows/ci.yml"
        existing = {"name": "Existing", "run": "python -c 'print(0)'"}
        added = {"name": "Added", "run": "python -c 'print(1)'"}
        def document(*steps):
            return {"jobs": {"check": {"runs-on": "ubuntu-24.04", "steps": [HOSTED_STEP, *steps]}}}
        with tempfile.TemporaryDirectory() as folder, contextlib.chdir(folder):
            summary = Path(folder, "summary.md")
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}):
                self.run_guard(workflow, files={workflow: (document(existing), document(existing, added))},
                               real_inventory=True)
            findings = json.loads(Path(".run/policy/findings.json").read_text(encoding="utf-8"))
            text = summary.read_text(encoding="utf-8")
        entries = findings["unresolved_executable_references"]
        self.assertEqual([(e["step"], e["new"]) for e in entries], [("Added", True), ("Existing", False)])
        added_line = 'NEW Unresolved executable reference in `".github/workflows/ci.yml / check / Added"`'
        existing_line = '- Unresolved executable reference in `".github/workflows/ci.yml / check / Existing"`'
        self.assertIn(added_line, text)
        self.assertLess(text.index(added_line), text.index(existing_line))

    def test_findings_carry_unresolved_executable_references(self):
        item = {"workflow": ".github/workflows/ci.yml", "job": "check", "step": "Run", "run_sha256": "0" * 64,
                "reference": "inline Python (-c or stdin)", "new": True}
        with tempfile.TemporaryDirectory() as folder, contextlib.chdir(folder), \
             patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}):
            report = policy.findings_report("dev", "a" * 40, "b" * 40, "c" * 40, [], "d" * 40, None, [item])
            written = json.loads(Path(".run/policy/findings.json").read_text(encoding="utf-8"))
            empty = policy.findings_report("dev", "a" * 40, "b" * 40, "c" * 40, [], "d" * 40)
        self.assertEqual(report["unresolved_executable_references"], [item])
        self.assertEqual(written["unresolved_executable_references"], [item])
        self.assertEqual(empty["unresolved_executable_references"], [])

    def test_inventory_accepts_literal_wrappers_and_tracks_extensionless_helpers(self):
        workflow = ".github/workflows/ci.yml"
        files = {
            workflow: json.dumps({"jobs": {"check": {"steps": [
                {"run": "mkdir -p reports.v1; alternate -m pytest"},
                {"run": 'python -m pip install --target "$RUNNER_TEMP/policy-yaml" -r requirements/policy.lock'},
                {"run": "./scripts/check && bash .github/scripts/run-check"},
                {"run": "bash -c 'python tools/check'"},
            ]}}}).encode(),
            "scripts/check": b"", ".github/scripts/run-check": b"", "tools/check": b"",
        }
        with patch.object(policy, "git", return_value=b"\0".join(p.encode() for p in files) + b"\0"), \
             patch.object(policy, "read_file", side_effect=lambda rev, path: files[path]):
            inventory = policy.control_inventory("a" * 40)
        self.assertEqual(inventory["direct_helpers"], set(files) - {workflow})
        for helper in inventory["direct_helpers"]:
            self.assertEqual(policy.tier(helper, None, files[helper], inventory)[0], 1)

    def test_inventory_refuses_unparseable_shell_run_with_workflow_and_reason(self):
        workflow = ".github/workflows/ci.yml"
        cases = (
            ("bash -c 'printf %s", "No closing quotation"),
            ('bash -c \'printf "%s" "unclosed\'', "No closing quotation"),
            ("bash -c", "shell -c needs a command"),
        )
        with patch.object(policy, "git", return_value=workflow.encode() + b"\0"), \
             patch.object(policy, "read_file") as read:
            for command, reason in cases:
                message = f"Cannot parse run step in {workflow}: {reason}; simplify the quoting"
                with self.subTest(command=command), self.assertRaisesRegex(ValueError, re.escape(message)):
                    read.return_value = json.dumps({"jobs": {"check": {"steps": [{"run": command}]}}}).encode()
                    policy.control_inventory("a" * 40)

    def test_inventory_follows_nested_composite_actions_and_direct_scripts(self):
        workflow = ".github/workflows/ci.yml"
        action = ".github/actions/check/action.yml"
        nested = ".github/actions/nested/action.yaml"
        files = {
            workflow: b'jobs: {check: {steps: [{uses: ./.github/actions/check}, {run: node tools/task.js}]}}',
            action: b'runs: {using: composite, steps: [{run: python3 -I tools/check.py}, {uses: ./.github/actions/nested}]}',
            nested: b'''runs: {using: composite, steps: [{run: bash "$GITHUB_ACTION_PATH/check.sh"}]}''',
            "tools/task.js": b"", ".github/actions/nested/check.sh": b"", "tools/check.py": b"pass\n",
        }
        with patch.object(policy, "git", return_value=b"\0".join(p.encode() for p in files) + b"\0"), \
             patch.object(policy, "read_file", side_effect=lambda rev, path: files[path]):
            inventory = policy.control_inventory("a" * 40)
            expected = set(files) - {workflow}
            self.assertEqual(inventory["helpers"], expected)
            for path in expected:
                self.assertEqual(policy.tier(path, None, files[path], inventory)[0], 1)
            files[nested] = b'runs: {using: composite, steps: [{run: python3.13 -I -m local_helper}]}'
            with self.assertRaisesRegex(ValueError, "python -m module"):
                policy.control_inventory("a" * 40)

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
                  kind="pull_request", after_removed=None, after_python=None, preview_probe=None,
                  before_codeowners=(b"/.github/ @ShiftEZ-ai/maintainers\n/ci/ @ShiftEZ-ai/maintainers\n"
                                     b"/tests/quarantine.py @ShiftEZ-ai/maintainers\n"
                                     b"/tests/quarantine.json @ShiftEZ-ai/maintainers\n"),
                  after_codeowners=None, candidate_helpers=None, http=None, token="synthetic-token", files=None,
                  real_inventory=False, default="dev"):
        base, head, candidate, source = (c * 40 for c in "abcd")
        data = {"number": 1, "pull_request": {"base": {"ref": branch, "sha": base, "repo": {"id": policy.REPOSITORY}},
                 "head": {"ref": "feature", "sha": head, "repo": {"id": policy.REPOSITORY}}, "merge_commit_sha": candidate}}
        data["repository"] = {"default_branch": default}
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
            if args == ("ls-tree", "-r", "--name-only", "-z", candidate, "--", ".github/workflows/"):
                return b"".join(name.encode() + b"\0" for name in files or {} if name.startswith(".github/workflows/"))
            if real_inventory and args[:4] == ("ls-tree", "-r", "--name-only", "-z") and len(args) == 5:
                return b"".join(name.encode() + b"\0" for name in files or {})
            if args[0]=='ls-tree':return b''
            if args[0] == "rev-parse": return source.encode()
            if args[0] == "diff": return changed.encode() + b"\0"
            if args[0] == "rev-list": return b"preview-merge\n" if preview_history else b""
            raise AssertionError(args)
        def record(branch, base, head, candidate, rows, source, candidate_sha=None, unresolved=()):
            report.update({"branch": branch, "target_sha": base, "candidate_tree": candidate, "candidate_sha": candidate_sha,
                           "tier1": [r for r in rows if r["tier"] == 1], "tier2": [r for r in rows if r["tier"] == 2],
                           "unresolved_executable_references": list(unresolved)})
            return report
        def read(rev, name):
            if files and name in files and (rev in {base, candidate} or branch == "main" and rev == source):
                value = files[name][rev != base]
                return value if value is None or isinstance(value, bytes) else json.dumps(value).encode()
            if name == policy.QUARANTINE_RETIRED:
                return b"[]"
            if name == ".github/CODEOWNERS":
                if rev == candidate and after_codeowners is not None:
                    return after_codeowners
                return before_codeowners if rev == base else None
            if name=='ci/removed-tests.json':
                return json.dumps(after_removed if rev==candidate and after_removed is not None else {'schema':2,'removed':[],'removed_nodes':[]}).encode()
            if name == policy.KNOWN:
                return json.dumps(after_known if rev == candidate and after_known is not None else self.before).encode()
            if name.endswith(".py"):
                return after_python if rev == candidate and after_python is not None else b"x=1\n"
            return b"{}"
        def inventory(rev):
            if rev == candidate and candidate_helpers is not None:
                return {**self.inventory, "helpers": candidate_helpers}
            return self.inventory.copy()
        mocks = {"event": lambda: data, "git": fake_git, "fetch": fake_fetch,
                 "merge_tree": lambda *a: candidate,
                 "tree": lambda rev: ((merge_tree or candidate) if rev == head else candidate),
                 "accepted_dev_commit": lambda candidate_tree, dev_tip: source,
                 "control_inventory": inventory,
                 "migrations": lambda revision: {}, "read_file": read, "findings_report": record}
        if real_inventory:
            del mocks["control_inventory"], mocks["findings_report"]
        with contextlib.ExitStack() as stack:
            for name, value in mocks.items():
                stack.enter_context(patch.object(policy, name, value))
            stack.enter_context(patch.dict(policy.os.environ, {"GITHUB_EVENT_NAME": kind, "TRUSTED_BOOTSTRAP": "false"}))
            stack.enter_context(patch.dict(policy.os.environ, {"GITHUB_REPOSITORY": "owner/repo", "GH_TOKEN": "",
                                                              "GITHUB_TOKEN": token}))
            stack.enter_context(patch.object(policy, "urlopen", http if http is not None else self.review_response()))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            policy.guard()
        return report

    @staticmethod
    def review_response(*, rules=None, **overrides):
        flags = dict.fromkeys(policy.REVIEW_FLAGS, True) | overrides
        payload = [{"type": "pull_request", "parameters": flags}] if rules is None else rules
        return Mock(return_value=contextlib.closing(io.BytesIO(json.dumps(payload).encode())))

    def test_guard_checks_active_reviews_on_both_targets_and_events(self):
        for branch in ("dev", "main"):
            for kind in ("pull_request", "merge_group"):
                with self.subTest(branch=branch, kind=kind):
                    http = self.review_response()
                    self.run_guard(".github/workflows/ci.yml", branch=branch, kind=kind, http=http)
                    http.assert_called_once()
                    request = http.call_args.args[0]
                    self.assertEqual(request.full_url, f"https://api.github.com/repos/owner/repo/rules/branches/{branch}")
                    self.assertEqual(request.get_method(), "GET")
                    self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-token")
        http = Mock()
        self.run_guard("apps/example.py", http=http, token="")
        http.assert_not_called()

    def test_guard_refuses_disabled_review_flags(self):
        for flag in policy.REVIEW_FLAGS:
            with self.subTest(flag=flag), self.assertRaisesRegex(ValueError, "Active branch rules must require"):
                self.run_guard(".github/workflows/ci.yml", http=self.review_response(**{flag: False}))

    def test_guard_requires_only_code_owner_review_and_stale_dismissal(self):
        self.assertEqual(policy.REVIEW_FLAGS, ("require_code_owner_review", "dismiss_stale_reviews_on_push"))
        for last_push in (False, True):
            with self.subTest(last_push=last_push):
                http = self.review_response(require_last_push_approval=last_push)
                self.run_guard(".github/workflows/ci.yml", http=http)
                http.assert_called_once()

    def test_guard_refuses_review_api_errors_or_malformed_responses(self):
        http = Mock(side_effect=HTTPError("", 403, "", None, None))
        with self.assertRaisesRegex(ValueError, "Could not read active branch review rules"):
            self.run_guard(".github/workflows/ci.yml", http=http)
        for rules in ({}, [None], [{"type": "pull_request", "parameters": {}}]):
            with self.subTest(rules=rules), self.assertRaisesRegex(ValueError, "Unexpected active branch"):
                self.run_guard(".github/workflows/ci.yml", http=self.review_response(rules=rules))

    def test_guard_refuses_missing_review_token_without_calling_api(self):
        http = Mock()
        with self.assertRaisesRegex(ValueError, "require a read-only GitHub token"):
            self.run_guard(".github/workflows/ci.yml", http=http, token="")
        http.assert_not_called()

    def test_guard_checks_privacy_of_every_candidate_workflow_blob(self):
        safe = b"on: [pull_request, merge_group]\n"
        bad = b"on: {issue_comment: null}\n"
        files = {".github/workflows/other.yaml": (safe, bad), ".github/workflows/ci.yml": (safe, safe)}
        for branch in ("dev", "main"):
            for kind in ("pull_request", "merge_group"):
                with self.subTest(branch=branch, kind=kind), self.assertRaisesRegex(ValueError, "issue_comment is forbidden"):
                    self.run_guard(".github/workflows/ci.yml", branch=branch, kind=kind, files=files)

    def test_checks_runs_the_trusted_guard_before_fan_out(self):
        import yaml
        workflows = HELPERS if (HELPERS / "ci.yml").is_file() else HELPERS.parent / "workflows"
        workflow = yaml.load((workflows / "ci.yml").read_bytes(), Loader=yaml.BaseLoader)
        jobs = workflow["jobs"]
        steps = jobs["checks"]["steps"]
        guard = next(step for step in steps if step.get("name") == "Enforce working policy before test fan-out")
        parser = next(step for step in steps if step.get("name") == "Install the trusted workflow parser")
        self.assertLess(steps.index(parser), steps.index(guard))
        self.assertEqual(parser["working-directory"], ".trusted-controls")
        self.assertIn("--require-hashes", parser["run"])
        self.assertIn("-r requirements/policy.lock", parser["run"])
        self.assertEqual(guard["working-directory"], ".trusted-controls")
        self.assertEqual(guard["if"], "github.event_name == 'pull_request' || github.event_name == 'merge_group'")
        guard_env = {"POLICY_WORKFLOW_SHA": "${{ github.workflow_sha }}", "GITHUB_TOKEN": "${{ github.token }}"}
        self.assertEqual(guard["env"], guard_env)
        self.assertEqual(jobs["checks"].get("permissions", workflow["permissions"]), {"contents": "read"})
        for i, step in enumerate(steps):
            if step.get("id") == "plan" or step.get("uses", "").startswith("actions/upload-artifact@"):
                self.assertLess(steps.index(guard), i)
        for job in ("unit", "browser", "static"):
            self.assertEqual(jobs[job]["needs"], "checks")
            self.assertIn("needs.checks.result == 'success'", jobs[job]["if"])
        standalone = yaml.load((workflows / "working-policy.yml").read_bytes(), Loader=yaml.BaseLoader)
        standalone_steps = standalone["jobs"]["policy"]["steps"]
        self.assertIn("python -I .github/scripts/working_policy.py guard", [step.get("run") for step in standalone_steps])
        self.assertEqual(next(s for s in standalone_steps if s.get("name") == "Enforce target policy without executing PR code")["env"], guard_env)
        self.assertEqual(standalone["jobs"]["policy"].get("permissions", standalone["permissions"]), {"contents": "read"})
        self.assertEqual(guard["run"],
                         '"${{ steps.policy-python.outputs.python }}" -I .github/scripts/working_policy.py guard')

    def run_checks_guard_step(self, guard_source, branch="dev", tier1_path=".github/workflows/ci.yml"):
        import yaml
        workflows = HELPERS if (HELPERS / "ci.yml").is_file() else HELPERS.parent / "workflows"
        steps = yaml.load((workflows / "ci.yml").read_bytes(), Loader=yaml.BaseLoader)["jobs"]["checks"]["steps"]
        step = next(step for step in steps if step.get("name") == "Enforce working policy before test fan-out")
        script = step["run"].replace("${{ steps.policy-python.outputs.python }}", Path(sys.executable).as_posix())
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as trusted:  # stands in for the .trusted-controls dev checkout
            (Path(trusted) / ".github/scripts").mkdir(parents=True)
            (Path(trusted) / ".github/scripts/working_policy.py").write_text(guard_source, encoding="utf-8")
            env = {key: value for key, value in os.environ.items() if key != "TRUSTED_BOOTSTRAP"}
            env.update(GITHUB_STEP_SUMMARY=str(Path(trusted) / "summary"), STUB_BRANCH=branch, STUB_TIER1=tier1_path)
            return subprocess.run(["bash", "-e", "-c", script], cwd=trusted, env=env, capture_output=True, text=True)

    def test_checks_runs_the_code_owner_guard_once_dev_has_it(self):
        new_guard = "import sys\nprint('code-owner guard', sys.argv[1:])\n"
        result = self.run_checks_guard_step(new_guard)
        self.assertEqual((result.returncode, result.stdout), (0, "code-owner guard ['guard']\n"), result.stderr)
        # Once this change lands, dev's guard is this tree's guard, so the wrapper branch can never run again.
        self.assertNotIn(LEGACY_ROUTE, (HELPERS / "working_policy.py").read_text(encoding="utf-8"))

    def test_guard_keeps_other_policy_refusals(self):
        addition = copy.deepcopy(self.before)
        addition["fails"].append({**addition["fails"][0], "node": "tests/test_new.py::test_case"})
        cases = [
            (policy.KNOWN, {"after_known": addition}, "Known-failures changes"),
            ("AGENTS.md", {}, "private agent instructions"),
            ("apps/example.py", {"after_python": b"x = ("}, "syntax error"),
            (".github/workflows/ci.yml", {"preview_history": True}, "Preview merge history"),
        ]
        for path, options, message in cases:
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, message):
                self.run_guard(path, **options)

    def test_guard_allows_changes_in_owned_tier1_paths(self):
        clean = self.run_guard("apps/example.py")
        self.assertEqual(clean["tier1"], [])
        for kind in ("pull_request", "merge_group"):
            with self.subTest(kind=kind):
                report = self.run_guard(".github/workflows/ci.yml", kind=kind)
                self.assertEqual([row["path"] for row in report["tier1"]], [".github/workflows/ci.yml"])
                self.assertEqual(report["candidate_sha"], "b" * 40 if kind == "merge_group" else None)

    def test_guard_refuses_unowned_tier1_path(self):
        with self.assertRaisesRegex(ValueError, "Add a CODEOWNERS rule for scripts/build[.]py"):
            self.run_guard("scripts/build.py")

    def test_guard_refuses_new_workflow_helper_without_base_owner(self):
        with self.assertRaisesRegex(ValueError, "Add a CODEOWNERS rule for scripts/check[.]py"):
            self.run_guard(".github/workflows/ci.yml\0scripts/check.py", candidate_helpers={"scripts/check.py"})

    def test_guard_refuses_tier1_mechanisms_outside_owned_paths(self):
        cases = (
            ("apps/example.py", b"def pytest_configure(config): pass\n"),
            ("apps/sitecustomize.py", b"x=2\n"),
        )
        for path, raw in cases:
            with self.subTest(path=path, raw=raw), self.assertRaisesRegex(ValueError, "Move the Tier 1 mechanism"):
                self.run_guard(path, after_python=raw)

    def test_guard_uses_codeowners_from_the_base_for_each_event(self):
        for kind in ("pull_request", "merge_group"):
            changed = ".github/CODEOWNERS\0scripts/build.py"
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "Add a CODEOWNERS rule for scripts/build"):
                self.run_guard(changed, kind=kind,
                               after_codeowners=b"/.github/ @ShiftEZ-ai/maintainers\n/scripts/ @ShiftEZ-ai/maintainers\n")
            report = self.run_guard(changed, kind=kind,
                                    before_codeowners=b"/.github/ @ShiftEZ-ai/maintainers\n/scripts/ @ShiftEZ-ai/maintainers\n",
                                    after_codeowners=b"/.github/ @team\n")
            self.assertEqual([row["path"] for row in report["tier1"]], [".github/CODEOWNERS", "scripts/build.py"])

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

    def test_guard_keeps_owned_tier1_and_tier2_findings(self):
        owned = self.run_guard("tests/quarantine.py")
        self.assertEqual(owned["tier1"][0]["path"], "tests/quarantine.py")
        report = self.run_guard("requirements/test.lock")
        self.assertEqual(report["tier2"][0]["path"], "requirements/test.lock")
        self.assertEqual(report["tier1"], [])

    def test_guard_refuses_an_expiry_extension_to_existing_entries(self):
        after = copy.deepcopy(self.before)
        after["unstable"][0]["expires_on"] = "2100-01-01"
        for kind in ("pull_request", "merge_group"):
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "Known-failures changes"):
                self.run_guard(policy.KNOWN, after_known=after, kind=kind)
        after["unstable"][0]["class"] = "baseline"
        with self.assertRaisesRegex(ValueError, "Known-failures changes"):
            self.run_guard(policy.KNOWN, after_known=after)

    def test_guard_removal_repair_and_promotion(self):
        after = copy.deepcopy(self.before)
        after["fails"].pop()
        self.assertEqual(self.run_guard(policy.KNOWN, after_known=after)["tier1"], [])
        self.run_guard(".github/workflows/ci.yml", branch="main")
        with self.assertRaises(ValueError): self.run_guard("apps/example.py", branch="main", merge_tree="u" * 40)
        with self.assertRaises(ValueError): self.run_guard("apps/example.py", preview_history=True)

    def test_guard_integration_target_is_the_default_branch_under_either_name(self):
        for default in ("dev", "main"):
            for kind in ("pull_request", "merge_group"):
                with self.subTest(default=default, kind=kind):
                    self.assertEqual(self.run_guard("apps/example.py", branch=default, kind=kind, default=default)["branch"],
                                     default)
                    http = self.review_response()
                    self.run_guard(".github/workflows/ci.yml", branch=default, kind=kind, default=default, http=http)
                    self.assertEqual(http.call_args.args[0].full_url,
                                     f"https://api.github.com/repos/owner/repo/rules/branches/{default}")
                    with self.assertRaisesRegex(ValueError, "Preview merge history"):
                        self.run_guard("apps/example.py", branch=default, kind=kind, default=default, preview_history=True)

    def test_main_target_is_release_checked_only_while_the_default_is_dev(self):
        for kind in ("pull_request", "merge_group"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(ValueError, "Main would add content beyond the accepted dev tree"):
                    self.run_guard("apps/example.py", branch="main", kind=kind, merge_tree="u" * 40)
                # Once main is the default it is the integration branch: no release tree equivalence applies.
                report = self.run_guard("apps/example.py", branch="main", kind=kind, merge_tree="u" * 40, default="main")
                self.assertEqual((report["branch"], report["tier1"]), ("main", []))

    def test_default_main_retires_the_dev_and_preview_targets(self):
        self.assertEqual(policy.working_branches("dev"), {"dev", "main", "preview"})
        self.assertEqual(policy.working_branches("main"), {"main"})
        self.assertEqual(self.run_guard("apps/example.py", branch="dev", default="main"), {})
        for default, branch in (("main", "dev"), ("main", "preview"), ("dev", "preview")):
            with self.subTest(default=default, branch=branch), self.assertRaisesRegex(ValueError, "Unexpected queue target"):
                self.run_guard("apps/example.py", branch=branch, kind="merge_group", default=default)

    def test_missing_or_unexpected_default_branch_refuses(self):
        for default in (None, "", "preview", "release"):
            for kind in ("pull_request", "merge_group"):
                with self.subTest(default=default, kind=kind), \
                        self.assertRaisesRegex(ValueError, "Unexpected repository or default branch"):
                    self.run_guard("apps/example.py", kind=kind, default=default)
        with self.assertRaisesRegex(ValueError, "Unexpected repository or default branch"):
            policy.default_branch({})
        repository = {"id": policy.REPOSITORY, "full_name": "ShiftEZ-ai/shiftez-clinical"}
        with tempfile.TemporaryDirectory() as folder:
            event = Path(folder) / "event.json"
            for extra in ({}, {"default_branch": None}, {"default_branch": "preview"}):
                event.write_text(json.dumps({"repository": {**repository, **extra}}))
                with self.subTest(extra=extra), patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(event)}), \
                        self.assertRaisesRegex(ValueError, "Unexpected repository or default branch"):
                    policy.event()
            for default in ("dev", "main"):
                event.write_text(json.dumps({"repository": {**repository, "default_branch": default}}))
                with patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(event)}):
                    self.assertEqual(policy.default_branch(policy.event()), default)

    def run_ci_context(self, kind, ref, default):
        before, head, accepted = "a" * 40, "b" * 40, "e" * 40
        data = {"before": before, "repository": {"default_branch": default},
                "pull_request": {"base": {"ref": ref, "sha": before}},
                "merge_group": {"base_ref": "refs/heads/" + ref, "base_sha": before}}
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "output"
            fetched = []
            with patch.object(policy, "event", return_value=data), patch.object(policy, "git", return_value=head.encode()), \
                 patch.object(policy, "tree", return_value="c" * 40), \
                 patch.object(policy, "fetch", side_effect=lambda name: fetched.append(name) or "d" * 40), \
                 patch.object(policy, "accepted_dev_commit", return_value=accepted), \
                 patch.dict(os.environ, {"GITHUB_EVENT_NAME": kind, "GITHUB_REF": "refs/heads/" + ref,
                                         "GITHUB_OUTPUT": str(output)}):
                policy.ci_context()
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        return values["base_sha"], values["release"], fetched

    def test_ci_context_follows_the_default_branch(self):
        before, head, accepted, tip = "a" * 40, "b" * 40, "e" * 40, "d" * 40
        cases = (("push", "dev", "dev", (before, "false", [])),
                 ("push", "main", "dev", (accepted, "true", ["refs/heads/dev"])),
                 ("push", "preview", "dev", (tip, "false", ["refs/heads/dev"])),
                 ("push", "main", "main", (before, "false", [])),
                 ("schedule", "main", "dev", (head, "false", [])),
                 ("schedule", "main", "main", (head, "false", [])),
                 ("pull_request", "main", "main", (before, "false", [])),
                 ("merge_group", "main", "main", (before, "false", [])),
                 ("pull_request", "dev", "dev", (before, "false", [])))
        for kind, ref, default, expected in cases:
            with self.subTest(kind=kind, ref=ref, default=default):
                self.assertEqual(self.run_ci_context(kind, ref, default), expected)
        for kind, ref, default in (("push", "dev", "main"), ("push", "preview", "main"), ("merge_group", "dev", "main"),
                                   ("push", "feature", "dev"), ("push", "main", None)):
            with self.subTest(kind=kind, ref=ref, default=default), self.assertRaises(ValueError):
                self.run_ci_context(kind, ref, default)

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
        with self.assertRaisesRegex(ValueError, "may only add removed nodes"):
            self.run_guard('ci/removed-tests.json', after_removed=after)

    def test_guard_only_removes_entries_from_owned_allowance_lists(self):
        for path, key in (("ci/known-skips.json", "skips"), ("ci/frozen-baseline-failures.json", None),
                          ("ci/failure-classifications.json", "entries"), ("ci/hosted-env-exceptions.json", "exceptions"),
                          ("ci/unstable-seeds.json", "nodes")):
            def allowance(entries):
                return {"schema": 1, key: entries} if key else entries
            first, second, edited = ({"node": "a"}, {"node": "b"}, {"node": "a", "reason": "edited"})
            before = allowance([first])
            for after in (allowance([first, second]), allowance([edited])):
                with self.subTest(path=path, after=after), self.assertRaisesRegex(ValueError, "may only remove"):
                    self.run_guard(path, files={path: (before, after)})
            with self.subTest(path=path):
                report = self.run_guard(path, files={path: (before, allowance([]))})
                self.assertEqual([row["path"] for row in report["tier1"]], [path])

    def quarantine_case(self, before, after, known_before=None, known_after=None):
        path = policy.QUARANTINE
        known = {"schema": 2, "tree": "a" * 40, "fails": [], "unstable": []}
        files = {path: (before, after), policy.KNOWN: (known_before or known, known_after or known_before or known)}
        changed = path + ("\0" + policy.KNOWN if known_before is not None else "")
        return self.run_guard(changed, files=files)

    QUARANTINE_NODE = "tests/test_a.py::test_a"
    MOVED = {"nodeid": QUARANTINE_NODE, "reason": "baseline: measured", "owner": "maintainers",
             "added": "2026-11-19", "expires": "2026-12-10", "issue": 7, "milestone": "v0.2.0", "blocker": False}
    FLAKE = {"nodeid": "tests/test_b.py::test_b", "reason": "unstable: recovered on retry", "owner": "maintainers",
             "added": "2026-11-19", "expires": "2026-12-19", "issue": 9, "milestone": "v0.2.0", "blocker": False,
             "recovered_runs": ["44575823857"]}

    def known_lists(self, *nodes):
        return {"schema": 2, "tree": "a" * 40, "unstable": [],
                "fails": [{"node": node, "class": "baseline", "owner": "maintainer", "reason": "measured"} for node in nodes]}

    def assert_quarantine_refused(self, message, after, known_before=None, known_after=None, before=()):
        with self.assertRaisesRegex(ValueError, "^" + re.escape(policy.QUARANTINE + " " + message)):
            self.quarantine_case(list(before), after, known_before, known_after)

    def test_quarantine_keeps_or_removes_entries_but_never_edits_them(self):
        # Design (19 Nov): quarantine additions are allowed only as MOVE or FLAKE; edits stay refused.
        report = self.quarantine_case([self.MOVED], [])
        self.assertIn(policy.QUARANTINE, [row["path"] for row in report["tier1"]])
        self.assert_quarantine_refused("may not edit an existing entry: " + self.QUARANTINE_NODE,
                                       [{**self.MOVED, "expires": "2027-01-10"}], before=[self.MOVED])

    def test_a_moved_unstable_entry_may_not_outlive_its_known_failures_expiry(self):
        node = self.QUARANTINE_NODE
        source = {"node": node, "class": "unstable", "owner": "maintainer", "reason": "recovered",
                  "issue": 7, "expires_on": "2026-12-01"}
        known_before = {"schema": 2, "tree": "a" * 40, "fails": [], "unstable": [source]}
        known_after = {**known_before, "unstable": []}
        moved = {**self.MOVED, "reason": "unstable: recovered", "expires": "2026-12-01"}
        self.quarantine_case([], [moved], known_before, known_after)
        self.assert_quarantine_refused("moved entries may not expire later than their " + policy.KNOWN + " entry: " + node,
                                       [{**moved, "expires": "2026-12-02"}], known_before, known_after)

    def test_quarantine_accepts_a_move_from_known_failures_with_the_same_class(self):
        known_before, known_after = self.known_lists(self.QUARANTINE_NODE), self.known_lists()
        report = self.quarantine_case([], [self.MOVED], known_before, known_after)
        self.assertIn(policy.QUARANTINE, [row["path"] for row in report["tier1"]])
        node = self.QUARANTINE_NODE
        cases = (
            ({**self.MOVED, "reason": "unstable: measured"}, known_after, "may only add entries moved from"),
            ({**self.MOVED, "issue": None}, known_after, "additions need an issue number of at least 1: " + node),
            ({**self.MOVED, "issue": True}, known_after, "additions need an issue number of at least 1: " + node),
            ({**self.MOVED, "expires": "2026-12-20"}, known_after, "additions need an expiry at most 30 days after added: " + node),
            ({**self.MOVED, "expires": "2026-11-18"}, known_after, "additions need an expiry at most 30 days after added: " + node),
        )
        for entry, after_known, message in cases:
            with self.subTest(message=message, entry=entry):
                self.assert_quarantine_refused(message, [entry], known_before, after_known)

    def test_guard_refuses_known_failure_quarantine_overlap(self):
        known = self.known_lists(self.FLAKE["nodeid"])
        message = "entries must be disjoint from " + policy.KNOWN + ": " + self.FLAKE["nodeid"]
        self.assert_quarantine_refused(message, [self.FLAKE], known, known)
        self.assert_quarantine_refused(message, [self.FLAKE], known, known, before=[self.FLAKE])
        original = self.known_lists(self.QUARANTINE_NODE)
        self.assert_quarantine_refused("entries must be disjoint from " + policy.KNOWN + ": " + self.QUARANTINE_NODE,
                                       [self.MOVED], original, original)

    def test_quarantine_accepts_a_recovered_flake_with_runs_issue_and_short_expiry(self):
        self.quarantine_case([], [self.FLAKE])
        node = self.FLAKE["nodeid"]
        without_runs = {key: value for key, value in self.FLAKE.items() if key != "recovered_runs"}
        runs = "flake additions need the run ids where they recovered: " + node
        cases = (
            ({**self.FLAKE, "expires": "2026-12-20"}, "additions need an expiry at most 30 days after added: " + node),
            ({**self.FLAKE, "expires": "2026-11-18"}, "additions need an expiry at most 30 days after added: " + node),
            ({**self.FLAKE, "recovered_runs": []}, runs),
            ({**self.FLAKE, "recovered_runs": ["latest"]}, runs),
            ({**self.FLAKE, "recovered_runs": ["\u0661\u0662\u0663"]}, runs),
            ({**self.FLAKE, "recovered_runs": "123"}, runs),
            (without_runs, runs),
            ({**self.FLAKE, "reason": "baseline: new failure"}, "may only add entries moved from"),
            ({**self.FLAKE, "reason": "no class prefix"}, "additions need a reason that starts with its class: " + node),
            ({**self.FLAKE, "owner": " "}, "additions need an owner: " + node),
            ({k: v for k, v in self.FLAKE.items() if k != "blocker"}, "additions need a true or false blocker flag: " + node),
            ({**self.FLAKE, "blocker": "false"}, "additions need a true or false blocker flag: " + node),
            ({**self.FLAKE, "issue": 0}, "additions need an issue number of at least 1: " + node),
            ({**self.FLAKE, "issue": True}, "additions need an issue number of at least 1: " + node),
        )
        for entry, message in cases:
            with self.subTest(message=message, entry=entry):
                self.assert_quarantine_refused(message, [entry])
        self.assert_quarantine_refused("entries need one unique nodeid each", [self.FLAKE, self.FLAKE])

    def retired_case(self, before, after, *, registry_before=None, registry_after=None):
        registry = [self.MOVED, self.FLAKE] if registry_before is None else registry_before
        changed = policy.QUARANTINE_RETIRED
        if registry_after is not None:
            changed += "\0" + policy.QUARANTINE
        return self.run_guard(changed, files={
            policy.QUARANTINE: (registry, registry if registry_after is None else registry_after),
            policy.QUARANTINE_RETIRED: (before, after),
        })

    def test_guard_accepts_an_unowned_retired_append(self):
        first, second = self.FLAKE["nodeid"], self.QUARANTINE_NODE
        for failure_class in ("baseline", "baseline-error"):
            with self.subTest(failure_class=failure_class):
                row = {**self.MOVED, "reason": failure_class + ": measured"}
                report = self.retired_case([first], [first, second], registry_before=[row, self.FLAKE])
                self.assertEqual(report["tier1"], [])
                self.assertEqual(report["tier2"], [])

    def test_guard_refuses_retiring_a_release_blocker(self):
        with self.assertRaisesRegex(ValueError, "release blockers may not be retired"):
            self.retired_case([], [self.QUARANTINE_NODE], registry_before=[{**self.MOVED, "blocker": True}])

    def test_guard_refuses_retiring_an_unstable_entry(self):
        with self.assertRaisesRegex(ValueError, "only baseline or baseline-error entries may be retired"):
            self.retired_case([], [self.FLAKE["nodeid"]])

    def test_guard_refuses_readding_a_still_retired_node(self):
        node = self.QUARANTINE_NODE
        flake = {**self.FLAKE, "nodeid": node, "blocker": True}
        # Base retired, candidate list emptied: the check must read the BASE list.
        files = {policy.QUARANTINE: ([], [flake]), policy.QUARANTINE_RETIRED: ([node], [])}
        with self.assertRaisesRegex(ValueError, "may not re-add a retired entry: " + re.escape(node)):
            self.run_guard(policy.QUARANTINE, files=files)
        files[policy.QUARANTINE_RETIRED] = ([], [])
        report = self.run_guard(policy.QUARANTINE, files=files)
        self.assertEqual([row["path"] for row in report["tier1"]], [policy.QUARANTINE])

    def test_guard_refuses_a_retired_entry_edit(self):
        node = self.QUARANTINE_NODE
        edited = b'["\\u0074' + node[1:].encode() + b'"]'
        with self.assertRaisesRegex(ValueError, "existing entries must stay byte-unchanged"):
            self.retired_case([node], edited)

    def test_guard_refuses_a_retired_entry_removal_without_registry_removal(self):
        with self.assertRaisesRegex(ValueError, "append-only; remove the same nodes"):
            self.retired_case([self.QUARANTINE_NODE], [])
        with self.assertRaisesRegex(ValueError, "append-only; remove the same nodes"):
            self.retired_case([self.QUARANTINE_NODE], [], registry_after=[self.MOVED])

    def test_guard_refuses_retired_entry_reordering(self):
        first, second = self.QUARANTINE_NODE, self.FLAKE["nodeid"]
        with self.assertRaisesRegex(ValueError, "byte-unchanged and in order"):
            self.retired_case([first, second], [second, first])

    def test_guard_refuses_a_retired_node_absent_from_the_base_registry(self):
        with self.assertRaisesRegex(ValueError, "new entry is absent from the base"):
            self.retired_case([], ["tests/test_other.py::test_other"])

    def test_guard_refuses_a_duplicate_retired_node(self):
        node = self.QUARANTINE_NODE
        with self.assertRaisesRegex(ValueError, "duplicate retired nodeid"):
            self.retired_case([node], [node, node])

    def test_guard_accepts_retired_compaction_with_owned_registry_removal(self):
        first, second = self.QUARANTINE_NODE, self.FLAKE["nodeid"]
        report = self.retired_case([first, second], [second], registry_after=[self.FLAKE])
        self.assertEqual([row["path"] for row in report["tier1"]], [policy.QUARANTINE])

    def test_guard_accepts_retired_compaction_after_prior_registry_removal(self):
        report = self.retired_case([self.QUARANTINE_NODE], [], registry_before=[self.FLAKE])
        self.assertEqual(report["tier1"], [])
        self.assertEqual(report["tier2"], [])

    def test_guard_allows_an_absent_retired_list_only_before_activation(self):
        script = ".github/scripts/working_policy.py"
        for branch in ("dev", "main"):
            for kind in ("pull_request", "merge_group"):
                with self.subTest(branch=branch, kind=kind):
                    files = {policy.QUARANTINE_RETIRED: (None, None), script: (b"import json", b"import json")}
                    report = self.run_guard("apps/ordinary.py", branch=branch, kind=kind, files=files)
                    self.assertEqual(report["branch"], branch)
                    active = b"from quarantine_registry import QUARANTINE_RETIRED"
                    files[script] = (active, active)
                    with self.assertRaisesRegex(ValueError, "retired list is missing"):
                        self.run_guard("apps/ordinary.py", branch=branch, kind=kind, files=files)

    def test_unowned_known_failures_still_allow_only_removals(self):
        # The base CODEOWNERS leaves ci/known-failures.json without an owner; the guard keeps it removal-only.
        codeowners = (b"/.github/ @ShiftEZ-ai/maintainers\n/ci/ @ShiftEZ-ai/maintainers\n"
                      b"/tests/quarantine.py @ShiftEZ-ai/maintainers\n/tests/quarantine.json @ShiftEZ-ai/maintainers\n"
                      b"/ci/known-failures.json\n")
        known, kept = self.known_lists("tests/test_a.py::test_a", "tests/test_b.py::test_b"), self.known_lists("tests/test_a.py::test_a")
        report = self.run_guard(policy.KNOWN, files={policy.KNOWN: (known, kept)}, before_codeowners=codeowners)
        self.assertEqual(report["tier1"], [])
        added = self.known_lists("tests/test_a.py::test_a", "tests/test_b.py::test_b", "tests/test_c.py::test_c")
        with self.assertRaisesRegex(ValueError, "^Known-failures changes must only remove existing entries$"):
            self.run_guard(policy.KNOWN, files={policy.KNOWN: (known, added)}, before_codeowners=codeowners)

    def test_known_failures_stay_removal_only_beside_a_quarantine_move(self):
        other = "tests/test_c.py::test_c"
        known_before, known_after = self.known_lists(self.QUARANTINE_NODE), self.known_lists(other)
        with self.assertRaisesRegex(ValueError, "^Known-failures changes must only remove existing entries$"):
            self.quarantine_case([], [self.MOVED], known_before, known_after)

    def test_guard_accepted_quarantine_entries_load_in_the_quarantine_plugin(self):
        import quarantine

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "quarantine.json")
            path.write_text(json.dumps([self.MOVED, self.FLAKE]), encoding="utf-8")
            loaded = quarantine.entries(path, policy.date(2026, 11, 20))
        self.assertEqual(set(loaded), {self.QUARANTINE_NODE, self.FLAKE["nodeid"]})

    def test_guard_refuses_edits_and_removals_in_owned_node_ledger(self):
        path = "ci/removed-tests.json"
        entry = {"node": "tests/test_a.py::test_old", "owner": "test-owner", "reason": "Reviewed redundant assertion"}
        before = {"schema": 2, "removed": [], "removed_nodes": [entry]}
        for rows in ([{**entry, "reason": "Reworded"}], []):
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, "may only add removed nodes"):
                self.run_guard(path, files={path: (before, {**before, "removed_nodes": rows})})

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
        files = {".github/workflows/ci.yml": b"jobs: {check: {steps: [{run: python scripts/check.py}]}}",
                 "scripts/check.py": b"x=1\n",
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
            data = {"merge_group":{"base_ref":"refs/heads/main", "base_sha":"a"*40,"head_sha":"b"*40},
                    "repository": {"default_branch": "dev"}}
            with patch.object(policy, "event", return_value=data), patch.object(policy, "fetch", side_effect=lambda ref, expected=None: expected or "d"*40), \
                 patch.object(policy, "tree", return_value="c"*40), patch.object(policy, "migrations", return_value={}), patch.object(policy,'assert_public_tree'), \
                 patch.object(policy, "git", return_value=b""), \
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

    def test_trusted_verdict_groups_all_expired_quarantine_entries_by_issue(self):
        rows = [self.quarantine_row(f"tests/test_unselected.py::test_{index}", issue, expires=expires)
                for index, (issue, expires) in enumerate(
                    [(19, "2000-01-02"), (2, "2000-01-02"), (19, "2000-01-03"), (2, "2099-01-01")])]
        self.exercise_verdict_materialization(
            "dev", quarantine_rows=rows,
            expired_summary="Expired quarantine entries by issue: #2: 1 node, #19: 2 nodes")

    def test_trusted_verdict_refuses_invalid_quarantine(self):
        for raw in (b'{"nodeid": "tests/test_a.py::test_a"}',
                    json.dumps([{**self.quarantine_row("tests/test_a.py::test_a", 7), "issue": True}]).encode(),
                    b"[", b"\xff", None):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "Invalid quarantine registry"):
                self.exercise_verdict_materialization("dev", quarantine_input=raw)

    def test_trusted_verdict_refuses_unreadable_quarantine(self):
        with self.assertRaisesRegex(ValueError, "Cannot read trusted quarantine registry"):
            self.exercise_verdict_materialization("dev", quarantine_input=OSError("unreadable fixture"))

    def test_main_refuses_quarantine_blocker_from_accepted_dev(self):
        blocker = self.quarantine_row("tests/test_a.py::test_a", 7, blocker=True)
        with self.assertRaisesRegex(ValueError, "Release blockers remain in quarantine.*test_a"):
            self.exercise_verdict_materialization("main", quarantine_rows=[blocker])

    def test_quarantined_disappearance_requires_a_removed_tests_line(self):
        row = self.quarantine_row("tests/test_a.py::test_a", 7)
        with self.assertRaisesRegex(ValueError, "Unlisted disappeared nodes.*test_a"):
            self.exercise_verdict_materialization("dev", quarantine_rows=[row], quarantine_node_disappears=True)

    @staticmethod
    def quarantine_row(node, issue, *, expires="2000-01-02", blocker=False):
        return {"nodeid": node, "owner": "maintainers", "reason": "baseline: reviewed",
                "added": "2000-01-01", "expires": expires, "issue": issue, "blocker": blocker}

    def test_trusted_verdict_refuses_an_expiry_extension(self):
        with self.assertRaisesRegex(ValueError, "Candidate allowance data must only remove existing entries"):
            self.exercise_verdict_materialization("dev", renew_expiry=True)

    def test_trusted_verdict_rejects_a_plan_with_the_wrong_replica_count(self):
        with self.assertRaisesRegex(ValueError, "Replica count differs from the trusted reader context"):
            self.exercise_verdict_materialization("dev", replicas=(1, 2))

    def test_old_base_pr_evaluates_merge_with_29_inherited_allowances_removed(self):
        self.exercise_verdict_materialization("dev", old_base_pr=True)

    def test_first_main_release_reads_accepted_dev_lists_when_main_has_none(self):
        self.exercise_verdict_materialization("main")
        # After the rename main is the integration branch: candidate lists apply and the PR gets the stale lookup.
        for event in ("pull_request", "merge_group", "push"):
            with self.subTest(event=event):
                self.exercise_verdict_materialization("main", event=event, default="main")
        with self.assertRaisesRegex(ValueError, "Unsupported branch"):
            self.exercise_verdict_materialization("dev", event="push", default="main")

    def test_main_refuses_a_recovery_from_an_earlier_attempt(self):
        with self.assertRaisesRegex(ValueError, "Release/known-skip execution contains a failure or error"):
            self.exercise_verdict_materialization("main", recovered={"tests/test_a.py::test_a": "failed"})
        self.exercise_verdict_materialization("dev", recovered={"tests/test_a.py::test_a": "failed"})

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
                                         old_base_pr=False, replicas=(1,), renew_expiry=False,
                                         quarantine_rows=(), quarantine_input=b"[]", expired_summary=None,
                                         quarantine_node_disappears=False, recovered=None, default="dev"):
        base, candidate, trusted, tree = (c * 40 for c in "abcd")
        if event == "workflow_dispatch": base = candidate
        head = "7" * 40
        release = branch == "main" and default != "main"
        target = "9" * 40 if release else base
        payload = {"before": base, "repository": {"full_name": "ShiftEZ-ai/shiftez-clinical", "default_branch": default},
                   "pull_request": {"base": {"ref": branch, "sha": target}, "head": {"sha": head}}}
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
        if renew_expiry:
            after = copy.deepcopy(before)
            after["unstable"][0]["expires_on"] = "2100-01-01"
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
            if name == policy.QUARANTINE:
                self.assertIn(revision, {base} if release else {base, candidate})
                if isinstance(quarantine_input, Exception):
                    raise quarantine_input
                return json.dumps(quarantine_rows).encode() if quarantine_rows else quarantine_input
            if name == policy.QUARANTINE_RETIRED:
                return b"[]"
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
            import make_known_failures as actual_reader
            maker.ReportEvidence = actual_reader.ReportEvidence
            retry_run = actual_reader.retry_run
            maker.retry_run = retry_run
            maker.write_summary_lines = actual_reader.write_summary_lines
            maker.canonical_node=lambda n:n
            maker.SAFE_NODE = re.compile(r"tests/.+::test_.+")
            observed = {"tests/test_a.py::test_a": "passed", "tests/test_browser_a.py::test_a": "passed"}
            if quarantine_node_disappears:
                del observed["tests/test_a.py::test_a"]

            def collect(*a, evidence=None, **kw):
                if recovered and evidence is not None:
                    evidence.recovered.update(recovered)
                return [observed]

            maker.collect_reports = collect
            maker.product_error_nodes = lambda *a, **kw: set()
            maker.mixed_listed_nodes = lambda *a: set()
            maker.outside_observations = lambda *a: []
            if real_reader:
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
            expected_release = release
            def compare(*, runs, evidence, product_errors, quarantine, release, quarantine_registry, **stale_options):
                self.assertEqual(quarantine, {row["nodeid"]: row for row in quarantine_rows})
                self.assertEqual(quarantine_registry, quarantine)
                self.assertEqual(release, expected_release)
                if release and quarantine:
                    actual_reader.evaluate(None, None, runs, quarantine=quarantine, release=release)
                self.assertEqual(set(stale_options), {"stale_lookup"} if event == "pull_request" and branch == default else set())
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
            step_summary = folder / "step-summary.md"
            env = {"GITHUB_STEP_SUMMARY": str(step_summary),
                   "NEEDS_JSON": json.dumps({k: {"result": "success"} for k in ("checks", "static", "unit", "browser")}),
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
                if verifier.hardening.load()["canaries"]:
                    canaries.verify_directory.assert_called_once_with(
                        reports, allow_product_errors=True, partial=False, allow_retry=retry_run(plan))
                else:  # stop-loss profiles reuse this helper with canaries switched off
                    canaries.verify_directory.assert_not_called()
            if expired_summary:
                self.assertIn(expired_summary + "\n", step_summary.read_text(encoding="utf-8"))
            self.assertEqual(summaries, [True])
            quarantine_paths = {policy.QUARANTINE, policy.QUARANTINE_RETIRED}
            for path in quarantine_paths:
                self.assertEqual({revision for revision, name in reads if name == path},
                                 {base} if release else {base, candidate})
            self.assertTrue(all(revision == base for revision, name in reads
                                if name not in quarantine_paths | {policy.KNOWN, 'ci/removed-tests.json'}))
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
                cache_only_push = "(github.event_name != 'push' || github.ref != format('refs/heads/{0}', github.event.repository.default_branch))"
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
                                'head': {'sha':head,'repo':{'id':policy.REPOSITORY}}},
                'repository': {'default_branch': 'dev'}}
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
        from make_known_failures import ReportEvidence
        maker = types.SimpleNamespace(ReportEvidence=ReportEvidence,
                                      collect_reports=lambda *a, **kw: [{'tests/test_a.py::test_a': 'failed'}] * 2)
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
        from make_known_failures import ReportEvidence
        for replicas, outcome in (([1],'passed'),([1,2],'error'),([1,2],'skipped')):
            maker = types.SimpleNamespace(ReportEvidence=ReportEvidence,
                                          collect_reports=lambda *a, **kw: [{'n': outcome}] * len(replicas))
            with patch.dict(os.environ, {'GITHUB_RUN_ID':'1','GITHUB_RUN_ATTEMPT':'1'}), \
                 patch.object(policy, 'read_file', return_value=None):
                with self.assertRaises(ValueError):
                    verifier.bootstrap_verdict({'replicas':replicas}, Path('.'), Path('.'), 'b'*40,
                                               'a'*40, 'a'*40, maker, {'canaries':False,'node_census':False})


    def test_findings_request_the_control_label_only_for_tier1_rows(self):
        rows = {"none": [{"path": "a.py", "tier": 0, "reason": "r"}],
                "tier2": [{"path": "uv.lock", "tier": 2, "reason": "r"}],
                "tier1": [{"path": ".github/workflows/ci.yml", "tier": 1, "reason": "r"}]}
        expected = {"none": (None, None), "tier2": ("changes what the checks run on", None),
                    "tier1": (None, "changes CI controls")}
        for name, case in rows.items():
            with tempfile.TemporaryDirectory() as folder, contextlib.chdir(folder),                  patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}):
                report = policy.findings_report("dev", "a" * 40, "b" * 40, "c" * 40, case, "d" * 40)
                written = json.loads(Path(".run/policy/findings.json").read_text(encoding="utf-8"))
            self.assertEqual((report["label_request"], report["control_label_request"]), expected[name])
            self.assertEqual(written["control_label_request"], expected[name][1])

from hardening_profiles import apply_profile
apply_profile(globals(),{'test_auditor_quarantine_hook_attack_is_tier1': ['content_classifier'], 'test_tier2_lock_dependency_and_imported_product': ['content_classifier'], 'test_all_named_hooks_are_content_protected': ['content_classifier'], 'test_plugins_conftest_and_registration': ['content_classifier'], 'test_config_sections_and_scripts': ['content_classifier'], 'test_computed_conftest_attack_refused': ['content_classifier'], 'test_plugin_mechanisms_and_aliases': ['content_classifier'], 'test_conftest_reporter_assignment_refused_other_support_flagged': ['content_classifier'], 'test_canaries_exact_outcomes_and_node_set': ['canaries'], 'test_same_pr_node_ledger_structural_addition_is_flagged_tier2': ['node_census']})

if __name__ == "__main__":
    unittest.main()
