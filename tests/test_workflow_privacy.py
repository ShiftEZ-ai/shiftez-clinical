"""Pipeline regression: environments may disclose names, never destination URLs."""
from pathlib import Path
import sys
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
        self.assertEqual(checkout['ref'],'refs/heads/dev')
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


if __name__ == "__main__":
    unittest.main()
