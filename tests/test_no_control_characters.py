"""Source files hold no stray control characters: an escaping slip in a shell edit wrote a backspace (0x08) for `\b`
into regex patterns twice on 10 Nov 2026, and a pattern holding one silently never matches."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STRAY = {chr(c) for c in range(32)} - {'\t', '\n', '\r', '\f'}


def test_no_source_file_holds_a_stray_control_character():
    found = [f'{p.relative_to(ROOT)}:{n}' for d in ('apps', 'packages', 'scripts', 'tests') for p in sorted((ROOT / d).rglob('*'))
             if p.suffix in {'.py', '.ts', '.css', '.html'} and 'node_modules' not in p.parts
             for n, line in enumerate(p.read_text(encoding='utf-8', errors='replace').splitlines(), 1) if STRAY & set(line)]
    assert found == [], found
