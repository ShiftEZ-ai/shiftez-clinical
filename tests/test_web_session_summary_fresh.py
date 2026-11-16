"""Review maintenance item 10: every caller of the one session summary passes the rota's validation freshness, so the docked
summary (Month, People, a score pill) says "The rota changed since the last check" after an edit, as the inline panel
does, instead of showing the previous check's findings as current."""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "apps" / "web" / "src"


def test_every_draft_session_summary_passes_freshness():
    calls = []
    for path in SRC.glob("*.ts"):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"sessionSummary\((?!ctx:Context)", text):
            depth, i = 1, m.end()
            while depth:
                depth += {"(": 1, ")": -1}.get(text[i], 0); i += 1
            calls.append((path.name, text[m.end():i]))
    assert len(calls) >= 2, calls
    for name, args in calls:
        assert re.search(r"\bfresh\b", args), f"{name}: sessionSummary({args[:120]}…) does not pass fresh"
