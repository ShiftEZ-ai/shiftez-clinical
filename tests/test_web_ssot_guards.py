"""Single-source guards for the web client . Fast, non-browser: they read
the TypeScript sources, so a second path for a shared behaviour fails here before it ships.

Rank tints : a rank has one tint everywhere, from the rank's own colour in the categories
catalogue, through the one helper `rankColour` in dom.ts. No hashed colour (`colourOf`) for a rank, and no literal
purple or slate standing in for one.
"""
import re
from pathlib import Path

from .web_strings import literals, web_literals

SRC = Path(__file__).resolve().parents[1] / 'apps/web/src'
# Not hand-written client code (generated contracts), or the helper's own home.
SKIP = {'generated.ts', 'dom.ts'}
RANKISH = re.compile(r"categor|rank|role(?:Of|Row|-none)", re.I)
# Ratchet: remaining call sites that may colour a rank another way. May only shrink.
RANK_HUE_ALLOWED: set[tuple[str, str]] = set()


def sources():
    for path in sorted(SRC.glob('*.ts')):
        if path.name not in SKIP:
            yield path, path.read_text(encoding='utf-8')


def call_args(text, name):
    """The argument text of every `name(...)` call (balanced parentheses)."""
    for m in re.finditer(re.escape(name) + r'\(', text):
        depth, i = 1, m.end()
        while i < len(text) and depth:
            depth += {'(': 1, ')': -1}.get(text[i], 0)
            i += 1
        yield m.start(), text[m.end():i - 1]


def line_of(text, at):
    return text.count('\n', 0, at) + 1


def test_rank_hues_come_only_from_rank_colour():
    found = []
    for path, text in sources():
        for at, args in call_args(text, 'colourOf'):
            line = text[text.rfind('\n', 0, at) + 1:text.find('\n', at)]
            if RANKISH.search(args):
                found.append((path.name, f'line {line_of(text, at)}: colourOf({args[:80]})'))
            # Any catalogue kind (`rows(kind)`, `catalogues[kind]`…): ranks among them must branch to rankColour.
            elif re.search(r"\[kind\]|rows\(kind\)|find\(kind\b|kind===", text[max(0, at - 160):at] + args) and 'rankColour' not in line:
                found.append((path.name, f'line {line_of(text, at)}: colourOf({args[:80]}) for any kind, ranks included'))
        # A rank (category) list or kind forced to a literal neutral or group hue: `['categories',…]…?'slate'`.
        for m in re.finditer(r"'categories'[^;:\n]{0,60}?'(?:slate|violet|group)'|'(?:slate|violet|group)'[^;:\n]{0,30}?'categories'", text):
            found.append((path.name, f'line {line_of(text, m.start())}: {m.group(0)[:100]}'))
        # A value chip coloured by its field's kind alone: only conditions.ts reads pillKind (valueTone tints a rank).
        for m in re.finditer(r"\bpillKind\(" if path.name != 'conditions.ts' else r"rp \$\{[^}]*pillKind\(", text):
            found.append((path.name, f'line {line_of(text, m.start())}: pillKind (use valueTone)'))
    bad = [f'{f}: {what}' for f, what in found if (f, what.split(':', 1)[0]) not in RANK_HUE_ALLOWED]
    assert not bad, 'A rank hue must come from rankColour (dom.ts; DESIGN.md C21):\n' + '\n'.join(bad)


# `data-lg-self` exempts a mark from the key. tests/legend_guard.py allows it only on an element whose own visible words
# are its whole meaning: a chip naming its value (a #tag, a rank: `tag-chip`, `role-chip`, `.rp`, the tinted scope chip)
# and the Rotation board's palette; never a band, row head or pattern, which go through the view's key (`key.mark`).
SELF_NAMING = ('tag-chip', 'role-chip', '`rp ', 'rp pick-chip', 'rb-palette', 'const tinted=', 'chipsOf=')
# Ratchet: existing exemptions may only shrink.
SELF_RATCHET = {
    'app.ts': 1,            # Rules list group toggle (rule-group-toggle)
    'schedule.ts': 1,       # Year view's invisible placeholder dot
    'schedule_grid.ts': 1,  # Schedule grid duty-type row head band: should be key.mark (lane: grid)
    'planner_week.ts': 1,   # Week/Month neutral group head (headHue)
}


def test_data_lg_self_only_where_the_legend_guard_allows_it():
    counts, chips = {}, 0
    for path, text in [(p, p.read_text(encoding='utf-8')) for p in sorted(SRC.glob('*.ts')) if p.name != 'generated.ts']:
        for m in re.finditer(r"data-lg-self", text):
            if any(c in text[max(0, m.start() - 600):m.start()] for c in SELF_NAMING):
                chips += 1
            else:
                counts[path.name] = counts.get(path.name, 0) + 1
    assert chips, 'the self-naming chips were not found: check SELF_NAMING'
    grown = {f: n for f, n in counts.items() if n > SELF_RATCHET.get(f, 0)}
    assert not grown, f'data-lg-self on a band, row head or other non-chip mark (use key.mark): {grown}'
    shrunk = {f: n for f, n in SELF_RATCHET.items() if counts.get(f, 0) < n}
    assert not shrunk, f'An exemption was removed: shrink SELF_RATCHET to {counts}'


def test_no_key_entry_ties_ranks_to_one_fixed_hue():
    """A key's words never say a fixed swatch means a rank (live check 7 Nov: the Rules key read "Rank or group" in purple
    after ranks took their own tints); a rank's swatch is its rankColour."""
    hue = r"(?:teal|blue|sky|violet|amber|coral|indigo|pink|slate)"
    found = [(p.name, m.group(0)) for p, t in sources()
             for m in re.finditer(r"\('" + hue + r"'\)\s*,\s*'[^']*\b[Rr]anks?\b[^']*'", t)]
    assert not found, f'A key entry names ranks under one fixed hue: {found}'


def test_rank_colour_is_one_helper_set_when_the_catalogue_loads():
    dom = (SRC / 'dom.ts').read_text(encoding='utf-8')
    assert re.search(r'export const rankColour=', dom) and re.search(r'export const setRankColours=', dom)
    # Duty types and ranks share one assignment rule (chosen stays, then free tints one each: duty types from the duty
    # hues, never coral or slate; ranks from their own RANK_PALETTE, tests/test_web_rank_hues.py).
    assert dom.count('assignHues(') == 3, 'setTypeColours and setRankColours share assignHues'
    defined = [p.name for p, t in sources() if re.search(r'\b(?:const|function)\s+rankColour\b', t)]
    assert not defined, f'A second rank colour helper: {defined}'
    app = (SRC / 'app.ts').read_text(encoding='utf-8')
    assert "setRankColours(catalogues.categories||[])" in app, 'Ranks are coloured when the catalogue loads'
    # Every rank picker reads rank choices with their tint (forms.ts rankOptions), never the bare list.
    forms = (SRC / 'forms.ts').read_text(encoding='utf-8')
    assert forms.count("options(all(ctx,'categories'))") == 1, "Use rankOptions(ctx) for rank choices"

# Shared-UI guards from the collection safety lane.

_REGEX_BEFORE = set('(,=:[!&|?{};+-*%<>~^') | {''}


def calls(text):
    """Yields (callee, open_index, first_argument_source, enclosing_callees, enclosing_object_text) for every call
    `name(` in the text; the object text runs from the innermost enclosing object literal's `{` to the call."""
    stack, i, n, last = [], 0, len(text), ''
    templates = []  # brace depth at which each open template literal's ${…} returns to its string
    while i < n:
        c = text[i]
        if c in ' \t\r\n':
            i += 1
            continue
        if text.startswith('//', i):
            i = text.find('\n', i)
            i = n if i < 0 else i
            continue
        if text.startswith('/*', i):
            i = text.find('*/', i + 2) + 2 or n
            continue
        if c in '\'"':
            i = _skip_string(text, i, c)
            last = 'x'
            continue
        if c == '`' or (c == '}' and templates and templates[-1] == len(stack)):
            if c == '}':
                templates.pop()
            i, opened = _skip_template(text, i + 1)
            if opened:
                templates.append(len(stack))
            last = 'x'
            continue
        if c == '/' and (last in _REGEX_BEFORE or last in ('return', 'typeof')):
            i = _skip_regex(text, i)
            last = 'x'
            continue
        if c.isalnum() or c in '_$':
            j = i
            while j < n and (text[j].isalnum() or text[j] in '_$'):
                j += 1
            last = text[i:j]
            i = j
            continue
        if c in '([{':
            name = last if c == '(' and re.fullmatch(r'[A-Za-z_$][\w$]*', last or '') else ''
            if c == '(' and last == '>':  # a generic call: command<{job:Row}>(…)
                m = re.search(r'([A-Za-z_$][\w$]*)\s*<[^()]*>\s*$', text[max(0, i - 300):i])
                name = m.group(1) if m else ''
            if name:
                objects = [at for _, at, obj in stack if obj]
                yield name, i, _first_arg(text, i + 1), [s for s, _, _ in stack if s], text[objects[-1]:i] if objects else ''
            # An object literal (not a block): its text up to a call says what the call is part of ({…,armed:true,run:…}).
            stack.append((name if c == '(' else '', i, c == '{' and last in set('(,[:?=') | {'return'}))
        elif c in ')]}':
            if stack:
                stack.pop()
        last = c
        i += 1


def _skip_string(text, i, quote):
    i += 1
    while i < len(text) and text[i] != quote:
        i += 2 if text[i] == '\\' else 1
    return i + 1


def _skip_template(text, i):
    """From inside a template literal to just after its end (False) or its next `${` (True)."""
    while i < len(text):
        if text[i] == '\\':
            i += 2
        elif text[i] == '`':
            return i + 1, False
        elif text.startswith('${', i):
            return i + 2, True
        else:
            i += 1
    return i, False


def _skip_regex(text, i):
    i, klass = i + 1, False
    while i < len(text) and text[i] != '\n':
        c = text[i]
        if c == '\\':
            i += 2
            continue
        if c == '[':
            klass = True
        elif c == ']':
            klass = False
        elif c == '/' and not klass:
            i += 1
            while i < len(text) and text[i].isalpha():
                i += 1
            return i
        i += 1
    return i


def _first_arg(text, i):
    m = re.match(r"\s*(?:<[^>()]*>\s*\()?\s*(['\"`])((?:(?!\1).)*)\1", text[i:i + 300])
    return m.group(2) if m else None


def test_scanner_sees_the_enclosing_calls():
    src = (r"const a='x(',b=/\)(/g;armed(button('Delete',async()=>{await command('thing.delete',{reason:`a ${f(1)} )`});}),"
           "'Delete?');acts.push({label:'End',armed:true,run:()=>command<{n:number}>('other.end',{})});"
           "// command('in.comment.delete')\n")
    found = [(arg, outer, obj) for name, _, arg, outer, obj in calls(src) if name == 'command']
    assert found == [('thing.delete', ['armed', 'button'], ''),
                     ('other.end', ['push'], "{label:'End',armed:true,run:()=>command<{n:number}>")]


# ---- 1. destructive commands: Undo or the same-button confirm

DESTRUCTIVE = re.compile(r'(?:^|[._])(delete|remove|discard|dismiss|archive|retire|revoke|end|cancel|withdraw|retract|'
                         r'decline|reject|disable|disconnect|unsubscribe)(?:$|[._])')
CONFIRMS = {'armed', 'confirmAction'}
# Kinds whose verb sounds destructive but which lose nothing, so they act at once without Undo (DESIGN.md D42).
LOSES_NOTHING = {
    'job.cancel': 'stops a running check or merge job; the saved results stay as they were',
    'policy.withdraw_approval': 'is itself the Undo of an approval just given (api.ts withdrawApprovals)',
    'publication.changes.withdraw': 'takes a set under review back into the list, unsent; every change is kept '
                                    '(also the Undo of Send, published_changes.ts)',
    'push.unsubscribe': 'turns alerts off for a device; Turn on subscribes again (notifications.ts arms Remove for '
                        'another device)',
}
# Sent on one click without an UNDO entry, by (file, kind), each with why it waits. Only files another lane owns may wait
# here (FIX-BRIEF); every other caller is fixed instead. This list may only shrink.
ALLOWED = {}


def undoable_kinds():
    """The command kinds api.ts UNDO reverses: its literal keys, plus those its Object.fromEntries rows build."""
    text = (SRC / 'api.ts').read_text(encoding='utf-8')
    block = text[text.index('const UNDO:'):]
    block = block[:block.index('\n};\n')]
    kinds = set(re.findall(r"^\s*'([a-z_]+(?:\.[a-z_]+)+)':", block, re.M))
    kinds |= set(re.findall(r",'([a-z_]+(?:\.[a-z_]+)+)':", block))
    for group in re.findall(r"Object\.fromEntries\(\[([^\]]*)\]\.(?:map|flatMap)", block):
        kinds |= set(re.findall(r"'([a-z_]+(?:\.[a-z_]+)+)'", group))
    # The person-fact families: each of propose/activate/retire, reversed through <family>.undo.
    for group in re.findall(r"Object\.fromEntries\(\[([^\]]*)\]\.flatMap\(f=>", block):
        for family in re.findall(r"'([a-z_.]+)'", group):
            kinds |= {f'{family}.{a}' for a in ('propose', 'activate', 'retire')}
    return kinds


def destructive_sends():
    """Each command(…) whose literal kind names a destructive verb: (file, kind, line, safe)."""
    undo = undoable_kinds()
    for path in sorted(SRC.glob('*.ts')):
        if path.name == 'generated.ts':
            continue
        text = path.read_text(encoding='utf-8')
        for name, at, kind, outer, obj in calls(text):
            if name == 'command' and kind and DESTRUCTIVE.search(kind.replace('${', '.').replace('}', '.')):
                # A row, bulk or selection action marked armed:true is wrapped in armed() by its shared renderer.
                safe = kind in undo or kind in LOSES_NOTHING or bool(CONFIRMS & set(outer)) or 'armed:true' in obj
                yield path.name, kind, text.count('\n', 0, at) + 1, safe


def test_destructive_commands_have_undo_or_the_same_button_confirm():
    assert {'table_view.delete', 'ai_provider.delete', 'rotation.remove', 'workspace.archive',
            'availability_pattern.end', 'person.workload.retire'} <= undoable_kinds()
    sends = list(destructive_sends())
    assert len(sends) > 20, 'the scan found too few command sends: has command() been renamed?'
    unsafe = {(name, kind): line for name, kind, line, safe in sends if not safe}
    new = {k: v for k, v in unsafe.items() if k not in ALLOWED}
    assert not new, ('A destructive command sent on one click: give its kind a reverse in api.ts UNDO (its success '
                     'message then offers Undo) or send it from inside armed(…) / confirmAction(…) (dom.ts): '
                     + ', '.join(f'{n}:{line} {k}' for (n, k), line in sorted(new.items())))
    assert not set(ALLOWED) - set(unsafe), f'Safe now: remove from ALLOWED: {sorted(set(ALLOWED) - set(unsafe))}'


# ---- 2. the filtered strip: one implementation, in collectionToolbar, fed by every caller

# Toolbars that do not report what they show yet, by (file, label), with what they wait for. This list may only shrink.
STRIP_PENDING = {
    ('app.ts', 'Schedule'): 'counts differ per layout and period (Week, Day, List, Calendar, People, Year), drawn in '
                            'planner_week.ts/planner_people.ts/schedule.ts; its empty states carry Show all '
                            '(schedule.ts scheduleEmpty) until each renderer reports its period counts',
}


def toolbar_labels(text):
    for m in re.finditer(r'collectionToolbar\(\{', text):
        label = re.search(r"label:'([^']+)'", text[m.end():m.end() + 200])
        yield label.group(1) if label else '?'


def test_every_collection_toolbar_says_what_it_shows():
    """A caller gives the strip its counts (`<toolbar>.shown(count, of, noun)` after each draw); its Show all comes from
    the toolbar's own search and filter (filter.reset, back to the view's own filters), so it clears exactly those."""
    missing, seen = [], set()
    for path in sorted(SRC.glob('*.ts')):
        text = path.read_text(encoding='utf-8')
        labels = [label for label in toolbar_labels(text)]
        seen |= {(path.name, label) for label in labels}
        needed = [label for label in labels if (path.name, label) not in STRIP_PENDING]
        reports = len(re.findall(r'\b[\w$]+\??\.shown\(', text))
        if reports < len(needed):
            missing.append(f'{path.name}: {len(needed)} toolbars ({", ".join(needed)}), {reports} .shown( calls')
    assert len(seen) >= 10, 'found too few collection toolbars: has collectionToolbar been renamed?'
    assert not missing, 'A collection toolbar that never says what it shows (its filtered strip and Show all): ' + '; '.join(missing)
    assert set(STRIP_PENDING) <= seen, f'Gone: remove from STRIP_PENDING: {sorted(set(STRIP_PENDING) - seen)}'


def test_the_filtered_strip_is_built_only_in_the_collection_toolbar():
    owners = [path.name for path in sorted(SRC.glob('*.ts')) for _ in re.finditer(r"class:'dt-filtered'", path.read_text(encoding='utf-8'))]
    assert owners == ['table_view.ts'], f'A second filtered strip: use collectionToolbar(…).shown(…) instead: {owners}'
    toolbar = (SRC / 'table_view.ts').read_text(encoding='utf-8')
    assert toolbar.count("button('Show all',") == 1, 'Show all for a filtered list is the toolbar strip\'s alone'


def test_desktop_notice_has_one_builder():
    # L16: the desktop-only notice is owned by dom.ts and reused by phone-scope callers.
    copies = [f'{p.name}:{n}' for p in SRC.glob('*.ts') if p.name != 'dom.ts'
              for n, line in enumerate(p.read_text(encoding='utf-8').splitlines(), 1)
              if not line.lstrip().startswith(('//', '*', '/*')) and re.search(r"['\"`]Best on a desktop|class:\s*['\"]desktop-only['\"]", line)]
    assert not copies, f'draw the notice with dom.ts desktopNotice(page): {copies}'
    callers = {p.name for p in SRC.glob('*.ts') if re.search(r'\bdesktopNotice\(', p.read_text(encoding='utf-8')) and p.name != 'dom.ts'}
    assert {'app.ts', 'sheet.ts'} <= callers

FILES = {p.name: p.read_text(encoding='utf-8') for p in sorted(SRC.glob('*.ts')) if p.name != 'generated.ts'}


def ratchet(pattern, owners, allowed):
    """Matches of `pattern` per file outside `owners`, against the allowed counts: none new, and a fixed one shrinks it."""
    found = {name: len(re.findall(pattern, text)) for name, text in FILES.items() if name not in owners}
    found = {name: n for name, n in found.items() if n}
    grown = {name: n for name, n in found.items() if n > allowed.get(name, 0)}
    stale = {name: (n, found.get(name, 0)) for name, n in allowed.items() if found.get(name, 0) < n}
    assert not grown, f'built outside {owners}: {grown}'
    assert not stale, f'fixed: lower the allowance {stale}'


def test_badges_are_built_by_badge_only():
    # C14: badge(value, tone, cls, {icon, tip, as:'button', …attributes}) in dom.ts builds every badge.
    # LABEL-PROPS owns schedule_export.ts (its "Changed" preset badge); migrate it, then drop the entry.
    ratchet(r"""['"`]badge(?=['"` ])""", {'dom.ts'}, {'schedule_export.ts': 1})


# Owner rule (DESIGN.md › Disclosure, C31): no disclosure arrow is ever shown. A menu button ends in the shared caret
# icon (icon('chevronDown')), never a glyph; a toggle is worded ("Show evidence" / "Show less") with aria-expanded.
GLYPHS = re.compile('[▴▶▸►▼▾◀◂]')


def test_no_disclosure_glyphs_in_user_text():
    hits = [(name, line, value) for name, values in web_literals().items() for line, value in values if GLYPHS.search(value)]
    assert not hits, hits
    css = re.sub(r'/\*.*?\*/', '', (SRC.parents[0] / 'public/assets/app.css').read_text(encoding='utf-8'), flags=re.S)
    assert not GLYPHS.search(css) and not re.search(r'content:\s*["\']\\(25b[468ace]|25c[02])', css, re.I)


def test_icon_only_disclosure_toggles_only_shrink():
    # Expand/collapse toggles drawn as a chevron icon (not menus: those carry aria-haspopup or popup()). Each is a lane's or awaits
    # New group headers use words.
    toggle = r"[^\n]*aria-expanded[^\n]*icon\([^)\n]*'chevron(?:Down|Right)'[^\n]*"
    found = {name: sum(1 for m in re.finditer(toggle, text) if 'aria-haspopup' not in m.group(0) and 'popup(' not in m.group(0)) for name, text in FILES.items()}
    allowed = {  # Group headers: groupToggle (dom.ts) everywhere else; these are the existing exceptions.
               'planner_people.ts': 1, 'rules_tree.ts': 2}
    assert {k: v for k, v in found.items() if v > allowed.get(k, 0)} == {}
    assert {k: v for k, v in allowed.items() if found.get(k, 0) < v} == {}, 'fixed: lower the allowance'


def test_literals_skip_comments_and_regexes():
    src = "// 'no'\nconst a='yes',b=`t ${f('x')} u`;/* \"no\" */const r=/['\"]/g;x=y/2;z='ok'"
    assert [v for _, v in literals(src)] == ['yes', 'x', 't   u', 'ok']

def test_module_level_document_listeners_survive_a_node_import():
    """Node unit tests import client modules under a stub `document` with no addEventListener; a module-level listener
    must check for it, or the whole import throws (issue_marker.ts, 7 Nov 2026). app.ts is the entry, never imported."""
    guard = "typeof document!=='undefined'&&typeof document.addEventListener==='function'"
    bad = [f'{path.name}:{line_of(text, m.start())}' for path, text in sources() if path.name != 'app.ts'
           for m in re.finditer(r'(?m)^[^\s/*].*?\bdocument\.addEventListener\(', text) if guard not in m.group(0)]
    assert not bad, f'Guard module-level document listeners with `if({guard})`:\n' + '\n'.join(bad)


def test_fill_option_asks_are_reset_and_stopped_with_their_surfaces():
    planner = (SRC / 'planner_week.ts').read_text(encoding='utf-8')
    app = (SRC / 'app.ts').read_text(encoding='utf-8')
    # candidates.ts askRegistry owns the held asks: reset() stops and releases every consumer, stop(surface) one.
    assert 'onSessionReset(()=>{ruleCache.clear();asks.reset();});' in planner
    assert "stopAsking('panel')" in planner
    assert "stopAsking('fix')" in app
    # The Fix list's in-place refresh rebuilds the same surface: it keeps its held ask (same key joins), never stops it.
    assert "onClose:leaving=>{if(fixRefreshing)return;stopAsking('fix');" in app

# ---- Person pills (D78, L7): one renderer for a person's name on a chip

def test_person_names_on_chips_are_drawn_by_the_one_person_pill():
    # person_pill.ts draws a pill's name (.pchip-name) and its narrow forms (.pl-narrow, .pl-tight, picked by the cell's
    # width), so a chip drawn by hand keeps its full label in a narrow cell (UX live verify row 4: the Week's published
    # grey chips). The other hand-made names are not people's (a duty code, a sample) or are a lane's: AUTOSCHED-UX owns
    # autoschedule_live.ts (its live preview retypes the name in place), LABEL-PROPS view_settings.ts (the label preview).
    ratchet(r"class:\s*['\"`]pchip-name['\"`]", {'person_pill.ts'},
            {'autoschedule_live.ts': 1, 'forms.ts': 1, 'schedule.ts': 1, 'view_settings.ts': 1})
    ratchet(r"['\"`][^'\"`]*\bpl-(?:narrow|tight)\b", {'person_pill.ts'}, {})


def test_the_weeks_published_chips_are_person_pills():
    # A published session around the draft (s.context) shows its people as the person pill with the .sd-greychip
    # modifier, not as a second grey renderer; only its key entry (lg-mark) is a drawn sample.
    text = FILES['planner_week.ts']
    for name, at, _, outer, _ in calls(text):
        if name != 'el':
            continue
        args = next(a for i, a in call_args(text[at - 2:], 'el'))
        if 'sd-greychip' in args and 'lg-mark' not in args:
            assert 'personPill' in outer, f'planner_week.ts:{line_of(text, at)}: a published chip outside personPill'


def test_browser_tests_name_a_draft_chip_through_the_one_helper():
    # Published chips around a draft carry data-person too, so `.pchip[data-person…]` alone matches both layers. A
    # browser test says which it means: draft_chip()/DRAFT_CHIP (tests/test_browser.py) or `.sd-greychip` explicitly.
    tests = Path(__file__).resolve().parent
    bare = re.compile(r"\.pchip\[data-person(?![^'\"\n]*sd-greychip)")
    helper = ("DRAFT_CHIP = '", "return f'.pchip")  # the helper's own two lines
    found = [f'{p.name}:{n}' for p in sorted(tests.glob('test_browser*.py'))
             for n, line in enumerate(p.read_text(encoding='utf-8').splitlines(), 1)
             if bare.search(line) and not line.lstrip().startswith(helper)]
    assert not found, 'Use draft_chip()/DRAFT_CHIP, or say .sd-greychip: ' + ', '.join(found)


_BIDI_MARKS = re.compile(r'[\u2066-\u2069]|\\u206[6-9]')
_UNISOLATED_DEFINITION = "export const unisolated=(text:string)=>text.replace(/[\\u2066-\\u2069]/g,'');"
_UNISOLATED_LINE = re.compile(rf'(?m)^{re.escape(_UNISOLATED_DEFINITION)}\r?$')


def _bidi_marks_outside_render_boundary(path, text):
    allowed = {m.start() for m in _UNISOLATED_LINE.finditer(text)} if path.name == 'feedback_context.ts' else set()
    if len(allowed) != 1:
        allowed.clear()
    for hit in _BIDI_MARKS.finditer(text):
        if text.rfind('\n', 0, hit.start()) + 1 not in allowed:
            yield hit


def test_bidi_marks_and_isolates_stay_at_the_render_boundary():
    """INN-043: FSI/PDI are implementation details of dom.ts, never data or a request/export value."""
    outside = [(path.name, line_of(text, hit.start())) for path, text in sources() if path.name != 'dom.ts'
               for hit in _bidi_marks_outside_render_boundary(path, text)]
    assert not outside, f'bidi isolate marks escaped dom.ts: {outside}'

    calls_outside = []
    for path, text in sources():
        if path.name == 'dom.ts':
            continue
        for name, at, args, outer, obj in calls(text):
            if name != 'isolate':
                continue
            if not ({'notify', 'el'} & set(outer)):
                calls_outside.append(f'{path.name}:{line_of(text, at)} outside notify()/el()')
            if re.search(r'\b(?:text|sort|facet|export|request|payload|body)\s*:', (obj or '') + (args or '')):
                calls_outside.append(f'{path.name}:{line_of(text, at)} in a raw-value object')
    assert not calls_outside, '\n'.join(calls_outside)


def test_bidi_allowlist_does_not_allow_a_second_feedback_mark():
    path = SRC / 'feedback_context.ts'
    text = path.read_text(encoding='utf-8') + "\nconst second='\\u2066';\n"
    found = list(_bidi_marks_outside_render_boundary(path, text))
    assert len(found) == 1
    assert text[found[0].start():found[0].end()] == r'\u2066'


def test_bidi_renderers_cover_the_remaining_name_chips_and_direction_stays_in_ua_bdi():
    expected = {
        'dom.ts': r"tagChip=\(text:string\)=>.*bidi\(text\)",
        'app.ts': r"bidi\(name\(kind,id\)\)",
        'rule_catalogue.ts': r"bidi\(text\)",
        'staff_import.ts': r"bidi\(nameOf\(ctx,kind,id\)\)",
        'tags.ts': r"bidi\(String\(tag\.name\)\)",
        'person_label.ts': r"bidi\(shown\)",
        'person_pill.ts': r"bidi\(o\.label",
    }
    for name, pattern in expected.items():
        assert re.search(pattern, (SRC / name).read_text(encoding='utf-8'), re.S), f'{name} lost its bidi renderer'
    css = (SRC.parents[0] / 'public/assets/app.css').read_text(encoding='utf-8')
    ts = '\n'.join(text for _, text in sources())
    assert not re.search(r'unicode-bidi', css + ts, re.I)
    literal_values = [value for values in web_literals().values() for _, value in values]
    assert not any(re.search(r'\bdir\s*=', value) or re.search(r'\bdir\s*:', value) for value in literal_values)

def test_one_resize_observer():
    """QA-116: every size watch goes through loading.ts observeSize, which runs callbacks in the next frame (never inside
    the observer's delivery, where resizing an observed box is the "ResizeObserver loop" console error)."""
    bad = [f'{path.name}:{line_of(text, m.start())}' for path in sorted(SRC.glob('*.ts')) if path.name != 'loading.ts'
           for text in [path.read_text(encoding='utf-8')] for m in re.finditer(r'\bnew\s+ResizeObserver\b', text)]
    assert not bad, 'Watch sizes with observeSize (loading.ts), not a ResizeObserver of its own:\n' + '\n'.join(bad)
    assert len(re.findall(r'\bnew\s+ResizeObserver\b', (SRC / 'loading.ts').read_text(encoding='utf-8'))) == 1


# Pages whose answers outlive a rebuild keep them only in api.ts keptRead (QA-115/118); `available` is the session's
# one capability check (does the server have the timeline), not an answer cache.
KEPT_READERS = ('fairness.ts', 'rules_explained.ts', 'person_timeline.ts', 'rule_simplify.ts', 'branding_settings.ts', 'fix_list.ts')
OWN_CACHE_ALLOWED = {('person_timeline.ts', 'available')}


def test_kept_answers_live_only_in_kept_read():
    bad = []
    for name in KEPT_READERS:
        text = (SRC / name).read_text(encoding='utf-8')
        assert re.search(r'\bkeptRead\b', text), f'{name} reads through api.ts keptRead'
        for m in re.finditer(r'new Map<string,\s*(?:Promise|Answer|\{\s*p:)', text):
            bad.append(f'{name}:{line_of(text, m.start())} a map of answers')
        for m in re.finditer(r'(?m)^(?:let|const)\s+(\w+)\s*:[^=\n]*Promise<[^\n]*?\|\s*null', text):
            if (name, m.group(1)) not in OWN_CACHE_ALLOWED:
                bad.append(f'{name}:{line_of(text, m.start())} {m.group(1)}')
    assert not bad, 'Keep answers across rebuilds with api.ts keptRead, not a cache of its own:\n' + '\n'.join(bad)
def catch_bodies(text):
    """(offset, body) of every failure handler: `.catch(...)` callbacks, `catch(e){...}` blocks and `.then(ok, failed)`
    second arguments are all found through their `catch` word or a balanced body after it."""
    for at, args in call_args(text, '.catch'):
        yield at, args
    for m in re.finditer(r'\bcatch\s*\([^)]*\)\s*\{', text):
        depth, i = 1, m.end()
        while i < len(text) and depth:
            depth += {'{': 1, '}': -1}.get(text[i], 0)
            i += 1
        yield m.start(), text[m.end():i - 1]


def test_a_failed_or_refused_read_is_never_an_empty_state():
    # QA-112: a 403 (or any failure) drawn as "No … yet" says the data is not there. A failed read draws readFailed().
    found = [f'{path.name}:{line_of(text, at)}' for path, text in sources() for at, body in catch_bodies(text)
             if re.search(r'(?<![\w.])(empty|emptyLine)\(', body)]
    assert not found, f'a failed read draws dom.ts readFailed(text, e, retry), never empty()/emptyLine(): {found}'


# Ratchet (may only shrink): hand-built "error note + Retry" for a failed read. home.ts's page-wide line gathers the
# cards that failed (one Retry for all).
FAILED_NOTE_ALLOWED = {('home.ts', 'showOffline')}


def test_a_failed_read_note_has_one_builder():
    # readFailed() (dom.ts) says a refusal as "Not available for your role." with no Retry, and anything else as an
    # error with Retry inside the message; a copy would show a 403 as a retryable fault.
    found = []
    for path, text in sources():
        for at, args in call_args(text, 'note'):
            end = at + len('note(') + len(args) + 1
            if re.search(r",\s*'error'", args) and "button('Retry'" in text[end:end + 220]:
                owner = re.findall(r'function (\w+)\(', text[:at])
                if (path.name, owner[-1] if owner else '') not in FAILED_NOTE_ALLOWED:
                    found.append(f'{path.name}:{line_of(text, at)}')
    assert not found, f'draw a failed read with dom.ts readFailed(text, e, retry): {found}'


def test_the_staff_people_placeholder_is_never_read_as_the_catalogue():
    # QA-112: staff hold no lists; their `catalogues={people:[]}` placeholder is always truthy, so "is the people list
    # held" is dom.ts holdsLists(catalogues), never the list's truthiness (which drew "No people yet" for staff).
    truthy = re.compile(r"!!(?:ctx\.)?catalogues\.people(?![.\w?])|if\(!?(?:ctx\.)?catalogues\.people\)"
                        r"|(?<![.\w])(?:ctx\.|sctx\(\)\.)?catalogues\.people(?:\?(?![.\[])|&&)")
    found = [f'{path.name}:{line_of(text, m.start())}' for path, text in sources() for m in truthy.finditer(text)]
    assert not found, f'ask holdsLists(catalogues) whether the people catalogue is held: {found}'
DOM_TS = (SRC / 'dom.ts').read_text(encoding='utf-8')


def _body(text, signature):
    """The `{…}` body of the function that starts at `signature` (balanced braces)."""
    i = text.index('{', text.index(signature))
    depth = 0
    for j in range(i, len(text)):
        depth += {'{': 1, '}': -1}.get(text[j], 0)
        if depth == 0:
            return text[i + 1:j]
    raise AssertionError(signature)


def test_every_popover_close_gives_focus_back_through_the_one_return_path():
    """QA-119 (WCAG 2.4.3): every popover and menu closes through popover()'s close(). It gives the focus back when it was
    inside the panel or is now lost (on the page itself or on a hidden field), through returnFocus, which only focuses
    a shown opener (or its redrawn twin) and otherwise the nearest shown [tabindex] region or #main: never the body."""
    popover = DOM_TS[DOM_TS.index('export function popover('):DOM_TS.index('export function closePopover(')]
    close = _body(popover, 'function close()')
    assert re.search(r'if \(had \|\| focusLost\(\)\) returnFocus\(openerOf\(to\)\);', close), close
    lost = re.search(r'const focusLost=\(\)=>\{([^\n]+)\};', DOM_TS).group(1)
    assert 'document.body' in lost and '!restable(a)' in lost
    restable = re.search(r'const restable=\(n:Element\)=>([^;]+);', DOM_TS).group(1)
    assert 'isConnected' in restable and 'getClientRects().length>0' in restable and '[inert]' in restable
    back = _body(DOM_TS, 'function returnFocus(')
    assert '.find(n=>n&&restable(n))' in back and 'focusRegion(previous)' in back
    region = _body(DOM_TS, 'function focusRegion(')
    assert 'document.body' not in region and "querySelector<HTMLElement>('#main')" in region
    # No second close path for a popover: its unsaved-changes guard closes through the same close().
    assert 'guard(panel, close, true)' in popover


def test_status_indicators_are_not_tab_stops():
    """QA-121 (WCAG 2.4.3, 4.1.2): a live status (role=status) or the environment badge does nothing, so it is never a
    Tab stop; screen readers read its name with the footer, and role=status announces its changes."""
    offenders = []
    for path, text in [(p, p.read_text(encoding='utf-8')) for p in sorted(SRC.glob('*.ts')) if p.name != 'generated.ts']:
        for m in re.finditer(r"el\('span',\{[^}]*\}", text):
            attrs = m.group(0)
            if re.search(r"role:'status'|env-badge", attrs) and re.search(r"tabindex:\s*'?0", attrs):
                offenders.append(f'{path.name}:{line_of(text, m.start())}')
    assert not offenders, offenders


def test_one_collator_orders_every_list_and_choices_by_name():
    """A list of people reads in name order wherever it is offered, the order a collection's sort by Name gives: one
    collator (dom.ts `collator`) and one comparison of choices by name (`byLabel`). A second collator, or choices
    sorted by their label another way, is a second order."""
    every = [(p, p.read_text(encoding='utf-8')) for p in sorted(SRC.glob('*.ts')) if p.name != 'generated.ts']
    assert [p.name for p, text in every for _ in range(text.count('Intl.Collator('))] == ['dom.ts']
    other = [f'{p.name}:{line_of(text, m.start())}' for p, text in every for m in re.finditer(r'label[.]localeCompare', text)]
    assert not other, other
    forms = (SRC / 'forms.ts').read_text(encoding='utf-8')
    # peopleChoices is the one place the order of people is set: by name, each with its rank and that rank's place in
    # the department's rank order (the categories catalogue as served). The one picker draws a single choice flat and a
    # multiple choice in rank sections (picker.ts), so no caller sorts the list again.
    people = _body(forms, 'export function peopleChoices(')
    assert '}).sort(byLabel);' in people and "ranks=all(ctx,'categories')" in people and 'groupOrder:at' in people
    again = [f'{p.name}:{line_of(text, m.start())}' for p, text in every for m in re.finditer(r'peopleChoices\([^)]*\)\)?[.]sort', text)]
    assert not again, again
