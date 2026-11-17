"""Affected local tests and CI planning (stdlib only; never collects or runs tests to select).

L2 PR interface v1 (all paths are repository-relative; include deleted paths):
    UNIT_WORKERS=2 BROWSER_WORKERS=2 python scripts/affected_tests.py --pr-affected-v1 [changed paths...]
    python scripts/affected_tests.py --shard unit 1/2 --only "<unit_tests output>"

v1 prints newline-delimited key=value records, ready for GITHUB_OUTPUT. unit_tests /
browser_tests contain space-separated pytest files or nodes; an empty value means
FULL ONLY when that suite has nonempty *_shards. [] shards means skip the suite.
*_shards are JSON 1-based indices; use each suite's OWN shard count with --shard.
selection_inputs.selected explicitly expands full suites, and full_suites identifies
them. full_fallback_reason explains conservative FULL decisions. shard_plan is JSON
{suite: [{index, targets, estimated_seconds}]}; selection_stats reports file coverage
and estimates. Estimates use durations.json and workers, exclude setup/queueing, and
are not a feedback-time guarantee. Defaults: two unit + two browser shards maximum.
No supplied paths is FULL (missing change discovery must never skip qualification).
The caller supplies the PR base...head diff, gates drafts and schedules both suites.
Legacy --ci / --pr-smoke / --smoke and merge-group early partitions stay compatible.

Local/FULL mapping: Python imports (absolute/relative, package initialization, test helpers, conftest
chains, pytest_plugins, literal dynamic imports) use a transitive reverse graph.
Data/fixture file readers seed that same graph. Unresolved loaders select more.
Migrations and contracts remain FULL: all database tests and generated.ts consumers
are included. Dependency/lock, shared configuration and selector changes are FULL.
Browser ownership combines transitive TS imports and test source references.
API dependencies flow through Python helpers, including the real live_server. Shared shell/CSS,
unknown page boundaries, parse errors and missing/deleted source fail closed.
PR feedback retains transitive unit owners until a shared root reaches over 40% of
unit files; those fan-outs use direct owners plus fixed smoke. Browser PR feedback
uses direct owners plus smoke. Merge groups use the same policy; FULL dev/nightly sweeps gate releases.
Unresolved unit ownership still selects the full unit suite. CI/tooling changes
select the existing CI/selector/retry contract tests. This is feedback selection,
not proof of complete affected coverage. Replay guards surviving unit/API regressions.

    python scripts/affected_tests.py                       # changes vs origin/dev (commits + working tree): print
    python scripts/affected_tests.py --run [pytest args]   # ... and run them (browser tests with -n 3)
    python scripts/affected_tests.py --smoke --run [pytest args]  # fixed smoke set, without change discovery
    python scripts/affected_tests.py --base origin/main    # another base
    python scripts/affected_tests.py --files a.py b.ts     # explicit paths
    python scripts/affected_tests.py --ci [files...]       # CI: affected + safety core (no files = everything)
    python scripts/affected_tests.py --pr-smoke [files...] # CI: guards plus changed/doc-linked test files
    python scripts/affected_tests.py --smoke               # CI: only SMOKE, one unit and one browser shard
    python scripts/affected_tests.py --shard browser 2/4 [--only "paths"]   # CI: one shard's test files
    python scripts/affected_tests.py --record junit*.xml   # refresh tests/durations.json from CI reports

Legacy local/--ci selections add SMOKE for code; --ci also retains SAFETY_CORE
and WORK_CORE. --pr-smoke is unchanged for old consumers and the early partition;
Native feedback uses --fast-ci; --full-ci is the capacity-aware complete sweep.
"""
import argparse
import ast
from functools import lru_cache
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))
DEFAULT_RUNNER = 'ubuntu-24.04'  # hosted only


def self_hosted_runner(raw):
    return raw
TESTS = ROOT / "tests"
DURATIONS = TESTS / "durations.json"
CORE = ("migrations/", "requirements/", "pyproject.toml", "alembic.ini", "package.json", "package-lock.json", ".github/workflows/",
        "infra/Dockerfile", ".github/scripts/pytest_ci.py", "scripts/affected_tests.py",
        "apps/__init__.py", "apps/api/__init__.py", "apps/api/main.py", "apps/api/models.py", "apps/api/db.py",
        "apps/api/schema.py", "apps/api/settings.py", "packages/contracts/", "scripts/manage.py", "scripts/build_web.py",
        "scripts/generate_contracts.py",
        "scripts/preflight.py", "scripts/preload_web.py")
SMOKE = {"unit": ["tests/test_api_lifecycle.py", "tests/test_security.py", "tests/test_domain.py",
                  "tests/test_timing_guard.py", "tests/test_cpu_budget.py", "tests/test_clock_guard.py",
                  "tests/test_postgres_markers.py",
                  "tests/test_quarantine_validity.py",
                  "tests/test_governance_guards.py", "tests/test_migration_ids.py", "tests/test_side_panel_kinds.py",
                  "tests/test_web_collection_ssot.py", "tests/test_web_error_reporting.py",
                  "tests/test_data_safety.py::test_no_migration_has_unreviewed_destructive_operations",
                  "tests/test_data_safety.py::test_grandfather_list_is_closed_and_honest",
                  "tests/test_data_safety.py::test_api_and_worker_never_migrate_seed_or_reset_on_start",
                  "tests/test_data_safety.py::test_hard_delete_paths_are_inventoried",
                  "tests/test_hosted_ci.py::test_runtime_hashes_are_checked_before_exporting_a_path"],
         "browser": ["tests/test_browser.py::test_every_permitted_surface_loads_without_javascript_errors"]}
# Fast non-browser web/client tests (~300, ~2 min at -n 4) run in every code selection: a wave merge skipped them and
# 9 reached the hub red . Globbed so new files join; part of SMOKE, so shared-root narrowing keeps them.
WEB_CLIENT_FAST = tuple(sorted(p.relative_to(ROOT).as_posix() for pattern in ("test_web_*.py", "test_client_*.py")
                               for p in TESTS.glob(pattern)))
SMOKE["unit"] = list(dict.fromkeys([*SMOKE["unit"], *WEB_CLIENT_FAST]))
# These tests compile the client with the pinned TypeScript (conftest client_modules); CI copies node_modules in.
NODE_TOOLCHAIN = "node_modules/typescript/bin/tsc"
WEB_BUILD = "apps/web/dist/index.html"
# planner_week.ts owns both the Week and Month item grids; schedule_item.ts and person_pill.ts draw their items. Keep
# these browser matrices on every PR/merge selection for those modules; route-name discovery alone missed them when
# their navigation was hidden behind shared helpers.
PLANNER_WEEK_SOURCES = ("apps/web/src/planner_week.ts", "apps/web/src/schedule_item.ts", "apps/web/src/person_pill.ts")
PLANNER_WEEK_BROWSER_GATES = (
    "tests/test_browser_item_properties.py",
    "tests/test_browser_schedule_views.py",
)
# Every Schedule layout drawn once (a draw-time error blanks the planner; ). Selected for any web client change.
WEB_BROWSER_SMOKE = ("tests/test_browser_schedule_smoke.py",)
SAFETY_CORE = (
    "tests/test_data_safety.py",
    "tests/test_migration_ids.py",
    "tests/test_postgres_markers.py",
    "tests/test_product_context_postgres.py",
    "tests/test_postgres_roles.py",
    "tests/test_api_lifecycle.py",
    "tests/test_security.py",
    "tests/test_security_review.py",
    "tests/test_governance_guards.py",
    "tests/test_developer_permissions.py",
    "tests/test_rule_safety_audit.py",
    "tests/test_legacy_safety_precedence.py",
    "tests/test_engine_restriction_boundaries.py",
    "tests/test_autoscheduler_audit_safety.py",
    "tests/test_autoscheduler_audit_supervision.py",
    "tests/test_supervision_pairing.py",
    "tests/test_rest_fixed_clinics.py",
    "tests/test_over_maximum_coverage.py",
    "tests/test_cp_sat_small_oracle.py",
    "tests/test_autoschedule_locked_sessions.py::test_locked_staffing_gap_never_bypasses_pooled_minimum_cover",
    "tests/test_cp_sat_generation.py::test_scoped_cover_and_overflow_use_distinct_configured_people",
    "tests/test_cp_sat_generation.py::test_diagnostic_proposal_exposes_gaps_without_relaxing_rest_or_quota_maximum",
    "tests/test_generation_department.py::test_scoped_coverage_does_not_trial_people_outside_required_scope",
    "tests/test_browser_operator_role_coverage.py::test_all_role_routes_forbidden_controls_and_common_help",
    "tests/test_browser_operator_role_coverage.py::test_every_role_acknowledges_own_inbox_item_and_reload_preserves_it",
    "tests/test_browser_governance.py::test_inactive_staff_keeps_personal_pages_without_forbidden_swap_background_reads",
)
WORK_CORE = (
    "tests/test_backend_scale.py::test_department_interactive_paths",
    "tests/test_planner_why.py::test_why_stays_fast_at_department_scale",
    "tests/test_source_parity_greedy_scale.py::test_incremental_people_trial_reuses_context_and_scans_only_the_candidates_duties",
    "tests/test_resolve_issues.py::test_best_replacements_for_500_issues_stay_fast_at_department_scale",
    "tests/test_scheduler_performance.py::test_date_set_day_index_keeps_department_year_checks_bounded",
    "tests/test_command_write_timeouts.py::test_command_advisory_lock_times_out_without_receipt_and_reads_stay_usable",
)
SHARD_SECONDS = 300  # target ~5 minutes of recorded test time per worker in a pull-request shard
SERIAL_FILES = (
    "tests/test_scheduler_performance.py",  # large department-scale CP-SAT models retain native solver heaps
    "tests/test_solver_run_compaction.py",  # historical heap isolation: repeated native model clones and solves
)


GATING_FILTER = " and not cpu_budget and not wall_benchmark"
CPU_BUDGET_FILTER = GATING_FILTER
MARKERS = {"unit": "not browser and not quarantine", "browser": "browser and not quarantine", "quarantine": "quarantine"}


def markers(cpu_budget_filter=None):
    """One pytest marker contract for planning, execution and exact-candidate validation."""
    if cpu_budget_filter is None:
        cpu_budget_filter = os.getenv("CPU_BUDGET_FILTER", CPU_BUDGET_FILTER)
    if cpu_budget_filter not in ("", GATING_FILTER):
        raise ValueError("Invalid CPU budget filter")
    return {kind: marker + cpu_budget_filter for kind, marker in MARKERS.items()}


def execution_marker(suite_name, *, quarantine=False, include_benchmarks=False):
    """Marker expression for a normal gating invocation, from one SSOT."""
    if suite_name not in ("unit", "browser", "mixed"):
        raise ValueError("Unknown gating suite")
    state = "quarantine" if quarantine else "not quarantine"
    suffix = "" if include_benchmarks else GATING_FILTER
    if suite_name == "mixed":
        return f"{state}{suffix}"
    base = "browser" if suite_name == "browser" else "not browser"
    return f"{base} and {state}{suffix}"


def wall_benchmark_marker(suite_name):
    """A suite's advisory wall benchmarks, shared by FULL and explicit diagnostics."""
    return execution_marker(suite_name, include_benchmarks=True) + " and wall_benchmark"


def cpu_budget_marker(suite_name):
    """A suite's CPU budgets, shared by FULL and advisory diagnostics."""
    return execution_marker(suite_name, include_benchmarks=True) + " and cpu_budget"


def suite(path):
    return "browser" if Path(path).name.startswith("test_browser") else "unit"


def test_files():
    return sorted(p.relative_to(ROOT).as_posix() for p in TESTS.rglob("test_*.py"))


@lru_cache(maxsize=2048)
def parsed(text):
    return ast.parse(text)


def closure(reverse, seeds):
    pending, reached = list(seeds), set()
    while pending:
        source = pending.pop()
        if source not in reached:
            reached.add(source)
            pending.extend(reverse.get(source, ()))
    return reached


def ts_imports(text):
    """Lex import specifiers without mistaking comments, labels or quoted code for imports."""
    tokens = re.findall(r"//[^\n]*|/\*[\s\S]*?\*/|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|"
                        r"`(?:\\.|[^`\\])*`|[A-Za-z_$][\w$]*|[^\s]", text)
    tokens = [t for t in tokens if not t.startswith(("//", "/*"))]
    if any(t.startswith("`") and "${" in t and re.search(r"\bimport\s*\(", t) for t in tokens):
        raise ValueError("Computed TypeScript template import")
    result = []
    for i, token in enumerate(tokens):
        if token not in ("import", "export") or i + 1 == len(tokens):
            continue
        after = tokens[i + 1]
        if after == ".":  # import.meta
            continue
        if token == "import" and after == "(":
            if i + 2 == len(tokens) or tokens[i + 2][0] not in "'\"":
                raise ValueError("Computed TypeScript import")
            result.append(tokens[i + 2][1:-1])
        elif token == "import" and after[0] in "'\"":
            result.append(after[1:-1])
        else:
            for j in range(i + 1, len(tokens) - 1):
                if tokens[j] == ";":
                    break
                if tokens[j] == "from" and tokens[j + 1][0] in "'\"":
                    result.append(tokens[j + 1][1:-1])
                    break
    return result


def graph(root):
    paths = sorted(p for top in ("apps", "packages", "scripts", "tests", "migrations", ".github/scripts")
                   for p in (root / top).rglob("*.py") if "__pycache__" not in p.parts)
    signature = tuple((p.relative_to(root).as_posix(), p.stat().st_mtime_ns, p.stat().st_size) for p in paths)
    return _graph(root, signature)


@lru_cache(maxsize=2)
def _graph(root, signature):
    # Repeated replay queries share parsing; edits, additions and deletions invalidate it.
    texts = {path: (root / path).read_text(encoding="utf-8") for path, _, _ in signature}
    modules = {path[:-3].replace("/", ".").removesuffix(".__init__"): path for path in texts}
    namespaces = {name.rsplit(".", i)[0] for name in modules for i in range(name.count(".") + 1)}
    reverse, opaque = {}, set()

    for source, text in texts.items():
        tree = parsed(text)
        package = source.rpartition("/")[0].replace("/", ".")
        loaders = {alias.asname or alias.name: alias.name for node in ast.walk(tree)
                   if isinstance(node, ast.ImportFrom) and node.module in ("importlib", "importlib.util")
                   for alias in node.names if alias.name in ("import_module", "spec_from_file_location")}

        def depend(name):
            if source.startswith("tests/") and "tests." + name in namespaces:
                name = "tests." + name
            if name.split(".")[0] in {"apps", "packages", "scripts", "tests", "migrations", "support"} and name not in namespaces:
                # A from-import may name an attribute; its parent module remains an owner.
                if name.rpartition(".")[0] not in modules:
                    opaque.add(source)
            for i in range(1, len(name.split(".")) + 1):
                target = modules.get(".".join(name.split(".")[:i]))
                if target and target != source:
                    reverse.setdefault(target, set()).add(source)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    depend(alias.name)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = package.split(".")
                    if node.level > len(parts):
                        raise ValueError("Unresolved relative import: " + source)
                    base = ".".join(parts[:len(parts) - node.level + 1] + ([base] if base else []))
                depend(base)
                for alias in node.names:
                    depend(base + "." + alias.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(t, ast.Name) and t.id == "pytest_plugins" for t in targets):
                    try:
                        plugins = ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        raise ValueError("Dynamic pytest_plugins: " + source) from None
                    for plugin in [plugins] if isinstance(plugins, str) else plugins:
                        if not isinstance(plugin, str):
                            raise ValueError("Invalid pytest_plugins: " + source)
                        depend(plugin)
            elif isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
                if name in loaders or name.endswith(("import_module", "__import__", "spec_from_file_location")):
                    argument = 1 if loaders.get(name, name).endswith("spec_from_file_location") else 0
                    if len(node.args) > argument and isinstance(node.args[argument], ast.Constant):
                        value = node.args[argument].value
                        if isinstance(value, str) and not value.startswith("."):
                            if value.endswith(".py"):
                                reverse.setdefault(value, set()).add(source)
                            else:
                                depend(value)
                            continue
                    opaque.add(source)
        # pytest loads every ancestor conftest, including plugins, without an explicit import.
        for parent in Path(source).parents:
            initializer = (parent / "__init__.py").as_posix()
            if initializer != source and initializer in texts:
                reverse.setdefault(initializer, set()).add(source)
        if source.startswith("tests/"):
            for parent in Path(source).parents:
                conftest = (parent / "conftest.py").as_posix()
                if conftest != source and conftest in texts:
                    reverse.setdefault(conftest, set()).add(source)
    return reverse, texts, opaque


def owners(root, changed, *, pr=False):
    reverse, texts, opaque = graph(root)
    seeds = set(changed)
    for path in changed:
        name = Path(path).name
        # File reads and Path joins (including directory/glob-driven fixtures).
        parts = [part for part in Path(path).parts[:-1]
                 if part not in {"tests", "scripts", "apps", "packages", ".github", "infra"} and len(part) >= 4]
        for source, text in texts.items():
            if path.startswith("apps/web/"):
                if not source.startswith(("tests/", "scripts/")):
                    continue
                if (not pr or source.startswith('tests/test_')) and ("apps/web" in text or re.search(r"['\"]apps['\"]\s*/\s*['\"]web['\"]", text)):
                    seeds.add(source)
            if (path in text or len(name) > 3 and name in text
                    or not path.endswith(".py") and any(re.search(r"['\"]" + re.escape(part) + r"[/\\'\"]", text)
                                                            for part in parts)):
                seeds.add(source)
    if pr and all(path.startswith(('apps/', 'packages/')) for path in changed):
        # Importing the app for collection does not make every test exercise each leaf module.
        reverse = {path: {consumer for consumer in consumers if not consumer.endswith('/conftest.py')}
                   for path, consumers in reverse.items()}
    reached = closure(reverse, seeds)
    if not pr and any(path.endswith(".py") for path in changed) and reached & {
            path for path in texts if Path(path).name.startswith("test_")}:
        reached |= closure(reverse, opaque)
    return {path for path in reached if Path(path).name.startswith("test_") and path.startswith("tests/")}


def mentions(path, test, text, names=True):
    name = Path(path).name
    words = [w for w in re.split(r"[_\W]+", Path(path).stem) if len(w) >= 4]
    return (path in text or (len(name) > 6 and name in text)
            or (path.endswith(".ts") and (f"/assets/{Path(path).stem}.js" in text
                or re.search(rf"\bimportBundle\(['\"]{re.escape(Path(path).stem)}\.js['\"]\)", text)))
            or (names and any(w in Path(test).stem for w in words)))


def web_graph():
    """The source dependency graph shared by browser and compiled-client readers."""
    web = {p.resolve(): p.read_text(encoding="utf-8", errors="replace") for p in (ROOT / "apps/web/src").rglob("*.ts")}
    importers = {p: set() for p in web}
    for source, text in web.items():
        try:
            dependencies = ts_imports(text)
        except ValueError:
            raise ValueError("Unresolved TypeScript imports") from None
        for relative in dependencies:
            if not relative.startswith("."):
                raise ValueError("Non-relative TypeScript import")
            if Path(relative).suffix not in ("", ".js", ".ts"):
                raise ValueError("Unsupported TypeScript import")
            target = (source.parent / relative).with_suffix(".ts").resolve()
            if target not in web:
                raise ValueError("Missing TypeScript dependency")
            importers[target].add(source)
    return web, importers


def compiled_web_tests(changed):
    """Literal compiled-module readers remain owners when their compiler fixture moves."""
    sources = [path for path in changed if path.startswith("apps/web/src/") and path.endswith(".ts")]
    if not sources:
        return set()
    _, importers = web_graph()
    modules = {path.with_suffix(".js").name for path in closure(importers, [(ROOT / p).resolve() for p in sources])}
    reverse, texts, _ = graph(ROOT)
    readers = {path for path, text in texts.items() if not path.endswith("/conftest.py") and any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"as_uri", "read_text", "read_bytes"}
        and isinstance(node.func.value, ast.BinOp) and isinstance(node.func.value.op, ast.Div)
        and isinstance(node.func.value.right, ast.Constant) and node.func.value.right.value in modules
        for node in ast.walk(parsed(text)))}
    reverse = {path: {consumer for consumer in consumers if not consumer.endswith("/conftest.py")}
               for path, consumers in reverse.items()}
    return {path for path in closure(reverse, readers)
            if path.startswith("tests/") and Path(path).name.startswith("test_")}


def web_tests(changed, files):
    """Browser owners of transitive TS importers; incomplete evidence fails closed."""
    try:
        web, importers = web_graph()
    except ValueError:
        return "all"
    pending = [(ROOT / path).resolve() for path in changed]
    entries = {(ROOT / "apps/web/src" / name).resolve() for name in ("app.ts", "startup.ts")}
    # Shared UI also serves inline entry pages whose suites cannot be proven by module-name matches.
    shared = {(ROOT / "apps/web/src" / name).resolve() for name in (
        "dom.ts", "conditions.ts", "picker.ts", "side_panel.ts", "table_view.ts", "table_menu.ts",
        "selection_bar.ts", "view_settings.ts")}
    # Entries own inline pages, but importing a page there does not make its siblings consumers.
    if entries & set(pending):
        return "all"
    reached = set()
    while pending:
        source = pending.pop()
        if source in entries:
            continue
        if source in shared:
            return "all"
        if source not in web and source.suffix != ".ts":
            return "all"
        if source not in reached:
            reached.add(source)
            pending.extend(importers.get(source, ()))
    owners = {f: {p for p in reached if mentions(p.relative_to(ROOT).as_posix(), f, text)}
              for f, text in files.items() if suite(f) == "browser"}
    covered = set().union(*owners.values())
    while newly_covered := {p for p in reached - covered if importers.get(p, set()) & covered}:
        covered |= newly_covered
    if reached - covered:
        return "all"
    return {f for f, modules in owners.items() if modules} or "all"


def doc_tests(changed, files):
    """Derive Markdown and JSON owners from literals and Path joins, including dynamic reads."""
    changed = [path for path in changed if path.startswith("docs/")
               or ("/" not in path and path.endswith((".md", ".json")))]
    if not changed:
        return set()

    def literal(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Path" and node.args:
            return literal(node.args[0])
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left, right = literal(node.left), literal(node.right)
            return left if left.endswith("/") else left + "/" + right
        return ""

    owners = {}
    for test, text in files.items():
        if "docs" not in text and not any(path in text for path in changed):
            continue
        references = set(re.findall(r"\bdocs(?:/[\w.*-]+)+/?", text))
        nodes = list(ast.walk(parsed(text)))
        joined = {id(part) for node in nodes if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
                  for part in (node.left, node.right)}
        joined |= {id(arg) for node in nodes if isinstance(node, ast.Call)
                   and isinstance(node.func, ast.Name) and node.func.id == "Path" for arg in node.args}
        for node in nodes:
            if id(node) not in joined and isinstance(node, (ast.BinOp, ast.Call, ast.Constant)):
                path = literal(node).strip("/")
                if path == "docs" or path.startswith("docs/") or path in changed:
                    references.add(path)
        for reference in references:
            prefix = reference.split("*", 1)[0].rstrip("/")
            owners.setdefault(prefix, set()).add(test)
    return {test for path in changed
            for prefix, tests in owners.items() if path == prefix or path.startswith(prefix + "/")
            for test in tests}


def pr_smoke(changed):
    files = {f: (ROOT / f).read_text(encoding="utf-8", errors="replace") for f in test_files()}
    targets = set(SMOKE["unit"]) | (set(changed) & files.keys()) | doc_tests(changed, files)
    targets -= {target for target in targets if "::" in target and target.split("::")[0] in targets}
    return {kind: {target for target in targets if suite(target) == kind} for kind in ("unit", "browser")}


def shared_foundation(path):
    if (path.startswith(("docs/", "changelog.d/"))
            or ("/" not in path and path.endswith(".md")) or path in ("LICENSE", ".gitignore")):
        return False
    return (path.startswith(CORE) or (path.startswith(".github/") and not path.startswith(".github/scripts/"))
            or path.endswith((".lock", "-lock.json"))
            or Path(path).name in ("manifest.json", "manifest.webmanifest")
            or path in ("tests/conftest.py", "tests/__init__.py"))


def select_with_reason(changed, *, pr=False):
    """Return the affected targets and a stable reason whenever ownership requires both full suites."""
    # Local FULL can be decided before inventory; PR feedback retains its narrower ownership policy.
    if not pr:
        for path in changed:
            if shared_foundation(path):
                return {"unit": "all", "browser": "all"}, f"shared-foundation: {json.dumps(path)}"
    files = {f: (ROOT / f).read_text(encoding="utf-8", errors="replace") for f in test_files()}
    docs = doc_tests(changed, files)
    picked = {kind: {test for test in docs if suite(test) == kind} for kind in ("unit", "browser")}
    code = False
    web = []
    python_hits = {}

    def add(f):
        if picked[suite(f)] != "all":
            picked[suite(f)].add(f)

    for path in changed:
        if path.startswith(("docs/", "changelog.d/")) or ("/" not in path and path.endswith(".md")) or path in ("LICENSE", ".gitignore"):
            continue
        code = True
        if pr and (path.startswith(('.github/', 'scripts/ci_'))
                   or path in ('scripts/affected_tests.py', 'tests/quarantine.json')):
            for test in files:
                if Path(test).name.startswith(('test_ci_', 'test_affected', 'test_retry_quarantine')):
                    add(test)
            continue
        if path.startswith("apps/web/"):
            web.append(path)
        if shared_foundation(path):
            return {"unit": "all", "browser": "all"}, f"shared-foundation: {json.dumps(path)}"
        if not (ROOT / path).is_file():
            return {"unit": "all", "browser": "all"}, f"missing-changed-path: {json.dumps(path)}"
        if path.endswith(".py") or path.startswith("tests/"):
            if path not in python_hits:
                try:
                    python_hits[path] = owners(ROOT, [path], pr=pr)
                except (OSError, SyntaxError, UnicodeError, ValueError, TypeError):
                    return {"unit": "all", "browser": "all"}, "unresolved-python-dependencies"
            hits = python_hits[path] | ({path} if path in files else set())
            if not hits:
                return {"unit": "all", "browser": "all"}, f"unknown-owner: {json.dumps(path)}"
            for f in hits:
                add(f)
            continue
        # Kit tests also read siblings via their directory; CI adds the safety and work core below.
        scope = "/".join(path.split("/")[:2]) if path.startswith("infra/") else ""
        names = not scope and not path.startswith(".github/scripts/") and path != "scripts/restore_drill.py"
        hits = [f for f, text in files.items()
                if mentions(path, f, text, names=names)
                or (scope and scope in text)]
        if not path.startswith(("apps/web/", "infra/", ".github/")):
            try:
                hits = set(hits) | owners(ROOT, [path], pr=pr)
            except (OSError, SyntaxError, UnicodeError, ValueError, TypeError):
                return {"unit": "all", "browser": "all"}, "unresolved-python-dependencies"
        if not hits and not path.startswith("apps/web/"):
            return {"unit": "all", "browser": "all"}, f"unknown-owner: {json.dumps(path)}"
        for f in hits:
            add(f)
    if web:
        try:
            browser = set() if pr else web_tests(web, files)
            for test in owners(ROOT, web, pr=pr) | compiled_web_tests(web):
                if suite(test) == "unit":
                    add(test)
        except (OSError, SyntaxError, UnicodeError, ValueError, TypeError):
            return {"unit": "all", "browser": "all"}, "unresolved-web-dependencies"
        picked["browser"] = "all" if browser == "all" else picked["browser"] | browser
        if picked["browser"] != "all" and any(path.startswith(("apps/web/src/", "apps/web/public/")) for path in web):
            picked["browser"] |= set(WEB_BROWSER_SMOKE)
    if code:
        for kind, tests in SMOKE.items():
            if picked[kind] != "all":
                picked[kind] |= set(tests)
                picked[kind] -= {t for t in picked[kind] if t.split("::")[0] in picked[kind] - {t}}  # a whole file covers its tests
    return picked, "unresolved-browser-ownership" if picked["browser"] == "all" else ""


def select(changed):
    """{"unit": set|"all", "browser": set|"all"} for local affected runs."""
    return select_with_reason(changed)[0]


def pr_select(changed):
    """PR and merge feedback use mapped transitive owners plus smoke; shared fan-outs run in FULL sweeps."""
    picked, reason = select_with_reason(changed, pr=True)
    if reason.startswith(('unknown-', 'missing-', 'unresolved-', 'selection-failed')):
        return {'unit': 'all', 'browser': 'all'}, reason
    if not any(not path.startswith(('docs/', 'changelog.d/')) and not ('/' not in path and path.endswith('.md'))
               and path not in ('LICENSE', '.gitignore') for path in changed):
        return picked, reason
    files = {f: (ROOT / f).read_text(encoding='utf-8') for f in test_files()}
    direct = {f for f, text in files.items() if f in changed or any(
        mentions(path, f, text, names=False) or Path(f).stem in (
            'test_' + Path(path).stem, 'test_browser_' + Path(path).stem)
        for path in changed)} | doc_tests(changed, files)
    direct |= compiled_web_tests(changed)
    if set(PLANNER_WEEK_SOURCES) & set(changed):
        direct.update(PLANNER_WEEK_BROWSER_GATES)
    if any(path.startswith(("apps/web/src/", "apps/web/public/")) for path in changed):
        direct.update(WEB_BROWSER_SMOKE)
    unit_files = sum(suite(f) == 'unit' for f in files)
    broad = picked['unit'] == 'all' or len({t.split('::')[0] for t in picked['unit']}) > .4 * unit_files
    # shortcut: shared-root PRs cover direct owners plus smoke; expand mapping if regression recall drops below 95%.
    if broad and (not reason or reason.startswith(('shared-foundation:', 'unresolved-browser-ownership'))):
        picked['unit'] = {f for f in direct if suite(f) == 'unit'} | set(SMOKE['unit'])
        reason = 'shared-fanout-deferred-to-full-sweep'
    elif picked['unit'] != 'all':
        picked['unit'] |= {f for f in direct if suite(f) == 'unit'}
    picked['browser'] = {f for f in direct if suite(f) == 'browser'} | set(SMOKE['browser'])
    for kind in picked:
        if picked[kind] != 'all':
            picked[kind] -= {t for t in picked[kind] if '::' in t and t.split('::')[0] in picked[kind]}
    return picked, reason


def core_targets():
    """A missing mandatory file/node is a planning failure, even when the remaining suite is FULL."""
    for target in SAFETY_CORE + WORK_CORE:
        path, *nodes = target.split("::")
        source = ROOT / path
        if not source.is_file() or (nodes and not any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == nodes[0]
                for node in ast.parse(source.read_text(encoding="utf-8")).body)):
            raise ValueError(f"Missing safety-core selector: {target}")
    return SAFETY_CORE + WORK_CORE


def durations(path=None):
    if path is None:
        if not DURATIONS.exists():
            return {}
        path = DURATIONS
    known = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(known, dict) or not known or any(
        not re.fullmatch(r"tests/test_\w+\.py(?:::[^\x00-\x1f]+)?", f) or type(seconds) not in (int, float)
        or seconds < 0 or seconds > sys.float_info.max or not math.isfinite(seconds) for f, seconds in known.items()
    ):
        raise ValueError("Durations must map test file paths to finite, non-negative seconds")
    if any('::' in path and path.split('::')[0] not in known for path in known):
        raise ValueError("Durations must map test file paths to finite, non-negative seconds; node weights need a file total")
    return known


def estimate(f, known):
    if f in known:
        return known[f]
    path = f.split("::")[0]
    if path in known:
        return known[path]
    values = sorted(seconds for path, seconds in known.items() if '::' not in path and suite(path) == suite(f))
    return values[len(values) // 2] if values else 60


def serial_groups(targets, known):
    """Keep native heap-heavy files together only while their serial tail fits twenty minutes."""
    files = {}
    for target in targets:
        if target.split("::")[0] in SERIAL_FILES:
            files.setdefault(target.split("::")[0], []).append(target)
    groups = sorted((sorted(targets) for targets in files.values()),
                    key=lambda group: (-sum(estimate(t, known) for t in group), group))
    if groups and sum(estimate(t, known) for group in groups for t in group) <= 1200:
        return [[t for group in groups for t in group]]
    return groups


def shard(kind, index, count, only=None, known=None):
    """Greedy longest-first split of whole test files by recorded duration (`-m` still picks the tests)."""
    tests = only.split() if only else [f for f in test_files() if suite(f) == kind]
    known = durations() if known is None else known
    serial = serial_groups(tests, known)
    parallel = [t for t in tests if t.split("::")[0] not in SERIAL_FILES]
    # Legacy one-row diagnostics already run serially; planners reserve enough rows to isolate files.
    if serial and count >= len(serial) + bool(parallel):
        if index <= len(serial):
            return serial[index - 1]
        tests, index, count = parallel, index - len(serial), count - len(serial)
    bins = [[0.0, []] for _ in range(count)]
    for f in sorted(tests, key=lambda f: (-estimate(f, known), f)):
        low = min(bins, key=lambda b: b[0])
        low[0] += estimate(f, known)
        low[1].append(f)
    return bins[index - 1][1]


def shard_counts():
    return {"unit": int(os.getenv("UNIT_SHARDS", "7")), "browser": int(os.getenv("BROWSER_SHARDS", "6"))}


PR_LARGE_SELECTION_MIN_FILES = 100  # per-suite programme threshold; full selection is always large


def pr_large_selection(tests):
    if tests == "all":
        return True
    if PR_LARGE_SELECTION_MIN_FILES is None:
        return False
    return len({target.split("::", 1)[0] for target in tests}) >= PR_LARGE_SELECTION_MIN_FILES


def ci(changed, full, max_shards, smoke=False, quick=False, pr=False, target_seconds=None, unit_only=False):
    """Key=value lines for $GITHUB_OUTPUT: what each suite runs and its shard numbers (JSON) for the job matrix."""
    if unit_only and (not full or not pr):
        raise ValueError("Unit-only selection is limited to PR feedback")
    changed = sorted(set(changed))
    reason, core = "", () if smoke or quick or pr else core_targets()
    if quick:
        picked = pr_smoke(changed)
    elif smoke:
        picked = SMOKE
    elif full:
        picked, reason = {"unit": "all", "browser": pr_select(changed)[0]['browser'] if unit_only else "all"}, "explicit-full"
    else:
        try:
            picked, reason = pr_select(changed) if pr else select_with_reason(changed)
        except Exception:
            picked, reason = {"unit": "all", "browser": "all"}, "selection-failed"
        for kind in picked if not pr else ():
            if picked[kind] != "all":
                picked[kind] |= set(SMOKE[kind]) | {target for target in core if suite(target) == kind}
                picked[kind] -= {target for target in picked[kind] if "::" in target
                                 and target.split("::")[0] in picked[kind]}
    if pr:
        for kind, targets in picked.items():
            universe = {f for f in test_files() if suite(f) == kind}
            if targets != "all" and universe and universe <= set(targets):
                picked[kind] = "all"
                reason = reason or "complete-dependency-closure"
    workers = {"unit": int(os.getenv("UNIT_WORKERS", "2")), "browser": int(os.getenv("BROWSER_WORKERS", "2"))}
    if min([*workers.values(), *max_shards.values()]) < 1:
        raise ValueError("CI shard and worker counts must be positive")
    known, lines, counts = durations(), [], []
    for kind, tests in picked.items():
        large_pr = pr and pr_large_selection(tests)
        if quick:
            shards = int(bool(tests))
        elif smoke:
            shards = 1
        elif tests == "all":
            shards = max_shards[kind]
        elif tests:
            volume_shards = max(1, math.ceil(
                sum(estimate(f, known) for f in tests) / ((target_seconds or SHARD_SECONDS) * workers[kind])))
            shards = max(max_shards[kind], volume_shards) if large_pr else min(max_shards[kind], len(tests), volume_shards)
        else:
            shards = 0
        targets = [f for f in test_files() if suite(f) == kind] if tests == "all" else tests
        serial = serial_groups(targets, known)
        if serial:
            shards = max(shards, len(serial) + any(t.split("::")[0] not in SERIAL_FILES for t in targets))
        counts.append(shards)
        lines += [f"{kind}_tests={'' if tests == 'all' else ' '.join(sorted(tests))}", f"{kind}_shards={json.dumps(list(range(1, shards + 1)))}"]
    lines.append(f"test_shards={json.dumps(list(range(1, max(counts) + 1)))}")
    digest = lambda value: hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()
    inputs = {"changed_paths": changed, "selector_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "core_sha256": digest({"safety": SAFETY_CORE, "work": WORK_CORE}),
              "safety_core_sha256": digest(SAFETY_CORE), "work_core_sha256": digest(WORK_CORE),
              "safety_core": list(SAFETY_CORE) if core else [], "work_core": list(WORK_CORE) if core else [],
              "selected": {kind: [f for f in test_files() if suite(f) == kind] if tests == "all" else sorted(tests)
                           for kind, tests in picked.items()},
              "full_suites": [kind for kind, tests in picked.items() if tests == "all"],
              "markers": {kind: marker for kind, marker in markers().items() if kind in picked}}
    large_selection = any(pr_large_selection(tests) for tests in picked.values())
    lines += ["selection_inputs=" + json.dumps(inputs, separators=(",", ":")),
              "full_fallback_reason=" + reason, f"large_selection={str(large_selection).lower()}"]
    if pr:
        plan, stats = {}, {}
        for kind, targets in inputs["selected"].items():
            indices = json.loads(next(line.split("=", 1)[1] for line in lines if line.startswith(kind + "_shards=")))
            plan[kind] = []
            serial = serial_groups(targets, known)
            for index in indices:
                part = shard(kind, index, len(indices), " ".join(targets), known=known)
                plan[kind].append({"index": index, "targets": part,
                                   "estimated_seconds": round(sum(estimate(f, known) for f in part) /
                                                              (1 if index <= len(serial) else workers[kind]), 1)})
            total = sum(suite(f) == kind for f in test_files())
            selected = len({target.split("::")[0] for target in targets})
            stats[kind] = {"selected_files": selected, "total_files": total,
                           "selected_share": round(selected / total, 4) if total else 0}
        lines += ["selection_version=1", "shard_plan=" + json.dumps(plan, separators=(",", ":")),
                  "selection_stats=" + json.dumps(stats, separators=(",", ":"))]
    return lines


def record(reports):
    import xml.etree.ElementTree as ET
    totals = {}
    for report in reports:
        for case in ET.parse(report).getroot().iter("testcase"):
            f = "tests/" + case.get("classname", "").split(".")[1] + ".py"
            totals[f] = totals.get(f, 0) + float(case.get("time", 0))
            node = f + '::' + case.get('name', '')
            if node in {target for targets in SMOKE.values() for target in targets}:
                totals[node] = totals.get(node, 0) + float(case.get('time', 0))
    known = durations()
    known.update({f: round(seconds, 1) for f, seconds in totals.items()})
    DURATIONS.write_text(json.dumps(dict(sorted(known.items())), indent=0) + "\n")


def fast_ci(changed, maxima=None, *, full=False, unit_only=False):
    """Choose two-minute shards and bounded xdist width by duration, within one shared CPU ceiling."""
    if unit_only and not full:
        raise ValueError("Unit-only selection is limited to PR feedback")
    maxima = maxima or shard_counts()
    if full:
        core_targets()
    values = dict(line.split('=', 1) for line in ci(changed, full or not changed, maxima, pr=True,
                                                      target_seconds=120, unit_only=unit_only))
    selected = json.loads(values['selection_inputs'])['selected']
    limits = {kind: int(os.getenv(kind.upper() + '_WORKERS', '2')) for kind in selected}
    capacity = int(os.getenv('CI_CAPACITY_CPU', '32')) - 6
    if min(limits.values()) < 1 or capacity < sum(bool(v) for v in selected.values()):
        raise ValueError('CI capacity must cover selected suites plus six CPU headroom')
    known = durations()
    known = {**known, **{t: estimate(t, known) for targets in selected.values() for t in targets}}
    serial = {kind: serial_groups(targets, known) for kind, targets in selected.items()}
    parallel = {kind: [t for t in targets if t.split('::')[0] not in SERIAL_FILES] for kind, targets in selected.items()}
    reserved = sum(map(len, serial.values()))
    if capacity < reserved + sum(bool(targets) for targets in parallel.values()):
        raise ValueError('CI capacity must cover serial rows and selected parallel suites plus six CPU headroom')
    costs = {kind: sum(estimate(t, known) for t in targets) for kind, targets in parallel.items()}
    options = {}
    for kind, targets in parallel.items():
        maximum = max(1, maxima[kind] - len(serial[kind]))
        options[kind] = [(0, 0, 0)] if not targets else []
        tails = {count: max(sum(known[t] for t in shard(kind, i, count, ' '.join(targets), known=known))
                            for i in range(1, count + 1))
                 for count in range(1, min(maximum, len(targets)) + 1)}
        for width in range(1, min(limits[kind], capacity) + 1):
            desired = min(maximum, len(targets), max(1, math.ceil(costs[kind] / (120 * width))))
            for count in range(1, desired + 1):
                tail = tails[count] / width
                options[kind].append((count, width, tail))
    choices = [(u, b) for u in options['unit'] for b in options['browser'] if reserved + u[0]*u[1] + b[0]*b[1] <= capacity]
    unit, browser = min(choices, key=lambda pair: (max(p[2] for p in pair), sum(p[0] for p in pair),
                                                   sum(p[0]*p[1] for p in pair)))
    counts = {'unit': unit[0] + len(serial['unit']), 'browser': browser[0] + len(serial['browser'])}
    workers = {'unit': unit[1], 'browser': browser[1]}
    matrix = []
    mixed_width = min(*limits.values(), capacity)
    tiny = (not reserved and all(values[k + '_tests'] for k in selected if selected[k])
            and not any(t.split('::')[0] == 'tests/test_contact_preview_perf.py' for targets in selected.values() for t in targets)
            and sum(costs.values()) / mixed_width <= 120)
    if tiny and all(selected.values()):
        matrix = [{'suite': 'mixed', 'shard': 1, 'total': 1, 'workers': str(mixed_width),
                   'only': ' '.join(sorted(set().union(*map(set, selected.values()))))}]
    else:
        for kind, count in counts.items():
            matrix += [{'suite': kind, 'shard': i, 'total': count, 'workers': '1' if i <= len(serial[kind]) else str(workers[kind]),
                        'only': values[kind + '_tests']} for i in range(1, count + 1)]
    plan = {}
    for kind, count in counts.items():
        values[kind + '_shards'] = json.dumps(list(range(1, count + 1)))
    values['test_shards'] = json.dumps(list(range(1, max(counts.values()) + 1)))
    for row in matrix:
        kind = row['suite']
        targets = (row['only'].split() if kind == 'mixed' else
                   shard(kind, row['shard'], row['total'], row['only'] or None, known=known))
        plan.setdefault(kind, []).append({'index': row['shard'], 'targets': targets,
            'estimated_seconds': round(sum(estimate(t, known) for t in targets) / int(row['workers']), 1)})
    values['shard_plan'] = json.dumps(plan, separators=(',', ':'))
    values['matrix'] = json.dumps(matrix, separators=(',', ':'))
    return [key + '=' + value for key, value in values.items()]


def diagnostic_ci(selection, source_sha, env=None):
    """Validate same-head targets or one advisory budget kind on the quiet lane.

    {"suite":"unit|browser","benchmark":"cpu_budget|wall_benchmark","nodes":[...]}
    requires named nodes of that kind. The original {"suite":"unit|browser",
    "benchmark":"wall"} selects the current wall set. Neither provides qualification.
    """
    env = os.environ if env is None else env
    repository = env.get('GITHUB_REPOSITORY', '')  # the repository running this workflow, never a fixed name
    if (env.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
            or not re.fullmatch(r'[\w.-]+/[\w.-]+', repository)
            or not re.fullmatch(r'[a-f0-9]{40}', source_sha or '')
            or source_sha != env.get('GITHUB_SHA') or source_sha != env.get('GITHUB_WORKFLOW_SHA')
            or not env.get('GITHUB_REF', '').startswith('refs/heads/')
            or env.get('GITHUB_WORKFLOW_REF') != repository + '/.github/workflows/ci.yml@' + env.get('GITHUB_REF', '')
            or subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip() != source_sha):
        raise ValueError('Diagnostic requires the exact dispatch source and CI workflow checkout')

    def unique(pairs):
        values = {}
        for key, value in pairs:
            if key in values:
                raise ValueError('Duplicate diagnostic suite')
            values[key] = value
        return values

    try:
        selected = json.loads(selection, object_pairs_hook=unique)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError('Diagnostic selection must be explicit unit/browser JSON target lists') from error
    whole_wall = (isinstance(selected, dict) and set(selected) == {'suite', 'benchmark'}
                  and selected['suite'] in ('unit', 'browser') and selected['benchmark'] == 'wall')
    named_budget = (isinstance(selected, dict) and set(selected) == {'suite', 'benchmark', 'nodes'}
                    and selected['suite'] in ('unit', 'browser')
                    and selected['benchmark'] in ('cpu_budget', 'wall_benchmark'))
    advisory = whole_wall or named_budget
    budget_kind, budget_suite = '', ''
    if advisory:
        budget_kind = 'wall_benchmark' if whole_wall else selected['benchmark']
        budget_suite = selected['suite']
        selected = {budget_suite: ['tests'] if whole_wall else selected['nodes']}
    if not isinstance(selected, dict) or len(selected) != 1 or set(selected) - {'unit', 'browser'}:
        raise ValueError('Diagnostic requires exactly one explicit unit or browser lane per dispatch')
    trees, matrix = {}, []
    for kind in ('unit', 'browser'):
        if kind not in selected:
            continue
        targets = selected[kind]
        if (not isinstance(targets, list) or not targets
                or any(not isinstance(t, str) or re.search(r'\s|[\x00-\x1f\x7f]', t) for t in targets)
                or len(set(targets)) != len(targets)):
            raise ValueError('Diagnostic targets must be distinct nonempty test entries')
        for target in targets:
            if whole_wall:
                continue  # pytest's shared wall marker selects the current suite, without a copied node list.
            if '[' in target and target.split('[', 1)[0] in targets:
                raise ValueError('Overlapping diagnostic function and parameter targets')
            path, *parts = target.split('::')
            file = (ROOT / path).resolve()
            if (not re.fullmatch(r'tests/(?:\w+/)*test_\w+\.py', path)
                    or not file.is_relative_to((ROOT / 'tests').resolve()) or not file.is_file()
                    or suite(path) != kind or parts and path in targets):
                raise ValueError('Unsupported diagnostic test path or suite: ' + target)
            if path not in trees:
                trees[path] = ast.parse(file.read_text(encoding='utf-8'))
            node, ancestors = trees[path], []
            for part in parts:
                name = part.split('[', 1)[0]
                if not re.fullmatch(r'\w+(?:\[.*\])?', part):
                    raise ValueError('Malformed diagnostic test node: ' + target)
                node = next((n for n in node.body if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                             and n.name == name), None)
                if node is None:
                    raise ValueError('Diagnostic test node is absent: ' + target)
                ancestors.append(node)
            if parts and (not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith('test_')
                          or any(not isinstance(parent, ast.ClassDef) for parent in ancestors[:-1])):
                raise ValueError('Diagnostic requires test files or test functions: ' + target)
            if named_budget and not parts:
                raise ValueError('Budget diagnostics require named test nodes: ' + target)
            scope = ancestors if parts else list(ast.walk(node))
            module_marks = [n.value for n in trees[path].body if isinstance(n, ast.Assign)
                            and any(isinstance(t, ast.Name) and t.id == 'pytestmark' for t in n.targets)]
            marks = module_marks + [decorator for owner in scope for decorator in getattr(owner, 'decorator_list', [])]
            budgets = {n.attr for mark in marks for n in ast.walk(mark)
                       if isinstance(n, ast.Attribute) and n.attr in ('cpu_budget', 'wall_benchmark')}
            if named_budget and budgets != {budget_kind}:
                raise ValueError('Diagnostic budget marker mismatch for ' + target + ': expected ' + budget_kind)
            if budgets and not named_budget:
                raise ValueError('CPU and wall budgets require their separate qualification lane')
        runner_key = 'HEAVY_DB_RUNNER' if advisory else kind.upper() + '_RUNNER'
        matrix.append({'suite': 'cpu-budget' if advisory else kind, 'shard': 1, 'total': 1, 'only': ' '.join(sorted(targets)),
                       'runner': self_hosted_runner(env.get(runner_key) or DEFAULT_RUNNER), 'workers': '0' if advisory else '1'})
    unit_matrix = [row for row in matrix if row['suite'] == 'unit' or advisory]
    browser_matrix = [row for row in matrix if row['suite'] == 'browser']
    return {'sha': source_sha, 'base': '', 'code': 'true', 'image': 'false', 'full': 'false', 'diagnostic': 'true',
            **({'advisory': 'true', 'budget_kind': budget_kind, 'budget_suite': budget_suite} if advisory else {}),
            'workflow_ref': env['GITHUB_WORKFLOW_REF'],
            **{kind + '_tests': ' '.join(sorted(selected.get(kind, []))) for kind in ('unit', 'browser')},
            'matrix': json.dumps(matrix, separators=(',', ':')),
            'unit_matrix': json.dumps(unit_matrix, separators=(',', ':')),
            'browser_matrix': json.dumps(browser_matrix, separators=(',', ':')),
            'large_selection': 'false',
            'hosted_unit_rows': 'false', 'hosted_browser_rows': 'false', 'hosted_test_rows': 'false'}


def changed_files(base):
    out = lambda *a: subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    return sorted(set(out("diff", "--no-renames", "--name-only", base + "...HEAD") + out("diff", "--no-renames", "--name-only", "HEAD")
                      + out("ls-files", "--others", "--exclude-standard")))


def include_failures(picked, reports):
    """Pure local diagnostic union; failed CPU/wall budgets require separate qualification."""
    import xml.etree.ElementTree as ET

    spec = importlib.util.spec_from_file_location("pytest_ci", ROOT / ".github/scripts/pytest_ci.py")
    pytest_ci = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pytest_ci)

    combined = {kind: "all" if targets == "all" else set(targets) for kind, targets in picked.items()}
    for report in reports:
        try:
            rows = list(pytest_ci.case_rows([report]))
            # A later passing occurrence must not erase an earlier budget classification.
            budget_nodes = {pytest_ci.case_node(case)
                            for case in ET.parse(report).getroot().iter("testcase")
                            for prop in case.findall("properties/property")
                            if (prop.get("name") == "fmuc_budget" and prop.get("value"))
                            or (prop.get("name") == "fmuc_no_retry"
                                and prop.get("value") in ("cpu_budget", "wall_benchmark"))}
        except (OSError, ValueError, ET.ParseError) as exc:
            raise ValueError("Unreadable or malformed failure report") from exc
        if not rows:
            raise ValueError("Empty failure report; investigate collection before selecting a repair run")
        for node, row in rows:
            if row["outcome"] != "failed":
                continue
            if node in budget_nodes:
                raise ValueError("Failed CPU/wall budgets need a repaired candidate and separate qualification")
            path = node.split("::")[0]
            source = (ROOT / path).resolve()
            if (not re.fullmatch(r"tests/(?:\w+/)*test_\w+\.py::[^\x00-\x1f]+", node)
                    or any(not part.strip() for part in node.split("[", 1)[0].split("::")[1:])
                    or not node.rsplit("::", 1)[-1].strip()
                    or not source.is_relative_to((ROOT / "tests").resolve()) or not source.is_file()):
                raise ValueError("Failed test is absent or unidentified; investigate before a repair run")
            kind = suite(path)
            if combined[kind] != "all":
                combined[kind].add(node)
    for kind, targets in combined.items():
        if targets != "all":
            combined[kind] = {node for node in targets if "::" not in node or node.split("::")[0] not in targets}
    return combined


def local_workers(extra=()):
    """Validate both local suites and any explicit pytest override before starting a process."""
    def bounded(value):
        if not re.fullmatch(r"[0-4]", str(value)):
            raise ValueError("Local worker counts must be 0..4 (0 runs serially)")
        return int(value)

    workers = {kind: bounded(os.getenv(kind.upper() + "_WORKERS", str(default)))
               for kind, default in (("unit", 4), ("browser", 3))}
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False, exit_on_error=False)
    parser.add_argument("-n", "--numprocesses", action="append")
    try:
        args, remaining = parser.parse_known_args(extra)
    except argparse.ArgumentError as exc:
        raise ValueError("Invalid local pytest worker override") from exc
    if args.numprocesses:
        if len(args.numprocesses) != 1:
            raise ValueError("Supply only one local pytest worker override")
        workers = dict.fromkeys(workers, bounded(args.numprocesses[0]))
    return workers, remaining


def main(argv):
    argv, run, extra = (argv[:argv.index("--run")], True, argv[argv.index("--run") + 1:]) if "--run" in argv else (argv, False, [])
    if argv[:1] == ['--diagnostic-ci']:
        parser = argparse.ArgumentParser(allow_abbrev=False)
        parser.add_argument('--diagnostic-ci', required=True)
        parser.add_argument('--sha', required=True)
        args = parser.parse_args(argv)
        return print(*(key + '=' + value for key, value in diagnostic_ci(args.diagnostic_ci, args.sha).items()), sep='\n')
    if argv[:1] == ["--markers"]:
        return print(MARKERS[argv[1]])
    if argv[:1] == ["--record"]:
        return record(argv[1:])
    if argv[:1] == ["--shard"]:
        kind, (index, count) = argv[1], map(int, argv[2].split("/"))
        only = argv[argv.index("--only") + 1] if "--only" in argv else None
        return print(*shard(kind, index, count, only), sep="\n")
    if argv[:1] == ["--ci"]:
        return print(*ci(argv[1:], len(argv) == 1, shard_counts()), sep="\n")
    if argv[:1] == ['--full-ci']:
        return print(*fast_ci([], full=True), sep='\n')
    if argv[:1] == ['--full-ci-unit']:
        return print(*fast_ci(argv[1:], full=True, unit_only=True), sep='\n')
    if argv[:1] == ['--fast-ci']:
        return print(*fast_ci(argv[1:]), sep='\n')
    if argv[:1] == ["--pr-affected-v1"]:
        maxima = {"unit": int(os.getenv("UNIT_SHARDS", "2")), "browser": int(os.getenv("BROWSER_SHARDS", "2"))}
        return print(*ci(argv[1:], len(argv) == 1, maxima, pr=True), sep="\n")
    if argv[:1] == ["--pr-smoke"]:
        return print(*ci(argv[1:], False, shard_counts(), quick=True), sep="\n")
    if argv == ["--smoke"] and not run:
        return print(*ci([], False, {"unit": 1, "browser": 1}, smoke=True), sep="\n")
    parser = argparse.ArgumentParser(description="Local affected/failed repair selection; never release qualification")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--smoke", action="store_true")
    mode.add_argument("--full", action="store_true", help="Both complete functional suites, with canonical markers")
    parser.add_argument("--base", default="origin/dev")
    parser.add_argument("--files", nargs="*")
    parser.add_argument("--failed-report", action="append", default=[], type=Path,
                        help="Original local JUnit after failure classification; repeat for each report")
    args = parser.parse_args(argv)
    if args.smoke:
        picked = SMOKE
    elif args.full:
        picked = {"unit": "all", "browser": "all"}
    else:
        picked = select(args.files if args.files is not None else changed_files(args.base))
    picked = include_failures(picked, args.failed_report)
    workers, extra = local_workers(extra) if run else ({}, extra)
    for kind, tests in picked.items():
        print(f"{kind}: {tests if tests == 'all' else ' '.join(sorted(tests)) or '-'}")
    unit = picked.get("unit")
    if run and unit and (unit == "all" or {t.split("::")[0] for t in unit} & set(WEB_CLIENT_FAST)):
        # Otherwise every web/client test errors in its fixture: hundreds of errors, not one cause.
        if not (ROOT / NODE_TOOLCHAIN).is_file() or not shutil.which("node"):
            print(f"Web/client tests need Node and {NODE_TOOLCHAIN}: install Node, then run `npm ci`", file=sys.stderr)
            return 2
        if not (ROOT / WEB_BUILD).is_file():  # CI shards download this build; locally it must exist first
            print(f"Web/client tests read {WEB_BUILD}: run `python scripts/build_web.py` first", file=sys.stderr)
            return 2
    status = 0
    for kind, tests in picked.items() if run else ():
        if tests != "all":
            target = sorted(tests)
        elif kind == "unit":
            target = ["tests", "--ignore-glob", f"*{os.sep}test_browser*.py"]
        else:
            target = [path for path in test_files() if suite(path) == kind]
        marker = ["-m", markers()[kind]]
        if target:
            result = subprocess.call([sys.executable, "-m", "pytest", "-q", *marker, "--dist", "worksteal", *target, *extra,
                                      "-n", str(workers[kind])], cwd=ROOT)
            status |= 0 if result == 5 else result  # An affected file may contain only nightly budgets.
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
