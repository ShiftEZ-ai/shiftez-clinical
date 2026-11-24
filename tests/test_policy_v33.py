"""Public v3.3 regressions: dispatch, private paths, YAML, expiry and ledger boundaries."""
import copy
from datetime import date
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml
HELPERS=Path(__file__).resolve().parents[1]/'.github/scripts'
if not HELPERS.is_dir():HELPERS=Path(__file__).resolve().parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0,str(HELPERS))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ci'))
if not (HELPERS/'hosted_ci.py').is_file():sys.path.insert(0,str(HELPERS.parents[1]/'workflows-next/.github/scripts'))
import working_policy as policy
import agent_names as names
import trusted_ci
import workflow_privacy as privacy
import node_census
import make_known_failures as maker

class V33Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=os.environ['RUNNER_TEMP']);self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)
        self.inventory={'helpers':set(),'product_imports':set()}
        self.node='tests/test_a.py::test_old'
        self.before={'schema':2,'removed':[],'removed_nodes':[]}
        self.after={**self.before,'removed_nodes':[{'node':self.node,'owner':'test-owner','reason':'Reviewed redundant assertion'}]}
    def test_dispatch_null_absent_or_empty_inputs_works(self):
        for event in ({},{'inputs':None},{'inputs':{}}):
            path=self.folder/'event.json';path.write_text(json.dumps(event))
            with patch.dict(os.environ,{'GITHUB_EVENT_PATH':str(path),'GITHUB_SHA':'a'*40,'GITHUB_EVENT_NAME':'workflow_dispatch'}), \
                 patch.object(trusted_ci,'settings',return_value={'unit_shards':1,'browser_shards':1}), \
                 patch.object(trusted_ci.subprocess,'check_output',return_value='b'*40),patch.object(trusted_ci,'output') as output:
                trusted_ci.select()
                self.assertEqual(output.call_args.args[0]['measurement'],'false')
                self.assertEqual(output.call_args.args[0]['candidate_sha'],'a'*40)
    def test_root_measurement_dispatches_only_from_the_default_branch(self):
        path=self.folder/'event.json'
        for default in ('dev','main',None):
            path.write_text(json.dumps({'inputs':{'root_measurement':'true'},'repository':{'default_branch':default}}))
            for ref in ('refs/heads/dev','refs/heads/main'):
                with self.subTest(default=default,ref=ref), \
                     patch.dict(os.environ,{'GITHUB_EVENT_PATH':str(path),'GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_REF':ref}), \
                     patch.object(trusted_ci,'settings',return_value={'unit_shards':1,'browser_shards':1}), \
                     patch.object(trusted_ci,'pinned_root',return_value={'sha':'c'*40}), \
                     patch.object(trusted_ci.subprocess,'check_output',return_value='b'*40),patch.object(trusted_ci,'output') as output:
                    if ref=='refs/heads/'+str(default):
                        trusted_ci.select()
                        self.assertEqual(output.call_args.args[0]['candidate_sha'],'c'*40)
                    else:
                        with self.assertRaises(ValueError): trusted_ci.select()
    def test_every_private_agent_variant_is_a_guard_refusal(self):
        cases = set(names.AGENT_FILES) | set(names.AGENT_SPECIAL_PATHS)
        for directory in names.AGENT_DIRECTORIES:
            cases.update((directory, directory + '/state', directory + '/rules/local.md'))
        for prefix in names.AGENT_PREFIXES:
            cases.update((prefix, prefix + '.conf.yml', prefix + 'ignore'))
        for name in sorted(cases):
            for path in (name, 'x/' + name.upper()):
                with self.subTest(path=path),self.assertRaisesRegex(ValueError,'private agent'):
                    policy.tier(path,None,b'private',self.inventory)
    def test_only_the_root_public_agent_files_are_public(self):
        self.assertEqual(len(policy.PUBLIC_AGENT_FILES), 2)
        self.assertIn('AGENTS.md', policy.PUBLIC_AGENT_FILES)
        self.assertEqual({name.casefold() for name in policy.PUBLIC_AGENT_FILES} - names.AGENT_FILES, set())
        for path in sorted(policy.PUBLIC_AGENT_FILES):
            with self.subTest(path=path):
                self.assertFalse(policy.private_agent_path(path))
                policy.tier(path, None, b'# Rules', self.inventory)
        refused = set(names.AGENT_FILES) | {directory + '/settings.json' for directory in names.AGENT_DIRECTORIES}
        for name in policy.PUBLIC_AGENT_FILES:
            refused.update(('docs/' + name, 'x/' + name, './' + name, name.lower(), name.upper()))
        for path in sorted(refused):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'private agent'):
                policy.tier(path, None, b'private', self.inventory)
    def test_every_tracked_ignore_variant_refuses_private_patterns(self):
        patterns = set(names.AGENT_FILES) | set(names.AGENT_SPECIAL_FILES)
        patterns.update(directory + '/' for directory in names.AGENT_DIRECTORIES)
        for prefix in names.AGENT_PREFIXES:
            patterns.update((prefix + '*', prefix + '.conf.yml'))
        for name in ('.dockerignore','nested/.gitignore','nested/toolignore','.npmignore'):
            for pattern in sorted(patterns):
                raw = ('safe/' + pattern.upper() + '/suffix').encode()
                with self.subTest(name=name,pattern=pattern),self.assertRaisesRegex(ValueError,'exclusions'):
                    policy.tier(name,b'build/',raw,self.inventory)
                self.assertTrue(policy.private_ignore_content(name,raw))
            self.assertEqual(policy.tier(name,b'build/',b'build/\n',self.inventory)[0],0)
    def test_invalid_python_refuses_with_path_and_syntax_error(self):
        for name in ('tests/test_bad.py','packages/bad.py','.github/scripts/bad.py'):
            with self.subTest(name=name),self.assertRaisesRegex(ValueError,name+': syntax error'):
                policy.tier(name,b'x=1',b'def broken(:',self.inventory)
    def test_unicode_runner_key_and_external_reusable_are_refused(self):
        with self.assertRaisesRegex(ValueError,'hosted'):
            privacy.check('"runs\\u002don": self-hosted')
        with self.assertRaisesRegex(ValueError,'External'):
            privacy.check('jobs:\n  external:\n    uses: other/repo/.github/workflows/test.yml@'+'a'*40)
    def test_hosted_label_alone_is_insufficient(self):
        with self.assertRaisesRegex(ValueError,'runner.environment'):
            privacy.check('jobs:\n  test:\n    runs-on: ubuntu-24.04\n    steps: []')
        doc={'jobs':{'test':{'runs-on':'ubuntu-24.04','steps':[copy.deepcopy(privacy.HOSTED_STEP)]}}}
        privacy.check(yaml.safe_dump(doc))
        doc['jobs']['test']['steps'][0]['if']='false'
        with self.assertRaises(ValueError):privacy.check(yaml.safe_dump(doc))
    def test_workflow_cache_event_boundaries(self):
        workflows=HELPERS.parent/'workflows' if (HELPERS.parent/'workflows').is_dir() else HELPERS
        restores, saves, keys = set(), {}, set()
        for name in ('ci.yml','ci-tests.yml'):
            doc=yaml.load((workflows/name).read_text(),Loader=yaml.BaseLoader)
            for job_name, job in doc['jobs'].items():
                for step in (s for s in job.get('steps',[]) if s.get('uses','').startswith('actions/cache')):
                    self.assertNotIn('restore-keys',step['with'])
                    keys.add(step['with']['key'])
                    action=step['uses'].split('@')[0]
                    job_id = (name, job_name)
                    if action == 'actions/cache/restore':
                        self.assertNotIn(job_id, restores)
                        self.assertNotIn('if', step)
                        restores.add(job_id)
                    else:
                        self.assertEqual(action, 'actions/cache/save')
                        self.assertNotIn(job_id, saves)
                        saves[job_id] = step['if']
        self.assertEqual(restores, {('ci.yml', 'checks')})
        self.assertEqual(saves, {('ci.yml', 'checks'): "github.event_name == 'push' && github.ref == "
                                "format('refs/heads/{0}', github.event.repository.default_branch) && "
                                "steps.dependency-cache.outputs.cache-hit != 'true'"})
        self.assertEqual(keys, {"public-ci-${{ runner.os }}-${{ runner.arch }}-"
                                "${{ hashFiles('candidate/requirements/*.lock', 'candidate/requirements/ci-runtimes.json', "
                                "'candidate/package-lock.json', '.trusted-controls/requirements/policy.lock') }}"})
        doc = yaml.load((workflows / 'ci.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(doc['on']['push']['branches'], ['dev', 'main', 'release/*'])
        for job in ('unit', 'browser', 'static'):
            self.assertEqual(doc['jobs'][job]['if'], "always() && !cancelled() && needs.checks.result == 'success'")
        self.assertEqual(doc['jobs']['ci-gate']['if'], "always()")

    def test_duplicate_yaml_keys_refused(self):
        with self.assertRaisesRegex(ValueError,'Duplicate'):privacy.check('runs-on: ubuntu-24.04\nruns-on: self-hosted')
    def test_expiry_only_edit_is_refused_and_remains_tier1(self):
        old={'schema':3,'fails':[],'unstable':[{'node':self.node,'class':'unstable','owner':'test-owner','reason':'Reviewed flake','issue':1,'expires_on':'2026-11-01'}]}
        new=copy.deepcopy(old);new['unstable'][0]['expires_on']='2026-12-01'
        a,b=json.dumps(old).encode(),json.dumps(new).encode()
        self.assertFalse(policy.removal_only(a,b))
        self.assertTrue(policy.removal_only(a,json.dumps({**old,'unstable':[]}).encode()))
        self.assertEqual(policy.tier(policy.KNOWN,a,b,self.inventory)[0],1)
    def test_allowance_edit_cannot_add_or_change_metadata(self):
        old={'schema':3,'fails':[],'unstable':[{'node':self.node,'expires_on':'2026-11-01','owner':'test-owner'}]}
        for new in ({**old,'fails':['new']},{**old,'unstable':[{**old['unstable'][0],'owner':'changed','expires_on':'2026-12-01'}]},
                    {**old,'unstable':[{**old['unstable'][0],'expires_on':'2026-10-01'}]}):
            self.assertFalse(policy.removal_only(json.dumps(old).encode(),json.dumps(new).encode()))
    def test_expiry_edit_is_refused_while_the_reader_honors_a_valid_existing_entry(self):
        old={'schema':3,'tree':'a'*40,'root_evidence_sha256':'b'*64,'root_runs':[],
             'classifications_sha256':'c'*64,'cleanup_cap':0,'fails':[],
             'unstable':[{'node':self.node,'class':'unstable','issue':1,'owner':'test-owner','reason':'Reviewed flake','expires_on':'2026-11-01'}]}
        renewed=copy.deepcopy(old);renewed['unstable'][0]['expires_on']='2026-12-01'
        self.assertFalse(policy.removal_only(json.dumps(old).encode(),json.dumps(renewed).encode()))
        self.assertTrue(maker.evaluate(renewed,renewed,[{self.node:'failed'}],today=date(2026,11,17)))
        with self.assertRaisesRegex(ValueError,'Expired'):
            maker.evaluate(old,old,[{self.node:'failed'}],today=date(2026,11,17))
    def test_same_pr_node_ledger_addition_only_passes_actual_disappearance(self):
        a,b=json.dumps(self.before).encode(),json.dumps(self.after).encode()
        self.assertEqual(node_census.ledger_additions(a,b),{self.node})
        allowed=node_census.allowed_removals(a,b,{self.node,'tests/test_a.py::test_keep'},{'tests/test_a.py::test_keep'},lambda n:'::test_' in n)
        self.assertEqual(allowed,{self.node})
        self.assertEqual(node_census.compare({self.node,'tests/test_a.py::test_keep'},{'tests/test_a.py::test_keep'},allowed)['summary'],'collect=+0/-1')
    def test_ledger_cannot_authorize_a_node_that_does_not_disappear(self):
        with self.assertRaisesRegex(ValueError,'actually removes'):
            node_census.allowed_removals(json.dumps(self.before),json.dumps(self.after),{self.node},{self.node},lambda n:True)
    def test_ledger_cannot_change_existing_metadata_or_file_ledger(self):
        for after in ({**self.after,'removed':[{'file':'extra'}]}, {**self.after,'removed_nodes':[{**self.after['removed_nodes'][0],'owner':'changed'}]}):
            before=self.before if after['removed'] else self.after
            self.assertIsNone(node_census.ledger_additions(json.dumps(before),json.dumps(after)))
    def test_current_dispatch_producer_and_workflow_triggers(self):
        # Runtime workflow fixtures are validated separately from this public source-only test.
        self.assertIn('(data.get("inputs") or {})',(HELPERS/'trusted_ci.py').read_text())

from hardening_profiles import apply_profile
apply_profile(globals(),{'test_invalid_python_refuses_with_path_and_syntax_error': ['content_classifier'], 'test_same_pr_node_ledger_addition_only_passes_actual_disappearance': ['node_census'], 'test_ledger_cannot_authorize_a_node_that_does_not_disappear': ['node_census'], 'test_ledger_cannot_change_existing_metadata_or_file_ledger': ['node_census']})

if __name__=='__main__':unittest.main()
