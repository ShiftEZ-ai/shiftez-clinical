#!/usr/bin/env python3
"""Fail when a web save sends a revision read at click time from the live account.

A form draws its values from one version of a record and must save on that version, so the server's own stale check
refuses a save over someone else's newer change. The signed-in account (`me`) is replaced by every background refresh,
so a save that reads `me….department` / `me….organisation` when it is pressed can send old values on a new revision
and silently revert another person's change (Display defaults, fixed in ; Where is everyone, ).
Forms capture their base when drawn (settings_base.ts `drawnBase`). The few saves listed in ALLOWED send no drawn
values, only the one field the person has just chosen (the server merges department.display by field) or a named
version to restore, so a click-time revision cannot revert anything.
"""
from __future__ import annotations

from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps/web/src"

# A command sent on the same line that reads the live account's department or organisation.
LIVE_BASE = re.compile(r"\bme!?\.(?:department|organisation)\b")
REVISION = re.compile(r"\.revision\b")
SAVE = re.compile(r"\bcommand(?:<[^>]*>)?\(\s*'([\w.]+)'\s*,\s*\{\s*(\w+)?")

# (command kind, first payload field): saves whose payload holds no value drawn earlier.
ALLOWED = {
    ("department.display", "rule_catalogue"): "one switch, the value just chosen; the server merges by field",
    ("department.display", "person_labels_enabled"): "one switch, the value just chosen; the server merges by field",
    ("department.display.restore", "version_id"): "restores a named version; no drawn values",
}


def findings(root: Path = WEB) -> list[str]:
    out: list[str] = []
    for path in sorted(root.rglob("*.ts")):
        if path.name == "generated.ts":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # Only a line that reads the live account AND sends a revision (a name read for a default is not a base).
            if not LIVE_BASE.search(line) or not REVISION.search(line):
                continue
            for kind, field in SAVE.findall(line):
                if (kind, field) not in ALLOWED:
                    where = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path.name
                    out.append(f"{where}:{number}: {kind} saves on a revision read at click from `me`; "
                               "capture the base when the form is drawn (settings_base.ts drawnBase)")
    return out


def main() -> int:
    errors = findings()
    if errors:
        print("Web save-base guard failed:", file=sys.stderr)
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Web save-base guard passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
