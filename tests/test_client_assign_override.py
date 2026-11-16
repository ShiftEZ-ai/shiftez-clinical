"""Real planner_week and dom modules: what a refusal of a reasoned assign (RUL-E04) does to its reason field."""
import json
import subprocess


def run(client_modules, script):
    source = (f"import assert from 'node:assert/strict';"
              f"const p=await import({json.dumps((client_modules / 'planner_week.js').as_uri())});"
              f"const d=await import({json.dumps((client_modules / 'dom.js').as_uri())});{script}")
    done = subprocess.run(['node', '--input-type=module', '-e', source], capture_output=True, text=True, timeout=15)
    assert done.returncode == 0, done.stderr


def test_only_person_unavailable_and_schema_refusals_keep_the_reason(client_modules):
    run(client_modules, """
for(const code of ['PERSON_UNAVAILABLE','SCHEMA_INVALID'])assert.equal(p.reasonRefusal(code),'retry',code);
for(const code of ['EDIT_INVALID','MOVE_INVALID','SWAP_INVALID'])assert.equal(p.reasonRefusal(code),'final',code);
for(const code of ['STALE_PREVIEW','CHECK_INCOMPLETE'])assert.equal(p.reasonRefusal(code),null,code);
""")


def test_a_reason_of_spaces_is_refused_before_it_is_armed(client_modules):
    run(client_modules, """
assert.equal(d.reasonProblem('',3),'','empty is the required check, not this one');
for(const v of ['   ','  a  ','ab '])assert.equal(d.reasonProblem(v,3),'At least 3 characters',v);
for(const v of ['abc',' a b '])assert.equal(d.reasonProblem(v,3),'',v);
""")



def test_save_without_names_one_two_three_and_more_people(client_modules):
    run(client_modules, """
const w=names=>`Save without ${d.namesWords(names)}`;
assert.equal(w(['Ann']),'Save without Ann');
assert.equal(w(['Ann','Bo']),'Save without Ann and Bo');
assert.equal(w(['Ann','Bo','Cy']),'Save without Ann, Bo and Cy');
assert.equal(w(['Ann','Bo','Cy','Di']),'Save without Ann, Bo, Cy and 1 other');
assert.equal(w(['Ann','Bo','Cy','Di','Ed']),'Save without Ann, Bo, Cy and 2 others');
""")
