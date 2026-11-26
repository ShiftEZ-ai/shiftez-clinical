"""maintenance/maintenance: who may give which role comes from the contract, the same answer the server gives.

role_grants.ts grantRefusal runs here in Node from the compiled client. For each holder of each role it must agree
with the server's own check (apps/api/services/access.py require_role_change): '' exactly when the server allows the
grant, otherwise the reason in words. Access & approvals lists every role through it, never a hand-written list, and an
invitation always sends a reason."""
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.api.services.access import require_role_change
from packages.contracts.schemas import ROLE_LABELS, ROLE_NAMES

SRC = Path(__file__).resolve().parents[1] / 'apps/web/src'


@pytest.fixture(scope='module')
def refusals(client_modules):
    """For every holder role: {role: refusal} from the compiled client, in one Node run."""
    script = (f"const g=await import({json.dumps((client_modules / 'role_grants.js').as_uri())});"
              f"const names={json.dumps(list(ROLE_NAMES))};"
              "console.log(JSON.stringify(Object.fromEntries(names.map(h=>[h,Object.fromEntries(names.map(r=>[r,g.grantRefusal(r,[h])]))]))));")
    out = subprocess.run([shutil.which('node'), '--input-type=module', '-e', script], capture_output=True, text=True,
                         check=True, timeout=30)
    return json.loads(out.stdout)


def server_allows(holder, role):
    actor = SimpleNamespace(roles={holder}, permits=lambda *roles: holder in roles)
    try:
        require_role_change(None, actor, role, grant=True)
        return True
    except Exception as refused:  # the server's 403 (FORBIDDEN or SYSTEM_ROLE_GRANT)
        assert getattr(refused, 'status_code', 403) == 403
        return False


@pytest.mark.parametrize('holder', ROLE_NAMES)
def test_the_client_refuses_exactly_what_the_server_refuses(refusals, holder):
    for role in ROLE_NAMES:
        said = refusals[holder][role]
        assert (said == '') == server_allows(holder, role), (holder, role, said)
        if said:
            assert re.fullmatch(r'Only an? [A-Z][\w ]+ can grant this', said), said


def test_an_admin_is_told_who_gives_auditor_and_developer(refusals):
    assert refusals['admin']['auditor'] == refusals['admin']['operator'] == f"Only a {ROLE_LABELS['operator']} can grant this"
    assert refusals['operator']['coordinator'] == f"Only an {ROLE_LABELS['admin']} can grant this"


def test_access_page_lists_every_role_through_the_shared_rule():
    app = (SRC / 'app.ts').read_text(encoding='utf-8')
    grants = (SRC / 'role_grants.ts').read_text(encoding='utf-8')
    assert "from './generated.js'" in grants and 'ROLE_GRANT_PERMISSIONS' in grants and 'PERMISSIONS[' in grants
    # No second copy of the rule in the page, and every role picker starts from the contract's ROLE_NAMES.
    assert 'EXPLICIT_ROLES' not in app
    assert "const manages=(r:string)=>!grantRefusal(r,me!.roles);" in app
    assert 'inviteDialog(peopleChoices(ctx()),refresh,roleChoices(ROLE_NAMES))' in app
    assert len(re.findall(r'roleForm\(form,roleChoices\(ROLE_NAMES', app)) == 2  # Give a role: one person, several
    assert 'grantable(' not in app


def test_an_invitation_requires_and_sends_a_reason():
    accounts = (SRC / 'accounts.ts').read_text(encoding='utf-8')
    invite = accounts[accounts.index('export function inviteDialog'):accounts.index('/** Session limits')]
    assert "reason=area('Reason');reason.input.required=true;" in invite
    assert 'reason:reason.input.value.trim()' in invite
