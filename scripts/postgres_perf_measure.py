#!/usr/bin/env python3
"""Bounded, read-only PostgreSQL performance measurement for LEAD-RELEASE.

The command deliberately accepts only a named non-production target and a
recorded operator acknowledgement.  It never prints a connection URL or raw
SQL, never commits a transaction, and writes a redacted JSON snapshot plus a
sha256 sidecar.  Application-pool and OS disk counters are reported as
unavailable unless an operator supplies a small numeric pool snapshot.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


SCHEMA_VERSION = 1
ACKNOWLEDGEMENT = "READ_ONLY_NO_PRODUCTION_NO_DECEMBER"
DEFAULT_MAX_ROWS = 50
DEFAULT_TIMEOUT_MS = 2_000
MAX_ROWS = 500
MAX_TIMEOUT_MS = 30_000
MAX_EXPLAINS = 4
MAX_SQL_BYTES = 4_096
MAX_MANIFEST_BYTES = 64 * 1_024
MAX_POOL_BYTES = 8 * 1_024
MAX_PLAN_NODES = 200

TARGET_PATTERN = re.compile(r"^(?:dev|test|qa|staging|preprod)(?:[-_][a-z0-9][a-z0-9._-]*)?$")
LABEL_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{1,63}$")
ENV_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
FORBIDDEN_IDENTIFIER = re.compile(
    r"(?i)(?:^|[^a-z0-9])(?:prod(?:uction)?|live|dec(?:ember)?|20\d{2}[-_/]?12(?:[-_/]?\d{1,2})?|20\d{2}12)(?:$|[^a-z0-9])"
)
FORBIDDEN_SQL = re.compile(
    r"(?i)\b(?:insert|update|delete|merge|copy|create|alter|drop|truncate|grant|revoke|vacuum|refresh|call|do|set|prepare|execute|lock|comment|security_label|pg_read_file|pg_read_binary_file|pg_ls_dir|pg_sleep|dblink|nextval|lo_import|lo_export|set_config)\b"
)
POOL_FIELDS = {
    "capacity",
    "in_use",
    "idle",
    "waiters",
    "wait_ms_p50",
    "wait_ms_p95",
    "timeouts",
    "sample_window_seconds",
}
PLAN_NUMERIC_FIELDS = {
    "Actual Loops",
    "Actual Rows",
    "Actual Startup Time",
    "Actual Total Time",
    "Execution Time",
    "Local Hit Blocks",
    "Local Read Blocks",
    "Local Dirtied Blocks",
    "Local Written Blocks",
    "Planning Time",
    "Shared Hit Blocks",
    "Shared Read Blocks",
    "Shared Dirtied Blocks",
    "Shared Written Blocks",
    "Temp Read Blocks",
    "Temp Written Blocks",
}


class CollectionRefusal(ValueError):
    """A target or input failed the fail-closed collection gate."""


class CollectionFailure(RuntimeError):
    """A required read-only collection step could not be completed."""


DATABASE_SQL = """
SELECT datname, numbackends, xact_commit, xact_rollback,
       blks_read, blks_hit, tup_returned, tup_fetched,
       temp_files, temp_bytes, deadlocks, blk_read_time, blk_write_time
FROM pg_stat_database
WHERE datname = current_database()
"""

TABLE_SQL = """
SELECT t.schemaname, t.relname, t.seq_scan, t.seq_tup_read,
       t.idx_scan, t.idx_tup_fetch, t.n_tup_ins, t.n_tup_upd,
       t.n_tup_del, t.n_live_tup, t.n_dead_tup,
       t.vacuum_count, t.autovacuum_count, t.analyze_count,
       t.autoanalyze_count, s.heap_blks_read, s.heap_blks_hit
FROM pg_stat_user_tables AS t
LEFT JOIN pg_statio_user_tables AS s USING (relid)
ORDER BY (COALESCE(t.seq_scan, 0) + COALESCE(t.idx_scan, 0)) DESC,
         t.schemaname, t.relname
LIMIT %s
"""

INDEX_SQL = """
SELECT i.schemaname, i.relname, i.indexrelname, i.idx_scan,
       i.idx_tup_read, i.idx_tup_fetch, s.idx_blks_read, s.idx_blks_hit
FROM pg_stat_user_indexes AS i
LEFT JOIN pg_statio_user_indexes AS s USING (relid, indexrelid)
ORDER BY COALESCE(i.idx_scan, 0) DESC, i.schemaname,
         i.relname, i.indexrelname
LIMIT %s
"""

SETTINGS_SQL = """
SELECT current_database() AS database_name,
       current_setting('server_version_num') AS server_version_num,
       current_setting('shared_buffers') AS shared_buffers,
       current_setting('work_mem') AS work_mem,
       current_setting('effective_cache_size') AS effective_cache_size
"""

ACTIVITY_SQL = """
SELECT count(*)::bigint AS backends,
       count(*) FILTER (WHERE state = 'active')::bigint AS active_backends,
       count(*) FILTER (WHERE wait_event IS NOT NULL)::bigint AS waiting_backends,
       count(*) FILTER (WHERE wait_event_type = 'Lock')::bigint AS lock_waiting_backends
FROM pg_stat_activity
WHERE datname = current_database()
"""

LOCK_SQL = """
SELECT count(*)::bigint AS locks,
       count(*) FILTER (WHERE NOT granted)::bigint AS ungranted_locks
FROM pg_locks AS l
JOIN pg_stat_activity AS a ON a.pid = l.pid
WHERE a.datname = current_database()
"""


def _forbidden_identifier(value: str) -> bool:
    return bool(FORBIDDEN_IDENTIFIER.search(value.replace("\\", "/")))


def validate_gate(target: str, operator: str, acknowledgement: str) -> tuple[str, str]:
    """Validate the explicit operator and non-production target gate."""
    normalized_target = target.strip().lower()
    normalized_operator = operator.strip()
    if acknowledgement != ACKNOWLEDGEMENT:
        raise CollectionRefusal("explicit read-only/no-production/no-December acknowledgement is required")
    if not TARGET_PATTERN.fullmatch(normalized_target) or _forbidden_identifier(normalized_target):
        raise CollectionRefusal("target must be an approved non-production, non-December identifier")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{1,63}", normalized_operator) or _forbidden_identifier(normalized_operator):
        raise CollectionRefusal("operator must be a short, non-sensitive identifier")
    return normalized_target, normalized_operator


def validate_artifact_path(path: Path) -> Path:
    """Reject evidence paths that could be mistaken for protected data."""
    resolved = path.expanduser().resolve()
    if resolved.suffix.lower() != ".json":
        raise CollectionRefusal("output must be a .json artifact")
    if _forbidden_identifier(str(resolved)):
        raise CollectionRefusal("output path contains a production or December identifier")
    return resolved


def validate_database_url(url: str) -> str:
    """Accept an explicit PostgreSQL URL without ever echoing its secret."""
    value = url.strip()
    if len(value) > MAX_SQL_BYTES:
        raise CollectionRefusal("the selected database URL is too long")
    if value.startswith("postgresql+psycopg://"):
        value = "postgresql://" + value.removeprefix("postgresql+psycopg://")
    parts = urlsplit(value)
    if parts.scheme not in {"postgresql", "postgres"} or not parts.hostname or not parts.path.strip("/"):
        raise CollectionRefusal("the selected database URL must be an explicit PostgreSQL URL")
    if _forbidden_identifier(parts.hostname) or _forbidden_identifier(parts.path.strip("/")):
        raise CollectionRefusal("database host/name contains a production or December identifier")
    return value


def _column_name(description: Any) -> str:
    return str(getattr(description, "name", description[0]))


def _rows(cursor: Any) -> list[dict[str, Any]]:
    columns = [_column_name(item) for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _one(cursor: Any) -> dict[str, Any]:
    result = _rows(cursor)
    if len(result) != 1:
        raise CollectionFailure("a required PostgreSQL query returned an unexpected row count")
    return result[0]


def _safe_error(error: BaseException) -> dict[str, str]:
    code = getattr(error, "sqlstate", None) or getattr(error, "pgcode", None)
    result = {"kind": type(error).__name__}
    if isinstance(code, str) and re.fullmatch(r"[0-9A-Z]{5}", code):
        result["sqlstate"] = code
    return result


def _number(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return None
    return int(converted) if converted.is_integer() else converted


def _ratio(hit: Any, read: Any) -> float | None:
    hit_number, read_number = _number(hit), _number(read)
    if hit_number is None or read_number is None or hit_number + read_number <= 0:
        return None
    return round(hit_number / (hit_number + read_number), 6)


def _database_stats(row: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: _number(value) for key, value in row.items() if key != "datname"}
    result["cache_hit_ratio"] = _ratio(row.get("blks_hit"), row.get("blks_read"))
    return result


def _table_stats(row: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: (_number(value) if key not in {"schemaname", "relname"} else value)
              for key, value in row.items()}
    result["cache_hit_ratio"] = _ratio(row.get("heap_blks_hit"), row.get("heap_blks_read"))
    return result


def _index_stats(row: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: (_number(value) if key not in {"schemaname", "relname", "indexrelname"} else value)
              for key, value in row.items()}
    result["cache_hit_ratio"] = _ratio(row.get("idx_blks_hit"), row.get("idx_blks_read"))
    return result


def _normalize_sql(sql: str) -> str:
    text = re.sub(r"'(?:''|[^'])*'", "?", sql)
    text = re.sub(r"\b\d+(?:\.\d+)?\b", "?", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def validate_select(sql: str) -> str:
    value = sql.strip()
    if len(value.encode("utf-8")) > MAX_SQL_BYTES:
        raise CollectionRefusal("an EXPLAIN fingerprint query exceeds the bounded SQL size")
    if not re.match(r"(?is)^select\b", value):
        raise CollectionRefusal("EXPLAIN fingerprints must be a single SELECT statement")
    if ";" in value or "--" in value or "/*" in value or "*/" in value:
        raise CollectionRefusal("EXPLAIN fingerprints cannot contain multiple statements or comments")
    if FORBIDDEN_SQL.search(value) or re.search(r"(?i)\b(?:for\s+(?:update|share)|into)\b", value):
        raise CollectionRefusal("EXPLAIN fingerprint contains a write, lock, or side-effecting keyword")
    if _forbidden_identifier(value):
        raise CollectionRefusal("EXPLAIN fingerprint contains a production or December identifier")
    return value


def load_fingerprints(path: Path | None) -> list[dict[str, str]]:
    if path is None:
        return []
    resolved = path.expanduser().resolve()
    if _forbidden_identifier(str(resolved)):
        raise CollectionRefusal("fingerprint manifest path contains a production or December identifier")
    try:
        if resolved.stat().st_size > MAX_MANIFEST_BYTES:
            raise CollectionRefusal("fingerprint manifest exceeds the bounded input size")
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CollectionRefusal("fingerprint manifest could not be read as JSON") from error
    entries = document.get("fingerprints") if isinstance(document, dict) else None
    if not isinstance(entries, list) or len(entries) > MAX_EXPLAINS:
        raise CollectionRefusal("fingerprint manifest must contain at most four entries")
    result: list[dict[str, str]] = []
    names: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("approved") is not True:
            raise CollectionRefusal("every EXPLAIN fingerprint must carry approved=true")
        name = entry.get("name")
        if not isinstance(name, str) or not LABEL_PATTERN.fullmatch(name) or _forbidden_identifier(name):
            raise CollectionRefusal("fingerprint names must be short, non-sensitive labels")
        if name in names:
            raise CollectionRefusal("fingerprint names must be unique")
        query = entry.get("sql")
        if not isinstance(query, str):
            raise CollectionRefusal("fingerprint SQL must be text")
        names.add(name)
        result.append({"name": name, "sql": validate_select(query)})
    return result


def load_pool_snapshot(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"status": "not_exposed", "reason_code": "APPLICATION_POOL_METRICS_NOT_EXPOSED"}
    resolved = path.expanduser().resolve()
    if _forbidden_identifier(str(resolved)):
        raise CollectionRefusal("pool snapshot path contains a production or December identifier")
    try:
        if resolved.stat().st_size > MAX_POOL_BYTES:
            raise CollectionRefusal("pool snapshot exceeds the bounded input size")
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CollectionRefusal("pool snapshot could not be read as JSON") from error
    if not isinstance(document, dict) or set(document) - POOL_FIELDS:
        raise CollectionRefusal("pool snapshot contains an unsupported field")
    metrics: dict[str, int | float] = {}
    for name, value in document.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or value > 1_000_000_000:
            raise CollectionRefusal("pool snapshot values must be bounded non-negative numbers")
        metrics[name] = value
    capacity = metrics.get("capacity")
    if capacity and "in_use" in metrics:
        metrics["utilization"] = round(float(metrics["in_use"]) / float(capacity), 6)
    return {"status": "collected", "source": "operator_supplied_read_only_snapshot", "metrics": metrics}


def _plan_fingerprint(document: Any) -> dict[str, Any]:
    """Keep plan shape and counters; drop SQL text, predicates, and literals."""
    if isinstance(document, list) and len(document) == 1 and isinstance(document[0], dict):
        document = document[0]

    nodes: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if len(nodes) >= MAX_PLAN_NODES:
            return
        if not isinstance(node, dict):
            return
        item: dict[str, Any] = {}
        if isinstance(node.get("Node Type"), str):
            item["node_type"] = node["Node Type"]
        for field in PLAN_NUMERIC_FIELDS:
            if field in node and isinstance(node[field], (int, float)):
                item[field.replace(" ", "_").lower()] = node[field]
        if item:
            nodes.append(item)
        for child in node.get("Plans", []) if isinstance(node.get("Plans"), list) else []:
            visit(child)

    if isinstance(document, dict):
        visit(document.get("Plan", document))
        for field in ("Planning Time", "Execution Time"):
            if isinstance(document.get(field), (int, float)):
                nodes.append({field.replace(" ", "_").lower(): document[field]})
    canonical = json.dumps(nodes, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return {"plan_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(), "nodes": nodes}


def _optional_query(cursor: Any, sql: str) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    cursor.execute("SAVEPOINT perf_optional")
    try:
        cursor.execute(sql)
        return _one(cursor), None
    except Exception as error:  # permission and version differences are optional evidence
        try:
            cursor.execute("ROLLBACK TO SAVEPOINT perf_optional")
            cursor.execute("RELEASE SAVEPOINT perf_optional")
        except Exception as rollback_error:
            raise CollectionFailure("read-only optional query could not be rolled back safely") from rollback_error
        return None, _safe_error(error)
    finally:
        try:
            cursor.execute("RELEASE SAVEPOINT perf_optional")
        except Exception:
            pass


def collect_database(database_url: str, max_rows: int, timeout_ms: int,
                     fingerprints: Sequence[Mapping[str, str]], pool: Mapping[str, Any]) -> dict[str, Any]:
    """Collect a bounded snapshot in one transaction configured read-only."""
    try:
        import psycopg
    except ImportError as error:
        raise CollectionFailure("psycopg is required only when the collector is invoked") from error

    try:
        connection = psycopg.connect(
            database_url,
            connect_timeout=10,
            options=f"-c statement_timeout={timeout_ms} -c default_transaction_read_only=on",
        )
    except Exception as error:
        raise CollectionFailure("PostgreSQL connection failed") from error

    warnings: list[dict[str, Any]] = []
    try:
        with connection.cursor() as cursor:
            cursor.execute("BEGIN TRANSACTION READ ONLY")
            cursor.execute("SHOW transaction_read_only")
            if str(cursor.fetchone()[0]).lower() != "on":
                raise CollectionFailure("PostgreSQL did not enter a read-only transaction")

            cursor.execute(SETTINGS_SQL)
            settings = _one(cursor)
            database_name = str(settings.pop("database_name"))
            cursor.execute(DATABASE_SQL)
            database_rows = _rows(cursor)
            if len(database_rows) != 1:
                raise CollectionFailure("pg_stat_database did not return the current database")
            cursor.execute(TABLE_SQL, (max_rows,))
            table_rows = _rows(cursor)
            cursor.execute(INDEX_SQL, (max_rows,))
            index_rows = _rows(cursor)

            activity, activity_error = _optional_query(cursor, ACTIVITY_SQL)
            if activity_error:
                warnings.append({"section": "database_backend_activity", "error": activity_error})
            locks, locks_error = _optional_query(cursor, LOCK_SQL)
            if locks_error:
                warnings.append({"section": "lock_waits", "error": locks_error})

            explain_rows: list[dict[str, Any]] = []
            for fingerprint in fingerprints:
                name, query = fingerprint["name"], fingerprint["sql"]
                cursor.execute("SAVEPOINT perf_explain")
                try:
                    cursor.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {query}")
                    raw = cursor.fetchone()[0]
                    if isinstance(raw, str):
                        raw = json.loads(raw)
                    explain_rows.append({
                        "endpoint": name,
                        "sql_fingerprint_sha256": hashlib.sha256(_normalize_sql(query).encode("utf-8")).hexdigest(),
                        "status": "collected",
                        **_plan_fingerprint(raw),
                    })
                except Exception as error:
                    explain_rows.append({"endpoint": name, "status": "unavailable", "error": _safe_error(error)})
                    cursor.execute("ROLLBACK TO SAVEPOINT perf_explain")
                finally:
                    try:
                        cursor.execute("RELEASE SAVEPOINT perf_explain")
                    except Exception:
                        pass

            # The transaction is explicitly rolled back: read-only evidence never commits.
            result = {
                "database": {
                    "name_sha256": hashlib.sha256(database_name.encode("utf-8")).hexdigest(),
                    "server_version_num": settings.pop("server_version_num"),
                    "settings": settings,
                    "pg_stat_database": _database_stats(database_rows[0]),
                },
                "user_tables": [_table_stats(row) for row in table_rows],
                "user_indexes": [_index_stats(row) for row in index_rows],
                "database_backend_activity": activity or {"status": "unavailable"},
                "lock_waits": locks or {"status": "unavailable"},
                "application_pool": dict(pool),
                "approved_explain": explain_rows,
                "warnings": warnings,
            }
            connection.rollback()
            return result
    except CollectionFailure:
        try:
            connection.rollback()
        except Exception:
            pass
        raise
    except Exception as error:
        try:
            connection.rollback()
        except Exception:
            pass
        raise CollectionFailure("a required read-only PostgreSQL query failed") from error
    finally:
        try:
            connection.close()
        except Exception:
            pass


def _artifact_bytes(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_artifacts(output: Path, document: Mapping[str, Any]) -> str:
    """Write immutable JSON and a standard sha256 sidecar without overwriting."""
    receipt = output.with_name(output.name + ".sha256")
    if output.exists() or receipt.exists():
        raise CollectionRefusal("refusing to overwrite an existing measurement artifact or receipt")
    output.parent.mkdir(parents=True, exist_ok=True)
    content = _artifact_bytes(document)
    digest = hashlib.sha256(content).hexdigest()
    try:
        with output.open("xb") as stream:
            stream.write(content)
        with receipt.open("x", encoding="ascii", newline="\n") as stream:
            stream.write(f"{digest}  {output.name}\n")
    except BaseException:
        output.unlink(missing_ok=True)
        receipt.unlink(missing_ok=True)
        raise
    return digest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", required=True, help="approved non-production target, e.g. dev-nov")
    parser.add_argument("--operator", required=True, help="short operator/team identifier")
    parser.add_argument("--ack", required=True, help=argparse.SUPPRESS)
    parser.add_argument("--database-url-env", default="FMUC_PERF_DATABASE_URL", metavar="NAME")
    parser.add_argument("--output", required=True, type=Path, help="new redacted JSON path")
    parser.add_argument("--fingerprints", type=Path, help="reviewed JSON manifest of approved SELECT fingerprints")
    parser.add_argument("--pool-metrics-file", type=Path, help="optional redacted numeric application-pool snapshot")
    parser.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS)
    parser.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        target, operator = validate_gate(args.target, args.operator, args.ack)
        if not ENV_PATTERN.fullmatch(args.database_url_env):
            raise CollectionRefusal("database URL environment name is invalid")
        if not 1 <= args.max_rows <= MAX_ROWS:
            raise CollectionRefusal(f"max-rows must be between 1 and {MAX_ROWS}")
        if not 100 <= args.timeout_ms <= MAX_TIMEOUT_MS:
            raise CollectionRefusal(f"timeout-ms must be between 100 and {MAX_TIMEOUT_MS}")
        output = validate_artifact_path(args.output)
        fingerprints = load_fingerprints(args.fingerprints)
        pool = load_pool_snapshot(args.pool_metrics_file)
        raw_url = os.environ.get(args.database_url_env)
        if not raw_url:
            raise CollectionRefusal(f"set the selected database URL environment variable: {args.database_url_env}")
        database_url = validate_database_url(raw_url)
        metrics = collect_database(database_url, args.max_rows, args.timeout_ms, fingerprints, pool)
        document = {
            "schema_version": SCHEMA_VERSION,
            "collector": "fmuc.postgres_perf_measure",
            "status": "read_only_snapshot",
            "target": target,
            "operator": operator,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "limits": {"max_rows": args.max_rows, "timeout_ms": args.timeout_ms, "max_explains": MAX_EXPLAINS},
            "transaction": {"read_only": True, "writes": 0},
            "metrics": metrics,
            "observability_boundaries": {
                "physical_disk": {"status": "not_collected", "reason_code": "OS_COUNTERS_OUT_OF_SCOPE"},
                "database_counters": {"status": "database_wide", "not_per_process": True},
                "backend_activity": {"status": "database_backend_snapshot", "not_os_process_attribution": True},
                "application_pool": {"status": pool.get("status"), "not_inferred_from_postgresql": True},
            },
        }
        digest = write_artifacts(output, document)
        print(json.dumps({"status": "written", "json": str(output), "sha256": digest,
                          "receipt": str(output.with_name(output.name + ".sha256"))}, ensure_ascii=True))
        return 0
    except CollectionRefusal as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 2
    except CollectionFailure as error:
        print(f"NOT_RUN: {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
