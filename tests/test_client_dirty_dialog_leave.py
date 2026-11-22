"""Leaving a page never discards a dialog's unsaved input (dom.ts askDirtyDialog, asked first by app.ts leaving):
a changed dialog asks its own "Close anyway?" and nothing else happens; Keep editing keeps it; Discard changes closes
it and goes on. An unchanged dialog, one whose save is under way, or a window with no form does not hold the way.
Real compiled dom.ts in Node with the small fake DOM of test_client_form_answers.py."""
import json
import subprocess

from tests.test_client_form_answers import FORM_DOM


def test_a_changed_dialog_asks_before_leaving_and_an_unchanged_one_does_not(client_modules):
    script = FORM_DOM + r"""
globalThis.window=Object.assign(new EventTarget(),{setTimeout,clearTimeout,innerHeight:900,innerWidth:1440,scrollY:0});
// The fake DOM's selectors know tags, classes, ids and bare attributes: the guard also asks [attr=value] and "X *".
const base=N.prototype.matches;
N.prototype.matches=function(s){return s.split(',').some(p=>{p=p.trim();
  if(p.endsWith(' *'))return !!this.parentElement?.closest(p.slice(0,-2));
  return [...p.matchAll(/\[([\w-]+)=([^\]]*)\]/g)].every(([,k,v])=>this.getAttribute(k)===v)&&base.call(this,p.replace(/\[[\w-]+=[^\]]*\]/g,''));});};
const dom=await import(""" + json.dumps((client_modules / 'dom.js').as_uri()) + r""");
const open=title=>{dom.formDialog(title,'',form=>{form.append(dom.area('Note','',2).wrap);return async()=>{};},'Save');
  return [...document.querySelectorAll('dialog')].at(-1);};
const change=d=>d.dispatchEvent(new Event('input'));  // the fake DOM does not bubble: the edit reaches the dialog's guard
const press=(d,name)=>d.querySelectorAll('button').find(b=>b.textContent===name).click();
const asked=d=>!!d.querySelector('.unsaved-bar');
const out={};let went=0;const go=()=>went++;

// 1. Unchanged: nothing to ask; leaving closes it as before.
const clean=open('Clean');out.clean={held:dom.askDirtyDialog(go),open:clean.isConnected,asked:asked(clean),went};
dom.closeDialogs();

// 2. Changed: it asks and holds the way; Keep editing keeps it; asked again, Discard changes closes it and goes on.
const changed=open('Changed');change(changed);
out.held={held:dom.askDirtyDialog(go),open:changed.isConnected,asked:asked(changed),went};
press(changed,'Keep editing');out.kept={open:changed.isConnected,asked:asked(changed),went};
out.again={held:dom.askDirtyDialog(go),asked:asked(changed)};
press(changed,'Discard changes');out.discarded={open:changed.isConnected,went};

// 3. A changed dialog under an unchanged one: the one above closes so the question can be answered.
const below=open('Below');change(below);const above=open('Above');
out.stacked={held:dom.askDirtyDialog(go),above:above.isConnected,below:below.isConnected,asked:asked(below)};
dom.closeDialogs();

// 4. A changed form whose save is under way (Save busy) goes on to where its save leads.
const saving=open('Saving');change(saving);saving.querySelector('button[type=submit]').setAttribute('aria-busy','true');
out.saving={held:dom.askDirtyDialog(go),asked:asked(saving)};
dom.closeDialogs();

// 5. A changed window with no form (settings kept for the session, as Print or export) closes as before.
const kept=dom.showDialog('Kept','',body=>body.append(dom.button('Move up',()=>{},'secondary')));change(kept.dialog);
out.formless={held:dom.askDirtyDialog(go),asked:asked(kept.dialog)};
console.log(JSON.stringify(out));process.exit(0);"""
    run = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    got = json.loads(run.stdout)
    assert got['clean'] == {'held': False, 'open': True, 'asked': False, 'went': 0}, got
    assert got['held'] == {'held': True, 'open': True, 'asked': True, 'went': 0}, got
    assert got['kept'] == {'open': True, 'asked': False, 'went': 0}, got
    assert got['again'] == {'held': True, 'asked': True}, got
    assert got['discarded'] == {'open': False, 'went': 1}, got
    assert got['stacked'] == {'held': True, 'above': False, 'below': True, 'asked': True}, got
    assert got['saving'] == {'held': False, 'asked': False}, got
    assert got['formless'] == {'held': False, 'asked': False}, got
