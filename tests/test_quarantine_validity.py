"""One visible gate for malformed reviewed quarantines."""
from collections import Counter
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from ci.quarantine_registry import decode_registry, expired_entries

from tests.quarantine import entries

FIXTURES = Path(__file__).with_name("fixtures")
MOVED_REGISTRY = FIXTURES / "quarantine_after_migration.json"


def test_reviewed_quarantine_is_valid_and_current():
    today = datetime.now(timezone.utc).date()  # real clock: quarantine expiry is wall-clock policy
    registry = entries(Path(__file__).with_name("quarantine.json"), today)
    for row in expired_entries(registry, today):
        print(f"Expired quarantine remains active: {row['nodeid']}, issue {row['issue']}")


def test_the_quarantine_migration_is_an_accepted_move(monkeypatch):
    root = Path(__file__).parents[1]
    monkeypatch.syspath_prepend(str(root / ".github/scripts"))
    import working_policy as policy

    # Freeze the exact inputs and outputs so the proof also works in a squash or shallow checkout.
    raw = (FIXTURES / "known_failures_before_quarantine.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == (
        "ef8ebc146e875f2a0728540ce24bd898468db94d3b3a0e832c57da370c11ab5d")
    source = json.loads(raw)
    expected = {row["node"]: row["class"] for row in source["fails"] + source["unstable"]}
    new_q = MOVED_REGISTRY.read_bytes()
    new_kf = (FIXTURES / "known_failures_after_quarantine.json").read_bytes()
    assert hashlib.sha256(new_q).hexdigest() == "06560a8c41549bd8033af5225742c8d74f39bbf6a6838b44caa231e87973d37a"
    assert hashlib.sha256(new_kf).hexdigest() == "21a5511dde774fa6dd9d8ca4f40cb33c7c370ef93a47765c30d72b6070b7aae7"
    policy.quarantine_change(b"[]\n", new_q, raw, new_kf)
    assert policy.removal_only(raw, new_kf)
    assert {row["nodeid"]: row["reason"].partition(":")[0] for row in json.loads(new_q)} == expected
    assert json.loads(new_kf) == {**source, "fails": [], "unstable": []}


def test_the_quarantine_migration_counts_each_class():
    rows = json.loads(MOVED_REGISTRY.read_bytes())
    counts = Counter(row["reason"].partition(":")[0] for row in rows)
    assert counts == {"baseline": 351, "baseline-error": 26, "unstable": 33}
    assert sum(row["blocker"] for row in rows) == 0


def registry_row(name, issue, *, expires="2000-01-02", kind="baseline", blocker=False):
    return {"nodeid": f"tests/test_example.py::test_{name}", "reason": f"{kind}: reviewed",
            "owner": "maintainers", "issue": issue, "blocker": blocker,
            "added": "2000-01-01", "expires": expires}


def test_quarantine_report_groups_expiry_classes_and_blockers(tmp_path):
    registry = tmp_path / "quarantine.json"
    registry.write_text(json.dumps([
        registry_row("a", 19, blocker=True),
        registry_row("b", 2, kind="unstable"),
        registry_row("c", 19),
        registry_row("d", 2, expires="2099-01-01", kind="unstable", blocker=True),
    ]), encoding="utf-8")
    script = Path(__file__).parents[1] / "ci/quarantine_report.py"
    result = subprocess.check_output([sys.executable, str(script), str(registry)], text=True)
    assert result.splitlines() == [
        "Expired quarantine entries by issue: #2: 1 node, #19: 2 nodes",
        "Quarantine entries by class: baseline: 2, unstable: 2",
        "Release blockers in quarantine: 2",
    ]


def test_quarantine_reader_preserves_reviewed_flake_evidence():
    row = {**registry_row("flake", 2, kind="unstable"), "recovered_runs": ["123456"]}
    today = date(2000, 1, 2)
    assert decode_registry(json.dumps([row]), today) == {row["nodeid"]: row}
    for run in ("latest", "\u0661\u0662\u0663"):
        with pytest.raises(ValueError, match="numeric run IDs"):
            decode_registry(json.dumps([{**row, "recovered_runs": [run]}]), today)
