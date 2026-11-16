"""maintenance: a person assigned from the fill panel leads only when the server's rota check says the session is short of a
team lead; the client never guesses a lead from who is on the session (a series lead elsewhere, no lead rule at all)."""
import json
import subprocess


def joins(client_modules, workspace, candidate, shift_id='s1'):
    script = (f"const m=await import({json.dumps((client_modules/'planner_week.js').as_uri())});"
              "const [w,c,id]=JSON.parse(process.argv[1]);console.log(JSON.stringify(m.joinsAsLead(w,id,c)));")
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps([workspace, candidate, shift_id])],
                         capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


GAP = {'code': 'LEAD_GAP', 'shift_id': 's1', 'detail': {'required': 1, 'available': 0}}
LEADER = {'person_id': 'p-a', 'can_lead': True}


def test_lead_only_on_the_servers_lead_gap(client_modules):
    # No lead requirement (no finding), or no rota check at all: no lead, even for someone who may lead.
    assert joins(client_modules, {'validation': {'findings': []}}, LEADER) is False
    assert joins(client_modules, {}, LEADER) is False
    # The server says this session is short of a lead: someone who may lead takes it; someone who may not does not.
    assert joins(client_modules, {'validation': {'findings': [GAP]}}, LEADER) is True
    assert joins(client_modules, {'validation': {'findings': [GAP]}}, {**LEADER, 'can_lead': False}) is False
    # Another session's gap, or the "more team leads than allowed" finding, never makes a lead here.
    assert joins(client_modules, {'validation': {'findings': [{**GAP, 'shift_id': 's2'}]}}, LEADER) is False
    too_many = {'code': 'LEAD_GAP', 'shift_id': 's1', 'detail': {'maximum': 1, 'scheduled': 2}}
    assert joins(client_modules, {'validation': {'findings': [too_many]}}, LEADER) is False


def test_replacing_a_lead_keeps_the_role_only_on_a_lead_gap(client_modules):
    # The leaving lead's flag is not a signal: with their lead in place the check shows no gap, so the newcomer does
    # not lead (they may be made lead from the panel); assignCandidate reads only joinsAsLead for the flag.
    assert joins(client_modules, {'validation': {'findings': []}}, LEADER) is False
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / 'apps/web/src/planner_week.ts').read_text(encoding='utf-8')
    body = source[source.index('export async function assignCandidate'):source.index('export function removeAssignment')]
    assert 'team_lead:joinsAsLead(w,shift.id,candidate)' in body and 'leaving.team_lead' not in body
