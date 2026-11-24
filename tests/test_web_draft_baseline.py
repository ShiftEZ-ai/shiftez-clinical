"""Draft Snapshot dialogs (New rota, Refresh draft baseline) and B014's terminal job states, in Node: no browser."""
import json
import re
import subprocess
from pathlib import Path
from typing import get_args

import pytest

from packages.contracts import schemas as s

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / 'apps/web/src'


def node(client_modules, body):
    script = (f"const DIR={json.dumps(str(client_modules))};const {{pathToFileURL}}=await import('node:url');"
              "const mod=f=>import(pathToFileURL(DIR+'/'+f).href);" + body)
    run = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


LIVE = {'people', 'calendars'}  # live in every draft: the server overlays them and never reports their drift
FREEZABLE = [x for x in s.BASELINE_SOURCE_NAMES if x not in LIVE]


def test_baseline_groups_are_the_freezable_list(client_modules):
    out = node(client_modules, "const m=await mod('draft_baseline.js');console.log(JSON.stringify(m.BASELINE_SOURCES.map(x=>x[0])));")
    assert out == FREEZABLE, out  # people and calendars stay live: never offered
    assert 'Calendars and availability' not in (WEB / 'forms.ts').read_text(encoding='utf-8')
    assert 'People and memberships' not in (WEB / 'draft_baseline.ts').read_text(encoding='utf-8')


def test_refresh_offers_a_decision_only_for_conflicts_and_apply_only_for_department_changes(client_modules):
    out = node(client_modules, """const m=await mod('draft_baseline.js');
const plans=[[],[{status:'draft'}],[{status:'department'},{status:'same'}],[{status:'conflict'},{status:'draft'}]].map(m.refreshPlan);
const labels=[{status:'conflict',safety:true},{status:'conflict',safety:false},{status:'department',safety:true},
  {status:'conflict',safety:true,mandatory:true},{status:'conflict',safety:true,mandatory:false}].map(m.changeLabel);
console.log(JSON.stringify({plans,labels,keep:m.SAFETY_KEEP,decided:m.SAFETY_DECIDED,undecided:m.UNDECIDED}));""")
    none, draft_only, plain, conflicts = out['plans']
    empty = 'No department changes since this draft was created'
    assert none == draft_only == {'empty': empty, 'decide': False, 'apply': False}, out
    assert plain == {'empty': '', 'decide': False, 'apply': True} and conflicts == {'empty': '', 'decide': True, 'apply': True}, out
    # Issue 1030: a conflict between advisory rules only says so; one with a mandatory or safety rule keeps its label.
    assert out['labels'] == ['Conflict · mandatory safety', 'Conflict', 'Department changed',
                             'Conflict · mandatory safety', 'Conflict · advisory'], out
    assert out['keep'] == 'mandatory safety rules stay with the department'
    assert out['decided'] == 'Use department · mandatory safety rules stay with the department'
    assert out['undecided'] == 'Choose an outcome for each conflict'


# D-068: the owner ruled helper text out of exactly these dialogs; system words never reach them.
GONE = ['Review the frozen snapshot', 'The preview is read-only until', 'No source groups have changed',
        'Refresh the draft baseline after reviewing', 'frozen snapshot', 'source group', 'department inputs',
        'Nothing has changed since', 'No conflicts to decide']


def test_draft_dialogs_carry_no_helper_text():
    text = ''.join((WEB / f).read_text(encoding='utf-8') for f in ('app.ts', 'forms.ts', 'draft_baseline.ts'))
    for words in GONE:
        assert words.lower() not in text.lower(), words


def test_uncovered_dates_nearest_first(client_modules):
    out = node(client_modules, """const m=await mod('draft_baseline.js');
const gaps=[{starts_on:'2026-01-01',ends_on:'2026-01-05'},{starts_on:'2026-03-20',ends_on:'2026-03-22'},
  {starts_on:'2026-02-25',ends_on:'2026-03-01'},{starts_on:'2026-03-09',ends_on:'2026-03-10'}];
console.log(JSON.stringify({order:m.nearestGaps(gaps,'2026-03-01','2026-03-08').map(g=>g.starts_on),shown:m.GAPS_SHOWN,same:gaps[0].starts_on}));""")
    assert out['order'] == ['2026-02-25', '2026-03-09', '2026-03-20', '2026-01-01'], out
    assert out['shown'] == 2 and out['same'] == '2026-01-01', out  # never sorts the caller's list


def test_draft_dialogs_use_the_shared_controls():
    forms, app = (WEB / 'forms.ts').read_text(encoding='utf-8'), (WEB / 'app.ts').read_text(encoding='utf-8')
    new_rota = forms[forms.index('Draft creation freezes'):forms.index("'Create draft');")]
    refresh = app[app.index('async function refreshDraftBaseline('):app.index('async function captureDraftBaseline(')]
    for part in (new_rota, refresh):
        assert not re.search(r"class:'[^']*\b(choice|baseline-)", part)  # design-system helpers only
        assert "el('select'" not in part and "type:'radio'" not in part
    # Routine fields first; the baseline is the last, quiet section, its custom scope behind Advanced.
    assert new_rota.index('form.append(baseline)') > new_rota.index('automate.wrap);')
    assert "el('summary',{},'Advanced')" in new_rota
    assert "nearestGaps(" in new_rota and 'GAPS_SHOWN' in new_rota
    # Refresh: no boilerplate description, no hash shown, an empty required reason and the shared select.
    assert "formDialog('Refresh draft baseline',''" in refresh and 'snapshot_hash}' not in refresh
    assert "area('Reason','')" in refresh and 'reason.input.required=true' in refresh
    assert 'select(`Decision for' in refresh and 'if(!plan.apply)' in refresh
    # Apply waits until every conflict is decided, saying why beside it (no explanatory paragraph).
    assert 'b.disabled=open' in refresh and "aria-describedby',why.id" in refresh and 'UNDECIDED' in refresh
    assert 'note(plan.empty' in refresh and refresh.count('note(') == 1
    # A safety conflict has one outcome: said as text and sent as use_department, never a one-value picker.
    assert "if(item.safety){fixed[item.source]='use_department';return SAFETY_DECIDED;}" in refresh
    assert 'disabled:true' not in refresh and 'decisions:{...fixed,' in refresh
    # New rota: the heading says Advanced once; the plural agrees; no recurring-sessions line under Start from.
    assert 'Advanced · Custom scope' not in forms and "plural(n,'group')" in new_rota and "'it depends':'they depend'" in new_rota
    assert "missingSetup(ctx,'recurring_series')" not in forms and "hint:'None set up yet · Configuration'" in forms


TERMINAL = [x for x in get_args(s.CatalogueProofJob.model_fields['status'].annotation) if x not in ('queued', 'running')]
WAIT = """
globalThis.window={setTimeout:f=>setTimeout(f,0),clearTimeout,addEventListener(){},removeEventListener(){},dispatchEvent:()=>true};
globalThis.Event=class{constructor(t){this.type=t;}};globalThis.location={hash:''};globalThis.history={pushState(){},replaceState(){}};
globalThis.document={cookie:''};
let reads=0;globalThis.fetch=async()=>{reads++;return new Response(JSON.stringify({job:{id:'j',status:reads<2?'running':FINAL}}),{status:200});};
const api=await mod('api.js');
const followed=await api.awaitJob({id:'j',status:'queued'},{every:1});const after={reads,watched:api.watchedJobs()};
reads=0;const already=await api.awaitJob({id:'k',status:FINAL},{every:1});
console.log(JSON.stringify({followed:followed?.status,after,already:already?.status,alreadyReads:reads,watched:api.watchedJobs()}));
"""


@pytest.mark.parametrize('final', TERMINAL)
def test_every_terminal_job_status_ends_the_wait(client_modules, final):
    """B014, generic: whatever terminal status the contract adds, a wait ends on it and releases its watch."""
    out = node(client_modules, f"const FINAL={json.dumps(final)};" + WAIT)
    assert out['followed'] == final and out['after'] == {'reads': 2, 'watched': []}, out
    assert out['already'] == final and out['alreadyReads'] == 0 and out['watched'] == [], out
