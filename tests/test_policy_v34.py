"""Public v3.4 regressions for original named artifacts and private agent controls."""
import hashlib
import json
import os
import re
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
import yaml
from unittest.mock import patch
HELPERS=Path(__file__).resolve().parents[1]/'.github/scripts'
if not HELPERS.is_dir():HELPERS=Path(__file__).resolve().parent
sys.path.insert(0,str(HELPERS))
if not (HELPERS/'hosted_ci.py').is_file():sys.path.insert(0,str(HELPERS.parents[1]/'workflows-next/.github/scripts'))
import cut_root_measurements as importer
import working_policy as policy
import nightly_budget

class ImporterV34Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=os.environ['RUNNER_TEMP']);self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)
        self.plan={'schema':1,'sha':'a'*40,'tree':'b'*40,'replicas':[1,2],'smoke':False}
        self.raw=json.dumps(self.plan).encode()
    def layout(self,corrupt=None):
        artifacts=[]
        for index in range(87):
            path=self.folder/(str(index+1)+'.zip')
            with zipfile.ZipFile(path,'w') as archive:
                archive.writestr('shard-plan.json',self.raw if index!=corrupt else self.raw+b'\n')
                if index:archive.writestr('shard-receipt.json','{}')
            sha=hashlib.sha256(path.read_bytes()).hexdigest()
            artifacts.append({'id':index+1,'name':'ci-plan-1' if index==0 else 'ci-test-1-'+str(index),'path':str(path),
                              'sha256':sha,'github_digest':'sha256:'+sha})
        capture={'run':{'run_attempt':1},'artifacts':artifacts}
        reports=self.folder/'unpacked'
        importer.unpack(capture,reports)
        return capture,reports
    def test_87_equal_original_plan_copies_pass(self):
        capture,reports=self.layout()
        self.assertEqual(len(list(reports.rglob('shard-plan.json'))),87)
        self.assertEqual(importer.load_plan(capture,reports),self.plan)
    def test_one_differing_shard_copy_is_refused(self):
        capture,reports=self.layout(corrupt=86)
        with self.assertRaisesRegex(ValueError,'differs'):importer.load_plan(capture,reports)
    def test_plan_is_selected_by_artifact_name_not_first_file(self):
        capture,reports=self.layout()
        capture['artifacts'][0]['name']='wrong-name'
        with self.assertRaisesRegex(ValueError,'named original plan'):importer.load_plan(capture,reports)
    def test_duplicate_named_plan_artifact_is_refused(self):
        capture,reports=self.layout();capture['artifacts'][1]['name']='ci-plan-1'
        with self.assertRaisesRegex(ValueError,'named original plan'):importer.load_plan(capture,reports)
    def test_added_mcp_configuration_is_hard_refusal_at_every_depth_case(self):
        for name in ('.mcp.json','nested/.MCP.JSON'):
            with self.subTest(name=name),self.assertRaisesRegex(ValueError,'private agent'):
                policy.tier(name,None,b'{}',{'helpers':set(),'product_imports':set()})
        with self.assertRaisesRegex(ValueError,'exclusions'):
            policy.tier('.dockerignore',b'',b'.mcp.json',{'helpers':set(),'product_imports':set()})
    def test_trusted_guard_checks_unchanged_tracked_ignore_files_too(self):
        with patch.object(policy,'git',return_value=b'unchanged/.dockerignore\0nested/toolignore\0'), \
             patch.object(policy,'read_file',side_effect=lambda rev,path:b'.mcp.json' if path.endswith('toolignore') else b'build/'):
            with self.assertRaisesRegex(ValueError,'nested/toolignore.*exclusions'):policy.assert_public_tree('a'*40)
    def test_initial_unstable_expiry_is_dispatch_utc_day_plus_30(self):
        capture={'run':{'created_at':'2026-11-17T00:30:00Z'}}
        importer.require_dispatch_expiry(capture,{'unstable_expires_on':'2026-12-17'})
        for wrong in ('2026-12-16','2026-12-18'):
            with self.assertRaisesRegex(ValueError,'plus 30'):importer.require_dispatch_expiry(capture,{'unstable_expires_on':wrong})
    def test_expiry_extension_changes_nothing_except_existing_date(self):
        old={'schema':3,'fails':[],'unstable':[{'node':'tests/test_a.py::test_a','class':'unstable','expires_on':'2026-12-17'}]}
        new=json.loads(json.dumps(old));new['unstable'][0]['expires_on']='2027-12-17'
        self.assertTrue(policy.renewal_only(json.dumps(old).encode(),json.dumps(new).encode()))
        for field,value in (('node','tests/test_b.py::test_b'),('class','baseline')):
            changed=json.loads(json.dumps(new));changed['unstable'][0][field]=value
            self.assertFalse(policy.renewal_only(json.dumps(old).encode(),json.dumps(changed).encode()))

    def test_nightly_requires_19_free_jobs(self):
        for running, admitted in ((0, True), (461, True), (462, False), (480, False)):
            with self.subTest(running=running):
                self.assertEqual(nightly_budget.can_start(running), admitted)

    def test_ci_jobs_run_by_event_with_skipped_budget(self):
        workflows = HELPERS if (HELPERS / 'ci.yml').is_file() else HELPERS.parent / 'workflows'
        jobs = yaml.load((workflows / 'ci.yml').read_text(), Loader=yaml.BaseLoader)['jobs']
        full = {'checks', 'static', 'unit', 'browser', 'result'}
        cases = (
            ('pull request', 'pull_request', 'refs/pull/1/merge', True, {}, full),
            ('merge group', 'merge_group', 'refs/heads/queue', True, {}, full),
            ('preview push', 'push', 'refs/heads/preview', True, {}, full),
            ('dev push', 'push', 'refs/heads/dev', True, {}, {'checks'}),
            ('dispatch', 'workflow_dispatch', 'refs/heads/dev', True, {}, full),
            ('admitted nightly', 'schedule', 'refs/heads/dev', True, {}, full | {'nightly-budget'}),
            ('declined nightly', 'schedule', 'refs/heads/dev', False, {}, {'nightly-budget'}),
            ('failed budget', 'schedule', 'refs/heads/dev', True,
             {'nightly-budget': 'failure'}, {'nightly-budget'}),
            ('failed checks', 'pull_request', 'refs/pull/1/merge', True,
             {'checks': 'failure'}, {'checks', 'result'}),
        )
        for label, event, ref, admitted, overrides, expected in cases:
            with self.subTest(case=label):
                results, ancestors = {}, {}

                def run(job):
                    if job in results:
                        return
                    needs = jobs[job].get('needs', [])
                    if isinstance(needs, str):
                        needs = [needs]
                    for dependency in needs:
                        run(dependency)
                    ancestors[job] = set(needs).union(*(ancestors[item] for item in needs))
                    condition = jobs[job]['if']
                    # GitHub's implicit success check includes skipped jobs earlier in the dependency chain.
                    implicit_success = not re.search(r'\b(?:always|cancelled|success|failure)\(\)', condition)
                    if implicit_success and any(results[item] != 'success' for item in ancestors[job]):
                        results[job] = 'skipped'
                        return

                    values = {
                        "github.event_name == 'schedule'": event == 'schedule',
                        "github.event_name != 'schedule'": event != 'schedule',
                        "github.event_name != 'push'": event != 'push',
                        "github.ref != 'refs/heads/dev'": ref != 'refs/heads/dev',
                        "vars.HOSTED_CI_ENABLED == 'true'": True,
                        "github.event.repository.private": False,
                        "always()": True,
                        "cancelled()": False,
                        "needs['nightly-budget'].result == 'success'": results.get('nightly-budget') == 'success',
                        "needs['nightly-budget'].outputs.admitted == 'true'": admitted,
                        "needs.checks.result == 'success'": results.get('checks') == 'success',
                        "needs.checks.result != 'skipped'": results.get('checks') != 'skipped',
                    }
                    for expression, value in values.items():
                        condition = condition.replace(expression, str(value))
                    condition = condition.replace('&&', ' and ').replace('||', ' or ').replace('!', ' not ')
                    # Evaluate only the known boolean grammar, with no access to builtins.
                    self.assertRegex(condition, r'^(?:True|False|and|or|not|[()\s])+$')
                    enabled = eval(condition.strip(), {'__builtins__': {}})
                    results[job] = overrides.get(job, 'success') if enabled else 'skipped'

                for job in jobs:
                    run(job)
                self.assertEqual({name for name, result in results.items() if result != 'skipped'}, expected)

    def test_pipeline_keeps_default_limits_until_measurement(self):
        # Lower limits require new measured runs in ci/hosted-durations.json.
        durations = Path(__file__).resolve().parents[1] / 'ci/hosted-durations.json'
        source_runs = json.loads(durations.read_text(encoding='utf-8'))['source_run_ids']
        self.assertCountEqual(source_runs, [44114266718, 44131154009])

        config = durations.with_name('bootstrap-settings.json')
        groups = json.loads(config.read_text(encoding='utf-8'))['merge_group_shards']
        self.assertEqual(groups, {'unit_shards': 25, 'browser_shards': 30})

        # Only the external packet copy has these sibling YAMLs; public layout resolves workflows from root.
        root=HELPERS if (HELPERS/'ci.yml').is_file() else HELPERS.parent/'workflows'
        for name in ('ci.yml','ci-tests.yml','working-policy.yml'):
            doc=yaml.load((root/name).read_text(),Loader=yaml.BaseLoader)
            jobs = doc['jobs']
            if name == 'ci.yml':
                self.assertEqual(doc['concurrency']['cancel-in-progress'], "${{ github.event_name == 'pull_request' }}")
                self.assertIn("github.event_name == 'schedule' && 'ci-nightly'", doc['concurrency']['group'])
                self.assertEqual(jobs['nightly-budget']['permissions'], {'actions': 'read', 'contents': 'read'})
                self.assertEqual(jobs['checks']['needs'], 'nightly-budget')
                limits = {
                    'unit': "${{ github.event_name == 'schedule' && 8 || (github.event_name == 'merge_group' || github.event_name == 'pull_request') && 256 || 10 }}",
                    'browser': "${{ github.event_name == 'schedule' && 10 || (github.event_name == 'merge_group' || github.event_name == 'pull_request') && 256 || 8 }}",
                }
                for shard, limit in limits.items():
                    self.assertEqual(jobs[shard].get('uses'), './.github/workflows/ci-tests.yml')
                    self.assertEqual(jobs[shard]['strategy'].get('max-parallel'), limit)
            for job_id, job in jobs.items():
                with self.subTest(workflow=name, job=job_id):
                    if name == 'ci.yml' and job_id in ('unit', 'browser'):
                        self.assertNotIn('timeout-minutes', job)
                    else:
                        expected = '60' if name == 'ci-tests.yml' else '10'
                        self.assertEqual(job.get('timeout-minutes'), expected)
                    for step in job.get('steps', []):
                        self.assertNotIn('timeout-minutes', step)
        self.assertNotIn('--timeout=',(HELPERS/'trusted_ci.py').read_text())

if __name__=='__main__':unittest.main()
