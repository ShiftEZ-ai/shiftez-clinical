"""A web save never sends a revision read at click time from the live account (scripts/check_web_save_base.py)."""
from scripts.check_web_save_base import ALLOWED, findings


def test_no_save_reads_its_revision_from_the_live_account():
    assert findings() == []


def test_the_guard_catches_a_click_time_revision_and_keeps_the_audited_saves(tmp_path):
    (tmp_path / "form.ts").write_text(
        "const d=me!.department as Row;await command('department.display',{names:draft},d.id,d.revision);\n"
        "const d=me!.department as Row,r=await command('department.display',{rule_catalogue:want,reason},d.id,d.revision);\n"
        "const base=drawnBase(me!.department as Row);\n"
        "await command('department.display',{names:draft},base.id,base.revision);\n",
        encoding="utf-8")
    found = findings(tmp_path)
    assert len(found) == 1 and "form.ts:1:" in found[0] and "department.display" in found[0]
    # Each allowed save says why a click-time revision cannot revert anything there.
    assert all(reason for reason in ALLOWED.values())
