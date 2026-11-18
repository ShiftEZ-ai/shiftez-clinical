"""Public README stays empty; runs against the candidate working directory."""
from pathlib import Path
import subprocess


def test_readme_empty():
    names=subprocess.check_output(["git","ls-tree","-r","--name-only","-z","HEAD"]).decode().rstrip("\0").split("\0")
    readmes=[Path(name) for name in names if Path(name).name.lower()=="readme" or Path(name).name.lower().startswith("readme.")]
    assert Path("README.md") in readmes
    for path in readmes:
        assert not path.is_symlink() and path.is_file() and path.read_bytes()==b"", "Every tracked README must be empty: "+str(path)


if __name__ == "__main__":
    test_readme_empty()
