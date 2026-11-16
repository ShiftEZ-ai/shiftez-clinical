"""Static, CI-aware performance budget contract.

This module deliberately does not start the API, a browser, PostgreSQL, or a
benchmark fixture.  It validates the budget contract and binds it to the
existing benchmark interfaces.  Hosted/runtime qualification is a separate
receipt owned by Performance.
"""

from __future__ import annotations

import ast
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

import pytest


PASS = "PASS"
FAIL = "FAIL"
NOT_RUN = "NOT_RUN"
SLOW_REQUEST_SECONDS = 3.0
REQUIRED_PHASES = ("command_writer", "end_to_end")

# The HTTP allowance is explicit and intentionally separate from the existing
# seven-times coverage allowance used by pure-Python scale tests.
CI_VARIANCE_MULTIPLIERS = {
    "native": 1.0,
    "ci": 1.25,
    "ci_coverage": 7.0,
}


@dataclass(frozen=True)
class EvidenceRow:
    key: str
    source: str
    observation: str
    owner: str


@dataclass(frozen=True)
class PerformanceBudget:
    key: str
    mode: str
    surface: str
    p50_seconds: float
    p95_seconds: float
    max_payload_bytes: int
    max_duplicate_requests: int
    source_row: str
    owner: str
    required_phases: tuple[str, ...] = REQUIRED_PHASES

    def limits(self, runner: str = "native") -> dict[str, float]:
        try:
            multiplier = CI_VARIANCE_MULTIPLIERS[runner]
        except KeyError as exc:
            raise ValueError(f"unknown CI variance class: {runner}") from exc
        return {
            "p50_seconds": self.p50_seconds * multiplier,
            "p95_seconds": self.p95_seconds * multiplier,
        }


# Recorded measurements are separate from acceptance thresholds.
EVIDENCE_ROWS = (
    EvidenceRow("warm-fairness", "",
                "warm Fairness data: 0.35 s", "Performance"),
    EvidenceRow("warm-rule-map", "",
                "warm rule-map data: 0.31 s", "Performance"),
    EvidenceRow("warm-fairness-shell", "",
                "warm Fairness shell: 119, 60, 56 ms (median 60 ms; worst 119 ms)",
                "Performance"),
    EvidenceRow("cold-fairness", "",
                "distribution data 10.262–14.698 s; 502 after 27.629–27.878 s",
                "Performance"),
    EvidenceRow("cold-fairness-summary", "",
                "Fairness goals reached 69.1 s in one run; 502s followed 27–28 s",
                "Performance"),
    EvidenceRow("cold-december-summary", "",
                "cold December summary: 56–65 s", "Performance"),
    EvidenceRow("cold-december-read", "",
                "December full read 3.237 s / 430,595 B; shifts part 1.524 s / 41,799 B",
                "Performance"),
    EvidenceRow("gateway-failure", "",
                "gateway failures at 27.6 s (observed 27.629–27.878 s)", "Performance"),
    EvidenceRow("command-writer", "",
                "command 4.841 s / 7,940 B; fill-options 17.577 s / 7,180 B; rule-map 12.653 s / 37,693 B",
                "Performance"),
    EvidenceRow("cold-rules", "",
                "cold rules catalogue 12.4–15.3 s; warm 0.27–1.05 s",
                "Performance"),
)


# A budget row is a diagnostic guard as well as a target.  A cold row may
# document a known defect, but a slow or failed sample still cannot qualify.
BUDGETS = (
    PerformanceBudget("fairness.warm.data", "warm", "data", 0.35, 0.35, 100_000, 0,
                       "warm-fairness", "Performance"),
    PerformanceBudget("fairness.warm.shell", "warm", "shell", 0.060, 0.119, 100_000, 0,
                       "warm-fairness-shell", "Performance"),
    PerformanceBudget("rule-map.warm.data", "warm", "data", 0.31, 0.31, 50_000, 0,
                       "warm-rule-map", "Performance"),
    PerformanceBudget("fairness.cold.data", "cold", "data", 14.0, 14.0, 200_000, 0,
                       "cold-fairness", "Performance"),
    PerformanceBudget("december-summary.cold.data", "cold", "data", 56.0, 65.0, 450_000, 0,
                       "cold-december-summary", "Performance"),
    # This row is a failure sentinel: 5xx is never converted into a latency pass.
    PerformanceBudget("gateway-failure.cold.data", "cold", "data", 27.6, 27.6, 450_000, 0,
                       "gateway-failure", "Performance"),
    PerformanceBudget("command-write.cold.data", "cold", "data", 3.0, 3.0, 12_000, 0,
                       "command-writer", "Performance"),
)


def _percentile(values: Iterable[float], percentile: float) -> float:
    """Linear percentile with deterministic one-sample behaviour."""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("percentile requires at least one value")
    if not 0 <= percentile <= 100:
        raise ValueError("percentile must be between 0 and 100")
    position = (len(ordered) - 1) * percentile / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def assess_budget(
    budget: PerformanceBudget,
    samples: Iterable[Mapping[str, object]],
    *,
    runner: str = "native",
    hosted_available: bool = True,
) -> dict[str, object]:
    """Assess metadata-only observations; absence and errors fail closed.

    Each sample is one phase of one request and must include status, timing,
    payload size and a phase-qualified request key.  No response body is read.
    """
    rows = [dict(sample) for sample in samples]
    if not hosted_available:
        return {"status": NOT_RUN, "reason": "hosted/runtime unavailable"}
    if not rows:
        return {"status": NOT_RUN, "reason": "no observations"}

    failures: list[str] = []
    phases: dict[str, list[float]] = {phase: [] for phase in budget.required_phases}
    payloads: list[int] = []
    keys_by_phase: dict[str, list[object]] = {phase: [] for phase in budget.required_phases}
    slow_requests = 0
    for index, row in enumerate(rows):
        phase = row.get("phase")
        if phase not in phases:
            failures.append(f"sample {index}: missing/unknown phase")
            continue
        status = row.get("status")
        if not isinstance(status, int) or not 200 <= status < 300:
            # Includes every 5xx, specifically 502 and 504; status is checked
            # before timing so an error can never qualify as a fast response.
            failures.append(f"sample {index}: HTTP status is not a 2xx")
        seconds = row.get("seconds")
        payload_bytes = row.get("payload_bytes")
        request_key = row.get("request_key")
        if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
            failures.append(f"sample {index}: invalid seconds")
        else:
            phases[phase].append(float(seconds))
            if float(seconds) > SLOW_REQUEST_SECONDS:
                slow_requests += 1
        if not isinstance(payload_bytes, int) or payload_bytes < 0:
            failures.append(f"sample {index}: invalid payload_bytes")
        else:
            payloads.append(payload_bytes)
        if not isinstance(request_key, str) or not request_key:
            failures.append(f"sample {index}: missing request_key")
        else:
            keys_by_phase[phase].append(request_key)

    missing = [phase for phase, values in phases.items() if not values]
    failures.extend(f"missing required phase: {phase}" for phase in missing)
    duplicate_requests = sum(len(keys) - len(set(keys)) for keys in keys_by_phase.values())
    if duplicate_requests > budget.max_duplicate_requests:
        failures.append(f"duplicate requests: {duplicate_requests}")
    if payloads and max(payloads) > budget.max_payload_bytes:
        failures.append(f"payload exceeds {budget.max_payload_bytes} bytes")
    if slow_requests:
        failures.append(f"request over {SLOW_REQUEST_SECONDS:g} s: {slow_requests}")

    limits = budget.limits(runner)
    percentiles: dict[str, dict[str, float]] = {}
    for phase, values in phases.items():
        if not values:
            continue
        percentiles[phase] = {
            "p50_seconds": _percentile(values, 50),
            "p95_seconds": _percentile(values, 95),
        }
        if percentiles[phase]["p50_seconds"] > limits["p50_seconds"]:
            failures.append(f"{phase} p50 exceeds budget")
        if percentiles[phase]["p95_seconds"] > limits["p95_seconds"]:
            failures.append(f"{phase} p95 exceeds budget")

    return {
        "status": PASS if not failures else FAIL,
        "p50_p95": percentiles,
        "slow_requests_over_3s": slow_requests,
        "duplicate_requests": duplicate_requests,
        "max_payload_bytes": max(payloads) if payloads else None,
        "limits": limits,
        "failures": failures,
    }


def _function_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _sample(status: int = 200, seconds: float = 0.2, *, phase: str = "command_writer",
            key: str = "request-1", payload_bytes: int = 100) -> dict[str, object]:
    return {"status": status, "seconds": seconds, "phase": phase,
            "request_key": key, "payload_bytes": payload_bytes}


def test_budget_rows_bind_live_evidence_and_explicit_ci_variance():
    by_key = {row.key: row for row in EVIDENCE_ROWS}
    assert by_key["warm-fairness"].observation.endswith("0.35 s")
    assert by_key["warm-rule-map"].observation.endswith("0.31 s")
    assert by_key["cold-december-summary"].observation.endswith("56–65 s")
    assert "27.629–27.878 s" in by_key["gateway-failure"].observation
    assert CI_VARIANCE_MULTIPLIERS == {"native": 1.0, "ci": 1.25, "ci_coverage": 7.0}
    return locals()


@pytest.mark.cpu_budget
def test_budget_rows_bind_ci_p95_seconds():
    state = test_budget_rows_bind_live_evidence_and_explicit_ci_variance()
    assert all(budget.limits("ci")["p95_seconds"] >= budget.p95_seconds
               for budget in state['BUDGETS'])


def test_budget_rows_are_separate_warm_cold_shell_data_and_owned():
    evidence_keys = {row.key for row in EVIDENCE_ROWS}
    assert len({budget.key for budget in BUDGETS}) == len(BUDGETS)
    assert {budget.mode for budget in BUDGETS} == {"warm", "cold"}
    assert {budget.surface for budget in BUDGETS} == {"shell", "data"}
    assert all(budget.p50_seconds <= budget.p95_seconds and budget.max_payload_bytes > 0
               and budget.max_duplicate_requests == 0 and budget.owner
               and budget.source_row for budget in BUDGETS)
    assert all(budget.source_row in evidence_keys for budget in BUDGETS)
    assert all(next(row for row in EVIDENCE_ROWS if row.key == budget.source_row).owner == budget.owner
               for budget in BUDGETS)
    assert all(budget.required_phases == REQUIRED_PHASES for budget in BUDGETS)


def test_existing_fixture_and_benchmark_interfaces_are_bound_without_running_them():
    root = Path(__file__).resolve().parent
    required = {
        "test_rule_map.py": {"department_map_measurement", "test_large_department_mapping_time",
                              "test_large_department_mapping_latency"},
        "test_scheduler_performance.py": {"eighty_people_month", "live_solution_department_month",
                                           "test_live_solution_department_month_event_cpu_budget"},
    }
    for filename, names in required.items():
        assert names <= _function_names(root / filename), filename


def test_percentiles_track_writer_and_end_to_end_and_keep_fast_success():
    budget = next(row for row in BUDGETS if row.key == "fairness.warm.data")
    observations = [
        _sample(seconds=0.20, phase="command_writer", key="a:writer"),
        _sample(seconds=0.25, phase="command_writer", key="b:writer"),
        _sample(seconds=0.30, phase="command_writer", key="c:writer"),
        _sample(seconds=0.20, phase="end_to_end", key="a:e2e"),
        _sample(seconds=0.25, phase="end_to_end", key="b:e2e"),
        _sample(seconds=0.30, phase="end_to_end", key="c:e2e"),
    ]
    result = assess_budget(budget, observations, runner="ci")
    assert result["status"] == PASS
    assert result["p50_p95"]["command_writer"]["p50_seconds"] == pytest.approx(0.25)
    assert result["p50_p95"]["end_to_end"]["p95_seconds"] == pytest.approx(0.295)


@pytest.mark.parametrize("status", [500, 502, 504])
def test_5xx_is_never_a_latency_pass(status):
    budget = next(row for row in BUDGETS if row.key == "fairness.warm.data")
    observations = [_sample(status=status, seconds=0.001, phase=phase, key=f"a:{phase}")
                    for phase in REQUIRED_PHASES]
    result = assess_budget(budget, observations)
    assert result["status"] == FAIL
    assert result["failures"]


def test_slow_payload_and_duplicate_limits_are_reported_as_failures():
    budget = next(row for row in BUDGETS if row.key == "rule-map.warm.data")
    observations = [
        _sample(seconds=3.001, phase="command_writer", key="same", payload_bytes=budget.max_payload_bytes + 1),
        _sample(seconds=0.2, phase="end_to_end", key="same"),
        _sample(seconds=0.2, phase="end_to_end", key="same"),
    ]
    result = assess_budget(budget, observations)
    assert result["status"] == FAIL
    assert result["slow_requests_over_3s"] == 1
    assert result["duplicate_requests"] == 1
    assert result["max_payload_bytes"] == budget.max_payload_bytes + 1


def test_missing_hosted_or_runtime_evidence_is_not_run():
    budget = BUDGETS[0]
    result = assess_budget(budget, [], hosted_available=False)
    assert result == {"status": NOT_RUN, "reason": "hosted/runtime unavailable"}
    assert assess_budget(budget, [], hosted_available=True)["status"] == NOT_RUN


def test_unknown_ci_variance_class_fails_closed():
    with pytest.raises(ValueError, match="unknown CI variance class"):
        BUDGETS[0].limits("unclassified-runner")
