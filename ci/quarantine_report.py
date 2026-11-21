"""Print the active quarantine inventory for weekly review."""
import argparse
from collections import Counter
from pathlib import Path

from quarantine_registry import QUARANTINE, decode_registry, expired_issue_summary, utc_today


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("registry", nargs="?", type=Path, default=Path(__file__).resolve().parents[1] / QUARANTINE)
    args = parser.parse_args()
    today = utc_today()
    registry = decode_registry(args.registry.read_text(encoding="utf-8"), today)
    counts = Counter(row["reason"].split(":", 1)[0].strip() for row in registry.values())
    print(expired_issue_summary(registry, today))
    print("Quarantine entries by class: " + (", ".join(f"{kind}: {counts[kind]}" for kind in sorted(counts)) or "None"))
    print(f"Release blockers in quarantine: {sum(row['blocker'] for row in registry.values())}")


if __name__ == "__main__":
    main()
