"""D-074: Add session's Clinics field is required exactly when the server requires a service (planning.py
SERVICE_REQUIRED: a staffed duty; TypeDefinition.purpose defaults to staffed). Real compiled planner_logic.ts in Node."""
import json
import subprocess


def test_a_clinic_is_needed_exactly_for_a_staffed_duty(client_modules):
    module = (client_modules / 'planner_logic.js').as_uri()
    script = f"""const m=await import({json.dumps(module)});
const t=p=>({{definition:p===undefined?{{}}:{{purpose:p}}}});
console.log(JSON.stringify([m.needsClinic(t('staffed')),m.needsClinic(t(undefined)),m.needsClinic(undefined),m.needsClinic(t('group')),m.needsClinic(t('protected'))]));"""
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [True, True, True, False, False]
