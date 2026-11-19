from fnmatch import fnmatchcase
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TEAM = "@ShiftEZ-ai/maintainers"
GUARDS = ["migrations/safety.py", "tests/test_codeowners.py", "tests/test_data_safety.py", "tests/test_readme_empty.py",
          "tests/test_repository_content.py", "tests/conftest.py", "tests/quarantine.json", "tests/quarantine.py"]
DEPENDENCIES = ["requirements/build.lock", "requirements/ci-runtimes.json", "package-lock.json", "pyproject.toml",
                "package.json"]


def rules():
    lines = (ROOT / ".github/CODEOWNERS").read_text(encoding="utf-8").splitlines()
    return [line.split() for line in lines if line.strip() and not line.startswith("#")]


def owned(path):
    """The subset of CODEOWNERS syntax this file uses: anchored paths, directory prefixes and * within a name."""
    for pattern, *_ in rules():
        anchored = pattern.lstrip("/")
        if pattern.endswith("/") and path.startswith(anchored):
            return True
        if fnmatchcase(path, anchored) and "/" not in path[len(anchored.rsplit("/", 1)[0]) + 1:]:
            return True
    return False


def tier_one_paths():
    """Every tracked path the policy guard treats as Tier 1 by path alone.

    tier() classifies a change; with identical before and after content only its path rules apply, which is what
    a CODEOWNERS rule can express. Content rules (an added pytest hook anywhere) are checked on each change.
    """
    sys.path.insert(0, str(ROOT / ".github/scripts"))
    import working_policy as policy
    head = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    inventory = policy.control_inventory(head)
    tracked = subprocess.check_output(["git", "-C", str(ROOT), "ls-files"], text=True).splitlines()
    return [path for path in tracked if policy.tier(path, b"", b"", inventory)[0] == 1]


def test_every_path_the_policy_guard_treats_as_tier_one_has_a_code_owner():
    tier_one = tier_one_paths()
    assert tier_one, "the policy guard found no Tier 1 path"
    assert [path for path in tier_one if not owned(path)] == []


def test_the_guards_and_dependency_files_have_a_code_owner():
    assert [path for path in GUARDS + DEPENDENCIES if not owned(path)] == []


def test_ordinary_product_paths_need_no_code_owner():
    assert not owned("apps/api/services/planning.py") and not owned("scripts/dev.py") and not owned("tests/test_domain.py")


def test_code_owners_are_one_team_never_personal_logins():
    assert all(owners == [TEAM] for _, *owners in rules())
    assert all(re.fullmatch(r"@ShiftEZ-ai/[a-z][a-z0-9-]*", owner) for _, *owners in rules() for owner in owners)
