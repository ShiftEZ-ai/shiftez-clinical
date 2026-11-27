"""Every side panel goes through the one shared panel (side_panel.ts sidePanel, which dockPanel uses), so every panel
kind gets the same resize handle (owner requirement, 8 Oct 2026). This enumerates the panel kinds in the web client: a
new kind fails here until it is listed (and, where it can be opened in a few steps, checked in the browser by
tests/test_browser_shared_controls.py::test_every_side_panel_has_the_resize_handle_on_desktop_only)."""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'apps' / 'web' / 'src'
# <aside> elements that are not side panels: the navigation, the Setup guide's own page column and the rotation board's
# palette of rotations to drag (a column of the board, not a panel).
NOT_PANELS = {'sidebar', 'setup-aside', 'rb-palette'}
KINDS = {
    # docked item and task panels (dockPanel)
    'person', 'request', 'tag', 'rule', 'rule-line', 'session-details', 'session-edit', 'rota-targets', 'fairness-drill',
    'staffing', 'staff-import', 'staff-onboarding', 'other-department-rules', 'pending-rules', 'rule-exceptions',
    'calendar-protection', 'simplify-rules', 'feedback', 'rotation', 'trainee', 'date-set-dates', 'bulk-change',
    'timeline-item', 'report-duties', 'fix-list',
    # lists and drill-downs moved out of modal dialogs (C26)
    'published-versions', 'archived-rotas', 'new-issues', 'fairness-month', 'fairness-goal', 'duty-people', 'publication-changes',
    'matched-automatically', 'self-approved',
    # page panels (sidePanel)
    'auto-schedule', 'rules-in-context', 'schedule', 'day-summary', 'merge', 'notifications', 'published-changes',
    'rule-proposal',
}


def sources():
    return {p.name: p.read_text(encoding='utf-8') for p in SRC.glob('*.ts')}


def test_every_aside_is_a_shared_side_panel():
    for name, text in sources().items():
        for m in re.finditer(r"(?:const|let)\s+(\w+)\s*=\s*(?:[\w.]+\s*\|\|\s*)?el\('aside',\{class:'([^']*)'", text):
            var, cls = m.groups()
            if set(cls.split()) & NOT_PANELS:
                continue
            assert re.search(rf'sidePanel\(\s*{var}\b', text), f'{name}: <aside class="{cls}"> is not routed through sidePanel'
        for m in re.finditer(r"el\('aside',\{class:'([^']*)'", text):
            line = text[text.rfind('\n', 0, m.start()) + 1:text.find('\n', m.start())]
            if set(m.group(1).split()) & NOT_PANELS or re.search(r'(?:const|let)\s+\w+\s*=', line[:line.find("el('aside'")]):
                continue
            assert 'sidePanel(' in line, f'{name}: inline <aside class="{m.group(1)}"> is not routed through sidePanel'


def test_every_panel_kind_is_known():
    keys = set()
    for text in sources().values():
        keys |= set(re.findall(r"dockPanel\(\{key:'([\w-]+)'", text))
        keys |= set(re.findall(r"sidePanel\([^;]*?\{key:'([\w-]+)'", text))
    assert keys == KINDS, f'new: {sorted(keys - KINDS)}, gone: {sorted(KINDS - keys)}'
    # dockPanel itself is built on sidePanel: every docked kind gets the same handle.
    side = (SRC / 'side_panel.ts').read_text(encoding='utf-8')
    assert re.search(r"function show\([^)]*\)\{[\s\S]*?sidePanel\(el\('aside'", side)

# Modal dialogs are only for small forms and governed or irreversible confirmations (DESIGN.md); lists and drill-downs
# open as docked panels (dockPanel: resize grip, Back, ↑ ↓, the unsaved-changes guard, a full-width sheet on a phone).
# Every showDialog call is listed by the start of its title: a new modal needs to be one of those, and is added here.
DIALOGS = {
    ('accounts.ts', 'title,link?`Send thi'),
    ('app.ts', "'Keyboard shortcuts'"),
    ('app.ts', '`About ${brand().sho'),
    ('app.ts', "'Print sheet','',(bo"),
    ('app.ts', 'actionLabel(c),c.sen'),
    ('app.ts', "'What this rule chan"),
    ('app.ts', "'Group similar rules"),  # a governed proposal form (LEAD-UIUX, C26)
    ('app.ts', "'Which rule applies?"),  # a list: waits for wave-1 audit-words (C26)
    ('app.ts', "'Import source evide"),
    ('app.ts', "(result.kind==='vali"),
    ('app.ts', "'Edit the draft now?"),
    ('app.ts', "'Refresh draft basel"),  # nothing to apply: one line, no form (D-068)
    ('app.ts', "'Institutional sign-"),
    ('bulk.ts', "'Paste names or code"),
    ('candidate_review.ts', "'Candidates consider"),  # a list opened from the Auto-schedule results modal: moves when that does (C26)
    ('dom.ts', "'Leave this page?', "),
    ('dom.ts', 'title,description,(b'),
    ('dom.ts', 'title,description,(b'),
    ('double_booking.ts', "'Double bookings','T"),  # a blocking confirmation mid-save: save with them, or Back (awaiting LEAD-UIUX)
    ('feedback.ts', "'Send feedback','',("),
    ('feedback.ts', "'Screenshot','',body"),
    ('feedback.ts', "'Create GitHub issue"),
    ('forms.ts', "'Request a rule exce"),
    ('pwa.ts', "way==='in-app'?'Open"),
    ('requests.ts', '`Decline ${plural(op'),
    ('rule_catalogue.ts', '`History · ${e.name}'),
    ('rule_review.ts', "'Review '+(title||po"),
    ('rule_review.ts', "'Change check','This"),
    ('rule_review.ts', "w.name||'Rota check'"),
    ('schedule_export.ts', "'Print or export',''"),
}


def test_modal_dialogs_are_only_forms_and_confirmations():
    found = []
    for name, text in sources().items():
        for m in re.finditer(r"(?<![\w.])showDialog\(\s*([^\n]{0,20})", text):
            if text[max(0, m.start() - 9):m.start()] != 'function ':
                found.append((name, m.group(1)))
    new = sorted(set(found) - DIALOGS)
    assert not new, f'A new modal dialog: a list or drill-down goes in dockPanel; a small form or a confirmation is added to DIALOGS: {new}'
    assert not DIALOGS - set(found), f'Gone: remove from DIALOGS: {sorted(DIALOGS - set(found))}'
    # The fairness dashboard's lists (Balance goals "All N", a month's people, a drill-down) are all docked.
    assert not any(name == 'fairness.ts' for name, _ in DIALOGS | set(found)), 'fairness.ts opens a list in a modal: use dockPanel'
