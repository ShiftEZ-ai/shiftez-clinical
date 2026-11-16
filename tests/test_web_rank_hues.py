"""Rank tints are their own colours (DESIGN.md › Ranks have one tint each, C21; UX-RANK-HUES).

An automatic rank tint (`rankColour` in dom.ts → `.duty-rankN` → `--rankN-*` in app.css) must never look like a colour
that already means something: a clinic or duty type, a group (purple), a person (sky), an area, a status (ok, warn, err,
info) or the accent. Every `--X-bg` hue triple and the status/accent tokens in the token blocks are reserved, so a new
duty or area hue is covered without editing this file. Distances are CIEDE2000 (ΔE00) per role: tint to tint, dot to
dot, text to text, plus a tint never close to a reserved dot or text colour. For scale, the reserved tints sit only
2.3–3.4 apart from each other (dark teal/green 2.7); the rank tints keep at least 3 from all of them."""
import itertools
import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / 'apps/web/public/assets/app.css').read_text(encoding='utf-8')
DOM = (ROOT / 'apps/web/src/dom.ts').read_text(encoding='utf-8')
# Measured worst cases (8 Nov 2026): tint 3.1, dot 8.2, text 8.4, tint vs reserved dot/text 23; rank vs rank 4.7.
NEAR = {'tint': 3.0, 'dot': 7.0, 'ink': 7.0, 'cross': 7.0}
APART = {'tint': 4.0, 'dot': 4.0}


def tokens(start, end):
    text = CSS[CSS.index(start):CSS.index(end)]
    return dict(re.findall(r'--([\w-]+):(#[0-9A-Fa-f]{6})\b', text))


THEMES = {'light': tokens(':root,.export-sheet', ':root[data-theme=dark]'),
          'dark': tokens(':root[data-theme=dark]', '@media (prefers-color-scheme:dark)')}
RANK_COUNT = int(re.search(r'export const RANK_PALETTE=Array\.from\(\{length:(\d+)\}', DOM).group(1))


def ranks(t):
    return [(t[f'rank{i}-bg'], t[f'rank{i}'], t[f'rank{i}-ink']) for i in range(1, RANK_COUNT + 1)]


def reserved(t):
    out = {}
    for name in (m[:-3] for m in t if m.endswith('-bg') and not re.fullmatch(r'rank\d+-bg', m)):
        dot = t.get(name) or t.get(name + '-c')  # teal's dot is --teal-c
        if dot and name + '-ink' in t:
            out[name] = (t[name + '-bg'], dot, t[name + '-ink'])
    for name, (soft, base, text) in {s: (s + '-soft', s, s + '-text') for s in ('ok', 'warn', 'err', 'info', 'accent')}.items():
        out[name] = (t[soft], t[base], t[text])
    return out


def rgb(h):
    return [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]


def lin(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def contrast(a, b):
    la, lb = (0.2126 * r + 0.7152 * g + 0.0722 * b_ for r, g, b_ in ([lin(c) for c in rgb(x)] for x in (a, b)))
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def lab(h):
    r, g, b = (lin(c) for c in rgb(h))
    xyz = ((0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047, 0.2126 * r + 0.7152 * g + 0.0722 * b,
           (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883)
    fx, fy, fz = (v ** (1 / 3) if v > 216 / 24389 else (24389 / 27 * v + 16) / 116 for v in xyz)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def de00(x, y):
    return de00_lab(lab(x), lab(y))


def de00_lab(p, q):
    """CIEDE2000 (Sharma, Wu & Dalal 2005)."""
    (l1, a1, b1), (l2, a2, b2) = p, q
    cb = (math.hypot(a1, b1) + math.hypot(a2, b2)) / 2
    g = 0.5 * (1 - math.sqrt(cb ** 7 / (cb ** 7 + 25 ** 7)))
    a1, a2 = (1 + g) * a1, (1 + g) * a2
    c1, c2 = math.hypot(a1, b1), math.hypot(a2, b2)
    h1, h2 = math.degrees(math.atan2(b1, a1)) % 360, math.degrees(math.atan2(b2, a2)) % 360
    dh = 0 if c1 * c2 == 0 else (h2 - h1 if abs(h2 - h1) <= 180 else h2 - h1 - 360 if h2 > h1 else h2 - h1 + 360)
    dl, dc, dhh = l2 - l1, c2 - c1, 2 * math.sqrt(c1 * c2) * math.sin(math.radians(dh / 2))
    lb, cbp = (l1 + l2) / 2, (c1 + c2) / 2
    hb = h1 + h2 if c1 * c2 == 0 else (h1 + h2) / 2 if abs(h1 - h2) <= 180 else (h1 + h2 + 360) / 2 if h1 + h2 < 360 else (h1 + h2 - 360) / 2
    t = (1 - 0.17 * math.cos(math.radians(hb - 30)) + 0.24 * math.cos(math.radians(2 * hb))
         + 0.32 * math.cos(math.radians(3 * hb + 6)) - 0.20 * math.cos(math.radians(4 * hb - 63)))
    sl = 1 + 0.015 * (lb - 50) ** 2 / math.sqrt(20 + (lb - 50) ** 2)
    sc, sh = 1 + 0.045 * cbp, 1 + 0.015 * cbp * t
    rt = -2 * math.sqrt(cbp ** 7 / (cbp ** 7 + 25 ** 7)) * math.sin(math.radians(60 * math.exp(-((hb - 275) / 25) ** 2)))
    return math.sqrt((dl / sl) ** 2 + (dc / sc) ** 2 + (dhh / sh) ** 2 + rt * (dc / sc) * (dhh / sh))


def test_de00_matches_the_published_reference_data():
    # Sharma, Wu & Dalal (2005), table 1, pairs 1, 7 and 17.
    assert round(de00_lab((50, 2.6772, -79.7751), (50, 0, -82.7485)), 4) == 2.0425
    assert round(de00_lab((50, 0, 0), (50, -1, 2)), 4) == 2.3669
    assert round(de00_lab((50, 2.5, 0), (73, 25, -18)), 4) == 27.1492


def test_the_rank_palette_is_twelve_tints_with_classes_in_both_themes():
    assert RANK_COUNT >= 12  # a department's real ranks (R1–R4, Fellow, Specialist, Consultant, Intern…) with room
    for i in range(1, RANK_COUNT + 1):
        assert f'.duty-rank{i}{{--duty-bg:var(--rank{i}-bg);--duty:var(--rank{i});--duty-ink:var(--rank{i}-ink)}}' in CSS
    for theme, t in THEMES.items():
        assert len(ranks(t)) == RANK_COUNT, theme


def test_no_rank_tint_is_near_a_colour_that_means_something_else():
    close = []
    for theme, t in THEMES.items():
        res = reserved(t)
        assert {'teal', 'violet', 'sky', 'ok', 'warn', 'err', 'info', 'accent'} <= set(res), theme
        for i, (bg, dot, ink) in enumerate(ranks(t), 1):
            for name, (rbg, rdot, rink) in res.items():
                for what, d in (('tint', de00(bg, rbg)), ('dot', de00(dot, rdot)), ('ink', de00(ink, rink)),
                                ('cross', min(de00(bg, rdot), de00(bg, rink)))):
                    if d < NEAR[what]:
                        close.append(f'{theme} rank{i} {what} vs {name}: ΔE00 {d:.1f} < {NEAR[what]}')
    assert not close, '\n'.join(close)


def test_two_ranks_never_share_a_tint():
    close = []
    for theme, t in THEMES.items():
        for (i, a), (j, b) in itertools.combinations(enumerate(ranks(t), 1), 2):
            for what, d in (('tint', de00(a[0], b[0])), ('dot', de00(a[1], b[1]))):
                if d < APART[what]:
                    close.append(f'{theme} rank{i}/rank{j} {what}: ΔE00 {d:.1f} < {APART[what]}')
    assert not close, '\n'.join(close)


def test_rank_chip_text_and_dot_contrast():
    """WCAG 2.2 SC 1.4.3 (chip text 4.5:1) and 1.4.11 (the dot on its tint 3:1), both themes."""
    low = [f'{theme} rank{i}: text {contrast(ink, bg):.2f}, dot {contrast(dot, bg):.2f}'
           for theme, t in THEMES.items() for i, (bg, dot, ink) in enumerate(ranks(t), 1)
           if contrast(ink, bg) < 4.5 or contrast(dot, bg) < 3]
    assert not low, low


def test_rank_tints_are_assigned_from_the_whole_catalogue():
    """A rank's tint is its place among ALL the department's ranks (archived included), so setRankColours only ever
    receives the full held catalogue, never a filtered or visible list (C21: identical on every screen and filter)."""
    calls = [(p.name, m.group(1)) for p in (ROOT / 'apps/web/src').glob('*.ts')
             for m in re.finditer(r'\bsetRankColours\(([^;]*?)\);', p.read_text(encoding='utf-8'))]
    assert calls and all(arg in ("catalogues.categories||[]", "rows('categories')") for _, arg in calls), calls
