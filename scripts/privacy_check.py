"""Public-pattern privacy check: secret shapes and host shapes in new lines, new file names, commit messages and PR text.

Usage:
  python scripts/privacy_check.py <base> <head> [--event PATH] [--text FILE ...]
  python scripts/privacy_check.py --all [--text FILE ...]
  python scripts/privacy_check.py --text FILE [--text FILE ...]

Each hit prints as `path:line: <pattern-id>`; the matched value is never printed. Exit 1 on any hit, 0 when clean.
Only public, generic patterns live here; any private name list is checked elsewhere.

Diff mode scans EVERY commit of base..head against its parents, never only the net diff, so a value added in one
commit and removed in the next still hits; each hit names its commit. It reads raw blobs, so no patch text, .gitattributes mark, diff driver,
quoted name or line separator other than "\n" can hide a line. Deliberate trade-off: `secret-assignment` only flags
quoted mixed-class literals, so lowercase-hex keys, unquoted `.env` or YAML values and passwords with punctuation
pass; GitHub secret scanning covers provider tokens.
"""
import argparse
import bisect
from collections import Counter
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys

# Built from parts so this file does not match its own patterns.
_TLD = r"(?:lan|internal|local|corp|home\.arpa)"
_ENV = r"(?:production|preview|prod|staging|stg|dev)"
_HOST = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"

PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
    # The documented shapes: classic tokens carry 36 characters (GitHub says this may grow), fine-grained ones 22 + 59.
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,251}|github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59})\b"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[0-9A-Z]{16}\b"),
    "slack-token": re.compile(r"\bxox[abposr]-[0-9A-Za-z-]{10,}|hooks\.slack\.com/services/T[0-9A-Z]+/B[0-9A-Z]+/\w+"),
    "google-api-key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])"),
    # Starts at the keyword and every run is bounded, so a long line scans in linear time.
    "secret-assignment": re.compile(
        r"(?i)(?:password|passwd|secret|token|api[_-]?key)[\w-]{0,64}[\"']?[ \t]{0,40}[:=][ \t]{0,40}[bru]{0,2}[\"']"
        r"([A-Za-z0-9+/=_.~-]{20,256})[\"']"),
    # ASCII digits only (other scripts' digits would crash ipaddress). A dash beside an address hides nothing: ranges,
    # file names (backup-<ip>.tar) and host names (<ip>-prod) all count.
    "ipv4": re.compile(r"(?<![\w.])([0-9]{1,3}(?:\.[0-9]{1,3}){3})(?![\w]|\.[0-9])"),
    "wildcard-dns-host": re.compile(r"(?i)\b(?:sslip|nip|xip)\.io\b|\.ts\.net\b|\b(?:duckdns\.org|ddns\.net)\b"),
    # These two find their literal tail first; HOST_BEFORE then checks the host in front of it (fast on big trees).
    "private-tld": re.compile(rf"(?i)\.{_TLD}(?![\w-]|\.\w|\()"),
    "env-host-port": re.compile(r"(?<=[A-Za-z0-9]):\d{2,5}\b"),
}
HOST_BEFORE = {
    "private-tld": re.compile(rf"(?i)(?<![\w.$-]){_HOST}(?:\.{_HOST})*$"),
    "env-host-port": re.compile(
        rf"(?i)(?:(?<![\w.-])(?:{_HOST}\.)*(?:[a-z0-9]+-)*{_ENV}(?:-[a-z0-9]+)*(?:\.{_HOST})*"
        rf"|\b{_ENV}\b[^\n]{{0,40}}?(?<![\w.-]){_HOST}(?:\.{_HOST})+)$"),
}

# Well-known public addresses that tests use as "some public host"; they reveal no deployment.
WELL_KNOWN = {"1.1.1.1", "1.0.0.1", "8.8.8.8", "8.8.4.4", "9.9.9.9", "93.184.216.34"}
DOC_NETWORKS = [ipaddress.ip_network(n) for n in ("127.0.0.0/8", "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]
# No "@": it would clear user@host, scp targets and DSN hosts, and a four-part package version after "@" is rare.
# Version markers and pins only. Tool and product words (python, node, postgres, release, "Name/") are not: they
# also precede real hosts. A "Name/a.b.c.d" product token counts as a version only when it ends in .0, as host
# addresses almost never do.
# "v" must touch the value (v1.2.3.4; not "curl -v <ip>"), and a pin must follow a name with no space (pkg==1.2.3.4;
# not `host == "<ip>"`).
VERSION_CONTEXT = re.compile(r"(?i)(?:\bv|\bver(?:sion)?\s*[\"':=]*\s*|[\w\]](?:==|>=|<=|~=))$")
PRODUCT_CONTEXT = re.compile(r"\b[A-Z][A-Za-z]+/$")
PLACEHOLDER = re.compile(r"(?i)example|placeholder|changeme|dummy|redacted|fake|test|sample|fixture|your[_-]|xxxx")
PROSE = re.compile(r"[a-z]{7,}")  # a random literal rarely holds 7 lowercase letters in a row; a word does
LOOPBACK_HOST = re.compile(r"(?i)^(?:localhost|127\.0\.0\.1|0\.0\.0\.0)$")


def entropy(value):
    counts = {c: value.count(c) for c in set(value)}
    return -sum(n / len(value) * math.log2(n / len(value)) for n in counts.values())


def hit_start(pattern_id, match, prefix):
    """Where the hit starts, or None for the generic false positives each pattern is known for."""
    if pattern_id == "ipv4":
        octets = match.group(1).split(".")
        if any(int(o) > 255 or (len(o) > 1 and o[0] == "0") for o in octets):
            return None
        address = ipaddress.ip_address(match.group(1))
        if (str(address) in WELL_KNOWN or address.is_unspecified or address.is_link_local or address.is_multicast
                or address.is_reserved or any(address in net for net in DOC_NETWORKS) or VERSION_CONTEXT.search(prefix)
                or (PRODUCT_CONTEXT.search(prefix) and octets[-1] == "0")):
            return None
    if pattern_id == "secret-assignment":
        value = match.group(1)
        classes = sum(bool(re.search(p, value)) for p in ("[a-z]", "[A-Z]", "[0-9]", "[^A-Za-z0-9]"))
        if PLACEHOLDER.search(value) or PROSE.search(value) or classes < 3 or entropy(value) < 3.5:
            return None
    if pattern_id in HOST_BEFORE:
        host = HOST_BEFORE[pattern_id].search(prefix[-80:])
        if not host or LOOPBACK_HOST.match(host.group(0).split()[-1]):
            return None
        return match.start() - len(host.group(0))
    return match.start()


def matches(text):
    """Yield (line number, pattern id, start, end) for each hit; whole-text search keeps it fast."""
    starts = [0] + [m.end() for m in re.finditer(r"\n", text)]
    for pattern_id, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            number = bisect.bisect_right(starts, match.start())
            # Only the tail before a hit decides; a bounded slice keeps many hits on one long line linear.
            start = hit_start(pattern_id, match, text[max(starts[number - 1], match.start() - 120):match.start()])
            if start is not None:
                yield number, pattern_id, start, match.end()


def findings(text):
    """(line number, pattern id) once per hit line, in line order."""
    order = list(PATTERNS)
    return sorted({hit[:2] for hit in matches(text)}, key=lambda hit: (hit[0], order.index(hit[1])))


def redact(text):
    hidden = [False] * len(text)
    for _, _, start, end in matches(text):
        hidden[start:end] = [True] * (end - start)
    return re.sub(r"\0+", "***", "".join("\0" if gone else c for c, gone in zip(text, hidden)))


def scan_text(where, text, hits, first=1):
    hits.extend(f"{redact(where)}:{number + first - 1}: {pattern_id}" for number, pattern_id in findings(text))


def git(*args):
    return subprocess.run(["git", "-c", "core.quotepath=off", *args], check=True, capture_output=True).stdout


def blobs(shas):
    """Yield the raw bytes of each blob in order through one `git cat-file --batch`: no filter, no attribute."""
    with subprocess.Popen(["git", "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE) as batch:
        for sha in shas:
            batch.stdin.write(sha.encode() + b"\n")
            batch.stdin.flush()
            header = batch.stdout.readline().split()
            if len(header) != 3:
                raise SystemExit(f"git cat-file: no blob {sha}")
            yield batch.stdout.read(int(header[2]))
            batch.stdout.read(1)
        batch.stdin.close()
    if batch.returncode:
        raise SystemExit("git cat-file failed")


def scan_blob(path, content, hits, old=b""):
    """Scan the lines of `content` that `old` does not hold, numbered as in `content`; split on "\n" only."""
    before = Counter(old.split(b"\n")) if old else Counter()
    kept = []
    for line in content.split(b"\n"):
        kept.append(b"" if before[line] else line)
        before[line] -= bool(before[line])
    scan_text(path, b"\n".join(kept).decode("utf-8", "replace"), hits)


EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def tree_blobs(rev):
    """path -> blob sha for every file in `rev`."""
    entries = [e for e in git("ls-tree", "-r", "-z", rev).split(b"\0") if e]
    return {path.decode("utf-8", "replace"): meta.split()[2].decode()
            for meta, path in (e.split(b"\t", 1) for e in entries) if meta.split()[1] == b"blob"}


def scan_commit(sha, parents, hits):
    """Scan what one commit adds against its first parent. In a merge, a line any parent already holds is not new."""
    first = parents[0] if parents else EMPTY_TREE
    fields = git("diff-tree", "-r", "-z", "--no-renames", "--raw", first, sha).split(b"\0")
    others = [tree_blobs(parent) for parent in parents[1:]]
    changes = []
    for meta, raw_path in zip(fields[0::2], fields[1::2]):
        old_mode, new_mode, old_sha, new_sha, status = meta.decode()[1:].split()
        path = raw_path.decode("utf-8", "replace")
        where = f"{sha[:12]}:{path}"
        if status == "A" and not any(path in other for other in others):  # a rename is a delete plus an add
            scan_text(where, path, hits, first=0)
        if status in ("A", "M", "T") and new_mode != "160000":  # a submodule entry names a commit, not a blob
            old = [old_sha] if status != "A" and old_mode != "160000" else []
            changes.append((where, old + [other[path] for other in others if path in other], new_sha))
    contents = blobs([s for _, olds, new in changes for s in (*olds, new)])
    for where, olds, _ in changes:
        old_content = b"\n".join(next(contents) for _ in olds)
        scan_blob(where, next(contents), hits, old_content)
    next(contents, None)  # run the reader to its end so its exit status is checked


def scan_diff(base, head, hits):
    """Every commit in base..head on its own: a value added in one commit and removed in the next still hits.
    Each commit's author, committer and message are scanned too."""
    for line in git("rev-list", "--reverse", "--topo-order", "--parents", f"{base}..{head}").decode().split("\n"):
        if line:
            sha, *parents = line.split()
            scan_commit(sha, parents, hits)
    log = git("log", "-z", "--format=%H%n%an <%ae>%n%cn <%ce>%n%B", f"{base}..{head}").split(b"\0")
    for entry in filter(None, log):
        sha, author, committer, message = (entry.decode("utf-8", "replace") + "\n").split("\n", 3)
        scan_text(f"commit {sha[:12]} author", author, hits)
        scan_text(f"commit {sha[:12]} committer", committer, hits)
        scan_text("commit " + sha[:12], message, hits)


def scan_all(hits):
    entries = [e.decode("utf-8", "replace") for e in git("ls-tree", "-r", "-z", "HEAD").split(b"\0") if e]
    files = [(meta.split()[2], path) for meta, path in (e.split("\t", 1) for e in entries) if meta.split()[1] == "blob"]
    for content, (_, path) in zip(blobs([sha for sha, _ in files]), files):  # reader first: zip runs it to its end
        scan_text(path, path, hits, first=0)
        scan_blob(path, content, hits)


def scan_pr_opener(user, hits):
    # CI runs this script from the trusted checkout while scanning the candidate.
    policy = Path(__file__).resolve().parents[1] / ".github" / "privacy-pr-openers.json"
    try:
        allowed = json.loads(policy.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        allowed = None
    if not isinstance(allowed, list) or not allowed or any(
        not isinstance(login, str) or not re.fullmatch(r"[a-z0-9]+(?:-+[a-z0-9]+)*\[bot\]", login)
        for login in allowed
    ):
        hits.append("pull_request.user.login:1: invalid-pr-opener-allowlist")
    elif not isinstance(user, dict) or user.get("type") != "Bot" or user.get("login") not in allowed:
        hits.append("pull_request.user:1: unapproved-pr-opener")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("revisions", nargs="*", metavar="base head")
    parser.add_argument("--text", action="append", default=[], metavar="FILE")
    parser.add_argument("--event", metavar="PATH")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(errors="backslashreplace")  # a non-ASCII path must not crash a narrow console
    if len(args.revisions) not in (0, 2) or (args.all and args.revisions) or not (args.revisions or args.all or args.text or args.event):
        parser.error("give <base> <head>, --all, --text or --event")
    hits = []
    if args.revisions:
        scan_diff(*args.revisions, hits)
    if args.all:
        scan_all(hits)
    for name in args.text:
        with open(name, encoding="utf-8", errors="replace") as handle:
            scan_text(name, handle.read(), hits)
    if args.event:
        with open(args.event, encoding="utf-8") as handle:
            event = json.load(handle)
        pull_request = event.get("pull_request")
        event_name = os.environ.get("GITHUB_EVENT_NAME")
        if event_name == "pull_request" or (not event_name and "pull_request" in event):
            scan_pr_opener(pull_request.get("user") if isinstance(pull_request, dict) else None, hits)
        if isinstance(pull_request, dict):
            for field in ("title", "body"):
                scan_text("pull_request." + field, pull_request.get(field) or "", hits)
    for hit in hits:
        print(hit)
    print(f"privacy check: {len(hits)} hit(s)", file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
