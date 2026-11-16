"""Undo of a display-settings save puts back a setting that was still at its default (names_default, the Rules page as
the catalogue). The reply's `previous` lists only the settings that were stored, so re-sending it as a new save left a
default-valued setting at the new value; the server's own undo (department.display.restore of the version before)
restores every field. The client's Undo is that served command (api.ts UNDO['department.display'])."""
import re
from pathlib import Path

API = Path(__file__).resolve().parents[1] / 'apps/web/src/api.ts'


def test_the_served_undo_restores_a_default_valued_setting_where_resending_previous_did_not(h):
    department = h.get('admin', '/api/me')['department']
    assert 'names_default' not in (department.get('display_settings') or {})  # still at its default ('full')
    saved = h.command('admin', 'department.display', {'names_default': 'code', 'rule_catalogue': True, 'reason': 'Change'},
                      department['id'], department['revision'])
    # The old Undo: `previous` as a new save. It does not name the two settings, so they stay changed.
    resent = h.command('admin', 'department.display', {**saved['previous'], 'reason': 'Undo'},
                       saved['department']['id'], saved['department']['revision'])
    after = h.get('admin', '/api/me')['department']['display_settings']
    assert (after['names_default'], after['rule_catalogue']) == ('code', True)
    # The served Undo of that save restores the version before it: both back to their defaults.
    undo = resent['undo']
    h.command('admin', undo['kind'], undo['payload'], undo['target_id'], undo['expected_revision'])
    undo = saved['undo']
    current = h.get('admin', '/api/me')['department']
    h.command('admin', undo['kind'], undo['payload'], undo['target_id'], current['revision'])
    restored = h.get('admin', '/api/me')['department']['display_settings']
    assert (restored['names_default'], restored['rule_catalogue']) == ('full', False)


def test_the_client_undo_of_a_display_save_is_the_served_restore():
    line = next(line for line in API.read_text(encoding='utf-8').splitlines() if line.strip().startswith("'department.display':"))
    assert re.search(r"undo:served\(x\)\.length\?served\(x\)", line), line
