"""A late answer is drawn into a page still being built (dom.ts `alive`), never dropped for not being on screen yet.

renderPage builds each page detached and shows it only once its build resolves and its skeleton settles (app.ts), so a
read that answers first (a kept reply, a fast server) finds its node not `isConnected`: a guard on attachment drops
the result for good ("Loading the score review…" forever, a hidden Cancel; maintenance traces of run 42835942764). Content
attached in the same task (a dialog, a dock panel, a popover, a hover card) or redrawn only after a user action on a
shown page may keep `isConnected`; those are counted below per file. A new one fails: use `alive(node)`, or add it
here with the reason, in the same change.
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'apps' / 'web' / 'src'

# `if(... x.isConnected ...)` directly followed by a draw.
GUARD = re.compile(r"if\(([^()]|\([^()]*\))*?\b[\w.?]+\.isConnected([^()]|\([^()]*\))*?\)\s*\{?\s*(?:void\s+)?[\w.?]*?"
                   r"(replaceChildren|append|prepend|draw|render\w*|sync|refresh|fill|swap)\(")

ALLOWED = {
    'ai_provider.ts': 2,  # redrawn after a save or a Test (user actions on the shown page)
    'app.ts': 6,  # print dialog; which-rule lookup and rule history (panels, user-run); exceptions table (dock/person panel); inbox popover; outbox paging (user)
    'branding_settings.ts': 1,  # after a save
    'bulk.ts': 1,  # a shown list's redraw (watchShown handles the unshown case)
    'bulk_change.ts': 1,  # dialog
    'date_sets.ts': 1,  # dock panel
    'dom.ts': 1,  # hover card on a shown target
    'fix_list.ts': 2,  # popover
    'forms.ts': 3,  # dialogs and a dock panel
    'integrations.ts': 1,  # after a save
    'loading.ts': 1,  # `!time.isConnected`: appends its own clock once, not a late-answer guard
    'planner_people.ts': 1,  # redraw requested by a later event, never during the build
    'published_changes.ts': 2,  # dock panel
    'rotation_board.ts': 2,  # tabs of a dock panel
    'rule_simplify.ts': 2,  # dock panel
    'sign_in_config.ts': 1,  # after a save
    'table_view.ts': 4,  # after a user's change, save or Undo on a shown table
    'tags.ts': 1,  # dialog preview while typing
}


def guards():
    found = {}
    for path in sorted(SRC.glob('*.ts')):
        if path.name == 'generated.ts':
            continue
        n = sum(1 for _ in GUARD.finditer(path.read_text(encoding='utf-8')))
        if n:
            found[path.name] = n
    return found


def test_no_new_attachment_guard_around_a_late_draw():
    found = guards()
    grown = {f: (n, ALLOWED.get(f, 0)) for f, n in found.items() if n > ALLOWED.get(f, 0)}
    assert not grown, ('An answer drawn only `if(node.isConnected)` is dropped when it lands before renderPage shows the '
                       f'page: use dom.ts alive(node), or list it with its reason. (found, allowed): {grown}')
    shrunk = {f: (found.get(f, 0), n) for f, n in ALLOWED.items() if found.get(f, 0) < n}
    assert not shrunk, f'Lower the allowance to what remains (found, allowed): {shrunk}'


def test_render_page_says_which_page_is_being_built():
    app = (SRC / 'app.ts').read_text(encoding='utf-8')
    assert "const content=el('div',{});buildingPage(content);" in app
    assert 'buildingPage(null)' in app
