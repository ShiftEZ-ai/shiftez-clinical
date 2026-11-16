"""UIUX-AUDIT L8: a roll-up marker's accessible name (issue_marker.ts issueLabel) says each severity and sentence once,
with how many sessions, must fix first, as issueList groups them; past ISSUE_LIST_LIMIT parts it ends "and N more".
The real compiled module in Node (logic only, no DOM)."""
import json
import subprocess


def label(client_modules, issues, title=''):
    script = (f"const m=await import({json.dumps((client_modules/'issue_marker.js').as_uri())});"
              "const i=JSON.parse(process.argv[1]);console.log(JSON.stringify(m.issueLabel(i.xs,i.title)));")
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps({'xs': issues, 'title': title})],
                         capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


def issue(words, shift=None, severity='error', where=None):
    return {'severity': severity, 'words': words, **({'shift': shift} if shift else {}), **({'where': where} if where else {})}


def test_a_repeated_sentence_is_said_once_with_its_session_count(client_modules):
    xs = [issue('Open place', 's1'), issue('Open place', 's2'), issue('Open place', 's3'),
          issue('Over the weekly limit', 's1', 'warning'), issue('Open place', 's3')]
    said = label(client_modules, xs, 'Sun 8')
    assert said == 'Sun 8. 5 issues. Must fix: Open place (3 sessions); Could improve: Over the weekly limit'
    assert said.count('Open place') == 1


def test_must_fix_comes_first_and_one_place_says_where(client_modules):
    xs = [issue('Over the weekly limit', severity='warning', where='Clinic A'), issue('Double booked', 's1')]
    assert label(client_modules, xs) == '2 issues. Must fix: Double booked; Could improve: Over the weekly limit (Clinic A)'


def test_places_without_a_session_are_counted_as_places(client_modules):
    xs = [issue('No cover', where='Clinic A'), issue('No cover', where='Clinic B')]
    assert label(client_modules, xs) == '2 issues. Must fix: No cover (2 places)'


def test_the_name_stays_short_past_the_list_limit(client_modules):
    xs = [issue(f'Sentence {n}', f's{n}') for n in range(8)]
    said = label(client_modules, xs)
    assert said.endswith('Must fix: Sentence 4; and 3 more')
    assert 'Sentence 5' not in said


def run(client_modules, expr, arg):
    script = (f"const m=await import({json.dumps((client_modules/'issue_marker.js').as_uri())});"
              f"const a=JSON.parse(process.argv[1]);console.log(JSON.stringify({expr}));")
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(arg)],
                         capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


def test_one_finding_that_is_both_must_fix_and_could_improve_is_said_once(client_modules):
    # Live check F1: grouped by identity (its code and words), never by severity and words; the worse mark wins.
    xs = [dict(issue('People of a kind per session', 's1'), code='PEOPLE_PER_SESSION'),
          dict(issue('People of a kind per session', 's2', 'warning'), code='PEOPLE_PER_SESSION')]
    said = label(client_modules, xs, 'Sun 11')
    assert said == 'Sun 11. 2 issues. Must fix: People of a kind per session (2 sessions)'
    # Two codes with the same words stay two items (different findings).
    other = [dict(issue('Same words', 's1'), code='A'), dict(issue('Same words', 's2'), code='B')]
    assert label(client_modules, other).count('Same words') == 2


def test_chips_on_the_same_day_carry_their_time(client_modules):
    # Live check F7: "Sun 11" eight times was eight different sessions.
    days = [{'day': 'Sun 11', 'time': '08:00'}, {'day': 'Sun 11', 'time': '13:00'}, {'day': 'Mon 12', 'time': '08:00'}, {'day': '', 'time': ''}]
    assert run(client_modules, 'm.dayLabels(a)', days) == ['Sun 11 · 08:00', 'Sun 11 · 13:00', 'Mon 12', '']
