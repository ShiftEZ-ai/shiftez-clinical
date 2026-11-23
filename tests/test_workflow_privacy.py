"""Pipeline regression: environments may disclose names, never destination URLs."""
from io import BytesIO
import json
from pathlib import Path
import sys
import tarfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import yaml

HELPERS = Path(__file__).resolve().parents[1] / ".github/scripts"
if not HELPERS.is_dir():
    HELPERS = Path(__file__).resolve().parent
sys.path.insert(0, str(HELPERS))
import workflow_privacy


class WorkflowPrivacyTests(unittest.TestCase):
    def test_pr1_workflows_use_read_only_guard_events(self):
        root=HELPERS if (HELPERS/'ci.yml').is_file() else HELPERS.parent/'workflows'
        for name in ('ci.yml','ci-tests.yml','working-policy.yml'):
            text=(root/name).read_text()
            self.assertNotIn('pull_request_target',text)
        guard=yaml.load((root/'working-policy.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(set(guard['on']),{'pull_request','merge_group'})
        self.assertEqual(guard['permissions'],{'contents':'read'})
        checkout=next(step['with'] for step in guard['jobs']['policy']['steps'] if step.get('uses','').startswith('actions/checkout@'))
        self.assertEqual(checkout['ref'],'refs/heads/${{ github.event.repository.default_branch }}')
        self.assertEqual(checkout['persist-credentials'],'false')

    def test_all_workflows_match_privacy_policy(self):
        root = HELPERS if (HELPERS / "ci.yml").is_file() else HELPERS.parent / "workflows"
        for path in sorted([*root.glob("*.yml"), *root.glob("*.yaml")]):
            with self.subTest(workflow=path.name):
                workflow_privacy.check(path.read_text(encoding="utf-8"))

    def test_only_reviewed_trigger_names_are_accepted_in_each_form(self):
        names = ["push", "pull_request", "merge_group", "schedule", "workflow_dispatch", "workflow_call"]
        for name in names:
            with self.subTest(name=name):
                workflow_privacy.check(f"on: {name}")
        workflow_privacy.check(yaml.safe_dump({"on": names}))
        workflow_privacy.check(yaml.safe_dump({"on": dict.fromkeys(names)}))
        with patch.object(workflow_privacy, "load", return_value={True: dict.fromkeys(names)}):
            workflow_privacy.check("")

    def test_other_trigger_names_are_refused_in_each_form(self):
        for name in ("issues", "discussion_comment", "repository_dispatch", "release", "future_event"):
            for text in (f"on: {name}", f"on: [push, {name}]", f'"on": {{{name}: null}}'):
                with self.subTest(text=text), self.assertRaisesRegex(ValueError, name + " is forbidden"):
                    workflow_privacy.check(text)
            with patch.object(workflow_privacy, "load", return_value={True: [name]}):
                with self.assertRaisesRegex(ValueError, name + " is forbidden"):
                    workflow_privacy.check("")

    def test_nested_trigger_list_entry_is_refused(self):
        with self.assertRaisesRegex(ValueError, "Workflow trigger must be a name"):
            workflow_privacy.check("on: [push, [workflow_call]]")

    def test_pull_request_target_is_refused_in_each_trigger_form(self):
        for text in ("on: pull_request_target", "on: [push, pull_request_target]",
                     '"on": {pull_request_target: {types: [opened]}}'):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "pull_request_target is forbidden"):
                workflow_privacy.check(text)
        workflow_privacy.check("on: [push, pull_request, merge_group]")

    def test_workflow_run_and_issue_comment_are_refused_in_each_trigger_form(self):
        for trigger in ("workflow_run", "issue_comment"):
            for text in (f"on: {trigger}", f"on: [push, {trigger}]", f'"on": {{{trigger}: null}}',
                         f"true: [{trigger}]"):
                with self.subTest(text=text), self.assertRaisesRegex(ValueError, trigger + " is forbidden"):
                    workflow_privacy.check(text)
            with patch.object(workflow_privacy, "load", return_value={True: {trigger: None}}):
                with self.assertRaisesRegex(ValueError, trigger + " is forbidden"):
                    workflow_privacy.check("")

    def test_local_and_pinned_actions_are_allowed(self):
        for action in ("./.github/actions/check", "actions/checkout@" + "a" * 40, "actions/cache/restore@" + "b" * 40):
            with self.subTest(action=action):
                workflow_privacy.check(yaml.safe_dump({"jobs": {"checks": {
                    "runs-on": "ubuntu-24.04", "steps": [workflow_privacy.HOSTED_STEP, {"uses": action}]
                }}}))
        workflow_privacy.check("jobs: {test: {uses: ./.github/workflows/ci-tests.yml}}")

    def test_local_uses_cannot_escape_actions_or_call_workflows_as_steps(self):
        for action in ("./actions/check", "./.github/actions/../check", "./.github/actions//check",
                       "./.github/actions/", "./.github/workflows/ci-tests.yml"):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "approved local path"):
                workflow_privacy.check(yaml.safe_dump({"jobs": {"checks": {
                    "runs-on": "ubuntu-24.04", "steps": [workflow_privacy.HOSTED_STEP, {"uses": action}]
                }}}))
        with self.assertRaisesRegex(ValueError, "approved local path"):
            workflow_privacy.check("jobs: {test: {uses: ./.github/workflows/unreviewed.yml}}")

    def test_unpinned_or_invalid_actions_are_refused(self):
        for action in ("actions/checkout@v4", "actions/cache/restore@main", "actions/checkout@" + "a" * 39,
                       "actions/checkout@" + "z" * 40, "docker://example", "./"):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "full commit SHA"):
                workflow_privacy.check(yaml.safe_dump({"jobs": {"checks": {
                    "runs-on": "ubuntu-24.04", "steps": [workflow_privacy.HOSTED_STEP, {"uses": action}]
                }}}))
        with self.assertRaisesRegex(ValueError, "full commit SHA"):
            workflow_privacy.check("jobs: {test: {uses: owner/repo@main}}")

    def test_step_and_default_shells_are_named_shells_only(self):
        hosted = workflow_privacy.HOSTED_STEP
        for shell in ("bash", "sh", "pwsh", "python"):
            with self.subTest(shell=shell):
                workflow_privacy.check(yaml.safe_dump({"jobs": {"checks": {"runs-on": "ubuntu-24.04",
                    "steps": [hosted, {"run": "true", "shell": shell}]}}}))
        for shell in ("./tools/shell", "/usr/bin/bash", "bash {0}", "perl {0}", "${{ vars.SHELL }}"):
            for document in ({"jobs": {"checks": {"runs-on": "ubuntu-24.04", "steps": [hosted, {"run": "true", "shell": shell}]}}},
                             {"defaults": {"run": {"shell": shell}}, "jobs": {"checks": {"runs-on": "ubuntu-24.04", "steps": [hosted]}}}):
                with self.subTest(shell=shell), self.assertRaisesRegex(ValueError, "Workflow shell must be one of"):
                    workflow_privacy.check(yaml.safe_dump(document))

    def test_hosted_label_allowed(self):
        workflow_privacy.check(yaml.safe_dump({"jobs":{"checks":{"runs-on":"ubuntu-24.04","steps":[workflow_privacy.HOSTED_STEP]}}}))

    def test_quoted_or_inline_runner_keys_cannot_hide_nonhosted_label(self):
        workflow_privacy.check(yaml.safe_dump({'jobs':{'checks':{'runs-on':'ubuntu-24.04','steps':[workflow_privacy.HOSTED_STEP]}}}))
        for text in ('"runs-on": self-hosted',"'runs-on': [self-hosted, linux]",'{"runs-on":"self-hosted"}'):
            with self.subTest(text=text),self.assertRaises(ValueError):workflow_privacy.check(text)

    def test_self_hosted_or_unreviewed_runner_is_refused(self):
        for label in ('self-hosted','[self-hosted, linux]','${{ vars.RUNNER }}','{group: private}'):
            with self.subTest(label=label),self.assertRaises(ValueError):workflow_privacy.check("runs-on: "+label)
    def test_name_only_environment_allowed(self):
        workflow_privacy.check(yaml.safe_dump({"jobs":{"publish":{"runs-on":"ubuntu-24.04","environment":{"name":"production"},"steps":[workflow_privacy.HOSTED_STEP]}}}))

    def test_inline_and_block_urls_refused(self):
        for value in ("environment: {name: dev, url: ${{ inputs.address }}}", "environment:\n  name: dev\n  url: https://example.invalid",
                      'environment: {"name": dev, "url": https://example.invalid}', "env:\n  X: ${{ environment.url }}"):
            with self.subTest(value=value), self.assertRaises(ValueError): workflow_privacy.check(value)

    def test_status_url_payloads_refused(self):
        for value in ("gh api statuses -f target_url=https://example.invalid", "gh api deployments/statuses --input body.json # environment_url",
                      'body={"environment_url":"https://example.invalid"}'):
            with self.subTest(value=value), self.assertRaises(ValueError): workflow_privacy.check(value)


class DefaultBranchTests(unittest.TestCase):
    REPO = Path(__file__).resolve().parents[1]
    TRIGGER = ["    branches:", "    # shortcut: dev is removed after the default branch is renamed to main.",
               "    - dev", "    - main"]

    def test_no_workflow_or_trusted_script_hard_codes_the_integration_branch(self):
        paths = [*sorted((self.REPO / ".github/workflows").glob("*.y*ml")), *sorted((self.REPO / ".github/scripts").glob("*.py")),
                 *sorted((self.REPO / "ci").glob("*.py")), self.REPO / "scripts/affected_tests.py"]
        self.assertGreater(len(paths), 10)
        offenders, triggers = [], 0
        for path in paths:
            lines = path.read_text(encoding="utf-8").splitlines()
            for number, line in enumerate(lines):
                where = path.relative_to(self.REPO).as_posix() + ":" + str(number + 1)
                if "refs/heads/dev" in line or "branch=dev" in line or "queue/dev/" in line:
                    offenders.append(where)
                if line.strip() == "- dev":
                    triggers += 1
                    if lines[number - 2:number + 2] != self.TRIGGER:
                        offenders.append(where)
        self.assertEqual(offenders, [])
        self.assertEqual(triggers, 3)
        for name in ("ci.yml", "dependency-graph.yml", "image.yml"):
            doc = yaml.load((self.REPO / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)
            self.assertEqual(doc["on"]["push"]["branches"], ["dev", "main"])

    def test_default_branch_image_keeps_the_dev_tag_names(self):
        import os, shutil, subprocess
        doc = yaml.load((self.REPO / ".github/workflows/image.yml").read_text(), Loader=yaml.BaseLoader)
        script = next(step["run"] for step in doc["jobs"]["publish"]["steps"] if step.get("id") == "push")
        block = script[script.index("case "):script.index("esac") + 4]
        bash = 'C:/Program Files/Git/bin/bash.exe' if os.name == 'nt' else shutil.which('bash')
        commit = "0123456789ab" + "c" * 28
        for default in ("dev", "main", ""):
            other = "main" if default == "dev" else "dev"
            for ref_type, ref_name, expected in (
                    ("branch", default, "--tag img:dev --tag img:dev-0123456789ab" if default else None),
                    ("tag", "v1.2.3", "--tag img:v1.2.3"), ("branch", other, None), ("branch", "feature", None)):
                with self.subTest(default=default, ref=ref_type + "/" + ref_name):
                    env = {**os.environ, "REF_TYPE": ref_type, "REF_NAME": ref_name, "COMMIT": commit,
                           "DEFAULT_BRANCH": default}
                    result = subprocess.run([bash, "-c", 'set -euo pipefail; image=img\n' + block + '\necho "${tags[@]}"'],
                                            env=env, capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.stdout.strip() if result.returncode == 0 else None, expected, result.stderr)

    def test_public_image_requires_main_and_a_passing_scan_before_push_and_attestation(self):
        doc = yaml.load((self.REPO / ".github/workflows/image.yml").read_text(), Loader=yaml.BaseLoader)
        jobs = doc["jobs"]
        self.assertEqual(set(doc["on"]), {"push"})
        self.assertEqual(doc["permissions"], {"contents": "read"})
        self.assertEqual(jobs["publish"]["permissions"], {
            "contents": "read", "packages": "write", "id-token": "write", "attestations": "write",
        })
        for name, job in jobs.items():
            if name != "publish":
                permissions = job.get("permissions", {})
                self.assertTrue(permissions == "read-all" or
                                (isinstance(permissions, dict) and set(permissions.values()) <= {"read"}))

        private_push, scanner, scan, login, push, attest = jobs["publish"]["steps"][-6:]
        self.assertEqual(private_push["id"], "push")
        self.assertNotIn("if", private_push)
        main_push = "github.event_name == 'push' && github.ref == 'refs/heads/main'"
        self.assertEqual(scanner["if"], main_push)
        self.assertEqual(scanner["working-directory"], "scripts")
        when_present, _, when_absent = scanner["run"].partition("else\n")
        self.assertIn("if [[ -f privacy_check.py ]]; then", when_present)
        self.assertIn('echo \'available=true\' >> "$GITHUB_OUTPUT"', when_present)
        self.assertIn("$GITHUB_STEP_SUMMARY", when_absent)
        self.assertNotIn("$GITHUB_OUTPUT", when_absent)
        self.assertEqual(scan["if"], f"steps.{scanner['id']}.outputs.available == 'true'")
        self.assertNotIn("continue-on-error", scan)
        self.assertTrue(scan["run"].startswith("set -euo pipefail\n"))
        self.assertIn("--load --provenance=false", scan["run"])
        self.assertNotIn("--push", scan["run"])
        self.assertIn('docker image save --output "$archive" "$image:$tag"', scan["run"])
        self.assertIn('python3 scripts/scan_public_image.py "$archive"', scan["run"])
        passed = f"steps.{scan['id']}.outcome == 'success'"
        self.assertEqual(login["if"], passed)
        self.assertEqual(push["if"], passed)
        self.assertEqual(push["env"], {
            "IMAGE": "${{ steps.public_scan.outputs.image }}", "TAG": "${{ steps.public_scan.outputs.tag }}",
        })
        self.assertIn('docker push "$IMAGE:$TAG"', push["run"])
        self.assertIn('docker buildx imagetools inspect "$IMAGE@$digest"', push["run"])
        self.assertEqual(jobs["publish"]["outputs"], {
            "digest": "${{ steps.push.outputs.digest }}", "public-digest": "${{ steps.public_push.outputs.digest }}",
        })
        self.assertEqual(attest["if"], f"steps.{push['id']}.outcome == 'success'")
        self.assertRegex(attest["uses"], r"^actions/attest-build-provenance@[0-9a-f]{40}$")
        self.assertEqual(attest["with"], {
            "subject-name": "${{ steps.public_scan.outputs.image }}",
            "subject-digest": "${{ steps.public_push.outputs.digest }}",
            "push-to-registry": "true",
            "create-storage-record": "false",
        })


def test_public_image_scan_rejects_config_and_removed_layer_content(tmp_path, capsys):
    from scripts import scan_public_image

    marker = "planted value"

    def tar(files):
        data = BytesIO()
        with tarfile.open(fileobj=data, mode="w") as archive:
            for name, content in files.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(content)
                archive.addfile(entry, BytesIO(content))
        return data.getvalue()

    image = tmp_path / "image.tar"
    manifest = [{"Config": "config.json", "Layers": ["first.tar", "second.tar"]}]
    with patch.dict(sys.modules, {"privacy_check": SimpleNamespace(
            findings=lambda text: [(1, "test-pattern")] if marker in text else [])}):
        for value, expected in (("clean", 0), (marker, 1)):
            image.write_bytes(tar({
                "manifest.json": json.dumps(manifest).encode(),
                "config.json": json.dumps({"config": {"Env": [f"VALUE={value}"]}}).encode(),
                "first.tar": tar({"removed.txt": value.encode()}),
                "second.tar": tar({".wh.removed.txt": b""}),
            }))
            assert scan_public_image.main(image) == expected
            result = capsys.readouterr()
            assert marker not in result.out + result.err
            assert result.out.splitlines() == (
                ["test-pattern: 2", "Image privacy scan: 2 hits"] if expected else ["Image privacy scan: 0 hits"]
            )
        image.write_bytes(tar({"manifest.json": json.dumps(manifest).encode()}))
        assert scan_public_image.main(image) == 2


if __name__ == "__main__":
    unittest.main()
