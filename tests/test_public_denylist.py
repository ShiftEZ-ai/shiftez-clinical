# Public deny-list test: GENERIC rules only (address and host shapes, runner and workflow rules, forbidden file
# names, binary allow-list, empty files, the legacy token only inside the listed identifiers). It fails closed: a
# missing or empty rule file, a file it cannot read, or a tree it cannot list is a failure, never a skip.
# Known limits: regexes see text after UTF-8 (else latin-1) decoding only; content inside compressed or encoded
# files is not read (binary files must be on the allow-list instead).
import hashlib
import json
import re
import subprocess
from pathlib import Path

# ---- denycore (generated copy; do not edit by hand) ----
# Shared path and workflow rules, embedded in tests/test_public_denylist.py.
import re











def decode(data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")




def glob_rx(glob, nocase=False):
    """`**` crosses folders, `*` and `?` do not."""
    out, i = [], 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out) + r"\Z", re.IGNORECASE if nocase else 0)


def allowed(path, allow_rxs):
    return any(rx.match(path) for rx in allow_rxs)


HOSTED = re.compile(r"(?:ubuntu-(?:latest|2[0-9]\.04)(?:-arm)?|windows-(?:latest|20[0-9]{2})|macos-(?:latest|[0-9]{2})(?:-large|-xlarge)?)")
_RUNS_ON = re.compile(r"""["']?runs-on["']?\s*:\s*([^\n,}\]#]*(?:\[[^\]]*\])?)""")
_GITHUB_RUNNER_VARS = "TEMP|TOOL_CACHE|OS|ARCH|NAME|DEBUG|ENVIRONMENT|WORKSPACE"
_BAD_REF = re.compile(r"CI_RUNS_ON\w*|\bRUNNER_(?!(?:" + _GITHUB_RUNNER_VARS + r")\b)[A-Z_]+|pull_request_target|self-hosted|\bvars\.\w*(?:MIRROR|REGISTRY)\w*|\b\w*(?:_MIRROR|_REGISTRY)\b", re.I)


def workflow_hits(path, text):
    """Public CI rule: only literal GitHub-hosted runners, no runner/mirror variables, no pull_request_target."""
    if not (path.startswith(".github/") and path.endswith((".yml", ".yaml"))):
        return []
    hits = []
    text = unescape(text)  # YAML/JSON \u and \x escapes, HTML entities, percent-encoding
    for m in _RUNS_ON.finditer(text):
        value = m.group(1).split("#", 1)[0].strip().strip("'\"")
        if not HOSTED.fullmatch(value):
            hits.append(("W01", "runs-on is not a literal GitHub-hosted label"))
    for m in _BAD_REF.finditer(text):
        hits.append(("W02", "runner, mirror or registry variable, self-hosted, or pull_request_target"))
    return hits


_UESC = re.compile(r"\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})")
_NONWORD = re.compile(r"[\W_]+")


def unescape(text):
    import html
    import urllib.parse
    text = html.unescape(text)
    text = _UESC.sub(lambda m: chr(int(m.group(1) or m.group(2), 16)), text)
    if "%" in text:
        text = urllib.parse.unquote(text)
    return text


def squash(text, keep_separators=False):
    """Case-folded, escapes decoded; without separators every non-alphanumeric run is dropped (fused, split, any spacing)."""
    text = unescape(text).casefold()
    return text if keep_separators else _NONWORD.sub("", text)

# ---- end denycore ----

ROOT = Path(__file__).resolve().parents[1]
LIST = Path(__file__).resolve().with_name("public_denylist.json")


def _load():
    data = json.loads(LIST.read_text(encoding="utf-8"))
    assert data.get("version") == 3, "public_denylist.json: unknown version"
    assert data.get("regex") and data.get("forbid_paths"), "public_denylist.json is empty"
    return data


def _tracked():
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "-s"], capture_output=True, check=True).stdout
    rows = [r for r in out.decode("utf-8").split("\0") if r]
    assert rows, "no tracked files listed"
    paths = []
    for r in rows:
        meta, path = r.split("\t", 1)
        assert meta.split()[0] in ("100644", "100755"), f"not a plain file: {path}"
        assert ".git" not in path.split("/"), f"git metadata tracked: {path}"
        paths.append(path)
    return sorted(paths)


def _hits():
    data = _load()
    exclude = set(data["exclude"])
    regexes = [
        {"id": r["id"], "class": r["class"], "rx": re.compile(r["pattern"], re.IGNORECASE if r["case"] == "insensitive" else 0),
         "allow": [glob_rx(g) for g in r.get("allow_paths", [])]}
        for r in data["regex"]
    ]
    binary_allow = [(glob_rx(g.split("#")[0]), g.split("#")[1] if "#" in g else None) for g in data["binary_allow"]]
    found = []
    for path in _tracked():
        raw = (ROOT / path).read_bytes()
        targets = [("name", path)]
        if path not in exclude:
            targets.append(("body", decode(raw)))
        if b"\0" in raw and not any(rx.match(path) and (sha is None or hashlib.sha256(raw).hexdigest() == sha)
                                     for rx, sha in binary_allow):
            found.append(("BIN", "binary-not-allowed", "body", path))
        for where, text in targets:
            for r in regexes:
                if not allowed(path, r["allow"]) and r["rx"].search(unescape(text)):
                    found.append((r["id"], r["class"], where, path))
            if where == "body":
                for wid, _why in workflow_hits(path, text):
                    found.append((wid, "workflow", where, path))

    return data, found


def test_no_forbidden_token_or_path():
    data, found = _hits()
    forbid = [(f["id"], glob_rx(f["glob"], nocase=True), [glob_rx(x, nocase=True) for x in f.get("except", [])])
              for f in data["forbid_paths"]]
    for path in _tracked():
        for fid, rx, exc in forbid:
            if rx.match(path) and not any(x.match(path) for x in exc):
                found.append((fid, "forbidden-path", "name", path))
    for path in data["empty"]:
        p = ROOT / path
        if p.exists() and p.stat().st_size:
            found.append(("EMPTY", "low-profile", "body", path))
    # ids, classes and paths only: never the token
    assert not found, "deny-list hits:\n" + "\n".join(" ".join(x) for x in sorted(set(found)))
