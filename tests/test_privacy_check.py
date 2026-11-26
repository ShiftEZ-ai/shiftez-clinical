"""Pipeline regression: the public-pattern privacy check finds every shape it names and never prints a value."""
import json
import re
import subprocess
import sys
import time
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
    "wildcard-dns-host": "node.tail1234" + ".ts" + ".net",
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


def run(*args, cwd=ROOT, script=SCRIPT):
    return subprocess.run([sys.executable, "-I", str(script), *args], cwd=cwd, capture_output=True, text=True,
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


HOST = "10." + "4.5.6"
TOOL_WORD_HITS = ["postgres:" + HOST, "POSTGRES=" + HOST, "node: " + HOST, "python " + HOST, "release " + HOST,
                  "Upstream/" + HOST, "~" + HOST, "^" + HOST, "x-" + HOST, HOST + "-host", "backup-" + HOST + ".tar",
                  "curl -v " + HOST + ":8080", "nc -v " + HOST, 'if host == "' + HOST + '"', "assert addr == '" + HOST + "'"]


@pytest.mark.parametrize("line", TOOL_WORD_HITS)
def test_tool_and_product_words_do_not_hide_an_address(line):
    assert ids(line) == ["ipv4"]


def test_both_ends_of_an_address_range_hit():
    hits = [hit for hit in privacy_check.matches(HOST + "-" + "10." + "4.5.9") if hit[1] == "ipv4"]
    assert len(hits) == 2


def test_other_script_digits_do_not_crash_the_check():
    fullwidth = "".join(chr(0xFF10 + int(c)) if c.isdigit() else c for c in HOST)
    assert ids("host " + fullwidth) == []


def test_private_names_match_in_any_case():
    assert ids("SQL01.CO" + "RP.LO" + "CAL") == ["private-tld"]


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


@pytest.fixture
def run_event(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    event = tmp_path / "event.json"

    def check(payload, *, script=SCRIPT):
        event.write_text(json.dumps({"repository": {"default_branch": "main"}, **payload}), encoding="utf-8")
        return run("--event", str(event), cwd=tmp_path, script=script)

    return check


def test_event_mode_reads_title_and_body(run_event):
    allowed = json.loads((ROOT / ".github/privacy-pr-openers.json").read_text(encoding="utf-8"))
    user = {"login": allowed[0], "type": "Bot"}
    result = run_event({"pull_request": {"title": "Fix " + PLANTED["ipv4"], "body": None, "user": user}})
    assert (result.returncode, result.stdout) == (1, "pull_request.title:1: ipv4\n")
    result = run_event({"pull_request": {"title": "Fix it", "body": "line\r\n" + PLANTED["private-tld"], "user": user}})
    assert (result.returncode, result.stdout) == (1, "pull_request.body:2: private-tld\n")
    assert run_event({"merge_group": {}}).returncode == 0


@pytest.mark.parametrize("user", [None, "fixture-person", {}, {"type": "Bot"},
                                  {"login": "fixture-person", "type": "User"},
                                  {"login": "fixture-other[bot]", "type": "Bot"}])
def test_event_mode_rejects_unapproved_openers_without_logging_identity(run_event, user):
    pull_request = {"title": "Fix it"}
    if user is not None:
        pull_request["user"] = user
    result = run_event({"pull_request": pull_request})
    assert (result.returncode, result.stdout, result.stderr) == (
        1, "pull_request.user:1: unapproved-pr-opener\n", "privacy check: 1 hit(s)\n")


def test_named_pull_request_event_requires_opener_data(run_event, monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    result = run_event({})
    assert (result.returncode, result.stdout) == (1, "pull_request.user:1: unapproved-pr-opener\n")


@pytest.fixture
def trusted_checker(tmp_path):
    trusted = tmp_path / ".trusted-controls"
    script = trusted / "scripts" / SCRIPT.name
    script.parent.mkdir(parents=True)
    script.write_bytes(SCRIPT.read_bytes())
    policy = trusted / ".github" / "privacy-pr-openers.json"
    policy.parent.mkdir()
    return script, policy


def test_event_mode_requires_an_allowed_bot(run_event, trusted_checker):
    script, policy = trusted_checker
    login = "fixture-publisher[bot]"
    policy.write_text(json.dumps([login]), encoding="utf-8")
    user = {"login": login, "type": "Bot"}
    assert run_event({"pull_request": {"user": user}}, script=script).returncode == 0
    user["type"] = "User"
    result = run_event({"pull_request": {"user": user}}, script=script)
    assert (result.returncode, result.stdout) == (1, "pull_request.user:1: unapproved-pr-opener\n")


@pytest.mark.parametrize("contents", [None, "{", "null", "[]", '["fixture-publisher[bot]", "fixture-person"]',
                                      '["fixture-publisher[bot]", 1]'])
def test_pr_event_refuses_missing_or_invalid_trusted_allowlist(
    tmp_path, run_event, trusted_checker, monkeypatch, contents,
):
    script, policy = trusted_checker
    if contents is not None:
        policy.write_text(contents, encoding="utf-8")
    login = "fixture-publisher[bot]"
    candidate_policy = tmp_path / ".github" / policy.name
    candidate_policy.parent.mkdir()
    candidate_policy.write_text(json.dumps([login]), encoding="utf-8")
    user = {"login": login, "type": "Bot"}
    result = run_event({"pull_request": {"user": user}}, script=script)
    assert (result.returncode, result.stdout, result.stderr) == (
        1, "pull_request.user.login:1: invalid-pr-opener-allowlist\n", "privacy check: 1 hit(s)\n")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "merge_group")
    assert run_event({"merge_group": {}}, script=script).returncode == 0


def git(repo, *args, data=None):
    return subprocess.run(["git", "-c", "user.name=ci", "-c", "user.email=ci@example.invalid", "-c", "commit.gpgsign=false",
                           *args], cwd=repo, check=True, capture_output=True, input=data).stdout


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
    # Raw blobs are scanned whatever they hold: a NUL byte no longer makes a file "binary" and unscanned.
    (repo / "binary.bin").write_bytes(b"\0" + PLANTED["ipv4"].encode())
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "head")
    result = run("HEAD~1", "HEAD", cwd=repo)
    assert result.returncode == 1
    head = git(repo, "rev-parse", "HEAD").decode()[:12]
    assert sorted(result.stdout.splitlines()) == [
        f"{head}:binary.bin:1: ipv4", f"{head}:moved/***.txt:0: ipv4", f"{head}:old.txt:3: env-host-port"]
    assert "20.30.40" not in result.stdout
    assert run("HEAD", "HEAD", cwd=repo).returncode == 0


IP = "10." + "20.30.40"
ENV_HOST = "produc" + "tion-db" + ":5432"
# Each case hides a host from a patch-text parser in a different way; the tree comparison must still see it.
BYPASSES = {
    "added line starting with ++": ({}, {"a.txt": b"++ note\n" + IP.encode() + b"\n"}, "", r"a\.txt:2: ipv4"),
    "file name with a tab and a quote": ({}, {'a\tb".cfg': IP.encode()}, "", r'a\tb"\.cfg:1: ipv4'),
    "form feed before the value": ({}, {"a.txt": b"ok\fleak " + IP.encode()}, "", r"a\.txt:1: ipv4"),
    "gitattributes -diff mark": ({}, {".gitattributes": b"h/*.cfg -diff\n", "h/x.cfg": ENV_HOST.encode()}, "",
                                 r"h/x\.cfg:1: env-host-port"),
    "symlink turned into a file": ({"link": ("120000", b"target")}, {"link": IP.encode()}, "", r"link:1: ipv4"),
    "user at address": ({}, {"a.sh": b"ssh user@" + IP.encode()}, "", r"a\.sh:1: ipv4"),
    "address in a database URL": ({}, {"a.env": b"postgresql://u:pw@" + IP.encode() + b":5432/db"}, "",
                                  r"a\.env:1: ipv4"),
    "scp target": ({}, {"a.sh": b"scp f deploy@" + IP.encode() + b":/srv"}, "", r"a\.sh:1: ipv4"),
    "production host and port": ({"a.txt": b"one\n"}, {"a.txt": b"one\n" + ENV_HOST.encode()}, "",
                                 r"a\.txt:2: env-host-port"),
    "commit message": ({}, {"a.txt": b"clean"}, "Point at " + IP, r"commit [0-9a-f]{12}:1: ipv4"),
}


def commit(repo, files, message):
    for path, value in files.items():
        mode, data = value if isinstance(value, tuple) else ("100644", value)
        sha = git(repo, "hash-object", "-w", "--stdin", data=data).decode().strip()
        # Index plumbing needs no checkout, so names a Windows file system refuses still work.
        git(repo, "-c", "core.protectNTFS=false", "update-index", "--add", "--cacheinfo", f"{mode},{sha},{path}")
    git(repo, "commit", "-q", "--allow-empty", "-m", message or "change")


@pytest.mark.parametrize("case", sorted(BYPASSES))
def test_diff_mode_sees_what_a_patch_parser_misses(tmp_path, case):
    base, head, message, expected = BYPASSES[case]
    git(tmp_path, "init", "-q")
    commit(tmp_path, base, "base")
    commit(tmp_path, head, message)
    result = run("HEAD~1", "HEAD", cwd=tmp_path)
    assert result.returncode == 1
    assert [line for line in result.stdout.splitlines() if re.fullmatch(r"(?:[0-9a-f]{12}:)?" + expected, line)], result.stdout
    assert "20.30.40" not in result.stdout + result.stderr


def test_every_commit_is_scanned_not_only_the_net_diff(tmp_path):
    git(tmp_path, "init", "-q")
    commit(tmp_path, {"a.txt": b"clean\n"}, "base")
    commit(tmp_path, {"a.txt": b"clean\n" + IP.encode() + b"\n"}, "add")
    added = git(tmp_path, "rev-parse", "HEAD").decode()[:12]
    commit(tmp_path, {"a.txt": b"clean\n"}, "remove")
    assert git(tmp_path, "diff", "HEAD~2", "HEAD") == b""  # the net diff is empty
    result = run("HEAD~2", "HEAD", cwd=tmp_path)
    assert (result.returncode, result.stdout) == (1, f"{added}:a.txt:2: ipv4\n")
    assert "20.30.40" not in result.stdout + result.stderr


def test_a_merge_adds_only_what_no_parent_holds(tmp_path):
    git(tmp_path, "init", "-q", "-b", "trunk")
    commit(tmp_path, {"a.txt": b"clean\n"}, "base")
    git(tmp_path, "checkout", "-q", "-b", "side")
    commit(tmp_path, {"side.txt": IP.encode() + b"\n"}, "side")
    side = git(tmp_path, "rev-parse", "HEAD").decode()[:12]
    git(tmp_path, "checkout", "-q", "trunk")
    commit(tmp_path, {"b.txt": b"other\n"}, "trunk")
    git(tmp_path, "merge", "-q", "--no-ff", "--no-commit", "side")
    commit(tmp_path, {"merge.txt": ENV_HOST.encode() + b"\n"}, "merge")
    merge = git(tmp_path, "rev-parse", "HEAD").decode()[:12]
    lines = run("HEAD~1~1", "HEAD", cwd=tmp_path).stdout.splitlines()
    # The side value hits at its own commit, not again at the merge; a value the merge itself adds hits there.
    assert sorted(lines) == sorted([f"{side}:side.txt:1: ipv4", f"{merge}:merge.txt:1: env-host-port"])


def test_author_and_committer_are_scanned(tmp_path):
    git(tmp_path, "init", "-q")
    commit(tmp_path, {"a.txt": b"clean"}, "base")
    host = "build" + ".co" + "rp"
    git(tmp_path, "-c", "user.email=ops@" + host, "commit", "-q", "--allow-empty", "-m", "change")
    lines = run("HEAD~1", "HEAD", cwd=tmp_path).stdout.splitlines()
    assert [line.split(" ", 2)[2] for line in lines] == ["author:1: private-tld", "committer:1: private-tld"]


@pytest.fixture
def long_clean_line():
    return ("-" * 10 + "QUJD" * 10) * 4000


def test_a_long_line_has_no_hits(long_clean_line):
    assert ids(long_clean_line) == []


@pytest.mark.wall_benchmark
def test_a_long_line_scans_in_linear_time(long_clean_line):
    started = time.perf_counter()
    ids(long_clean_line)
    elapsed = time.perf_counter() - started
    assert elapsed < 1


def test_all_mode_scans_every_tracked_file_at_head(repo):
    (repo / "untracked.txt").write_text(PLANTED["private-key"] + "\n", encoding="utf-8")
    result = run("--all", cwd=repo)
    assert (result.returncode, result.stdout) == (1, "old.txt:1: ipv4\n")


def test_usage_errors_exit_two():
    assert run().returncode == 2
    assert run("HEAD").returncode == 2
    assert run("--all", "HEAD~1", "HEAD").returncode == 2


def test_a_bad_revision_fails(repo):
    assert run("no-such-revision", "HEAD", cwd=repo).returncode != 0


def test_the_checker_and_its_tests_pass_their_own_check():
    for path in (SCRIPT, Path(__file__)):
        source = path.read_text(encoding="utf-8")
        assert ids(source) == [], path.name
        assert all(value not in source for value in [*PLANTED.values(), *EXTRA_HITS.values()])
