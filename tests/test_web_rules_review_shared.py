"""Rules UI/UX review items maintenance, maintenance and maintenance (11 Nov 2026), guarded at their shared roots in the web source.

maintenance a disabled button always says why: `button()` refuses `disabled` without `why` (dom.ts `ButtonAttrs`), and no call
   site builds one without it. maintenance/maintenance every mark the Rules list and a rule's lines draw is tagged (`ruleMark`,
   `data-rule-mark`) with an entry of the one Rules key (`RULE_MARKS`), which the shared `legend()` lists on one line
   with "+N". maintenance a rank is never offered a red-family colour and its key swatch is drawn in rank tints only."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'apps/web/src'
DOM = (SRC / 'dom.ts').read_text(encoding='utf-8')
CATALOGUE = (SRC / 'rule_catalogue.ts').read_text(encoding='utf-8')
TREE = (SRC / 'rules_tree.ts').read_text(encoding='utf-8')
FORMS = (SRC / 'forms.ts').read_text(encoding='utf-8')
CSS = (ROOT / 'apps/web/public/assets/app.css').read_text(encoding='utf-8')


def _opener(text, at):
    """The name of the call whose open parenthesis encloses `at` ('' when none on the way back)."""
    depth = 0
    for i in range(at - 1, -1, -1):
        ch = text[i]
        if ch == ')':
            depth += 1
        elif ch == '(':
            if depth == 0:
                m = re.search(r'([A-Za-z_$][\w$]*)\s*$', text[:i])
                return m.group(1) if m else ''
            depth -= 1
    return ''


def _last_arg(text, at):
    """The quoted key that ends the call opened just before `at` (`ruleMark(node,'key')`), '' when it is not a literal."""
    depth, i = 0, at
    while depth >= 0:
        depth += {'(': 1, ')': -1}.get(text[i], 0)
        i += 1
    m = re.search(r"'([a-z_]+)'\)$", text[at:i])
    return m.group(1) if m else ''


def _objects(text):
    """Flat object literals `{…}` holding a `disabled:` key, with their start offsets."""
    return [(m.start(), m.group(0)) for m in re.finditer(r'\{[^{}]*\bdisabled:[^{}]*\}', text)]


def test_button_refuses_a_disabled_state_without_a_reason():
    assert 'export type ButtonAttrs=Record<string,unknown>&({disabled?:undefined;why?:undefined}|{disabled:boolean;why:string});' in DOM
    assert "kind = 'secondary', attrs: ButtonAttrs = {}) {" in DOM
    # The why is visible words right after the button and its description, shown only while it is disabled and not busy.
    assert "attributeFilter:['disabled','aria-busy']" in DOM
    assert "b.setAttribute('aria-describedby'" in DOM and "class:'why-unavailable'" in DOM


def test_no_button_is_built_disabled_without_its_reason():
    bare = []
    for path in sorted(SRC.glob('*.ts')):
        if path.name == 'generated.ts':
            continue
        text = path.read_text(encoding='utf-8')
        for at, obj in _objects(text):
            if _opener(text, at) == 'button' and not re.search(r'\bwhy:', obj):
                bare.append(f'{path.name}:{text.count(chr(10), 0, at) + 1} {obj[:80]}')
    assert not bare, f'Disabled buttons with no reason (pass why, review #7): {bare}'


def test_a_rule_waiting_for_approval_says_why_and_offers_open_it():
    # A waiting rule explains the disabled Propose action and offers Open it; DEV keeps the new-line control absent.
    assert '`${e.name} already has a change waiting for approval`' in CATALOGUE
    assert "button('Open it',()=>decideProposal()" in CATALOGUE
    assert "const add:HTMLButtonElement|null=editable?button('+ New line'" in CATALOGUE
    assert ":blocked?disabledWhy(button('+ New line'" not in CATALOGUE
    # The Rules list: Propose says why; the rule page keeps the DEV omission for "+ New line" while waiting.
    assert 'disabledWhy(proposeBtn,n?\'\':TERMS.nothingToPropose)' in TREE
    assert "`${e.name} already has a change waiting for approval`" in TREE and "button('Open it',()=>proposalPanel(" in TREE


def _rule_marks():
    block = CATALOGUE[CATALOGUE.index('const RULE_MARKS={'):CATALOGUE.index('export type RuleMarkKey')]
    return set(re.findall(r'(?:^|[{,\s])(\w+):\[', block))


def test_every_mark_the_rules_renderers_draw_has_a_key_entry():
    keys = _rule_marks()
    used = set()
    for text in (CATALOGUE, TREE):
        used |= {_last_arg(text, m.end()) for m in re.finditer(r'\bruleMark\(', text)} - {''}
        for m in re.finditer(r"'data-rule-mark':([^,}]*(?:\?[^,}]*)*)", text):
            used |= set(re.findall(r"'(\w+)'", m.group(1)))
        for line in (x for x in text.splitlines() if 'ruleMark(' in x):  # a key chosen by a ternary: both keys
            used |= {k for pair in re.findall(r"\?'([a-z_]+)':'([a-z_]+)'\)", line) for k in pair}
    # A line's check badge is marked by its issue kind: each kind the cell names is an entry.
    used |= set(re.findall(r"x\.kind==='(\w+)'\?'⚠", CATALOGUE))
    # The DEV Rules tree does not draw a Must fix badge; dead mustFixMark was removed with that behaviour.
    assert {'safety', 'attention', 'waiting', 'off', 'tag', 'not', 'proposed', 'starts_later', 'no_one',
            'gives_way'} <= used, sorted(used)
    missing = used - keys
    assert not missing, f'Marks drawn without a Rules key entry (RULE_MARKS): {sorted(missing)}'
    # The key enters exactly what is tagged: both Rules views stamp what they draw.
    assert 'stamp<T extends Child|Child[]>(nodes:T):T' in CATALOGUE
    assert 'return marks.stamp(out);' in TREE and 'return marks.stamp([row]);' in TREE
    for cell in ('marks.stamp(reachNode(', 'marks.stamp(checkCell(', 'return key?key.stamp(list):list;'):
        assert cell in CATALOGUE, cell


def test_the_legend_is_one_line_below_its_view_with_plus_n():
    legend = DOM[DOM.index('export function legend('):DOM.index('export type Legend=')]
    assert "overflowMore(n,['legend item','legend items']" in legend and 'observeSize(root,' in legend
    assert re.search(r'\.view-legend\{[^}]*flex-wrap:nowrap;overflow:hidden', CSS)
    assert '.view-legend li[hidden]{display:none}' in CSS
    assert '@media print{.view-legend{flex-wrap:wrap}.view-legend li[hidden]{display:flex}' in CSS
    # The Rules list's key sits after the table it explains.
    assert 'holder.replaceChildren(t.root,body,keyRoot,bar.root);' in TREE


def test_a_rank_is_never_offered_a_problem_colour_and_its_key_swatch_is_rank_tints():
    assert re.search(r"export const PROBLEM_HUES:readonly string\[\]=\['coral','pink'\];", DOM)
    assert re.search(r"colourSwatches\([^;]*'This rank’s one colour everywhere it is shown\.[^;]*,PROBLEM_HUES\);", FORMS)
    assert ".filter(([v])=>!exclude.includes(v))" in FORMS
    swatch = re.search(r'\.lg-swatch\.lg-ranks\{[^}]*\}', CSS).group(0)
    assert re.findall(r'var\(--(\w+)\)', swatch) and all(re.fullmatch(r'rank\d+', t) for t in re.findall(r'var\(--(\w+)\)', swatch))
    assert "rank:[()=>el('span',{class:'lg-swatch lg-ranks'" in CATALOGUE
    # A pill holding several ranks never reads as a Group (purple): its rank tint, or neutral words.
    assert "return v.length&&v.every(x=>/category_ids?$/.test(x.field))?tones.size===1?[...tones][0]:'all':'group';" in CATALOGUE
