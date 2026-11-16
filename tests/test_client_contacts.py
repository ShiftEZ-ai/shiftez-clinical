"""The client's contact words and visibility check (contacts.ts), in Node against the server's own disclosure filter
(services/contacts.py `permits`): for every role, mode and self/colleague case they agree, so the card offers an empty
contact for editing exactly when the server would show it. Synthetic ids only."""
import json
import subprocess

from apps.api.auth import Actor
from apps.api.services import contacts
from packages.contracts import schemas as s

MODES = [{'mode': 'everyone'}, {'mode': 'self_and_admins'}, {'mode': 'roles', 'roles': []},
         *({'mode': 'roles', 'roles': [r]} for r in s.ROLE_NAMES), {'mode': 'roles', 'roles': ['coordinator', 'admin']}]


def run(client_modules, expression, payload):
    script = (f"globalThis.document={{cookie:''}};const m=await import({json.dumps((client_modules/'contacts.js').as_uri())});"
              f"const input=JSON.parse((await import('node:fs')).readFileSync(0,'utf8'));console.log(JSON.stringify({expression}));")
    # stdin, not argv: the all-roles payload exceeds Windows' 32K command line (WinError 206)
    out = subprocess.run(['node', '--input-type=module', '-e', script], input=json.dumps(payload), capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


def test_visibility_matches_the_server_filter_for_every_role_and_mode(client_modules):
    cases = [{'roles': [role], 'person_id': me, 'visibility': v, 'target': 'person-a'}
             for role in s.ROLE_NAMES for me in ('person-a', 'person-b', None) for v in MODES]
    got = run(client_modules, 'input.map(c=>m.contactVisibleTo(c.visibility,{roles:c.roles,person_id:c.person_id},c.target))', cases)
    want = [contacts.permits(Actor('i', 'd', 'o', 'Synthetic reader', frozenset(c['roles']), c['person_id'], 's'), c['visibility'], c['target'])
            for c in cases]
    assert got == want


def test_audience_words_name_the_roles_and_every_mode(client_modules):
    got = run(client_modules, 'input.map(v=>m.contactAudience(v))',
              [{'mode': 'everyone'}, {'mode': 'self_and_admins'}, {'mode': 'roles', 'roles': ['coordinator', 'admin']}, {'mode': 'roles', 'roles': []}, None])
    assert got == ['Everyone', 'Only the person and admins', 'Coordinator, Admin', 'Nobody', 'Only the person and admins']


def test_every_contact_kind_has_exactly_one_entry_in_the_action_mapping():
    """CONTACT_KIND_ACTIONS is the one mapping the card reads: keyed by every contact kind of the contract, no more."""
    import typing
    kinds = set(typing.get_args(s.ContactTypeInput.model_fields['kind'].annotation))
    assert set(s.CONTACT_KIND_ACTIONS) == kinds
    assert set(s.CONTACT_KIND_ACTIONS.values()) <= {'call', 'email', None}
