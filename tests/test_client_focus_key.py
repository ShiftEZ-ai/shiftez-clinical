"""keepFocus's matching (app.ts, focus_key.ts sameControl) from the real compiled client in Node: after a redraw, focus
goes back to the same control by kind and name, and where the name repeats, by its row; it never guesses."""
import json
import subprocess


def same(client_modules, had, found):
    script = (f"const m=await import({json.dumps((client_modules/'focus_key.js').as_uri())});const i=JSON.parse(process.argv[1]);"
              "console.log(JSON.stringify(m.sameControl(i.had,i.found)));")
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps({'had': had, 'found': found})],
                         capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


def key(kind, name, row=''):
    return {'kind': kind, 'name': name, 'row': row}


def test_the_same_control_is_found_by_kind_and_name(client_modules):
    found = [key('button', 'Filter'), key('input:checkbox', 'View settings: Week'), key('button', 'View settings: Week')]
    assert same(client_modules, key('button', 'View settings: Week'), found) == 2
    assert same(client_modules, key('a', 'View settings: Week'), found) == -1  # another kind is another control
    assert same(client_modules, key('button', 'Gone'), found) == -1


def test_a_repeated_name_is_told_apart_by_its_row_and_never_guessed(client_modules):
    found = [key('input:checkbox', 'Select session', 's1'), key('input:checkbox', 'Select session', 's2')]
    assert same(client_modules, key('input:checkbox', 'Select session', 's2'), found) == 1
    assert same(client_modules, key('input:checkbox', 'Select session', 's3'), found) == -1
    # One control of that name: its row may have been redrawn under a new id, and it is still that control.
    assert same(client_modules, key('input:checkbox', 'Select session', 's3'), found[:1]) == 0
    # No row to tell them apart: not guessed either (Design Lead on 99adb216e).
    assert same(client_modules, key('button', 'Open'), [key('button', 'Open'), key('button', 'Open')]) == -1
