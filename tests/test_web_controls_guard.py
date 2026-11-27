"""Shared controls have one home . Each row names a pattern only its owning
module may build: a copy anywhere else fails here, fast and without a browser, naming the helper to use instead.

RATCHET lists the remaining copies. It may only shrink: when a
copy is removed, its count must come down too, so a fixed site cannot quietly come back.
"""
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / 'apps/web/src'

# (name, pattern, owning files, what to use instead)
OWNED = [
    ('placement', r"\b(?:tether|untether|viewportBottom)\(", {'dom.ts'},
     'placeNear() in dom.ts: the one placement engine (flip, footer ceiling, viewport clamp, arrow)'),
    ('anchored top', r"setProperty\('top'", {'dom.ts', 'feedback_capture.ts'},
     'placeNear() in dom.ts places a panel against its anchor; do not position one by hand'),
    # The collection helpers keep their labelled menus inside a dialog (optionMenu, Saved views, the column menu).
    ('menu', r"class:['`]menu-list|role:'menu'|\bcond-menu(?![-\w])|\browMenu\(", {'dom.ts', 'table_view.ts', 'table_menu.ts'},
     'menu() in dom.ts (items as data; the popup is the menu, a child menu inside an open popover)'),
    ('popup aria', r"'aria-haspopup'", {'dom.ts', 'picker.ts', 'date_picker.ts'},
     'opens(trigger, "menu" | "dialog") in dom.ts, or menu(), which says what the button opens'),
    # A group's header is its own toggle (no disclosure arrow): built only by groupToggle (a surface passes its class).
    ('group toggle', r"class:'[^']*\b(?:wk-toggle|dt-group-toggle|tl-gtoggle|rule-group-toggle|rt-toggle)(?![-\w])", {'dom.ts'},
     'groupToggle() in dom.ts: the header is the button, its count chip says "N hidden" when collapsed'),
    # Nor any disclosure arrow, whatever the class (L-TIMELINE-1: a head flipping chevronRight/chevronDown on its state).
    ('disclosure arrow', r"'chevron(?:Right|Down)'\s*:\s*'chevron(?:Right|Down)'", set(),
     'groupToggle() in dom.ts: open or closed is said in words on the count chip, never with an arrow'),
    # One segmented choice: a radio group built by segmented() (words on the buttons, one Tab stop).
    # (Radio cards and swatches, e.g. name formats, colours, rule cards, are a different control and not counted.)
    ('segmented', r"class:['`]segmented(?![-\w])", {'dom.ts'},
     'segmented() in dom.ts: a radiogroup of worded options, arrows move, Space/auto chooses'),
    # One step rail (the number stepper of a field, stepper-box, is another control).
    ('step rail', r"class:['`](?:stepper|step-rail)(?![-\w])|class:['`]step-link", {'dom.ts'},
     'stepper() in dom.ts: numbered steps, done with a check, the current one aria-current="step"'),
    # One tab set: tabs() (APG Tabs; manual or auto activation, lazy panels).
    ('tabs', r"role:'tablist'", {'dom.ts'}, 'tabs() in dom.ts (activation, cls, panelsCls)'),
    # One "+N": overflowMore() for the button, moreWords() ("and N more") in a sentence; never "+N more" by hand.
    ('overflow', r"\+ ?\$\{[^}`]*\} more|class:['`][^'`]*\bpill-more\b", {'dom.ts'},
     'overflowMore() (a "+N" button) or moreWords() (a sentence) in dom.ts'),
    # One search box in a panel: panelSearch() (named; Esc clears first, then the panel or dock). The collection toolbar
    # builds its box with it too (live 235391b: a docked list's first Esc closed it with the text in it). The picker's and
    # the command palette's (a combobox) are their own.
    ('panel search', r"type:'search'", {'dom.ts', 'picker.ts', 'palette.ts'},
     'panelSearch() in dom.ts (named, Esc clears what is typed first)'),
    # One way to ask who can work a session (PERF async fill-options): a job where workers serve jobs, its pages,
    # cancel on change, the exact fingerprint. Never a direct POST elsewhere (a 4-minute synchronous wait).
    ('fill options', r"/api/planner/fill-options", {'candidates.ts'},
     'candidates.ts fillOptions() (or askCandidates for its pages)'),
]
# {(name, file): copies still allowed}. May only shrink.
RATCHET = {
    # Panel searches still hand-built: planner_week.ts candidates (wave-1 audit-words, names-c19b) and its hidden
    # placeholder input for candidateList.
    ('panel search', 'planner_week.ts'): 2,
    # Tab sets still hand-built: app.ts page tabs (wave-1 audit-words changes those lines); forms.ts session editor
    # (its panels live inside the form grid around the form's footer; tabs() would need a panel host first).
    ('tabs', 'app.ts'): 1, ('tabs', 'forms.ts'): 1,
    # Score check's rail: wave-1 audit-words changes those lines.
    ('step rail', 'score_check.ts'): 1,
    # Segmented choices still hand-built: app.ts Appearance (names-c19b; becomes the Account menu's radio items) and
    # Auto-schedule priorities (autosched-stop, autosched-no-chain); drafts.ts (audit-words, fixlist-live-fixes,
    # footer-health-click); fairness.ts (names-c19b). Lanes: fix_list.ts FIXLIST; view_settings.ts LABEL.
    ('segmented', 'app.ts'): 2, ('segmented', 'drafts.ts'): 1, ('segmented', 'fairness.ts'): 1, ('segmented', 'fix_list.ts'): 1,
    ('segmented', 'view_settings.ts'): 1,
    # Group headers still drawn with a chevron: planner_people.ts and rules_tree.ts

    # (the per-rule toggle, kept with its column head "Show all lines" so the tree changes at once) wait for wave-1
    # audit-words, footer-health-click, legend-gaps and names-c19b, which change those lines.
    ('group toggle', 'planner_people.ts'): 1, ('group toggle', 'rules_tree.ts'): 2,
    ('disclosure arrow', 'planner_people.ts'): 1, ('disclosure arrow', 'rules_tree.ts'): 2,
    # Menus still built by hand. app.ts: Account (721), Rota/Schedule tools (2154, 2178) wait for wave-1 names-c19b,
    # approval-review and footer-health-click, which change those lines (Published versions and Discarded drafts are now docked lists, C26). planner_week.ts
    # 1497/1556 and score.ts wait for wave-1 legend-gaps, names-c19b, words-badges and audit-words. schedule_export.ts:
    # lane LABEL.
    ('menu', 'app.ts'): 6, ('menu', 'planner_week.ts'): 2, ('menu', 'schedule_export.ts'): 2, ('menu', 'score.ts'): 2,
    # Popup ARIA still written by hand (use popup()/opens()): the rest sit on lines unmerged wave-1 branches change
    # (app.ts: names-c19b, approval-review, fixlist-live-fixes, footer-health-click, inbox-names; score_check.ts,
    # bulk_change.ts, fairness.ts: audit-words; table_view.ts: safety-focus; planner_week.ts: legend-gaps, names-c19b;
    # published_changes.ts: approval-review; rule_line_reach.ts: names-c19b; score.ts, issue_marker.ts:
    # fixlist-live-fixes). fix_list.ts: lane FIXLIST; schedule_export.ts: lane LABEL.
    ('popup aria', 'app.ts'): 8, ('popup aria', 'bulk_change.ts'): 1, ('popup aria', 'fairness.ts'): 1,
    ('popup aria', 'fix_list.ts'): 1, ('popup aria', 'issue_marker.ts'): 1, ('popup aria', 'planner_week.ts'): 1,
    ('popup aria', 'published_changes.ts'): 1, ('popup aria', 'rule_line_reach.ts'): 1, ('popup aria', 'schedule_export.ts'): 1,
    ('popup aria', 'score.ts'): 1, ('popup aria', 'score_check.ts'): 7, ('popup aria', 'table_view.ts'): 2,
}


def code(text):
    """The source without comments, so a comment naming a helper is not a call."""
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    return re.sub(r'(?m)(^|[^:\'"`\\])//.*$', r'\1', text)


def counts(pattern):
    found = {}
    for path in sorted(SRC.glob('*.ts')):
        if path.name == 'generated.ts':
            continue
        n = len(re.findall(pattern, code(path.read_text(encoding='utf-8'))))
        if n:
            found[path.name] = n
    return found


@pytest.mark.parametrize('name,pattern,owners,instead', OWNED, ids=[row[0] for row in OWNED])
def test_shared_control_is_built_only_by_its_owner(name, pattern, owners, instead):
    found = {f: n for f, n in counts(pattern).items() if f not in owners}
    over = {f: n for f, n in found.items() if n > RATCHET.get((name, f), 0)}
    assert not over, f'{name} built outside {sorted}: {over}. Use {instead}.'
    fixed = {f: n for (row, f), n in RATCHET.items() if row == name and found.get(f, 0) < n}
    assert not fixed, f'{name}: fewer copies now, lower RATCHET to {found}'


def test_the_owners_still_hold_the_helpers():
    dom = (SRC / 'dom.ts').read_text(encoding='utf-8')
    assert re.search(r'export function placeNear\(', dom)
    # popover, the hover card and the picker are all placed by it (no engine of their own).
    assert len(re.findall(r'\bplaceNear\(', code(dom))) >= 3
    assert 'placeNear(' in (SRC / 'picker.ts').read_text(encoding='utf-8')
    # The collection toolbar's search is the panel search (Esc clears first), not a box of its own.
    assert 'panelSearch(' in code((SRC / 'table_view.ts').read_text(encoding='utf-8'))
    # One action menu and one way for a button to say what it opens.
    assert re.search(r'export function menu\(', dom) and re.search(r'export function opens<', dom)
    assert re.search(r'export function segmented<', dom), 'segmented(): the one segmented choice'
    assert re.search(r'export function stepper\(', dom), 'stepper(): the one step rail'
    assert 'export function overflowMore(' in dom and 'export const moreWords=' in dom
    assert 'export function panelSearch(' in dom
    assert re.search(r'export const popup=', dom), "popup(kind): the one source of a button's popup ARIA"


def test_comments_do_not_count():
    assert code("a(); // tether(x)\n/* viewportBottom() */ b('http://x');") == "a(); \n b('http://x');"


def test_bottom_bars_take_their_layer_from_the_one_scale():
    """Floating layers come from one scale (app.css :root, DESIGN.md › Visual contract): the selection bar and the rule
    highlight bar share --z-bottom-bar, and the highlight rises above the selection bar while it shows (live 235391b: the
    bar covered the locked highlight)."""
    css = (SRC.parents[0] / 'public/assets/app.css').read_text(encoding='utf-8')
    assert re.search(r'--z-bottom-bar:\d+', css)
    for selector in (r'\.selection-bar\{', r'\.rk-spot\{position:fixed'):
        rule = re.search(selector + r'[^}]*\}', css).group(0)
        assert 'z-index:var(--z-bottom-bar)' in rule, rule
    assert re.search(r'html:has\(\.selection-bar:not\(\[hidden\]\)\) \.rk-spot\{bottom:calc\([^}]*--selection-bar-h', css)


def test_footer_links_and_legend_overflow_draw_their_focus_ring_inside_the_clipped_strip():
    """D-163 keeps View settings as the single owner of legend content; the legend's remaining +N control still needs the
    shared inside-ring rule because the strip clips its contents."""
    css = (SRC.parents[0] / 'public/assets/app.css').read_text(encoding='utf-8')
    rule = re.search(r':is\(([^{]*)\):focus-visible\{outline-offset:-2px\}', css)
    assert rule, 'the one inside-ring focus rule is missing'
    selectors = {s.strip() for s in rule.group(1).split(',')}
    assert {'.app-footer .link-button', '.view-legend .pill-more'} <= selectors, sorted(selectors)
