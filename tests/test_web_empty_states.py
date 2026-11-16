"""C18: an empty state is `empty()` (a page or section) or `emptyLine()` (a card, panel, popover or list) from dom.ts,
never a hand-written muted "No …" paragraph: one look, one short sentence and at most one next action
(DESIGN.md › No dead ends)."""
import re
from collections import Counter
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'apps/web/src'

# A muted p/div whose text (or a ternary's first branch) starts "No", "Nothing", "None" or "Nobody".
HAND_EMPTY = re.compile(
    r"""el\('(?:p|div)',\{class:'[^']*\bmuted\b[^']*'[^{}]*\},\s*(?:[^,()'`]*\?)?['`](?:No|Nothing|None|Nobody)\b""")

# Ratchet: existing copies are listed below. Counts may only go down.
LANE_ALLOWED = {
    'fix_list.ts': 1,  # FIXLIST-UX ("No justification recorded" is a quoted fact)
    'planner_rules.ts': 8,  # ROTA-RULES-UX: the rules / selection panel
    'schedule_export.ts': 1,  # LABEL-PROPS
    'score_review.ts': 1,  # AUTOSCHED-UX
    'timeline.ts': 2,  # TIMELINE-UX
}


def _found():
    return Counter({p.name: len(HAND_EMPTY.findall(p.read_text(encoding='utf-8'))) for p in SRC.glob('*.ts')
                    if p.name != 'dom.ts'})


def test_empty_states_use_the_shared_helpers():
    found = _found()
    over = {name: n for name, n in found.items() if n > LANE_ALLOWED.get(name, 0)}
    assert not over, f'Use empty() or emptyLine() from dom.ts, not a muted "No …" paragraph: {over}'
    shrunk = {name: (n, found.get(name, 0)) for name, n in LANE_ALLOWED.items() if found.get(name, 0) < n}
    assert not shrunk, f'Migrated: lower LANE_ALLOWED to the new counts (allowed, now): {shrunk}'


def test_the_guard_sees_the_variants():
    for line in ("el('p',{class:'muted'},'No saved versions yet.')",
                 "el('div',{class:'person muted'},'No people assigned')",
                 "el('p',{class:'rk-none muted'},q?'No one matches that search.':'No one in this view.')",
                 "el('p',{class:'muted home-none'},`No ${words[1]} yet.`)",
                 "el('p',{class:'muted',role:'listitem'},'Nobody to show')"):
        assert HAND_EMPTY.search(line), line
    for line in ("emptyLine('No saved versions yet.')", "el('p',{class:'muted'},'Notes')",
                 "el('p',{class:'hint'},'No duties yet.')"):
        assert not HAND_EMPTY.search(line), line
