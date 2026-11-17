"""Both the effective previous settings and the served version restore put back display defaults.
The client prefers the served revision-bound restore command."""
import re
from pathlib import Path

API = Path(__file__).resolve().parents[1] / 'apps/web/src/api.ts'


def test_previous_settings_and_served_undo_restore_default_valued_settings(h):
    department = h.get('admin', '/api/me')['department']
    assert 'names_default' not in (department.get('display_settings') or {})  # still at its default ('full')
    saved = h.command('admin', 'department.display', {'names_default': 'code', 'rule_catalogue': True, 'reason': 'Change'},
                      department['id'], department['revision'])
    # The compatibility save must restore settings that had no stored value.
    resent = h.command('admin', 'department.display', {**saved['previous'], 'reason': 'Undo'},
                       saved['department']['id'], saved['department']['revision'])
    after = h.get('admin', '/api/me')['department']['display_settings']
    assert (after['names_default'], after['rule_catalogue']) == ('full', False)
    # Each served Undo restores its recorded version, including the original defaults.
    undo = resent['undo']
    intermediate = h.command('admin', undo['kind'], undo['payload'], undo['target_id'], undo['expected_revision'])['department']['display_settings']
    assert (intermediate['names_default'], intermediate['rule_catalogue']) == ('code', True)
    undo = saved['undo']
    current = h.get('admin', '/api/me')['department']
    h.command('admin', undo['kind'], undo['payload'], undo['target_id'], current['revision'])
    restored = h.get('admin', '/api/me')['department']['display_settings']
    assert (restored['names_default'], restored['rule_catalogue']) == ('full', False)


def test_the_client_undo_of_a_display_save_is_the_served_restore():
    line = next(line for line in API.read_text(encoding='utf-8').splitlines() if line.strip().startswith("'department.display':"))
    assert re.search(r"undo:served\(x\)\.length\?served\(x\)", line), line
