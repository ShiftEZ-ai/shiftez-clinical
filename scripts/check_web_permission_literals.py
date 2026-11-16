#!/usr/bin/env python3
"""Fail when web permission gates copy raw role names instead of using contract capabilities."""
from __future__ import annotations

from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps/web/src"
sys.path.insert(0, str(ROOT))


def findings(root: Path = WEB) -> list[str]:
    # Role values in event/audience payloads are data, but a role literal next to an access-gate
    # operation is a second permission source. The shared helper and generated contract data are
    # the only places allowed to define those values directly.
    from packages.contracts.schemas import ROLE_NAMES

    role = "|".join(re.escape(value) for value in ROLE_NAMES)
    literal = re.compile(rf"(['\"])(?:{role})\1")
    gate = re.compile(r"(?:permits\s*\(|(?:\.roles|\broles)\.(?:includes|some|every)\s*\(|\b(?:hasAccess|grants)\s*\(|\broles\s*:\s*\[)")
    allowed = {"dom.ts", "generated.ts"}
    out: list[str] = []
    for path in sorted(root.rglob("*.ts")):
        if path.name in allowed:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if gate.search(line) and literal.search(line):
                out.append(f"{path.relative_to(ROOT)}:{number}: raw role literal in permission gate")
    return out


def main() -> int:
    errors = findings()
    if errors:
        print("Web permission source guard failed:", file=sys.stderr)
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Web permission source guard passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
