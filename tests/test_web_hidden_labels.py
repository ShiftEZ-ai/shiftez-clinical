"""A button that shrinks to its icon keeps its words for assistive technology (WCAG 2.2 4.1.2 Name, 2.5.3 Label in Name).

Compact toolbars and bars hide a button's label span (`.label`, `.dt-tool-label`) when space runs out. `display:none`
removes the text from the accessibility tree, so a button without its own aria-label (e.g. "Show changed · 1") is left
nameless. Those rules must clip the label visually instead (the `.sr-only` declarations).

Labels are hidden only by clipping: no rule sets `display` or `visibility` on a button label. A "hide here, show again
there" pair of display rules is how a clip rule can silently start applying where it was always overridden."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / 'apps/web/public/assets/app.css').read_text(encoding='utf-8')
LABEL = re.compile(r'\.(dt-tool-label|label)(?![\w-])[^\s>+~,]*$')


def rules(css):
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', css):
        decl = {k.strip(): v.strip() for k, v in (d.split(':', 1) for d in m.group(2).split(';') if ':' in d)}
        yield m.group(1).strip(), decl


def hidden_labels(css):
    """Button-label selectors whose rule sets display or visibility."""
    return [sel for sel, decl in rules(css) if {'display', 'visibility'} & decl.keys()
            for sel in [s.strip() for s in sel.split(',')] if LABEL.search(sel)]


def test_button_labels_are_hidden_only_by_clipping():
    found = hidden_labels(CSS)
    assert not found, 'Hide button labels only by clipping (sr-only declarations), never display/visibility:\n' + '\n'.join(found)


def test_the_scan_finds_hidden_labels():
    css = """.a .dt-tool-label{display:none}@media(x){.b>.label,.c{display:none}}
    .d .label{display:inline}.e .label{visibility:hidden}
    .ok1 .label-x{display:none}.ok2 .label{position:absolute;clip-path:inset(50%)}.ok3 .label .x{display:none}"""
    assert hidden_labels(css) == ['.a .dt-tool-label', '.b>.label', '.d .label', '.e .label']


def test_schedule_redraw_does_not_anchor_the_window():
    """Clipped labels change position and size when fitRow refits the redrawn planner bar, which suppresses Chromium's
    scroll-anchoring correction after the unfitted bar's growth; the Schedule turns window anchoring off instead."""
    assert ('html:has(#main.page-planner)', {'overflow-anchor': 'none'}) in list(rules(CSS))
