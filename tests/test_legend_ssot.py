"""One key component (owner, 7 Nov 2026: "nothing displayed without legend! … it has SSOT for all of these!"; DESIGN.md ›
Every mark has a legend entry). Source checks beside the browser guard (tests/legend_guard.py): only dom.ts builds a
key, and an entry comes only from the mark it explains (`legend().mark`); the one exception, `laidOut`, is the
virtualised timeline's rows not yet drawn."""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'apps' / 'web' / 'src'
SOURCES = {p.name: p.read_text(encoding='utf-8') for p in SRC.glob('*.ts') if p.name != 'generated.ts'}


def test_only_the_shared_legend_builds_a_key():
    builders = [name for name, text in SOURCES.items() if name != 'dom.ts' and re.search(r"""class:\s*['"`]view-legend""", text)]
    assert builders == [], f'Build the key with dom.ts legend(), not by hand: {builders}'


def test_an_entry_comes_only_from_its_mark():
    assert 'laidOut:add' in SOURCES['dom.ts'] and not re.search(r'return \{root,add', SOURCES['dom.ts'])
    users = sorted(name for name, text in SOURCES.items() if '.laidOut(' in text)
    assert users == ['timeline.ts'], f'laidOut is for the virtualised timeline only: {users}'


def test_no_second_legend_system():
    """The footer holds status only: no footer legend (UIUX-AUDIT L9); every key is dom.ts legend() beside its view."""
    users = sorted(name for name, text in SOURCES.items() if 'footerLegend' in text or 'footer-legend' in text)
    assert users == [], f'Footer legends are retired; use legend() beside the view: {users}'
