"""Focused source and truth-table checks for the web permission SSOT."""
import json
import re

from packages.contracts import schemas as s
from scripts.check_web_permission_literals import findings
from scripts.generate_contracts import render


def _generated_capabilities() -> dict[str, list[str]]:
    source = render()[0]
    match = re.search(r"export const CAPABILITIES = (.+) as const;", source)
    assert match, "generated contracts must export CAPABILITIES"
    return json.loads(match.group(1))


def test_web_permission_gates_have_no_raw_role_literals():
    assert findings() == []


def test_capability_map_preserves_every_authoritative_action_truth_table():
    generated = _generated_capabilities()
    assert generated == {key: list(roles) for key, roles in s.CAPABILITIES.items()}

    # Before: server action permissions. After: the shared capability map consumed by the web
    # helper. Every role/action cell must remain identical; ui.* aliases add only named gates.
    for action in s.PERMISSION_NAMES:
        before = {role: role in s.PERMISSIONS[action] for role in s.ROLE_NAMES}
        after = {role: role in s.CAPABILITIES[action] for role in s.ROLE_NAMES}
        assert after == before, action
    for role in s.ROLE_NAMES:
        assert s.CAPABILITIES[f"ui.{role}"] == (role,)
        truth = {candidate: candidate == role for candidate in s.ROLE_NAMES}
        assert {candidate: candidate in s.CAPABILITIES[f"ui.{role}"] for candidate in s.ROLE_NAMES} == truth
