"""Real compiled client helpers in Node (logic only, no DOM): automatic short codes stay unique at department scale
(AO-003), and the feedback context never keeps a credential link (CC-01). Synthetic sentinel values only."""
import json
import subprocess



def run(client_modules, module, expression, payload=None):
    script = (f"globalThis.document={{cookie:''}};const m=await import({json.dumps((client_modules/module).as_uri())});"
              f"const input=JSON.parse(process.argv[1]);console.log(JSON.stringify({expression}));")
    result = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(payload)],
                            capture_output=True, text=True, check=True, timeout=15)
    return json.loads(result.stdout)


def test_same_initial_crowd_keeps_unique_contract_sized_codes_and_saved_codes(client_modules):
    people = [{'id': f'p{i}', 'display_name': f'Fictional audit person {i:03}'} for i in range(400)]
    people.append({'id': 'saved', 'display_name': 'Fictional saved', 'short_code': 'F10'})
    codes = run(client_modules, 'dom.js', '[[...m.uniqueCodes(input)],[...m.uniqueCodes([...input].reverse())]]', people)
    first, again = dict(codes[0]), dict(codes[1])
    assert first == again  # the same people get the same codes, whatever order they come in
    assert first['saved'] == 'F10'
    automatic = [first[f'p{i}'] for i in range(400)]
    assert len(set(automatic)) == 400 and 'F10' not in automatic
    assert all(1 <= len(c) <= 6 for c in automatic)


def test_credential_links_are_redacted_before_they_are_remembered(client_modules):
    expression = ("(m.recordError('GET /api/account-links/SENTINEL-A · no response','network'),"
                  "m.recordError(new Error('Failed https://example.test/calendar/SENTINEL-B.ics'),'console'),"
                  "m.recordRoute('#people'),"
                  "{errors:m.recentErrors().map(e=>e.message),routes:m.recentRoutes(),"
                  "text:m.redactCredentials('/invite/SENTINEL-C /reset/SENTINEL-D?x /api/calendar-feed')})")
    got = run(client_modules, 'feedback_context.js', expression)
    assert 'SENTINEL' not in json.dumps(got)
    assert got['errors'] == ['GET /api/account-links/[redacted] · no response',
                             'Failed https://example.test/calendar/[redacted]']
    assert got['text'] == '/invite/[redacted] /reset/[redacted]?x /api/calendar-feed'
