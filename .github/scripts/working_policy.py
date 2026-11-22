"""Trusted dev policy: branch roles, tree promotion, shrinking allowances and migration floors."""
import argparse
import ast
from datetime import date
import difflib
from fnmatch import fnmatchcase
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tomllib
import sys
from urllib.request import Request, urlopen
sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[2] / "ci")]
import node_census
import hardening
from quarantine_registry import QUARANTINE, QUARANTINE_RETIRED, decode_retired

from agent_names import AGENT_FILES, AGENT_DIRECTORIES, AGENT_PREFIXES, private_ignore_content
from agent_names import private_agent_path as agent_name_path

# The public agent instructions at the repository root, by exact name and byte-encoded as in agent_names;
# nested copies, other cases and tool folders stay private.
PUBLIC_AGENT_FILES = frozenset(bytes(value).decode("ascii") for value in (
    (65, 71, 69, 78, 84, 83, 46, 109, 100),
    (67, 76, 65, 85, 68, 69, 46, 109, 100),
))


def private_agent_path(path):
    return path not in PUBLIC_AGENT_FILES and agent_name_path(path)

HOOKS = {
    "pytest_runtest_makereport", "pytest_report_teststatus", "pytest_collection_modifyitems",
    "pytest_deselected", "pytest_ignore_collect", "pytest_collect_file", "pytest_pycollect_makeitem",
    "pytest_runtest_protocol", "pytest_runtest_call", "pytest_configure", "pytest_addhooks",
    "pytest_terminal_summary", "pytest_sessionfinish", "pytest_unconfigure", "pytest_addoption",
}
PYTHON_MODULES = {"pytest", "playwright", "ruff", "venv", "pip"}
PYTHON = re.compile(r"python(?:3(?:\.\d+)?)?")
PIP = re.compile(r"pip(?:3(?:\.\d+)?)?")
MODULE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
PYTHON_MODE = re.compile(r"-[bBdEhiIOPqRsSuvx]*([mc])(.*)")
EXPRESSION = re.compile(r"\$\{\{.*?\}\}")
SCRIPT_SUFFIXES = (".py", ".sh", ".mjs", ".cjs", ".js", ".ts")
SHELL_SEPARATORS = ";|&<>()"
SHELL_PREFIXES = {"if", "elif", "then", "else", "while", "until", "do", "!", "env", "exec", "command", "time"}
SHELL_ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=")

UNRESOLVED_KEYS = ("workflow", "job", "step", "run_sha256", "reference")


class Unresolved(str):
    """A run-step form the inventory cannot resolve; reported in findings.json for the workflow's reviewer."""


REPOSITORY = 1459752401
CODEOWNER_TEAM = "@ShiftEZ-ai/maintainers"
# Last-push approval is deliberately not required: stale-review dismissal already voids an approval on a new push.
REVIEW_FLAGS = ("require_code_owner_review", "dismiss_stale_reviews_on_push")
KNOWN = "ci/known-failures.json"
NODE_LEDGER = "ci/removed-tests.json"
SHRINK_ONLY = {
    "ci/known-skips.json": ("skips",),
    "ci/frozen-baseline-failures.json": (),
    "ci/failure-classifications.json": ("entries",),
    "ci/hosted-env-exceptions.json": ("exceptions",),
    "ci/unstable-seeds.json": ("nodes",),
}
QUARANTINE_DAYS = 30
MARKER = "ci/working-rules.json"
CONTRACT = {"schema": 4, "ci_result_contract": "failclosed-main-reviewed-errors-v32"}
LISTS = {
    KNOWN: False, MARKER: False,
    "ci/known-skips.json": False,
    "ci/failure-classifications.json": False,
    "ci/hardening.json": True,
    "ci/frozen-baseline-failures.json": False,
    "ci/root-run-evidence.json": False, NODE_LEDGER: False,
    "ci/hosted-env-exceptions.json": False,
    "ci/unstable-seeds.json": True, "ci/hosted-durations.json": True,
    QUARANTINE: True, QUARANTINE_RETIRED: True,
}


def git(*args):
    return subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL)


def commit(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{40}", value):
        raise ValueError("Invalid commit identity")
    return value


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def decode(raw):
    return json.loads(raw, object_pairs_hook=unique)


def tree(revision):
    return git("rev-parse", revision + "^{tree}").decode().strip()


def read_file(revision, path):
    entry = git("ls-tree", "-z", revision, "--", path)
    if not entry:
        return None
    metadata, _, name = entry.rstrip(b"\0").partition(b"\t")
    allowed = (b"100644 blob ",) if path in LISTS else (b"100644 blob ", b"100755 blob ")
    if name != path.encode() or not metadata.startswith(allowed):
        raise ValueError("Policy input must be a regular file")
    return git("show", revision + ":" + path)


def retired_at_revision(revision, *, allow_legacy=True):
    try:
        raw = read_file(revision, QUARANTINE_RETIRED)
        if raw is None and allow_legacy:
            source = read_file(revision, ".github/scripts/working_policy.py")
            # QUARANTINE_RETIRED was introduced with the list and must remain the activation marker.
            if source and b"QUARANTINE_RETIRED" not in source:
                return None, {}
        return raw, decode_retired(raw)
    except (OSError, subprocess.CalledProcessError):
        raise ValueError("Cannot read trusted quarantine retired list: " + QUARANTINE_RETIRED) from None
    except (ValueError, RecursionError) as error:
        raise ValueError(f"Invalid quarantine retired list {QUARANTINE_RETIRED}: {error}") from None


def known_failure_classes(raw):
    data = decode(raw) if raw else {}
    return {entry["node"]: entry.get("class", "unstable" if key == "unstable" else None)
            for key in ("fails", "unstable") for entry in (data.get(key) or [])
            if isinstance(entry, dict) and isinstance(entry.get("node"), str)}


def known_failure_expiries(raw):
    data = decode(raw) if raw else {}
    return {entry["node"]: entry["expires_on"] for key in ("fails", "unstable") for entry in (data.get(key) or [])
            if isinstance(entry, dict) and isinstance(entry.get("node"), str) and isinstance(entry.get("expires_on"), str)}


def moved_failure_classes(before, after):
    return {node: cls for node, cls in known_failure_classes(before).items() if node not in after}


def quarantine_change(before_raw, after_raw, known_before_raw, known_after_raw, *, retired=frozenset()):
    """Keep or remove existing quarantine entries; add only entries MOVED from known failures or recovered FLAKES."""
    def encoded(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    before = decode(before_raw) if before_raw is not None else []
    after = decode(after_raw) if after_raw is not None else None
    if not isinstance(before, list) or not isinstance(after, list):
        raise ValueError(QUARANTINE + " must stay a list of entries")
    existing = {entry.get("nodeid"): encoded(entry) for entry in before if isinstance(entry, dict)}
    known_after = known_failure_classes(known_after_raw)
    moved = moved_failure_classes(known_before_raw, known_after)
    source_expiry = known_failure_expiries(known_before_raw)
    seen = set()
    for entry in after:
        node = entry.get("nodeid") if isinstance(entry, dict) else None
        if not isinstance(node, str) or node in seen:
            raise ValueError(QUARANTINE + " entries need one unique nodeid each")
        seen.add(node)
        if node in known_after:
            raise ValueError(QUARANTINE + " entries must be disjoint from " + KNOWN + ": " + node)
        if node in existing:
            if encoded(entry) != existing[node]:
                raise ValueError(QUARANTINE + " may not edit an existing entry: " + node)
            continue
        if node in retired:
            raise ValueError(QUARANTINE + " may not re-add a retired entry: " + node)
        issue, owner, reason = entry.get("issue"), entry.get("owner"), entry.get("reason")
        if type(issue) is not int or issue < 1:
            raise ValueError(QUARANTINE + " additions need an issue number of at least 1: " + node)
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError(QUARANTINE + " additions need an owner: " + node)
        if type(entry.get("blocker")) is not bool:
            raise ValueError(QUARANTINE + " additions need a true or false blocker flag: " + node)
        if not isinstance(reason, str) or ":" not in reason:
            raise ValueError(QUARANTINE + " additions need a reason that starts with its class: " + node)
        try:
            days = (date.fromisoformat(entry["expires"]) - date.fromisoformat(entry["added"])).days
        except (KeyError, TypeError, ValueError):
            days = None
        # The bound counts from 'added'; the quarantine validity test refuses a future 'added', so it cannot be stretched.
        if days is None or not 0 <= days <= QUARANTINE_DAYS:
            raise ValueError(QUARANTINE + " additions need an expiry at most 30 days after added: " + node)
        cls = reason.split(":", 1)[0].strip()
        flake = cls == "unstable" and node not in moved
        if flake or "recovered_runs" in entry:
            runs = entry.get("recovered_runs")
            if (not isinstance(runs, list) or not runs
                    or not all(isinstance(run, str) and re.fullmatch(r"[0-9]+", run) for run in runs)):
                raise ValueError(QUARANTINE + " flake additions need the run ids where they recovered: " + node)
        if moved.get(node) == cls and node in source_expiry:
            try:
                later = date.fromisoformat(entry["expires"]) > date.fromisoformat(source_expiry[node])
            except ValueError:
                later = True
            if later:
                raise ValueError(QUARANTINE + " moved entries may not expire later than their " + KNOWN
                                 + " entry: " + node)
        if moved.get(node) == cls or flake:
            continue
        raise ValueError(QUARANTINE + " may only add entries moved from " + KNOWN
                         + " with their class, or recovered flakes with run evidence: " + node)


def quarantine_retired_change(before_raw, after_raw, registry_before, registry_after):
    try:
        before = decode_retired(before_raw) if before_raw is not None else {}
        after = decode_retired(after_raw)
    except (ValueError, RecursionError) as error:
        raise ValueError(f"Invalid {QUARANTINE_RETIRED}: {error}") from None
    append_only = QUARANTINE_RETIRED + " is append-only"
    base_entries = {entry["nodeid"]: entry for entry in decode(registry_before)}
    removed = before.keys() - after.keys()
    if removed:
        remaining_nodes = {entry["nodeid"] for entry in decode(registry_after)}
        if removed & remaining_nodes:
            raise ValueError(append_only + "; remove the same nodes from " + QUARANTINE + " before or with compaction")
    retained = [(node, token) for node, token in before.items() if node in after]
    if (list(after.items())[:len(retained)] != retained
            or before_raw is not None and before == after and before_raw != after_raw):
        raise ValueError(append_only + "; existing entries must stay byte-unchanged and in order")
    for node in after.keys() - before.keys():
        if node not in base_entries:
            raise ValueError(append_only + "; new entry is absent from the base " + QUARANTINE + ": " + node)
        row = base_entries[node]
        if row["blocker"]:
            raise ValueError(append_only + "; release blockers may not be retired: " + node)
        if row["reason"].split(":", 1)[0].strip() not in {"baseline", "baseline-error"}:
            raise ValueError(append_only + "; only baseline or baseline-error entries may be retired: " + node)


def removal_only(before_raw, after_raw, entry_keys=("fails", "unstable")):
    if before_raw is None or after_raw is None:
        return False
    before, after = decode(before_raw), decode(after_raw)
    def encoded(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    if entry_keys:
        if not isinstance(before, dict) or not isinstance(after, dict) or set(before) != set(after):
            return False
        if type(before.get("schema")) is not int or before["schema"] not in (1, 2, 3):
            return False
        if any(encoded(before[k]) != encoded(after[k]) for k in before if k not in entry_keys):
            return False
        lists = [(before.get(key), after.get(key)) for key in entry_keys]
    else:
        lists = [(before, after)]
    removed = 0
    for old, new in lists:
        if not isinstance(old, list) or not isinstance(new, list):
            return False
        remaining = iter(old)
        for entry in new:
            if not any(encoded(entry) == encoded(original) for original in remaining):
                return False
        removed += len(old) - len(new)
    return removed > 0


def review_required(changed_paths):
    """Load the pre-landing review patterns from the trusted checkout."""
    source = Path(__file__).resolve().parents[2] / "ci/review_required_paths.txt"
    patterns = [line.partition("#")[0].strip() for line in source.read_text(encoding="utf-8").splitlines()]
    if not any(patterns):
        raise ValueError("Review-required path list is empty")
    return any(fnmatchcase(path, pattern) for path in changed_paths for pattern in patterns if pattern)


def protected(path):
    return (path in {b".github", b"ci", b"tests/quarantine.json"}
            or path.startswith((b".github/", b"ci/")))


def codeowner_rules(text):
    return [line.split() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def owned(path, rules):
    """Match anchored CODEOWNERS paths, directory prefixes and * within a name.

    The last matching rule wins, as on GitHub; an ownerless rule un-owns its paths.
    """
    parent, _, name = path.rpartition("/")
    match = False
    for pattern, *owners in rules:
        anchored = pattern[1:]
        directory, _, glob = anchored.rpartition("/")
        if (not pattern.startswith("/") or not anchored
                or any("#" in owner for owner in owners)
                or any(mark in pattern for mark in ("#", "[", "]", "!", "\\", "**", "?"))
                or "*" in directory or any(part in {"", ".", ".."} for part in anchored.removesuffix("/").split("/"))):
            raise ValueError("Unsupported CODEOWNERS rule")
        if pattern.endswith("/"):
            if path.startswith(anchored):
                match = owners == [CODEOWNER_TEAM]
        elif parent == directory and fnmatchcase(name, glob):
            match = owners == [CODEOWNER_TEAM]
    return match


def paths(raw):
    if raw and not raw.endswith(b"\0"):
        raise ValueError("Incomplete path inventory")
    return raw[:-1].split(b"\0") if raw else []


def assert_public_tree(candidate):
    for raw in paths(git('ls-tree','-r','--name-only','-z',candidate)):
        path=raw.decode('utf-8')
        if private_agent_path(path):
            raise ValueError(path+': private agent instructions/state are forbidden')
        if path.casefold().split('/')[-1].endswith('ignore') and private_ignore_content(path,read_file(candidate,path)):
            raise ValueError(path+': private agent exclusions belong in local .git/info/exclude')


def python_controls(raw):
    if raw is None:
        return []
    parsed = ast.parse(raw)
    controls = []
    for node in ast.walk(parsed):
        hook = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name.startswith("pytest_") or any("hookimpl" in ast.unparse(d)
                or any(isinstance(n, ast.keyword) and n.arg == "specname" and isinstance(n.value, ast.Constant)
                       and isinstance(n.value.value, str) and n.value.value.startswith("pytest_") for n in ast.walk(d))
                for d in node.decorator_list))
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else []
        registration = any(isinstance(t, ast.Name) and t.id.startswith("pytest_")
                           or isinstance(t, ast.Attribute) and t.attr.startswith("pytest_")
                           or isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                           and isinstance(t.slice.value, str) and t.slice.value.startswith("pytest_")
                           for t in targets)
        if isinstance(node, ast.Call):
            registration |= (isinstance(node.func, ast.Attribute) and node.func.attr == "register"
                             and any(isinstance(n, ast.Attribute) and n.attr == "pluginmanager"
                                     or isinstance(n, ast.Name) and n.id == "pluginmanager" for n in ast.walk(node.func.value)))
            registration |= (len(node.args) > 1 and (isinstance(node.func, ast.Name) and node.func.id == "setattr"
                             or isinstance(node.func, ast.Attribute) and node.func.attr == "setattr")
                             and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
                             and node.args[1].value.startswith("pytest_"))
        if hook or registration:
            controls.append(ast.dump(node, include_attributes=False))
    return sorted(controls)


def mechanisms(raw, conftest=False):
    if raw is None:
        return []
    parsed = ast.parse(raw)
    def literal(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return literal(node.left) + literal(node.right)
        return ""
    aliases = {"pluginmanager", "reporter", "terminalreporter", "terminal"}
    for node in ast.walk(parsed):
        if isinstance(node, ast.Assign) and any(isinstance(n, ast.Attribute) and n.attr in aliases for n in ast.walk(node.value)):
            aliases.update(t.id for t in node.targets if isinstance(t, ast.Name))
    controls = []
    for node in ast.walk(parsed):
        text = ast.unparse(node)
        dynamic = isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Call))
        if dynamic and (any("pytest_" in literal(n) or literal(n) in {"PYTEST_PLUGINS", "PYTEST_ADDOPTS"} for n in ast.walk(node))
                and any(word in text for word in ("setattr", "globals", "locals", "update", "environ"))
                or any(isinstance(n, ast.Attribute) and n.attr in {"register", "import_plugin"}
                       and (any(a in ast.unparse(n.value) for a in aliases) or conftest) for n in ast.walk(node))
                or conftest and any(isinstance(n, ast.Attribute) and n.attr in aliases | {"outcome", "_tw", "stats", "_node_reporters"}
                                    for n in ast.walk(node))):
            controls.append(ast.dump(node, include_attributes=False))
    return sorted(controls)


def reporter_controls(raw):
    if raw is None:
        return []
    parsed=ast.parse(raw)
    aliases={"TestReport","LogXML","_NodeReporter","TerminalReporter","TerminalWriter"}
    reporter_module=False
    for node in ast.walk(parsed):
        if isinstance(node,ast.Import):
            for alias in node.names:
                if alias.name.startswith(("_pytest.reports","_pytest.junitxml","_pytest.terminal","_pytest._io")):
                    aliases.add(alias.asname or alias.name.split('.')[0]); reporter_module=True
        elif isinstance(node,ast.ImportFrom) and ((node.module or '').startswith(("_pytest.reports","_pytest.junitxml","_pytest.terminal","_pytest._io"))
                or node.module=="pytest" and any(a.name in {"TestReport","CollectReport"} for a in node.names)):
            aliases.update(alias.asname or alias.name for alias in node.names);reporter_module=True
    for node in ast.walk(parsed):
        if isinstance(node,ast.Assign) and any(isinstance(n,ast.Name) and n.id in aliases or isinstance(n,ast.Attribute) and n.attr in aliases for n in ast.walk(node.value)):
            aliases.update(t.id for t in node.targets if isinstance(t,ast.Name))
    controls=[]
    for node in ast.walk(parsed):
        targets=node.targets if isinstance(node,ast.Assign) else [node.target] if isinstance(node,(ast.AnnAssign,ast.AugAssign)) else []
        mutates_alias=any(isinstance(t,(ast.Attribute,ast.Subscript)) and any(
            isinstance(n,ast.Name) and n.id in aliases or isinstance(n,ast.Attribute) and n.attr in aliases for n in ast.walk(t)) for t in targets)
        mutates_result=reporter_module and any(isinstance(n,ast.Attribute) and n.attr in {"outcome","longrepr","wasxfail","sections","stats","_tw"} for t in targets for n in ast.walk(t))
        patch_call=isinstance(node,ast.Call) and ("setattr" in ast.unparse(node.func) or "patch" in ast.unparse(node.func)) and any(
            isinstance(n,ast.Name) and n.id in aliases or isinstance(n,ast.Constant) and isinstance(n.value,str)
            and any(alias in n.value for alias in aliases) for n in ast.walk(node))
        if mutates_alias or mutates_result or patch_call:
            controls.append(ast.dump(node,include_attributes=False))
    return sorted(controls)


def import_paths(path, raw, tracked):
    if raw is None or not path.endswith(".py"):
        return set()
    modules = set()
    package = path.removesuffix(".py").split("/")[:-1]
    for node in ast.walk(ast.parse(raw)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = package[:len(package) - node.level + 1] if node.level else []
            prefix += node.module.split(".") if node.module else []
            modules.add(".".join(prefix))
            modules.update(".".join(prefix + [alias.name]) for alias in node.names)
    found = set()
    for module in modules:
        parts = module.split(".")
        for index in range(1, len(parts) + 1):
            stem = "/".join(parts[:index])
            found.update(p for p in (stem + ".py", stem + "/__init__.py") if p in tracked)
    return found


def run_commands(line, path):
    tokens = shlex.shlex(EXPRESSION.sub("${WORKFLOW_EXPRESSION}", line), posix=True, punctuation_chars=True)
    tokens.whitespace_split = True
    try:
        words = list(tokens)
    except ValueError as error:
        raise ValueError(f"Cannot parse run step in {path}: {error}; simplify the quoting") from None
    command = []
    for token in words:
        if token and all(char in SHELL_SEPARATORS for char in token):
            if command:
                yield command
                command = []
        else:
            command.append(token)
    if command:
        yield command


def python_arguments(arguments):
    options = iter(arguments)
    for argument in options:
        if argument == "--":
            return "script", next(options, "")
        if argument in {"-W", "-X", "--check-hash-based-pycs"}:
            next(options, None)
        elif mode := PYTHON_MODE.fullmatch(argument):
            return mode[1], mode[2] or next(options, "")
        elif argument == "-" or not argument.startswith("-"):
            return "script", argument
    return "script", ""


def script_arguments(run, path, tracked):
    lines = iter(run.splitlines())
    for line in lines:
        for command in run_commands(line, path):
            names = [word.rsplit("/", 1)[-1] for word in command]
            for index, word in enumerate(command):
                name = names[index]
                arguments = command[index + 1:]
                following = arguments[0] if arguments else ""
                if word.startswith("./"):
                    yield word
                module = name
                if word == "-m" and MODULE.fullmatch(following):
                    if following not in PYTHON_MODULES:
                        raise ValueError("Workflow python -m module is outside the allowed list")
                    module = following
                if word == "-p" and MODULE.fullmatch(following) and names[0] != "mkdir":
                    yield Unresolved("plugin option -p " + following)
                is_python = PYTHON.fullmatch(name)
                if word.startswith("$") and all(
                        prefix in SHELL_PREFIXES or SHELL_ASSIGNMENT.match(prefix) for prefix in command[:index]):
                    mode, operand = python_arguments(arguments)
                    if mode in {"m", "c"} or operand == "-" or operand.endswith(SCRIPT_SUFFIXES) or (
                            operand and (operand.startswith("./") or operand in tracked or "/" in operand)):
                        yield Unresolved("nonliteral interpreter " + word)
                        is_python = True
                if is_python:
                    mode, operand = python_arguments(arguments)
                    if mode == "m":
                        if operand not in PYTHON_MODULES:
                            raise ValueError("Workflow python -m module is outside the allowed list")
                        module = operand
                    elif mode == "c" or operand == "-":
                        yield Unresolved("inline Python (-c or stdin)")
                        if operand == "-" and "<<'PY'" in line:
                            for body in lines:
                                if body == "PY":
                                    break
                    else:
                        yield operand
                if module == "pytest" and any(arg.startswith("-p") for arg in arguments):
                    yield Unresolved("pytest plugin option")
                if PIP.fullmatch(module) and "install" in arguments and any(
                        arg.startswith("-e") or arg.split("=", 1)[0] == "--editable" for arg in arguments):
                    yield Unresolved("editable pip install")
                if name in {"bash", "sh", "node"}:
                    options = iter(arguments)
                    for option in options:
                        if not option.startswith("-"):
                            yield option
                            break
                        if name != "node" and re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", option):
                            nested = next(options, None)
                            if nested is None:
                                raise ValueError(f"Cannot parse run step in {path}: shell -c needs a command; simplify the quoting")
                            yield from script_arguments(nested, path, tracked)
                            break
                if index == 0 and "/" in word and word in tracked:
                    yield word


def workflow_path(path):
    return path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml"))


def control_inventory(source):
    from workflow_privacy import check, load

    tracked = {p.decode() for p in paths(git("ls-tree", "-r", "--name-only", "-z", source))}
    read = {}
    def content(path):
        if path not in read:
            read[path] = read_file(source, path)
        return read[path]
    literal = re.compile(r"(?:\.github/scripts|scripts|ci)/[A-Za-z0-9_./-]+\.(?:py|sh|mjs|cjs|js|ts)\b")
    helpers, unresolved = set(), set()
    pending_workflows = sorted(p for p in tracked if workflow_path(p))
    visited = set()
    while pending_workflows:
        path = pending_workflows.pop()
        if path in visited:
            continue
        visited.add(path)
        text = content(path).decode()
        helpers.update(literal.findall(text))
        is_action = not workflow_path(path)
        document = check(text) if is_action else load(text)
        steps = ([("action", step) for step in document.get("runs", {}).get("steps", [])] if is_action else
                 [(name, step) for name, job in document.get("jobs", {}).items() for step in job.get("steps", [])])
        for index, (job_name, step) in enumerate(steps):
            action = step.get("uses", "")
            if action.startswith("./"):
                directory = action.removeprefix("./").rstrip("/")
                manifests = {directory + "/action.yml", directory + "/action.yaml"} & tracked
                if len(manifests) != 1:
                    raise ValueError("Local action manifest is missing or ambiguous")
                helpers.update(manifests)
                pending_workflows.extend(manifests)
            body = step.get("run", "")
            run = body.replace("\\\n", "")
            for prefix in (".trusted-controls", ".trusted-verdict"):
                run = run.replace(f"$GITHUB_WORKSPACE/{prefix}/", "")
            if is_action:
                directory = path.rsplit("/", 1)[0]
                for variable in ("${GITHUB_ACTION_PATH}", "$GITHUB_ACTION_PATH", "${{ github.action_path }}"):
                    run = run.replace(variable + "/", directory + "/")
            for argument in script_arguments(run, path, tracked):
                if not argument:
                    continue
                if isinstance(argument, Unresolved):
                    unresolved.add((path, str(job_name), str(step.get("id") or step.get("name") or f"step {index + 1}"),
                                    hashlib.sha256(body.encode()).hexdigest(), str(argument)))
                    continue
                if "$" in argument:
                    raise ValueError("Trusted workflow has a nonliteral helper; audit the inventory parser")
                relative = argument.removeprefix("./")
                matches = {relative} if relative in tracked else set()
                if argument.endswith(SCRIPT_SUFFIXES) and not matches:
                    matches = {p for p in tracked if argument.endswith("/" + p)}
                    if len(matches) != 1:
                        raise ValueError("Trusted workflow helper is missing or ambiguous")
                helpers.update(matches)
    direct = set(helpers)
    pending = [(p, True) for p in helpers] + [(p, False) for p in {"conftest.py", "tests/conftest.py", "tests/quarantine.py"} & tracked]
    seen, products = set(), set()
    while pending:
        path, controls = pending.pop()
        if (path, controls) in seen:
            continue
        if path not in tracked:
            raise ValueError("Trusted workflow names a missing literal helper")
        seen.add((path, controls))
        raw = content(path)
        imports = import_paths(path, raw, tracked)
        if path.endswith(".py"):
            imports |= set(literal.findall(raw.decode())) & tracked
        for imported in imports:
            control_import = controls and imported.startswith(("scripts/", ".github/", "ci/"))
            if control_import:
                helpers.add(imported)
            else:
                products.add(imported)
            pending.append((imported, control_import))
    return {"direct_helpers": direct, "helpers": helpers, "product_imports": products - helpers,
            "unresolved": [dict(zip(UNRESOLVED_KEYS, entry)) for entry in sorted(unresolved)]}


def mark_new(candidate, base):
    """Mark each unresolved entry the base inventory lacks as new; new entries come first."""
    known = {tuple(entry[key] for key in UNRESOLVED_KEYS) for entry in base}
    marked = [{**entry, "new": tuple(entry[key] for key in UNRESOLVED_KEYS) not in known} for entry in candidate]
    return sorted(marked, key=lambda entry: not entry["new"])


def tier(path, before, after, inventory):
    content_enabled=hardening.load()['content_classifier']
    if after is not None and private_agent_path(path):
        raise ValueError(path+': private agent instructions/state are forbidden')
    if private_ignore_content(path,after):
        raise ValueError(path+': private agent exclusions belong in local .git/info/exclude')
    if content_enabled and path.endswith('.py') and after is not None:
        try:ast.parse(after,filename=path)
        except (SyntaxError,ValueError,UnicodeDecodeError) as error:
            raise ValueError(path+': syntax error'+(' at line '+str(error.lineno) if isinstance(error,SyntaxError) else '')) from None
    if protected(path.encode()) or path == "tests/quarantine.py" or path in inventory["helpers"]:
        return 1, "CI policy, allowance or mechanically derived helper"
    name = path.rsplit("/", 1)[-1]
    if after is not None and (name in {"sitecustomize.py", "usercustomize.py"} or name.endswith(".pth")
            or any(part in {"sitecustomize", "usercustomize"} for part in path.split("/")[:-1])):
        return 1, "Python startup mechanism"
    if before is None and (name == "conftest.py" or path.endswith(".py") and "plugin" in name.lower()):
        return 1, "New conftest/plugin file"
    if name in {"pytest.toml", ".pytest.toml", "pytest.ini", ".pytest.ini", "setup.cfg", "tox.ini", ".npmrc", "alembic.ini"} or re.fullmatch(
            r"(?:tsconfig|jsconfig)(?:\.[^.]+)*\.json|(?:vite|webpack|babel)\.config\.[^.]+|\.babelrc(?:\.[^.]+)?", name):
        return 1, "Test/compiler/tool configuration"
    if path.endswith("pyproject.toml"):
        if not content_enabled:return 1,'Path-based test/compiler configuration'
        def pytest_config(raw):
            data = tomllib.loads((raw or b"").decode())
            return {"tool": data.get("tool", {}).get("pytest"),
                    "plugins": data.get("project", {}).get("entry-points", {}).get("pytest11")}
        if pytest_config(before) != pytest_config(after):
            return 1, "Pytest TOML configuration or plugin registration"
    if path.endswith('package.json') and not content_enabled:return 1,'Path-based package scripts configuration'
    if path.endswith("package.json") and (decode(before or b"{}").get("scripts") != decode(after or b"{}").get("scripts")):
        return 1, "Package scripts"
    if path == "requirements/ci-runtimes.json":
        return 1, "Executable runtime pins"
    try:
        before_text, after_text = ((raw or b"").decode("utf-8") for raw in (before, after))
    except UnicodeDecodeError:
        return 2, "changes what the checks run on"
    if "\x00" in before_text + after_text:
        return 2, "changes what the checks run on"
    added = "\n".join(line[1:] for line in difflib.unified_diff(before_text.splitlines(), after_text.splitlines())
                       if line.startswith("+") and not line.startswith("+++"))
    if content_enabled and re.search(r"PYTEST_(?:PLUGINS|ADDOPTS)|pytest11|pluginmanager\.(?:register|import_plugin)", added):
        return 1, "Pytest registration/configuration mechanism"
    if content_enabled and path.endswith(".py"):
        if (python_controls(before) != python_controls(after)
                or mechanisms(before, name == "conftest.py") != mechanisms(after, name == "conftest.py")
                or reporter_controls(before) != reporter_controls(after)):
            return 1, "Added/changed pytest hook or plugin registration, regardless of path"
    if (name == "conftest.py" or path.startswith(("tests/", "test_support/"))
            or path in inventory["product_imports"] or name.endswith((".lock", "-lock.json"))
            or path.startswith("requirements/") or name in {"package.json", "pyproject.toml"}):
        return 2, "changes what the checks run on"
    return 0, "Ordinary candidate code/data"


def findings_report(branch, base, head, candidate_tree, rows, source, candidate_sha=None, unresolved=()):
    report = {"schema": 2, "repository_id": REPOSITORY, "branch": branch, "target_sha": base,
              "head_sha": head, "candidate_tree": candidate_tree, "candidate_sha": candidate_sha, "trusted_source_sha": source,
              "tier1": [r for r in rows if r["tier"] == 1], "tier2": [r for r in rows if r["tier"] == 2]}
    report["label_request"] = "changes what the checks run on" if report["tier2"] else None
    report["control_label_request"] = "changes CI controls" if report["tier1"] else None
    # The owned workflow's reviewer must look at every form the inventory could not resolve.
    report["unresolved_executable_references"] = list(unresolved)
    destination = Path(".run/policy/findings.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write("### CI control changes\n\n")
            for row in rows:
                if row["tier"]:
                    summary.write("- Tier " + str(row["tier"]) + ": `" + json.dumps(row["path"]).replace("`", "\\u0060") + "` — " + row["reason"] + "\n")
            if not any(r["tier"] for r in rows):
                summary.write("No Tier 1 or Tier 2 changes.\n")
            for item in report["unresolved_executable_references"]:
                where = json.dumps(item["workflow"] + " / " + item["job"] + " / " + item["step"]).replace("`", "\\u0060")
                summary.write("- " + ("NEW " if item.get("new") else "") + "Unresolved executable reference in `"
                              + where + "`: " + json.dumps(item["reference"]).replace("`", "\\u0060") + "\n")
    return report


def migration_floor(before, after):
    if any(after.get(path) != blob for path, blob in before.items()):
        raise ValueError("An existing target migration was changed, renamed or removed")
    pattern = r"migrations/versions/([0-9]+)_[A-Za-z0-9_]+\.py"
    floor = max((int(m.group(1)) for p in before if (m := re.fullmatch(pattern, p))), default=0)
    for path in after.keys() - before.keys():
        match = re.fullmatch(pattern, path)
        if match is None or int(match.group(1)) <= floor:
            raise ValueError("New migrations must be above the target branch's append-only floor")


def migrations(revision):
    rows = paths(git("ls-tree", "-r", "-z", revision, "--", "migrations/versions/"))
    result = {}
    for row in rows:
        metadata, _, name = row.partition(b"\t")
        if not metadata.startswith((b"100644 blob ", b"100755 blob ")):
            raise ValueError("Migration must be a regular file")
        result[name.decode()] = metadata.decode()
    return result


def accepted_dev_commit(candidate_tree, dev_tip):
    # Tree equality survives squash on dev and the single release squash on main.
    for line in git("log", "--first-parent", "--format=%H:%T", dev_tip).decode().splitlines():
        revision, actual_tree = line.split(":")
        if actual_tree == candidate_tree:
            if decode(read_file(revision, MARKER) or b"null") != CONTRACT:
                raise ValueError("Dev tree predates the required CI contract")
            return commit(revision)
    raise ValueError("Release head tree is not an accepted tree on dev")


def default_branch(data):
    # The integration branch is the repository default from the trusted event payload, never PR content.
    branch = (data.get("repository") or {}).get("default_branch")
    if branch not in {"dev", "main"}:
        raise ValueError("Unexpected repository or default branch")
    return branch


def working_branches(default):
    # shortcut: main is the release branch and preview a promotion branch only while the default is dev;
    # once the default is main both paths are unreachable, and the next CI change deletes them.
    return {default} if default == "main" else {default, "main", "preview"}


def event():
    data = decode(Path(os.environ["GITHUB_EVENT_PATH"]).read_bytes())
    repo = data["repository"]
    if repo["id"] != REPOSITORY or repo["full_name"] != "ShiftEZ-ai/shiftez-clinical":
        raise ValueError("Unexpected repository or default branch")
    default_branch(data)
    return data


def require_branch_reviews(branch):
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if not token:
        raise ValueError("Branch review rules require a read-only GitHub token")
    repository = os.getenv("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository, re.ASCII):
        raise ValueError("Branch review rules require a valid repository identity")
    try:
        request = Request(f"https://api.github.com/repos/{repository}/rules/branches/{branch}", headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        with urlopen(request, timeout=10) as response:
            rules = decode(response.read())
    except (OSError, ValueError, HTTPException):
        raise ValueError("Could not read active branch review rules") from None
    if not isinstance(rules, list) or any(
        not isinstance(rule, dict) or not isinstance(rule.get("type"), str) for rule in rules
    ):
        raise ValueError("Unexpected active branch rules response")
    reviews = [rule.get("parameters") for rule in rules if rule["type"] == "pull_request"]
    if any(not isinstance(review, dict) or any(type(review.get(flag)) is not bool for flag in REVIEW_FLAGS)
           for review in reviews):
        raise ValueError("Unexpected active branch review rule parameters")
    if not any(all(review[flag] for flag in REVIEW_FLAGS) for review in reviews):
        raise ValueError("Active branch rules must require code-owner review and stale-review dismissal")


def fetch(ref, expected=None, *, allow_missing=False):
    try:
        git("fetch", "--no-tags", "origin", ref)
    except subprocess.CalledProcessError:
        if allow_missing:
            try:
                git("ls-remote", "--exit-code", "--refs", "origin", ref)
            except subprocess.CalledProcessError as probe:
                if probe.returncode == 2:
                    return None
                raise
        raise
    actual = commit(git("rev-parse", "FETCH_HEAD").decode().strip())
    if expected is not None and actual != expected:
        raise ValueError("Ref advanced; require a fresh event")
    return actual


def merge_tree(base, head):
    # Writes Git tree objects only in the trusted job; never relies on asynchronous PR mergeability.
    output = git("merge-tree", "--write-tree", base, head).decode().splitlines()
    if not output:
        raise ValueError("Merge tree is missing")
    return commit(output[0])


def bootstrap(data, kind, base):
    if os.getenv("TRUSTED_BOOTSTRAP") != "true":
        return False
    pr = data.get("pull_request", {})
    if (kind != "pull_request" or pr.get("base", {}).get("ref") != default_branch(data)
            or pr["base"]["repo"]["id"] != REPOSITORY or pr["head"]["repo"]["id"] != REPOSITORY
            or os.environ["GITHUB_RUN_ATTEMPT"] != "1"
            or git("rev-list", "--parents", "-n", "1", base).decode().split() != [base]
            or read_file(base, ".github/scripts/trusted_ci.py") is not None
            or read_file(base, KNOWN) is not None
            or git("rev-parse", "HEAD").decode().strip() != commit(pr["head"]["sha"])):
        raise ValueError("Bootstrap is restricted to the initial same-repository dev PR")
    print("BOOTSTRAP: initial controls; hosted evidence is the measured base")
    return True


def guard():
    data = event()
    default = default_branch(data)
    targets = working_branches(default) - {"preview"}
    kind = os.environ["GITHUB_EVENT_NAME"]
    if kind == "pull_request":
        pr = data["pull_request"]
        branch = pr["base"]["ref"]
        if branch not in targets:
            print("Policy is not required for this PR target")
            return
        if pr["base"]["repo"]["id"] != REPOSITORY or type(data["number"]) is not int or data["number"] < 1:
            raise ValueError("Unexpected PR identity")
        base, head = commit(pr["base"]["sha"]), commit(pr["head"]["sha"])
        fetch(base, base)
        fetch("refs/pull/" + str(data["number"]) + "/head", head)
        candidate = merge_tree(base, head)
        candidate_sha = None
        comparison = base + "..." + head
        if branch == default and pr["head"]["repo"]["id"] == REPOSITORY and pr["head"]["ref"] == "preview":
            raise ValueError("Preview is never promoted into dev")
    elif kind == "merge_group":
        group = data["merge_group"]
        if group["base_ref"] not in {"refs/heads/" + name for name in targets}:
            raise ValueError("Unexpected queue target")
        branch = group["base_ref"].removeprefix("refs/heads/")
        base, head = commit(group["base_sha"]), commit(group["head_sha"])
        fetch(base, base)
        candidate_sha = fetch(head, head)
        candidate = tree(candidate_sha)
        comparison = base + ".." + head
    else:
        raise ValueError("Unsupported policy event")
    import workflow_privacy

    assert_public_tree(candidate)
    for raw_path in paths(git("ls-tree", "-r", "--name-only", "-z", candidate, "--", ".github/workflows/")):
        path = raw_path.decode("utf-8")
        if workflow_path(path):
            workflow_privacy.check(read_file(candidate, path).decode("utf-8"))
    migration_floor(migrations(base), migrations(candidate))
    initial = bootstrap(data, kind, base)
    if branch != default:  # the release branch main, only while the default is dev
        dev_tip = fetch("refs/heads/" + default)
        source = accepted_dev_commit(candidate, dev_tip)
        if candidate != tree(head):
            raise ValueError("Main would add content beyond the accepted dev tree")
        # A promotion uses accepted dev's list/marker, never main's older allowances.
        policy_base = source
        source_policy = commit(git("rev-parse", "HEAD").decode().strip())
        inventory = control_inventory(source_policy)
        rows = []
        rules = codeowner_rules((read_file(base, ".github/CODEOWNERS") or b"").decode("utf-8"))
        owned_controls = False
        changed = [p.decode("utf-8") for p in paths(git(
            "diff", "--name-only", "--no-ext-diff", "--no-textconv", "--no-renames", "-z", base, candidate, "--"))]
        if any(path.startswith((".github/workflows/", ".github/actions/")) for path in changed):
            inventory["helpers"] = inventory["helpers"] | control_inventory(candidate)["helpers"]
        for path in changed:
            level, reason = tier(path, read_file(base, path), read_file(candidate, path), inventory)
            if level == 1:
                owned_controls |= owned(path, rules)
                level, reason = 0, "Accepted dev-tree promotion; dev path rejection does not apply"
            rows.append({"path": path, "tier": level, "reason": reason})
        findings_report(branch, base, head, candidate, rows, source_policy, candidate_sha)
        if owned_controls:
            require_branch_reviews(branch)
    else:
        policy_base = base
        changed = [p.decode("utf-8") for p in paths(git(
            "diff", "--name-only", "--no-ext-diff", "--no-textconv", "--no-renames", "-z", comparison, "--"))]
        source = commit(git("rev-parse", "HEAD").decode().strip())
        inventory = control_inventory(source)
        unowned, unresolved = set(), []
        if not initial:
            rules = codeowner_rules((read_file(base, ".github/CODEOWNERS") or b"").decode("utf-8"))
            unowned = {path for path in changed if not owned(path, rules)}
        if (any(path.startswith((".github/workflows/", ".github/actions/")) for path in changed)
                or unowned and inventory["helpers"].intersection(changed)):
            # A changed workflow, action or helper can make another changed path a control.
            candidate_inventory = control_inventory(candidate)
            inventory["helpers"] = inventory["helpers"] | candidate_inventory["helpers"]
            unresolved = mark_new(candidate_inventory.get("unresolved", []),
                                  control_inventory(base).get("unresolved", []))
        rows = []
        for path in changed:
            old, new = read_file(base, path), read_file(candidate, path)
            if path == KNOWN and removal_only(old, new):
                level, reason = 0, "Known-failures entry removal only"
            elif not initial and path == QUARANTINE_RETIRED:
                quarantine_retired_change(old, new, read_file(base, QUARANTINE), read_file(candidate, QUARANTINE))
                level, reason = 0, "Quarantine retirement or compaction after registry removal"
            elif not initial and path == NODE_LEDGER:
                if not hardening.load()["node_census"] or node_census.ledger_additions(old, new) is None:
                    raise ValueError(NODE_LEDGER + " may only add removed nodes for census verification")
                level, reason = 2, "Node-ledger additions only; CI must prove each node disappears in this candidate"
            else:
                if not initial and path == QUARANTINE:
                    _, retired = retired_at_revision(base)
                    quarantine_change(old, new, read_file(base, KNOWN), read_file(candidate, KNOWN), retired=retired)
                if not initial and path in SHRINK_ONLY and not removal_only(old, new, SHRINK_ONLY[path]):
                    raise ValueError(path + " may only remove existing entries")
                level, reason = tier(path, old, new, inventory)
            rows.append({"path": path, "tier": level, "reason": reason})
        report = findings_report(branch, base, head, candidate, rows, source, candidate_sha, unresolved)
        if not initial:
            for row in report["tier1"]:
                path = row["path"]
                if path == KNOWN:
                    raise ValueError("Known-failures changes must only remove existing entries")
                if path in unowned:
                    if tier(path, b"", None, inventory)[0] == 1:
                        raise ValueError("Add a CODEOWNERS rule for " + path)
                    raise ValueError("Move the Tier 1 mechanism in " + path + " into a CODEOWNERS-owned file")
            if report["tier1"]:
                require_branch_reviews(branch)
        preview = fetch("refs/heads/preview", allow_missing=True)
        if preview is not None:
            preview_merges = set(git("rev-list", "--first-parent", "--merges", preview, "--not", base).splitlines())
            incoming = set(git("rev-list", head, "--not", base).splitlines())
            if preview_merges & incoming:
                raise ValueError("Preview merge history may not enter dev")
    if initial:
        policy_base = head
    receipts = {}
    for path, required in LISTS.items():
        if path == QUARANTINE_RETIRED:
            raw, _ = retired_at_revision(policy_base)
        else:
            raw = read_file(policy_base, path)
            if raw is None and required:
                raise ValueError("Required policy list is absent")
            if raw is not None:
                decode(raw)
        receipts[path] = "ABSENT" if raw is None else hashlib.sha256(raw).hexdigest()
    print(json.dumps({"branch": branch, "target_sha": base, "policy_input_sha": policy_base,
                      "head_sha": head, "candidate_sha": candidate_sha, "candidate_tree": candidate, "lists": receipts,
                      "workflow_source_sha": os.getenv("POLICY_WORKFLOW_SHA", "UNKNOWN")}, sort_keys=True))
    print("Working policy: PASS")


def ci_context():
    data = event()
    default = default_branch(data)
    kind = os.environ["GITHUB_EVENT_NAME"]
    if kind == "pull_request":
        branch, base = data["pull_request"]["base"]["ref"], commit(data["pull_request"]["base"]["sha"])
    elif kind == "merge_group":
        branch = data["merge_group"]["base_ref"].removeprefix("refs/heads/")
        base = commit(data["merge_group"]["base_sha"])
    elif kind == "schedule":
        branch, base = default, commit(git("rev-parse", "HEAD").decode().strip())
    else:
        branch, base = os.environ["GITHUB_REF"].removeprefix("refs/heads/"), commit(git("rev-parse", "HEAD").decode().strip())
    if branch not in working_branches(default):
        raise ValueError("CI supports only the working branches")
    census_base = commit(data["before"]) if kind=="push" else base
    release = branch == "main" and default != "main"
    if release:
        base = accepted_dev_commit(tree("HEAD"), fetch("refs/heads/" + default))
    elif branch == "preview":
        base = fetch("refs/heads/" + default)
    elif kind == "push":
        base = commit(data["before"])
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write("census_base_sha=" + census_base + "\nbase_sha=" + base + "\nrelease=" + str(release).lower() + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("guard", "ci-context"))
    args = parser.parse_args()
    if args.command == "guard":
        guard()
    else:
        ci_context()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, SyntaxError, subprocess.CalledProcessError) as error:
        raise SystemExit("Working policy refused: " + (str(error) if isinstance(error, ValueError) else type(error).__name__)) from None
