from pathlib import Path
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/scripts"))
from working_policy import codeowner_rules, control_inventory, owned, tier

TEAM = "@ShiftEZ-ai/maintainers"
GUARDS = [".dockerignore", ".gitattributes", "tests/hardening_profiles.py", "tests/test_postgres_markers.py",
          "tests/test_quarantine_validity.py", "tests/test_timing_guard.py", "migrations/safety.py", "tests/test_codeowners.py", "tests/test_data_safety.py", "tests/test_readme_empty.py",
          "tests/test_repository_content.py", "tests/conftest.py", "tests/quarantine.json", "tests/quarantine.py"]
DEPENDENCIES = ["requirements/build.lock", "requirements/ci-runtimes.json", "package-lock.json", "pyproject.toml",
                "package.json"]


def rules():
    return codeowner_rules((ROOT / ".github/CODEOWNERS").read_text(encoding="utf-8"))


def tier_one_paths():
    """Every tracked path the policy guard treats as Tier 1 by path alone.

    tier() classifies a change; with identical before and after content only its path rules apply, which is what
    a CODEOWNERS rule can express. Content rules (an added pytest hook anywhere) are checked on each change.
    """
    head = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    inventory = control_inventory(head)
    tracked = subprocess.check_output(["git", "-C", str(ROOT), "ls-files"], text=True).splitlines()
    return [path for path in tracked if tier(path, b"", b"", inventory)[0] == 1]


# Removal-only by the required CI result verdict and the policy guard, so it needs no owner review.
UNOWNED_BY_DESIGN = {"ci/known-failures.json"}


def test_every_path_the_policy_guard_treats_as_tier_one_has_a_code_owner():
    tier_one = tier_one_paths()
    assert tier_one, "the policy guard found no Tier 1 path"
    owners = rules()
    assert [path for path in tier_one if not owned(path, owners) and path not in UNOWNED_BY_DESIGN] == []


def test_known_failures_alone_in_ci_is_unowned():
    owners = rules()
    assert not owned("ci/known-failures.json", owners)
    assert owned("ci/make_known_failures.py", owners) and owned("ci/quarantine_registry.py", owners)
    assert owned("ci/known-skips.json", owners) and owned("ci/removed-tests.json", owners)
    # The exemption is the file's own rule, last in the file and without an owner.
    assert rules()[-1] == ["/ci/known-failures.json"]


def test_the_guards_and_dependency_files_have_a_code_owner():
    owners = rules()
    assert [path for path in GUARDS + DEPENDENCIES if not owned(path, owners)] == []


def test_policy_guard_tests_have_a_code_owner():
    versions = sorted((ROOT / "tests").glob("test_policy_v3*.py"))
    assert versions
    modules = {"ci", *(path.stem for directory in (".github/scripts", "ci") for path in (ROOT / directory).rglob("*.py"))}
    imported = "|".join(re.escape(module) for module in sorted(modules))
    pipeline_reference = re.compile(
        r"""\.github[/\\](?:scripts|workflows)\b|["']\.github["']\s*/\s*["'](?:scripts|workflows)["']"""
        r"""|/\s*["']ci["']|["']ci/|^\s*(?:from|import)\s+[^\n#]*?\b(?:%s)\b""" % imported,
        re.MULTILINE,
    )
    paths = set(versions) | {path for path in (ROOT / "tests").rglob("test_*.py")
                             if pipeline_reference.search(path.read_text(encoding="utf-8"))}
    owners = rules()
    names = sorted(path.relative_to(ROOT).as_posix() for path in paths)
    assert [path for path in names if not owned(path, owners)] == []


def test_local_actions_directory_is_explicitly_owned():
    assert ["/.github/actions/", TEAM] in rules()
    assert owned(".github/actions/check/action.yml", rules())


def test_ordinary_product_paths_need_no_code_owner():
    owners = rules()
    assert not owned("apps/api/services/planning.py", owners)
    assert not owned("scripts/dev.py", owners)
    assert not owned("tests/test_domain.py", owners)
    assert not owned("packages/domain/conditions.py", owners)
    assert not owned("migrations/0001_initial.py", owners)


def test_codeowner_glob_matches_only_within_one_path_component():
    owners = codeowner_rules(f"/*.py {TEAM}\n/scripts/ci_*.py {TEAM}\n/ci/ {TEAM}\n/ci/unowned.py @owner/other")
    assert owned("task.py", owners)
    assert not owned("nested/task.py", owners)
    assert owned("scripts/ci_build.py", owners)
    assert not owned("scripts/ci_nested/build.py", owners)
    assert owned("ci/nested/check.py", owners)
    assert not owned("ci/unowned.py", owners)


@pytest.mark.parametrize("pattern,owners", [
    ("/task.py", f"{TEAM} # comment"),
    ("/task[.py", TEAM),
    ("/task].py", TEAM),
    ("/!task.py", TEAM),
    (r"/task\.py", TEAM),
    ("/**.py", TEAM),
    ("/tas?.py", TEAM),
    ("task.py", TEAM),
    ("/dir*/task.py", TEAM),
])
def test_owned_refuses_unsupported_codeowner_rules(pattern, owners):
    with pytest.raises(ValueError, match="Unsupported CODEOWNERS rule"):
        owned("task.py", codeowner_rules(f"{pattern} {owners}\n/task.py {TEAM}"))


def test_an_ownerless_rule_unowns_its_paths():
    assert owned("task.py", codeowner_rules(f"/task.py {TEAM}"))
    assert not owned("task.py", codeowner_rules(f"/task.py {TEAM}\n/task.py"))
    assert not owned("ci/check.py", codeowner_rules(f"/ci/ {TEAM}\n/ci/check.py"))
    assert owned("ci/other.py", codeowner_rules(f"/ci/ {TEAM}\n/ci/check.py"))


@pytest.mark.parametrize("owners", ["@owner/other", f"{TEAM} @owner/other", f"{TEAM} {TEAM}"])
def test_owned_requires_the_exact_team_as_the_only_owner(owners):
    assert not owned("task.py", codeowner_rules(f"/task.py {owners}"))


def test_code_owners_are_one_team_never_personal_logins():
    assert all(owners == [TEAM] for pattern, *owners in rules() if pattern.lstrip("/") not in UNOWNED_BY_DESIGN)
    assert [pattern for pattern, *owners in rules() if not owners] == ["/ci/known-failures.json"]
    assert all(re.fullmatch(r"@ShiftEZ-ai/[a-z][a-z0-9-]*", owner) for _, *owners in rules() for owner in owners)
