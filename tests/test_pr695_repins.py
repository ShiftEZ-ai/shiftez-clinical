"""Cheap maintenance repins: client edit gates match the single-edit command roles."""
import json
from pathlib import Path

import pytest

from apps.api.commands import command_roles
from packages.contracts import schemas as s


def _single_edit_roles():
    expected = {}
    for key, collection in s.BULK_COLLECTIONS.items():
        action = next((collection['actions'].get(name) for name in ('edit', 'update', 'update_dates', 'propose')
                       if collection['actions'].get(name)), None)
        if not action or not collection['fields']:
            continue
        payload = action.get('payload') or {'catalogue': key}
        expected[key] = sorted(command_roles(s.Command(kind=action['kind'], payload=payload)))
    return expected


def test_bulk_editable_kinds_match_single_edit_roles_for_every_role():
    """The generated Sheet and every single-edit gate use command-bus roles, for all ROLE_NAMES."""
    expected = _single_edit_roles()
    source = (Path(__file__).resolve().parents[1] / 'apps' / 'web' / 'src' / 'generated.ts').read_text(encoding='utf-8')
    line = next(line for line in source.splitlines() if line.startswith('export const BULK_COLLECTIONS = '))
    generated = json.loads(line.removeprefix('export const BULK_COLLECTIONS = ').removesuffix(' as const;'))
    for kind, roles in expected.items():
        action = next(name for name in ('edit', 'update', 'update_dates', 'propose')
                      if name in generated[kind]['actions'])
        actual = sorted(generated[kind]['actions'][action]['roles'])
        assert actual == roles, kind
        assert {role: role in actual for role in s.ROLE_NAMES} == {role: role in roles for role in s.ROLE_NAMES}


@pytest.mark.skip(reason='NOT_RUN on PC: coverPreview is a browser-bound closure; run this Change dates spec in CI browser')
def test_change_dates_arms_only_for_removed_days_and_counts_only_removed_window():
    """Browser spec: lengthening is one-click and unarmed; shortening arms only for cover in removed days."""
    pass
