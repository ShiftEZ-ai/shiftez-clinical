"""Find a small necessary capacity conflict; integer CP-SAT always confirms LP hints."""
import math
import time
from collections import defaultdict
from datetime import timedelta
from zoneinfo import ZoneInfo

from ortools.linear_solver import pywraplp
from ortools.sat.python import cp_model
from .time import aware


def linear_rows(model, groups, references):
    rows = defaultdict(list)
    for key, indexes in groups.items():
        for index in indexes:
            constraint = model.proto.constraints[index]
            ref = references[index]
            if (constraint.enforcement_literal or not (ref.get('rule_kind') == 'minimum_cover'
                    or ref.get('rule_kind') == 'block_quota' and ref.get('comparison') == 'maximum')
                    or not constraint.has_linear() or len(constraint.linear.domain) != 2):
                continue
            rows[key].append(index)
    return dict(rows)


def compact(model, indexes, control, deadline):
    """Copy exact domains and unconditional linear rows, omitting every other constraint."""
    result = cp_model.CpModel()
    variables = {}
    for index in indexes:
        control.check()
        if time.monotonic() >= deadline:
            return None, {}
        source = model.proto.constraints[index]
        target = result.proto.constraints.add()
        target.copy_from(source)
        target.linear.vars.clear()
        for var in source.linear.vars:
            if var not in variables:
                variables[var] = len(result.proto.variables)
                result.proto.variables.add().copy_from(model.proto.variables[var])
            target.linear.vars.append(variables[var])
    return result, variables


def lp(model, indexes, remaining, objective=None, *, control):
    deadline = time.monotonic() + remaining
    solver = pywraplp.Solver.CreateSolver('GLOP')
    variables = {}

    def variable(index):
        if index not in variables:
            source = model.proto.variables[index]
            variables[index] = solver.NumVar(source.domain[0], source.domain[len(source.domain) - 1], source.name)
        return variables[index]

    for index in indexes:
        control.check()
        if time.monotonic() >= deadline:
            return solver.NOT_SOLVED, None
        source = model.proto.constraints[index].linear
        row = solver.RowConstraint(source.domain[0], source.domain[len(source.domain) - 1], str(index))
        for var, coefficient in zip(source.vars, source.coeffs):
            row.SetCoefficient(variable(var), coefficient)
    if objective:
        for index in objective:
            solver.Objective().SetCoefficient(variable(index), 1)
        solver.Objective().SetMaximization()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return solver.NOT_SOLVED, None
    solver.SetTimeLimit(max(1, math.ceil(remaining * 1000)))
    status = solver.Solve()
    return status, solver.Objective().Value() if status == solver.OPTIMAL else None


def _reduce(model, groups, enabled, control, deadline):
    size = max(1, len(enabled) // 2)
    while enabled:
        ordered = sorted(enabled)
        for offset in range(0, len(ordered), size):
            control.check()
            if time.monotonic() >= deadline:
                return enabled
            trial = enabled - set(ordered[offset:offset + size])
            indexes = [i for key in sorted(trial) for i in groups[key]]
            status, _ = lp(model, indexes, deadline - time.monotonic(), control=control)
            if status == pywraplp.Solver.INFEASIBLE:
                enabled = trial
        if size == 1:
            return enabled
        size = max(1, size // 2)
    return enabled


def _confirm(model, indexes, control, deadline):
    reduced, _ = compact(model, indexes, control, deadline)
    if reduced is None:
        return False
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    solver.parameters.max_time_in_seconds = max(0, deadline - time.monotonic())
    return control.solve(solver, reduced, 'diagnostics') == cp_model.INFEASIBLE


def capacity_core(model, groups, references, control, deadline):
    rows = linear_rows(model, groups, references)
    if not rows or time.monotonic() >= deadline:
        return None
    enabled = set(rows)
    indexes = lambda keys: [i for key in sorted(keys) for i in rows[key]]
    status, _ = lp(model, indexes(enabled), deadline - time.monotonic(), control=control)
    if status != pywraplp.Solver.INFEASIBLE:
        return None
    # Retain the first integer proof before spending any allowance on optional reduction.
    selected = {key: rows[key] for key in sorted(enabled)} if _confirm(
        model, indexes(enabled), control, (time.monotonic() + deadline) / 2) else None
    reduced = _reduce(model, rows, enabled, control, (time.monotonic() + deadline) / 2)
    if (selected is None or reduced != enabled) and _confirm(model, indexes(reduced), control, deadline):
        selected = {key: rows[key] for key in sorted(reduced)}
    if selected is None:
        return None
    occurrence_indexes = [i for key in selected for i in selected[key]]
    # Keep the first confirmed set if occurrence refinement cannot be confirmed in the remaining allowance.
    occurrences = {i: [i] for i in occurrence_indexes}
    refined = _reduce(model, occurrences, set(occurrences), control, (time.monotonic() + deadline) / 2)
    if refined != set(occurrences) and time.monotonic() < deadline and _confirm(model, sorted(refined), control, deadline):
        selected = defaultdict(list)
        for index in sorted(refined):
            selected[references[index]['key']].append(index)
    return list(selected), selected


def arithmetic(model, groups, references, data, control, deadline):
    """Report a certified upper bound for disjoint session floors in a quota window."""
    indexes = [i for rows in groups.values() for i in rows]
    caps = [i for i in indexes if references[i].get('rule_kind') == 'block_quota'
            and references[i].get('comparison') == 'maximum' and references[i].get('metric') == 'duties'
            and model.proto.constraints[i].has_linear() and len(model.proto.constraints[i].linear.domain) == 2
            and not model.proto.constraints[i].enforcement_literal]
    floors = [i for i in indexes if references[i].get('rule_kind') == 'minimum_cover']
    windows = sorted({(references[i].get('starts_on'), references[i].get('ends_on')) for i in caps})
    zone = ZoneInfo(data.get('organisation', {}).get('timezone', 'UTC'))
    day = lambda value: aware(value).astimezone(zone).date()
    for start, end in windows:
        control.check()
        if time.monotonic() >= deadline or start is None or end is None:
            continue
        selected, variables, sessions, required = [], set(), set(), 0
        for index in floors:
            ref, constraint = references[index], model.proto.constraints[index]
            if (constraint.enforcement_literal or not constraint.has_linear() or len(constraint.linear.domain) != 2
                    or not ref.get('start') or not start <= day(ref['start']).isoformat() < end
                    or len(ref.get('session_ids', [])) != 1 or sessions.intersection(ref['session_ids'])):
                continue
            row = constraint.linear
            upper = row.domain[len(row.domain) - 1]
            if upper >= 0 or any(c != -1 for c in row.coeffs) or variables.intersection(row.vars):
                continue
            selected.append(index)
            sessions.update(ref['session_ids'])
            variables.update(row.vars)
            required -= upper
        if not selected:
            continue
        status, upper = lp(model, caps, deadline - time.monotonic(), variables, control=control)
        if status != pywraplp.Solver.OPTIMAL or upper is None or upper >= required:
            continue
        bound = math.floor(upper + 1e-6)
        check, mapping = compact(model, caps, control, deadline)
        if check is None:
            break
        for var in sorted(variables - mapping.keys()):
            mapping[var] = len(check.proto.variables)
            check.proto.variables.add().copy_from(model.proto.variables[var])
        check.add(sum(check.get_int_var_from_proto_index(mapping[var]) for var in variables) >= bound + 1)
        solver = cp_model.CpSolver()
        solver.parameters.num_workers = 1
        solver.parameters.max_time_in_seconds = max(0, deadline - time.monotonic())
        if control.solve(solver, check, 'diagnostics') != cp_model.INFEASIBLE:
            continue
        refs = [references[i] for i in selected]
        first, last = min(day(ref['start']) for ref in refs), max(day(ref['start']) for ref in refs)
        return {'required_places': required, 'maximum_possible': bound, 'complete': True,
                'basis': 'confirmed_capacity_upper_bound', 'session_ids': sorted(sessions),
                'person_ids': sorted({pid for ref in refs for pid in ref.get('person_ids', [])}),
                'starts_on': first.isoformat(), 'ends_on': (last + timedelta(days=1)).isoformat(),
                'message': f"{len(sessions)} sessions from {first.isoformat()} to {last.isoformat()} "
                           f"need {required} places; these caps allow at most {bound}."}
    return {'required_places': None, 'maximum_possible': None, 'complete': False,
            'basis': 'not_established', 'session_ids': [], 'person_ids': [],
            'message': 'These named requirements conflict; a staffing total has not been established.'}
