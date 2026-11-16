"""Bounded QA load harness for the Rota API.

This is an operator-run harness, not a seeder and not a deployment tool.  It
accepts a redacted journey plan, uses only an existing QA session, and fails
closed before making a request when the plan is not demonstrably for 2027
non-December data.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit

import httpx


MAX_USERS = 200
MAX_DURATION_SECONDS = 600
MAX_STAGE_SECONDS = 60
MAX_RATE_RPS = 20.0
MAX_WRITE_OPERATIONS = 20
DEFAULT_TIMEOUT_SECONDS = 10.0
OFF_PEAK_GATE = "OFF_PEAK_QA_2027"
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
READ_METHODS = frozenset({"GET", "HEAD"})
STOP_STATUSES = frozenset({500, 502, 503, 504})
DATE_TOKEN = re.compile(r"(?<!\d)(20\d{2})[-_/]?(0[1-9]|1[0-2])[-_/]?([0-3]\d)(?!\d)")
YEAR_MONTH_TOKEN = re.compile(r"(?<!\d)(20\d{2})[-_/]?(0[1-9]|1[0-2])(?!\d)")
SECRET_TOKEN = re.compile(
    r"(?i)(bearer\s+|authorization\s*[:=]\s*|cookie\s*[:=]\s*|token\s*[:=]\s*)[^\s,;]+"
)
UUID_TOKEN = re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f-]{27,}\b")
LONG_HEX = re.compile(r"(?i)\b[0-9a-f]{24,}\b")


class SafetyRefusal(ValueError):
    """A request that must be refused before any network activity."""


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _sha(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _redact_text(value: str) -> str:
    value = SECRET_TOKEN.sub(r"\1[REDACTED]", value)
    value = UUID_TOKEN.sub("[UUID]", value)
    value = LONG_HEX.sub("[HEX]", value)
    return value[:160]


def _guard_qa_text(value: str, field: str) -> None:
    lowered = value.lower()
    if "december" in lowered or re.search(r"\bdec\b", lowered):
        raise SafetyRefusal(f"{field} names December data")
    for match in DATE_TOKEN.finditer(value):
        year, month = int(match.group(1)), int(match.group(2))
        if year != 2027:
            raise SafetyRefusal(f"{field} contains a non-2027 date")
        if month == 12:
            raise SafetyRefusal(f"{field} contains a December date")
    for match in YEAR_MONTH_TOKEN.finditer(value):
        year, month = int(match.group(1)), int(match.group(2))
        if year != 2027:
            raise SafetyRefusal(f"{field} contains a non-2027 year/month")
        if month == 12:
            raise SafetyRefusal(f"{field} contains a December year/month")


def guard_qa_value(value: Any, field: str = "plan") -> None:
    """Reject December and non-2027 date material recursively."""

    if isinstance(value, str):
        _guard_qa_text(value, field)
    elif isinstance(value, Mapping):
        for key, child in value.items():
            guard_qa_value(child, f"{field}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            guard_qa_value(child, f"{field}[{index}]")


def _require_relative_path(path: Any, field: str) -> str:
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise SafetyRefusal(f"{field} must be an absolute API path, not an external URL")
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc:
        raise SafetyRefusal(f"{field} must not contain a host")
    _guard_qa_text(path, field)
    return path


def _require_https_origin(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise SafetyRefusal("--base-url must be an HTTPS origin without query or fragment")
    return value.rstrip("/")


def _method(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise SafetyRefusal(f"{field} must be an HTTP method")
    return value.upper()


def _journey(item: Any, kind: str, index: int) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise SafetyRefusal(f"{kind}[{index}] must be an object")
    name = item.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name):
        raise SafetyRefusal(f"{kind}[{index}].name is invalid")
    method = _method(item.get("method"), f"{kind}[{index}].method")
    allowed = READ_METHODS if kind == "reads" else WRITE_METHODS
    if method not in allowed:
        raise SafetyRefusal(f"{kind}[{index}] has an invalid method")
    path = _require_relative_path(item.get("path"), f"{kind}[{index}].path")
    budget = item.get("budget_ms", 2000 if kind == "reads" else 3000)
    if type(budget) is not int or not 1 <= budget <= 30_000:
        raise SafetyRefusal(f"{kind}[{index}].budget_ms must be 1..30000")
    params = item.get("params", {})
    payload = item.get("payload")
    if params is not None and not isinstance(params, Mapping):
        raise SafetyRefusal(f"{kind}[{index}].params must be an object")
    if kind == "reads" and payload is not None:
        raise SafetyRefusal(f"reads[{index}] may not carry a request payload")
    if kind == "writes":
        undo = item.get("undo")
        if not isinstance(undo, Mapping):
            raise SafetyRefusal(f"writes[{index}] must declare an undo operation")
        undo_method = _method(undo.get("method"), f"writes[{index}].undo.method")
        if undo_method not in WRITE_METHODS:
            raise SafetyRefusal(f"writes[{index}].undo.method must be a write method")
        _require_relative_path(undo.get("path"), f"writes[{index}].undo.path")
        if "payload" not in item or "payload" not in undo:
            raise SafetyRefusal(f"writes[{index}] and its undo must declare payloads")
    guard_qa_value(item, f"{kind}[{index}]")
    return dict(item)


def validate_plan(plan: Any) -> dict[str, Any]:
    """Validate a complete plan without touching the network."""

    if not isinstance(plan, Mapping):
        raise SafetyRefusal("plan must be a JSON object")
    qa_id = plan.get("qa_data_id")
    if not isinstance(qa_id, str) or not re.fullmatch(r"qa-2027-[A-Za-z0-9][A-Za-z0-9_.-]{1,100}", qa_id):
        raise SafetyRefusal("qa_data_id must be a qa-2027-* identifier")
    qa_date = plan.get("qa_date")
    if not isinstance(qa_date, str):
        raise SafetyRefusal("qa_date is required")
    try:
        parsed = datetime.strptime(qa_date, "%Y-%m-%d").date()
    except ValueError as error:
        raise SafetyRefusal("qa_date must be YYYY-MM-DD") from error
    if parsed.year != 2027 or parsed.month == 12:
        raise SafetyRefusal("qa_date must be a non-December 2027 date")
    guard_qa_value(plan)
    reads = plan.get("reads")
    writes = plan.get("writes", [])
    if not isinstance(reads, list) or not reads:
        raise SafetyRefusal("plan must define at least one read journey")
    if not isinstance(writes, list) or len(writes) > MAX_WRITE_OPERATIONS:
        raise SafetyRefusal(f"writes must contain at most {MAX_WRITE_OPERATIONS} operations")
    result = dict(plan)
    result["reads"] = [_journey(item, "reads", index) for index, item in enumerate(reads)]
    result["writes"] = [_journey(item, "writes", index) for index, item in enumerate(writes)]
    telemetry = plan.get("telemetry")
    if not isinstance(telemetry, Mapping):
        raise SafetyRefusal("plan.telemetry is required for saturation stop protection")
    _require_relative_path(telemetry.get("path"), "plan.telemetry.path")
    for key, default in (("cpu_percent", 85.0), ("pool_percent", 90.0)):
        value = telemetry.get(key, default)
        if not isinstance(value, (int, float)) or not 1 <= float(value) <= 100:
            raise SafetyRefusal(f"plan.telemetry.{key} must be 1..100")
    return result


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return round(ordered[low], 3)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (position - low), 3)


@dataclass
class Observation:
    kind: str
    journey: str
    method: str
    path_hash: str
    status: int | None
    latency_ms: float | None
    request_payload_bytes: int
    response_bytes: int
    error: str | None = None


@dataclass
class RunState:
    observations: list[Observation] = field(default_factory=list)
    telemetry: list[dict[str, float]] = field(default_factory=list)
    stop_reason: str | None = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    write_pending: dict[str, Any] | None = None
    writes_completed: int = 0
    undos_completed: int = 0

    def stop(self, reason: str) -> None:
        if self.stop_reason is None:
            self.stop_reason = reason
        self.stop_event.set()


class RateGate:
    def __init__(self, rate: float):
        self.interval = 1.0 / rate
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next - now)
            self._next = max(now, self._next) + self.interval
        if delay:
            await asyncio.sleep(delay)


def _path_with_params(path: str, params: Mapping[str, Any] | None) -> str:
    if not params:
        return path
    encoded = urlencode(sorted((str(key), str(value)) for key, value in params.items()))
    return path + ("&" if "?" in path else "?") + encoded


def _numeric(payload: Any, *names: str) -> float | None:
    if not isinstance(payload, Mapping):
        return None
    candidates: list[Any] = [payload]
    for key in ("metrics", "system", "database", "db"):
        if isinstance(payload.get(key), Mapping):
            candidates.append(payload[key])
    for candidate in candidates:
        for name in names:
            value = candidate.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
    return None


def _pool_percent(payload: Any) -> float | None:
    direct = _numeric(payload, "pool_percent", "db_pool_percent", "pool_used_percent")
    if direct is not None:
        return direct
    if not isinstance(payload, Mapping):
        return None
    candidates = [payload]
    for key in ("metrics", "database", "db", "pool"):
        if isinstance(payload.get(key), Mapping):
            candidates.append(payload[key])
    for candidate in candidates:
        used = candidate.get("pool_in_use", candidate.get("in_use"))
        capacity = candidate.get("pool_capacity", candidate.get("capacity"))
        if isinstance(used, (int, float)) and isinstance(capacity, (int, float)) and capacity > 0:
            return float(used) * 100 / float(capacity)
    return None


async def _request(
    client: httpx.AsyncClient,
    base_url: str,
    spec: Mapping[str, Any],
    kind: str,
    state: RunState,
    rate_gate: RateGate | None,
    timeout_seconds: float,
    force: bool = False,
) -> Observation:
    if rate_gate is not None:
        await rate_gate.wait()
    path = _path_with_params(str(spec["path"]), spec.get("params"))
    payload = spec.get("payload") if kind in {"write", "undo"} else None
    request_size = len(_json_bytes(payload)) if payload is not None else 0
    started = time.perf_counter()
    status: int | None = None
    response_bytes = 0
    error: str | None = None
    try:
        response = await client.request(
            str(spec["method"]),
            base_url.rstrip("/") + path,
            params=None,
            json=payload,
            timeout=timeout_seconds,
        )
        status = response.status_code
        response_bytes = len(response.content)
        if status in STOP_STATUSES or status >= 500:
            error = f"http_{status}"
            state.stop(error)
    except httpx.TimeoutException:
        error = "request_timeout"
        state.stop(error)
    except httpx.HTTPError as exc:
        error = type(exc).__name__
        state.stop("transport_error")
    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    budget = int(spec.get("budget_ms", 2000 if kind == "read" else 3000))
    if latency_ms > budget and not force:
        error = "latency_budget_exceeded"
        state.stop(f"{kind}:{spec['name']}:latency_budget_exceeded")
    observation = Observation(
        kind=kind,
        journey=str(spec["name"]),
        method=str(spec["method"]),
        path_hash=_sha(path),
        status=status,
        latency_ms=latency_ms,
        request_payload_bytes=request_size,
        response_bytes=response_bytes,
        error=error,
    )
    state.observations.append(observation)
    return observation


async def _telemetry_loop(
    client: httpx.AsyncClient,
    base_url: str,
    plan: Mapping[str, Any],
    state: RunState,
    deadline: float,
) -> None:
    telemetry = plan["telemetry"]
    while not state.stop_event.is_set() and time.monotonic() < deadline:
        try:
            response = await client.get(base_url.rstrip("/") + str(telemetry["path"]), timeout=DEFAULT_TIMEOUT_SECONDS)
            if response.status_code >= 500:
                state.stop(f"telemetry_http_{response.status_code}")
                return
            payload = response.json()
            cpu = _numeric(payload, "cpu_percent", "cpu")
            pool = _pool_percent(payload)
            if cpu is None or pool is None:
                state.stop("telemetry_missing_cpu_or_pool")
                return
            sample = {"cpu_percent": round(cpu, 3), "pool_percent": round(pool, 3)}
            state.telemetry.append(sample)
            if cpu >= float(telemetry.get("cpu_percent", 85.0)):
                state.stop("cpu_saturation")
                return
            if pool >= float(telemetry.get("pool_percent", 90.0)):
                state.stop("db_pool_saturation")
                return
        except (httpx.HTTPError, ValueError, json.JSONDecodeError):
            state.stop("telemetry_unavailable")
            return
        await asyncio.sleep(1.0)


async def _user_loop(
    user_id: int,
    client: httpx.AsyncClient,
    base_url: str,
    reads: list[Mapping[str, Any]],
    state: RunState,
    rate_gate: RateGate,
    deadline: float,
) -> None:
    while not state.stop_event.is_set() and time.monotonic() < deadline:
        for spec in reads:
            if state.stop_event.is_set() or time.monotonic() >= deadline:
                return
            await _request(client, base_url, spec, "read", state, rate_gate, DEFAULT_TIMEOUT_SECONDS)


async def _write_phase(
    client: httpx.AsyncClient,
    base_url: str,
    writes: list[Mapping[str, Any]],
    repetitions: int,
    state: RunState,
) -> None:
    for spec in writes:
        for _ in range(repetitions):
            if state.stop_event.is_set():
                return
            state.write_pending = None
            result = await _request(client, base_url, spec, "write", state, None, DEFAULT_TIMEOUT_SECONDS)
            if result.status is None or result.status >= 500:
                # A timeout/5xx has unknown commit state; use the declared idempotent undo.
                state.write_pending = dict(spec["undo"])
                return
            if result.status >= 400:
                return
            state.writes_completed += 1
            state.write_pending = dict(spec["undo"])
            undo_result = await _request(client, base_url, state.write_pending, "undo", state, None, DEFAULT_TIMEOUT_SECONDS, force=True)
            state.write_pending = None
            if undo_result.status is None or undo_result.status >= 400:
                state.stop("undo_failed")
                return
            state.undos_completed += 1


def _journey_summary(observations: list[Observation]) -> dict[str, Any]:
    grouped: dict[str, list[Observation]] = {}
    for observation in observations:
        grouped.setdefault(f"{observation.kind}:{observation.journey}", []).append(observation)
    result: dict[str, Any] = {}
    for key, items in sorted(grouped.items()):
        latencies = [item.latency_ms for item in items if item.latency_ms is not None]
        result[key] = {
            "requests": len(items),
            "errors": sum(item.error is not None or (item.status is not None and item.status >= 400) for item in items),
            "statuses": {str(status): sum(item.status == status for item in items) for status in sorted({item.status for item in items if item.status is not None})},
            "payload_bytes": {"request": sum(item.request_payload_bytes for item in items), "response": sum(item.response_bytes for item in items)},
            "latency_ms": {"p50": percentile(latencies, 0.50), "p95": percentile(latencies, 0.95), "p99": percentile(latencies, 0.99)},
        }
    return result


async def run_phase(plan: Mapping[str, Any], args: argparse.Namespace, phase: str) -> dict[str, Any]:
    state = RunState()
    deadline = time.monotonic() + args.duration_seconds
    ramp = [count for count in args.ramp if count <= args.users]
    if not ramp or ramp[-1] != args.users:
        ramp.append(args.users)
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    async with httpx.AsyncClient(headers=headers, verify=True, follow_redirects=False) as client:
        telemetry_task = asyncio.create_task(_telemetry_loop(client, args.base_url, plan, state, deadline))
        workers: list[asyncio.Task[None]] = []
        rate_gate = RateGate(args.rate_rps)
        current = 0
        try:
            for target in ramp:
                for user_id in range(current, target):
                    workers.append(asyncio.create_task(_user_loop(user_id, client, args.base_url, plan["reads"], state, rate_gate, deadline)))
                current = target
                await asyncio.sleep(min(args.stage_seconds, max(0.0, deadline - time.monotonic())))
                if state.stop_event.is_set() or time.monotonic() >= deadline:
                    break
            await asyncio.gather(*workers, return_exceptions=True)
            if args.include_writes and not state.stop_event.is_set():
                await _write_phase(client, args.base_url, plan["writes"], args.write_repetitions, state)
        finally:
            if state.write_pending is not None:
                await _request(client, args.base_url, state.write_pending, "undo", state, None, DEFAULT_TIMEOUT_SECONDS, force=True)
                state.write_pending = None
            state.stop_event.set()
            await telemetry_task
    if state.stop_reason is None:
        state.stop_reason = "duration_complete"
    errors = sum(item.error is not None or (item.status is not None and item.status >= 400) for item in state.observations)
    writes_balanced = state.writes_completed == state.undos_completed
    report = {
        "schema": "fmuc.performance.load200.v1",
        "phase": phase,
        "qa_data_id_sha256": _sha(plan["qa_data_id"]),
        "qa_date": plan["qa_date"],
        "users": {"requested": args.users, "ramp": ramp, "max_allowed": MAX_USERS},
        "budgets": {"duration_seconds": args.duration_seconds, "stage_seconds": args.stage_seconds, "rate_rps": args.rate_rps},
        "stop_reason": state.stop_reason,
        "passed": state.stop_reason == "duration_complete" and errors == 0 and writes_balanced,
        "errors": errors,
        "observations": len(state.observations),
        "journeys": _journey_summary(state.observations),
        "telemetry": {"samples": state.telemetry, "thresholds": {"cpu_percent": plan["telemetry"].get("cpu_percent", 85.0), "pool_percent": plan["telemetry"].get("pool_percent", 90.0)}},
        "writes": {"enabled": args.include_writes, "completed": state.writes_completed, "undone": state.undos_completed, "planned_repetitions": args.write_repetitions},
        "redaction": {"request_paths": "sha256 only", "payloads": "byte counts only", "credentials": "omitted"},
        "revision": args.revision or None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return report


def write_receipt(report: Mapping[str, Any], directory: Path, stem: str) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    canonical = dict(report)
    receipt_hash = _sha(canonical)
    output = dict(canonical)
    output["receipt_sha256"] = receipt_hash
    json_path = directory / f"{stem}.json"
    sha_path = directory / f"{stem}.sha256"
    json_path.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sha_path.write_text(f"{receipt_hash}  {json_path.name}\n", encoding="ascii")
    return json_path, sha_path


def _parse_ramp(value: str) -> list[int]:
    try:
        values = [int(part) for part in value.split(",") if part.strip()]
    except ValueError as error:
        raise SafetyRefusal("ramp must be comma-separated integers") from error
    if not values or values[0] < 1 or any(a >= b for a, b in zip(values, values[1:])):
        raise SafetyRefusal("ramp must be strictly increasing positive integers")
    if values[-1] > MAX_USERS:
        raise SafetyRefusal(f"ramp cannot exceed {MAX_USERS} users")
    return values


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--base-url", help="deployed HTTPS origin")
    parser.add_argument("--users", type=int, default=1)
    parser.add_argument("--ramp", default="1,10,25,50,100,150,200")
    parser.add_argument("--duration-seconds", type=int, default=120)
    parser.add_argument("--stage-seconds", type=int, default=15)
    parser.add_argument("--rate-rps", type=float, default=10.0)
    parser.add_argument("--phase", choices=("warm", "cold-restart", "both"), default="warm")
    parser.add_argument("--restart-proof", type=Path)
    parser.add_argument("--operator-gate", required=True)
    parser.add_argument("--include-writes", action="store_true")
    parser.add_argument("--write-repetitions", type=int, default=1)
    parser.add_argument("--token-env", default="FMUC_LOAD200_TOKEN")
    parser.add_argument("--output-dir", type=Path, default=Path("reports/PERF-LOAD200"))
    parser.add_argument("--revision")
    parser.add_argument("--validate", action="store_true", help="validate plan and gates without network activity")
    args = parser.parse_args(argv)
    if args.operator_gate != OFF_PEAK_GATE:
        raise SafetyRefusal(f"operator gate must be exactly {OFF_PEAK_GATE}")
    if not 1 <= args.users <= MAX_USERS:
        raise SafetyRefusal(f"users must be 1..{MAX_USERS}")
    if not 1 <= args.duration_seconds <= MAX_DURATION_SECONDS:
        raise SafetyRefusal(f"duration-seconds must be 1..{MAX_DURATION_SECONDS}")
    if not 1 <= args.stage_seconds <= MAX_STAGE_SECONDS:
        raise SafetyRefusal(f"stage-seconds must be 1..{MAX_STAGE_SECONDS}")
    if not 0.1 <= args.rate_rps <= MAX_RATE_RPS:
        raise SafetyRefusal(f"rate-rps must be 0.1..{MAX_RATE_RPS}")
    if not 1 <= args.write_repetitions <= MAX_WRITE_OPERATIONS:
        raise SafetyRefusal(f"write-repetitions must be 1..{MAX_WRITE_OPERATIONS}")
    args.ramp = _parse_ramp(args.ramp)
    if args.ramp[-1] > args.users:
        args.ramp = [count for count in args.ramp if count < args.users] + [args.users]
    if args.phase in {"cold-restart", "both"}:
        if args.restart_proof is None:
            raise SafetyRefusal("cold-restart requires --restart-proof from the non-December restart")
        proof = json.loads(args.restart_proof.read_text(encoding="utf-8"))
        if proof.get("restarted") is not True:
            raise SafetyRefusal("restart proof must record restarted=true")
        guard_qa_value(proof, "restart_proof")
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv or sys.argv[1:])
        plan = validate_plan(json.loads(args.plan.read_text(encoding="utf-8")))
        if args.validate:
            print(json.dumps({"valid": True, "qa_data_id": "[REDACTED]", "phase": args.phase}, sort_keys=True))
            return 0
        if not args.base_url:
            raise SafetyRefusal("--base-url is required for a live run")
        args.base_url = _require_https_origin(args.base_url)
        token = os.environ.get(args.token_env)
        args.token = token
        if not token:
            raise SafetyRefusal(f"token environment variable {args.token_env} is unset")
        phases = ("warm", "cold-restart") if args.phase == "both" else (args.phase,)
        exit_code = 0
        for phase in phases:
            report = asyncio.run(run_phase(plan, args, phase))
            stem = f"load-200-{phase}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
            json_path, sha_path = write_receipt(report, args.output_dir, stem)
            print(json.dumps({"phase": phase, "report": str(json_path), "sha_receipt": str(sha_path), "stop_reason": report["stop_reason"]}, sort_keys=True))
            if not report["passed"]:
                exit_code = 3
                break
        return exit_code
    except (OSError, json.JSONDecodeError, SafetyRefusal) as error:
        print(f"REFUSED: {_redact_text(str(error))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
