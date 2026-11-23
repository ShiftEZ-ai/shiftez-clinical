import json
from pathlib import Path
import re
import subprocess
import sys

from setuptools.config.pyprojecttoml import read_configuration

from apps.api.version import __version__


ROOT = Path(__file__).resolve().parents[1]


def test_release_version_agrees_across_api_web_and_package():
    assert re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}", __version__)
    generated = (ROOT / "apps/web/src/generated.ts").read_text(encoding="utf-8")
    assert f"export const APP_VERSION = {json.dumps(__version__)};" in generated.splitlines()
    assert read_configuration(ROOT / "pyproject.toml")["project"]["version"] == __version__


def test_release_tag_reads_the_target_commit(tmp_path):
    def git(*args):
        return subprocess.check_output(
            ["git", "-c", "user.name=test", "-c", "user.email=test", "-c", "commit.gpgSign=false", *args],
            cwd=tmp_path, text=True,
        ).strip()

    git("init", "--quiet", "--template=")
    version_file = tmp_path / "apps/api/version.py"
    version_file.parent.mkdir(parents=True)
    version_file.write_text('__version__ = "0.0.0"\n', encoding="utf-8")
    git("add", "apps/api/version.py")
    git("commit", "--quiet", "-m", "first version")
    previous = git("rev-parse", "HEAD")
    version_file.write_text(f'__version__ = "{__version__}"\n', encoding="utf-8")
    git("commit", "--quiet", "-am", "next version")
    current = git("rev-parse", "HEAD")

    def check(tag, commit):
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts/check_release_version.py"), tag, commit],
            cwd=tmp_path, capture_output=True, text=True,
        )

    assert check(f"v{__version__}", current).returncode == 0
    assert check(f"v{__version__}-rc.3", current).returncode == 0
    assert check("v0.0.0", previous).returncode == 0
    mismatch = check(f"v{__version__}-rc.3", previous)
    assert mismatch.returncode != 0 and "reports '0.0.0'" in mismatch.stderr
    invalid_tag = check(f"v{__version__}-rc", current)
    assert invalid_tag.returncode != 0 and "expected a tag" in invalid_tag.stderr
    missing_commit = check(f"v{__version__}", "missing")
    assert missing_commit.returncode != 0 and "target commit" in missing_commit.stderr
