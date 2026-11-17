"""A complete validation delta settles the held draft as current evidence."""
import json
import subprocess

from .test_client_autoschedule_live import GLOBALS


def test_complete_check_delta_marks_the_held_workspace_current(client_modules):
    module = (client_modules / 'edit_delta.js').as_uri()
    script = (GLOBALS + f"const m=await import({json.dumps(module)});"
              "const held={workspace:{id:'w',revision:4,validation:{fingerprint:'old'},validation_current:false},"
              "shifts:[],current_fingerprint:'old'};"
              "const reply={delta:{kind:'complete_check',workspace_id:'w',revision:4,"
              "current_fingerprint:'new',validation:{fingerprint:'new',findings:[],coverage:[]}}};"
              "const value=m.applyCheckDelta(held,reply);"
              "console.log(JSON.stringify({current:value.workspace.validation_current,fingerprint:value.current_fingerprint}));")
    result = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {'current': True, 'fingerprint': 'new'}
