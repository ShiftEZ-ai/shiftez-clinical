"""The pinned Rules panel's rule-map answers, run as the real compiled client in Node (logic only, no DOM)."""
import json
from pathlib import Path
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]


def version(revision):
    return {'workspace': {'id': 'w1', 'revision': revision, 'validation': {'evaluated_at': f't{revision}'}}, 'current_fingerprint': f'f{revision}'}


def test_an_older_rule_map_answer_never_draws_over_the_newer_rota_version(client_modules):
    # A live refresh asks again for version 2 while version 1's request is still out; 2 answers first, then 1 fails.
    script = f"""const m=await import({json.dumps((client_modules/'planner_rules.js').as_uri())});
const [v1,v2]=JSON.parse(process.argv[1]),pin={{}},key=m.keyOf;
console.log(JSON.stringify({{
  older:m.answerApplies(pin,pin,v2,key(v1)), newer:m.answerApplies(pin,pin,v2,key(v2)),
  otherPin:m.answerApplies({{}},pin,v2,key(v2)), closed:m.answerApplies(null,pin,v2,key(v2)), noRota:m.answerApplies(pin,pin,null,key(v2))}}));"""
    result = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps([version(1), version(2)])],
                            capture_output=True, text=True, check=True, timeout=15)
    assert json.loads(result.stdout) == {'older': False, 'newer': True, 'otherPin': False, 'closed': False, 'noRota': False}
