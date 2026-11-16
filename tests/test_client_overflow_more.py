"""The one "+N" for hidden items (dom.ts overflowMore/paintMore/moreName, DESIGN D8): it shows "+N" and its name says it
in full with the noun ("2 more sessions"), singular for one, plus what it does when it opens something."""
import json

import pytest

from .test_client_autoschedule_live import CLICKS, FAKE_DOM, node

DOM = FAKE_DOM + CLICKS


def test_plus_n_shows_the_count_and_names_it_in_full(client_modules):
    out = node(client_modules, 'dom.js', DOM + (
        "let opened=0;const pick=b=>({text:b.textContent,name:b.attrs['aria-label']??null,tag:b.tag});"
        "const two=m.overflowMore(2,['session','sessions'],{all:5,open:()=>opened++}),"
        "one=m.overflowMore(1,['session','sessions']),"
        "cell=m.overflowMore(3,['person','people'],{of:'on 4 Oct',action:'show all'}),"
        "plain=m.overflowMore(1,['rule','rules'],{as:'span'}),"
        "inside=m.overflowMore(4,['wish','wishes'],{as:'hidden'});"
        "two.click();m.paintMore(one,7,{noun:['session','sessions']});"
        "console.log(JSON.stringify({two:pick(two),tip:two.dataset.tip,one:pick(one),cell:pick(cell),opened,"
        "plain:{shown:plain.kids[0].textContent,hidden:plain.kids[0].attrs['aria-hidden'],words:plain.kids[1].textContent},"
        "inside:{...pick(inside),hidden:inside.attrs['aria-hidden']},"
        "word:m.moreName(2,{noun:'Applies to'})}));"))
    assert out == {
        'two': {'text': '+2', 'name': '2 more sessions: show all 5', 'tag': 'button'}, 'tip': 'Show all 5',
        'one': {'text': '+7', 'name': '7 more sessions', 'tag': 'button'},  # repainted for a new count
        'cell': {'text': '+3', 'name': '3 more people on 4 Oct: show all', 'tag': 'button'},
        'opened': 1,
        'plain': {'shown': '+1', 'hidden': 'true', 'words': '1 more rule'},  # a plain mark says it in hidden words
        'inside': {'text': '+4', 'name': None, 'tag': 'span', 'hidden': 'true'},  # its control names everything
        'word': '2 more Applies to',
    }


def test_one_hidden_item_is_singular(client_modules):
    out = node(client_modules, 'dom.js', DOM + (
        "const b=m.overflowMore(1,['session','sessions'],{all:3,open:()=>{}});"
        "console.log(JSON.stringify([b.textContent,b.attrs['aria-label']]));"))
    assert out == ['+1', '1 more session: show all 3']


def pill_more(client_modules, nouns, shown):
    """A rule's Who/Where pills: "+N" names the hidden pills' kind (from their condition fields)."""
    return node(client_modules, 'table_menu.js', DOM + (
        f"const n=JSON.parse({json.dumps(json.dumps(nouns))});"
        f"const w=m.pillList(n.map((_,i)=>'v'+i),{{label:'Who',shown:{shown},nouns:n}}),b=w.querySelector('.pill-more');"
        "console.log(JSON.stringify([b.textContent,b.attrs['aria-label']]));"))


@pytest.mark.parametrize(('fields', 'shown', 'expected'), [
    (['category_ids'] * 4, 2, ['+2', '2 more ranks: show all 4']),                        # one kind
    (['category_ids', 'category_ids', 'category_ids', 'service_ids'], 2, ['+2', '2 more items: show all 4']),  # mixed
    (['tags', 'category_ids', 'category_ids'], 1, ['+2', '2 more ranks: show all 3']),   # only the hidden ones count
    (['category_ids'] * 4, 3, ['+1', '1 more rank: show all 4']),                         # singular
    (['person_ids', 'person_ids', 'person_ids'], 2, ['+1', '1 more person: show all 3']),
    (['eligible_service_ids'] * 3, 1, ['+2', '2 more clinics they can work at: show all 3']),
    (['category_ids', None, None], 1, ['+2', '2 more items: show all 3']),               # a group pill has no kind
])
def test_rule_pills_name_the_hidden_kind(client_modules, fields, shown, expected):
    nouns = node(client_modules, 'conditions.js', f"console.log(JSON.stringify({json.dumps(fields)}.map(f=>f&&m.fieldNoun(f))));")
    assert all(n for n, f in zip(nouns, fields) if f), nouns
    assert pill_more(client_modules, nouns, shown) == expected
    assert 'Who' not in expected[1]


def test_field_nouns_come_from_the_contract(client_modules):
    from packages.contracts.schemas import CONDITION_FIELD_DEFINITIONS as defs  # noqa: N811
    keys = [d['key'] for d in defs if d.get('plural')]
    got = node(client_modules, 'conditions.js', f"console.log(JSON.stringify({json.dumps(keys)}.map(m.fieldNoun)))")
    for key, (one, many) in zip(keys, got):
        assert many == next(d['plural'] for d in defs if d['key'] == key)
        assert one != many and not one.split()[0].endswith('s'), (key, one)  # a singular, never "1 more ranks"
