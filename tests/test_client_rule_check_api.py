"""B3/B4/B5 pure logic from the real compiled rule_check_api.ts in Node: the relation sentence, D8's default strength
and the effect words (no DOM)."""
import json
import subprocess


def run(client_modules, expr, data):
    script = (f"const m=await import({json.dumps((client_modules/'rule_check_api.js').as_uri())});const i=JSON.parse(process.argv[1]);"
              f"console.log(JSON.stringify({expr}));")
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(data)], capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


REL = {'line_id': 'l4', 'line_no': 4, 'overrides': False, 'reason': 'strength_differs', 'fix': 'match_strength', 'default_severity': {'min': 'mandatory'}}
WORDS = {'subject': 'Jules Foster', 'broader': 'Internship ≥ 1', 'still': 'Jules Foster still needs 1 duty',
         'strength': {'label': 'minimum', 'theirs': 'Mandatory', 'mine': 'Advisory'}}


def test_a_weaker_person_line_warns_that_both_apply_and_offers_the_one_fix(client_modules):
    t = run(client_modules, 'm.relationText(i.r,i.o)', {'r': REL, 'o': WORDS})
    assert t == {'warn': True, 'lead': 'Does not override ', 'link': 'line 4',
                 'tail': ': its minimum is Mandatory, this one Advisory. Both apply, so Jules Foster still needs 1 duty.', 'fix': 'Make it Mandatory'}


def test_an_overriding_line_says_whom_it_takes_over_for(client_modules):
    t = run(client_modules, 'm.relationText(i.r,i.o)', {'r': {**REL, 'overrides': True, 'reason': None, 'fix': None}, 'o': WORDS})
    assert (t['warn'], t['lead'] + t['link'] + t['tail'], t['fix']) == (False, 'Overrides line 4 for Jules Foster · Internship ≥ 1 at the same clinics', None)


def test_a_safety_line_never_offers_a_fix(client_modules):
    t = run(client_modules, 'm.relationText(i.r,i.o)', {'r': {**REL, 'reason': 'safety', 'fix': None}, 'o': WORDS})
    assert t['warn'] and t['fix'] is None and 'safety lines all apply' in t['tail']


UCC = {'match': 'all', 'conditions': [{'field': 'service_ids', 'op': 'any', 'values': ['ucc']}]}


def strength(client_modules, lines, line, key='min'):
    return run(client_modules, "m.broaderStrength(i.line,i.lines,i.key,l=>l.who?3:1)", {'line': line, 'lines': lines, 'key': key})


def test_a_new_person_line_takes_the_strength_of_the_broader_line_with_the_same_where(client_modules):
    broad = {'id': 'b', 'where': UCC, 'numbers': {'min': {'value': 1, 'level': 'mandatory'}}}
    other = {'id': 'o', 'where': None, 'numbers': {'min': {'value': 2, 'level': 'low'}}}
    mine = {'id': 'n', 'who': {'match': 'all', 'conditions': []}, 'where': UCC, 'numbers': {}}
    assert strength(client_modules, [other, broad], mine) == 'mandatory'
    # Another where, or a number the broader line does not set: no default from it.
    assert strength(client_modules, [other], mine) is None
    assert strength(client_modules, [broad], mine, 'max') is None


def test_effect_words_never_estimate_and_failed_checks_are_not_ready(client_modules):
    rows = [{'workspace_id': 'a', 'name': 'Dec', 'status': 'ready', 'clears': 1, 'effect': 'clears'},
            {'workspace_id': 'b', 'name': 'Nov', 'status': 'ready', 'clears': 0, 'adds': 0},
            {'workspace_id': 'c', 'name': 'Jan', 'status': 'running', 'started_at': '2026-11-02T10:00:00Z'},
            {'workspace_id': 'd', 'name': 'Feb', 'status': 'failed'}]
    out = run(client_modules, '[i.rows.map(m.effectWords),m.effectReady(i.rows.slice(0,2)),m.effectReady(i.rows)]', {'rows': rows})
    assert out == [[{'text': 'Clears 1 check', 'tone': 'ok'}, {'text': 'No change', 'tone': 'neutral'}, None, {'text': 'Check failed', 'tone': 'err'}], True, False]


def test_only_a_check_that_has_not_started_says_it_waits(client_modules):
    """One wording for both review panels: queued (a job in the queue, or a check just asked for) waits whatever its
    times say; running and every ended status give no waiting word."""
    out = run(client_modules, 'i.map(s=>m.waitingWord(s))', ['queued', 'running', 'checking', 'ready', 'cancelled', None])
    assert out == ['Waiting to check', '', '', '', '', '']


def test_a_rota_revision_is_read_from_the_compact_detail_not_the_whole_rota(client_modules):
    """A rule adjustment asks only for the rota's revision: one compact read, never the whole rota's megabytes."""
    script = ("const paths=[];globalThis.document={cookie:''};"
              "globalThis.fetch=async p=>{paths.push(String(p));return new Response(JSON.stringify({workspace:{revision:7}}),{status:200});};"
              "return {revision:await m.workspaceRevision('w 1'),paths};")
    got = run(client_modules, f"await (async()=>{{{script}}})()", None)
    assert got == {'revision': 7, 'paths': ['/api/workspaces/w%201?view=compact']}


def footer(client_modules, **given):
    base = {'stalled': False, 'checking': False, 'provisional': False, 'checked': True, 'counts': True, 'current': True,
            'partial': False, 'jobsRead': True, 'ended': False, 'whole': True, 'draft': True}
    return run(client_modules, 'm.footerCheck(i)', {**base, **given})


def test_only_a_check_that_is_not_current_says_needs_a_check(client_modules):
    # Current: its counts, or "Checked" while no Fix list summary is held; never a warning (DEV smoke a5dd59837).
    assert footer(client_modules) == 'counts'
    assert footer(client_modules, counts=False) == 'checked'
    assert footer(client_modules, counts=False, draft=False) == 'checked'
    # Not current: the draft keeps its last counts beside Check now; a reader sees the pill.
    assert footer(client_modules, current=False) == 'stale'
    assert footer(client_modules, current=False, counts=False) == 'check-now'
    assert footer(client_modules, current=False, counts=False, draft=False) == 'needs-check'
    # A window (whole rota still loading) keeps the counts it has.
    assert footer(client_modules, current=False, whole=False) == 'counts'
    assert footer(client_modules, checked=False) == 'not-checked'


def test_a_partial_check_waits_until_the_job_list_is_read_then_proves_nothing(client_modules):
    assert footer(client_modules, partial=True, jobsRead=False) == 'waiting'
    assert footer(client_modules, partial=True, jobsRead=False, counts=False) == 'waiting'
    # Read with no live check job in it (D-062): not current, whatever the stored flag says.
    assert footer(client_modules, partial=True) == 'stale'
    assert footer(client_modules, partial=True, counts=False, draft=False) == 'needs-check'
    # A listed job says its own words; a running or stopped check is decided first.
    assert footer(client_modules, partial=True, provisional=True, counts=False) == 'provisional'
    assert footer(client_modules, checking=True, provisional=True) == 'checking'
    # A check this page saw end (refused, stopped) waits for no job: its pill says so (Check now's words), not "Waiting".
    assert footer(client_modules, partial=True, jobsRead=False, ended=True) == 'stale'
    assert footer(client_modules, partial=True, jobsRead=False, ended=True, counts=False) == 'check-now'
    assert footer(client_modules, stalled=True, checking=True) == 'stalled'
