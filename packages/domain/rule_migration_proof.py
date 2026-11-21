"""Fail-closed catalogue-v2 proof over the same engine and exact source identities.

Unlike the historical grouping proof, this retains duplicates, all finding details,
quota/fairness identities and every optimization tier. Reports contain digests, not
staff names; the authorized source snapshot remains the inspectable evidence.
"""
import copy
import hashlib
import json
import platform
from collections import Counter

from ortools import __version__ as solver_version

from .conditions import active_rules, counting_views
from .score import rule_copy_key, schedule_score
from .validation import evaluate
from .solver_control import SolverControl

VERSION = 'catalogue-v2-proof-1'
BOUND_TEMPLATE = 'objective <= best + int(best * tolerance_percent / 100); enforced priority_bound:tier'


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), default=str)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def comparison(before, after, *, multiset=False):
    """Compare complete values, without renaming source ids or discarding fields."""
    a, b = (Counter(map(encoded, rows)) for rows in (before, after)) if multiset else (encoded(before), encoded(after))
    out = {'equal': a == b, 'before_digest': digest(a if not multiset else sorted(a.items())),
           'after_digest': digest(b if not multiset else sorted(b.items()))}
    if multiset:
        out.update(count_before=sum(a.values()), count_after=sum(b.values()),
                   only_before=[{'digest': digest(key), 'count': count} for key, count in sorted((a - b).items())],
                   only_after=[{'digest': digest(key), 'count': count} for key, count in sorted((b - a).items())])
    return out


def _retained_rules(data, retirement_cutoff):
    resolved = active_rules(data['policies'], context=data)
    if not retirement_cutoff:
        return resolved
    from .rule_catalogue_migration import lines_of
    from .rule_lines import line_validity
    cutoff = min(str(retirement_cutoff), str(data.get('workspace', {}).get('starts_on') or retirement_cutoff))
    retained = []
    for policy in resolved:
        if '_catalogue_sources' in policy:
            # Catalogue provenance already carries the resolver's effective line and quota dates.
            source = policy['_catalogue_sources'].get(f"{policy['id']}:{policy['template_line']['id']}")
            if source and source.get('ends_on') and str(source['ends_on']) <= cutoff:
                continue
            policy = {**policy, '_catalogue_sources': {key: value for key, value in policy['_catalogue_sources'].items()
                if not value.get('ends_on') or str(value['ends_on']) > cutoff}}
        else:
            # Retirement needs dates, not rerouting an already-resolved Count scope.
            entry, lines = lines_of({**policy, 'definition': {**policy['definition'],
                'listed_only': False, 'per_person': [], 'scope': {}}}, validate=False)
            projected = {**policy, 'definition': {'rule': {'id': entry}}}
            if lines and all(line_validity(projected, line, cutoff)['effective'] == 'ended' for line in lines):
                continue
        retained.append(policy)
    return retained


def strict_expanded(before, after, *, retirement_cutoff=None):
    """Resolved primitive and Count copies, retaining full provenance and multiplicity.

    The v2 adapter preserves the original source partitions exactly. Comparing the
    complete effective copies is stronger than atomizing/merging their date spans.
    An explicit retirement cutoff omits ended history, bounded by the rota's start.
    """
    def copies(data):
        resolved = _retained_rules(data, retirement_cutoff)
        # The resolver's private idempotency guard carries no source or clinical value.
        return [{k: v for k, v in p.items() if k != '_v2_source'}
                for p in counting_views({**data, 'policies': resolved}, resolved)]
    return comparison(copies(before), copies(after), multiset=True)


def canonical_full_model(data, *, cancelled=None):
    """Capture the production compiler's hard constraints and complete ordered tiers.

    Variable indices retain their construction order and occurrence identity, also
    checking deterministic tie-breaking. Sorting constraints preserves duplicates.
    No feasibility-only model or successful search is substituted for compilation.
    """
    from . import cp_sat
    control = SolverControl(cancelled=cancelled)
    control.check()
    captured = []

    def capture(model, tiers):
        objectives = []
        for name, penalties, tolerance in tiers:
            control.check()
            tier = model.clone()
            tier.minimize(sum(penalties))
            invalid = tier.validate()
            if invalid:
                raise ValueError('Invalid full-objective proof model: ' + invalid)
            bounds = []
            for best in (-1_000_003, -1, 0, 1, 1_000_003):
                control.check()
                bounded = model.clone()
                flag = bounded.new_bool_var(f'priority_bound:{len(objectives)}')
                bounded.proto.variables[flag.index].domain.clear()
                bounded.proto.variables[flag.index].domain.extend([1, 1])
                bounded.add(sum(penalties) <= cp_sat.objective_bound(best, tolerance)).only_enforce_if(flag)
                bounds.append([best, str(bounded.proto.variables[flag.index]),
                               [str(c) for c in list(bounded.proto.constraints)[len(model.proto.constraints):]]])
            objectives.append({'name': name, 'objective': str(tier.proto.objective),
                               'tolerance_percent': tolerance, 'bound_template': BOUND_TEMPLATE, 'bound_probes': bounds})
        captured.append({'variables': [str(v) for v in model.proto.variables],
                         'constraints': sorted(str(c) for c in model.proto.constraints),
                         'objectives': objectives, 'model_metadata': str(model.proto)})

    result = cp_sat.generate(copy.deepcopy(data), 5, 1, deterministic=True, _on_objectives=capture, cancelled=cancelled)
    control.check()
    if result['status'] != 'model_ready':
        return {'status': result['status'], 'ready': False, 'issues_digest': digest(result.get('issues', result.get('message')))}
    [model] = captured
    return {'status': 'model_ready', 'ready': True, 'digest': digest(model),
            'variables': len(model['variables']), 'constraints': len(model['constraints']),
            'tiers': model['objectives']}


def same_full_model(before, after):
    return {'equal': before['ready'] and after['ready'] and before['digest'] == after['digest'],
            'before': before, 'after': after}


def _solve(before, after, seconds, *, cancelled=None):
    from . import cp_sat, generation

    def result(value):
        comparable = {k: value.get(k) for k in ('status', 'additions', 'removals', 'objectives', 'optimality_proven',
                                                'best_bound', 'objective_value', 'issues', 'diagnostics')}
        # The compiler's own complete validation is part of the candidate evidence.
        comparable['validation'] = {k: v for k, v in (value.get('validation') or {}).items() if k != 'fingerprint'}
        return comparable

    solvers = {
        'cp_sat': lambda d: cp_sat.generate(copy.deepcopy(d), seconds, 1, deterministic=True, cancelled=cancelled),
        'greedy': lambda d: generation.generate(copy.deepcopy(d), seconds, cancelled=cancelled, deterministic=True),
    }
    out = {}
    for name, run in solvers.items():
        a, b = (run(d) for d in (before, after))
        inconclusive = any(str(x.get('status')).lower() in ('unknown', 'unknown_time_limit', 'cancelled') for x in (a, b))
        part = comparison(result(a), result(b))
        part.update(status_before=a.get('status'), status_after=b.get('status'),
                    conclusive=not inconclusive, equal=None if inconclusive else part['equal'])
        if name == 'greedy':
            part['not_applied'] = a['not_applied']
        if name == 'cp_sat':
            def status(value):
                if value.get('status') == 'candidate_feasible':
                    return 'OPTIMAL' if value.get('optimality_proven') else 'FEASIBLE'
                return {'requirements_conflict': 'INFEASIBLE', 'unknown_time_limit': 'UNKNOWN'}.get(value.get('status'), value.get('status'))
            part.update(solver_status_before=status(a), solver_status_after=status(b),
                        objectives_before=a.get('objectives'), objectives_after=b.get('objectives'))
        out[name] = part
    return out


def strict_proof(before, after, *, model=True, solve=False, seconds=5, cancelled=None, retirement_cutoff=None):
    """One rota's strict evidence; omitted/failed model or unfinished search fails closed."""
    from . import fairness, generation, score, solver_rules, validation
    control = SolverControl(cancelled=cancelled)
    validations, scores = [], []
    for data in (before, after):
        control.check()
        validations.append(evaluate(copy.deepcopy(data)))
    a, b = validations
    for data, findings in zip((before, after), validations):
        control.check()
        scored = schedule_score(copy.deepcopy(data), findings,
                                data.get('generation_defaults') or data.get('generation_options'))
        if retirement_cutoff:
            # Only metadata for retired history is omitted; all numeric scoring outputs still compare in full.
            retained = {rule_copy_key(policy)[:5] for policy in _retained_rules(data, retirement_cutoff)}
            scored['rule_weights'] = [row for row in scored['rule_weights'] if
                (row['rule_id'], row['line'], row['slot'], row['severity'], row['strength']) in retained]
        scores.append(scored)
    control.check()
    out = {'version': VERSION, 'solver_version': solver_version,
           'toolchain': {'python': platform.python_version(), 'compiler': solver_rules.COMPILER_VERSION,
                         'validator': validation.ENGINE_VERSION, 'greedy': generation.ALGORITHM_VERSION,
                         'score': score.VERSION, 'fairness': fairness.VERSION},
           'input_hashes': {'before': digest(before), 'after': digest(after)},
           'expanded': strict_expanded(before, after, retirement_cutoff=retirement_cutoff),
           'findings': comparison(a['findings'], b['findings'], multiset=True),
           'quota': comparison(a.get('quota_report'), b.get('quota_report')),
           'fairness': comparison(a.get('goals', []), b.get('goals', []), multiset=True),
           'coverage': comparison(a.get('coverage', []), b.get('coverage', []), multiset=True),
           'validation': comparison({k: v for k, v in a.items() if k not in ('findings', 'coverage', 'goals', 'quota_report', 'fingerprint')},
                                    {k: v for k, v in b.items() if k not in ('findings', 'coverage', 'goals', 'quota_report', 'fingerprint')}),
           'score': comparison(*scores)}
    if retirement_cutoff:
        out['retirement_cutoff'] = str(retirement_cutoff)
    out['model'] = same_full_model(canonical_full_model(before, cancelled=cancelled),
                                   canonical_full_model(after, cancelled=cancelled)) if model else {'equal': None, 'skipped': True}
    control.check()
    if solve:
        out['solve'] = _solve(before, after, seconds, cancelled=cancelled)
        control.check()
    out['identical'] = all(out[key]['equal'] is True for key in ('expanded', 'findings', 'quota', 'fairness', 'coverage', 'validation', 'score', 'model'))
    if solve:
        out['identical'] = out['identical'] and all(x['equal'] is True for x in out['solve'].values())
    return out


def leadership_proof(before, after, *, model=True, cancelled=None):
    """Equivalent legacy transfer: compare role semantics and complete objective/fairness outputs.

    Only leadership source navigation is projected. Existing non-lead findings keep every field.
    Whole-duty clinical changes cannot pass this conversion proof.
    """
    from . import leadership
    from .time import aware
    from .validation import finding_observations
    control = SolverControl(cancelled=cancelled)

    def semantics(data):
        rows = []
        for shift in data.get('shifts', []):
            a, b = aware(shift['start']), aware(shift['end'])
            for lo, hi, rules in leadership.segments(data, shift, a, b, people=data['people']):
                control.check()
                lead = leadership.summary(data, shift, lo, hi)
                rows.append((shift['id'], lo.isoformat(), hi.isoformat(),
                    None if not lead else (lead['minimum'], lead['maximum'], lead['continuity'],
                        sorted(pid for pid in data['people'] if leadership.qualified(data, pid, shift, lo, hi)))))
        return rows

    def findings(validation):
        out = []
        for row in finding_observations(validation):
            if row['code'] not in {'LEAD_GAP', 'LEAD_NOT_QUALIFIED'}:
                out.append(row)
                continue
            # New policy/line identity, Who display and contextual navigation have no legacy policy identity.
            # Preserve the complete operational observation, including every count and interval.
            detail = row.get('detail') or {}
            out.append({key: row.get(key) for key in ('code', 'severity', 'shift_id', 'person_id', 'start', 'end')}
                       | {'detail': {key: detail[key] for key in ('required', 'available', 'maximum', 'scheduled') if key in detail}})
        return out

    checked = [evaluate(copy.deepcopy(data)) for data in (before, after)]
    scores = [schedule_score(copy.deepcopy(data), result,
        data.get('generation_defaults') or data.get('generation_options')) for data, result in zip((before, after), checked)]
    for data, score in zip((before, after), scores):
        converted = {p['id'] for p in active_rules(data) if p['kind'] == 'team_lead_requirement'}
        score['rule_weights'] = [row for row in score.get('rule_weights', []) if row['rule_id'] not in converted]
        for name in ('why', 'contributions'):
            for row in score.get(name, []):
                if row.get('rule_id') in converted:
                    row['rule_id'] = None
                    for field in ('line', 'slot', 'rule_severity', 'rule_strength', 'rule_weight', 'rule_source_id'):
                        if field in row:
                            row[field] = None

    out = {'role_semantics': comparison(semantics(before), semantics(after), multiset=True),
           'findings': comparison(findings(checked[0]), findings(checked[1]), multiset=True),
           'quota': comparison(*(r.get('quota_report') for r in checked)),
           'fairness': comparison(*(r.get('goals', []) for r in checked), multiset=True),
           'score': comparison(*scores)}
    out['model'] = same_full_model(canonical_full_model(before, cancelled=cancelled),
                                   canonical_full_model(after, cancelled=cancelled)) if model else {'equal': None, 'skipped': True}
    out['identical'] = all(section['equal'] is True for section in out.values())
    return out
