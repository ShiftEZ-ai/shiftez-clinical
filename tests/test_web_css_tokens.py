"""Colours come from tokens (DESIGN.md: "Tokens first; components use tokens only"; audit X-05, X-26, X-32).

Source scans of the app's linked stylesheets: every hex colour lives in a token block, so a rule written after them cannot keep a light-only
literal that stays the same in dark mode; the dark tokens written twice (the explicit [data-theme=dark] block and the
prefers-color-scheme copy for "system") stay identical; and badges have exactly one tone vocabulary (dom.ts Tone)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / 'apps/web/public'
SHEETS = {href: (PUBLIC / href.lstrip('/')).read_text(encoding='utf-8')
          for href in re.findall(r'<link\b[^>]*href="(/assets/[^"?]+\.css)"', (PUBLIC / 'index.html').read_text(encoding='utf-8'))}
CSS = SHEETS['/assets/app.css']
ALL_CSS = '\n'.join(SHEETS.values())
DOM = (ROOT / 'apps/web/src/dom.ts').read_text(encoding='utf-8')
HEX = re.compile(r'#[0-9a-fA-F]{3,8}\b')


def block(start):
    """The text of the `{…}` block that opens at the first `{` after `start` (balanced braces)."""
    i = CSS.index('{', CSS.index(start))
    depth = 0
    for j in range(i, len(CSS)):
        depth += {'{': 1, '}': -1}.get(CSS[j], 0)
        if depth == 0:
            return CSS[i + 1:j]
    raise AssertionError(f'unbalanced block after {start}')


def declarations(text):
    return sorted(d.strip() for d in text.replace('\n', '').split(';') if d.strip())


def test_the_two_dark_token_blocks_are_identical():
    explicit = block(':root[data-theme=dark]')
    system = block('@media (prefers-color-scheme:dark){:root:not([data-theme=light])')
    system = system[system.index('{') + 1:system.rindex('}')]
    assert declarations(explicit) == declarations(system)


def test_no_hex_colour_outside_the_token_blocks():
    tokens_end = CSS.index('/* ---------- Base ---------- */')
    offenders = []
    for path, text in SHEETS.items():
        start = tokens_end if path == '/assets/app.css' else 0
        for n, line in enumerate(text[start:].splitlines(), text[:start].count('\n') + 1):
            # A mask's opaque stop is not a colour anyone sees (mask-image only reads alpha).
            literal = [h for h in HEX.findall(line) if not ('mask-image' in line and h == '#000')]
            if literal:
                offenders.append(f'{path}:{n}: {", ".join(literal)}')
    assert not offenders, 'use a token from the :root blocks instead:\n' + '\n'.join(offenders)


def test_badges_have_one_tone_vocabulary():
    tones = re.search(r"export type Tone=([^;]+);", DOM).group(1)
    allowed = set(re.findall(r"'(\w+)'", tones))
    assert allowed == {'ok', 'warn', 'err', 'info', 'draft', 'neutral'}
    named = set(re.findall(r'\.badge\.([a-z]+)\b', ALL_CSS))
    # Any other .badge.<word> is a layout or kind class, never a second word for a tone.
    synonyms = {'success', 'warning', 'error', 'published', 'approved', 'covered', 'submitted', 'queued', 'gap',
                'failed', 'running', 'superseded', 'cancelled'}
    assert not named & synonyms, named & synonyms
    assert allowed - {'draft', 'neutral'} <= named | {'neutral'}


def test_all_css_vars_defined():
    """FE-04: a `var(--x)` with no definition computes to `initial` (square corners, no fill); every custom property used
    is declared in a linked stylesheet or set at run time by the client (`style.setProperty('--x', …)`)."""
    ts = ''.join(p.read_text(encoding='utf-8') for p in (ROOT / 'apps/web/src').glob('*.ts'))
    defined = set(re.findall(r'(--[\w-]+)\s*:', ALL_CSS)) | set(re.findall(r"""['"`](--[\w-]+)""", ts))
    used = set(re.findall(r'var\(\s*(--[\w-]+)', ALL_CSS))
    assert not used - defined, sorted(used - defined)


def _luminance(hex_colour):
    h = hex_colour.lstrip('#')
    channels = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    r, g, b = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    x, y = sorted([_luminance(a), _luminance(b)])
    return (y + 0.05) / (x + 0.05)


def test_placeholder_text_is_4_5_to_1_on_every_field_background_in_both_themes():
    """QA-120 (WCAG 2.2 SC 1.4.3): placeholder text is information. The one ::placeholder rule reads a token, and that
    token is at least 4.5:1 on every surface a field is drawn on, in light and in both dark blocks."""
    rules = re.findall(r'(?:^|[}\s])::placeholder\{([^}]*)\}', ALL_CSS)
    assert len(rules) == 1, rules
    token = re.search(r'color:var\((--[\w-]+)\)', rules[0]).group(1)
    assert 'opacity:1' in rules[0]
    blocks = {'light': block(':root,.export-sheet'), 'dark': block(':root[data-theme=dark]'),
              'system dark': block('@media (prefers-color-scheme:dark){:root:not([data-theme=light])')}
    low = {}
    for theme, text in blocks.items():
        values = dict(re.findall(r'(--[\w-]+):(#[0-9A-Fa-f]{6})\b', text))
        for bg in ('--surface', '--subtle', '--sub', '--hover', '--bg'):
            ratio = _contrast(values[token], values[bg])
            if ratio < 4.5:
                low[f'{theme} {token} on {bg}'] = round(ratio, 2)
    assert not low, low


def test_the_chosen_segment_has_a_3_to_1_boundary_against_its_track_in_both_themes():
    """D-076 / X-1 (WCAG 2.2 SC 1.4.11): the selected option of every segmented control is told from its track by a
    boundary of at least 3:1, not by a background two shades apart. One rule draws it for every segmented control."""
    rules = re.findall(r'\.segmented button\[aria-checked=true\][^{]*\{([^}]*)\}', CSS)
    assert len(rules) == 1, rules
    token = re.search(r'inset 0 0 0 1px var\((--[\w-]+)\)', rules[0]).group(1)
    blocks = {'light': block(':root,.export-sheet'), 'dark': block(':root[data-theme=dark]'),
              'system dark': block('@media (prefers-color-scheme:dark){:root:not([data-theme=light])')}
    low = {}
    for theme, text in blocks.items():
        values = dict(re.findall(r'(--[\w-]+):(#[0-9A-Fa-f]{6})\b', text))
        for bg in ('--sub', '--surface'):  # the track, and the chosen option's own fill
            ratio = _contrast(values[token], values[bg])
            if ratio < 3:
                low[f'{theme} {token} on {bg}'] = round(ratio, 2)
    assert not low, low


def test_pressed_weekdays_and_property_grips_meet_3_to_1_in_both_themes():
    """D-059 (SC 1.4.11): a pressed weekday is told by its accent border, and the Properties drag grip (an operable icon)
    is drawn in a 3:1 token, on --surface in light and both dark blocks."""
    pressed = re.search(r'\.vs-days button\[aria-pressed=true\][^{]*\{[^}]*border-color:var\((--[\w-]+)\)', CSS).group(1)
    grip = re.search(r'\.vs-prop \.grip\{[^}]*color:var\((--[\w-]+)\)', CSS).group(1)
    blocks = {'light': block(':root,.export-sheet'), 'dark': block(':root[data-theme=dark]'),
              'system dark': block('@media (prefers-color-scheme:dark){:root:not([data-theme=light])')}
    low = {}
    for theme, text in blocks.items():
        values = dict(re.findall(r'(--[\w-]+):(#[0-9A-Fa-f]{6})\b', text))
        for token in (pressed, grip):
            ratio = _contrast(values[token], values['--surface'])
            if ratio < 3:
                low[f'{theme} {token}'] = round(ratio, 2)
    assert not low, low
