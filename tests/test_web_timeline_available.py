"""People › Timeline stays the saved layout when the availability probe fails for any reason but a 404 (a busy server
or a reload racing the session): only a server without /api/timeline falls back to the table (person_timeline.ts)."""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "apps" / "web" / "src" / "person_timeline.ts"


def test_only_a_404_turns_the_timeline_off():
    text = SRC.read_text(encoding="utf-8")
    body = text[text.index("export function timelineAvailable"):]
    body = body[:body.index("\n}\n")]
    rejected = re.search(r"\.then\(data=>\{.*?return true;\},e=>\{(.*?)\}\);", body, re.S)
    assert rejected, body
    handler = rejected.group(1)
    assert "status===404" in handler
    assert re.search(r"return\s*!missing", handler), "a non-404 failure must keep the timeline"
