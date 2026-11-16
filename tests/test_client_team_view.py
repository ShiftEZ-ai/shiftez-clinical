"""The supervision team box (team_view.ts, D66), from the real compiled client in Node (logic only, no DOM): its full
accessible name, which problem a team shows (from the recorded pairing and the check's findings, the limit taken from
the finding), and the dated rank code."""
import json
import subprocess


def run(client_modules, body, data):
    script = (f"globalThis.document={{cookie:''}};globalThis.matchMedia=()=>({{matches:false,addEventListener(){{}}}});"
              f"const t=await import({json.dumps((client_modules/'planner_teams.js').as_uri())});"
              f"const m=await import({json.dumps((client_modules/'team_view.js').as_uri())});const i=JSON.parse(process.argv[1]);"
              f"console.log(JSON.stringify(({body})));")
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(data)], capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


def p(pid, sup=None, role='trainee', **extra):
    return {'person_id': pid, 'supervisor_person_id': sup, 'participation': role, **extra}


NAMES = {'x': 'Dr X', 'a': 'A', 'b': 'B', 'c': 'C', 'y': 'Dr Y', 'gone': 'Dr Gone'}
RANKS = {'a': 'R2', 'b': 'R1', 'c': 'R3'}
LABEL = "m.teamLabel(g,id=>i.names[id],id=>i.ranks[id]||'',m.teamProblem(g,i.roster,pid=>i.errors[pid]||[]))"


def labels(client_modules, roster, errors=None):
    return run(client_modules, f"t.teamGroups(i.roster).map(g=>{LABEL})", {'roster': roster, 'errors': errors or {}, 'names': NAMES, 'ranks': RANKS})


def test_team_box_name_says_the_supervisor_and_each_trainee_with_rank(client_modules):
    roster = [p('a', 'x'), p('x', role='supervisor'), p('b', 'x')]
    assert labels(client_modules, roster) == ['Dr X supervising A (R2) and B (R1)']


def test_problem_states_are_said_in_words(client_modules):
    # Named supervisor not on this session, and a trainee naming no one while a supervisor is here.
    roster = [p('a', 'gone'), p('y', role='supervisor'), p('c')]
    assert labels(client_modules, roster) == ['A (R2), with Dr Gone, not on this session', 'Dr Y', 'Unpaired: C (R3)']
    # The check says a mandatory rule is broken: Needs supervisor (no one is here to pair with).
    roster = [p('b')]
    assert labels(client_modules, roster, {'b': [{'code': 'SUPERVISION_PAIRING', 'severity': 'error'}]}) == ['Needs supervisor: B (R1)']
    # A lone trainee with no supervisor and no finding is not a problem the box can claim.
    assert labels(client_modules, roster) == ['B (R1)']


def test_over_the_limit_takes_its_numbers_from_the_finding(client_modules):
    roster = [p('x', role='supervisor'), p('a', 'x'), p('b', 'x'), p('c', 'x')]
    found = {'x': [{'code': 'SUPERVISOR_CAPACITY', 'severity': 'error', 'detail': {'maximum': 2, 'scheduled': 3}}]}
    assert labels(client_modules, roster, found) == ['Dr X supervising A (R2), B (R1) and C (R3); 3 · limit 2']
    scoped = {'x': [{'code': 'SUPERVISOR_SCOPED_CAPACITY', 'severity': 'error', 'detail': {'maximum': 1, 'permitted_maximum': 2, 'scheduled': 3}}]}
    got = run(client_modules, "t.teamGroups(i.roster).map(g=>m.teamProblem(g,i.roster,pid=>i.errors[pid]||[]))", {'roster': roster, 'errors': scoped})
    assert got == [{'kind': 'over', 'count': 3, 'limit': 2}]
    # The permitted-extra warning is not over the limit.
    warn = {'x': [{'code': 'SUPERVISOR_CAPACITY', 'severity': 'warning', 'detail': {'maximum': 2, 'scheduled': 3}}]}
    assert run(client_modules, "t.teamGroups(i.roster).map(g=>m.teamProblem(g,i.roster,pid=>i.errors[pid]||[]))", {'roster': roster, 'errors': warn}) == [None]


def test_rank_is_the_one_held_on_the_duty_day_from_the_rank_catalogue(client_modules):
    catalogues = {'categories': [{'id': 'r1', 'name': 'Resident year 1', 'short_code': 'R1'}, {'id': 'r2', 'name': 'Resident year 2', 'short_code': ''}],
                  'memberships': [{'person_id': 'a', 'category_id': 'r1', 'starts_on': '2026-07-01', 'ends_on': '2026-11-10'},
                                  {'person_id': 'a', 'category_id': 'r2', 'starts_on': '2026-11-10', 'ends_on': None}]}
    got = run(client_modules, "['2026-11-09','2026-11-10','2026-06-01'].map(d=>m.rankOn(i,'a',d))", catalogues)
    assert got == ['R1', 'Resident year 2', '']


def test_team_rows_is_a_view_setting_with_the_contract_default():
    """Week/Month's "+N more" threshold is the planner view's own setting (PlannerViewSettings.team_rows), defaulting
    to the contract's PLANNER_TEAM_ROWS_DEFAULT and bounded by PLANNER_TEAM_ROWS_LIMIT; the client reads both from it."""
    import pytest
    from pydantic import ValidationError
    from packages.contracts import schemas as s
    assert s.PlannerViewSettings().team_rows is None and s.PLANNER_TEAM_ROWS_DEFAULT == 3
    assert s.PlannerViewSettings(team_rows=5).team_rows == 5
    for bad in (0, s.PLANNER_TEAM_ROWS_LIMIT + 1):
        with pytest.raises(ValidationError):
            s.PlannerViewSettings(team_rows=bad)
    from pathlib import Path
    week = Path('apps/web/src/planner_week.ts').read_text()
    assert 'limit:weekView.forPrint?undefined:weekView.teamRows' in week and 'teamRows:PLANNER_TEAM_ROWS_DEFAULT' in week
