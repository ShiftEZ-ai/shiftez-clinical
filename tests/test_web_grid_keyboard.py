"""maintenance (WCAG 2.1.1, 2.4.3; WAI-ARIA APG grid pattern, roving tabindex): every role=grid renderer in the web client
keeps exactly one item in the Tab order. Fast and non-browser: it reads the TypeScript sources, so a new grid without
the shared roving tabindex (planner_week.ts `roving`) fails here before it ships. The browser journey is
tests/test_browser_grid_keyboard.py.
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'apps/web/src'
GRID = re.compile(r"role:\s*'grid'")
# Grids with a roving tabindex of their own (one cell tabIndex 0, the others -1), each checked below.
OWN_ROVING = {'sheet.ts': "td.tabIndex=here?0:-1"}


def test_every_role_grid_renderer_has_one_tab_stop():
    seen = []
    for path in sorted(SRC.glob('*.ts')):
        text = path.read_text(encoding='utf-8')
        if not GRID.search(text):
            continue
        seen.append(path.name)
        if path.name in OWN_ROVING:
            assert OWN_ROVING[path.name] in text, f'{path.name}: its own roving tabindex is gone'
        else:
            assert re.search(r'\broving\(grid,', text), f'{path.name} draws a role=grid without the shared roving()'
    assert {'planner_week.ts', 'planner_people.ts', 'schedule_grid.ts', 'sheet.ts'} <= set(seen)


def test_roving_keeps_resize_grips_out_of_the_grid_and_restores_its_stop():
    text = (SRC / 'planner_week.ts').read_text(encoding='utf-8')
    body = text[text.index('export function roving('):]
    body = body[:body.index('\nfunction ')]
    # A grip is no grid item (its arrows resize): out of the set, in the Tab order only behind its focused heading.
    assert "classList.contains('dt-resize')" in body and '!grip(n)' in body
    assert 'n.parentElement===current' in body
    assert 'grip(from)' in body, 'arrows on a grip must stay the grip\'s own'
    # Redrawn: the last focused item (by name) gets the stop back, else today's session, else the first cell.
    assert 'lastRoving' in body and ".closest('.today')" in body and 'pool.find(cell)' in body
    # The focused item removed in place: focus goes to its successor, never to the page.
    assert 'document.activeElement===document.body' in body
    # A gripped heading joins the roving set, so arrows reach it and Tab its grip.
    widths = text[text.index('export function scheduleWidths('):]
    assert 'at.tabIndex=-1' in widths[:widths.index('\n}\n')]


def test_a_resize_grip_keeps_focus_after_a_keyboard_resize():
    text = (SRC / 'table_menu.ts').read_text(encoding='utf-8')
    grip = text[text.index('export function columnGrip('):]
    keys = grip[grip.index("addEventListener('keydown',e=>{if(e.key!=='ArrowLeft'"):]
    keys = keys[:keys.index('\n}')]
    assert 'requestAnimationFrame' in keys and '.dt-resize[aria-label=' in keys and '.focus(' in keys


def test_qa127_schedule_semantics_stay_in_their_shared_helpers():
    week = (SRC / 'planner_week.ts').read_text(encoding='utf-8')
    # (a) Every Week session item is named with its place, from one helper.
    assert 'const sessionWords=(s:Row)=>' in week and 'labelOf(locations.get(s.location_id))' in week
    assert '${s.type_snapshot.name}, ${formatDate(s.start,zone)} ${time(s)}`,' in week  # only the hovercard title
    assert week.count('${s.type_snapshot.name}, ${formatDate(s.start,zone)} ${time(s)}') == 1
    # (c) The Why list is not a live region; a short summary is.
    why = (SRC / 'planner_why.ts').read_text(encoding='utf-8')
    assert "class:'why-list','aria-live'" not in why and "role:'status'" in why and 'said.textContent=' in why
    # (d) A step of the period navigator says the new range politely, through the shared announce().
    nav = (SRC / 'schedule.ts').read_text(encoding='utf-8')
    assert 'announce(' in nav and ',true);' in nav[nav.index('let stepped='):nav.index('const nav=periodNav(')]
    dom = (SRC / 'dom.ts').read_text(encoding='utf-8')
    assert "export function announce(message: string, polite = false)" in dom
    # (e) Every shared table has a caption.
    table = (SRC / 'table_view.ts').read_text(encoding='utf-8')
    assert "el('caption',{class:'sr-only'}" in table
