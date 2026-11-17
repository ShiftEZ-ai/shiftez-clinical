"""Pipeline regression: environments may disclose names, never destination URLs."""
from pathlib import Path
import sys
import unittest
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
