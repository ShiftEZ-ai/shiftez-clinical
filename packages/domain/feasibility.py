"""Read-only comparison of current and proposed rules on immutable snapshots."""

from .candidate_evidence import focused_candidate_people
from .time import aware, now as clock_now
import copy
import json
import time
from collections import defaultdict
from bisect import bisect_left

from packages.contracts.schemas import RuleCheckComparison
from .cp_sat import generate
from .solver_rules import COMPILER_VERSION
from .validation import evaluate, finding_observations
from .finding_identity import finding_index, finding_substance, recited_findings
from .generation import citation_finding_key


def _problem(row):
    """Keep semantic evidence, excluding presentation, timing and candidate drill-downs."""
    ignored = {'message', 'key', 'accepted', 'checked_at', 'elapsed_seconds', 'input_fingerprint',
               'boundary', 'candidates', 'available_person_ids', 'capability_name', 'requirement_name', 'name',
               'cause', 'fix_hints', 'proration', 'display', 'causes', 'timing', 'people', 'sessions'}
    if isinstance(row, dict):
        semantic = {k: _problem(v) for k, v in row.items() if k not in ignored}
        if row.get('code') == 'CONFLICTING_REQUIREMENTS':
            semantic['requirements'] = sorted(semantic.get('requirements', []),
                                              key=lambda v: json.dumps(v, sort_keys=True))
        return semantic
    if isinstance(row, list):
        return [_problem(v) for v in row]
    return row


def _comparison_parts(row, *, aggregate_coverage=False, citation_sensitive=False):
    semantic = _problem(row)
    detail = semantic.get('detail') or {}
    detail.pop('derived_findings', None)
    aggregate = (aggregate_coverage and semantic.get('code') == 'SCOPED_COVERAGE_GAP'
                 and not detail.get('affected_shift_ids'))
    if aggregate:
        # A scoped generation may move an already-authorised unfilled place to a
        # different session. Compare that bounded deficit as one rule budget;
        # required/available measures still reject a larger or deeper shortfall.
        semantic.pop('shift_id', None)
        semantic.pop('start', None)
        semantic.pop('end', None)
    measures = []
    if semantic.get('code') in {'QUOTA_BELOW_MINIMUM', 'QUOTA_ABOVE_MAXIMUM'} and {'amount', 'actual'} <= detail.keys():
        direction = 1 if semantic['code'] == 'QUOTA_BELOW_MINIMUM' else -1
        measures.append((direction * detail.pop('amount'), direction * detail.pop('actual')))
        detail.pop('difference', None)
    for values in (semantic, detail):
        for required, available, direction in (('required', 'available', 1), ('required', 'available_upper_bound', 1),
                                              ('required_hours', 'actual_hours', 1), ('maximum', 'scheduled', -1),
                                              ('maximum', 'actual', -1), ('capacity', 'scheduled_sessions', -1),
                                              ('trainees', 'qualified_supervisors', 1)):
            if required in values and available in values:
                measures.append((direction * values.pop(required), direction * values.pop(available)))
                break
    if 'detail' in semantic:
        semantic['detail'] = detail or None
    interval = None
    if (measures and not aggregate and semantic.get('code') in {
            'COVERAGE_GAP', 'SCOPED_COVERAGE_GAP', 'CAPABILITY_GAP', 'DISTINCT_PERSON_GAP', 'LEAD_GAP',
            'MAXIMUM_PARTICIPANTS', 'SCOPED_PARTICIPANT_LIMIT', 'SUPERVISION_GAP', 'SUPERVISOR_CAPACITY',
            'SUPERVISOR_SCOPED_CAPACITY'} and semantic.get('start') and semantic.get('end')):
        start, end = aware(semantic['start']), aware(semantic['end'])
        if start < end:
            interval = start, end
            semantic.pop('start')
            semantic.pop('end')
    key = citation_finding_key(semantic) if citation_sensitive else json.dumps(semantic, sort_keys=True)
    return key, measures, interval


def compare_findings(baseline, proposed, *, severities=('error',), aggregate_coverage=False,
                     protect_intervals=True):
    """Compare observed problems; matching evidence never proves full feasibility."""
    required = {'findings', 'diagnostics', 'candidate_evidence'}
    complete = all(check.get('status') in {'candidate_feasible', 'requirements_conflict'}
                   and required <= check.keys() for check in (baseline, proposed))
    observations, boundaries = [], defaultdict(set)
    indexes = [{k: f for k, f in finding_index(check).items() if f.get('severity') in severities}
               for check in (baseline, proposed)]
    for check, indexed in zip((baseline, proposed), indexes):
        rows = []
        findings = ([f for f in finding_observations(check) if f.get('severity') in severities]
                    if protect_intervals else indexed.values())
        for finding, is_finding in [*((f, True) for f in findings),
                                    *((d, False) for d in check.get('diagnostics', []))]:
            substance = finding_substance(finding) if is_finding else finding
            key, measures, interval = _comparison_parts(substance, aggregate_coverage=aggregate_coverage)
            if is_finding and protect_intervals and (interval or aggregate_coverage):
                # Normalize measures/windows, but keep every fixed-rule constraint and its multiplicity.
                constraint, _, _ = _comparison_parts(finding, aggregate_coverage=aggregate_coverage,
                                                     citation_sensitive=True)
                key = (key, constraint)
            rows.append((finding, key, measures, interval))
            if interval:
                boundaries[key].update(interval)
        observations.append(rows)
    boundaries = {key: sorted(values) for key, values in boundaries.items()}
    before = defaultdict(list)
    new, unchanged = [], 0
    for rows, previous in zip(observations, (True, False)):
        for finding, key, measures, interval in rows:
            if finding.get('proven') is False or finding.get('implementation_error'):
                complete = False
            windows = [(None, None)]
            if interval:
                # Assignment changes can split or merge the same constraint's observed intervals.
                points = boundaries[key]
                start, end = (bisect_left(points, at) for at in interval)
                windows = zip(points[start:end], points[start + 1:end + 1])
            changes = []
            for window in windows:
                matches = before[key, window]
                if previous:
                    matches.append(measures)
                    continue
                match = next((i for i, old in enumerate(matches) if len(old) == len(measures)
                              and all(r <= old_r and a >= old_a for (r, a), (old_r, old_a) in zip(measures, old))), None)
                if match is not None:
                    matches.pop(match)
                else:
                    changes.append('worse' if matches else 'new')
            if previous:
                continue
            if not changes:
                unchanged += 1
            else:
                new.append({**{k: v for k, v in finding.items() if k != 'candidates'},
                            'change': 'new' if 'new' in changes else 'worse'})
    pools = []
    identities = []
    for check in (baseline, proposed):
        counts = defaultdict(int)
        rows = check.get('candidate_evidence', [])
        ids = {(row['shift_id'], row['person_id']) for row in rows}
        complete = complete and len(ids) == len(rows)
        identities.append(ids)
        for row in rows:
            counts[row['shift_id']] += int(row['eligible_and_available'])
        pools.append(counts)
    complete = complete and identities[0] == identities[1]
    for shift_id, available in pools[0].items():
        if pools[1].get(shift_id, 0) < available:
            new.append({'code': 'FEWER_ELIGIBLE_CANDIDATES', 'severity': 'error', 'shift_id': shift_id,
                        'available_before': available, 'available_after': pools[1].get(shift_id, 0),
                        'change': 'worse', 'message': 'Fewer eligible and available people can work this session.'})
    return RuleCheckComparison(new_or_worse_findings=new, unchanged_existing_count=unchanged,
                               comparison_complete=complete, no_new_problems=complete and not new,
                               recited=recited_findings(*indexes)).model_dump()


def unchanged_input_issues(checks):
    """An unrelated rule may be reviewed without certifying an already invalid rota."""
    baseline, proposed = (checks.get(key, {}) for key in ('baseline', 'proposed'))
    if not (baseline.get('status') == proposed.get('status') == 'invalid_inputs'
            and baseline.get('issues') and proposed.get('issues')):
        return False
    return sorted(json.dumps(i, sort_keys=True) for i in baseline['issues']) == sorted(
        json.dumps(i, sort_keys=True) for i in proposed['issues'])


def check(snapshot, *, progress=None, cancelled=None):
    started = time.monotonic()
    results = {}
    compare_problems = not snapshot['bindings'] or any('fact_id' not in row for row in snapshot['bindings'])
    targets = {binding['policy_id'] for binding in snapshot['bindings']}
    line_check = any(p.get('definition', {}).get('rule', {}).get('kind') == 'catalogue' and
        (not targets or p['id'] in targets) for p in snapshot['proposed'].get('policies', []))
    evaluations_complete = True
    evaluations = {}
    for phase in ('baseline', 'proposed'):
        data = copy.deepcopy(snapshot[phase])
        if data['workspace']['status'] == 'published':
            for shift in data['shifts']:
                shift['locked'] = True
        def report(update):
            if progress:
                progress({**update, 'check_phase': phase})
        attempts = []
        for seconds in (snapshot['check_seconds'], snapshot.get('second_pass_seconds')):
            if seconds is None or attempts and seconds <= snapshot['check_seconds']:
                break
            began = time.monotonic()
            result = generate(data, seconds, workers=1,
                              progress=report, cancelled=cancelled, feasibility_only=True)
            attempts.append({'limit_seconds': seconds, 'status': result['status'],
                             'solver_status': result.get('solver_status'), 'elapsed_seconds': time.monotonic() - began})
            if result['status'] != 'unknown_time_limit':
                break
        result['search_attempts'] = attempts
        # Check receipts cannot be applied as assignment proposals.
        result.pop('additions', None)
        result.pop('removals', None)
        results[phase] = result
        if result['status'] == 'cancelled':
            return {'status': 'cancelled', 'checks': results}
        if result['status'] == 'candidate_feasible':
            result.setdefault('diagnostics', [])
        if compare_problems and (result['status'] in {'candidate_feasible', 'requirements_conflict'} or
                                line_check and result['status'] == 'unknown_time_limit') and 'shifts' in data:
            evaluation = evaluate(data)
            evaluations[phase] = evaluation
            result['findings'] = evaluation['findings']
            evaluations_complete = evaluations_complete and (evaluation.get('quota_report') or {}).get('complete', True)
        else:
            evaluations_complete = False
    baseline, proposed = (results[p]['status'] for p in ('baseline', 'proposed'))
    comparison = RuleCheckComparison(new_or_worse_findings=[], unchanged_existing_count=0,
                                    comparison_complete=False, no_new_problems=False).model_dump()
    if compare_problems:
        comparison = compare_findings(results['baseline'], results['proposed'], protect_intervals=False)
        for phase, result in results.items():
            data = snapshot[phase]
            expected = {(shift['id'], pid) for shift in data.get('shifts', [])
                        for pid in focused_candidate_people(data, shift)}
            actual = {(row['shift_id'], row['person_id']) for row in result.get('candidate_evidence', [])}
            context = data.get('quota_context')
            if (expected != actual or result['status'] == 'requirements_conflict' and not result.get('diagnostics')
                    or context is not None and (context.get('complete') is not True or context.get('limit_exceeded'))):
                comparison.update(comparison_complete=False, no_new_problems=False)
        # A changed quota window can load different external context; that is not a same-input comparison.
        non_rules = lambda data: {k: v for k, v in data.items() if k not in {
            'policies', 'scheduling_facts', 'quota_context', 'fingerprint', 'input_manifest'}}
        if not evaluations_complete or non_rules(snapshot['baseline']) != non_rules(snapshot['proposed']):
            comparison.update(comparison_complete=False, no_new_problems=False)
    feasible = proposed == 'candidate_feasible'
    conflict = proposed == 'requirements_conflict'
    protected_conflicts = []
    if conflict and snapshot['proposed']['workspace']['status'] == 'published':
        before = {p['id']: p for p in snapshot['baseline']['policies']}
        changed = {p['id'] for p in snapshot['proposed']['policies']
                   if p['id'] not in before or p['definition'] != before[p['id']]['definition']
                   or p['status'] != before[p['id']]['status']}
        if changed:
            # Published duties cannot move. The shared validator gives exact
            # rule-specific intervals where a proposed limit conflicts with them.
            findings = results['proposed'].get('findings')
            if findings is None:
                findings = evaluate(snapshot['proposed'])['findings']
            protected_conflicts = [f for f in findings
                                   if f['severity'] == 'error' and f['rule_id'] in changed]
    outcome = ('feasible' if feasible else 'requirements_conflict' if conflict else
               'implementation_error' if proposed in {'model_invalid', 'translation_error'} else
               'configuration_needs_attention' if proposed in {'invalid_inputs', 'unsupported_rules', 'unsupported_size'} else
               'inconclusive')
    effect = {'status': 'unresolved'}
    if len(evaluations) == 2:
        from .rule_effects import observed_effect
        effect = observed_effect(evaluations['baseline'], evaluations['proposed'])
    return {'status': outcome,
        'effect': effect,
        **comparison,
        'checks': results, 'bindings': snapshot['bindings'],
        'input_fingerprint': snapshot['fingerprint'], 'compiler_version': COMPILER_VERSION,
        'checked_at': clock_now().isoformat(),
        'elapsed_seconds': time.monotonic() - started,
        'search_seconds_per_check': snapshot['check_seconds'],
        'newly_introduced_contradiction': baseline == 'candidate_feasible' and conflict,
        'pre_existing_conflict': baseline == 'requirements_conflict',
        'pre_existing_input_issues': unchanged_input_issues(results),
        'conflict_resolved': baseline == 'requirements_conflict' and feasible,
        'full_feasibility_established': feasible,
        'protected_assignment_conflicts': protected_conflicts,
        'boundary': 'Results apply only to these inputs. An inconclusive check is not proof of feasibility or impossibility.'}
