"""Schedule score 0-100, one scorer for generated and hand-made rotas.

Pure Python over the same
snapshot and validation result the validator produces; the solver scores its proposal with this function
and never reports its own objective as the score.

Every component is a value in [0, 1]. Each component's loss (1 - value) is split exactly over the
instances that caused it, so the "why" points add up to 100 - score. Any mandatory breach caps the
score at 59 and marks the rota not publishable.
"""

from collections import defaultdict
from math import floor, fsum
import time
from zoneinfo import ZoneInfo

from .recurrence import inactive
from .conditions import active_rules, counting_key, counting_views
from .fairness import goal_reports, quota_goal_reports, report_weights, share_value, unit_for_amount
from .patterns import SHAPE_KINDS, wish_outcomes
from .quotas import quota_identity
from .score_normalization import SCORE_SCALE, rule_normalization, rule_loss, coverage_satisfaction, reduced_component_weights, goal_cost
from .time import aware, duration_hours
from .validation import finding_observations, index_person_rows, named_people, policy_applies, session_scope

# v10: bind weighted score parity, Fair share adjustments and scoped preference changes.
VERSION = 'score-v12'
CAP = 59
COMPONENTS = ('coverage', 'rules', 'fairness', 'preferences', 'stability')
SESSION_KINDS = {'minimum_cover', 'maximum_participants'}
NOT_BREACHES = {'RULE_EXCEPTION', 'CAPACITY_OVERFLOW', 'BALANCE_GOAL', 'BALANCE_PAIR'}  # goals score under fairness


def progress_score(scored):
    """The footer's uncapped total and canonical component values; an empty rota has no score."""
    if scored['uncapped_score'] is None:
        return None
    return {'total': scored['uncapped_score'],
            'components': {name: scored['components'][name]['value'] for name in COMPONENTS}}


def _component_totals(components, settings, assigned):
    weights = reduced_component_weights(settings['weights'])
    present = {name: value for name, value in components.items() if value is not None and weights[name]}
    total_weight = sum(weights[name] for name in present)
    raw = (100 - sum(100 * weights[name] * (1 - value) / total_weight for name, value in present.items())
           if total_weight else 100)
    empty_loss = raw if not assigned else 0
    return weights, present, total_weight, raw - empty_loss, empty_loss


def compiled_progress_score(program, solution, options=None):
    """Evaluate recorded score arithmetic, never unchecked derived callback variables.

    The compiler records its existing formulas as linear/max/min/positive/div/mul/mod operations. Native inputs
    belong to the checked prefix; negative indices are metadata-only temporary registers. Missing definitions
    fail closed. The footer includes recorded publication stability, never a temporary optimizer baseline.
    """
    try:
        deadline = time.thread_time() + .1
        prefix = program['prefix_size']
        if not isinstance(prefix, int) or not 0 <= prefix <= len(solution) or program['scale'] != SCORE_SCALE:
            return None
        if not isinstance(program.get('goal_cost_checks'), list):
            return None
        if not program.get('has_sessions', True):
            return None
        values = {}
        remaining_terms = 256

        def expression(value):
            nonlocal remaining_terms
            if isinstance(value, int):
                return value
            result = value['offset']
            for index, coefficient in value['terms']:
                remaining_terms -= 1
                if not remaining_terms:
                    remaining_terms = 256
                    if time.thread_time() >= deadline:
                        raise ValueError('Live score CPU allowance exhausted')
                if index in values:
                    term = values[index]
                elif 0 <= index < prefix:
                    term = solution[index]
                else:
                    raise ValueError('Unrecorded score dependency')
                if not isinstance(term, int) or not isinstance(coefficient, int):
                    raise ValueError('Noninteger score arithmetic')
                result += coefficient * term
            if not isinstance(result, int):
                raise ValueError('Noninteger score arithmetic')
            return result

        for position, (index, operation, operands) in enumerate(program['ops']):
            if position % 64 == 0 and time.thread_time() >= deadline:
                return None
            if not isinstance(index, int) or 0 <= index < prefix or index in values:
                return None
            args = [expression(operand) for operand in operands]
            if operation == 'linear' and len(args) == 1:
                result = args[0]
            elif operation == 'positive' and len(args) == 1:
                result = int(args[0] > 0)
            elif operation == 'max' and args:
                result = max(args)
            elif operation == 'min' and args:
                result = min(args)
            elif operation == 'mul' and len(args) == 2:
                result = args[0] * args[1]
            elif operation in {'div', 'mod'} and len(args) == 2 and args[1]:
                quotient = abs(args[0]) // abs(args[1])
                quotient = -quotient if (args[0] < 0) != (args[1] < 0) else quotient
                result = quotient if operation == 'div' else args[0] - quotient * args[1]
            else:
                return None
            if not -(1 << 63) <= result < 1 << 63:
                return None
            values[index] = result
        for position, row in enumerate(program['goal_cost_checks']):
            if position % 64 == 0 and time.thread_time() >= deadline:
                return None
            numerator, denominator, cost = [expression(item) for item in row]
            if goal_cost(min(1.0, numerator / max(1, denominator))) != cost / SCORE_SCALE:
                return None
        components = {}
        for name in COMPONENTS:
            root = program['components'].get(name)
            publication_stability = name == 'stability' and program.get('publication_stability') is True
            if publication_stability and root is None:
                return None
            if root is None or name == 'stability' and not publication_stability:
                components[name] = None
                continue
            loss, present = [expression(item) for item in root]
            if (present not in (0, 1) or not 0 <= loss <= SCORE_SCALE
                    or publication_stability and present != 1):
                return None
            components[name] = 1 - loss / SCORE_SCALE if present else None
            # shortcut: five-tick guard covers coverage truncation; other components lack a global error bound.
            # Keep ambiguous samples unknown; an exact interval compiler is needed for a universal parity guarantee.
            if present and abs((SCORE_SCALE - loss) % 100 - 50) <= 5:
                return None
        assigned = expression(program['assigned'])
        if assigned not in (0, 1):
            return None
        _, _, _, raw, _ = _component_totals(components, settings_of(options), assigned)
        if time.thread_time() >= deadline:
            return None
        # Million-tick formulas approximate canonical floats; this defensive window is not an exact error bound.
        rounding_boundary = (floor(raw * 10) + .5) / 10
        if abs(raw - rounding_boundary) <= .01:
            return None
        return {'total': round(raw, 1),
                'components': {name: round(value, 4) if value is not None else None
                               for name, value in components.items()}}
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        return None


def settings_of(options):
    """Scoring settings with defaults filled in (from a department's or a run's generation options)."""
    from packages.contracts.schemas import ScoringSettings
    return ScoringSettings.model_validate((options or {}).get('scoring') or {}).model_dump()


def score_dependencies(options):
    """Bind saved whole-rota scores to all configured scoring inputs, including balance options."""
    return {'version': VERSION, 'options': options or {}}


def strength(policy, settings):
    return (settings['strengths'].get(policy['definition'].get('strength', 'medium'), 0)
            * policy['definition']['rule'].get('weight', 1))


def rule_copy_key(policy):
    line = policy.get('template_line') or {}
    return (policy['id'], line.get('line'), line.get('slot'), policy['definition']['severity'],
            policy['definition'].get('strength', 'medium'), counting_key(policy))


def legacy_lead_cap(data, policy):
    """C1 warned/optimised these caps but omitted them from the score; edits use normal scoring."""
    from packages.contracts.schemas import BlockQuota, Scope
    from .rule_lines import scoring_default_id
    definition, rule = policy['definition'], policy['definition']['rule']
    types = rule.get('assignment_type_ids') or []
    if rule['kind'] != 'block_quota' or rule.get('metric') != 'lead_turns' or len(types) != 1:
        return False
    tid = types[0]
    identity = (policy.get('template_line') or {}).get('id', policy['id'])
    if identity != scoring_default_id(policy['department_id'], 'lead-cap:' + tid):
        return False
    lead = (data.get('types', {}).get(tid, {}).get('definition') or {}).get('lead') or {}
    if not lead.get('most_turns'):
        return False
    expected = BlockQuota(kind='block_quota', metric='lead_turns', comparison='maximum', amount=lead['most_turns'],
        starts_on=rule['starts_on'], ends_on=rule.get('ends_on'), repeat=True,
        assignment_type_ids=[tid], window_alignment='calendar_' + lead.get('most_turns_per', 'month')).model_dump(mode='json')
    return (definition['severity'] == 'advisory' and definition.get('strength') == 'critical'
            and {**expected, **rule} == expected
            and {**Scope().model_dump(mode='json'), **definition['scope']} in (
                Scope().model_dump(mode='json'), Scope(assignment_type_id=tid).model_dump(mode='json')))


def schedule_score(data, validation, options=None, baseline=None, limit=12):
    """`options`: generation options (scoring settings, balance measure). `baseline`: the assignments before a
    change, {(shift, person): flags(participant)} (or bare pairs); stability is scored only when there is one."""
    if baseline is None:
        baseline = publication_baseline(data)
    options = options if options is not None else data.get('generation_options')
    check = data.get('_check_budget', lambda: None)
    check()
    settings = settings_of(options)
    zone = ZoneInfo(data['organisation']['timezone'])
    focus = set(data['focus_ids'])
    shifts = {s['id']: s for s in data['shifts'] if not inactive(s) and s['id'] in focus}
    if not shifts:
        from .rota_summary import submission_prerequisite
        prerequisite = submission_prerequisite(len(focus))
        return {'version': VERSION, 'score': None, 'raw': None, 'uncapped_score': None,
                'blocking_count': sum(f['severity'] == 'error' for f in validation.get('findings', [])),
                'capped': False, 'capped_points': 0, 'publishable': False,
                'gate': {'code': 'EMPTY_ROTA', 'message': prerequisite['message'] if prerequisite else 'No active sessions to score'},
                'settings': settings, 'components': {c: {'value': None, 'weight': settings['weights'][c], 'lost': 0}
                                                    for c in COMPONENTS},
                'why': [], 'people': {}, 'contributions': [], 'coverage_fraction': None, 'rule_weights': []}
    people = data['people']
    # Templates and overrides read as the checks read them: one copy per per-person line, overridden people left out.
    rules = data['_active_policies'] if '_active_policies' in data else active_rules(data['policies'], context=data)
    data = {**data, 'policies': rules, '_person_rows': data.get('_person_rows') or index_person_rows(data),
            '_score_options': options or {},
            '_policy_applies_cache': data.get('_policy_applies_cache', {}),
            '_policy_applies_people': True,
            '_allocation_match_cache': data.get('_allocation_match_cache', {}),
            '_scope_interval_cache': data.get('_scope_interval_cache', {}),
            '_restriction_index_cache': data.get('_restriction_index_cache', {}),
            '_categories_cache': {}, '_capabilities_cache': {}, '_scope_matches_cache': {},
            '_restriction_interval_cache': {},
            '_line_index_cache': data.get('_line_index_cache', {}),
            '_person_dates_cache': data.get('_person_dates_cache', {})}
    from .date_sets import prepare_date_sets
    data['_date_set_cache'] = prepare_date_sets(data)
    policies = {p['id']: p for p in reversed(rules)}  # by id: a rule's first copy (its name and default)
    counted = data['_counted_policies'] if '_counted_policies' in data else counting_views(data, rules)
    copies = {rule_copy_key(p): p for p in counted}
    unscored_caps = {(p['id'], (p.get('template_line') or {}).get('id')) for p in counted if legacy_lead_cap(data, p)}

    def finding_policy(finding):
        detail = finding.get('detail') or {}
        if 'rule_severity' in detail:
            return copies.get((finding.get('rule_id'), detail.get('line'), detail.get('slot'),
                               detail['rule_severity'], detail.get('rule_strength', 'medium'), counting_key(detail)))
        return policies.get(finding.get('rule_id'))  # older stored findings lack copy provenance

    def provenance(source=None):
        source = source or {}
        level = source.get('strength')
        weight = source.get('weight', source.get('rule', {}).get('weight', 1))
        source_id = source.get('policy_id') or source.get('id')
        if source_id is None and source.get('goal_id'):
            # Goal identities are namespaced by their source policy id; IDs are UUIDs and contain no colon.
            source_id = str(source['goal_id']).split(':', 1)[0]
        return {'line': source.get('line'), 'line_id': source.get('line_id'), 'slot': source.get('slot'),
                'rule_severity': source.get('severity'), 'rule_strength': level,
                'rule_weight': settings['strengths'][level] * weight if level in settings['strengths'] else None,
                # A grouped Fair-share report carries the comparison policy id separately from the source line.
                # Keep that source identity so score-review can attribute one contribution to its actual line.
                'rule_source_id': source_id}

    def policy_provenance(policy):
        return provenance({'id': policy['id'], **policy['definition'], **(policy.get('template_line') or {}),
                           'strength': policy['definition'].get('strength', 'medium')}) if policy else provenance()
    assigned = defaultdict(list)  # person -> [(shift, participant)]
    for shift in shifts.values():
        for p in shift['participants']:
            assigned[p['person_id']].append((shift, p))

    def who(pid):
        return people.get(pid, {}).get('display_name', 'Someone')

    def when(shift):
        return f"{shift['type_snapshot']['name']} {aware(shift['start']).astimezone(zone):%a %d %b %H:%M}"

    # instance: (component, loss on that component's 0-1 scale, text, person, shift, rule, provenance). A component's
    # instances add up exactly to 1 - its value.
    components, instances = {}, []

    # Coverage: unfilled required seat-hours, then desired cover at a quarter of the weight.
    required, wanted, gaps, desired = 0.0, 0.0, defaultdict(float), defaultdict(float)
    for row in validation.get('coverage', []):
        if row['shift_id'] in shifts and row['required'] and row['status'] in {'covered', 'gap'}:
            hours = duration_hours(aware(row['start']), aware(row['end']))
            required += row['required'] * hours
            gaps[row['shift_id']] += max(0, row['required'] - row['available_eligible']) * hours
    for f in validation.get('findings', []):
        if f['code'] == 'COVERAGE_TARGET' and f['shift_id'] in shifts:
            hours = duration_hours(aware(f['start']), aware(f['end']))
            desired[f['shift_id']] += (f['detail']['target'] - f['detail']['available']) * hours
            wanted += f['detail']['target'] * hours
    if required or desired:
        open_hours, below = sum(gaps.values()), sum(desired.values())
        unfilled = open_hours / required if required else 0.0
        short = min(1.0, below / (required or wanted)) if desired else 0.0
        components['coverage'] = coverage_satisfaction(unfilled, short, factor=settings['desired_cover_factor'])
        instances += [('coverage', unfilled * hours / open_hours, f'{hours:g} unfilled seat-hours · {when(shifts[sid])}', None, sid, None, provenance())
                      for sid, hours in gaps.items() if hours]
        instances += [('coverage', settings['desired_cover_factor'] * short * (1 - unfilled) * hours / below,
                       f'{hours:g} seat-hours below desired cover · {when(shifts[sid])}', None, sid, None, provenance())
                      for sid, hours in desired.items()]

    # Rules: each advisory rule per person (per session for cover rules) and each advisory quota row.
    items = {}  # key -> [strength, cost 0-1, text, person, shift, rule, provenance]
    for policy in counted:
        check()
        rule = policy['definition']['rule']
        if policy['definition']['severity'] != 'advisory' or rule['kind'] in {'allocation_preference', 'block_quota', 'balance_goal'} | SHAPE_KINDS:
            continue
        weight = strength(policy, settings)
        if rule['kind'] in SESSION_KINDS:
            session_policy = {**policy, 'definition': {**policy['definition'], 'scope': session_scope(policy['definition']['scope'])}}
            for sid, shift in shifts.items():
                if policy_applies(data, session_policy, None, shift, aware(shift['start']), aware(shift['end'])):
                    items[rule_copy_key(policy), sid] = [weight, 0.0, f"{policy['name']} · {when(shift)}", None, sid, policy['id'], policy_provenance(policy)]
            continue
        if rule['kind'] == 'spread_evenly':
            for sid, shift in shifts.items():
                if any(policy_applies(data, policy, pid, shift, aware(p['start']), aware(p['end']))
                       for pid, rows in assigned.items() for s, p in rows if s['id'] == sid):
                    items[rule_copy_key(policy), sid] = [weight, 0.0, f"{policy['name']} · {when(shift)}", None,
                                                         sid, policy['id'], policy_provenance(policy)]
            continue
        named = named_people(policy['definition']['scope'])  # a rule naming people never applies to anyone else
        for pid, rows in assigned.items():
            if (named is None or pid in named) and any(policy_applies(data, policy, pid, s, aware(p['start']), aware(p['end'])) for s, p in rows):
                items[rule_copy_key(policy), pid] = [weight, 0.0, f"{who(pid)} · {policy['name']}", pid, None, policy['id'], policy_provenance(policy)]
    breaches = defaultdict(int)
    for f in finding_observations(validation):
        policy = finding_policy(f)
        if (f['severity'] != 'warning' or f['code'] in NOT_BREACHES or f['code'].startswith('QUOTA_') or not policy
                or policy['definition']['severity'] != 'advisory' or policy['definition']['rule']['kind'] in SHAPE_KINDS):
            continue
        key = (rule_copy_key(policy), f['person_id'] or f['shift_id'])
        if key not in items:  # the validator saw a subject the scan above did not: still counted
            label = who(f['person_id']) if f['person_id'] else when(shifts[f['shift_id']]) if f['shift_id'] in shifts else ''
            items[key] = [strength(policy, settings), 0.0, f"{label} · {policy['name']}", f['person_id'], f['shift_id'], policy['id'], policy_provenance(policy)]
        breaches[key] += 1
        items[key][1] = 1.0  # a broken rule instance is fully violated (worst plausible value 1)
    for key, n in breaches.items():
        if n > 1:
            items[key][2] += f' ({n}×)'
    quota_weight = 0
    quota_goals = any(report.get('quota_target') for report in validation.get('goals', []))
    for row in (validation.get('quota_report') or {}).get('rows', []):
        policy = policies.get(row['policy_id'])
        if not policy or row['status'] == 'unresolved' or (row['policy_id'], row.get('line_id')) in unscored_caps:
            continue
        if row['comparison'] == 'target' or row['severity'] == 'advisory':
            cost = 0.0 if row['status'] in {'met', 'excepted'} else min(1.0, abs(row.get('difference', row['actual'] - row['amount'])) / max(abs(row['amount']), 1))
            quota_weight += settings['strengths'][row.get('strength', policy['definition'].get('strength', 'medium'))]
            amount = format(row['amount'], '.2f' if row['comparison'] == 'target' else 'g')
            items[quota_identity(row), row['person_id']] = [
                settings['strengths'][row.get('strength', policy['definition'].get('strength', 'medium'))], cost,
                f"{who(row['person_id'])} · {row['policy_name']}: {row['actual']:g} vs {row['comparison']} {amount} {row['metric'].replace('_', ' ')}",
                row['person_id'], None, row['policy_id'],
                provenance({**row, 'strength': row.get('strength', policy['definition'].get('strength', 'medium'))})]
    total = rule_normalization(data, settings['strengths'], [quota_weight])
    if total:
        violated = sum(item[0] * item[1] for item in items.values())
        loss = rule_loss(violated, total)
        components['rules'] = 1 - loss
        instances += [('rules', loss * w * c / violated, text, pid, sid, rid, source)
                      for w, c, text, pid, sid, rid, source in items.values() if c]

    # Workload fairness uses a fixed audience and cover budget; only overload loses points.
    measures = []  # (value, loss by person, costs, texts, weight, rule, provenance)
    checked_options = data.get('generation_options') or {}
    compatible_goals = ((options or {}).get('balance_measure', 'reserved_hours') ==
                        checked_options.get('balance_measure', 'reserved_hours') and
                        settings['fairness_worst_off_emphasis'] ==
                        settings_of(checked_options)['fairness_worst_off_emphasis'] and
                        (not data.get('fingerprint') or not validation.get('fingerprint') or
                         data['fingerprint'] == validation['fingerprint']))
    # A validation result belongs to this snapshot; trial callers invalidate its goal rows when changing work.
    reports = (validation['goals'] if 'goals' in validation and compatible_goals else
               goal_reports(data) if data.get('workspace', {}).get('department_id') else validation.get('goals', []))
    if not quota_goals:
        reports = reports + quota_goal_reports(data, validation.get('quota_report') or {})
    for report in reports:
        check()
        if not report.get('score_present', True):
            continue
        source = {row['person_id']: [provenance(item) for item in row.get('sources') or [{**report, 'severity': 'advisory'}]]
                  for row in report['people'] + report['pairs']}
        # Every configured goal, including workload spread, counts by its strength against the other goals.
        weight = settings['strengths'].get(report['strength'], 0) * report.get('weight', 1) / settings['strengths']['medium']
        if report['pairs']:
            rows = report['pairs']
            costs, loss = defaultdict(float), defaultdict(float)
            for r in rows:
                costs[r['person_id']] = max(costs[r['person_id']], r['cost'])
                loss[r['person_id']] += r['cost'] / len(rows)
            texts = {pid: f"{who(pid)} · {report['name']}: {sum(1 for r in rows if r['person_id'] == pid and r['cost'])} partner(s) below {rows[0]['expected']:g}"
                     for pid in costs}
            measures.append((1 - sum(r['cost'] for r in rows) / len(rows), loss, costs, texts, weight, report['policy_id'],
                             source))
            continue
        rows = report['people']
        if not rows or report.get('quota_target'):
            continue  # quota targets belong to the rules component
        row_weights = report_weights(report, settings['strengths'])
        weight = sum(row_weights.values()) / len(row_weights) / settings['strengths']['medium']
        value = report['value'] if report.get('workload_spread') else share_value(
            rows, row_weights, settings['fairness_worst_off_emphasis'])
        costs = {r['person_id']: r['cost'] for r in rows}
        # Split the goal's loss over the people outside their band, by how far outside (inside the band loses nothing).
        total = sum(c * row_weights[pid] for pid, c in costs.items()) or 1
        loss = {pid: (1 - value) * c * row_weights[pid] / total for pid, c in costs.items()}
        texts = {r['person_id']: f"{who(r['person_id'])} · {r['actual']:g} {unit_for_amount(report['unit'], r['actual'])} vs aim {r['expected']:.1f} ({report['name']})" for r in rows}
        measures.append((value, loss, costs, texts, weight, report['policy_id'], source))
    fairness_cost = defaultdict(float)  # person -> worst normalized deviation over the goals
    if measures and assigned:
        weights_total = fsum(m[4] for m in measures)
        components['fairness'] = fsum(m[0] * m[4] for m in measures) / weights_total
        for value, loss, costs, texts, weight, rid, source in measures:
            for pid, c in costs.items():
                fairness_cost[pid] = max(fairness_cost[pid], c)
            for pid, part in loss.items():
                if part:
                    total = sum(item['rule_weight'] or 1 for item in source[pid])
                    instances += [('fairness', part * weight / weights_total * (item['rule_weight'] or 1) / total,
                                   texts[pid], pid, None, rid, item) for item in source[pid]]

    # Preferences, per person: allocation preferences (each rule's strength), staff wishes (medium strength x the wish's weight,
    # cost = share of days not honoured, or 0/1 for a pattern wish) and shape rules (strength, fully broken when any breach).
    own = defaultdict(list)  # person -> [weight, cost, text, rule, provenance]
    medium = settings['strengths']['medium']
    from .allocation_score import allocation_score_items, SCALE as ALLOCATION_SCALE
    for item in allocation_score_items(data, strengths=settings['strengths']):
        if not item['active']:
            continue
        policy, pid = item['policy'], item['person_id']
        label = (policy['name'] + ' (' + item['label'] + ')') if policy else item['label']
        own[pid].append([float(item['weight']), item['loss'] / ALLOCATION_SCALE,
                         f"{who(pid)}: {label}", policy['id'] if policy else None,
                         policy_provenance(policy)])
    outcomes = wish_outcomes(data)
    for wish in data.get('wishes') or []:
        if wish['id'] in outcomes and wish.get('weight'):
            honoured, n = outcomes[wish['id']]
            own[wish['person_id']].append([medium * wish['weight'], 1 - honoured, f"{who(wish['person_id'])} · preference '{wish_words(wish, data)}' "
                                           + ('not granted' if honoured == 0 else f'granted on {round(honoured * n)} of {n} days'), None, provenance()])
    shapes = {}
    for policy in counted:
        check()
        if policy['definition']['rule']['kind'] in SHAPE_KINDS and policy['definition']['severity'] == 'advisory':
            for pid, rows in assigned.items():
                if any(policy_applies(data, policy, pid, s, aware(p['start']), aware(p['end'])) for s, p in rows):
                    shapes[rule_copy_key(policy), pid] = [strength(policy, settings), 0.0, f"{who(pid)} · {policy['name']}", policy['id'], policy_provenance(policy)]
    counts = defaultdict(int)
    for f in validation.get('findings', []):
        policy = finding_policy(f)
        if policy and policy['definition']['rule']['kind'] in SHAPE_KINDS and f['person_id']:
            key = (rule_copy_key(policy), f['person_id'])
            shapes.setdefault(key, [strength(policy, settings), 0.0, f"{who(f['person_id'])} · {policy['name']}", policy['id'], policy_provenance(policy)])[1] = 1.0
            counts[key] += 1
    for (rid, pid), item in shapes.items():
        if counts[rid, pid] > 1:
            item[2] += f' ({counts[rid, pid]}×)'
        own[pid].append(item)
    sat = {pid: 1 - sum(w * c for w, c, _, _, _ in items) / sum(w for w, _, _, _, _ in items) for pid, items in own.items() if sum(w for w, _, _, _, _ in items)}
    if sat and assigned:
        low = min(sat, key=lambda pid: (sat[pid], pid))
        mean_weight, worst_weight = settings['preferences_mean_weight'], settings['preferences_worst_weight']
        components['preferences'] = (mean_weight * sum(sat.values()) / len(sat) + worst_weight * sat[low]) / (mean_weight + worst_weight)
        for pid in sat:
            loss = (mean_weight * (1 - sat[pid]) / len(sat)
                    + (worst_weight * (1 - sat[low]) if pid == low else 0)) / (mean_weight + worst_weight)
            spent = sum(w * c for w, c, _, _, _ in own[pid])
            instances += [('preferences', loss * w * c / spent, text, pid, None, rid, source) for w, c, text, rid, source in own[pid] if loss and w * c]

    # Stability: existing assignments removed, moved or swapped away, or with a changed lead/overtime flag, against
    # the baseline (only when there is one). Filling an open place is not a change.
    # shortcut: changes are not yet weighted by how close to today they are.
    changes = defaultdict(int)
    if baseline is not None:
        baseline = baseline if isinstance(baseline, dict) else dict.fromkeys(baseline)  # pairs alone: flags not compared
        now = {(s['id'], p['person_id']): flags(p) for s in shifts.values() for p in s['participants']}
        before = {key: value for key, value in baseline.items() if key[0] in shifts}
        moved = [key for key, value in before.items() if key not in now or value is not None and now[key] != value]
        for _, pid in moved:
            changes[pid] += 1
        components['stability'] = 1 - min(1.0, len(moved) / max(1, len(before)))
        instances += [('stability', (1 - components['stability']) * n / len(moved),
                       f'{who(pid)} · {n} assignment change{"s" if n > 1 else ""}', pid, None, None, provenance()) for pid, n in changes.items()]

    weights, present, total_weight, raw, empty_loss = _component_totals(components, settings, bool(assigned))
    errors = [f for f in validation.get('findings', []) if f['severity'] == 'error']
    # The displayed gate diagnosis must not depend on the database's rule order.
    first = min(errors, key=lambda f: (
        f['code'], f.get('rule_id') or '', f.get('person_id') or '', f.get('shift_id') or '',
        f.get('start') or '', f.get('end') or '', str(f.get('detail')), f['message'])) if errors else None
    score = min(raw, CAP) if errors else raw
    lines = [(100 * weights[c] * loss / total_weight, c, text, pid, sid, rid, source)
             for c, loss, text, pid, sid, rid, source in instances if c in present]
    if empty_loss:
        lines.append((empty_loss, 'other', 'No assignments in this rota', None, None, None, provenance()))
    if errors and raw > CAP:
        lines.append((raw - CAP, 'gate', f"Capped at {CAP}: not publishable until the rota check errors are fixed ({first['message']})",
                      first['person_id'], first['shift_id'], first['rule_id'], policy_provenance(finding_policy(first))))
    # Equal remainders belong to the same source regardless of rule/report traversal order.
    lines.sort(key=lambda line: (line[1], line[5] or '', line[3] or '', line[4] or '',
                                 str(line[6].get('line')), str(line[6].get('slot')), line[2]))
    shown = round(score, 1)
    tenths = apportion([line[0] for line in lines], round((100 - shown) * 10))
    contributions = sorted(({'component': c, 'points': t / 10, 'text': text, 'person_id': pid, 'shift_id': sid, 'rule_id': rid, **source}
                            for t, (_, c, text, pid, sid, rid, source) in zip(tenths, lines) if t), key=lambda w: (-w['points'], w['text']))
    why = [{k: row[k] for k in ('component', 'points', 'text', 'person_id', 'shift_id', 'rule_id')} for row in contributions]
    lost = defaultdict(int)
    for t, line in zip(tenths, lines):
        lost[line[1]] += t
    top = why[:limit]
    if why[limit:]:
        top.append({'component': 'other', 'points': sum(round(w['points'] * 10) for w in why[limit:]) / 10,
                    'text': f'{len(why) - limit} smaller items', 'person_id': None, 'shift_id': None, 'rule_id': None})

    own_rules = defaultdict(lambda: [0.0, 0.0])
    for w, c, _, pid, _, _, _ in items.values():
        if pid:
            own_rules[pid][0] += w
            own_rules[pid][1] += w * c
    per_person = {}
    for pid in sorted(set(assigned) | (set(own_rules) | set(sat) if assigned else set()) | set(changes)):
        parts = {}
        if 'rules' in present and pid in own_rules:
            parts['rules'] = 1 - own_rules[pid][1] / own_rules[pid][0]
        if 'fairness' in present and pid in fairness_cost:
            parts['fairness'] = 1 - fairness_cost[pid]
        if 'preferences' in present and pid in sat:
            parts['preferences'] = sat[pid]
        if 'stability' in present:
            own = sum(1 for key in baseline if key[1] == pid and key[0] in shifts)
            parts['stability'] = 1 - min(1.0, changes[pid] / max(1, own))
        weight = sum(weights[c] for c in parts)
        per_person[pid] = {'score': round(100 * sum(weights[c] * v for c, v in parts.items()) / weight, 1) if weight else 100.0,
                           **{c: round(v, 3) for c, v in parts.items()},
                           'issues': [{'points': w['points'], 'text': w['text']} for w in why if w['person_id'] == pid][:3]}
    return {'version': VERSION, 'score': shown, 'raw': round(raw, 1), 'uncapped_score': round(raw, 1),
            'blocking_count': len(errors), 'capped': bool(errors and raw > CAP), 'publishable': not errors,
            'gate': {'code': first['code'], 'message': first['message']} if first else None,
            'settings': settings, 'capped_points': lost['gate'] / 10,
            'components': {c: {'value': round(components[c], 4) if c in components else None, 'weight': settings['weights'][c],
                               'lost': lost[c] / 10} for c in COMPONENTS},
            'why': top, 'people': per_person, 'contributions': contributions,
            'coverage_fraction': components.get('coverage'),
            'rule_weights': [{'rule_id': p['id'], 'severity': p['definition']['severity'],
                              'strength': p['definition'].get('strength', 'medium'),
                              'weight': strength(p, settings),
                              'line': (p.get('template_line') or {}).get('line'),
                              'slot': (p.get('template_line') or {}).get('slot')}
                             # Metadata order must not depend on the database's source-row order.
                             for p in sorted(rules, key=lambda p: tuple(str(value) for value in rule_copy_key(p)))]}


def score_review_lines(data, validation, options=None, scored=None):
    """Return every advisory line with its scored loss and current-draft audience.

    This is deliberately derived from the same expanded rules and score contributions as ``schedule_score``.  A line
    with no loss still appears, so a review can decide whether a zero is expected or an unused configuration.
    """
    context = type(data)(data) if hasattr(data, '__dict__') else dict(data)
    if hasattr(data, 'read_quality'):
        context.read_quality = data.read_quality
    context.setdefault('_line_index_cache', {})
    data = context
    scored = scored or schedule_score(data, validation, options, limit=0)
    rules = data['_active_policies'] if '_active_policies' in data else active_rules(data['policies'], context=data)
    counted = data['_counted_policies'] if '_counted_policies' in data else counting_views(data, rules)
    focus_ids = set(data['focus_ids'])
    shifts = {row['id']: row for row in data['shifts'] if row['id'] in focus_ids and not inactive(row)}
    quota_rows = (validation.get('quota_report') or {}).get('rows', [])
    groups = {}
    for policy in counted:
        definition, rule = policy['definition'], policy['definition']['rule']
        if definition.get('severity') != 'advisory':
            continue
        template = policy.get('template_line') or {}
        level = definition.get('strength', 'medium')
        key = (policy['id'], template.get('id'), template.get('line'), template.get('slot'), level)
        row = groups.setdefault(key, {'policy': policy, 'sessions': set(), 'people': set(), 'components': set(),
                                      'weights': [], 'lost': 0.0})
        kind = rule['kind']
        if kind == 'balance_goal':
            from .fairness import goal_people, goal_applies
            audience = goal_people(data, policy).get('who', [])
            row['people'].update(audience)
            for shift in shifts.values():
                for participant in shift['participants']:
                    pid = participant['person_id']
                    if pid in audience and goal_applies(data, policy, pid, shift,
                                                        aware(participant['start']), aware(participant['end'])):
                        row['sessions'].add(shift['id'])
                        break
            continue
        scoped_policy = ({**policy, 'definition': {**definition, 'scope': session_scope(definition['scope'])}}
                         if kind in SESSION_KINDS else policy)
        for shift in shifts.values():
            a, b = aware(shift['start']), aware(shift['end'])
            if kind in SESSION_KINDS:
                if policy_applies(data, scoped_policy, None, shift, a, b):
                    row['sessions'].add(shift['id'])
                continue
            for participant in shift['participants']:
                pid = participant['person_id']
                if policy_applies(data, policy, pid, shift, aware(participant['start']), aware(participant['end'])):
                    row['people'].add(pid)
                    row['sessions'].add(shift['id'])
        if kind == 'block_quota':
            # Quota reports include the fixed audience even when a person has no current assignment.
            # Use their exact line copy so a zero-assignment line is still visible in the review.
            for quota in quota_rows:
                if quota.get('policy_id') != policy['id']:
                    continue
                if quota.get('line_id') != template.get('id') or quota.get('line') != template.get('line'):
                    continue
                if quota.get('slot') != template.get('slot') or quota.get('person_id') is None:
                    continue
                row['people'].add(quota['person_id'])
    for value in groups.values():
        policy = value['policy']
        template = policy.get('template_line') or {}
        level = policy['definition'].get('strength', 'medium')
        for contribution in scored.get('contributions', []):
            if policy['id'] not in {contribution.get('rule_id'), contribution.get('rule_source_id')}:
                continue
            if contribution.get('line') != template.get('line') or contribution.get('slot') != template.get('slot'):
                continue
            if contribution.get('line_id') not in (None, template.get('id')):
                continue
            if contribution.get('rule_strength', level) != level:
                continue
            value['lost'] += contribution.get('points', 0) or 0
            value['components'].add(contribution.get('component'))
            if contribution.get('rule_weight') is not None:
                value['weights'].append(contribution['rule_weight'])
    total_lost = round(sum(contribution.get('points', 0) or 0 for contribution in scored.get('contributions', [])), 1)
    settings = settings_of(options if options is not None else data.get('generation_options'))
    result = []
    for key, value in groups.items():
        policy = value['policy']
        definition, rule = policy['definition'], policy['definition']['rule']
        template = policy.get('template_line') or {}
        level = definition.get('strength', 'medium')
        configured = rule.get('weight', definition.get('weight', 1))
        weight = max(value['weights'], default=settings['strengths'].get(level, 0) * configured)
        components = sorted(c for c in value['components'] if c in {'rules', 'fairness', 'preferences'})
        result.append({'rule_id': policy['id'], 'line_id': template.get('id'), 'line': template.get('line'),
                       'slot': template.get('slot'), 'name': policy['name'], 'kind': rule['kind'], 'level': level,
                       'weight': weight, 'lost_points': round(value['lost'], 1),
                       'lost_share': round(value['lost'] / total_lost, 6) if total_lost else 0,
                       'session_count': len(value['sessions']), 'people_count': len(value['people']),
                       'component': components[0] if len(components) == 1 else None})
    return {'score': scored, 'total_lost_points': total_lost,
            'lines': sorted(result, key=lambda row: (-row['lost_points'], row['name'], row['rule_id'],
                                                     row['line'] if row['line'] is not None else -1,
                                                     row['slot'] or ''))}


def score_effect(with_score, without_score):
    """Signed displayed deltas, including the empty-rota adjustment and separate safety gate."""
    parts = {c: round(without_score['components'][c]['lost'] - with_score['components'][c]['lost'], 1)
             for c in COMPONENTS}
    gate = round(without_score['capped_points'] - with_score['capped_points'], 1)
    points = round(with_score['score'] - without_score['score'], 1)
    # All displayed losses are apportioned tenths; the remaining part is the empty-rota adjustment.
    other = round(points - sum(parts.values()) - gate, 1)
    return {**parts, 'gate': gate, 'other': other, 'points': points,
            'score_with': with_score['score'], 'score_without': without_score['score'],
            'uncapped_score_with': with_score['uncapped_score'],
            'uncapped_score_without': without_score['uncapped_score']}


def placement_effects(data, validation, shift_id, person_ids, options=None, *, base_score=None, baseline=None, proposals=None):
    """The score with each person on one session versus without them. Revalidate each actual trial: coverage,
    rules, shapes and goals depend on the assignment. One baseline plus one shared scoring pass per candidate;
    the caller bounds the work. No stability baseline is invented for saved rotas."""
    from .validation import evaluate, prepare_assignment_trials, supervision_commitments
    # Retained assignments keep their times/segments throughout these add/remove trials.
    data = {**prepare_assignment_trials(data), '_participation_cache': {}}
    shift = next(s for s in data['shifts'] if s['id'] == shift_id)
    base = base_score if base_score is not None else schedule_score(data, validation, options, baseline=baseline, limit=0)
    granted = wish_outcomes(data)
    wishes = defaultdict(list)
    for wish in data.get('wishes') or []:
        wishes[wish['person_id']].append(wish)
    out = {}
    session_cache, trial_cache = {}, {}
    for pid in dict.fromkeys(person_ids):
        on = any(p['person_id'] == pid for p in shift['participants'])
        people = ([p for p in shift['participants'] if p['person_id'] != pid] if on else shift['participants'] + [
            {'id': 'why-' + pid, 'shift_id': shift_id, 'person_id': pid, 'start': shift['start'], 'end': shift['end'],
             'participation': 'staff', 'overtime': False, **((proposals or {}).get(pid) or {})}])
        trial = {**data, 'shifts': [dict(s, participants=people) if s['id'] == shift_id else s for s in data['shifts']]}
        trial['_supervision_commitments'] = supervision_commitments(trial)
        after = schedule_score(trial, evaluate(trial, session_cache=session_cache, trial_cache=trial_cache,
                                             explain_findings=False), options, baseline=baseline, limit=0)
        moved = wish_outcomes(trial)
        out[pid] = {**(score_effect(base, after) if on else score_effect(after, base)),
                    'wishes': [{'text': wish_words(w, data), 'granted': (moved[w['id']][0] < granted[w['id']][0]) == on}
                               for w in wishes[pid] if w['id'] in granted and moved.get(w['id'], (None,))[0] != granted[w['id']][0]]}
    return out


def flags(participant):
    """What stability compares on an assignment that is kept: its lead and overtime flags."""
    return bool(participant.get('team_lead')), bool(participant.get('overtime'))


def publication_baseline(data):
    """JSON-safe publication evidence shared by saved scores and the optimizer; absent/unmappable stays absent."""
    evidence = data.get('score_baseline')
    if evidence is None or evidence.get('assignments') is None:
        return None
    return {(row['shift_id'], row['person_id']): flags(row) for row in evidence['assignments']}


def apportion(values, total):
    """Whole tenths for `values` (points) that add up exactly to `total` tenths (largest remainder)."""
    raw = [max(0.0, v) * 10 for v in values]
    parts = [int(r) for r in raw]
    order = sorted(range(len(raw)), key=lambda i: raw[i] - parts[i], reverse=True)
    missing = total - sum(parts)
    for i in range(max(0, missing) if order else 0):
        parts[order[i % len(order)]] += 1
    for j in sorted(range(len(parts)), key=lambda j: -parts[j]):
        while missing < 0 and parts[j]:
            parts[j] -= 1
            missing += 1
    return parts


WISH_WORDS = {'prefer_off': 'off', 'prefer_on': 'on duty', 'prefer_shift_type': 'on', 'avoid_shift_type': 'not on',
              'max_days_in_row': 'at most {limit} days in a row', 'min_days_off_together': 'at least {limit} days off together',
              'max_weekends_in_row': 'at most {limit} weekends in a row', 'whole_weekends': 'whole weekends'}


def wish_words(wish, data):
    """e.g. "off Fri 20 Mar", "on Night Wed 4 – Fri 6 Mar"."""
    from datetime import date
    a, b = date.fromisoformat(str(wish['first_day'])), date.fromisoformat(str(wish['last_day']))
    duty = (data.get('types', {}).get(wish.get('assignment_type_id') or '', {}).get('name', 'a duty type') + ' ') if wish.get('assignment_type_id') else ''
    when = f'{a:%a} {a.day} {a:%b}' + (f' – {b:%a} {b.day} {b:%b}' if b != a else '')
    return f"{WISH_WORDS[wish['kind']].format(**wish)} {duty}{when}"
