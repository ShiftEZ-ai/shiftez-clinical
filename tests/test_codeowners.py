from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
TEAM = "@ShiftEZ-ai/maintainers"
OWNED = ["/.github/", "/ci/", "/infra/", "/tests/conftest.py", "/tests/quarantine.json", "/tests/quarantine.py",
         "/requirements/*.lock", "/requirements/ci-runtimes.json", "/package-lock.json", "/pyproject.toml",
         "/package.json"]


def rules():
    lines = (ROOT / ".github/CODEOWNERS").read_text(encoding="utf-8").splitlines()
    return [line.split() for line in lines if line.strip() and not line.startswith("#")]


def test_pipeline_and_dependency_paths_need_the_maintainers_team():
    owned = {pattern: owners for pattern, *owners in rules()}
    assert sorted(owned) == sorted(OWNED)
    assert all(owners == [TEAM] for owners in owned.values())


def test_code_owners_are_teams_never_personal_logins():
    assert all(re.fullmatch(r"@ShiftEZ-ai/[a-z][a-z0-9-]*", owner) for _, *owners in rules() for owner in owners)
