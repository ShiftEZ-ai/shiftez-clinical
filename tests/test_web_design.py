"""Design rules checked in the web source (no browser)."""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'apps/web/src'


def test_no_native_date_inputs():
    """FE-01 / SR-23: no browser date picker anywhere (DESIGN.md "Dates"): a raw date input is always handed to the
    shared date field (date_picker.ts dateInput), which turns it into a text field with the shared calendar."""
    loose = []
    for path in SRC.glob('*.ts'):
        text = path.read_text(encoding='utf-8')
        for m in re.finditer(r"(\w+)=el\('input',\{type:'(date|datetime-local|time)'", text):
            if not re.search(rf"dateInput\({m.group(1)}\b", text):
                loose.append(f'{path.name}: {m.group(1)} ({m.group(2)})')
    assert not loose, loose


def test_rota_status_takes_its_tone_from_the_one_palette():
    """L11 (DESIGN.md status palette): a rota's one status goes through statusBadge, so its class is a Tone, never a state
    name; and the review strip's facts are a factLine (CSS separators), never a literal " · " that can end a line."""
    app = (SRC / 'app.ts').read_text(encoding='utf-8')
    pill = re.search(r'function rotaStatusPill\(.*?\n}', app, re.S).group(0)
    assert 'statusBadge(' in pill and "'rota-status '+" not in pill
    css = (SRC.parent / 'public/assets/app.css').read_text(encoding='utf-8')
    assert not re.search(r'\.rota-status\.(submitted|approved|published|outdated)\b', css)
    governance = (SRC / 'governance.ts').read_text(encoding='utf-8')
    strip = re.search(r'export function reviewPanel\(.*?\n}', governance, re.S).group(0)
    assert 'factLine(' in strip and 'review-step' not in strip


def test_month_cell_pills_keep_a_24px_pitch():
    """WCAG 2.2 SC 2.5.8 spacing exception: Month draws its pills at --pill-h-compact (under 24 px), so they stack at the
    cell's one pill gap and every pitch is at least 24 px; no rule gives a day cell a narrower gap of its own."""
    css = (SRC.parent / 'public/assets/app.css').read_text(encoding='utf-8')
    tokens = {k: int(v) for k, v in re.findall(r'--(pill-h-compact|pill-gap):(\d+)px', css)}
    assert tokens['pill-h-compact'] + tokens['pill-gap'] >= 24, tokens
    gaps = []
    for selector, body in re.findall(r'([^{}]*)\{([^{}]*)\}', css):
        parts = [p.strip() for p in selector.split('*/')[-1].split(',')]
        gap = re.search(r'(?:^|;)gap:([^;]+)', body)
        if gap and any(re.search(r'\.wk-cell(?:[.:][\w-]+)*$', p) for p in parts):
            gaps.append((selector.split('*/')[-1].strip(), gap.group(1).strip()))
    assert gaps == [('.wk-cell', 'var(--pill-gap)')], gaps
