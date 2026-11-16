"""Review maintenance (SHOULD 6): the recurring form's live clash check keeps its drawn list only when the server returns
exactly the same clashes. Comparing clash IDs alone kept buttons bound to old cancel actions after another admin's edit
moved a revision (the cancel then failed with 409); the comparison must cover the whole clash (facts and actions)."""
import re
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[1] / "apps" / "web" / "src" / "double_booking.ts").read_text(encoding="utf-8")


def test_the_live_check_reuses_its_list_only_for_identical_clashes():
    run = SOURCE[SOURCE.index("async function run()"):SOURCE.index("const go=()=>")]
    same = re.search(r"const same=([^;]+);", run)
    assert same, "liveClashCheck no longer decides when to keep its drawn list"
    assert "JSON.stringify(r.conflicts)===JSON.stringify(conflicts)" in same.group(1), same.group(1)
    assert "ids(" not in same.group(1), "clash IDs alone miss a changed cancel action or revision"
    assert re.search(r"if\(!same\)draw\(\)", run)
