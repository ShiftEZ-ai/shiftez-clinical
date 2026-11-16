"""Owner, 7 Nov 2026 (auto-schedule run UX): one duration format as people read it, one short scope summary instead of a
wall of names, and Add time only when minutes can be added. Real compiled client modules in Node."""
import json

from .test_client_autoschedule_live import node


def test_durations_read_as_people_read_them(client_modules):
    out = node(client_modules, 'dom.js', "console.log(JSON.stringify([0,0.4,45,124,600,3300,3599,3900,7200,-5].map(m.duration)));")
    assert out == ['0 s', '< 1 s', '45 s', '2 min 4 s', '10 min', '55 min', '59 min', '1 h 5 min', '2 h', '0 s']


def test_a_scope_is_one_short_line_and_the_plurals_are_the_contracts(client_modules):
    people = [f'p{i}' for i in range(80)]
    tree = {'match': 'all', 'conditions': [{'field': 'category_ids', 'op': 'any', 'values': [f'r{i}' for i in range(9)]},
                                          {'field': 'person_ids', 'op': 'any', 'values': people}]}
    cases = [tree,
             {'match': 'all', 'conditions': [{'field': 'category_ids', 'op': 'any', 'values': ['r1']}]},
             {'match': 'any', 'conditions': [{'field': 'service_ids', 'op': 'none', 'values': ['a', 'b', 'c']},
                                             {'field': 'person_ids', 'op': 'any', 'values': ['p1']}]},
             None]
    out = node(client_modules, 'conditions.js',
               f"console.log(JSON.stringify({json.dumps(cases)}.map(t=>m.conditionsSummary(t,(f,v)=>'Name '+v))));")
    assert out == ['9 ranks · 80 people', 'Name r1', 'except 3 clinics or Name p1', '']


def test_add_time_room_is_the_ceiling_less_the_runs_own_search_time(client_modules):
    jobs = [{'search_control': {'minutes': 60}}, {'search_control': {'minutes': 10}}, {'effective_options': {'search_minutes': 5}},
            {'progress': {'time_limit_seconds': 1800}}, {}, {'search_control': {'minutes': 70}}]
    out = node(client_modules, 'autoschedule_setup.js',
               f"console.log(JSON.stringify({json.dumps(jobs)}.map(j=>m.addTimeRoom(j,60,5))));")
    assert out == [0, 50, 55, 30, 55, 0]


def test_a_duration_is_typed_as_people_write_it(client_modules):
    """LEAD-UIUX L10: one unit for every time field; a bare number is minutes; anything else is not a duration."""
    cases = ['15 min', '45 s', '1 h 5 min', '90 sec', '2 hours', '10', '1.5', '15 min 30 s', '15 minx', 'soon', '', '1 h and 5 min']
    out = node(client_modules, 'dom.js', f"console.log(JSON.stringify({json.dumps(cases)}.map(t=>{{const n=m.parseDuration(t);return Number.isNaN(n)?null:n;}})));")
    assert out == [900, 45, 3900, 90, 7200, 600, 90, 930, None, None, None, 3900]


def test_the_run_times_words_and_bounds_are_the_contracts():
    """The labels every screen uses for the run's times come from one source (GENERATION_TIME_FIELDS)."""
    from packages.contracts import schemas as s
    fields = s.GenerationOptions.model_fields
    assert fields['search_minutes'].title == 'Search time'
    assert fields['stop_without_improvement_seconds'].title == 'Stop after no improvement'
    assert fields['fairness_plateau_seconds'].title == 'Keep improving fairness'
    web = (__import__('pathlib').Path(__file__).resolve().parents[1] / 'apps/web/src').glob('*.ts')
    stray = [p.name for p in web if p.name != 'generated.ts' and any(x in p.read_text() for x in ('Search time (minutes)', "fairness (seconds)", 'improvement (seconds)'))]
    assert not stray, stray
