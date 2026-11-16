"""maintenance: every reversible command has Undo, from the one registry (api.ts UNDO) or, where only the screen knows the
state before (a rota link), from its own message; the governed ones without a reverse are named as such."""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'apps/web/src'
REVERSIBLE = ['availability.record', 'availability.retract', 'availability.restore', 'availability_pattern.end',
              'availability_pattern.restore', 'membership.end', 'eligibility.end', 'grant.revoke', 'membership.create',
              'grant.create', 'eligibility.extend', 'request.decide', 'request.decide_many', 'availability_pattern.decide',
              'role.grant', 'role.revoke', 'wish.create', 'wish.withdraw', 'self_approval.grant', 'review_delegation.create']
REVERSIBLE += ['membership.withdraw', 'eligibility.withdraw', 'programme_membership.withdraw']
# No reverse by nature: their dialog is governed (route, reopen) or their button armed (a revoked link).
WITHOUT = ['workspace.reopen', 'review_route.*', 'account.revoke_invite']


def test_every_reversible_command_has_undo():
    api = (SRC / 'api.ts').read_text(encoding='utf-8')
    registry = api[api.index('const UNDO:'):api.index('// A rule\'s new version names')]
    keys = set(re.findall(r"'([a-z_]+\.[a-z_.]+)':", registry)) | set(re.findall(r"'([a-z_]+\.[a-z_]+)'", registry))
    assert not [k for k in REVERSIBLE if k not in keys], [k for k in REVERSIBLE if k not in keys]
    for kind in WITHOUT:
        assert f'({kind})' in api, kind  # named in the registry's comment
    requests = (SRC / 'requests.ts').read_text(encoding='utf-8')
    link = requests[requests.index('async function linkRota'):]
    assert "label:'Undo'" in link and "command('request.link_rota',{workspace_id:before" in link
