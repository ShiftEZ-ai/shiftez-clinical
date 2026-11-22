"""The built-in Waiting for approval view lists proposals for an older (non-catalogue) rule kind as rows of its own, and
its count holds each waiting proposal once. Real compiled rules_tree.ts and table_view.ts in Node (no DOM)."""
import json
import subprocess


def test_waiting_view_lists_older_kind_proposals_once_and_only_when_waiting_is_asked(client_modules):
    script = f"""const tree=await import({json.dumps((client_modules / 'rules_tree.js').as_uri())});
const {{joinFilter}}=await import({json.dumps((client_modules / 'table_view.js').as_uri())});
const {{WAITING}}=await import({json.dumps((client_modules / 'rule_catalogue.js').as_uri())});
const older=[{{id:'p1',name:'No duties every day',status:'proposed',kind:'policy'}},{{id:'p2',name:'Rest after nights',status:'proposed',kind:'policy'}}];
const entries=[{{id:'a',proposed:{{id:'c1'}}}},{{id:'b'}},{{id:'c',proposed:{{id:'c2'}}}}];
const ids=rows=>rows.map(p=>p.id);
const waiting=joinFilter({{state:[WAITING]}},null);
console.log(JSON.stringify({{
  waiting:ids(tree.olderShown(older,waiting)),
  allRules:ids(tree.olderShown(older,null)),
  onOnly:ids(tree.olderShown(older,joinFilter({{state:['On']}},null))),
  withFamily:ids(tree.olderShown(older,joinFilter({{state:[WAITING],family:['Staffing']}},null))),
  searched:ids(tree.olderShown(older,waiting,t=>t.toLowerCase().includes('rest'))),
  count:tree.waitingCount(entries,older),countNone:tree.waitingCount(entries,[]),
}}));process.exit(0);"""
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    # Waiting for approval shows every older-kind proposal; All rules and other states do not.
    assert got['waiting'] == ['p1', 'p2']
    assert got['allRules'] == [] and got['onOnly'] == []
    # A filter on a property an older rule does not have excludes it; a search matches its name.
    assert got['withFamily'] == []
    assert got['searched'] == ['p2']
    # Two catalogue rules with a proposal plus two older-kind proposals, each counted once.
    assert got['count'] == 4 and got['countNone'] == 2
