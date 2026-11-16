"""Cooperative preparation checks and independent CP-SAT interruption."""
import threading
import time
from math import isfinite
from zoneinfo import ZoneInfo

from ortools.sat.python import cp_model

from .time import aware, now as utc_now


class Cancelled(Exception):
    pass


class TimeLimit(Exception):
    pass


class StopGraceExpired(TimeLimit):
    pass


class SolverControl:
    SCORE_INTERVAL_SECONDS = 2

    def __init__(self, progress=None, cancelled=None, *, search_control=None, checkpoint_solution=None):
        self.has_progress = progress is not None
        self.progress = progress or (lambda update: None)
        self.cancelled = cancelled or (lambda: False)
        self.started = time.monotonic()
        self.deadline = None
        self.reserve_seconds = 0
        self.phase_timings = {'preparation_seconds': 0, 'search_seconds': 0}
        self._phase, self._phase_started = 'preparation', self.started
        self.first_solution_seconds = None
        # Conservative admission estimate for optional improvement calls: includes search before the first
        # incumbent, not just fixed native startup. Keep the largest observed search-call sample.
        self.incumbent_latency_seconds = 0
        self.stalled = False
        self.stall_seconds = None
        self.fairness_plateau = False
        self.fairness_stall_seconds = None
        self.fairness_loss = None
        self.last_search = {}
        # Shown with every update: search budget, when the search began, which objective is being improved.
        self.context = {}
        self.search_control = search_control or (lambda: {})
        self.checkpoint_solution = checkpoint_solution
        self.search_budget = None
        self.search_spent = float((self.search_control() or {}).get('spent_seconds', 0))
        if not isfinite(self.search_spent) or self.search_spent < 0:
            raise ValueError('Persisted search duration must be finite and nonnegative')
        self.phase_timings['search_seconds'] = self.search_spent
        self._solve_started = None
        self.completion_budget = None
        self.completion_spent = 0
        self.diagnostic_deadline = None
        self.stopped = False
        self.stop_grace_seconds = None
        self.stop_deadline = None
        self.validated_result = None
        self._stage = 'checking'
        self.improvements_found = 0
        self._live_choices = None
        self._live_seq = 0
        self._last_live_at = None
        self.score_program = None
        self.score_options = None
        self.score_metrics = {'cpu_seconds': 0, 'evaluated': 0, 'published': 0, 'unavailable': 0}
        self._last_score_at = None
        self._score_lock = threading.Lock()
        self._score_pending = False
        self.last_solve_deterministic_time = None
        self.target_gap_percent = None
        self.target_score = None
        self.stop_by = None
        self._stop_by_zone = ZoneInfo('UTC')
        self._stop_by_date = None
        self.best_score = None
        self.stop_reason = None

    def configure_stop_conditions(self, options, timezone='UTC'):
        """Install the run's bounded, optional stop conditions from GenerationOptions."""
        self.stop_grace_seconds = options.generation_stop_grace_seconds
        self.target_gap_percent = options.target_gap_percent
        self.target_score = options.target_score
        self.stop_by = options.stop_by
        try:
            self._stop_by_zone = ZoneInfo(timezone or 'UTC')
        except Exception:
            self._stop_by_zone = ZoneInfo('UTC')
        self._stop_by_date = utc_now().astimezone(self._stop_by_zone).date() if self.stop_by else None

    def clock_stop_due(self):
        if self.stop_by is None or self._stop_by_date is None:
            return False
        local = utc_now().astimezone(self._stop_by_zone)
        hour, minute = (int(part) for part in self.stop_by.split(':'))
        target_minutes = hour * 60 + minute
        current_minutes = local.hour * 60 + local.minute
        return local.date() > self._stop_by_date or (local.date() == self._stop_by_date
            and current_minutes >= target_minutes and target_minutes < 24 * 60)

    def target_stop_reason(self, objective, best_bound):
        if self.target_gap_percent is not None and objective is not None and best_bound is not None:
            # CP-SAT minimizes the weighted objective.  Use the usual relative
            # incumbent/bound denominator and keep zero-valued objectives at a
            # zero gap (they are already at the best possible bound).
            objective, best_bound = float(objective), float(best_bound)
            if isfinite(objective) and isfinite(best_bound):
                denominator = max(1.0, abs(objective), abs(best_bound))
                gap = max(0.0, objective - best_bound) / denominator * 100
                if gap <= self.target_gap_percent:
                    return 'target_gap'
        if self.target_score is not None and self.best_score is not None and self.best_score >= self.target_score:
            return 'target_score'
        return None

    def mark_stop(self, reason):
        if reason == 'operator_stop' or self.stop_reason is None or (self.stop_reason == 'optimal' and reason != 'optimal'):
            self.stop_reason = reason

    def publish_score(self, sequence, score, *, final=False):
        if (not final and sequence != self.improvements_found) or self.cancelled():
            return False
        now = time.monotonic()
        if not final and self._last_score_at is not None and now - self._last_score_at < self.SCORE_INTERVAL_SECONDS:
            return False
        if isinstance(score, dict) and score.get('total') is not None:
            self.best_score = (score['total'] if final else
                               max(self.best_score, score['total']) if self.best_score is not None else score['total'])
        self.stage(self._stage, best_score=score)
        self._last_score_at = time.monotonic()
        return True

    def incumbent_score(self, solution):
        now = time.monotonic()
        if self.score_program is None or self._last_score_at is not None and now - self._last_score_at < self.SCORE_INTERVAL_SECONDS:
            return {}
        from .score import compiled_progress_score
        cpu = time.thread_time()
        score = compiled_progress_score(self.score_program, solution, self.score_options)
        self.score_metrics['cpu_seconds'] += time.thread_time() - cpu
        self.score_metrics['evaluated'] += 1
        if score is None:
            self.score_metrics['unavailable'] += 1
            return {'best_score': None}
        self.score_metrics['published'] += 1
        self.best_score = max(self.best_score, score['total']) if self.best_score is not None else score['total']
        return {'best_score': score}

    def live_solutions(self, choices, baseline, coverage, max_updates_per_second=3):
        """Read existing model choices; coverage uses the peak headcount deficit per session.

        Advisory warnings remain unknown until shared validation, rather than implying an unchecked zero.
        """
        self._live_choices = [(pair, variable.index) for pair, variable in choices.items()]
        self._live_previous = set(baseline)
        self._live_coverage = []
        for sid, expression, minimum in coverage:
            if isinstance(expression, int):
                self._live_coverage.append((sid, minimum, expression, []))
                continue
            flat = cp_model.FlatIntExpr(expression)
            self._live_coverage.append((sid, minimum, flat.offset,
                [(variable.index, coefficient) for variable, coefficient in zip(flat.vars, flat.coeffs)]))
        self._live_interval = 1 / min(8, max(1, max_updates_per_second))

    def live_event(self, solution, objective, bound, now):
        """Reuse the existing checkpoint list, after rate admission; skipped incumbents never become the baseline."""
        selected = {pair for pair, index in self._live_choices if solution[index]}
        adds, removes = selected - self._live_previous, self._live_previous - selected
        truncated = len(adds) + len(removes) > 200
        open_by_shift = {}
        for sid, minimum, offset, terms in self._live_coverage:
            covered = offset + sum(coefficient for index, coefficient in terms if solution[index])
            open_by_shift[sid] = max(open_by_shift.get(sid, 0), minimum - covered)
        self._live_previous = selected
        self._live_seq += 1
        self._last_live_at = now
        self.live_solution = {'seq': self._live_seq,
            'pairs': [{'shift_id': sid, 'person_id': pid} for sid, pid in sorted(selected)]}
        return {'seq': self._live_seq, 'at': utc_now().isoformat(), 'kind': 'improvement',
                'objective': objective, 'bound': bound,
                'adds': [] if truncated else [{'shift_id': sid, 'person_id': pid} for sid, pid in sorted(adds)],
                'removes': [] if truncated else [{'shift_id': sid, 'person_id': pid} for sid, pid in sorted(removes)],
                'open_places': sum(open_by_shift.values()), 'warnings': None, 'truncated': truncated}

    def poll(self):
        update = self.search_control() or {}
        if self.search_budget is not None and update.get('minutes') is not None:
            self.search_budget = update['minutes'] * 60
            self.context['time_limit_seconds'] = self.search_budget
        self.stopped = self.stopped or bool(update.get('stop'))
        if self.stopped:
            self.mark_stop('operator_stop')
            if self.stop_deadline is None and self.stop_grace_seconds is not None:
                age = (max(0, (utc_now() - aware(update['stop_requested_at'])).total_seconds())
                       if update.get('stop_requested_at') is not None else 0)
                self.stop_deadline = time.monotonic() + max(0, self.stop_grace_seconds - age)

    def check_stop_grace(self):
        if self.stop_deadline is not None and time.monotonic() >= self.stop_deadline:
            raise StopGraceExpired()

    def remaining(self, reserve=0):
        if self.search_budget is not None:
            self.poll()
            active = time.monotonic() - self._solve_started if self._solve_started is not None else 0
            return 0 if self.stopped else max(0, self.search_budget - self.search_spent - active)
        return float('inf') if self.deadline is None else max(0, self.deadline - time.monotonic() - reserve)

    def phase(self, name):
        now = time.monotonic()
        if self._phase:
            key = self._phase + '_seconds'
            if self._phase == 'search' and self.search_budget is not None:
                self.phase_timings[key] = self.search_spent
            else:
                self.phase_timings[key] = self.phase_timings.get(key, 0) + now - self._phase_started
        self._phase, self._phase_started = name, now

    def check(self):
        if self.cancelled():
            raise Cancelled()
        self.poll()
        self.check_stop_grace()
        if self.stopped and self.search_budget is None and self._phase == 'preparation':
            raise TimeLimit()
        if self.search_budget is None and self.remaining() <= 0:
            raise TimeLimit()

    def stage(self, stage, **details):
        self.check()
        self._stage = stage
        self.progress({**self.context, 'stage': stage, 'elapsed_seconds': time.monotonic() - self.started,
                       'improvements_found': self.improvements_found,
                       'first_solution_seconds': self.first_solution_seconds,
                       'search_elapsed_seconds': self.search_spent,
                       'phase_timings': self.timings(), **details})

    def timings(self):
        timings = dict(self.phase_timings)
        if self._phase:
            timings[self._phase + '_seconds'] = timings.get(self._phase + '_seconds', 0) + time.monotonic() - self._phase_started
        if self.search_budget is not None:
            active = time.monotonic() - self._solve_started if self._solve_started is not None else 0
            timings['search_seconds'] = self.search_spent + active
        return timings

    def solve(self, solver, model, tier):
        """Confirm every native infeasibility result without presolve, within the same budget."""
        self.last_solve_deterministic_time = 0.0
        parameters = getattr(solver, 'parameters', None)
        if tier == 'diagnostics' and self.diagnostic_deadline is not None:
            remaining = self.diagnostic_deadline - time.monotonic()
            if remaining <= 0:
                return cp_model.UNKNOWN
            parameters.max_time_in_seconds = min(parameters.max_time_in_seconds, remaining)
        wall_limit = getattr(parameters, 'max_time_in_seconds', float('inf'))
        deterministic_limit = getattr(parameters, 'max_deterministic_time', float('inf'))
        initial_budget, started = self.search_budget, time.monotonic()
        status = self._solve_once(solver, model, tier)
        if status != cp_model.INFEASIBLE:
            return status
        # OR-Tools 9.15 may falsely reject even an assumption model with default probing.
        # Deterministic confirmation uses work units; wall-clock calls retain their slice and 10-second cap.
        managed = self.search_budget is not None and isinstance(tier, int)
        extension = max(0, self.search_budget - initial_budget) if managed else 0
        deterministic = self.search_budget is None and isfinite(deterministic_limit)
        remaining = float('inf') if deterministic else min(10.0, wall_limit + extension - (time.monotonic() - started))
        if tier == 'diagnostics' and self.diagnostic_deadline is not None:
            remaining = min(remaining, self.diagnostic_deadline - time.monotonic())
        if managed or self.search_budget is None:
            remaining = min(remaining, self.remaining(self.reserve_seconds))
        if tier is None:
            remaining = min(remaining, max(0, self.completion_budget - self.completion_spent))
        deterministic_remaining = deterministic_limit - self.last_solve_deterministic_time
        if remaining <= 0 or deterministic_remaining <= 0:
            return cp_model.UNKNOWN
        presolve = parameters.cp_model_presolve
        try:
            parameters.cp_model_presolve = False
            parameters.max_time_in_seconds = remaining
            parameters.max_deterministic_time = deterministic_remaining
            return self._solve_once(solver, model, tier, extend_slice=False)
        finally:
            parameters.cp_model_presolve = presolve
            parameters.max_time_in_seconds = wall_limit
            parameters.max_deterministic_time = deterministic_limit

    def _solve_once(self, solver, model, tier, *, extend_slice=True):
        """Solve, reporting each better solution. With `self.stall_seconds` set, stop once a solution exists and
        the last `stall_seconds` gained less than 1%. Recent progress on unmet fairness extends
        that search through its own window. `self.stalled` tells the caller it stopped early.
        `tier=None` shares its first finite allowance across score completions, without changing search progress
        or plateau state."""
        self.check()
        if self.stopped:
            return cp_model.UNKNOWN
        searching = isinstance(tier, int)
        managed = self.search_budget is not None and searching
        slice_seconds = solver.parameters.max_time_in_seconds if managed else None
        initial_budget = self.search_budget
        if managed:
            if self.remaining() <= 0:
                return cp_model.UNKNOWN
            # The watcher enforces a live limit; a native fixed limit cannot be extended during solve().
            solver.parameters.max_time_in_seconds = float('inf')
        elif self.deadline is not None:
            remaining = self.remaining(self.reserve_seconds)
            if remaining <= 0:
                return cp_model.UNKNOWN
            solver.parameters.max_time_in_seconds = min(solver.parameters.max_time_in_seconds, remaining)
        completion_seconds = None
        if tier is None:
            completion_seconds = solver.parameters.max_time_in_seconds
            if not isfinite(completion_seconds) or completion_seconds < 0:
                raise ValueError('Score completion requires a finite nonnegative time allowance')
            if self.completion_budget is None:
                self.completion_budget = completion_seconds
            completion_seconds = min(completion_seconds, max(0, self.completion_budget - self.completion_spent))
            if completion_seconds <= 0:
                return cp_model.UNKNOWN
            solver.parameters.max_time_in_seconds = completion_seconds
        stall_seconds = self.stall_seconds if searching else None
        control = self
        if searching:
            self.stalled = False
            self.last_search = {}
        found = []  # (time, objective) of each better solution in this solve
        fairness = []
        stopped_by = []
        first_incumbent = []

        class Incumbent(cp_model.CpSolverSolutionCallback):
            def on_solution_callback(self):
                control.poll()
                if not searching:
                    if control.cancelled() or control.stopped:
                        self.stop_search()
                    return
                if not first_incumbent:
                    first_incumbent.append(time.monotonic() - solve_started)
                if control.first_solution_seconds is None:
                    control.first_solution_seconds = time.monotonic() - control.started
                found.append((time.monotonic(), self.objective_value))
                if control.fairness_loss is not None:
                    fairness.append((time.monotonic(), self.value(control.fairness_loss)))
                control.improvements_found += 1
                now = time.monotonic()
                emit = control._live_choices is not None and (control._last_live_at is None
                    or now - control._last_live_at >= control._live_interval)
                with control._score_lock:
                    score_due = control.score_program is not None and not control._score_pending and (
                        control._last_score_at is None or now - control._last_score_at >= control.SCORE_INTERVAL_SECONDS)
                    if score_due:
                        control._score_pending = True
                solution = list(self.response_proto.solution) if emit or control.checkpoint_solution is not None or score_due else None
                if control.checkpoint_solution is not None:
                    control.checkpoint_solution(solution)
                control.poll()
                if control.stopped:
                    self.stop_search()
                    return
                score_update = control.incumbent_score(solution) if score_due else {}
                target_reason = control.target_stop_reason(self.objective_value, self.best_objective_bound)
                live = {'events': [control.live_event(solution, self.objective_value, self.best_objective_bound, now)],
                        'solution': control.live_solution} if emit else {}
                # An incumbent satisfies the solver model; shared validation is later.
                control._stage = 'improving'
                control.progress({**control.context, 'stage': 'improving', 'tier': tier,
                    'elapsed_seconds': time.monotonic() - control.started,
                    'first_solution_seconds': control.first_solution_seconds,
                    'objective_value': self.objective_value, 'best_bound': self.best_objective_bound,
                    'improvements_found': control.improvements_found,
                    **live, **score_update,
                    'search_elapsed_seconds': control.timings()['search_seconds'],
                    'phase_timings': control.timings(),
                    'shared_validation_passed': False})
                if score_due:
                    with control._score_lock:
                        control._last_score_at = time.monotonic()
                        control._score_pending = False
                control.poll()
                if control.stopped:
                    self.stop_search()
                elif target_reason:
                    control.mark_stop(target_reason)
                    self.stop_search()
                elif control.cancelled():
                    self.stop_search()

        finished = threading.Event()
        def interrupt():
            last_progress = solve_started
            while not finished.wait(.1):
                self.poll()
                if self.cancelled():
                    # Repeat until completion: cancellation can race solver startup.
                    solver.stop_search()
                elif self.stopped:
                    solver.stop_search()
                elif tier == 'diagnostics' and self.diagnostic_deadline is not None and time.monotonic() >= self.diagnostic_deadline:
                    solver.stop_search()
                elif (managed or self.search_budget is None and tier is not None) and self.remaining(self.reserve_seconds) <= 0:
                    if not stopped_by:
                        reason = 'operator_stop' if self.stopped else 'time_limit'
                        stopped_by.append(reason)
                        control.mark_stop(reason)
                    solver.stop_search()
                elif managed and time.monotonic() - solve_started >= slice_seconds + (
                        max(0, self.search_budget - initial_budget) if extend_slice else 0):
                    if not stopped_by:
                        stopped_by.append('slice_limit')
                        control.mark_stop('slice_limit')
                    solver.stop_search()
                elif completion_seconds is not None and time.monotonic() - solve_started >= completion_seconds:
                    solver.stop_search()
                elif searching and control.clock_stop_due():
                    if not stopped_by:
                        stopped_by.append('stop_by')
                        control.mark_stop('stop_by')
                    solver.stop_search()
                elif stall_seconds and found and control.on_plateau(found, time.monotonic() - stall_seconds, fairness):
                    self.stalled = True
                    if not stopped_by:
                        stopped_by.append('plateau')
                        control.mark_stop('plateau')
                    solver.stop_search()
                if managed and time.monotonic() - last_progress >= 1:
                    last_progress = time.monotonic()
                    self.progress({**self.context, 'stage': self._stage, 'tier': tier,
                        'elapsed_seconds': last_progress - self.started,
                        'first_solution_seconds': self.first_solution_seconds,
                        'improvements_found': self.improvements_found,
                        'search_elapsed_seconds': self.timings()['search_seconds'],
                        'phase_timings': self.timings(), 'shared_validation_passed': False})

        solve_started = time.monotonic()
        if managed:
            self._solve_started = solve_started
        watcher = threading.Thread(target=interrupt, daemon=True)
        watcher.start()
        try:
            status = solver.solve(model, Incumbent())
            response = getattr(solver, 'response_proto', None)
            self.last_solve_deterministic_time += getattr(response, 'deterministic_time', 0.0)
        finally:
            finished.set()
            watcher.join()
            if managed:
                self.search_spent += time.monotonic() - solve_started
                self._solve_started = None
            elif completion_seconds is not None:
                self.completion_spent += time.monotonic() - solve_started
        self.check()
        if searching and self.fairness_plateau:
            ended = time.monotonic()
            def last_gain(rows):
                best, at, gains = None, None, 0
                for when, value in rows:
                    if best is None or value < best:
                        gains += int(best is not None)
                        best, at = value, when
                return ended - at if at is not None else None, gains, best
            age, gains, _ = last_gain(found)
            fair_changes = [(at, value - previous) for (_, previous), (at, value) in zip(fairness, fairness[1:]) if value != previous]
            fair_gains = [at for at, change in fair_changes if change < 0]
            fair_age = ended - fair_gains[-1] if fair_gains else None
            notice_window = self.fairness_stall_seconds or self.stall_seconds
            self.last_search = {
                'stop_reason': 'optimal' if status == cp_model.OPTIMAL else
                    stopped_by[0] if stopped_by else 'time_limit',
                'objective_improvement_age_seconds': age, 'objective_improvements': gains,
                'fairness_improvement_age_seconds': fair_age,
                'fairness_penalty': fairness[-1][1] if fairness else None,
                'fairness_improving': bool(fair_gains and fairness[-1][1] > 0 and fair_changes[-1][1] < 0
                    and notice_window and fair_age < notice_window),
            }
        if status == cp_model.OPTIMAL:
            self.mark_stop('optimal')
        elif stopped_by:
            self.mark_stop(stopped_by[0])
        elif searching:
            # A native FEASIBLE/UNKNOWN return without a watcher callback is
            # still a bounded search stop.  Do not leave the run's reason blank.
            self.mark_stop('time_limit')
        if first_incumbent:
            self.incumbent_latency_seconds = max(self.incumbent_latency_seconds, first_incumbent[0])
        return status

    def on_plateau(self, found, since, fairness=()):
        """Keep the existing weighted plateau, extended only by progress on unmet fairness.

        The first incumbent gets a fairness window. A lower penalty restarts it; a worsening
        opens a fresh repair window. Equal fairness values (including preference-only gains)
        never extend it. Zero penalty immediately returns to the ordinary plateau.
        """
        before = [value for at, value in found if at <= since]
        plateau = bool(before) and found[-1][1] >= before[-1] - abs(before[-1]) / 100
        if not plateau or not self.fairness_plateau or not fairness or fairness[-1][1] <= 0:
            return plateau
        if self.fairness_stall_seconds == 0:
            return False  # Explicit full allowance while fairness remains unmet.
        changed = [at for (_, previous), (at, value) in zip(fairness, fairness[1:]) if value != previous]
        return time.monotonic() - (changed[-1] if changed else fairness[0][0]) >= self.fairness_stall_seconds
