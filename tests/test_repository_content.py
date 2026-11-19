"""Public source contains no private agent instructions, state or tracked ignore patterns."""
from pathlib import Path
import subprocess
import sys
HELPERS=Path(__file__).resolve().parents[1]/'.github/scripts'
if not HELPERS.is_dir():HELPERS=Path(__file__).resolve().parent
sys.path.insert(0,str(HELPERS))
from working_policy import private_agent_path as private_path, private_ignore_content

def tracked():
    return subprocess.check_output(['git','ls-tree','-r','--name-only','-z','HEAD']).decode().rstrip('\0').split('\0')

def test_no_private_agent_files_tracked():
    assert not [name for name in tracked() if private_path(name)]

def test_private_agent_names_are_not_in_tracked_gitignore():
    # Covers .gitignore, .dockerignore and every other tracked *ignore at every depth.
    for name in tracked():
        if name.casefold().endswith('ignore'):
            assert not private_ignore_content(name,Path(name).read_bytes()),name
