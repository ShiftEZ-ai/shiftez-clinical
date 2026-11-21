"""Client-side rule-line wording and exception-shape regressions."""
import json
import re
import subprocess
from pathlib import Path

import pytest

from packages.domain.rule_lines import ATTENTION


def _evaluate(client_modules, expression, payload):
    script = (
        f"const m=await import({json.dumps((client_modules / 'rule_catalogue.js').as_uri())});"
        f"const d=await import({json.dumps((client_modules / 'dom.js').as_uri())});"
        f"const g=await import({json.dumps((client_modules / 'generated.js').as_uri())});"
        "d.setWeekStart(6);"
        f"const p=JSON.parse(process.argv[1]);console.log(JSON.stringify({expression}));"
    )
    result = subprocess.run(
        ['node', '--input-type=module', '-e', script, json.dumps(payload)],
        capture_output=True, text=True, check=True, timeout=15,
    )
    return json.loads(result.stdout)


def _person(person='p1'):
    return {'match': 'all', 'conditions': [{'field': 'person_ids', 'values': [person]}]}


def _maximum(value, *, who=None, level='mandatory', settings=None):
    return {
        'id': f'l{value}{level}',
        'who': who,
        'where': None,
        'numbers': {'max': {'value': value, 'level': level}},
        'measure': 'duties',
        'period': {'kind': 'calendar_week'},
        'weekdays': list(range(7)),
        'settings': settings or {},
    }


def _tree(client_modules, anchor, exception, replaces=None):
    return _evaluate(
        client_modules,
        "m.exceptionTree(g.RULE_CATALOGUE_V2.find(e=>e.id==='workload_limit'),"
        "[p.anchor,p.exception],x=>x).map(x=>({id:x.item.id,under:x.under,always:x.always,"
        "instead:m.replacedWords(g.RULE_CATALOGUE_V2.find(e=>e.id==='workload_limit'),x.item,[p.anchor,p.exception],p.replaces)}))",
        {'anchor': anchor, 'exception': exception, 'replaces': replaces},
    )


def test_same_bound_requires_matching_line_settings_and_strength(client_modules):
    """The server resolves matching bounds and strength; the client never infers a replacement without that evidence."""
    settings = [
        ('max_duties.window', {'max_duties': {'window': 'week'}}, {'max_duties': {'window': 'month'}}),
        ('max_duties.group_by', {'max_duties': {'group_by': 'all'}}, {'max_duties': {'group_by': 'weekday'}}),
        ('block_quota.metric', {'block_quota': {'metric': 'duties'}}, {'block_quota': {'metric': 'hours'}}),
        ('block_quota.participation', {'block_quota': {'participation': 'all'}}, {'block_quota': {'participation': 'supervised'}}),
        ('overtime', {'allocation_overtime': {'volunteer_required': True}}, {'allocation_overtime': {'volunteer_required': False}}),
    ]
    for _name, anchor_settings, exception_settings in settings:
        out = _tree(client_modules, _maximum(5, settings=anchor_settings),
                    _maximum(3, who=_person(), settings=exception_settings))
        assert out[1]['under'] and out[1]['instead'] == '', (_name, out)

    weaker = _tree(client_modules, _maximum(5), _maximum(3, who=_person(), level='medium'))
    assert weaker[1]['under'] and weaker[1]['instead'] == '', weaker

    # The same resolved replacement survives different authored object-key order.
    equal = _tree(
        client_modules,
        _maximum(5, settings={'max_duties': {'window': 'week', 'group_by': 'all'}}),
        _maximum(3, who=_person(), settings={'max_duties': {'group_by': 'all', 'window': 'week'}}),
        replaces={'l3mandatory': [{'replaced_line_id': 'l5mandatory', 'numbers': ['max']}]},
    )
    assert equal[1]['instead'].startswith('≤ 5'), equal


@pytest.mark.parametrize('case', ['present', 'no_map', 'no_line', 'two_broader', 'empty_numbers', 'missing_broader'])
def test_replacement_words_follow_only_the_servers_listed_numbers(client_modules, case):
    anchor = _maximum(5)
    anchor['numbers']['min'] = {'value': 2, 'level': 'medium'}
    exception = _maximum(3, who=_person())
    middle = {**_maximum(4), 'id': 'middle'}
    replaces = {exception['id']: [{'replaced_line_id': anchor['id'], 'numbers': ['max']}]}
    if case == 'no_map':
        replaces = None
    elif case == 'no_line':
        replaces = {}
    elif case == 'two_broader':
        replaces[exception['id']].append({'replaced_line_id': middle['id'], 'numbers': ['max']})
    elif case == 'empty_numbers':
        replaces[exception['id']][0]['numbers'] = []
    elif case == 'missing_broader':
        replaces[exception['id']][0]['replaced_line_id'] = 'not-in-this-rule'
    words = _evaluate(client_modules,
        "m.replacedWords(g.RULE_CATALOGUE_V2.find(e=>e.id==='workload_limit'),p.line,p.lines,p.replaces)",
        {'line': exception, 'lines': [anchor, middle, exception], 'replaces': replaces})
    assert words == ('≤ 5 duties per calendar week' if case == 'present' else '')
    assert '2' not in words
    assert not any(level in words for level in ('Must', 'Prefer', 'mandatory', 'medium'))


def test_replacement_words_have_one_server_driven_producer():
    src = Path(__file__).parents[1] / 'apps/web/src'
    catalogue = (src / 'rule_catalogue.ts').read_text(encoding='utf-8')
    tree = (src / 'rules_tree.ts').read_text(encoding='utf-8')
    assert not re.search(r'\b(?:sameBound|settingsKey)\b', catalogue)
    assert 'export function replacedWords(' in catalogue
    assert 'replacedWords(e,s.item,draft.lines,found?.line_replaces' in catalogue
    assert 'replacedWords(e,x.item.line,linesOf(e),e.line_replaces' in tree
    assert sorted(re.findall(r'`\(instead of \$\{([^}]+)\}\)`', catalogue + tree)) == ['instead', 's.instead']


def test_restriction_words_cover_leave_window_and_unknown_settings(client_modules):
    entry = {'numbers': {'apply': [{'kind': 'restriction'}]}, 'default_settings': {}}

    def words(settings, weekdays=None):
        line = {
            'numbers': {'apply': {'value': None, 'level': 'mandatory'}},
            'weekdays': list(range(7)) if weekdays is None else weekdays,
            'settings': {'restriction': settings},
        }
        return _evaluate(client_modules, "({condition:m.conditionWords(p.entry,p.line),when:m.whenWords(p.entry,p.line)})",
                         {'entry': entry, 'line': line})

    limited = words({'weekdays': [3, 6], 'leave_buffer_days': 3})
    assert limited == {'condition': 'Sun, Thu · within 3 days of leave',
                       'when': 'Sun, Thu · within 3 days of leave'}
    timed = words({'start_time': '08:00', 'end_time': '16:00'})
    assert '08:00–16:00' in timed['condition'] and '08:00–16:00' in timed['when']
    one_day = words({'leave_buffer_days': 1})
    assert one_day['condition'] == 'within 1 day of leave'
    unknown = words({'conditions': {'match': 'all', 'conditions': []}})
    assert unknown['condition'] == 'Conditions apply' and unknown['when'] == 'Conditions apply'
    empty = words({})
    assert empty['condition'] == '' and 'within' not in empty['when'] and 'Conditions apply' not in empty['when']


def _invalid_minmax(css):
    for start in re.finditer(r'\bminmax\s*\(', css):
        depth, end = 1, start.end()
        while depth and end < len(css):
            depth += {'(': 1, ')': -1}.get(css[end], 0)
            end += 1
        argument = css[start.end():end - 1]
        if re.search(r'\bfit-content\s*\(', argument):
            yield argument


def test_open_lines_grid_does_not_nest_fit_content_inside_minmax():
    css = (Path(__file__).parents[1] / 'apps' / 'web' / 'public' / 'assets' / 'app.css').read_text(encoding='utf-8')
    assert not list(_invalid_minmax(css)), 'minmax() must not contain fit-content()'


@pytest.mark.parametrize('tracks,invalid', [
    ('minmax(0, fit-content(320px))', True),
    ('minmax(min(1rem, 3ch), fit-content(320px))', True),
    ('minmax(0, 1fr) fit-content(320px)', False),
    ('minmax(min(1rem, 3ch), 1fr)', False),
])
def test_grid_guard_distinguishes_nested_functions_from_valid_siblings(tracks, invalid):
    assert bool(list(_invalid_minmax(tracks))) is invalid


def test_every_server_attention_kind_has_client_words():
    src = Path(__file__).parents[1] / 'apps/web/src'
    tree = (src / 'rules_tree.ts').read_text(encoding='utf-8')
    kinds = tree.split('export const KINDS:', 1)[1].split('const kindOf=', 1)[0]
    what = tree.split('function what(', 1)[1].split('function fixWords(', 1)[0]
    assert set(ATTENTION) <= set(re.findall(r'^\s*(\w+):\{label:', kinds, re.M))
    assert set(ATTENTION) - {'blocks'} <= set(re.findall(r"x\.kind==='(\w+)'", what))
    obsolete = re.compile(r'OVERLAPS_UNACKNOWLEDGED|\backKey\s*\(|That is right')
    assert not [path.name for path in src.glob('*.ts') if obsolete.search(path.read_text(encoding='utf-8'))]


def test_unknown_attention_kind_without_drafts_has_words(client_modules):
    # what() closes over the current collection. Exercise its compiled fallback with an empty collection.
    script = r"""
        import {readFileSync} from 'node:fs';
        const file = new URL(process.argv[1]);
        const {KINDS} = await import(file.href);
        const {plural} = await import(new URL('dom.js', file).href);
        const text = readFileSync(file, 'utf8');
        const kindOf = text.match(/^const kindOf = .*;$/m)[0];
        const cap = text.match(/^const cap = .*;$/m)[0];
        const what = text.match(/^    function what\(x\) \{[\s\S]*?^    \}/m)[0];
        const describe = new Function('KINDS', 'plural', 'byId',
            [kindOf, cap, what, 'return what;'].join('\n'))(KINDS, plural, new Map());
        console.log(describe({kind:'future_issue', facts:{}, line_ids:[], people:0}));
    """
    result = subprocess.run(
        ['node', '--input-type=module', '-e', script, (client_modules / 'rules_tree.js').as_uri()],
        capture_output=True, text=True, check=True, timeout=15,
    )
    assert result.stdout.strip() == 'Future issue'
