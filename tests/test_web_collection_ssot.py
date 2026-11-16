"""Collections filter, group and sort through one shared implementation .

Source scans, like the other web guards: a collection toolbar's Filter is the shared condition builder
(table_view.ts conditionFilter), never a bespoke stack of one dropdown per property; every dropdown is the shared
`select()` / `multiPick()` of dom.ts (opened by picker.ts), never a hand-built <select>. The few toolbars not migrated yet
are listed by name with what they wait for, so a new bespoke panel fails here."""
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from packages.contracts.schemas import TABLE_CONDITION_DEFINITIONS, TableViewDefinition

SRC = Path(__file__).resolve().parents[1] / 'apps/web/src'
# Bespoke Filter panels still waiting, by file and toolbar label, with what they need (docs: PR description, CHANGELOG).
PENDING = {
    ('app.ts', 'Schedule'): 'PlannerViewSettings has flat filters only; needs a condition tree and the planner evaluating it',
    ('app.ts', 'Audit log'): 'server-paged; /api/audit filter needs a "kind of change" (action group) field',
    ('app.ts', 'Rules'): 'catalogue v1 page, replaced by the Rules database (rules_tree.ts)',
}


def toolbars():
    """Each collectionToolbar({label:'…', …}) call with the text up to the next call (its spec and the code around it)."""
    for path in sorted(SRC.glob('*.ts')):
        text = path.read_text(encoding='utf-8')
        calls = list(re.finditer(r"collectionToolbar\(\{label:'([^']+)'", text))
        for i, call in enumerate(calls):
            yield path.name, call.group(1), text[call.start():calls[i + 1].start() if i + 1 < len(calls) else len(text)]


def bespoke(spec):
    """A Filter written in place (`filter:{count:…`) or passed in (`filter,`) rather than conditionFilter(…)."""
    return bool(re.search(r"\bfilter:\{count:|(?:^|[,{\s])filter,", spec[:4000]))


def test_every_collection_filter_is_the_shared_condition_builder():
    found = {(name, label) for name, label, spec in toolbars() if bespoke(spec)}
    assert found <= set(PENDING), f'A bespoke Filter panel: use table_view.ts conditionFilter instead: {sorted(found - set(PENDING))}'
    assert not set(PENDING) - found, f'Migrated: remove from PENDING {sorted(set(PENDING) - found)}'


def test_rules_and_rotation_board_use_the_shared_filter():
    rules = (SRC / 'rules_tree.ts').read_text(encoding='utf-8')
    assert 'filter:conditionFilter(' in rules and 'filter:{count:' not in rules
    assert "'Any').wrap);}}" not in rules  # the old stack of "Any" dropdowns, one per property
    board = (SRC / 'rotation_board.ts').read_text(encoding='utf-8')
    assert 'conditionFilter(' in board and 'filter:{count:' not in board


def test_dropdowns_are_the_shared_select_or_picker():
    native = {p.name: len(re.findall(r"el\('select'", p.read_text(encoding='utf-8'))) for p in SRC.glob('*.ts')}
    outside = {name: n for name, n in native.items() if n and name not in {'dom.ts', 'picker.ts'}}
    assert not outside, f'Use dom.ts select() or multiPick() (the shared picker), not a hand-built <select>: {outside}'


def test_no_per_record_command_loops_remain_in_the_web_client():
    """Every bulk change is one command (bulk.apply): no client loop sends a command per record."""
    text = {p.name: p.read_text(encoding='utf-8') for p in SRC.glob('*.ts')}
    assert not any('eachInTurn' in t for t in text.values())
    loop = re.compile(r'for\s*\(\s*const\s+\w+\s+of\s+[^)]*\)\s*\{?[^{}]{0,200}?await\s+command\(')
    offenders = [name for name, t in text.items() if name != 'api.ts' and loop.search(t)]
    assert not offenders, offenders


def test_rules_collection_properties_come_from_the_contract():
    rules = (SRC / 'rules_tree.ts').read_text(encoding='utf-8')
    assert "TABLE_CONDITION_DEFINITIONS['rule-tree']" in rules
    own = set(re.findall(r"\b(state|family|issue|availability|kind|rule):\['", rules))
    assert own <= set(TABLE_CONDITION_DEFINITIONS['rule-tree']), own - set(TABLE_CONDITION_DEFINITIONS['rule-tree'])


def test_saved_views_keep_further_sort_keys():
    view = TableViewDefinition(sort=('name', 'asc'), then_sort=[('rank', 'desc'), ('code', 'asc')])
    assert view.model_dump(mode='json')['then_sort'] == [['rank', 'desc'], ['code', 'asc']]
    assert 'then_sort' not in TableViewDefinition(sort=('name', 'asc')).model_dump(mode='json')  # older views read the same
    with pytest.raises(ValidationError):
        TableViewDefinition(then_sort=[('name', 'sideways')])
    with pytest.raises(ValidationError):
        TableViewDefinition(then_sort=[('k' + str(i), 'asc') for i in range(31)])
