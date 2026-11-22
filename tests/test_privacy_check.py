"""Pipeline regression: the public-pattern privacy check finds every shape it names and never prints a value."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/privacy_check.py"
sys.path.insert(0, str(SCRIPT.parent))
import privacy_check  # noqa: E402

# Every planted value is joined at run time, so no secret- or host-shaped literal sits in the tree.
A36 = "aB3dE5gH7jK9mN1pQ2rS4tU6vW8xY0zA1bC3"
PLANTED = {
    "private-key": "-----BEGIN " + "RSA PRIVATE" + " KEY-----",
    "github-token": "token: " + "gh" + "p_" + A36,
    "aws-access-key": "id = " + "AK" + "IA" + "Q3EXAMPL7NOTREAL",
    "slack-token": "xo" + "xb-" + "1234567890-abcdefABCDEF",
    "google-api-key": "key " + "AI" + "za" + "Sy" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q",
    "secret-assignment": "api_" + "key = \"" + "Zq8" + "Xv2Lp9Rt4Wm7Ny3Kb6Hd1\"",
    "ipv4": "connect to " + "10." + "20.30.40",
    "wildcard-dns-host": "app.10-20-30-40." + "ss" + "lip.io",
    "private-tld": "https://db" + ".corp" + "/path",
    "env-host-port": "st" + "g-api" + ":8443",
}
EXTRA_HITS = {
    "github-token": "github" + "_pat_" + "A" * 22 + "_" + "b" * 59,
    "ipv4": "see http://" + "192." + "168.1.20/ or " + "10.1." + "2.3.",
    "private-tld": "printer.office" + ".home" + ".arpa",
    "env-host-port": "staging database at db" + ".example" + ".com:5432",
}
CLEAN = [
    "listen on 127.0.0.1:8000 or localhost:5173 or 0.0.0.0",
    "docs use 192.0.2.10, 198.51.100.7 and 203.0.113.9",
    "version 1.2.3.4 and v10.0.0.1 and pkg==2.0.0.1 and Mozilla/5.0 Chrome/130.0.0.0",
    "OID 1.2.840.113549.1 and dotted 1.2.3.4.5",
    "public 8.8.8.8 and link-local 169.254.169.254 and multicast 224.0.0.1",
    'password = "secret" and token = "short"',
    'TEST_PASSWORD = "obviously-fake-test-password-123!"',
    'SECRET_CONTEXT = "auth-configuration:oidc-client-secret:v1"',
    'token = "${{ github.token }}"',
    "threading.local() and dev server on localhost:8000 and times 10:30",
    "development notes at 12:00; product docs",
]


def ids(text):
    return [pattern_id for _, pattern_id in privacy_check.findings(text)]


def run(*args, cwd=ROOT):
    return subprocess.run([sys.executable, "-I", str(SCRIPT), *args], cwd=cwd, capture_output=True, text=True,
                          timeout=120)


@pytest.mark.parametrize("pattern_id", sorted(PLANTED))
def test_every_pattern_finds_its_planted_shape(pattern_id):
    assert set(PLANTED) == set(privacy_check.PATTERNS)
    assert pattern_id in ids(PLANTED[pattern_id])
    if pattern_id in EXTRA_HITS:
        assert pattern_id in ids(EXTRA_HITS[pattern_id])


@pytest.mark.parametrize("line", CLEAN)
def test_clean_samples_pass(line):
    assert ids(line) == []


def test_text_mode_reports_location_and_id_never_the_value(tmp_path):
    body = tmp_path / "body.md"
    body.write_text("clean first line\n" + "\n".join(PLANTED.values()) + "\n", encoding="utf-8")
    result = run("--text", str(body))
    assert result.returncode == 1
    lines = result.stdout.splitlines()
    assert {line.rsplit(" ", 1)[1] for line in lines} == set(PLANTED)
    assert lines[0] == f"{body}:2: private-key"
    for value in [*PLANTED.values(), *EXTRA_HITS.values()]:
        for part in value.split():
            if len(part) > 6:
                assert part not in result.stdout + result.stderr


def test_event_mode_reads_title_and_body(tmp_path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"title": "Fix " + PLANTED["ipv4"], "body": None}}), encoding="utf-8")
    result = run("--event", str(event))
    assert (result.returncode, result.stdout) == (1, "pull_request.title:1: ipv4\n")
    event.write_text(json.dumps({"pull_request": {"title": "Fix it", "body": "line\r\n" + PLANTED["private-tld"]}}))
    assert run("--event", str(event)).stdout == "pull_request.body:2: private-tld\n"
    event.write_text(json.dumps({"merge_group": {}}), encoding="utf-8")
    assert run("--event", str(event)).returncode == 0


def git(repo, *args):
    subprocess.run(["git", "-c", "user.name=ci", "-c", "user.email=ci@example.invalid", "-c", "commit.gpgsign=false",
                    *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "old.txt").write_text("keep " + PLANTED["ipv4"] + "\n", encoding="utf-8")
    (tmp_path / "moved.txt").write_text("moved content\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def test_diff_mode_scans_only_added_lines_and_new_names(repo):
    (repo / "old.txt").write_text("keep " + PLANTED["ipv4"] + "\nclean\n" + PLANTED["env-host-port"] + "\n",
                                  encoding="utf-8")
    (repo / "moved").mkdir()
    git(repo, "mv", "moved.txt", "moved/" + PLANTED["ipv4"].split()[-1] + ".txt")
    (repo / "binary.bin").write_bytes(b"\0" + PLANTED["ipv4"].encode())
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "head")
    result = run("HEAD~1", "HEAD", cwd=repo)
    assert result.returncode == 1
    assert sorted(result.stdout.splitlines()) == ["moved/***.txt:0: ipv4", "old.txt:3: env-host-port"]
    assert "20.30.40" not in result.stdout
    assert run("HEAD", "HEAD", cwd=repo).returncode == 0


def test_all_mode_scans_every_tracked_file_at_head(repo):
    (repo / "untracked.txt").write_text(PLANTED["private-key"] + "\n", encoding="utf-8")
    result = run("--all", cwd=repo)
    assert (result.returncode, result.stdout) == (1, "old.txt:1: ipv4\n")


def test_usage_errors_exit_two():
    assert run().returncode == 2
    assert run("HEAD").returncode == 2
    assert run("--all", "HEAD~1", "HEAD").returncode == 2


def test_the_checker_and_its_tests_pass_their_own_check():
    for path in (SCRIPT, Path(__file__)):
        source = path.read_text(encoding="utf-8")
        assert ids(source) == [], path.name
        assert all(value not in source for value in [*PLANTED.values(), *EXTRA_HITS.values()])
