"""maintenance (maintenance): a settings form saves on the revision its shown values were read at (settings_base.ts drawnBase), never
one read again at the click. A background refresh replaces the account (`me=account`, app.ts refreshOnce) before it
decides not to redraw a form in use; a revision read from `me` at the click then belongs to values the form never
showed, the server's stale check passes, and the form's old values overwrite the other person's save without a word.
The real compiled module in Node (no DOM), the two forms' wiring in app.ts, and the server's side of both outcomes."""
import json
import re
import subprocess
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / 'apps/web/src/app.ts'


def test_the_save_sends_the_revision_the_form_was_drawn_at_after_the_account_is_replaced(client_modules):
    script = (f"const m=await import({json.dumps((client_modules/'settings_base.js').as_uri())});"
              "let me={department:{id:'d1',revision:5}};const base=m.drawnBase(me.department);"
              # The refresh replaces the account with the server's newer one while the form stays as drawn.
              "me={department:{id:'d1',revision:6}};const replaced=[base.id,base.revision,me.department.revision];"
              # A reply patched into the same object (settingsSaved, independentSaved) does not move it either.
              "const kept={id:'d1',revision:5},b=m.drawnBase(kept);kept.revision=7;"
              "console.log(JSON.stringify({replaced,patched:b.revision}));")
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, check=True, timeout=15)
    # Read at the click the save would carry 6 (the last number): the drawn base still says 5, which the server refuses.
    assert json.loads(out.stdout) == {'replaced': ['d1', 5, 6], 'patched': 5}


def test_forms_that_send_drawn_display_values_save_on_their_drawn_base():
    source = APP.read_text(encoding='utf-8')
    # The two department.display saves whose payload is what the form showed (the whole draft; both Where is everyone
    # numbers). The single-switch saves send only the value just chosen, which the server merges by field.
    saves = [line for line in source.splitlines() if "command('department.display',{...draft" in line
             or "command('department.display',{whereabouts:want" in line]
    assert len(saves) == 2
    for line in saves:
        assert re.search(r"reason\},base\.id,base\.revision\)", line), line
        assert 'me!' not in line.split('notify(')[0], line
    assert source.count('const base=drawnBase(me!.department)') == 2
    # A refusal is said on the page with Reload (settingsMoved), not left to the dialog's bare server words.
    assert source.count('settingsMoved(e,conflict,') == 2


def test_the_server_refuses_the_drawn_revision_and_would_overwrite_on_the_newer_one(h):
    drawn = h.get('admin', '/api/me')['department']                       # admin A opens Display defaults
    shown = {'names_default': 'full', 'ignore_al_prefix': True, 'avoid_duplicate_codes': True, 'tags': []}
    theirs = h.command('admin', 'department.display', {**shown, 'names_default': 'code', 'reason': 'B changes names'},
                       drawn['id'], drawn['revision'])['department']      # admin B saves meanwhile
    mine = {**shown, 'avoid_duplicate_codes': False, 'reason': 'A saves the form as drawn'}
    # The fix: on the revision A's form was drawn at, the save is refused and B's change stands.
    refused = h.command('admin', 'department.display', mine, drawn['id'], drawn['revision'], status=409)
    assert 'STALE_REVISION' in json.dumps(refused)
    assert h.get('admin', '/api/me')['department']['display_settings']['names_default'] == 'code'
    # The defect: the same form sent on the refreshed account's revision passes and takes B's change back.
    h.command('admin', 'department.display', mine, drawn['id'], theirs['revision'])
    assert h.get('admin', '/api/me')['department']['display_settings']['names_default'] == 'full'
