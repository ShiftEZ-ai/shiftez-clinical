"""maintenance / maintenance (DEV trace: six ~5 s serialised `workspace.baseline.refresh.preview` commands in one short session):
baseline_drift.ts decides when the page asks for a draft's drift count. A redraw or the draft's own save (a new
revision, a 'workspace' pulse) keeps the key, so nothing is asked; another draft, a new baseline or a pulse in a
department-input scope (rules, catalogue, settings) changes it once that pulse is applied; a pulse kept for later (a
popover open) changes nothing until then. The real compiled module in Node (logic only, no DOM)."""
import json
import subprocess


def keys(client_modules, steps):
    """Run steps in one module instance: ['key', draft] reads the key, ['pulse', changes, input_revision] notes a pulse
    and applies it, ['held', …] only notes it (kept for later), ['apply'] applies what was kept."""
    script = (f"const m=await import({json.dumps((client_modules/'baseline_drift.js').as_uri())});const out=[];"
              "for(const s of JSON.parse(process.argv[1])){if(s[0]==='key')out.push(m.baselineDriftKey(s[1]));else if(s[0]==='apply')m.applyDriftInputs();else{m.noteDriftInputs(s[1],s[2]);if(s[0]==='pulse')m.applyDriftInputs();}}"
              "console.log(JSON.stringify(out));")
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(steps)],
                         capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


A1 = {'id': 'rota-a', 'revision': 1, 'baseline': {'snapshot_hash': 'h1'}}
A2 = {**A1, 'revision': 2}  # the draft after its own save
B = {'id': 'rota-b', 'revision': 1, 'baseline': {'snapshot_hash': 'h1'}}


def test_redraws_and_own_saves_ask_once(client_modules):
    got = keys(client_modules, [['pulse', None, 7], ['key', A1], ['key', A1], ['key', A2],
                                ['pulse', [{'scope': 'workspace', 'id': 'rota-a'}], 7], ['key', A2],
                                ['pulse', [{'scope': 'people', 'id': None}, {'scope': 'availability', 'id': None}], 8], ['key', A2]])
    assert len(set(got)) == 1


def test_another_draft_or_a_new_baseline_asks(client_modules):
    got = keys(client_modules, [['key', A1], ['key', B], ['key', {**A1, 'baseline': {'snapshot_hash': 'h2'}}]])
    assert len(set(got)) == 3


def test_department_input_scopes_ask_again(client_modules):
    for scope in ('rules', 'catalogue', 'settings'):
        before, after = keys(client_modules, [['key', A1], ['pulse', [{'scope': scope, 'id': None}], 1], ['key', A2]])
        assert before != after, scope


def test_a_pulse_naming_nothing_asks_only_when_inputs_moved(client_modules):
    got = keys(client_modules, [['pulse', None, 5], ['key', A1], ['pulse', None, 5], ['key', A1],
                                ['pulse', None, 6], ['key', A1]])
    assert got[0] == got[1] != got[2]


def test_a_pulse_kept_for_later_asks_only_when_applied(client_modules):
    """maintenance (444ac3e): a department change pushed while the Filter popover is open is kept; a filter change under it reads
    nothing, the drift preview included. Applied (popover closed), the count is asked for again, once."""
    got = keys(client_modules, [['key', A1], ['held', [{'scope': 'catalogue', 'id': None}], 2], ['key', A1],
                                ['apply'], ['key', A1], ['apply'], ['key', A1]])
    assert got[0] == got[1] != got[2] == got[3]
