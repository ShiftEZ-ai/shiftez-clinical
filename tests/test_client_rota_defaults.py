"""D-001: default rotas follow action capabilities, never a role-name ordering."""
import json
import subprocess

from packages.contracts.schemas import ROLE_NAMES


def test_rota_actions_and_default_order_follow_capabilities(client_modules):
    script = (
        f"const m=await import({json.dumps((client_modules / 'schedule.js').as_uri())});"
        f"const g=await import({json.dumps((client_modules / 'generated.js').as_uri())});"
        f"const roles={json.dumps(ROLE_NAMES)};"
        "const states=['draft','submitted','approved','published'];"
        "console.log(JSON.stringify({keys:m.ACTS_ON,capabilities:g.CAPABILITIES,"
        "actions:Object.fromEntries(roles.map(r=>[r,Object.fromEntries(states.map(s=>[s,m.actsOn(s,[r])]))])),"
        "defaults:Object.fromEntries(roles.map(r=>[r,m.defaultRotaStates([r])]))}));"
    )
    result = subprocess.run(['node', '--input-type=module', '-e', script],
                            capture_output=True, text=True, check=True, timeout=30)
    got = json.loads(result.stdout)
    assert got['keys'] == {'approved': ['workspace.publish'],
                           'submitted': ['ui.approver', 'ui.programme_lead'],
                           'draft': ['planning.draft']}
    for role in ROLE_NAMES:
        for state in ('draft', 'submitted', 'approved', 'published'):
            expected = any(role in got['capabilities'][key] for key in got['keys'].get(state, []))
            assert got['actions'][role][state] is expected, (role, state)
        assert got['defaults'][role] == [state for state in ('approved', 'submitted', 'draft')
                                         if got['actions'][role][state]], role
    for role, states in {'coordinator': ['approved', 'draft'], 'approver': ['approved', 'submitted'],
                         'programme_lead': ['submitted'], 'auditor': [], 'admin': []}.items():
        assert got['defaults'][role] == states, role
