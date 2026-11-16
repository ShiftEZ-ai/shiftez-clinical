"""No coloured side bars, ever : no card, row, list item, panel or callout
carries a coloured bar or stripe on its edge. A hue is shown as a dot or swatch beside its label; only neutral 1px
dividers may sit on a side. Scans the shared stylesheets and the web client's sources, so a new stripe fails here."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHEETS = [ROOT / 'apps/web/public/assets/app.css', ROOT / 'apps/web/public/assets/print.css',
          ROOT / 'apps/web/public/assets/schedule_teams.css']
NEUTRAL = re.compile(r'^(transparent|none|0|var\(--(line|line-soft|line-2|control-line|surface|subtle|sub)\))$')
# Not edges of a card, row or panel: a positioned "now" line inside the timeline.
ALLOWED = {'.tl-line,.tl-today', '.tl-today'}
SIDE = r'border-(?:left|right|inline-start|inline-end)'


def rules(css):
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', css):
        yield m.group(1).strip(), {k.strip(): v.strip() for k, v in
                                   (d.split(':', 1) for d in m.group(2).split(';') if ':' in d)}


def px(token):
    m = re.fullmatch(r'(-?[\d.]+)(px)?', token)
    return float(m.group(1)) if m else None


def colour_of(parts):
    return ' '.join(p for p in parts if px(p) is None and p not in ('solid', 'dashed', 'dotted', 'double', 'inset')) or '0'


def side_bars(selector, decl):
    """Why this rule paints a side bar, or nothing."""
    for prop, value in decl.items():
        value = value.replace('!important', '').strip()
        if re.fullmatch(SIDE, prop):
            parts = value.split()
            width = next((px(p) for p in parts if px(p) is not None), 0)
            colour = colour_of(parts)
            if width and (width >= 2 or not NEUTRAL.match(colour)):
                yield f'{prop}:{value}'
        elif re.fullmatch(SIDE + r'-color', prop) and not NEUTRAL.match(value):
            yield f'{prop}:{value}'
        elif prop == 'box-shadow':
            # inset X 0 [blur] [spread] colour, X != 0, no blur: a vertical stripe |X|+spread wide.
            for shadow in re.split(r',(?![^()]*\))', value):
                parts = shadow.split()
                if 'inset' not in parts:
                    continue
                nums = [px(p) for p in parts if px(p) is not None]
                if len(nums) < 2 or not nums[0] or nums[1] or (len(nums) > 2 and nums[2]):
                    continue
                width = abs(nums[0]) + (nums[3] if len(nums) > 3 else 0)
                if width > 0 and (width >= 2 or not NEUTRAL.match(colour_of(parts))):
                    yield f'box-shadow:{shadow.strip()}'
    if re.search(r'::?(before|after)', selector) and decl.get('position') == 'absolute':
        tall = decl.get('top') == '0' and decl.get('bottom') == '0' or decl.get('height') == '100%'
        width = px(decl.get('width', ''))
        paint = decl.get('background', decl.get('background-color', 'transparent'))
        if tall and width is not None and 0 < width <= 4 and not NEUTRAL.match(paint):
            yield f'{selector} is a {width:g}px bar of {paint}'


def test_no_stylesheet_paints_a_coloured_side_bar():
    found = []
    for sheet in SHEETS:
        for selector, decl in rules(sheet.read_text(encoding='utf-8')):
            if selector in ALLOWED:
                continue
            found += [f'{sheet.name}: {selector} {{{why}}}' for why in side_bars(selector, decl)]
    assert not found, 'Coloured side bars (use a dot or swatch beside the label):\n' + '\n'.join(found)


def test_no_source_sets_a_side_bar_inline():
    pattern = re.compile(r"borderLeft|borderRight|borderInline|'border-(left|right|inline)|inset \d+px 0 0")
    found = [f'{f.relative_to(ROOT)}:{n}' for f in (ROOT / 'apps/web/src').rglob('*.ts')
             for n, line in enumerate(f.read_text(encoding='utf-8').splitlines(), 1) if pattern.search(line)]
    assert not found, found


def test_the_scan_catches_every_kind_of_stripe():
    css = """.a{border-left:3px solid var(--duty)}.b{box-shadow:inset 3px 0 0 var(--accent)}
    .c{border-inline-start:2px solid var(--line)}.d::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--ok)}
    .e{border-left:1px solid var(--accent)}.f{box-shadow:inset -2px 0 0 var(--warn),0 1px 2px red}
    .ok1{border-left:1px solid var(--line)}.ok2{box-shadow:inset 0 0 0 1px var(--accent)}.ok3{box-shadow:inset 0 -2px 0 var(--accent)}
    .ok4{border-left-color:transparent}.ok5{box-shadow:inset 13px 0 0 -12px var(--line-2)}"""
    hit = {s for s, d in rules(css) if list(side_bars(s, d))}
    assert hit == {'.a', '.b', '.c', '.d::before', '.e', '.f'}
