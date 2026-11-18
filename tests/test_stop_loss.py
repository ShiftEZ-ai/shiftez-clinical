"""Every one-line hardening switch preserves the required core verdict."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
HELPERS=Path(__file__).resolve().parents[1]/'.github/scripts'
if not HELPERS.is_dir():HELPERS=Path(__file__).resolve().parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0,str(HELPERS))
import hardening
import working_policy as policy
import trusted_verdict as verdict
import workflow_privacy
import test_readme_empty
import test_working_policy as core_cases
import test_policy_v34 as importer_cases
from hardening_profiles import apply_profile

class StopLossTests(unittest.TestCase):
    def check_core(self,disabled):
        flags=hardening.load();flags.update({k:False for k in disabled})
        with tempfile.TemporaryDirectory(dir=os.environ['RUNNER_TEMP']) as folder:
            root=Path(folder);config=root/'hardening.json';config.write_text(json.dumps(flags))
            with patch.object(hardening,'CONFIG',config):
                helper=core_cases.PolicyTests();helper.setUp()
                helper.exercise_verdict_materialization('dev')
                helper.exercise_verdict_materialization('main')
                good={n:{'result':'success'} for n in ('checks','static','unit','browser')}
                verdict.required_producers(json.dumps(good))
                for name in good:
                    bad=copy.deepcopy(good);bad[name]['result']='skipped'
                    with self.assertRaises(ValueError):verdict.required_producers(json.dumps(bad))
                after=copy.deepcopy(helper.before);after['fails'].pop()
                self.assertTrue(policy.removal_only(json.dumps(helper.before).encode(),json.dumps(after).encode()))
                after=copy.deepcopy(helper.before);after['fails'].append({'node':'new','class':'baseline'})
                self.assertFalse(policy.removal_only(json.dumps(helper.before).encode(),json.dumps(after).encode()))
                for path in ('.github/workflows/ci.yml','ci/hardening.json','ci/known-failures.json','tests/quarantine.py','pytest.ini'):
                    self.assertEqual(policy.tier(path,b'{}',b'{}',helper.inventory)[0],1)
                with self.assertRaises(ValueError):policy.tier('AGENTS.md',None,b'private',helper.inventory)
                with self.assertRaises(ValueError):workflow_privacy.check('runs-on: self-hosted')
                with self.assertRaises(ValueError):helper.run_guard('apps/change.py',branch='main',merge_tree='e'*40)
                (root/'README.md').write_text('not empty')
                old=Path.cwd()
                try:
                    os.chdir(root)
                    with patch.object(test_readme_empty.subprocess,'check_output',return_value=b'README.md\0'):
                        with self.assertRaises(AssertionError):test_readme_empty.test_readme_empty()
                finally:os.chdir(old)
    def test_canaries_off_keeps_core_verdict(self):self.check_core({'canaries'})
    def test_node_census_off_keeps_core_verdict(self):self.check_core({'node_census'})
    def test_content_classifier_off_keeps_core_verdict(self):self.check_core({'content_classifier'})
    def test_plan_lapse_backstop_off_keeps_core_verdict(self):self.check_core({'plan_lapse_backstop'})
    def test_window_expiry_off_keeps_core_verdict(self):self.check_core({'window_expiry'})
    def test_all_hardening_off_keeps_core_verdict(self):self.check_core(hardening.KEYS)
    def test_invalid_switch_is_not_silently_disabled(self):
        with tempfile.TemporaryDirectory(dir=os.environ['RUNNER_TEMP']) as folder:
            path=Path(folder)/'config.json';data=hardening.load();data['canaries']='false';path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError,'Invalid'):hardening.load(path)
    def test_candidate_flag_edit_is_still_tier1(self):
        flags=hardening.load();flags['content_classifier']=False
        with patch.object(hardening,'load',return_value=flags):
            self.assertEqual(policy.tier('ci/hardening.json',b'{}',json.dumps(flags).encode(),{'helpers':set(),'product_imports':set()})[0],1)
    def test_content_switch_stops_fingerprints_but_keeps_path_policy(self):
        flags=hardening.load();flags['content_classifier']=False
        attack=b'def pytest_runtest_makereport(): pass\n'
        with patch.object(hardening,'load',return_value=flags),patch.object(policy,'python_controls',side_effect=AssertionError('disabled classifier called')):
            self.assertEqual(policy.tier('tests/test_a.py',b'def test_a(): pass',attack,{'helpers':set(),'product_imports':set()})[0],2)
            self.assertEqual(policy.tier('.github/scripts/control.py',b'x=1',attack,{'helpers':set(),'product_imports':set()})[0],1)
    def test_all_switches_off_keep_bootstrap_plan_integrity(self):
        fixture=importer_cases.ImporterV34Tests();fixture.setUp()
        try:
            flags=hardening.load();flags.update({key:False for key in hardening.KEYS})
            with patch.object(hardening,'load',return_value=flags):
                capture,reports=fixture.layout()
                self.assertEqual(importer_cases.importer.load_plan(capture,reports),fixture.plan)
                (reports/'87/shard-plan.json').write_bytes(b'{}')
                with self.assertRaisesRegex(ValueError,'differs'):importer_cases.importer.load_plan(capture,reports)
        finally:fixture.doCleanups()
    def test_disabled_feature_test_does_not_execute_feature_or_remove_core_test(self):
        def feature():raise AssertionError('Disabled hardening executed')
        def core():return 'core ran'
        names={'__name__':'synthetic_profiles','test_feature':feature,'test_core':core}
        flags=hardening.load();flags['canaries']=False
        apply_profile(names,{'test_feature':['canaries']})
        with patch.object(hardening,'load',return_value=flags):
            self.assertIsNone(names['test_feature']())
            self.assertEqual(names['test_core'](),'core ran')
            self.assertEqual(names['test_feature'].__name__,'feature')

if __name__=='__main__':unittest.main()
