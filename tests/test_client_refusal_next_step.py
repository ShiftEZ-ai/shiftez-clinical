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
    assert said(client_modules, REFUSAL, found('TIME_OVERLAP')) == REFUSAL
    assert said(client_modules, REFUSAL, None) == REFUSAL
    assert said(client_modules, REFUSAL, [{'field': 'x', 'message': 'y'}]) == REFUSAL
