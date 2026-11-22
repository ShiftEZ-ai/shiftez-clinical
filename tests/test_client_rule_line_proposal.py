"""A line proposed from the rule line panel (relax or promote, rule_catalogue.ts lineProposal) keeps the rule's own on or
off, settings and balance from the draft: adding a line never switches a rule that is off back on, nor one that is on
off. Real compiled rule_catalogue.ts in Node."""
import json
import subprocess

import pytest

from .test_client_toast_save import FAKE_DOM


@pytest.mark.parametrize('on', [False, True])
def test_a_proposed_line_keeps_the_rules_on_or_off_settings_and_balance(client_modules, on):
    script = FAKE_DOM + f"""
const {{lineProposal}}=await import({json.dumps((client_modules / 'rule_catalogue.js').as_uri())});
const draft={{on:{json.dumps(on)},lines:[{{id:'a'}}],settings:{{block_quota:{{metric:'duties'}}}},balance:false,base:3,baseId:'r1',seen:['a']}};
const lines=[{{id:'a'}},{{id:'b'}}];
console.log(JSON.stringify(lineProposal(draft,lines)));
"""
    result = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout.strip().splitlines()[-1])
    assert body == {'on': on, 'lines': [{'id': 'a'}, {'id': 'b'}], 'settings': {'block_quota': {'metric': 'duties'}},
                    'balance': False, 'seen': ['a']}


@pytest.mark.parametrize('lines,on', [([{'id': 'a'}], True), ([], False)], ids=['first-line', 'no-line'])
def test_the_first_line_of_a_rule_never_in_force_turns_it_on(client_modules, lines, on):
    """#1200: a rule with no version yet (baseId null) is turned on by its first line; a rule in force keeps its own."""
    script = FAKE_DOM + f"""
const {{lineProposal}}=await import({json.dumps((client_modules / 'rule_catalogue.js').as_uri())});
const draft={{on:false,lines:[],settings:{{}},balance:true,base:null,baseId:null,seen:[]}};
console.log(JSON.stringify(lineProposal(draft,{json.dumps(lines)}).on));
"""
    result = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) is on
