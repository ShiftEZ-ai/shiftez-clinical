"""README files must be zero bytes, not whitespace.

The repository keeps a low public profile: the owner's rule.
"""
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_tracked_readmes_are_empty():
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True,
    )
    tracked = result.stdout.decode("utf-8").split("\0")
    assert "README.md" in tracked, "root README.md must be tracked"
    assert (ROOT / "README.md").is_file(), "root README.md must exist"
    nonempty = [
        path for path in tracked
        if (Path(path).name.casefold() == "readme" or Path(path).name.casefold().startswith("readme."))
        and (ROOT / path).stat().st_size != 0
    ]
    assert not nonempty, "tracked README files must be zero bytes: " + ", ".join(nonempty)
