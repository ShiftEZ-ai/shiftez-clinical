"""Run the shared condition cases (tests/fixtures/condition_cases.json) against the browser's evaluator
(conditions.ts lineMatches, through table_view's tree walker), compiled and executed in Node, so the client's display
matching and the server's evaluator cannot drift.

Relative values reach the client the way they do in the app: conditions.resolveOperands asks
/api/conditions/resolve-operands (stubbed here with the server's own resolve_operand results, shaped as resolve_batch
returns them). Values the client deliberately leaves to the server (per duty, a row's own target) are not part of the
client's job and are skipped by name. Logic only: no DOM, keyboard or visual claims.
"""
from copy import deepcopy
import re
from datetime import date
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from packages.contracts.schemas import condition_line_definition
from packages.domain.conditions import EvaluationContext, resolve_operand

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / 'tests/fixtures/condition_cases.json').read_text(encoding='utf-8'))
# Resolved only by the server, per duty or per row (conditions.resolvable): the client holds back nothing for them.
SERVER_ONLY = {'duty-relative-is-independent-of-today', 'relative-number-target'}

RUNNER = r"""
const [dir, payload] = process.argv.slice(1);
const input = JSON.parse(payload);
const answers = [];
globalThis.fetch = async (path, init) => {
  const body = JSON.parse(init.body), results = body.operands.map(o => answers.find(a => a.key === JSON.stringify([o.field, o.operand])).result);
  return new Response(JSON.stringify({results}), {status: 200, headers: {'Content-Type': 'application/json'}});
};
const dom = await import(new URL('dom.js', dir));
const conditions = await import(new URL('conditions.js', dir));
const table = await import(new URL('table_view.js', dir));
// No browser: the modules load in Node; then the request helper reads only the CSRF cookie.
globalThis.document = {cookie: ''};
const out = {};
for (const c of input) {
  dom.setWeekStart(c.context.week_start, c.context.timezone);
  answers.splice(0, answers.length, ...c.answers);
  const scope = {context: c.context.context || 'filter', ...(c.context.table_key ? {tableKey: c.context.table_key} : {})};
  await conditions.resolveOperands(c.answers.map(a => ({field: JSON.parse(a.key)[0], operand: JSON.parse(a.key)[1]})), scope);
  const kind = f => c.kinds[f];  // as the list's column gives it (the server's field metadata)
  const have = f => { const v = c.held[f]; return Array.isArray(v) ? v : v === null || v === undefined ? [] : [v]; };
  out[c.id] = table.holds(c.tree, have, kind, scope);
}
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope='module')
def client(client_modules):
    node = shutil.which('node')
    folder = client_modules

    return node, folder


def lines(node):
    return [x for child in node['conditions'] for x in lines(child)] if 'conditions' in node else [node]


def relative_operands(node):
    """Every relative operand of a tree, with its line's field."""
    if 'conditions' in node:
        return [x for child in node['conditions'] for x in relative_operands(child)]
    values = node.get('value') if isinstance(node.get('value'), list) else [node.get('value')]
    return [(node['field'], v) for v in values if isinstance(v, dict) and v.get('kind') == 'relative']


def answers(case, merged):
    """The server's resolve_batch answer for each relative operand of the case."""
    calendar = deepcopy(merged)
    for key in ('today', 'as_of', 'duty_day'):
        if calendar.get(key):
            calendar[key] = date.fromisoformat(calendar[key])
    if 'allowed_tokens' in calendar:
        calendar['allowed_tokens'] = frozenset(calendar['allowed_tokens'])
    context = EvaluationContext(**calendar)
    out = []
    for field, operand in relative_operands(case['tree']):
        resolved = resolve_operand(operand, context, field, row=case.get('row', case['held']))
        result = {'label': resolved['label'], 'display': resolved['display']}
        if resolved['from'] is not None:
            result.update({'from': resolved['from'], 'to': resolved['to']})
        elif isinstance(resolved['value'], (list, tuple, set, frozenset)):
            result['ids'] = list(resolved['value'])
        else:
            result['value'] = resolved['value']
        out.append({'key': json.dumps([field, operand], separators=(',', ':')), 'result': result})
    return out


def test_the_client_evaluator_agrees_with_every_shared_case(client):
    node, folder = client
    cases = []
    for case in FIXTURE['cases']:
        if case['id'] in SERVER_ONLY:
            continue
        merged = FIXTURE['context'] | case.get('context', {})
        kinds = {line['field']: (condition_line_definition(line['field'], merged.get('table_key'), line.get('op'), line.get('value')) or {}).get('value_kind')
                 for line in lines(case['tree'])}
        cases.append({'id': case['id'], 'tree': case['tree'], 'held': case['held'], 'context': merged, 'kinds': kinds,
                      'answers': answers(case, merged)})
    got = {}
    # One Node process per case: the client keeps resolved values per account and day, as in the app.
    for case in cases:
        result = subprocess.run([node, '--input-type=module', '-e', RUNNER, folder.as_uri() + '/', json.dumps([case])],
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, (case['id'], result.stderr)
        got.update(json.loads(result.stdout.strip().splitlines()[-1]))
    expected = {case['id']: case['expected'] for case in FIXTURE['cases'] if case['id'] not in SERVER_ONLY}
    assert {k: v for k, v in got.items() if v != expected[k]} == {}
    assert len(got) == len(FIXTURE['cases']) - len(SERVER_ONLY)


ORDER_RUNNER = r"""
const [dir] = process.argv.slice(1);
const held = [];
globalThis.fetch = (path, init) => new Promise(done => held.push(done));
const reply = (i, ids) => held[i](new Response(JSON.stringify({results: [{label: 'Clinics I can work at', display: '', ids}]}),
  {status: 200, headers: {'Content-Type': 'application/json'}}));
globalThis.document = {cookie: ''};
const conditions = await import(new URL('conditions.js', dir));
const mine = {kind: 'relative', token: 'my_clinics'}, scope = {context: 'filter', tableKey: 'people'};
const item = [{field: 'service_ids', operand: mine}];
const old = conditions.resolveOperands(item, scope);       // asked before an eligibility save
await new Promise(r => setTimeout(r, 0));
conditions.forgetResolved();                                // the save invalidates what is known
const fresh = conditions.resolveOperands(item, scope);
await new Promise(r => setTimeout(r, 0));
reply(1, ['clinic-b']); await fresh;                        // the fresh answer arrives first
reply(0, ['clinic-a']); await old;                          // the delayed old answer arrives last
const again = conditions.resolveOperands(item, scope);      // known: no new request
console.log(JSON.stringify({ids: conditions.resolvedNow('service_ids', mine, scope)?.ids, asked: held.length,
  failure: conditions.resolveFailure('service_ids', mine, scope)}));
await again;
"""


def test_a_reply_asked_before_an_invalidation_never_overwrites_the_fresh_answer(client):
    """A resolve request still in flight when the cache is invalidated is fenced: the fresh answer wins."""
    node, folder = client
    run = subprocess.run([node, '--input-type=module', '-e', ORDER_RUNNER, folder.as_uri() + '/'],
                         check=True, capture_output=True, text=True)
    assert json.loads(run.stdout.strip().splitlines()[-1]) == {'ids': ['clinic-b'], 'asked': 2, 'failure': ''}


def test_browser_has_one_condition_tree_walk():
    """RUL-06: the browser walks a condition tree in one place (conditions.ts treeMatches, covered by the shared cases above
    through table_view holds); the lead hint and the rules list's "Who" filter use it rather than their own walk."""
    src = ROOT / 'apps/web/src'
    walks = {p.name for p in src.glob('*.ts') if p.name != 'conditions.ts'
             and re.search(r"match===?'any'\?\w+\.(some|every)\(Boolean\)|match===?'any'\?[\w.]+conditions\.some\(", p.read_text(encoding='utf-8'))}
    assert walks == set(), walks
    assert 'treeMatches(' in (src / 'forms.ts').read_text(encoding='utf-8')
