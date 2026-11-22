"""The shared rotaOutdated helper follows the authoritative detail verdict when one is loaded."""
import json
import subprocess


def run(client_modules, row, detail):
    script = (f"globalThis.document={{cookie:''}};const m=await import({json.dumps((client_modules/'dom.js').as_uri())});"
              "console.log(JSON.stringify(m.rotaOutdated(JSON.parse(process.argv[1]), JSON.parse(process.argv[2]))));")
    result = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(row), json.dumps(detail)],
                            capture_output=True, text=True, check=True, timeout=15)
    return json.loads(result.stdout)


def test_rota_outdated_covers_stale_inputs_and_detail_presence_matrix(client_modules):
    row = {'status': 'approved', 'outdated': True}
    assert run(client_modules, row, {'review_progress': {'stale_inputs': True}}) is True
    assert run(client_modules, row, {'review_progress': {'stale_inputs': False}}) is False
    assert run(client_modules, row, {'review_progress': {}}) is False
    assert run(client_modules, row, None) is True
    assert run(client_modules, {**row, 'outdated': False}, None) is False
    assert run(client_modules, {'status': 'approved'}, None) is False
