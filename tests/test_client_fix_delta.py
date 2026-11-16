"""maintenance with LEAD-SCHEDULING's confirmed payload (7 Nov 2026): fix_list.ts applyFixDelta, the one way an edit's (or
Undo's) fix_list_delta updates the Fix list held for the footer, its popover and the panel. The real compiled module
in Node (logic only, no DOM)."""
import json
import subprocess


def apply(client_modules, held, delta):
    script = (f"const m=await import({json.dumps((client_modules/'fix_list.js').as_uri())});"
              "const a=JSON.parse(process.argv[1]);console.log(JSON.stringify(m.applyFixDelta(a.held,a.delta)));")
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps({'held': held, 'delta': delta})],
                         capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


def cause(cid, level='must', shifts=()):
    return {'id': cid, 'level': level, 'sentence': cid, 'shift_ids': list(shifts), 'finding_keys': [cid]}


HELD = {'causes': [cause('a', shifts=['s1', 's2']), cause('b', shifts=['s3']), cause('c', 'could')],
        'summary': {'must': 2, 'could': 1}}


def test_a_transition_delta_clears_updates_and_adds_with_its_summary(client_modules):
    now = apply(client_modules, HELD, {'cleared': ['b'], 'new': ['d'], 'changed': ['a'],
                                       'causes': [cause('a', shifts=['s2']), cause('d', shifts=['s9'])],
                                       'summary': {'must': 2, 'could': 1}})
    assert [c['id'] for c in now['causes']] == ['a', 'd', 'c']
    assert now['causes'][0]['shift_ids'] == ['s2']  # s1 left the cause: its ✓ chip
    assert now['summary'] == {'must': 2, 'could': 1}  # counts are the summary, never the arrays' lengths


def test_a_delta_without_a_baseline_is_the_whole_list(client_modules):
    # before_fix is None: cleared/new/changed empty on purpose, causes = every cause after the edit.
    now = apply(client_modules, HELD, {'cleared': [], 'new': [], 'changed': [],
                                       'causes': [cause('a', shifts=['s2']), cause('c', 'could'), cause('e', shifts=['s7'])],
                                       'summary': {'must': 2, 'could': 1}})
    assert [c['id'] for c in now['causes']] == ['a', 'c', 'e']  # b cleared (s3 ✓), e new; shown ones keep their place
    assert sum(c['level'] == 'must' for c in now['causes']) == now['summary']['must']  # rows agree with the counts


def test_an_edit_that_changed_nothing_keeps_the_list(client_modules):
    now = apply(client_modules, HELD, {'cleared': [], 'new': [], 'changed': [], 'causes': [], 'summary': HELD['summary']})
    assert now['causes'] == HELD['causes']
