"""A refused edit says what is wrong and what to do (D65, DL-048), from the real compiled client in Node: findings.ts
refusalText, from the one code → words and code → next-step maps that api.ts applies to every refusal's message."""
import json
import subprocess

REFUSAL = 'This edit introduces a conflict that a manual reason cannot override'
PAIRING = 'This session has no named, qualified supervisor. Assign or restore one first.'
ATTACHED = 'A trainee in this session isn’t paired with the consultant they’re attached to. Pair them first.'


def said(client_modules, message, details):
    script = (f"globalThis.document={{cookie:''}};globalThis.matchMedia=()=>({{matches:false,addEventListener(){{}}}});"
              f"const f=await import({json.dumps((client_modules/'findings.js').as_uri())});const i=JSON.parse(process.argv[1]);"
              f"console.log(JSON.stringify(f.refusalText(i.message,i.details)));")
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps({'message': message, 'details': details})],
                         capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


def found(*codes):
    return {'findings': [{'code': code, 'severity': 'error'} for code in codes]}


def test_known_code_replaces_the_generic_sentence_with_what_is_wrong_and_what_to_do(client_modules):
    assert said(client_modules, REFUSAL, found('TIME_OVERLAP', 'SUPERVISION_PAIRING')) == PAIRING
    assert said(client_modules, 'The move introduces a mandatory problem; no assignment was moved', found('SUPERVISION_PAIRING')) == PAIRING
    # Each distinct known code once, in the server's order.
    assert said(client_modules, REFUSAL, found('SUPERVISION_PAIRING', 'ATTACHMENT_PAIRING', 'SUPERVISION_PAIRING')) == f'{PAIRING} {ATTACHED}'


def test_a_specific_server_prefix_is_kept(client_modules):
    assert said(client_modules, f'This person cannot be assigned to this duty. {REFUSAL}', found('SUPERVISION_PAIRING')) == (
        f'This person cannot be assigned to this duty. {PAIRING}')


def test_unknown_or_missing_codes_keep_the_server_message_unchanged(client_modules):
    assert said(client_modules, REFUSAL, found('NO_SUCH_CODE')) == REFUSAL
    assert said(client_modules, REFUSAL, None) == REFUSAL
    assert said(client_modules, REFUSAL, [{'field': 'x', 'message': 'y'}]) == REFUSAL
    # Only a mandatory finding names the problem.
    assert said(client_modules, REFUSAL, {'findings': [{'code': 'TIME_OVERLAP', 'severity': 'warning'}]}) == REFUSAL


def test_a_generic_sentence_names_what_was_not_done_and_what_is_wrong(client_modules):
    overlap = 'Already working another duty at this time.'
    assert said(client_modules, REFUSAL, found('TIME_OVERLAP')) == f'Not saved. {overlap}'
    assert said(client_modules, 'The complete swap failed validation; neither side was saved',
                found('TIME_OVERLAP', 'UNAVAILABLE')) == f'Not exchanged. {overlap}'
    transfer = {'findings': [{'code': 'MIN_REST', 'severity': 'error', 'message': 'Rest between duties is too short'}]}
    assert said(client_modules, 'The complete duty transfer failed validation; no change was saved', transfer) == (
        'Not transferred. Rest between duties is too short.')
    assert said(client_modules, f'This person cannot be assigned to this duty. {REFUSAL}', found('TIME_OVERLAP')) == (
        f'This person cannot be assigned to this duty. Not saved. {overlap}')
    # A message that is not a generic sentence already names its problem.
    assert said(client_modules, 'Alex Morgan is on another duty then', found('TIME_OVERLAP')) == 'Alex Morgan is on another duty then'


def test_a_new_lead_or_supervision_gap_says_the_way_through(client_modules):
    move = 'The move introduces a mandatory problem; no assignment was moved'
    supervision = {'findings': [{'code': 'SUPERVISION_GAP', 'severity': 'error',
                                 'message': 'Qualified available supervision capacity is insufficient'}]}
    assert said(client_modules, move, supervision) == (
        'Qualified available supervision capacity is insufficient. Add a supervisor first, or add both together.')
    lead = {'findings': [{'code': 'LEAD_GAP', 'severity': 'error', 'message': 'Team lead requirement not met'}]}
    assert said(client_modules, REFUSAL, lead) == (
        'Team lead requirement not met. Set who leads that session first, or make both changes together.')
    # Without a message for it there is nothing to say what is wrong: the server's sentence stays.
    assert said(client_modules, REFUSAL, found('LEAD_GAP')) == REFUSAL
