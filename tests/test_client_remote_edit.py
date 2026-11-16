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
