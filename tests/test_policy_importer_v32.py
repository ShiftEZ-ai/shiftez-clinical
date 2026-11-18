"""Public v3.2 guard, original-root importer and node-census regressions."""
import copy
from datetime import date, timedelta
import hashlib
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

HERE=Path(__file__).resolve().parents[1]/".github/scripts"
if not HERE.is_dir(): HERE=Path(__file__).resolve().parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0,str(HERE))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"ci"))
if not (HERE/"hosted_ci.py").is_file(): sys.path.insert(0,str(HERE.parents[1]/"workflows-next/.github/scripts"))
import hosted_ci as hosted
import working_policy as policy
import known_skips
import make_known_failures as maker
import cut_root_measurements as importer
import failure_classifications
import node_census

class PublicV32Cases(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="v32-public-",dir=os.environ["RUNNER_TEMP"])
        self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)
        self.root={"sha":"a"*40,"tree":"b"*40,"base_sha":"c"*40}
        self.run={"id":10,"repository_id":1459752401,"run_attempt":1,"head_sha":self.root["sha"],
                  "head_branch":"p3/pipeline","event":"pull_request","status":"completed","path":".github/workflows/ci.yml",
                  "pull_requests":[{"head_sha":self.root["sha"],"base_sha":self.root["base_sha"]}]}
        self.capture={"schema":1,"run":self.run,"run_readback_after_download":copy.deepcopy(self.run),"artifacts":[{"id":1}]}
        self.plan={"schema":1,"sha":self.root["sha"],"tree":self.root["tree"],"base_sha":self.root["base_sha"],
            "replicas":[1,2],"smoke":False,"suites":{
                "unit":{"shards":[{"files":["tests/test_unit.py"]}]},
                "browser":{"shards":[{"files":["tests/test_browser_x.py"]}]}}}
        self.capture['performance']=[{'suite':suite,'shard':1,'replica':replica,'queue_seconds':1.0,'runtime_seconds':2.0,'status':'completed','conclusion':'success'}
            for suite in ('unit','browser') for replica in (1,2)]
        self.capture['performance'].append({'name':'Checks','suite':None,'queue_seconds':1.0,'runtime_seconds':2.0,'status':'completed','conclusion':'success'})
        self.reports=self.folder/"reports";self.reports.mkdir()
        self.inventory={"helpers":set(),"product_imports":set()}

    def write_reports(self, error=False):
        for suite,row in self.plan["suites"].items():
            stem=row["shards"][0]["files"][0]
            for replica in self.plan['replicas']:
                folder=self.reports/(suite+str(replica));folder.mkdir(exist_ok=True)
                node=stem+"::test_case"
                report=folder/"junit.xml"
                outcome='<error message="synthetic setup error">synthetic</error>' if error and suite=="unit" else ''
                report.write_text('<testsuites><testsuite><testcase classname="'+stem[:-3].replace('/','.')+'" name="test_case">'+outcome+'</testcase></testsuite></testsuites>')
                selection=folder/"junit.selection.json";selection.write_text(json.dumps([node]))
                deselected=folder/"junit.deselected.json";deselected.write_text('[]')
                receipt={"schema":1,"sha":self.root["sha"],"tree":self.root["tree"],"plan_sha256":hosted.digest(self.plan),
                    "suite":suite,"shard":1,"replica":replica,"run_id":"10","run_attempt":"1","reports":[
                    {"file":"junit.xml","exit_code":1 if outcome else 0,"sha256":{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (report,selection,deselected)}}]}
                (folder/"shard-receipt.json").write_text(json.dumps(receipt))

    def test_guard_changed_binary_is_flagged(self):
        for name,raw in (("image.png",b'\x89PNG\xff'),("font.woff2",b'\x00wOFF'),("sheet.xlsx",b'PK\xff')):
            self.assertEqual(policy.tier(name,b'old',raw,self.inventory)[0],2)

    def test_guard_mkdir_p_does_not_block(self):
        self.assertEqual(policy.tier("docs/example.md",b'',b'mkdir -p output\ngit add -p\n',self.inventory)[0],0)

    def test_guard_startup_packages_are_tier1(self):
        for name in ("sitecustomize/__init__.py","usercustomize/__init__.py","nested/sitecustomize/other.py"):
            self.assertEqual(policy.tier(name,None,b'x=1',self.inventory)[0],1)

    def test_guard_all_documented_pytest_config_names(self):
        for name in ("pytest.toml",".pytest.toml","pytest.ini",".pytest.ini","tox.ini","setup.cfg"):
            self.assertEqual(policy.tier(name,b'',b'',self.inventory)[0],1)
        self.assertEqual(policy.tier("pyproject.toml",b'',b'[tool.pytest]\naddopts=["-p","x"]',self.inventory)[0],1)

    def test_guard_nonpython_plugin_name_is_ordinary(self):
        self.assertEqual(policy.tier("apps/plugins.ts",None,b'export const x=1',self.inventory)[0],0)

    def test_empty_bootstrap_baseline_and_template_ledger_are_valid(self):
        baseline=self.folder/'baseline.json';baseline.write_text(maker.encoded([]))
        ledger=self.folder/'ledger.json';ledger.write_text(maker.encoded({'schema':2,'removed':[],'removed_nodes':[]}))
        self.assertEqual(maker.load_frozen_baseline(baseline)[0],set())
        self.assertEqual(maker.load_removed_tests(ledger)[0],[])
        self.write_reports()
        runs,_=importer.import_runs(self.capture,self.plan,self.reports,self.root,maker)
        self.assertEqual(len(runs),2)
        with patch.object(maker,'load_frozen_baseline',side_effect=AssertionError('Hand-read must not consume allowance files')):
            importer.validate_capture(self.capture,self.root)

    def test_pr1_evidence_reader_accepts_only_root_parent_bound_measurement(self):
        data={'schema':1,'sha':self.root['sha'],'tree':self.root['tree'],'base_sha':self.root['base_sha'],
              'baseline_sha256':'a'*64,'runs':[{'run_id':'10','run_attempt':'1','replica':i} for i in (1,2)],
              'counts':{'baseline':0,'cleanup':0,'unstable':0},'receipts_sha256':'b'*64}
        path=self.folder/'evidence.json';path.write_text(maker.encoded(data))
        def git(*args):return self.root['tree'] if args[0]=='rev-parse' else self.root['base_sha']
        with patch.object(maker,'git',side_effect=git):
            self.assertEqual(maker.load_root_evidence(path,'a'*64)[0],data)
        path.write_text(maker.encoded({k:v for k,v in data.items() if k!='base_sha'}))
        with self.assertRaises(ValueError):maker.load_root_evidence(path,'a'*64)

    def ancestry_history(self):
        repo = self.folder / 'history'
        subprocess.run(['git', 'init', str(repo)], check=True, capture_output=True)
        env = {**os.environ, 'GIT_AUTHOR_NAME':'Test', 'GIT_AUTHOR_EMAIL':'test@example.invalid',
               'GIT_COMMITTER_NAME':'Test', 'GIT_COMMITTER_EMAIL':'test@example.invalid'}
        def git(*args, **kwargs):
            return subprocess.check_output(['git', '-C', str(repo), *args], env=env, **kwargs)
        tree = git('mktree', input=b'').decode().strip()
        root = git('commit-tree', tree, input=b'Root\n').decode().strip()
        first = git('commit-tree', tree, '-p', root, input=b'Initial controls\n').decode().strip()
        head = git('commit-tree', tree, '-p', first, input=b'Bootstrap controls\n').decode().strip()
        orphan = git('commit-tree', tree, input=b'Unrelated root\n').decode().strip()
        git('update-ref', 'refs/heads/main', head)
        git('symbolic-ref', 'HEAD', 'refs/heads/main')
        return git, root, first, head, orphan, tree

    def ancestry_inputs(self, head, tree, base):
        root = {'sha':head, 'tree':tree, 'base_sha':base}
        capture = copy.deepcopy(self.capture)
        capture['run'].update(head_sha=head, pull_requests=[{'head_sha':head,'base_sha':base}])
        capture['run_readback_after_download'] = copy.deepcopy(capture['run'])
        evidence = {'schema':1, **root, 'baseline_sha256':'a'*64,
                    'runs':[{'run_id':'10','run_attempt':'1','replica':i} for i in (1,2)],
                    'counts':{'baseline':0,'cleanup':0,'unstable':0},'receipts_sha256':'b'*64}
        path = self.folder / 'ancestry-evidence.json'
        path.write_text(maker.encoded(evidence))
        return root, capture, evidence, path

    def test_two_commit_pr1_chain_is_accepted_by_both_readers(self):
        git, base, first, head, orphan, tree = self.ancestry_history()
        root, capture, evidence, path = self.ancestry_inputs(head, tree, base)
        self.assertNotEqual(git('show','-s','--format=%P',head).decode().strip(), base)
        with patch.object(policy, 'git', side_effect=git), \
             patch.object(maker, 'git', side_effect=lambda *args:git(*args).decode().strip()):
            importer.validate_capture(capture, root)
            importer.validate_source(root)
            self.assertEqual(maker.load_root_evidence(path, 'a'*64)[0], evidence)

    def test_pr1_head_outside_root_history_is_refused_by_both_readers(self):
        git, base, first, head, orphan, tree = self.ancestry_history()
        root, capture, evidence, path = self.ancestry_inputs(orphan, tree, base)
        with patch.object(policy, 'git', side_effect=git), \
             patch.object(maker, 'git', side_effect=lambda *args:git(*args).decode().strip()):
            importer.validate_capture(capture, root)
            with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                importer.validate_source(root)
            with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                maker.load_root_evidence(path, 'a'*64)

    def test_pr1_base_other_than_root_is_refused_by_both_readers(self):
        git, base, first, head, orphan, tree = self.ancestry_history()
        root, capture, evidence, path = self.ancestry_inputs(head, tree, first)
        with patch.object(policy, 'git', side_effect=git), \
             patch.object(maker, 'git', side_effect=lambda *args:git(*args).decode().strip()):
            with self.assertRaises(ValueError):
                importer.validate_capture(capture, {**root, 'base_sha':base})
            with self.assertRaises(ValueError):
                maker.load_root_evidence(path, 'a'*64)

    def check_event_replicas(self, event, replicas):
        accepted={'schema':3,'fails':[],'unstable':[]}
        planner=importer.trusted_ci
        def git(*args):
            return self.root['tree'] if args[-1]=='HEAD^{tree}' else 'tests/test_policy_importer_v32.py\ntests/test_browser_recurring_sessions.py' if args[0]=='ls-files' else self.root['sha']
        durations={'suites':{'unit':{'files':{'tests/test_policy_importer_v32.py':1.0}},'browser':{'files':{'tests/test_browser_recurring_sessions.py':1.0}}}}
        with patch.dict(os.environ,{'GITHUB_EVENT_NAME':event}), patch.object(maker,'at_revision',return_value=accepted):
            context=maker.context(self.root['base_sha'])
            self.assertEqual(context['replicas'],replicas)
            with patch.object(planner,'settings',return_value={'unit_shards':1,'browser_shards':1}), \
                 patch.object(hosted,'git',side_effect=git),patch.object(hosted,'load_durations',return_value=durations), \
                 patch.object(planner.canaries,'sha',return_value='f'*64):
                self.assertEqual(planner.plan(self.root['base_sha'],False)['replicas'],context['replicas'])
            self.plan['replicas']=replicas
            self.write_reports()
            self.assertEqual(len(maker.collect_reports(self.plan,self.reports,'10','1')),len(replicas))
        with patch.dict(os.environ,{'GITHUB_EVENT_NAME':event}),patch.object(maker,'at_revision',return_value=None):
            self.assertEqual(maker.context(self.root['base_sha'])['replicas'],[1,2])

    def test_ordinary_pull_request_uses_two_replicas(self):
        self.check_event_replicas('pull_request',[1,2])

    def test_merge_group_uses_two_replicas(self):
        self.check_event_replicas('merge_group',[1,2])

    def test_pr1_wrong_head_is_refused(self):
        self.capture['run']['head_sha']='d'*40
        with self.assertRaises(ValueError):importer.validate_capture(self.capture,self.root)

    def test_pr1_wrong_base_is_refused(self):
        self.capture['run']['pull_requests'][0]['base_sha']='d'*40
        with self.assertRaises(ValueError):importer.validate_capture(self.capture,self.root)

    def test_pr1_single_replica_is_refused(self):
        self.plan['replicas']=[1]
        with self.assertRaises(ValueError):importer.import_runs(self.capture,self.plan,self.reports,self.root,maker)

    def test_pr1_wrong_plan_base_is_refused(self):
        self.plan['base_sha']='d'*40
        with self.assertRaises(ValueError):importer.import_runs(self.capture,self.plan,self.reports,self.root,maker)

    def test_pr1_attempt_two_is_refused(self):
        self.capture['run']['run_attempt']=2
        with self.assertRaises(ValueError):importer.validate_capture(self.capture,self.root)

    def test_parentless_root_dispatch_is_no_longer_accepted(self):
        self.capture['run'].update(event='workflow_dispatch',head_branch='main')
        with self.assertRaises(ValueError):importer.validate_capture(self.capture,self.root)

    def test_pr1_checks_red_or_incomplete_shard_is_refused(self):
        for index,field,value in ((-1,'conclusion','failure'),(0,'status','in_progress'),(0,'conclusion','cancelled')):
            capture=copy.deepcopy(self.capture)
            capture['performance'][index][field]=value
            with self.subTest(index=index,field=field),self.assertRaises(ValueError):
                importer.import_runs(capture,self.plan,self.reports,self.root,maker)

    def test_stop2_one_run_two_replicas_is_imported(self):
        self.write_reports()
        runs,measurements=importer.import_runs(self.capture,self.plan,self.reports,self.root,maker)
        self.assertEqual(len(runs),2);self.assertEqual([m["replica"] for m in measurements],[1,2])
        self.assertEqual({m["run_id"] for m in measurements},{"10"})

    def test_stop2_attempt_two_is_refused(self):
        self.capture["run"]["run_attempt"]=2
        with self.assertRaises(ValueError):importer.validate_capture(self.capture,self.root)

    def test_stop2_duplicate_replica_is_refused(self):
        self.write_reports();path=self.reports/"unit2/shard-receipt.json";data=json.loads(path.read_text());data["replica"]=1;path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):importer.import_runs(self.capture,self.plan,self.reports,self.root,maker)

    def test_stop2_missing_replica_is_refused(self):
        self.write_reports();(self.reports/"unit2/shard-receipt.json").rename(self.reports/"unit2/held.json")
        with self.assertRaises(ValueError):importer.import_runs(self.capture,self.plan,self.reports,self.root,maker)

    def test_stop2_wrong_run_receipt_is_refused(self):
        self.write_reports();path=self.reports/"unit1/shard-receipt.json";data=json.loads(path.read_text());data["run_id"]="11";path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):importer.import_runs(self.capture,self.plan,self.reports,self.root,maker)

    def test_stop2_wrong_root_tree_is_refused(self):
        plan=copy.deepcopy(self.plan);plan["tree"]="c"*40
        with self.assertRaises(ValueError):importer.import_runs(self.capture,plan,self.reports,self.root,maker)

    def test_stop2_altered_junit_hash_is_refused(self):
        self.write_reports();(self.reports/"unit1/junit.xml").write_text('changed')
        with self.assertRaises(ValueError):importer.import_runs(self.capture,self.plan,self.reports,self.root,maker)

    def test_stop2_root_error_has_own_class_and_replica_metadata(self):
        self.write_reports(error=True)
        runs,measurements=importer.import_runs(self.capture,self.plan,self.reports,self.root,maker)
        config={"unstable_issue":1,"unstable_expires_on":(date.today()+timedelta(days=30)).isoformat(),"unstable_limit":80,"cleanup_issue":1}
        metadata=json.dumps({'schema':1,'status':'REVIEWED','entries':[{'node':'tests/test_unit.py::test_case','class':'baseline-error','owner':'runtime-owner','reason':'Reviewed original root setup error'}]}).encode()
        raw,listing,skips=importer.build_cut(runs,measurements,self.root,config,maker,set(),"a"*64,set(),{},metadata,self.capture['performance'])
        self.assertEqual(listing["fails"],[{"node":"tests/test_unit.py::test_case","class":"baseline-error","owner":"runtime-owner","reason":"Reviewed original root setup error"}])
        self.assertEqual(json.loads(raw)["counts"]["baseline-error"],1)
        after=copy.deepcopy(listing);after["fails"]=[]
        self.assertTrue(policy.removal_only(json.dumps(listing).encode(),json.dumps(after).encode()))
        maker.evaluate(listing,listing,runs)
        with self.assertRaises(ValueError):known_skips.comparison_runs(runs,set(),release=True,allowed_errors={"tests/test_unit.py::test_case"})
        with self.assertRaises(ValueError):maker.evaluate(after,listing,runs)

    def test_stop2_new_error_and_reclassification_are_refused(self):
        listing={"schema":2,"tree":"a"*40,"root_evidence_sha256":"b"*64,"root_runs":[{"run_id":"10","run_attempt":"1","replica":i} for i in (1,2)],"cleanup_cap":0,"fails":[],"unstable":[]}
        with self.assertRaises(ValueError):maker.evaluate(listing,listing,[{"tests/test_a.py::test_a":"error"}])
        before=copy.deepcopy(listing);before["fails"]=[{"node":"tests/test_a.py::test_a","class":"baseline-error"}]
        after=copy.deepcopy(before);after["fails"][0]["class"]="baseline"
        with self.assertRaises(ValueError):maker.require_class_ratchet(after,before)

    def test_stop2_artifact_digest_is_verified(self):
        archive=self.folder/"one.zip"
        with zipfile.ZipFile(archive,'w') as z:z.writestr('plan.json','{}')
        digest=hashlib.sha256(archive.read_bytes()).hexdigest()
        capture={"artifacts":[{"id":1,"path":str(archive),"sha256":digest,"github_digest":"sha256:"+digest}]}
        importer.unpack(capture,self.folder/"out")
        capture["artifacts"][0]["sha256"]="f"*64
        with self.assertRaises(ValueError):importer.unpack(capture,self.folder/"other")

    def test_stop2_archive_traversal_is_refused(self):
        archive=self.folder/"evil.zip"
        with zipfile.ZipFile(archive,'w') as z:z.writestr('../escape.txt','x')
        digest=hashlib.sha256(archive.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):importer.unpack({"artifacts":[{"id":1,"path":str(archive),"sha256":digest,"github_digest":"sha256:"+digest}]},self.folder/"out")

    def test_seat_b_reporter_constructor_forgery_is_tier1(self):
        attack=b'''from _pytest.reports import TestReport
original = TestReport.__init__
def conceal(self, *args, **kwargs):
    original(self, *args, **kwargs)
    if self.nodeid.endswith("test_new_regression") and self.outcome == "failed":
        self.outcome = "passed"
TestReport.__init__ = conceal
'''
        self.assertEqual(policy.tier('tests/test_synthetic.py',b'def test_keep(): pass\n',attack,self.inventory)[0],1)

    def test_reporter_alias_junit_terminal_mutations_are_tier1(self):
        for attack in (b'from _pytest.reports import TestReport as R\nR.__init__=replacement\n',
                       b'import _pytest.junitxml as j\nj.LogXML.node_reporter=fake\n',
                       b'from _pytest.terminal import TerminalReporter as T\nsetattr(T,"write",fake)\n',
                       b'from _pytest._io import TerminalWriter as W\nAlias=W\nAlias.write=fake\n'):
            self.assertEqual(policy.tier('packages/unusual.py',b'x=1\n',attack,self.inventory)[0],1)

    def test_in_file_node_disappearance_is_refused(self):
        before={'tests/test_a.py::test_old','tests/test_a.py::test_keep'}
        after={'tests/test_a.py::test_renamed','tests/test_a.py::test_keep'}
        with self.assertRaisesRegex(ValueError,'Unlisted disappeared'):node_census.compare(before,after,set())

    def test_approved_node_removal_is_tier2_and_prints_delta(self):
        raw=json.dumps({'schema':2,'removed':[],'removed_nodes':[{'node':'tests/test_a.py::test_old','owner':'test-owner','reason':'Reviewed replacement'}]})
        allowed=node_census.removed_nodes(raw,maker.SAFE_NODE.fullmatch)
        result=node_census.compare({'tests/test_a.py::test_old','tests/test_a.py::test_keep'},
            {'tests/test_a.py::test_new','tests/test_a.py::test_keep'},allowed)
        self.assertEqual(result['summary'],'collect=+1/-1')
        self.assertEqual(result['tier2'],['tests/test_a.py::test_old'])

    def test_base_census_requires_complete_source_bound_artifacts(self):
        self.write_reports();expected={'tests/test_unit.py','tests/test_browser_x.py'}
        for suite,row in self.plan['suites'].items():
            for replica in (1,2):
                folder=self.reports/(suite+str(replica));path=folder/'base-census.json'
                data={'base_sha':'c'*40,'head_sha':self.root['sha'],'run_id':'10','run_attempt':'1','suite':suite,'shard':1,
                      'replica':replica,'files':row['shards'][0]['files'],'nodes':[row['shards'][0]['files'][0]+'::test_case']}
                path.write_text(json.dumps(data));receipt=folder/'shard-receipt.json';value=json.loads(receipt.read_text())
                value['base_census_sha256']=hashlib.sha256(path.read_bytes()).hexdigest();receipt.write_text(json.dumps(value))
        nodes=node_census.base_nodes(self.plan,self.reports,'c'*40,'10','1',maker.canonical_node,expected)
        self.assertEqual(len(nodes),2)
        (self.reports/'unit1/base-census.json').write_text('{}')
        with self.assertRaises(ValueError):node_census.base_nodes(self.plan,self.reports,'c'*40,'10','1',maker.canonical_node,expected)

    def test_class_owner_reason_are_required_and_bound(self):
        listing={'schema':2,'tree':'a'*40,'root_evidence_sha256':'b'*64,'root_runs':[],
                 'cleanup_cap':1,'fails':[{'node':'tests/test_a.py::test_a','class':'cleanup','issue':1}],'unstable':[]}
        metadata=json.dumps({'schema':1,'status':'REVIEWED','entries':[{'node':'tests/test_a.py::test_a','class':'baseline',
            'owner':'G4','reason':'Reviewed baseline family'}]}).encode()
        current=failure_classifications.attach(listing,metadata,maker.SAFE_NODE.fullmatch,1)
        self.assertEqual(current['fails'][0]['class'],'baseline')
        self.assertEqual(current['fails'][0]['owner'],'G4')
        failure_classifications.validate(current,metadata,maker.SAFE_NODE.fullmatch)
        current['fails'][0]['reason']='changed'
        with self.assertRaises(ValueError):failure_classifications.validate(current,metadata,maker.SAFE_NODE.fullmatch)
        with self.assertRaises(ValueError):failure_classifications.load(b'{"schema":1,"status":"TODO","entries":[]}',maker.SAFE_NODE.fullmatch)

    def test_digestless_seed_format_does_not_block_pr1_preflight(self):
        seeds={'schema':1,'nodes':['tests/test_a.py::test_a']}
        (self.folder/'ci').mkdir();(self.folder/'ci/unstable-seeds.json').write_text(json.dumps(seeds))
        self.assertEqual(policy.decode((self.folder/'ci/unstable-seeds.json').read_bytes()),seeds)
        ctx={'base_sha':'a'*40,'bootstrap':True,'base':None,'current':None}
        previous=Path.cwd()
        try:
            os.chdir(self.folder)
            with patch.object(maker,'load_frozen_baseline',return_value=(set(),'a'*64)), \
                 patch.object(maker,'load_env_exceptions',return_value=(set(),'b'*64)), \
                 patch.object(maker,'load_root_evidence',return_value=(None,None)), \
                 patch.object(maker,'require_root_binding'),patch.object(maker,'load_removed_tests',return_value=([],None)), \
                 patch.object(maker,'git',return_value=''):
                maker.preflight(ctx,smoke=False,limit=80,issue=1,expires_on=(date.today()+timedelta(days=30)).isoformat())
        finally:os.chdir(previous)
        self.assertNotIn('source_sha256',(HERE/'cut_root_measurements.py').read_text())


from hardening_profiles import apply_profile
apply_profile(globals(),{'test_seat_b_reporter_constructor_forgery_is_tier1': ['content_classifier'], 'test_reporter_alias_junit_terminal_mutations_are_tier1': ['content_classifier'], 'test_in_file_node_disappearance_is_refused': ['node_census'], 'test_approved_node_removal_is_tier2_and_prints_delta': ['node_census'], 'test_base_census_requires_complete_source_bound_artifacts': ['node_census']})

if __name__=="__main__":unittest.main()
