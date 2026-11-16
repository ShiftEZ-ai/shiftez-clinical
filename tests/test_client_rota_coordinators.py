"""SW3-A-10: the client knows who the Rota Coordinators are from the server's own planning-settings reply (the real
compiled drafts.ts in Node, fed the reply the API gives a coordinator, fetch stubbed)."""
import json
import subprocess

from .test_client_autoschedule_live import GLOBALS
from .test_planning_groups_drafts import settings


def test_rota_coordinators_reads_the_real_planning_settings_reply(client_modules, h):
    settings(h)
    reply = h.get('coordinator', '/api/planning-settings')
    script = (GLOBALS + "globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}};"
              f"const reply={json.dumps(reply)};"
              "globalThis.fetch=async()=>new Response(JSON.stringify(reply),{status:200,headers:{'content-type':'application/json'}});"
              f"const d=await import({json.dumps((client_modules / 'drafts.js').as_uri())});"
              "console.log(JSON.stringify(await d.rotaCoordinators()));")
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1]) == [h.seed['identities']['admin']]


def test_only_rota_coordinator_builds_each_sentence_with_its_own_preposition(client_modules):
    """terms.ts onlyRotaCoordinator: one sentence shape, each action's phrase carrying its own preposition."""
    script = (GLOBALS + f"const terms=await import({json.dumps((client_modules / 'terms.js').as_uri())});"
              "console.log(JSON.stringify(['add','remove','close','lock','define'].map(a=>terms.onlyRotaCoordinator(a))));")
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1]) == [
        "Only the Rota Coordinator can add sessions to the period's rota.",
        "Only the Rota Coordinator can remove sessions from the period's rota.",
        "Only the Rota Coordinator can close or reopen sessions on the period's rota.",
        "Only the Rota Coordinator can lock or unlock sessions on the period's rota.",
        "Only the Rota Coordinator can change a session's type, time, place or clinics on the period's rota."]
