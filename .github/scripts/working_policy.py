"""Trusted dev policy: branch roles, tree promotion, shrinking allowances and migration floors."""
import argparse
import ast
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from datetime import date, datetime, timezone
import node_census
import hardening

from agent_names import AGENT_FILES, AGENT_DIRECTORIES, AGENT_PREFIXES, private_agent_path, private_ignore_content

HOOKS = {
    "pytest_runtest_makereport", "pytest_report_teststatus", "pytest_collection_modifyitems",
    "pytest_deselected", "pytest_ignore_collect", "pytest_collect_file", "pytest_pycollect_makeitem",
    "pytest_runtest_protocol", "pytest_runtest_call", "pytest_configure", "pytest_addhooks",
    "pytest_terminal_summary", "pytest_sessionfinish", "pytest_unconfigure", "pytest_addoption",
}

REPOSITORY = 1459752401
KNOWN = "ci/known-failures.json"
MARKER = "ci/working-rules.json"
CONTRACT = {"schema": 4, "ci_result_contract": "failclosed-main-reviewed-errors-v32"}
LISTS = {
    KNOWN: False, MARKER: False,
    "ci/known-skips.json": False,
    "ci/failure-classifications.json": False,
    "ci/hardening.json": True,
    "ci/frozen-baseline-failures.json": False,
    "ci/root-run-evidence.json": False, "ci/removed-tests.json": False,
    "ci/hosted-env-exceptions.json": False,
    "ci/unstable-seeds.json": True, "ci/hosted-durations.json": True,
    "tests/quarantine.json": True,
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


def removal_only(before_raw, after_raw):
    if before_raw is None or after_raw is None:
        return False
    before, after = decode(before_raw), decode(after_raw)
    if not isinstance(before, dict) or not isinstance(after, dict) or set(before) != set(after):
        return False
    if type(before.get("schema")) is not int or before.get("schema") not in (1, 2, 3) or type(after.get("schema")) is not int or before.get("schema") != after.get("schema"):
        return False
    def encoded(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    if any(encoded(before[k]) != encoded(after[k]) for k in before if k not in {"fails", "unstable"}):
        return False
    removed = 0
    for key in ("fails", "unstable"):
        old, new = before.get(key), after.get(key)
        if not isinstance(old, list) or not isinstance(new, list):
            return False
        remaining = iter(old)
        for entry in new:
            if not any(encoded(entry) == encoded(original) for original in remaining):
                return False
        removed += len(old) - len(new)
    return removed > 0


def renewal_only(before_raw, after_raw, *, today=None):
    """Only expiry extensions qualify for the audited Tier-1 renewal route."""
    if before_raw is None or after_raw is None:return False
    before,after=decode(before_raw),decode(after_raw)
    if not isinstance(before,dict) or not isinstance(after,dict) or set(before)!=set(after):return False
    if any(before[k]!=after[k] for k in before if k!='unstable'):return False
    old,new=before.get('unstable'),after.get('unstable')
    if not isinstance(old,list) or not isinstance(new,list) or len(old)!=len(new):return False
    today=today or datetime.now(timezone.utc).date()
    changed=False
    for a,b in zip(old,new):
        if not isinstance(a,dict) or not isinstance(b,dict) or set(a)!=set(b):return False
        if any(a[k]!=b[k] for k in a if k!='expires_on'):return False
        if a==b:continue
        try:
            previous,current=date.fromisoformat(a['expires_on']),date.fromisoformat(b['expires_on'])
        except (KeyError,TypeError,ValueError):return False
        if previous>=current:return False
        changed=True
    return changed


def protected(path):
    return (path in {b".github", b"ci", b"tests/quarantine.json"}
            or path.startswith((b".github/", b"ci/")))


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


def control_inventory(source):
    tracked = {p.decode() for p in paths(git("ls-tree", "-r", "--name-only", "-z", source))}
    read = {}
    def content(path):
        if path not in read:
            read[path] = read_file(source, path)
        return read[path]
    literal = re.compile(r"(?:\.github/scripts|scripts|ci)/[A-Za-z0-9_./-]+\.(?:py|sh|mjs|cjs|js|ts)\b")
    helpers = set()
    for path in sorted(p for p in tracked if p.startswith(".github/workflows/") and p.endswith((".yml", ".yaml"))):
        text = content(path).decode().replace("$GITHUB_WORKSPACE/.trusted-controls/", "").replace("$GITHUB_WORKSPACE/.trusted-verdict/", "")
        helpers.update(literal.findall(text))
        for match in re.finditer(r'''\b(?:python(?:3(?:\.\d+)?)?|node|bash|sh)\s+(?:-(?:I|B|u)\s+)*("[^"]*"|'[^']*'|\S+)''', text):
            argument = match.group(1).strip("\"'")
            if argument.startswith("-"):
                continue
            if "$" in argument:
                raise ValueError("Trusted workflow has a nonliteral helper; audit the inventory parser")
            if argument.endswith((".py", ".sh", ".mjs", ".cjs", ".js", ".ts")):
                matches = {p for p in tracked if argument.removeprefix("./") == p or argument.endswith("/" + p)}
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
    return {"direct_helpers": direct, "helpers": helpers, "product_imports": products - helpers}


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


def findings_report(branch, base, head, candidate_tree, rows, source, candidate_sha=None):
    report = {"schema": 2, "repository_id": REPOSITORY, "branch": branch, "target_sha": base,
              "head_sha": head, "candidate_tree": candidate_tree, "candidate_sha": candidate_sha, "trusted_source_sha": source,
              "tier1": [r for r in rows if r["tier"] == 1], "tier2": [r for r in rows if r["tier"] == 2]}
    report["label_request"] = "changes what the checks run on" if report["tier2"] else None
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


def event():
    data = decode(Path(os.environ["GITHUB_EVENT_PATH"]).read_bytes())
    repo = data["repository"]
    if (repo["id"] != REPOSITORY or repo["full_name"] != "ShiftEZ-ai/shiftez-clinical"
            or repo["default_branch"] != "dev"):
        raise ValueError("Unexpected repository or default branch")
    return data


def fetch(ref, expected=None):
    git("fetch", "--no-tags", "origin", ref)
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
    if (kind != "pull_request" or pr.get("base", {}).get("ref") != "dev"
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
    kind = os.environ["GITHUB_EVENT_NAME"]
    if kind == "pull_request":
        pr = data["pull_request"]
        branch = pr["base"]["ref"]
        if branch not in {"dev", "main"}:
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
        if branch == "dev" and pr["head"]["repo"]["id"] == REPOSITORY and pr["head"]["ref"] == "preview":
            raise ValueError("Preview is never promoted into dev")
    elif kind == "merge_group":
        group = data["merge_group"]
        if group["base_ref"] not in {"refs/heads/dev", "refs/heads/main"}:
            raise ValueError("Unexpected queue target")
        branch = group["base_ref"].removeprefix("refs/heads/")
        base, head = commit(group["base_sha"]), commit(group["head_sha"])
        fetch(base, base)
        candidate_sha = fetch(head, head)
        candidate = tree(candidate_sha)
        comparison = base + ".." + head
    else:
        raise ValueError("Unsupported policy event")
    assert_public_tree(candidate)
    migration_floor(migrations(base), migrations(candidate))
    initial = bootstrap(data, kind, base)
    if branch == "main":
        dev_tip = fetch("refs/heads/dev")
        source = accepted_dev_commit(candidate, dev_tip)
        if candidate != tree(head):
            raise ValueError("Main would add content beyond the accepted dev tree")
        # A promotion uses accepted dev's list/marker, never main's older allowances.
        policy_base = source
        source_policy = commit(git("rev-parse", "HEAD").decode().strip())
        inventory = control_inventory(source_policy)
        rows = []
        for raw_path in paths(git("diff", "--name-only", "--no-ext-diff", "--no-textconv", "--no-renames", "-z", base, candidate, "--")):
            path = raw_path.decode("utf-8")
            level, reason = tier(path, read_file(base, path), read_file(candidate, path), inventory)
            if level == 1:
                level, reason = 0, "Accepted dev-tree promotion; dev path rejection does not apply"
            rows.append({"path": path, "tier": level, "reason": reason})
        findings_report(branch, base, head, candidate, rows, source_policy, candidate_sha)
    else:
        policy_base = base
        changed = paths(git("diff", "--name-only", "--no-ext-diff", "--no-textconv", "--no-renames", "-z", comparison, "--"))
        source = commit(git("rev-parse", "HEAD").decode().strip())
        inventory = control_inventory(source)
        rows = []
        for raw_path in changed:
            path = raw_path.decode("utf-8")
            old, new = read_file(base, path), read_file(candidate, path)
            if path == KNOWN and removal_only(old, new):
                level, reason = 0, "Known-failures entry removal only"
            elif hardening.load()['node_census'] and path == 'ci/removed-tests.json' and node_census.ledger_additions(old,new) is not None:
                level,reason=2,'Node-ledger additions only; CI must prove each node disappears in this candidate'
            else:
                level, reason = tier(path, old, new, inventory)
            rows.append({"path": path, "tier": level, "reason": reason})
        report = findings_report(branch, base, head, candidate, rows, source, candidate_sha)
        if report["tier1"] and not initial:
            raise ValueError("Tier 1 control changes require the audited maintenance route")
        preview = fetch("refs/heads/preview")
        preview_merges = set(git("rev-list", "--first-parent", "--merges", preview, "--not", base).splitlines())
        incoming = set(git("rev-list", head, "--not", base).splitlines())
        if preview_merges & incoming:
            raise ValueError("Preview merge history may not enter dev")
    if initial:
        policy_base = head
    receipts = {}
    for path, required in LISTS.items():
        raw = read_file(policy_base, path)
        if raw is None:
            if required:
                raise ValueError("Required policy list is absent")
            receipts[path] = "ABSENT"
        else:
            decode(raw)
            receipts[path] = hashlib.sha256(raw).hexdigest()
    print(json.dumps({"branch": branch, "target_sha": base, "policy_input_sha": policy_base,
                      "head_sha": head, "candidate_sha": candidate_sha, "candidate_tree": candidate, "lists": receipts,
                      "workflow_source_sha": os.getenv("POLICY_WORKFLOW_SHA", "UNKNOWN")}, sort_keys=True))
    print("Working policy: PASS")


def ci_context():
    data = event()
    kind = os.environ["GITHUB_EVENT_NAME"]
    if kind == "pull_request":
        branch, base = data["pull_request"]["base"]["ref"], commit(data["pull_request"]["base"]["sha"])
    elif kind == "merge_group":
        branch = data["merge_group"]["base_ref"].removeprefix("refs/heads/")
        base = commit(data["merge_group"]["base_sha"])
    elif kind == "schedule":
        branch, base = "dev", commit(git("rev-parse", "HEAD").decode().strip())
    else:
        branch, base = os.environ["GITHUB_REF"].removeprefix("refs/heads/"), commit(git("rev-parse", "HEAD").decode().strip())
    if branch not in {"dev", "preview", "main"}:
        raise ValueError("CI supports only the three working branches")
    census_base = commit(data["before"]) if kind=="push" else base
    if branch == "main":
        base = accepted_dev_commit(tree("HEAD"), fetch("refs/heads/dev"))
    elif branch == "preview":
        base = fetch("refs/heads/dev")
    elif kind == "push":
        base = commit(data["before"])
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write("census_base_sha=" + census_base + "\nbase_sha=" + base + "\nrelease=" + str(branch == "main").lower() + "\n")


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
