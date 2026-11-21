"""PERF remote-watcher (1f08c8059) + LEAD-UIUX, 8 Nov 2026: another person's edit carried on its workspace pulse is
applied through the edit reply's own path (edit_delta.ts remoteEdit, then applyEditDelta via app.ts patchDraft) only
when the held draft is at the delta's base revision: drawn from memory, no read. A different base, a partial delta
(no scoped findings or coverage) or a windowed copy give null: the caller reads the rota whole, once. The real
compiled module in Node (logic only, no DOM)."""
import json
import subprocess


def run(client_modules, held, change):
    script = (f"const m=await import({json.dumps((client_modules/'edit_delta.js').as_uri())});const a=JSON.parse(process.argv[1]);"
              "const r=m.remoteEdit(a.change);const p=r?m.applyEditDelta(a.held,r):null;"
              "console.log(JSON.stringify({remote:!!r,applied:p?{revision:p.workspace.revision,people:p.shifts.map(s=>s.participants.length)}:null}));")
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps({'held': held, 'change': change})],
                         capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


def held(revision=4, partial=False):
    return {'workspace': {'id': 'w', 'revision': revision, 'validation': {'findings': [], 'coverage': []}},
            'shifts': [{'id': 's1', 'start': '2026-10-04T08:00:00+03:00', 'participants': [{'id': 'a1', 'person_id': 'p1'}]}],
            **({'partial': True} if partial else {})}


def change(base=4, findings=True, coverage=True):
    delta = {'base_revision': base, 'revision': 5, 'current_fingerprint': 'f',
             'shifts': [{'id': 's1', 'start': '2026-10-04T08:00:00+03:00',
                         'participants': [{'id': 'a1', 'person_id': 'p1'}, {'id': 'a2', 'person_id': 'p2'}]}],
             **({'findings': {'shift_ids': ['s1'], 'items': []}} if findings else {}),
             **({'coverage': {'shift_ids': ['s1'], 'items': []}} if coverage else {})}
    return {'scope': 'workspace', 'id': 'w', 'delta': delta, 'workspace': {'id': 'w', 'revision': 5, 'content_revision': 3},
            'health': {'summary': {}}, 'validation': {}, 'fix_list_delta': {}}


def test_a_matching_base_applies_the_remote_edit_with_no_read(client_modules):
    assert run(client_modules, held(4), change(4)) == {'remote': True, 'applied': {'revision': 5, 'people': [2]}}


def test_a_different_base_is_read_whole(client_modules):
    assert run(client_modules, held(3), change(4)) == {'remote': True, 'applied': None}


def test_a_partial_delta_or_a_windowed_copy_is_read_whole(client_modules):
    assert run(client_modules, held(4), change(4, coverage=False)) == {'remote': False, 'applied': None}
    assert run(client_modules, held(4), change(4, findings=False)) == {'remote': False, 'applied': None}
    assert run(client_modules, held(4, partial=True), change(4)) == {'remote': True, 'applied': None}


def test_edit_and_check_deltas_match_the_schedule_representation(client_modules):
    edit = json.dumps((client_modules / 'edit_delta.js').as_uri())
    contracts = json.dumps((client_modules / 'generated.js').as_uri())
    script = '''
import assert from 'node:assert/strict';
const {applyCheckDelta,applyEditDelta}=await import(EDIT_MODULE);
const {SCHEDULE_DETAIL_FIELDS:fields}=await import(CONTRACTS_MODULE);
const detail=keys=>Object.fromEntries(keys.map(key=>[key,{retained:'server evidence'}]));
const participant={id:'a',person_id:'p',override_reason:'Retained admission'};
const prior={id:'s',start:'2026-10-04T08:00:00+00:00',participants:[participant]};
const edited={...prior,locked:true};
const coverage={shift_id:'s',required:1};
const validation={findings:[],coverage:[coverage],fingerprint:'new'};
const held={workspace:{id:'w',revision:4,validation:{...validation,fingerprint:'old'}},
    shifts:[prior],current_fingerprint:'old'};
const fresh={...held,workspace:{id:'w',revision:5,validation},shifts:[edited],current_fingerprint:'new'};
const assignment={...participant,...detail(fields.participant),shift_id:'s'};
const shift={...edited,...detail(fields.session),participants:[assignment]};
const reply={workspace:{id:'w',revision:5},validation:{fingerprint:'new',...detail(fields.check)},
    delta:{base_revision:4,revision:5,current_fingerprint:'new',shifts:[shift],
        findings:{shift_ids:['s'],items:[]},coverage:{shift_ids:['s'],items:[{...coverage,...detail(fields.coverage)}]}}};
assert.deepEqual(JSON.parse(JSON.stringify(applyEditDelta(held,reply))),fresh);
assert.equal(shift.series_snapshot.retained,'server evidence');
const {shifts,...delta}=reply.delta;
assert.deepEqual(JSON.parse(JSON.stringify(applyEditDelta(held,{...reply,delta:{...delta,assignments:[assignment]}}))),
    {...fresh,shifts:[prior]});
const check={delta:{kind:'complete_check',workspace_id:'w',revision:5,current_fingerprint:'new',
    validation:{...validation,...detail(fields.check),coverage:reply.delta.coverage.items}}};
assert.deepEqual(applyCheckDelta(fresh,check),fresh);
'''
    subprocess.run(['node', '--input-type=module', '-e', script.replace('EDIT_MODULE', edit)
                    .replace('CONTRACTS_MODULE', contracts)], capture_output=True, text=True, check=True, timeout=15)


def test_an_edit_delta_adding_a_new_assignment_lands_in_its_session(client_modules):
    # Design's check: an assignment-only delta with a new id joins its held session in the Schedule representation.
    script = '''
import assert from 'node:assert/strict';
const {applyEditDelta}=await import(EDIT_MODULE);
const {SCHEDULE_DETAIL_FIELDS:fields}=await import(CONTRACTS_MODULE);
const detail=keys=>Object.fromEntries(keys.map(key=>[key,'server evidence']));
const kept={id:'a1',person_id:'p1',override_reason:null};
const other={id:'s2',start:'2026-10-05T08:00:00+00:00',participants:[]};
const held={workspace:{id:'w',revision:4,validation:{findings:[],coverage:[]}},
    shifts:[{id:'s1',start:'2026-10-04T08:00:00+00:00',participants:[kept]},other],current_fingerprint:'old'};
const added={id:'a2',person_id:'p2',start:'2026-10-04T08:00:00+00:00',end:'2026-10-04T12:00:00+00:00',override_reason:null};
const reply={workspace:{id:'w',revision:5},delta:{base_revision:4,revision:5,current_fingerprint:'new',
    assignments:[{...added,...detail(fields.participant),shift_id:'s1'}],
    findings:{shift_ids:['s1'],items:[]},coverage:{shift_ids:['s1'],items:[]}}};
const next=JSON.parse(JSON.stringify(applyEditDelta(held,reply)));
assert.deepEqual(next.shifts,[{id:'s1',start:'2026-10-04T08:00:00+00:00',participants:[kept,added]},other]);
assert.equal(next.workspace.revision,5);
'''
    subprocess.run(['node', '--input-type=module', '-e', script
                    .replace('EDIT_MODULE', json.dumps((client_modules / 'edit_delta.js').as_uri()))
                    .replace('CONTRACTS_MODULE', json.dumps((client_modules / 'generated.js').as_uri()))],
                   capture_output=True, text=True, check=True, timeout=15)
