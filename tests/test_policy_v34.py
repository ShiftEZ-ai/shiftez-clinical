"""Public v3.4 regressions for original named artifacts and private agent controls."""
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
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
    def test_expiry_extension_is_refused_even_without_other_changes(self):
        old={'schema':3,'fails':[],'unstable':[{'node':'tests/test_a.py::test_a','class':'unstable','expires_on':'2026-12-17'}]}
        new=json.loads(json.dumps(old));new['unstable'][0]['expires_on']='2027-12-17'
        self.assertFalse(policy.removal_only(json.dumps(old).encode(),json.dumps(new).encode()))
        for field,value in (('node','tests/test_b.py::test_b'),('class','baseline')):
            changed=json.loads(json.dumps(new));changed['unstable'][0][field]=value
            self.assertFalse(policy.removal_only(json.dumps(old).encode(),json.dumps(changed).encode()))

    def test_nightly_requires_19_free_jobs(self):
        for running, admitted in ((0, True), (461, True), (462, False), (480, False)):
            with self.subTest(running=running):
                self.assertEqual(nightly_budget.can_start(running), admitted)

    def test_ci_jobs_run_by_event_with_skipped_budget(self):
        workflows = HELPERS if (HELPERS / 'ci.yml').is_file() else HELPERS.parent / 'workflows'
        jobs = yaml.load((workflows / 'ci.yml').read_text(), Loader=yaml.BaseLoader)['jobs']
        full = {'checks', 'static', 'unit', 'browser', 'ci-gate'}
        cases = (
            ('pull request', 'pull_request', 'refs/pull/1/merge', True, {}, full),
            ('merge group', 'merge_group', 'refs/heads/queue', True, {}, full),
            ('preview push', 'push', 'refs/heads/preview', True, {}, full),
            ('release branch push', 'push', 'refs/heads/release/v1.2.3', True, {}, full),
            ('default branch push', 'push', 'refs/heads/dev', True, {}, full),
            ('dispatch', 'workflow_dispatch', 'refs/heads/dev', True, {}, full),
            ('admitted nightly', 'schedule', 'refs/heads/dev', True, {}, full | {'nightly-budget'}),
            ('declined nightly', 'schedule', 'refs/heads/dev', False, {}, {'nightly-budget', 'ci-gate'}),
            ('failed budget', 'schedule', 'refs/heads/dev', True,
             {'nightly-budget': 'failure'}, {'nightly-budget', 'ci-gate'}),
            ('failed checks', 'pull_request', 'refs/pull/1/merge', True,
             {'checks': 'failure'}, {'checks', 'ci-gate'}),
        )
        for default, (label, event, ref, admitted, overrides, expected) in (
                (default, case) for default in ('dev', 'main') for case in cases):
            ref = ref.replace('refs/heads/dev', 'refs/heads/' + default)
            with self.subTest(case=label, default=default):
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
                        "vars.HOSTED_CI_ENABLED == 'true'": True,
                        "github.event.repository.private": False,
                        "always()": True,
                        "cancelled()": False,
                        "needs['nightly-budget'].result == 'success'": results.get('nightly-budget') == 'success',
                        "needs['nightly-budget'].outputs.admitted == 'true'": admitted,
                        "needs.checks.result == 'success'": results.get('checks') == 'success',
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

    def test_concurrency_cancels_only_superseded_pull_requests(self):
        workflows = HELPERS if (HELPERS / 'ci.yml').is_file() else HELPERS.parent / 'workflows'
        pr_only = "${{ github.event_name == 'pull_request' }}"
        expected = {
            'ci.yml': {'group': 'ci-${{ github.head_ref || github.run_id }}', 'cancel-in-progress': pr_only},
            'working-policy.yml': {
                'group': 'working-policy-${{ github.head_ref || github.run_id }}', 'cancel-in-progress': pr_only,
            },
            'image.yml': {'group': 'image-${{ github.ref }}-${{ github.sha }}', 'cancel-in-progress': 'false'},
            'dependency-graph.yml': {
                'group': 'dependency-graph-${{ github.ref }}', 'cancel-in-progress': 'false',
            },
        }
        files = sorted(workflows.glob('*.yml')) + sorted(workflows.glob('*.yaml'))
        self.assertTrue(expected.keys() <= {path.name for path in files})
        for path in files:
            with self.subTest(workflow=path.name):
                doc = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
                config = doc.get('concurrency')
                if path.name in expected:
                    self.assertEqual(config, expected[path.name])
                elif isinstance(config, dict):
                    self.assertEqual(config.get('cancel-in-progress', 'false'), 'false')
                for job in doc['jobs'].values():
                    config = job.get('concurrency')
                    if isinstance(config, dict):
                        self.assertIn(config.get('cancel-in-progress', 'false'), ('false', pr_only))

    def test_ci_gate_runs_unconditionally_and_checks_actual_producer_results(self):
        workflows = HELPERS if (HELPERS / 'ci.yml').is_file() else HELPERS.parent / 'workflows'
        gate = yaml.load((workflows / 'ci.yml').read_text(), Loader=yaml.BaseLoader)['jobs']['ci-gate']
        self.assertEqual(gate['name'], 'ci-gate')
        self.assertEqual(gate['if'], 'always()')
        producers = ('checks', 'static', 'unit', 'browser')
        self.assertCountEqual(gate['needs'], ('nightly-budget', *producers))
        step = next(step for step in gate['steps'] if step.get('id') == 'producers')
        self.assertNotIn('if', step)
        self.assertEqual(step['shell'], 'python')
        self.assertEqual(step['env']['NEEDS_JSON'], '${{ toJSON(needs) }}')
        for following in gate['steps'][gate['steps'].index(step) + 1:]:
            self.assertIn("steps.producers.outputs.run_verdict == 'true'", following['if'])
        success = dict.fromkeys(producers, 'success')
        skipped = dict.fromkeys(producers, 'skipped')
        cases = (
            ('pull_request', 'skipped', None, success, True, False),
            ('merge_group', 'skipped', None, success, True, False),
            ('push', 'skipped', None, success, True, False),
            ('workflow_dispatch', 'skipped', None, success, True, False),
            ('schedule', 'success', 'true', success, True, False),
            ('schedule', 'success', 'false', skipped, False, True),
            ('schedule', 'success', 'true', skipped, False, False),
            ('schedule', 'success', None, skipped, False, False),
            ('schedule', 'success', 'false', dict(skipped, browser='cancelled'), False, False),
            ('schedule', 'failure', None, skipped, False, False),
            ('schedule', 'cancelled', None, skipped, False, False),
            ('schedule', 'skipped', None, skipped, False, False),
            ('push', 'skipped', None, skipped, False, False),
            ('pull_request', 'skipped', None, dict(success, unit='failure'), False, False),
            ('merge_group', 'skipped', None, dict(success, browser='cancelled'), False, False),
            ('push', 'skipped', None, dict(success, static='skipped'), False, False),
            ('push', 'skipped', None, dict(success, unit='failure'), False, False),
            ('push', 'skipped', None, {'checks': 'success'}, False, False),
        )
        for event, budget, admitted, results, verdict, deferred in cases:
            with self.subTest(event=event, budget=budget, admitted=admitted, results=results), \
                 tempfile.TemporaryDirectory(dir=os.environ['RUNNER_TEMP']) as directory:
                needs = {job: {'result': result} for job, result in results.items()}
                budget_outputs = {} if admitted is None else {'admitted': admitted}
                all_needs = dict(needs, **{'nightly-budget': {'result': budget, 'outputs': budget_outputs}})
                env, output, summary = (Path(directory) / name for name in ('env', 'output', 'summary'))
                result = subprocess.run(
                    [sys.executable, '-I', '-c', step['run']],
                    env={**os.environ, 'GITHUB_EVENT_NAME': event, 'NEEDS_JSON': json.dumps(all_needs),
                         'GITHUB_ENV': str(env), 'GITHUB_OUTPUT': str(output), 'GITHUB_STEP_SUMMARY': str(summary)},
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode == 0, verdict or deferred, result.stderr)
                self.assertEqual(env.exists(), verdict)
                self.assertEqual(output.exists(), verdict)
                self.assertEqual(summary.exists(), deferred)
                if verdict:
                    self.assertEqual(output.read_text().splitlines(), ['run_verdict=true'])
                    self.assertEqual(json.loads(env.read_text().removeprefix('NEEDS_JSON=')), needs)
                if deferred:
                    self.assertIn('Nightly suites deferred', summary.read_text())

    def test_shard_workflow_keeps_browser_diagnostics_separate(self):
        workflow = HELPERS.parent / "workflows/ci-tests.yml"
        job = yaml.load(workflow.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)["jobs"]["tests"]
        env = job["env"]
        self.assertEqual(env["PW_TRACE_MODE"], "off")
        self.assertNotEqual(env["PW_TRACE_DIR"], env["PW_SCREENSHOT_DIR"])
        uploads = [step for step in job["steps"] if step.get("uses", "").startswith("actions/upload-artifact@")]
        self.assertEqual(len(uploads), 2)
        for upload in uploads:
            paths = upload["with"]["path"].splitlines()
            for key in ("PW_TRACE_DIR", "PW_SCREENSHOT_DIR"):
                self.assertIn(f"candidate/{env[key]}/", paths)
        checkout = next(step for step in job["steps"] if step.get("with", {}).get("path") == ".trusted-controls")
        self.assertEqual(checkout["with"]["ref"], "${{ inputs.trusted_sha }}")
        run = next(step["run"] for step in job["steps"] if step.get("name") == "Run the exact planned shard once")
        self.assertIn('python -I "$GITHUB_WORKSPACE/.trusted-controls/.github/scripts/trusted_ci.py" run', run)

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
            if name == 'working-policy.yml':
                self.assertEqual(doc['name'], 'Working policy')
                self.assertEqual(jobs['policy']['name'], 'Working policy')
            if name == 'ci-tests.yml':
                setup = next(step for step in jobs['tests']['steps']
                             if step.get('name') == 'Check Postgres and retain runner/setup evidence')
                self.assertIn(
                    '( while :; do date +%s; cat /proc/loadavg; head -1 /proc/stat; '
                    'cat /proc/pressure/cpu /proc/pressure/io /proc/pressure/memory 2>/dev/null; '
                    'sleep 15; done > .run/reports/cpu-samples.txt 2>&1 & )',
                    setup['run'].splitlines(),
                )
            if name == 'ci.yml':
                # A label added by a person must not start a new run and cancel the running one.
                self.assertEqual(doc['on']['pull_request']['types'],
                                 ['opened', 'synchronize', 'reopened', 'ready_for_review'])
                self.assertEqual(jobs['nightly-budget']['permissions'], {'actions': 'read', 'contents': 'read'})
                self.assertEqual(jobs['checks']['needs'], 'nightly-budget')
                plan = next(step for step in jobs['checks']['steps'] if step.get('id') == 'plan')
                self.assertIn('.trusted-controls/.github/scripts/trusted_ci.py" plan', plan['run'])
                limits = {
                    'unit': "${{ github.event_name == 'schedule' && 8 || (github.event_name == 'merge_group' || github.event_name == 'pull_request') && 256 || 10 }}",
                    'browser': "${{ github.event_name == 'schedule' && 10 || (github.event_name == 'merge_group' || github.event_name == 'pull_request') && 256 || 8 }}",
                }
                for shard, limit in limits.items():
                    self.assertEqual(jobs[shard].get('uses'), './.github/workflows/ci-tests.yml')
                    self.assertEqual(jobs[shard]['strategy'].get('max-parallel'), limit)
                    count = shard + '_count'
                    self.assertEqual(jobs['checks']['outputs'][count],
                                     '${{ steps.plan.outputs.' + count + ' || steps.select.outputs.' + count + ' }}')
                    self.assertEqual(jobs['checks']['outputs'][shard + '_matrix'],
                                     '${{ steps.plan.outputs.' + shard + '_matrix }}')
                    self.assertEqual(jobs[shard]['strategy']['matrix']['shard'],
                                     '${{ fromJSON(needs.checks.outputs.' + shard + '_matrix) }}')
                    self.assertEqual(jobs['checks']['outputs']['replicas'], '${{ steps.plan.outputs.replicas }}')
                    self.assertEqual(jobs[shard]['strategy']['matrix']['replica'],
                                     '${{ fromJSON(needs.checks.outputs.replicas) }}')
            for job_id, job in jobs.items():
                with self.subTest(workflow=name, job=job_id):
                    if name == 'ci.yml' and job_id in ('unit', 'browser'):
                        self.assertNotIn('timeout-minutes', job)
                    else:
                        expected = '60' if name == 'ci-tests.yml' else '10'
                        self.assertEqual(job.get('timeout-minutes'), expected)
                    for step in job.get('steps', []):
                        self.assertNotIn('timeout-minutes', step)
        trusted_ci = (HELPERS/'trusted_ci.py').read_text()
        for option in ('--timeout=600', '--timeout-method=signal', 'faulthandler_timeout=540'):
            self.assertIn(option, trusted_ci)

if __name__=='__main__':unittest.main()
