"""Both amendment entry points consume one server-target navigation rule, without guessing clone lineage."""
import json
import subprocess
from urllib.parse import parse_qs, urlsplit


def test_amendment_routes_use_only_a_proved_target_and_explain_missing_matches(client_modules):
    base = {'key': 'amend_publication', 'publication_id': 'publication', 'workspace_id': 'issued',
            'shift_id': 'old-issued-session', 'amendment_workspace_id': 'amendment', 'date': '2026-10-06'}
    actions = [{**base, 'amendment_shift_state': 'matched', 'amendment_shift_id': 'actual-draft-session'},
               {**base, 'amendment_shift_state': 'ambiguous', 'amendment_shift_id': None},
               {**base, 'amendment_shift_state': 'changed_or_missing', 'amendment_shift_id': None},
               {**base, 'amendment_shift_state': 'matched', 'amendment_shift_id': None},
               {**base, 'amendment_workspace_id': None, 'amendment_shift_state': 'not_created'}, base]
    script = (f"const m=await import({json.dumps((client_modules/'published_amendment.js').as_uri())});"
              "console.log(JSON.stringify(JSON.parse(process.argv[1]).map(m.amendmentDestination)));")
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(actions)],
                         capture_output=True, text=True, check=True, timeout=15)
    results = json.loads(out.stdout)
    assert results[4] is None
    for n in (0, 1, 2, 3, 5):
        route = urlsplit(results[n]['route'])
        assert route.path == 'schedule/week/2026-10-06'
        assert parse_qs(route.query) == ({'edit': ['amendment'], 'shift': ['actual-draft-session'], 'panel': ['session']}
                                          if n == 0 else {'edit': ['amendment']})
        assert 'old-issued-session' not in results[n]['route']
    assert results[0]['notice'] == ''
    assert 'cannot be matched uniquely' in results[1]['notice']
    assert 'changed or been removed' in results[2]['notice']
    assert results[3]['notice'] and results[5]['notice']
