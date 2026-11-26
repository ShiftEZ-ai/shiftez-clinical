"""Staff onboarding's client logic in Node from the compiled client: the sign-in name it proposes from a work email is
one the Username contract accepts, and the private link file is a spreadsheet that cannot run a formula."""
import csv
import io
import json
import subprocess

import pytest
from pydantic import TypeAdapter, ValidationError

from packages.contracts.schemas import Username


def run(client_modules, expression, payload=None):
    script = (f"globalThis.document={{cookie:'',addEventListener(){{}}}};globalThis.addEventListener=()=>{{}};const m=await import({json.dumps((client_modules / 'accounts.js').as_uri())});"
              f"const input=JSON.parse(process.argv[1]);console.log(JSON.stringify({expression}));")
    result = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(payload)],
                            capture_output=True, text=True, check=True, timeout=15)
    return json.loads(result.stdout)


@pytest.mark.parametrize('email,expected', [
    ('Staff.One@example.test', 'staff.one'),
    ('staff+rota@example.test', 'staffrota'),
    ('_.-staff-two@example.test', 'staff-two'),  # the contract's first character is a letter or digit
    ('a@example.test', ''),  # one character is below the contract's minimum
    ('x' * 80 + '@example.test', 'x' * 63),
    ('', ''),
])
def test_proposed_sign_in_name_fits_the_username_contract(client_modules, email, expected):
    got = run(client_modules, 'm.usernameFrom(input)', email)
    assert got == expected
    if got:
        TypeAdapter(Username).validate_python(got)
    else:
        with pytest.raises(ValidationError):
            TypeAdapter(Username).validate_python(email.split('@')[0].lower())


def test_link_file_lists_each_person_once_and_neutralises_formulas(client_modules):
    rows = [{'person_id': 'p1', 'person_label': 'Staff One', 'one_time_link': 'https://app.example.test/invite/one'},
            {'person_id': 'p2', 'person_label': '=HYPERLINK("x")', 'one_time_link': 'https://app.example.test/invite/two'}]
    sign_in_names = [['p2', '002'], ['p1', '02']]
    text = run(client_modules, 'm.invitationsCsv(input.rows,new Map(input.names))', {'rows': rows, 'names': sign_in_names})
    assert text.startswith('\ufeff')  # Excel reads it as UTF-8
    parsed = list(csv.reader(io.StringIO(text[1:])))
    assert parsed[0] == ['Person', 'One-time link']
    assert [r[1] for r in parsed[1:]] == [r['one_time_link'] for r in rows]
    assert parsed[1][0] == 'Staff One (02)'
    assert parsed[2][0] == '\'=HYPERLINK("x") (002)'
