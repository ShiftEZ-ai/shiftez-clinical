"""Which rules touch which sessions, people and days of one rota (the planner's rule map). Pure: it reads a domain
snapshot and asks the one evaluator (active_rules and the validator's scope tests); it never matches on its own.
Work is shared: sessions are grouped by what a scope can see of them, and each person-side scope is read once per person."""

import json
from datetime import date, timedelta
from zoneinfo import ZoneInfo
from packages.contracts.schemas import safety_rule
from .conditions import COUNT_BUCKET, OVERRIDDEN, OWN_LINE, PERSON_FIELDS, _bucket_descriptor, absolute_values, active_rules, bucket_atoms, bucket_count, change_points, has_session_lines, scope_tree, selector_sources, scope_applies, scope_has_duty_witness, scope_matches_throughout, scope_segments
from .time import aware, local_midnight
from .recurrence import inactive, usual_assignee_ids
from .validation import ALL_DAYS, ancestor_ids, category_scope_ids, every_day_all_day, index_person_rows, named_people, person_scope_ids, policy_applies, restricted_intervals, scope_intervals, session_scope, within_dates

# The one number each rule kind has: (field, unit, which way is stricter). A rota may change these numbers.
NUMBERS = {'min_rest': ('hours', 'hours', 'higher'), 'max_hours': ('hours', 'hours', 'lower'),
           'max_duties': ('maximum', 'duties', 'lower'), 'max_consecutive_days': ('maximum', 'days', 'lower'),
           'max_consecutive_weekends': ('maximum', 'weekends', 'lower'), 'minimum_cover': ('minimum', 'people', 'higher'),
           'maximum_participants': ('maximum', 'people', 'lower'), 'travel_buffer': ('minutes', 'minutes', 'higher'),
           'block_quota': ('amount', None, None), 'spread_evenly': ('maximum_gap', 'gap', 'lower')}
NUMERIC_LIMITS = {'max_hours', 'max_duties', 'max_consecutive_days', 'max_consecutive_weekends'}
COVERAGE = {'minimum_cover', 'maximum_participants'}
SESSION_KEYS = ('service_id', 'service_ids', 'location_id', 'assignment_type_id')
CLOCK_KEYS = ('weekdays', 'start_time', 'end_time')


def weakens_safety(policy, entry):
    """Admission, historical adjustment projection and reach markers share the same stricter direction."""
    definition = policy['definition']
    if entry.get('line_id') and definition['rule']['kind'] == 'catalogue':
        rule = definition['rule']
        line = next((line for line in rule.get('lines') or []
                     if line.get('_source_identity', line['id']) == entry['line_id'] and entry.get('number') in line['numbers']), None)
        if line is None:
            return True  # A missing binding cannot authorize a historical relaxation.
        definition = {**definition, 'rule': {**rule, 'lines': [{**line,
            'numbers': {entry['number']: line['numbers'][entry['number']]}}]}}
    if not safety_rule(definition) or entry['mode'] == 'clear':
        return False
    if entry['mode'] == 'off':
        return True
    if definition['rule']['kind'] == 'catalogue':
        from packages.contracts.rule_catalogue import catalogue_entry, slot_for_number
        rule = definition['rule']
        line = next((line for line in rule.get('lines') or []
                     if line.get('_source_identity', line['id']) == entry.get('line_id')), None)
        if line is not None and entry.get('number') in line.get('numbers', {}):
            slot = slot_for_number(catalogue_entry(rule['id'], rule.get('catalogue_version', 1)), entry['number'], rule,
                                   line, line['numbers'])
            if slot['kind'] == 'spread_evenly' and slot['field']:
                original = line['numbers'][entry['number']].get('value')
                return original is None or entry['amount'] > original
    field, _, direction = NUMBERS.get(definition['rule']['kind'], (None, None, None))
    if field is None:
        return True
    amount, original = entry['amount'], definition['rule'][field]
    return amount < original if direction == 'higher' else amount > original


def numeric_limit(rule):
    return rule['kind'] in NUMERIC_LIMITS or rule['kind'] == 'block_quota' and rule.get('comparison') == 'maximum'


def overridable(definition):
    """Whether a narrower rule may replace this one: never a safety rule; advisory rules and numeric limits may be."""
    return not safety_rule(definition) and (definition.get('severity') == 'advisory' or numeric_limit(definition['rule']))


def rule_number(rule):
    """The number a rule uses, its unit and which way is stricter (a quota target has no stricter side). None: no number."""
    if rule['kind'] == 'catalogue':
        from packages.contracts.rule_catalogue import catalogue_entry, slot_for_number
        lines = rule.get('lines') or []
        # One rota-wide input cannot choose between different lines or minimum/maximum/target numbers.
        if len(lines) != 1 or len(lines[0]['numbers']) != 1:
            return None
        line = lines[0]
        key, value = next(iter(line['numbers'].items()))
        catalogue = catalogue_entry(rule['id'], rule.get('catalogue_version', 1))
        slot = slot_for_number(catalogue, key, rule, line, line['numbers'])
        if not slot['field'] or value.get('value') is None:
            return None
        kind = slot['kind']
        number = rule_number({**catalogue['default_settings'].get(kind, {}), **rule.get('settings', {}).get(kind, {}),
                              **line.get('settings', {}).get(kind, {}), **slot['fixed'], 'kind': kind, slot['field']: value['value']})
        return {**number, 'field': key} if number else None
    if rule['kind'] not in NUMBERS:
        return None
    field, unit, stricter = NUMBERS[rule['kind']]
    if rule['kind'] == 'block_quota':
        unit, stricter = rule.get('metric'), {'maximum': 'lower', 'minimum': 'higher'}.get(rule.get('comparison'))
        target = {'target': rule['target_amount']} if rule.get('target_amount') is not None else {}
        if rule.get('comparison') == 'range':  # at least `value`, at most `maximum`
            return {'field': field, 'value': rule.get(field), 'maximum': rule.get('maximum_amount'), 'unit': unit, 'stricter': None, **target}
        if target:
            return {'field': field, 'value': rule.get(field), 'unit': unit, 'stricter': stricter, **target}
    return {'field': field, 'value': rule.get(field), 'unit': unit, 'stricter': stricter}


def _binding_values(data, a, b, zone):
    """Changed numbers from the check's resolved policies, indexed by source and slot."""
    from packages.contracts.rule_catalogue import catalogue_entry, slot_for_number
    from packages.contracts.schemas import safety_direction

    captured, effective = data.get('_rule_map_policies', ((), ()))
    if not effective:
        return {}, {}
    department = data['workspace']['department_id']
    sources = {p['id']: p['id'] for p in captured
               if p['department_id'] == department and p['status'] == 'active' and p.get('active', True)}
    predecessors = {p['id']: p['definition']['replaces_id']
                    for p in data.get('_live_safety_staleness', {}).get('policies', ())
                    if p['department_id'] == department and p['definition'].get('replaces_id')}

    def source_id(policy):
        ident = policy.get('_source_policy_id', policy['id'])
        prior, path = ident, set()
        while prior not in sources and prior in predecessors and prior not in path:
            path.add(prior)
            prior = predecessors[prior]
        source = sources.get(prior, prior if prior not in path else ident)
        sources.update(dict.fromkeys(path, source))
        return source

    def index(policies):
        policies = [p for p in policies if p['department_id'] == department]
        catalogue_slots = {}
        for policy in policies:
            rule = policy['definition']['rule']
            if rule['kind'] != 'catalogue' or policy['status'] != 'active' or not policy.get('active', True):
                continue
            entry = catalogue_entry(rule['id'], rule.get('catalogue_version', 1))
            for i, line in enumerate(rule.get('lines') or ()):
                ident = line.get('_source_identity', line['id'])
                for key, number in line['numbers'].items():
                    slot = slot_for_number(entry, key, rule, line, line['numbers'])
                    if slot['field'] and number.get('value') is not None:
                        catalogue_slots[policy['id'], ident, line.get('_source_index', i), key] = (rule['id'], slot['field'])

        numbers, zeroes = {}, set()
        for copy in active_rules(policies, context=data):
            if not within_dates(copy, a, b, zone):
                continue
            definition, template = copy['definition'], copy.get('template_line') or {}
            rule = definition['rule']
            slots = []
            if 'id' in template and 'slot' in template:
                for slot in template['slot'].split('+'):
                    match = catalogue_slots.get((copy['id'], template['id'], template['line'], slot))
                    if match:
                        catalogue, field = match
                        direction = safety_direction(rule['kind'], field) or {'max': 'min', 'min': 'max'}.get(slot)
                        slots.append(((catalogue, template['id'], slot), field, direction))
            elif not template and (number := rule_number(rule)):
                direction = {'lower': 'min', 'higher': 'max'}.get(number['stricter'])
                # active_rules splits quota ranges into separate minimum and maximum copies.
                slot = rule.get('comparison') if rule['kind'] == 'block_quota' else number['field']
                slots.append(((source_id(copy), None, slot), number['field'], direction))
            for key, field, direction in slots:
                values = numbers.setdefault(key, set())
                if definition['severity'] == 'mandatory' and (value := rule.get(field)) is not None:
                    values.add((value, direction))
                    if value == 0 and copy.get('_live_zero'):
                        zeroes.add(key)

        def scalar(values):
            amounts = {value for value, _ in values}
            directions = {direction for _, direction in values}
            if len(amounts) == 1:
                return next(iter(amounts)), next(iter(directions)) if len(directions) == 1 else None
            if directions == {'min'}:
                return min(amounts), 'min'
            if directions == {'max'}:
                return max(amounts), 'max'
            return None, None

        return {key: scalar(values) for key, values in numbers.items()}, zeroes

    before, _ = index(captured)
    after, zeroes = index(effective)
    changed = {}
    # shortcut: loosened revisions show the captured value because review still checks it; show both once review reads live (#1032)
    for key, (value, direction) in after.items():
        if key not in before and key in zeroes:
            changed[key] = 0
            continue
        saved, saved_direction = before.get(key, (None, None))
        if value is not None and saved is not None and direction == saved_direction and (
                direction == 'min' and value < saved or direction == 'max' and value > saved):
            changed[key] = value
    return changed, sources


def _one_changed(values):
    # A scalar cannot describe several independently changed numbers.
    changed = [value for value in values if value is not None]
    return changed[0] if len(changed) == 1 else None


def adjustable(rule):
    """Whether a rota may use another number for this rule (a range has two: change it in the rule)."""
    if rule['kind'] == 'catalogue':
        return rule_number(rule) is not None and (not safety_rule({'rule': rule}) or rule.get('id') == 'spread_evenly')
    return rule['kind'] in NUMBERS and rule.get('comparison') != 'range'


def _lines(tree, fields):
    """Every line (in groups too) on these fields."""
    for c in (tree or {}).get('conditions') or []:
        if 'conditions' in c:
            yield from _lines(c, fields)
        elif c['field'] == COUNT_BUCKET:
            for value in absolute_values(c) or ():
                yield from _lines(_bucket_descriptor(value)[0], fields)
        elif c['field'] in fields:
            yield c


def named(data, definition):
    """What a rule's scope names outright: flat fields plus "is any of" lines, and what its lines name (people, clinics,
    duty types and days)."""
    scope, rows = definition['scope'], definition.get('per_person') or []

    def pick(field, *flat):
        values = [v for k in flat for v in (scope.get(k) if isinstance(scope.get(k), list) else [scope[k]] if scope.get(k) else [])]
        values += [v for c in _lines(scope.get('conditions'), {field}) if c.get('op', 'any') in {'any', 'is'}
                   for v in absolute_values(c) or ()]
        return list(dict.fromkeys(values + [v for r in rows for v in r.get(field) or []]))
    places = set(pick('location_ids', 'location_id'))
    weekdays = scope.get('weekdays', list(range(7)))
    if rows and all(r.get('weekdays') is not None for r in rows):
        weekdays = sorted({d for r in rows for d in r['weekdays']})
    return {'people': list(dict.fromkeys(pick('person_ids', 'person_id', 'person_ids') + [r['person_id'] for r in rows if r.get('person_id')])),
            'services': pick('service_ids', 'service_id', 'service_ids'),
            'locations': [l for l in data.get('locations', {}) if places & set(ancestor_ids(data, l))] if places else [],
            'types': pick('assignment_type_ids', 'assignment_type_id'),
            'weekdays': None if set(weekdays) == ALL_DAYS else sorted(weekdays)}


def person_side(definition):
    scope = definition['scope']
    return bool(person_scope_ids(scope) or category_scope_ids(scope) or scope.get('programme_id') or scope.get('rotation_type_id')
                or scope.get('tags') or scope.get('capability_ids') or definition.get('per_person')
                or next(_lines(scope.get('conditions'), PERSON_FIELDS), None))


def session_side(definition):
    scope, rule = definition['scope'], definition['rule']
    return bool(rule['kind'] in COVERAGE | {'team_lead_requirement'} or any(scope.get(k) for k in SESSION_KEYS) or has_session_lines(scope_tree(scope))
                or not every_day_all_day({k: scope[k] for k in CLOCK_KEYS if k in scope})
                or rule['kind'] == 'restriction' and not every_day_all_day({k: rule[k] for k in CLOCK_KEYS if k in rule}))


def _listed(scope):
    """People a scope is limited to for sure (flat list or a top-level "is any of" line), for a quick no; None: anyone."""
    return named_people(scope)


def _ranges(mask):
    out, i = [], 0
    while mask >> i:
        if mask >> i & 1:
            j = i
            while mask >> j & 1:
                j += 1
            out.append([i, j])
            i = j
        else:
            i += 1
    return out


def _days(mask):
    return [i for i in range(mask.bit_length()) if mask >> i & 1]


def _clinic_grants(data, department_id):
    by_clinic = {}
    for grant in data.get('grants') or []:
        if not grant.get('revoked') and (grant.get('service_id') or grant['department_id'] != department_id):
            by_clinic.setdefault(grant['capability_id'], set()).add(grant['person_id'])
    return by_clinic


def _skill_context(data, scope, pool, by_clinic):
    if not by_clinic:
        return frozenset(), frozenset()
    scopes, seen, skills, presence, negative = [scope], set(), set(), False, False
    while scopes:
        item = scopes.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        skills.update(item.get('capability_ids') or ())
        for line in _lines(item.get('conditions'), {'capability_ids'}):
            skills.update(absolute_values(line) or ())
            presence |= line.get('op') in {'empty', 'not_empty'}
            negative |= line.get('op') in {'none', 'is_not'}
        scopes.extend(source['definition']['scope'] for source in
                      selector_sources(pool, item.get('conditions'), data['_line_index_cache']))
    people = frozenset().union(*(by_clinic.values() if presence else
                                 (by_clinic.get(skill, ()) for skill in skills)))
    return people, people if presence or negative else frozenset()


def rule_map(data, originals=None):
    """The rule map of the snapshot's rota. `originals`: the department rules as they are before this rota's
    adjustments, by id (for the department's number and for rules switched off here)."""
    from .rule_lines import bound_policies, catalogue_display_policies, safety_display_key
    if not data.get('_catalogue_v2_materialized') and (data.get('source_bindings') or data.get('scheduling_facts')):
        data = {**data, 'policies': bound_policies(data['policies'], data), '_catalogue_v2_materialized': True}
    data = {**data, 'policies': catalogue_display_policies(data['policies'], data)}
    originals = originals or {}
    ws, zone = data['workspace'], data['organisation']['timezone']
    tz, first, last = ZoneInfo(zone), date.fromisoformat(str(ws['starts_on'])), date.fromisoformat(str(ws['ends_on']))
    n = (last - first).days
    days = [(first + timedelta(days=i)).isoformat() for i in range(n)]
    A, B, full = local_midnight(first, zone), local_midnight(last, zone), (1 << n) - 1
    bindings, binding_sources = _binding_values(data, A, B, zone)
    dept = ws['department_id']
    focus = set(data['focus_ids'])
    shifts = [s for s in data['shifts'] if s['id'] in focus and not inactive(s)]
    starts = [aware(s['start']) for s in shifts]
    ends = [aware(s['end']) for s in shifts]
    session_signatures = [(tuple(s['service_ids']), s['location_id'], s['assignment_type_id'], usual_assignee_ids(s))
                          for s in shifts]
    day_of = lambda t: (t.astimezone(tz).date() - first).days

    def span(lo, hi):  # the rota days that [lo, hi) touches
        a, b = max(day_of(lo), 0), min(day_of(hi - timedelta(microseconds=1)) + 1, n)
        return ((1 << b) - 1) ^ ((1 << a) - 1) if a < b else 0

    def dated(item):  # rota days inside an item's dates
        a = (date.fromisoformat(item['starts_on']) - first).days if item.get('starts_on') else 0
        b = (date.fromisoformat(item['ends_on']) - first).days if item.get('ends_on') else n
        a, b = max(a, 0), min(b, n)
        mask = ((1 << b) - 1) ^ ((1 << a) - 1) if a < b else 0
        interval = item.get('_protected_interval')
        return mask & span(aware(interval['start']), aware(interval['end'])) if interval else mask

    raw = [p for p in data['policies'] if p['department_id'] == dept]
    referenced_policy_ids = {destination.get('id')
                             for raw_binding in (data.get('source_bindings') or [])
                             for binding in [raw_binding.get('data', raw_binding)]
                             for destination in (binding.get('destinations') or ())
                             if destination.get('kind') == 'policy'}
    catalogue_rows = {}
    active_catalogue_policy_ids = set()
    for policy in raw:
        rule = policy.get('definition', {}).get('rule') or {}
        active = policy.get('status', 'active') == 'active' and policy.get('active', True)
        if (active or policy.get('id') in referenced_policy_ids) \
                and rule.get('kind') == 'catalogue' and rule.get('id'):
            catalogue_rows.setdefault(rule['id'], []).append(policy)
            if active:
                active_catalogue_policy_ids.add(policy['id'])
    mapped_source_ids = {binding.get('source_policy_id')
                         for raw_binding in (data.get('source_bindings') or [])
                         for binding in [raw_binding.get('data', raw_binding)]
                         if any(destination.get('kind') == 'policy'
                                and destination.get('id') in active_catalogue_policy_ids
                                for destination in (binding.get('destinations') or ()))}
    display_lines = {}
    for catalogue_id, rows in catalogue_rows.items():
        lines = {}
        for policy in rows:
            if policy['id'] not in active_catalogue_policy_ids:
                continue
            for line in (policy.get('definition', {}).get('rule') or {}).get('lines') or ():
                lines.setdefault(line['id'], line)
        display_lines[catalogue_id] = list(lines.values())
    copies = [c for c in active_rules(raw, context=data) if within_dates(c, A, B, zone)]
    live = {c['id'] for c in copies}
    policies = [p for p in raw if p['id'] in live and not (
        p['id'] in mapped_source_ids and p['definition']['rule']['kind'] != 'catalogue')]
    policies = [{**p, 'definition': {**p['definition'], 'rule': {**p['definition']['rule'],
        'lines': display_lines[p['definition']['rule']['id']]}}}
                if p['definition']['rule'].get('kind') == 'catalogue' and p['definition']['rule'].get('id') in display_lines
                else p for p in policies]
    data = {**data, 'policies': copies, '_person_rows': index_person_rows(data), '_person_dates_cache': {},
            '_line_index_cache': {}, '_staffing_selection_cache': {}}
    from .date_sets import prepare_date_sets
    data['_date_set_cache'] = prepare_date_sets(data)
    people = sorted({r['person_id'] for r in data['memberships'] if r['department_id'] == dept and within_dates(r, A, B, zone)
                     and data['people'].get(r['person_id'], {}).get('active', True)})

    subs, pool_indexes = {}, {}

    def sub(scope, pool):
        """The snapshot with only the rules (and catalogue lines) this scope's internal lines name, and theirs: same
        answers, far less to scan."""
        internal = {OVERRIDDEN, OWN_LINE}
        if id(pool) not in pool_indexes:
            by_id = {}
            for q in pool:
                by_id.setdefault(q['id'], []).append(q)
            pool_indexes[id(pool)] = by_id
        by_id = pool_indexes[id(pool)]
        refs, todo = set(), [absolute_values(c) or () for c in _lines(scope.get('conditions'), internal)]
        while todo:
            new = set(todo.pop()) - refs
            refs |= new
            todo += [absolute_values(c) or () for rid in new for q in by_id.get(rid, ())
                     for c in _lines(q['definition']['scope'].get('conditions'), internal)]
        # Legacy policy rows are already indexed by refs; only catalogue descriptors carry source keys.
        sources = [s for s in selector_sources(pool, scope.get('conditions'), data['_line_index_cache']) if 'key' in s]
        key = (id(pool), tuple(sorted(refs)), tuple(s['key'] for s in sources))
        if key not in subs:
            rows = [q for rid in sorted(refs) for q in by_id.get(rid, ())]
            if sources:
                rows.append({**pool[0], '_catalogue_sources': {s['key']: s for s in sources}})
            cuts = {local_midnight(date.fromisoformat(q[f]), zone) for q in [*rows, *sources]
                    for f in ('starts_on', 'ends_on') if q.get(f)}
            # Matching answers belong to this fixed source pool, never another override/source subview.
            subs[key] = ({**data, 'policies': rows, '_policy_applies_cache': {},
                          '_policy_applies_people': True}, cuts)
        return subs[key]

    by_clinic = _clinic_grants(data, dept)
    skill_scopes, projected_views = {}, {}

    def skill_context(scope, pool):
        key = (id(scope), id(pool))
        if key not in skill_scopes:
            skill_scopes[key] = _skill_context(data, scope, pool, by_clinic)
        return skill_scopes[key]

    person_cache = {}

    def person_masks(copy, pool):
        """Per person: rota days where the person side of this rule copy holds (its own dates not yet applied)."""
        scope = copy['definition']['scope']
        listed = _listed(scope)
        skey = (json.dumps({k: v for k, v in scope.items() if k not in SESSION_KEYS + CLOCK_KEYS}, sort_keys=True), id(pool))
        view, cuts = sub(scope, pool)
        _, project = skill_context(scope, pool)
        if project:
            key = (id(view), project)
            if key not in projected_views:
                # Clinic-dependent absence cannot be inferred from the union of a person's grants.
                projected_views[key] = {**view, '_project_clinic_skills': project, '_line_index_cache': {}}
            view = projected_views[key]
        out = {}
        for pid in (people if listed is None else [p for p in people if p in listed]):
            if (skey, pid) not in person_cache:
                person_cache[(skey, pid)] = sum(span(lo, hi) for lo, hi, matched in
                    scope_segments(view, scope, pid, None, A, B, extra_points=cuts) if matched)
            out[pid] = person_cache[(skey, pid)]
        return out, view

    match_cache, clock_cache, hit_cache = {}, {}, {}

    def session_key(node):
        from .conditions import COUNT_BUCKET, SESSION_FIELDS
        if 'conditions' in node:
            return {**node, 'conditions': [session_key(child) for child in node['conditions']]}
        # With no person, every person-side condition is met, including exclusions; keep OR/AND grouping.
        return node if node['field'] in SESSION_FIELDS or node['field'] == COUNT_BUCKET else {'match': 'all', 'conditions': []}

    def session_hits(copy):
        definition = copy['definition']
        selector = session_scope(definition['scope'])
        plain = {k: v for k, v in selector.items() if k not in CLOCK_KEYS}
        clocks = {k: selector[k] for k in CLOCK_KEYS if k in selector}
        projected = {**plain, **({'conditions': session_key(plain['conditions'])} if plain.get('conditions') else {})}
        pkey, ckey = json.dumps(projected, sort_keys=True), json.dumps(clocks, sort_keys=True)
        date_scoped = has_session_lines(scope_tree(plain), {'date_set_ids', 'date'})
        rule = definition['rule']
        key = (copy['department_id'], copy.get('starts_on'), copy.get('ends_on'), pkey, ckey,
               json.dumps(rule, sort_keys=True) if rule['kind'] == 'restriction' else None,
               json.dumps(copy.get('_protected_interval'), sort_keys=True))
        if key in hit_cache:
            return hit_cache[key]
        hits = []
        for i, s in enumerate(shifts):
            if data['types'][s['assignment_type_id']]['department_id'] != copy['department_id'] or not within_dates(copy, starts[i], ends[i], zone):
                continue
            # Date-set scopes also read the duty's local start day; other scopes retain their shared signatures.
            sig = (pkey, session_signatures[i],
                   starts[i].astimezone(tz).date() if date_scoped else None)
            if sig not in match_cache:
                match_cache[sig] = scope_matches_throughout(data, plain, None, s, starts[i], ends[i])
            if not match_cache[sig]:
                continue
            if clocks and not every_day_all_day(clocks):
                if (ckey, i) not in clock_cache:
                    clock_cache[(ckey, i)] = bool(scope_intervals(data, clocks, None, starts[i], ends[i]))
                if not clock_cache[(ckey, i)]:
                    continue
            # A leave-buffer restriction depends on each person's leave: mapped() keeps the sessions worked within it.
            if rule['kind'] == 'restriction' and not rule.get('leave_buffer_days') and not restricted_intervals(data, copy, starts[i], ends[i]):
                continue
            hits.append(i)
        hit_cache[key] = hits
        return hits

    duties, crew = {}, {}  # person -> session index -> (session index, person, start, end, rota days touched), per place
    for i, s in enumerate(shifts):
        for p in s['participants']:
            a, b = aware(p['start']), aware(p['end'])
            duties.setdefault(p['person_id'], []).append((i, a, b, span(a, b)))
            crew.setdefault(i, []).append((p['person_id'], a, b, span(a, b)))

    near_cache = {}

    def near_leave(copy, view, pid):
        """Rota days within a leave-buffer restriction's buffer of this person's own leave (and its weekdays and times)."""
        key = (copy['id'], id(copy), pid)
        if key not in near_cache:
            near_cache[key] = sum(span(lo, hi) for lo, hi in restricted_intervals(view, copy, A, B, pid))
        return near_cache[key]

    # Skills granted for some clinics only: a rule on them holds for a person at one session and not another on the
    # same day, so its person days alone would claim too much. Those rules are asked per session and person, as the
    # "Why?" panel asks (policy_applies); `not_here` lists where the person's days say yes and the session says no.
    # Only the people holding such a grant can differ from their days.
    exact_people = {}  # rule id -> the people to ask per session
    for c in copies if by_clinic else ():
        found, _ = skill_context(c['definition']['scope'], copies)
        if found:
            exact_people.setdefault(c['id'], set()).update(found)
    applies_cache = {}

    def applies(view, copy, pid, i, a, b):
        key = (copy['id'], id(copy), pid, i)
        if key not in applies_cache:
            applies_cache[key] = policy_applies(view, copy, pid, shifts[i], a, b)
        return applies_cache[key]

    def mapped(pool, only=None):
        """Per rule id: (sessions, people masks, days mask, {person: sessions where their days over-claim}) over the
        rule copies in `pool` (a template's copies merge)."""
        out, said = {}, {}
        for copy in pool:
            if only is not None and copy['id'] not in only:
                continue
            sessions, masks, day_mask, _ = out.get(copy['id'], (set(), {}, 0, None))
            window = dated(copy)
            held, view = person_masks(copy, pool)
            held = {pid: m & window for pid, m in held.items() if m & window}
            rule = copy['definition']['rule']
            buffered = rule['kind'] == 'restriction' and rule.get('leave_buffer_days')
            if buffered:  # only the days within the buffer of each person's own leave
                held = {pid: near for pid, m in held.items() if (near := m & near_leave(copy, view, pid))}
            for pid, m in held.items():
                masks[pid] = masks.get(pid, 0) | m
            if copy.get('_protected_interval') or session_side(copy['definition']):
                mine = []
                for i in session_hits(copy):
                    if buffered and not any(held.get(pid, 0) & touched and restricted_intervals(view, copy, a, b, pid)
                                            for pid, a, b, touched in crew.get(i, ())):
                        continue  # nobody works it within their buffer
                    sessions.add(i)
                    mine.append(i)
                    day = day_of(starts[i])
                    if 0 <= day < n:
                        day_mask |= 1 << day
                pairs = ((pid, i, a, b) for i in mine for pid, a, b, touched in crew.get(i, ()) if held.get(pid, 0) & touched)
            else:
                # A people-only rule checks the sessions its people work. Within one day nothing a person holds changes
                # (days are cut at every change), so the day's answer is the validator's; skills read the session's
                # clinics and longer places are asked of the validator itself.
                scope = copy['definition']['scope']
                exact = not (scope.get('capability_ids') or next(_lines(scope.get('conditions'), {'capability_ids'}), None)
                             or exact_people.get(copy['id']))
                for pid, m in held.items():
                    day_mask |= m
                    sessions.update(i for i, a, b, touched in duties.get(pid, ()) if i not in sessions and m & touched and (
                        data['types'][shifts[i]['assignment_type_id']]['department_id'] == copy['department_id']
                        if exact and not touched & (touched - 1) else applies(view, copy, pid, i, a, b)))
                pairs = ((pid, i, a, b) for pid, m in held.items() for i, a, b, touched in duties.get(pid, ()) if m & touched)
            if copy['id'] in exact_people:
                ask = exact_people[copy['id']]
                yes, no = said.setdefault(copy['id'], (set(), set()))
                for pid, i, a, b in pairs:
                    if pid in ask:
                        (yes if applies(view, copy, pid, i, a, b) else no).add((pid, i))
            out[copy['id']] = (sessions, masks, day_mask, None)
        for rid, (yes, no) in said.items():
            not_here = {}
            for pid, i in sorted(no - yes):
                not_here.setdefault(pid, []).append(i)
            out[rid] = out[rid][:3] + (not_here,)
        return out

    found = mapped(copies)
    # Who follows a line that names no person (a programme's or a rank's line), on which rota days: the planner shows
    # that line's number as theirs. {rule id: {person: [(first day, end day, line index in the rule's per_person)]}}.
    line_people, first_slots = {}, {}
    for copy in copies:
        line = copy.get('template_line') or {}
        if line.get('id'):
            first_slots.setdefault((copy['id'], line['id']), line['slot'])
    for copy in sorted(copies, key=safety_display_key):
        line = copy.get('template_line')
        if not line or line.get('person_id') or line.get('id') and line['slot'] != first_slots[(copy['id'], line['id'])]:
            continue
        held, _ = person_masks(copy, copies)
        window = dated(copy)
        for pid, mask in held.items():
            if mask & window:
                line_people.setdefault(copy['id'], {}).setdefault(pid, []).extend((a, b, line['line']) for a, b in _ranges(mask & window))
    replaced_by = {}
    for p in policies:
        for target in p['definition'].get('overrides') or []:
            replaced_by.setdefault(target, []).append(p['id'])
    plain = mapped([c for c in active_rules(raw, overrides=False, context=data) if within_dates(c, A, B, zone)], set(replaced_by) & live)
    sources = {p['id']: p for p in raw} | originals
    stored_adjustments = {r['policy_id']: r for r in ws.get('rule_adjustments') or [] if not r.get('line_id')}
    adjustments = {r['policy_id']: r for r in stored_adjustments.values()
                   if r['policy_id'] not in sources or not weakens_safety(sources[r['policy_id']], r)}
    names = {p['id']: p['name'] for p in policies} | {i: p['name'] for i, p in originals.items()}
    rules, entries = [], {}
    for p in policies + [originals[i] for i, r in adjustments.items() if r['mode'] == 'off' and i in originals and i not in live]:
        if p['definition']['rule']['kind'] == 'catalogue':
            entry = entries[p['id']] = _catalogue_entry(data, p, [c for c in copies if c['id'] == p['id']], found, line_people, dated, ws, adjustments, p['id'] not in live, originals.get(p['id'], p), bindings)
            rules.append(entry)
            continue
        definition, off = p['definition'], p['id'] not in live
        sessions, masks, day_mask, not_here = found.get(p['id'], (set(), {}, 0, None))
        rule = definition['rule']
        number = rule_number(rule)
        binding = None
        if bindings and number and definition.get('severity') == 'mandatory':
            comparison = rule.get('comparison') if rule['kind'] == 'block_quota' else None
            slots = ('minimum', 'maximum') if comparison == 'range' else (comparison or number['field'],)
            ident = p.get('_source_policy_id', p['id'])
            source_id = binding_sources.get(ident, ident)
            binding = _one_changed(bindings.get((source_id, None, slot)) for slot in slots)
        before = originals.get(p['id'], p)
        adjusted = stored_adjustments.get(p['id'])
        gone = {pid: m & ~masks.get(pid, 0) for pid, m in plain.get(p['id'], ({}, {}, 0))[1].items()}
        entry = entries[p['id']] = {
            'id': p['id'], 'name': p['name'], 'kind': definition['rule']['kind'], 'severity': definition.get('severity'),
            'strength': definition.get('strength'), 'safety': safety_rule(definition),
            'exception_allowed': bool(definition.get('exception_allowed')) and not safety_rule(definition), 'adjustable': adjustable(definition['rule']),
            'overridable': overridable(definition), 'starts_on': p.get('starts_on'), 'ends_on': p.get('ends_on'),
            'block': [definition['rule']['starts_on'], definition['rule']['ends_on']] if definition['rule']['kind'] == 'block_quota' else None,
            'number': number, 'department_value': (rule_number(before['definition']['rule']) or {}).get('value'),
            'binding_value': binding,
            'adjusted': {'mode': adjusted['mode'], 'amount': adjusted.get('amount'), 'by': adjusted.get('by'), 'at': adjusted.get('at'),
                         **({'applied': False} if p['id'] not in adjustments else {})} if adjusted else None,
            'off': off, 'workspace_only': definition.get('workspace_id') == ws['id'],
            'overrides': list(definition.get('overrides') or []), 'overridden_by': replaced_by.get(p['id'], []),
            'named': {**named(data, definition), 'dated': bool(dated(p) != full)},
            'person_side': person_side(definition), 'session_side': session_side(definition),
            'sessions': sorted(sessions), 'people': {pid: _ranges(m) for pid, m in masks.items()},
            'replaced': {pid: _ranges(m) for pid, m in gone.items() if m}, 'days': _days(day_mask), 'not_here': not_here or {},
            'lines': [r for r in definition.get('per_person') or [] if dated(r)]}
        shown = {i: k for k, i in enumerate(i for i, r in enumerate(definition.get('per_person') or []) if dated(r))}
        entry['line_people'] = {pid: [[a, b, shown[n]] for a, b, n in spans if n in shown] for pid, spans in line_people.get(p['id'], {}).items()}
        rules.append(entry)

    markers = []

    def marker(kind, rule_id, text, by=None, at=None, people=(), services=(), locations=(), types=(), days=None, sessions=(), is_global=False):
        markers.append({'kind': kind, 'rule_id': rule_id, 'rule_name': names.get(rule_id), 'text': text, 'by': by, 'at': at,
                        'people': list(people), 'services': list(services), 'locations': list(locations), 'types': list(types),
                        'days': days, 'sessions': list(sessions), 'global': is_global})

    def amount(value, number):
        return f"{value:g} {number['unit'] or ''}".strip() if number else f'{value:g}'
    for p in policies:
        entry, definition = entries[p['id']], p['definition']
        if definition['rule']['kind'] == 'catalogue':
            _narrower_markers(p, entry, copies, person_masks, dated, marker)
            continue
        number = entry['number']
        for r in entry['lines']:
            if r.get('amount') is None or not r.get('person_id') or not number:
                continue  # a line without its own number for a person reads as the rule itself
            marker('template_line', p['id'], f"Own number in “{p['name']}”: {amount(r['amount'], number)} instead of {amount(number['value'], number)}",
                   p.get('approved_by'), p.get('created_at'), people=[r['person_id']], days=_days(dated(r)))
        targets = [entries[t] for t in definition.get('overrides') or [] if t in entries]
        if targets:
            said = '; '.join(f"“{t['name']}”" + (f": {amount(number['value'], number)} instead of {amount(t['number']['value'], t['number'])}"
                                                   if number and t['number'] else '') for t in targets)
            limited = entry['named']['weekdays'] is not None or entry['named']['dated']
            marker('override', p['id'], f"“{p['name']}” replaces {said}", p.get('approved_by'), p.get('created_at'),
                   people=list(entry['people']) if entry['person_side'] else (), services=entry['named']['services'],
                   locations=entry['named']['locations'], types=entry['named']['types'],
                   days=entry['days'] if limited else None, sessions=entry['sessions'] if entry['session_side'] else ())
    by_id = {p['id']: p for p in policies}
    for e in data.get('exceptions') or []:
        p = by_id.get(e['policy_id'])
        if (p and not safety_rule(p['definition']) and e['status'] == 'approved' and e['policy_revision'] == p['revision'] and p['definition'].get('exception_allowed')
                and dated(e)):
            end = (date.fromisoformat(e['ends_on']) - timedelta(days=1)).isoformat()
            marker('exception', p['id'], f"Exception to “{p['name']}”: {e['starts_on']} to {end}", e.get('approved_by'), e.get('created_at'),
                   people=[e['person_id']], days=_days(dated(e)))
    for policy_id, r in adjustments.items():
        entry = entries.get(policy_id)
        if entry is None:
            continue
        what = entry['named']
        text = ('Switched off for this rota' if r['mode'] == 'off'
                else f"This rota: {amount(r['amount'], entry['number'])} instead of {amount(entry['department_value'], entry['number'])}")
        marker('rota_adjustment', policy_id, text, r.get('by'), r.get('at'), people=what['people'], services=what['services'],
               locations=what['locations'], types=what['types'],
               is_global=not (what['people'] or what['services'] or what['locations'] or what['types']))
    for r in ws.get('target_overrides') or []:
        entry = entries.get(r['policy_id'])
        if entry is not None:
            marker('target_override', r['policy_id'], f"This rota’s target in “{entry['name']}”: {amount(r['amount'], entry['number'])}",
                   r.get('by'), r.get('at'), people=[r['person_id']])
    return {'days': days, 'shifts': [s['id'] for s in shifts], 'rules': rules, 'markers': markers}


def _catalogue_entry(data, p, mine, found, line_people, dated, ws, adjustments, off, original, bindings):
    """A catalogue rule in the rule map: one entry, its lines (dated within the rota) and what each line's copies reach."""
    from packages.contracts.rule_catalogue import catalogue_entry
    rule = p['definition']['rule']
    catalogue = catalogue_entry(rule['id'], rule.get('catalogue_version', 1))
    sessions, masks, day_mask, not_here = found.get(p['id'], (set(), {}, 0, None))
    names = [named(data, c['definition']) for c in mine]
    pick = lambda key: list(dict.fromkeys(v for n in names for v in n[key]))  # noqa: E731
    weekdays = sorted({d for n in names for d in (n['weekdays'] if n['weekdays'] is not None else range(7))})
    scoped = [r for r in ws.get('rule_adjustments') or [] if r['policy_id'] == p['id'] and r.get('line_id')]
    shown_rule = original['definition']['rule'] if scoped else rule
    lines = shown_rule.get('lines') or []
    displayed_lines = [{**line, 'binding_value': _one_changed(
        bindings.get((catalogue['id'], line.get('_source_identity', line['id']), slot)) for slot in line['numbers'])}
        for line in lines if dated(line)]
    number = rule_number(shown_rule)
    shown = {i: k for k, i in enumerate(i for i, line in enumerate(lines) if dated(line))}
    display_index = {copy.get('template_line', {}).get('line'): shown[index]
                     for copy in mine if copy.get('template_line')
                     for index, line in enumerate(lines) if line.get('id') == copy['template_line'].get('id') and index in shown}
    adjusted = adjustments.get(p['id'])
    return {'id': p['id'], 'name': p['name'], 'kind': 'catalogue', 'catalogue_id': catalogue['id'],
            **({'_display_source_ids': p['_display_source_ids']} if p.get('_display_source_ids') else {}),
            'severity': 'mandatory' if any(c['definition']['severity'] == 'mandatory' for c in mine) else 'advisory', 'strength': None,
            'safety': any(safety_rule(c['definition']) for c in mine),
            'exception_allowed': bool(p['definition'].get('exception_allowed')) and not any(safety_rule(c['definition']) for c in mine), 'adjustable': adjustable(shown_rule),
            'overridable': False, 'starts_on': p.get('starts_on'), 'ends_on': p.get('ends_on'), 'block': None, 'number': number,
            'binding_value': displayed_lines[0]['binding_value'] if number and displayed_lines else None,
            'scoped_adjustments': scoped,
            'department_value': (rule_number(original['definition']['rule']) or {}).get('value'), 'off': off, 'workspace_only': False, 'overrides': [], 'overridden_by': [],
            'adjusted': {'mode': adjusted['mode'], 'amount': adjusted.get('amount'), 'by': adjusted.get('by'), 'at': adjusted.get('at')} if adjusted else None,
            'named': {'people': pick('people'), 'services': pick('services'), 'locations': pick('locations'), 'types': pick('types'),
                      'weekdays': None if set(weekdays) == ALL_DAYS or not weekdays else weekdays,
                      'dated': any(dated(line) != dated({}) for line in lines)},
            'person_side': any(person_side(c['definition']) for c in mine),
            'session_side': any(c.get('_protected_interval') or session_side(c['definition']) for c in mine),
            'sessions': sorted(sessions), 'people': {pid: _ranges(m) for pid, m in masks.items()}, 'replaced': {},
            'days': _days(day_mask), 'not_here': not_here or {}, 'lines': displayed_lines,
            'line_people': {pid: [[a, b, display_index.get(n, shown.get(n, n))] for a, b, n in spans
                                  if n in display_index or n in shown] for pid, spans in line_people.get(p['id'], {}).items()}}


def _narrower_markers(p, entry, copies, person_masks, dated, marker):
    """Where a catalogue line a more specific line outranks no longer applies: a marker per winning line, for its people."""
    from .rule_lines import outranking, source_key, source_lines
    lines = p['definition']['rule'].get('lines') or []
    winners = sorted({w for ws in outranking(p).values() for w in ws})
    sources = source_lines(p) if winners else {}  # A raw policy's descriptor lookup serializes the whole rule once.
    for w in winners:
        copy = sources.get(source_key(p, lines[w]))
        if copy is None or not dated(lines[w]):
            continue
        held, _ = person_masks(copy, copies)
        people = [pid for pid, m in held.items() if m & dated(lines[w])]
        if people:
            marker('narrower_line', p['id'], f"A narrower line of “{p['name']}” replaces its broader line here", p.get('approved_by'),
                   p.get('created_at'), people=people, days=_days(dated(lines[w])))


def short_days(result, data, findings):
    """Per rule of a rule map: the rota days its stored check found short, from the check's findings by rule
    {rule id: [[day, errors, warnings, first message], ...]}. A finding without a time (a period goal) has no day."""
    first, zone = date.fromisoformat(str(data['workspace']['starts_on'])), ZoneInfo(data['organisation']['timezone'])
    n, out = len(result['days']), {}
    for f in findings:
        if not f.get('rule_id') or not f.get('start'):
            continue
        day = (aware(f['start']).astimezone(zone).date() - first).days
        if 0 <= day < n:
            row = out.setdefault(f['rule_id'], {}).setdefault(day, [day, 0, 0, f['message']])
            row[1 if f['severity'] == 'error' else 2] += 1
    return {rid: sorted(days.values()) for rid, days in out.items()}


def reach(data, policies, a, b, *, details=False, policy_id=None, line_id=None):
    """How many people each rule reaches on one day [a, b): {rule id: count}. Active rules are read as every check
    reads them (their lines, and the rules that replace them for some people); a proposed rule as if it were active.
    Each distinct scope is asked once per person (a quota's minimum, maximum and target share it), and a scope
    limited to listed people asks only them."""
    zone = data['organisation']['timezone']
    live = [p for p in policies if p['status'] == 'active' and p.get('active', True)]
    known = {p['id'] for p in live}
    copies = active_rules(live, context=data)
    for p in policies:
        if p['id'] not in known:
            copies += active_rules([{**p, 'status': 'active', 'active': True}], overrides=False)
    data = {**data, 'policies': copies, '_person_rows': index_person_rows(data), '_person_dates_cache': {}, '_line_index_cache': {}}
    selected = [p for p in policies if (policy_id is None or p['id'] == policy_id)
                and (line_id is None or any(line.get('id') == line_id for line in p['definition']['rule'].get('lines') or []))]
    ids = {p['id'] for p in selected}
    chosen = [c for c in copies if c['id'] in ids and (line_id is None or (c.get('template_line') or {}).get('id') == line_id)]
    if details:
        sources = {(p['id'], line['id']): line for p in selected for line in p['definition']['rule'].get('lines') or []}
        found = reach_details(data, chosen, a, b, sources=sources, line_id=line_id)
        return {'reach': {p['id']: len({person['person_id'] for line in found.get(p['id'], {}).get('lines', [])
                                       for person in line['people']}) for p in selected}, 'details': found}
    people, seen, out = list(data['people']), {}, {p['id']: set() for p in selected}
    clinic_grants = {department_id: _clinic_grants(data, department_id) for department_id in {c['department_id'] for c in chosen}}
    for c in chosen:
        if not within_dates(c, a, b, zone):
            continue
        scope = c['definition']['scope']
        lo = max(a, local_midnight(date.fromisoformat(str(c['starts_on'])), zone)) if c.get('starts_on') else a
        hi = min(b, local_midnight(date.fromisoformat(str(c['ends_on'])), zone)) if c.get('ends_on') else b
        key = (json.dumps(scope, sort_keys=True), lo, hi, c['department_id'])
        if key not in seen:
            listed = _listed(scope)
            clinic_people, _ = _skill_context(data, scope, copies, clinic_grants[c['department_id']])
            seen[key] = {pid for pid in (people if listed is None else [p for p in people if p in listed])
                          if (scope_has_duty_witness(data, scope, pid, lo, hi, c['department_id'])
                              if pid in clinic_people else scope_applies(data, scope, pid, None, lo, hi))}
        out[c['id']] |= seen[key]
    return {rid: len(found) for rid, found in out.items()}


def reach_details(data, copies, a, b, *, sources=None, line_id=None):
    """Reached source lines, their matching branches and exact counting windows. No loaded rota: unknown counts.

    Condition matching and bucket ownership remain in conditions.py; the map only projects those answers onto
    records and asks the existing workload/quota calculators for their numbers.
    """
    from .conditions import SESSION_FIELDS, _requirements, _scope_duty_contexts, bucket_keys, counting_key, counting_views, matched_paths, rule_holds
    from .quotas import evaluate_quotas
    from .validation import exception_for, workload_actual, workload_selected, workload_windows
    from .time import overlaps
    zone = data['organisation']['timezone']
    loaded = 'workspace' in data and 'shifts' in data
    counted = counting_views(data, copies)
    view = {**data, '_person_rows': index_person_rows(data), '_staffing_selection_cache': {}, '_scope_interval_cache': {}}
    people = sorted(pid for pid, person in data['people'].items() if person.get('active', True))
    if loaded:
        department = data['workspace']['department_id']
        members = {m['person_id'] for m in data['memberships'] if m['department_id'] == department and within_dates(m, a, b, zone)}
        people = [pid for pid in people if pid in members]
    dates = {pid: change_points(view, pid, a, b) + [b] for pid in people}
    duties, signatures, dated_signatures = {}, {}, None
    for shift in data.get('shifts') or []:
        if inactive(shift):
            continue
        if overlaps(a, b, aware(shift['start']), aware(shift['end'])):
            signature = (tuple(sorted(shift['service_ids'])), shift.get('location_id'), shift['assignment_type_id'],
                         usual_assignee_ids(shift))
            signatures.setdefault(signature, []).append(shift)
        for participant in shift['participants']:
            duties.setdefault(participant['person_id'], []).append((shift, participant, aware(participant['start']), aware(participant['end'])))
    quotas = None
    if loaded and any(c['definition']['rule']['kind'] == 'block_quota' for c in counted):
        quotas = evaluate_quotas({**view, '_quota_evaluation_ids': True}, lambda *args, **kwargs: None,
                                 ancestor_ids, exception_for, policy_ids={c['id'] for c in copies}, line_id=line_id)
    scope_people, session_cache, path_cache, count_cache, goal_cache, out = {}, {}, {}, {}, {}, {}
    clinic_grants = {department_id: _clinic_grants(view, department_id) for department_id in {copy['department_id'] for copy in counted}}
    comparison_groups = None
    for copy in counted:
        if not within_dates(copy, a, b, zone):
            continue
        scope, rule = copy['definition']['scope'], copy['definition']['rule']
        source_scope = copy.get('_count_scope', scope)
        tree = scope_tree(source_scope)
        flat = {key: value for key, value in source_scope.items()
                if key not in {'conditions', 'tags', 'capability_ids', 'programme_id', 'rotation_type_id'}}
        projected_tree = {'match': 'all', 'conditions': list((tree or {}).get('conditions') or []) + _requirements(flat)}
        bucket = counting_key(copy)
        line = copy.get('template_line') or {}
        source = (sources or {}).get((copy['id'], line.get('id')))
        display_tree, display_bucket, sides = _reach_filters(source, tree, bucket) if source else (tree, bucket, None)
        display_nodes = _reach_nodes(display_tree)
        display_key = json.dumps(display_tree, sort_keys=True)
        key = (json.dumps(scope, sort_keys=True), copy.get('starts_on'), copy.get('ends_on'), copy['department_id'], display_key)
        if key not in scope_people:
            listed = _listed(scope)
            clinic_people, _ = _skill_context(view, scope, copies, clinic_grants[copy['department_id']])
            found = {}
            for pid in people if listed is None else (pid for pid in people if pid in listed):
                paths = set()
                cuts = sorted(set(dates[pid]) | {point for field in ('starts_on', 'ends_on') if copy.get(field)
                    and a < (point := local_midnight(date.fromisoformat(str(copy[field])), zone)) < b})
                for lo, hi in zip(cuts, cuts[1:]):
                    present = any(member['department_id'] == copy['department_id'] and within_dates(member, lo, hi, zone)
                                  for member in view['_person_rows']['memberships'].get(pid, ()))
                    if present and within_dates(copy, lo, hi, zone):
                        contexts = (_scope_duty_contexts(view, [scope], pid, lo, hi, copy['department_id'])
                                    if pid in clinic_people else [(lo, hi, None)])
                        for start, end, duty in contexts:
                            if scope_matches_throughout(view, scope, pid, duty, start, end):
                                paths.update(matched_paths(display_tree, rule_holds(view, pid, duty, start, end), display_bucket))
                                found.setdefault(pid, set()).update(paths)
            scope_people[key] = found
        row = {'line_id': line.get('id'), 'line': line.get('line'), 'slot': line.get('slot'), 'kind': rule['kind'],
               'bucket': [list(atom) for atom in bucket], 'number': rule_number(rule), 'people': [], 'clinics': [], 'places': [], 'sessions': []}
        if source:
            from .rule_lines import tier
            row['specificity'] = tier(source)
        for pid, paths in sorted(scope_people[key].items()):
            counts = []
            if not loaded:
                counts = _reach_unobserved(rule, a, b)
            elif rule['kind'] in {'max_duties', 'max_hours'}:
                metric_key = (key, pid, copy['department_id'], json.dumps(rule, sort_keys=True))
                if metric_key not in count_cache:
                    selected, unknown = workload_selected(view, copy, pid, duties.get(pid, ()))
                    windows = {window for _, _, lo, hi in selected for window in workload_windows(lo, hi, rule, zone, data['organisation'].get('week_start', 0))
                               if overlaps(a, b, *window)}
                    if not windows:
                        windows = workload_windows(a, b, rule, zone, data['organisation'].get('week_start', 0))
                    for lo, hi in sorted(windows):
                        for group, actual in workload_actual(rule, selected, lo, hi, zone):
                            counts.append({'starts_on': lo.isoformat(), 'ends_on': hi.isoformat(), 'actual': round(actual, 4),
                                           'limit': rule['hours'] if rule['kind'] == 'max_hours' else rule['maximum'],
                                           'comparison': 'maximum', **({rule['group_by']: group} if rule.get('group_by') else {})})
                            if any(overlaps(lo, hi, aware(record['start']), aware(record['end'])) for record in unknown):
                                counts[-1].update(actual=None, status='unresolved')
                    count_cache[metric_key] = counts
                counts = count_cache[metric_key]
            elif rule['kind'] == 'block_quota' and quotas:
                counts = [{k: quota[k] for k in ('starts_on', 'ends_on', 'actual', 'amount', 'comparison', 'metric', 'status', 'proration', 'display') if k in quota}
                          for quota in quotas['rows'] if quota['policy_id'] == copy['id'] and quota['person_id'] == pid
                          and quota.get('line_id') == line.get('id') and quota.get('line') == line.get('line')
                          and quota.get('slot') == line.get('slot') and tuple(tuple(atom) for atom in quota.get('count_bucket', ())) == bucket
                          and quota['comparison'] == rule['comparison']
                          and quota['starts_on'] < b.astimezone(ZoneInfo(zone)).date().isoformat()
                          and a.astimezone(ZoneInfo(zone)).date().isoformat() < quota['ends_on']]
                for count in counts:
                    if count['status'] == 'unresolved':
                        count['actual'] = None
            elif rule['kind'] in {'max_consecutive_days', 'max_consecutive_weekends', 'run_length', 'days_off_run', 'min_rest', 'travel_buffer'}:
                metric_key = (key, pid, json.dumps(rule, sort_keys=True))
                if metric_key not in count_cache:
                    count_cache[metric_key] = _reach_workload_observations(view, copy, pid, duties.get(pid, ()), a, b)
                counts = count_cache[metric_key]
            elif rule['kind'] == 'balance_goal':
                from .fairness import block_of, goal_groups, goal_identity, goal_reports, period_bounds, rota_dates
                if comparison_groups is None:
                    comparison_groups = {goal_identity(source): group for group in goal_groups(view) for source in group}
                group = comparison_groups[goal_identity(copy)]
                identity = tuple(goal_identity(source) for source in group)
                if identity not in goal_cache:
                    goal_cache[identity] = goal_reports(view, [group])[0]
                report = goal_cache[identity]
                first, last = rota_dates(data)
                lo, hi = period_bounds(rule, first, last, block_of(data, pid, first) if rule['period'] == 'block' else None)
                counts = [{**observation, 'starts_on': lo.isoformat(), 'ends_on': hi.isoformat(),
                           'metric': report['metric'], 'comparison': 'target', 'target': observation['expected']}
                          for observation in report['people'] + report['pairs'] if observation['person_id'] == pid]
            row['people'].append({'person_id': pid, 'matched_paths': _reach_display_paths(paths, sides), 'counts': counts})
        # Session selection is shared across person lines; source display paths are rendered separately.
        def only_session(node):
            return all(only_session(child) if 'conditions' in child else child['field'] in SESSION_FIELDS
                       for child in (node or {}).get('conditions') or [])
        projected = session_scope(copy.get('_count_scope', scope)) if only_session(tree) else session_scope(scope)
        matching_scope = session_scope(scope)
        groups = signatures
        if has_session_lines(tree, {'date_set_ids', 'date'}):
            if dated_signatures is None:
                dated_signatures = {}
                for signature, group in signatures.items():
                    for shift in group:
                        day = aware(shift['start']).astimezone(ZoneInfo(zone)).date()
                        dated_signatures.setdefault((*signature, day), []).append(shift)
            groups = dated_signatures
        session_key = (json.dumps(projected, sort_keys=True), bucket, copy.get('starts_on'), copy.get('ends_on'), copy['department_id'])
        if session_key not in session_cache:
            matches = session_cache[session_key] = []
            for group in groups.values():
                for shift in group:
                    lo, hi = max(a, aware(shift['start'])), min(b, aware(shift['end']))
                    if not within_dates(copy, lo, hi, zone) or data['types'][shift['assignment_type_id']]['department_id'] != copy['department_id']:
                        continue
                    if scope_matches_throughout(view, matching_scope, None, shift, lo, hi):
                        matches.append(shift)
                        break
                    if every_day_all_day(matching_scope):
                        break
        locations, clinics = {}, {}
        path_key = (session_key, display_key, display_bucket)
        if path_key not in path_cache:
            path_cache[path_key] = []
            shared_paths, session_paths = None, has_session_lines(display_tree)
            for shift in session_cache[session_key]:
                lo, hi = max(a, aware(shift['start'])), min(b, aware(shift['end']))
                if session_paths or shared_paths is None:
                    paths = matched_paths(display_tree, rule_holds(view, None, shift, lo, hi), display_bucket)
                    if not session_paths:
                        shared_paths = paths
                else:
                    paths = shared_paths
                path_cache[path_key].append((shift, paths))
        for shift, paths in path_cache[path_key]:
            for sid in shift['service_ids']:
                clinics.setdefault(sid, set()).update(paths)
            if shift.get('location_id'):
                selected_places = bucket_atoms(bucket, 'location_ids')
                for place in selected_places or [shift['location_id']]:
                    locations.setdefault(place, set()).update(paths)
        for field, records, identifier, target in (('service_ids', data.get('services') or {}, 'service_id', clinics),
                                                  ('location_ids', data.get('locations') or {}, 'location_id', locations)):
            if not loaded:
                unfiltered = not next(_lines(projected_tree, {field}), None) and not next(_lines(display_tree, {field}), None)
                if unfiltered:
                    def unknown(field, values=()):
                        return None
                    unknown.locations = data.get('locations', {})
                    matches = bucket in bucket_keys(projected_tree, unknown)
                    paths = set(matched_paths(display_tree, unknown, display_bucket)) if matches else set()
                for rid in sorted(records):
                    selected_places = bucket_atoms(bucket, 'location_ids')
                    if field == 'location_ids' and selected_places and rid not in selected_places:
                        continue
                    held = set(ancestor_ids(data, rid)) if field == 'location_ids' else {rid}
                    holds = lambda f, values=(), held=held, field=field: held if f == field else None
                    holds.locations = data.get('locations', {})
                    if unfiltered:
                        if matches:
                            target[rid] = paths
                    elif bucket in bucket_keys(projected_tree, holds):
                        target[rid] = set(matched_paths(display_tree, holds, display_bucket))
            destination = row['clinics'] if field == 'service_ids' else row['places']
            for rid, paths in sorted(target.items()):
                held = set(ancestor_ids(data, rid)) if field == 'location_ids' else {rid}
                matched = _reach_record(display_nodes, paths, field, held, display_bucket)
                if matched is not None:
                    matched['matched_paths'] = _reach_display_paths(matched['matched_paths'], sides)
                    destination.append({identifier: rid, **matched})
        if loaded and rule['kind'] == 'team_lead_requirement':
            row['sessions'] = _reach_leadership(view, copy, a, b)
        if loaded and rule['kind'] in COVERAGE | {'supervision_limit'}:
            row['sessions'] = _reach_coverage(view, copy, a, b)
            if rule['kind'] == 'supervision_limit':
                for person in row['people']:
                    person['counts'] = [count for count in row['sessions'] if count['person_id'] == person['person_id']]
        out.setdefault(copy['id'], {'lines': []})['lines'].append(row)
    return out


def _reach_filters(line, tree, bucket):
    """Project Count ownership onto source Who/Where rows without treating equal selected values as equal branches."""
    from packages.contracts.schemas import _canonical_tree
    sides = [side for side in ('who', 'where') if (line.get(side) or {}).get('conditions')]
    source = {'match': 'all', 'conditions': [line[side] for side in sides]} if sides else None

    def counts(node, path=''):
        if node is None:
            return []
        found = [(path, _canonical_tree(node if 'conditions' in node else {'conditions': [node]}))] if (
            node.get('count') == 'each' and ('conditions' in node or node.get('op') != 'none')) else []
        return found + [item for n, child in enumerate(node.get('conditions') or [])
                        for item in counts(child, f'{path}.{n}' if path else str(n))]

    original, compiled = counts(source), counts(tree)
    # The resolver only lifts non-Count filters; changing Count-node order must update this projection too.
    assert [node for _, node in original] == [node for _, node in compiled], 'Source Count filters differ from compiled filters'
    paths = {compiled_path: source_path for (source_path, _), (compiled_path, _) in zip(original, compiled)}
    key = tuple((paths[path.rpartition(':')[0]] + ':' + path.rpartition(':')[2], value) for path, value in bucket)
    return source, key, sides


def _reach_display_paths(paths, sides):
    return sorted(sides[int(path.split('.')[0])] + '.' + path.partition('.')[2] for path in paths) if sides is not None else sorted(paths)


def _reach_unobserved(rule, a, b, *, status=None):
    """The configured bounds without inventing a count when there is no loaded/observed comparison."""
    kind = rule['kind']
    bounds = []
    if kind in {'max_duties', 'max_consecutive_days', 'max_consecutive_weekends', 'maximum_participants', 'supervision_limit'}:
        if rule.get('maximum') is not None:
            bounds.append(('maximum', rule['maximum']))
    elif kind == 'max_hours':
        bounds.append(('maximum', rule['hours']))
    elif kind in {'minimum_cover', 'days_off_run'}:
        bounds.append(('minimum', rule['minimum']))
    elif kind == 'run_length':
        bounds.extend((key, rule[key]) for key in ('minimum', 'maximum') if rule.get(key) is not None)
    elif kind in {'min_rest', 'travel_buffer'}:
        bounds.append(('minimum', rule['hours'] if kind == 'min_rest' else rule['minutes']))
    elif kind == 'block_quota':
        bounds.append((rule['comparison'], rule['amount']))
    elif kind == 'spread_evenly':
        bounds.append(('maximum', rule.get('maximum_gap')))
    elif kind == 'balance_goal':
        bounds.append(('target', rule.get('amount')))
    return [{'starts_on': a.isoformat(), 'ends_on': b.isoformat(), 'actual': None,
             'comparison': comparison, 'limit': limit, **({'status': status} if status else {})}
            for comparison, limit in bounds]


def _reach_workload_observations(data, policy, pid, entries, a, b):
    """Projection of the validator's run/gap metrics and the shape evaluator's observed runs."""
    from .patterns import shape_counts, shape_duties, shape_types
    from .time import overlaps
    from .validation import consecutive_day_count, workload_day_observations, workload_gaps, workload_selected, workload_weekend_runs
    rule, zone, rows = policy['definition']['rule'], data['organisation']['timezone'], []
    selected, unknown = workload_selected(data, policy, pid, entries)
    focus = {s['id'] for s, _, lo, hi in entries if s['id'] in data['focus_ids'] and overlaps(a, b, lo, hi)}
    if rule['kind'] in {'run_length', 'days_off_run'}:
        for first, last, comparison, actual, limit in shape_counts(rule, shape_types(shape_duties(data, policy, pid, entries))):
            lo, hi = local_midnight(first, zone), local_midnight(last + timedelta(days=1), zone)
            if overlaps(a, b, lo, hi):
                rows.append({'starts_on': lo.isoformat(), 'ends_on': hi.isoformat(), 'actual': actual,
                             'comparison': comparison, 'limit': limit, 'metric': 'days'})
        return rows or _reach_unobserved(rule, a, b, status='no_observations')
    if rule['kind'] in {'min_rest', 'travel_buffer'}:
        targets = ({s['id'] for s, _, lo, hi in entries if s['id'] in focus and policy_applies(data, policy, pid, s, lo, hi)}
                   if rule['kind'] == 'min_rest' else focus)
        factor = 60 if rule['kind'] == 'travel_buffer' else 1
        for observation in workload_gaps(data, rule, selected, targets):
            rows.append({'starts_on': observation['start'].isoformat(), 'ends_on': observation['end'].isoformat(),
                         'actual': None if observation['unknown_location'] else round(observation['actual_hours'] * factor, 4),
                         'limit': observation['required_hours'] * factor, 'comparison': 'minimum',
                         'metric': 'minutes' if factor == 60 else 'hours', 'previous_shift_id': observation['previous']['id'],
                         'next_shift_id': observation['current']['id'],
                         **({'status': 'unresolved'} if observation['unknown_location'] else {})})
        return rows or _reach_unobserved(rule, a, b, status='no_observations')
    if rule['kind'] == 'max_consecutive_days':
        observations = [(run[0], run[-1] + timedelta(days=1), consecutive_day_count(rule, len(run), ordinary))
                        for run, ordinary in workload_day_observations(selected, zone)]
    else:
        observations = [(run[0][0], run[-1][0] + timedelta(days=rule['weekend_days']), len(run))
                        for run in workload_weekend_runs(data, policy, pid, selected, focus)
                        if not rule['overtime_only'] or any(overtime for _, overtime, _ in run[-(rule['maximum'] + 1):])]
    for first, last, actual in observations:
        lo, hi = local_midnight(first, zone), local_midnight(last, zone)
        if overlaps(a, b, lo, hi):
            rows.append({'starts_on': lo.isoformat(), 'ends_on': hi.isoformat(), 'actual': actual,
                         'limit': rule['maximum'], 'comparison': 'maximum',
                         'metric': 'days' if rule['kind'] == 'max_consecutive_days' else 'weekends'})
            if any(overlaps(lo, hi, aware(record['start']), aware(record['end'])) for record in unknown):
                rows[-1].update(actual=None, status='unresolved')
    return rows or [{'starts_on': a.isoformat(), 'ends_on': b.isoformat(), 'actual': None if unknown else 0,
                    'limit': rule['maximum'], 'comparison': 'maximum', **({'status': 'unresolved'} if unknown else {})}]


def _reach_nodes(tree):
    """Source paths are fixed for the line; share their index across all reached records."""
    nodes = {}
    def visit(node, path):
        for index, child in enumerate((node or {}).get('conditions') or []):
            child_path = f'{path}.{index}' if path else str(index)
            nodes[child_path] = child
            if 'conditions' in child:
                visit(child, child_path)
    visit(tree, '')
    return nodes


def _reach_record(nodes, paths, field, held, bucket):
    """Label the matched filter that names this record; Count on an unrelated filter does not relabel it."""
    named = [p for p in paths if nodes.get(p, {}).get('field') == field]
    relevant = [p for p in named if nodes[p].get('op') == 'not_empty'
                or held.intersection(absolute_values(nodes[p]) or ())] if named else list(paths)
    if named and not relevant:
        return None
    return {'matched_paths': sorted(relevant), 'count': bucket_count(bucket, field, relevant) if named else 'together'}


def _reach_coverage(data, policy, a, b):
    """The validator's qualified continuous staffing counts, projected onto this line's reached sessions."""
    from .conditions import counting_key
    from .participation import presence_intervals
    from .time import overlaps
    from .validation import authority_intervals, person_rows, qualified_supervisor, staffing_counts, staffing_peers, supervision_commitments, supervision_counts
    rule, scope = policy['definition']['rule'], policy['definition']['scope']
    zone, focus = data['organisation']['timezone'], set(data['focus_ids'])
    if rule['kind'] == 'supervision_limit':
        data = {**data, '_supervision_commitments': supervision_commitments(data)}
        qualification = {**data, '_supervision_commitments': {}}
    rows = []
    for shift in data['shifts']:
        start, end = max(a, aware(shift['start'])), min(b, aware(shift['end']))
        if shift['id'] not in focus or inactive(shift) or start >= end:
            continue
        if not within_dates(policy, start, end, zone) or data['types'][shift['assignment_type_id']]['department_id'] != policy['department_id'] or not scope_matches_throughout(data, session_scope(scope), None, shift, start, end):
            continue
        peers = [shift]
        if rule.get('aggregation') in {'service', 'service_group'}:
            peers = staffing_peers(data, policy, start, end)
        cuts = {start, end}
        for peer in peers:
            cuts.update(t for t in (aware(peer['start']), aware(peer['end'])) if start < t < end)
            for person in peer['participants']:
                present = presence_intervals(person)
                cuts.update(t for pair in present for t in pair if start < t < end)
                if rule['kind'] == 'supervision_limit' and any(
                        qualified_supervisor(qualification, peer, person, a, b)
                        for lo, hi in present for a, b in authority_intervals(data, lo, hi)):
                    for sid, lo, hi in data['_supervision_commitments'].get(person['person_id'], ()):
                        if sid != peer['id'] and overlaps(start, end, lo, hi):
                            cuts.update(t for t in (lo, hi) if start < t < end)
                cuts.update(t for t in change_points(data, person['person_id'], start, end) if start < t < end)
                for absence in person_rows(data, 'availability', person['person_id']):
                    cuts.update(t for t in (aware(absence['start']), aware(absence['end'])) if start < t < end)
                for restriction in data['policies']:
                    if restriction['definition']['rule']['kind'] == 'restriction' and policy_applies(data, restriction, person['person_id'], peer, start, end):
                        cuts.update(t for pair in restricted_intervals(data, restriction, start, end, person['person_id']) for t in pair if start < t < end)
        cuts.update(t for pair in scope_intervals(data, scope, None, start, end) for t in pair if start < t < end)
        for lo, hi in zip(sorted(cuts), sorted(cuts)[1:]):
            if not within_dates(policy, lo, hi, zone) or not scope_matches_throughout(data, session_scope(scope), None, shift, lo, hi):
                continue
            if rule['kind'] == 'supervision_limit':
                for pid, detail in supervision_counts(data, policy, shift, lo, hi):
                    rows.append({'shift_id': shift['id'], 'person_id': pid, 'starts_on': lo.isoformat(), 'ends_on': hi.isoformat(),
                                 'actual': detail['scheduled'], 'limit': detail['maximum'], 'comparison': 'maximum',
                                 'bucket': [list(atom) for atom in counting_key(policy)],
                                 **{k: v for k, v in detail.items() if k not in {'scheduled', 'maximum'}}})
                continue
            for detail in sorted(staffing_counts(data, policy, shift, lo, hi, peers=peers), key=lambda row: row.get('service_id', '')):
                result = {'shift_id': shift['id'], 'starts_on': lo.isoformat(), 'ends_on': hi.isoformat(),
                          'actual': detail['available'] if rule['kind'] == 'minimum_cover' else detail['scheduled'],
                          'limit': detail['required'] if rule['kind'] == 'minimum_cover' else detail['maximum'],
                          'comparison': 'minimum' if rule['kind'] == 'minimum_cover' else 'maximum',
                          'bucket': [list(atom) for atom in counting_key(policy)],
                          **{k: v for k, v in detail.items() if k in {'service_id', 'permitted_maximum'}}}
                rows.append(result)
    return rows


def _reach_leadership(data, policy, a, b):
    """Observe the same flagged, qualified coverage as validation, retaining each source line."""
    from . import leadership
    from .validation import coverage_boundaries, coverage_boundary_inputs
    data = {**data, 'policies': active_rules(data)}
    inputs = coverage_boundary_inputs(data)
    line = policy.get('template_line') or {}
    rows = []
    for shift in data.get('shifts', []):
        if shift['id'] not in data['focus_ids'] or inactive(shift):
            continue
        start, end = max(a, aware(shift['start'])), min(b, aware(shift['end']))
        if start >= end:
            continue
        bounds = sorted(coverage_boundaries(data, shift, start, end, inputs=inputs))
        for lo, hi in zip(bounds, bounds[1:]):
            rules = leadership.requirements(data, shift, lo, hi)
            own = [r for r in rules if r.get('policy', {}).get('id') == policy['id']
                   and (r['policy'].get('template_line') or {}) == line]
            if not own:
                continue
            lead = leadership.counts(data, shift, lo, hi)
            for r in own:
                if r.get('count_scope', 'duty') == 'concurrent_duties':
                    groups = [g for g in lead.get('_groups', ()) if g['key'] == leadership._requirement_key(r)
                              and g['start'] <= lo and hi <= g['end']]
                    if not groups or not leadership.is_group_anchor(data, shift, r, lo, hi):
                        continue
                    actual_available, actual_scheduled = groups[0]['available'], groups[0]['scheduled']
                    affected = [duty['id'] for duty, _ in groups[0]['duties']]
                else:
                    actual_available, actual_scheduled, affected = lead['available'], lead['scheduled'], [shift['id']]
                for comparison, actual, limit in (('minimum', actual_available, r['minimum']),
                                                   ('maximum', actual_scheduled, r['maximum'])):
                    if limit is not None:
                        rows.append({'shift_id': shift['id'], 'starts_on': lo.isoformat(), 'ends_on': hi.isoformat(),
                                     'actual': actual, 'limit': limit, 'comparison': comparison,
                                     'lead_sources': [leadership.source(r)], 'affected_shift_ids': affected, 'bucket': []})
    return rows
