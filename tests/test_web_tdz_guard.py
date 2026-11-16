"""No client code reads a let/const/class during the synchronous run that comes before its declaration (live 9d55e1057:
every draft Week and Month failed with "Cannot access 'pillParts' before initialization"). tests/tdz_guard.mjs follows
local calls, IIFEs, array-iterator callbacks and inline callbacks a local helper calls at once; the fixtures below pin
what it reports and what it leaves alone. Callbacks handed to imported code are out of its reach (32d0d4b1f's People
table); tests/test_browser_schedule_smoke.py covers the Schedule at run time."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / 'tests/tdz_guard.mjs'


def tdz(*files):
    node = shutil.which('node')
    assert node and (ROOT / 'node_modules/typescript/package.json').is_file(), 'Node and the pinned TypeScript are required'
    result = subprocess.run([node, str(GUARD), *map(str, files)], capture_output=True, text=True, check=True, timeout=60)
    return json.loads(result.stdout)


def test_no_client_module_reads_a_declaration_before_it_runs():
    found = tdz(*sorted((ROOT / 'apps/web/src').glob('*.ts')))
    assert found == [], '\n'.join(f"{Path(f['file']).name}:{f['line']} reads `{f['name']}` before its declaration "
                                  f"(via {f['read']}); declare it before the code that runs this" for f in found)


CASES = {
    # 9d55e1057's shape: the draw runs a hoisted helper that hands a local runner a callback reading a later const.
    'month_bands': ("""export function grid(xs:number[]){
  function drawnMarks<T>(build:()=>T):T{return build();}
  function cell(x:number){return xs.map(()=>pillParts(x));}
  function bands(){return drawnMarks(()=>cell(1));}
  bands();
  const pillParts=(x:number)=>x+1;
}""", {'pillParts'}),
    'direct_iterator': ("""export function f(xs:number[]){
  const out=xs.map(x=>later+x);
  const later=1;return out;
}""", {'later'}),
    'iife': ("""export function f(){
  const a=(()=>b)();
  const b=1;return a;
}""", {'b'}),
    # Read later: an event handler, a stored callback, a callback given to imported code.
    'deferred': ("""import {button} from './dom.js';
export function f(node:HTMLElement){
  node.addEventListener('click',()=>later);
  const run=()=>later;const b=button('Go',()=>later);
  const later=1;return [run,b];
}""", set()),
    # A parameter or inner declaration of the same name is not the later one.
    'shadowed': ("""export function f(xs:number[]){
  const a=xs.map(later=>later+1);
  function g(later:number){return later;}g(1);
  const later=1;return a;
}""", set()),
    # Properties, type positions and import aliases are not reads.
    'not_reads': ("""import {x as later0} from './dom.js';
export function f(o:{later:number}){
  const a=o.later,b={later:1};type T=typeof later0;
  const later=1;return [a,b,later];
}""", set()),
}


@pytest.mark.parametrize('name', CASES)
def test_the_guard_reports_reads_that_run_before_the_declaration_and_nothing_else(tmp_path, name):
    source, expected = CASES[name]
    path = tmp_path / f'{name}.ts'
    path.write_text(source, encoding='utf-8')
    assert {f['name'] for f in tdz(path)} == expected
