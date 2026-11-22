"""Similar rules become one rule: the planner for grouping, and the proof that a grouping changes nothing.

Rules that say the same thing for different people, programmes, sessions or dates become one rule, in four steps:
- series: copies of one rule for back-to-back periods (a weekly ceiling for 19 weeks) become one rule over the whole
  period; a quota then counts each week (or month, or N days) as its own block, exactly as the copies did;
- places: copies of one restriction that differ only by the clinic become one restriction for all those clinics;
- lines: rules that differ only by who (a person or a programme), their number, dates, days, hours, clinics or duty
  types become one rule for its lines only: each line is one of the old rules, as a narrower copy of the new one;
- kinds: rules under different names that are the same rule for different lines (the same settings, names sharing a
  word: "Residents weekly UCC ceiling" and "Rotators weekly UCC ceiling"), including rules grouped before, become one
  rule; its lines may also pick ranks, and an allocation order's lines set each clinic's session priority.

`plan` is pure: callers pass the rules and the names to leave out of rule names; each group's rule is a normal rule
input. `expanded` reads rules the way every check does (the one `active_rules` reading), in a form where a group and
the rules it replaces compare equal; `same_checks` compares validator findings and quota rows after the id mapping.
"""
from .time import now as clock_now
import itertools
import json
import re
from collections import Counter, defaultdict
from datetime import date

from packages.contracts.schemas import LINE_KINDS, LINE_SETTINGS, PER_PERSON_FIELD, PER_PERSON_KINDS, PolicyDefinition, PolicyInput, line_field
from .conditions import INHERITED_LEVEL, OUTRANKED, OVERRIDDEN, OWN_LINE, _requirements, absolute_values, active_rules, effective_severity, has_each, has_session_lines, level_selector, source_index
from .quotas import window_blocks

MAX_LINES = 2000        # per_person max_length
MAX_MERGED = 500        # merged_ids max_length
MAX_REQUIRED = 100      # required overtime limits of one rule
CAP_KINDS = {'forbidden_transition', 'max_consecutive_weekends'}
COVERAGE = {'minimum_cover', 'maximum_participants'}
# Checked session by session on the session's day: copies for back-to-back dates read as one over all the dates.
DAY_LOCAL = {'restriction', 'allocation_overtime', 'minimum_cover', 'maximum_participants', 'supervision_limit', 'spread_evenly'}
PEOPLE = ('person_id', 'person_ids')
# What a line may narrow (and so what may differ between rules that become one): sessions, and the programme when
# the rules do not name people.
SESSION = ('weekdays', 'start_time', 'end_time', 'assignment_type_id', 'service_id', 'service_ids')
RANKS = ('category_id', 'category_ids')
# What a line of a rule may say (besides its number and dates).
LINE_FIELDS = ('person_id', 'programme_id', 'category_ids', 'service_ids', 'assignment_type_ids', 'weekdays', 'start_time', 'end_time')
# Words too common to say two rule names are about the same thing.
STOP_WORDS = {'the', 'and', 'for', 'per', 'with', 'from', 'all', 'any', 'not', 'only', 'each', 'rule', 'rules', 'day', 'days', 'time', 'times'}
# Restrictions named like this are protected time: linked to the academic calendar one entry each, never joined to others.
PROTECTED_TIME = r"protect|academic|lecture|educational|teaching"


def _day(value):
    return date.fromisoformat(str(value)[:10]) if value else None


def normal(definition):
    """A definition with every default written out, so equal rules compare equal."""
    return PolicyDefinition.model_validate(definition, context={'historical': True}).model_dump(mode='json')


def is_cap(policy):
    rule, definition = policy['definition']['rule'], policy['definition']
    return (policy['status'] == 'active' and policy.get('active', True)
            and effective_severity(rule['kind'], definition.get('severity')) == 'mandatory'
            and (rule['kind'] == 'block_quota' and rule['comparison'] in ('maximum', 'range') or rule['kind'] in CAP_KINDS))


def rule_stem(name, who=()):
    """A rule's name without the names (people, programmes, clinics) or dates in it
    ("Kopofiku – Weekly clinical target – 2026-01-05" → "Weekly clinical target")."""
    for text in sorted((t for t in who if t), key=len, reverse=True):
        name = name.replace(text, '')
    name = re.sub(r'\d{4}-\d{2}(-\d{2})?', '', name)
    name = re.sub(r'\s+[·\-–]\s+\d+\s*$', '', name)  # "… · 3" copy numbers
    name = re.sub(r'\s+\d+\s*/\s*\d+\s*$', '', name)  # "… 1 / 2" copy numbers
    parts = [x.strip() for x in re.split(r'\s+[·\-–]\s+|^\s*[·\-–]\s*|\s*[·\-–]\s*$', name) if x and x.strip()]
    out = ' · '.join(parts)
    return out[:1].upper() + out[1:] if out else name


def _people(scope):
    return list(scope.get('person_ids') or ([scope['person_id']] if scope.get('person_id') else []))


def _key(*parts):
    return json.dumps(parts, sort_keys=True, default=str)


def _groupable(p, waiting):
    d = p['definition']
    return (p['status'] == 'active' and p.get('active', True) and p['id'] not in waiting and not d.get('per_person')
            and not d.get('overrides') and not d.get('merged_ids') and not d.get('calendar_entry_id') and not d.get('replaces_id'))


def _joinable(p, waiting):
    """A rule for its lines only (grouped before, or written so): it may join rules under other names (the kinds step)."""
    d = p['definition']
    return bool(p['status'] == 'active' and p.get('active', True) and p['id'] not in waiting and (not d.get('per_person') or d.get('listed_only'))
                and not d.get('overrides') and not d.get('calendar_entry_id'))


def _number_field(definition):
    """The number a rule's lines set: an allocation order (clinics ranked by session priority, nothing else) ranks its
    lines by session priority; every other rule, its own number."""
    rule = definition['rule']
    if definition.get('line_number'):
        return definition['line_number']
    if (rule['kind'] == 'allocation_preference' and rule.get('session_priority') and not rule.get('priority')
            and not rule.get('overtime_priority') and rule.get('balance_by', 'none') == 'none'):
        return 'session_priority'
    return PER_PERSON_FIELD.get(rule['kind'])


def plan(policies, labels=None, week_start=0, held=()):
    """Proposed groups for the active rules in `policies` (pass every status: proposed groups hold their rules back,
    and retired rules tell whether an overtime rule's required limits are in force). `labels` maps 'people',
    'programmes' and 'services' ids to names (left out of group names). `week_start` is the department's first weekday.
    Each group: {shape, rule_ids, name, kind, payload | problem, ...}."""
    labels = labels or {}
    waiting = {i for p in policies if p['status'] == 'proposed' for i in p['definition'].get('merged_ids') or []}
    pool, joinable = [], []
    for p in policies:
        if p['id'] in held:
            continue
        into = pool if _groupable(p, waiting) else joinable if _joinable(p, waiting) else None
        if into is not None:
            try:
                into.append({**p, 'definition': normal(p['definition']), '_ids': [p['id']], '_shapes': []})
            except ValueError:
                continue
    from .generation import overtime_cap_ids
    caps = overtime_cap_ids({'policies': policies})
    ctx = {'caps': caps, 'week_start': week_start, 'people': list(labels.get('people', {}).values()),
           'words': [n for kind in ('people', 'programmes', 'services') for n in labels.get(kind, {}).values()]}
    problems, made = [], 0
    for shape, finder in (('series', _series), ('places', _places), ('lines', _lines), ('kinds', _kinds)):
        if shape == 'kinds':
            pool += joinable
        taken, found = set(), []
        for members, payload in finder(pool, ctx):
            ids = [i for m in members for i in m['_ids']]
            try:
                PolicyInput.model_validate({**payload, 'definition': {**payload['definition'], 'merged_ids': ids[:MAX_MERGED]}})
                if len(ids) > MAX_MERGED:
                    raise ValueError(f'A rule can replace at most {MAX_MERGED} rules')
                # Never propose a rule that checks anything differently from the rules it replaces.
                probe = {**payload, 'id': '~probe', 'status': 'active', 'active': True, 'department_id': members[0].get('department_id')}
                if expanded(members, {m['id']: '~probe' for m in members}, week_start) != expanded([probe], None, week_start):
                    raise ValueError('Joined, these rules would check something differently; they stay separate')
            except ValueError as error:
                problems.append({'shape': shape, 'rule_ids': ids, 'kind': members[0]['kind'], 'name': payload['name'], 'problem': str(error)[:300]})
                continue
            taken.update(m['id'] for m in members)
            made += 1
            found.append({'id': f'~{made}', 'status': 'active', 'active': True, 'kind': members[0]['kind'], 'name': payload['name'],
                          'owner': payload['owner'], 'starts_on': payload['starts_on'], 'ends_on': payload['ends_on'],
                          'department_id': members[0].get('department_id'),
                          'definition': normal({**payload['definition'], 'merged_ids': []}), '_ids': ids,
                          '_shapes': sorted({shape, *(s for m in members for s in m['_shapes'])}), '_payload': payload})
        pool = [p for p in pool if p['id'] not in taken] + found
    groups = [_describe(item, caps) for item in pool if '_payload' in item]
    _distinct_names(groups, labels)
    groups.sort(key=lambda g: (-len(g['rule_ids']), g['name']))
    return groups + problems


def _describe(item, caps):
    payload = {**item['_payload'], 'source': f"Grouped from {len(item['_ids'])} similar rules"}
    payload['definition'] = {**payload['definition'], 'merged_ids': item['_ids']}
    d = payload['definition']
    lines = d.get('per_person') or []
    field = line_field(d)
    values = [r['amount'] for r in lines if r.get('amount') is not None] + ([d['rule'][field]] if field and not d.get('line_number') else [])
    required = d['rule'].get('required_policy_ids') or []
    return {'shape': '+'.join(item['_shapes']), 'rule_ids': item['_ids'], 'kind': item['kind'], 'name': payload['name'],
            'lines': len(lines), 'people': len({r['person_id'] for r in lines if r.get('person_id')}),
            'amounts': [min(values), max(values)] if values else None, 'default_amount': d['rule'].get(field) if field else None,
            'starts_on': payload['starts_on'], 'ends_on': payload['ends_on'],
            'switched_off': bool(required) and not set(required) <= caps, 'payload': payload}


def _distinct_names(groups, labels):
    """Two groups with the same name say what tells them apart (the programme, else the number, else the start)."""
    programmes, people = labels.get('programmes', {}), labels.get('people', {})

    def said(g):  # what may tell a group apart, in order
        scope, rule = g['payload']['definition']['scope'], g['payload']['definition']['rule']
        return [programmes.get(scope.get('programme_id') or ''),
                'with ' + ', '.join(people.get(i, 'a named person') for i in rule['when_person_ids']) if rule.get('when_person_ids') else None,
                f"{g['default_amount']:g}" if isinstance(g.get('default_amount'), (int, float)) else None]
    by_name = {}
    for g in groups:
        by_name.setdefault(g['name'], []).append(g)
    for same in by_name.values():
        if len(same) < 2:
            continue
        # The first thing that differs between them is added to the names that have it.
        n = next((i for i in range(3) if len({said(g)[i] for g in same}) > 1), None)
        for g in same if n is not None else ():
            extra = said(g)[n]
            if extra and extra.casefold() not in g['name'].casefold():
                g['name'] = g['payload']['name'] = f"{g['name']} · {extra}"[:160]
    seen = Counter(g['name'] for g in groups)
    for g in groups:
        if seen[g['name']] > 1:  # runs of one rule with a gap between them
            g['name'] = g['payload']['name'] = f"{g['name']} · from {g['payload']['starts_on']}"[:160]


def _concept(p, ctx):
    return rule_stem(p['name'], ctx['words'])


def _shared_words(stems):
    """The words every name has, in the first name's order ("UCC senior floor" + "UCC trainee floor" → "UCC floor");
    a last "· word" that only names what differs is left out."""
    sets = [{w.casefold() for w in x.split()} for x in stems[1:]]
    shared = ' '.join(w for w in stems[0].split() if all(w.casefold() in t for t in sets)).strip(' ·-–')
    head, dot, tail = shared.rpartition(' · ')
    if dot and len(tail.split()) == 1:
        shared = head
    return shared[:1].upper() + shared[1:] if len(shared.split()) >= 2 else ''


QUOTA_UNITS = {'duties': 'duty', 'session_units': 'session-unit', 'scheduled_hours': 'hours', 'reserved_hours': 'reserved-hours',
               'workload_credit': 'workload', 'training_credit': 'training', 'lead_turns': 'lead-turn'}


def _shape_name(definition):
    """A name from what the rule is, for rules joined under names that share too little."""
    rule, lines = definition['rule'], definition.get('per_person') or []
    if rule['kind'] == 'block_quota':
        every = {'calendar_week': 'Weekly ', 'calendar_month': 'Monthly ', 'fixed': f"{rule.get('window_days')}-day "}.get(rule.get('window_alignment'), '')
        what = {'exclude': 'ordinary ', 'only': 'overtime '}.get(rule.get('overtime'), '') + QUOTA_UNITS.get(rule['metric'], rule['metric'])
        name = f"{every}{what} {({'maximum': 'ceiling', 'minimum': 'minimum', 'target': 'target', 'range': 'range'})[rule['comparison']]}"
        return name[:1].upper() + name[1:]
    if rule['kind'] == 'restriction':
        used = [what for what, key in (('clinics', 'service_ids'), ('duty types', 'assignment_type_ids'), ('days', 'weekdays'))
                if any(r.get(key) for r in lines)]
        return 'Excluded ' + used[0] if len(used) == 1 else 'Exclusions'
    if rule['kind'] == 'allocation_preference':
        return 'Allocation order' if definition.get('line_number') == 'session_priority' else 'Personal preferences'
    return ''


def _common_name(members, ctx, definition=None):
    """The shared name: the stem most of them have, or the words every name has, in order ("UCC weekday morning senior
    floor" + "UCC weekend senior floor" → "UCC senior floor"), or else a name from what the joined rule is."""
    named = [rule_stem(p['name'], ctx['people']) for p in members]
    if len(set(named)) == 1:  # the same name once the people and dates are left out: keep the programme or clinic in it
        return named[0]
    stems = [_concept(p, ctx) for p in members]
    if len(set(stems)) == 1:
        return stems[0]
    shared = _shared_words(stems) or _shared_words(named)
    if shared:
        return shared
    if len(set(stems)) < len(stems):
        return Counter(stems).most_common(1)[0][0]
    return (_shape_name(definition) if definition else '') or named[max(
        range(len(members)), key=lambda i: (len(members[i].get('_ids') or [1]), len(members[i]['definition'].get('per_person') or []), -i))]


def _payload(members, ctx, **definition):
    first = members[0]
    ends = [p['ends_on'] for p in members]
    return {'name': _common_name(members, ctx, {**first['definition'], **definition})[:160],
            'owner': Counter(p['owner'] for p in members).most_common(1)[0][0],
            'source': f'Grouped from {len(members)} similar rules',
            'starts_on': min(str(p['starts_on']) for p in members),
            'ends_on': None if None in ends else max(str(e) for e in ends), 'active': True,
            'definition': {**first['definition'], 'merged_ids': [], **definition}}


def _span(p):
    rule = p['definition']['rule']
    if rule['kind'] == 'block_quota':
        return rule['starts_on'], rule['ends_on']
    return str(p['starts_on']), str(p['ends_on']) if p['ends_on'] else None


def _plain_quota(rule):
    return not rule['window_days'] and rule['window_alignment'] == 'rolling' and not rule['prorate_department_days']


# Series ----------------------------------------------------------------------------------------------------------------

def _series(pool, ctx):
    buckets = {}
    for p in pool:
        d, scope, rule = p['definition'], p['definition']['scope'], p['definition']['rule']
        if _people(scope) or d['per_person']:
            continue
        quota = rule['kind'] == 'block_quota'
        if quota and (not _plain_quota(rule) or d['exception_allowed'] or (str(p['starts_on']), str(p['ends_on'])) != (rule['starts_on'], rule['ends_on'])):
            continue  # plain blocks only, whose rule's dates are its block
        if not quota and (not p['ends_on'] or rule['kind'] not in DAY_LOCAL):
            continue
        rest = {k: v for k, v in rule.items() if k not in ('starts_on', 'ends_on')}
        key = _key(rule['kind'], d['severity'], d['strength'], d['exception_allowed'], d['workspace_id'], rest, scope, _concept(p, ctx))
        buckets.setdefault(key, []).append(p)
    for members in buckets.values():
        members.sort(key=lambda p: str(p['starts_on']))
        runs = [[members[0]]]
        for p in members[1:]:
            joins = str(p['starts_on']) == str(runs[-1][-1]['ends_on'])
            if joins and p['kind'] == 'block_quota':
                joins = (_day(p['ends_on']) - _day(runs[-1][0]['starts_on'])).days <= 366
            if joins:
                runs[-1].append(p)
            else:
                runs.append([p])
        for run in runs:
            if len(run) < 2:
                continue
            rule = dict(run[0]['definition']['rule'])
            if rule['kind'] == 'block_quota':
                repeat = _repeat_lines([[_span(p) for p in run]], ctx['week_start'])
                if not repeat:
                    continue
                rule.update(starts_on=str(run[0]['starts_on']), ends_on=str(run[-1]['ends_on']), **repeat['rule'])
            yield run, _payload(run, ctx, rule=rule)


# Places ----------------------------------------------------------------------------------------------------------------

def _places(pool, ctx):
    buckets = {}
    for p in pool:
        d, scope, rule = p['definition'], p['definition']['scope'], p['definition']['rule']
        if rule['kind'] != 'restriction' or _people(scope) or d['per_person'] or not (scope.get('service_id') or scope.get('service_ids')):
            continue
        where = {k: v for k, v in scope.items() if k not in ('service_id', 'service_ids')}
        key = _key(d['severity'], d['strength'], d['exception_allowed'], d['workspace_id'], rule, where,
                   str(p['starts_on']), str(p['ends_on']), _concept(p, ctx))
        buckets.setdefault(key, []).append(p)
    for members in buckets.values():
        services = list(dict.fromkeys(s for p in members for s in p['definition']['scope']['service_ids'] or [p['definition']['scope']['service_id']]))
        if len(members) < 2 or len(services) > 32:
            continue
        members.sort(key=lambda p: p['name'])
        scope = {**members[0]['definition']['scope'], 'service_id': None, 'service_ids': services}
        yield members, _payload(members, ctx, scope=scope)


# Lines -----------------------------------------------------------------------------------------------------------------

def _settings(p, ctx, field, concept):
    """What must be equal for rules to be one rule: everything but who, which sessions, the number and the dates.
    None: the rule cannot become a line of another."""
    d, scope, rule = p['definition'], p['definition']['scope'], p['definition']['rule']
    rest = {k: v for k, v in rule.items() if k not in (field, 'starts_on', 'ends_on', 'required_policy_ids', 'window_alignment', 'window_days')}
    if rule['kind'] == 'block_quota':
        rest['_repeat'] = _alignment(rule, ctx['week_start'], d['exception_allowed'])
        if rule['prorate_department_days']:
            rest['_block'] = (rule['starts_on'], rule['ends_on'])  # a prorated target keeps its own dates
    vary = SESSION + ('programme_id',) + (RANKS if concept is None else ())
    where = {k: v for k, v in scope.items() if k not in PEOPLE + vary}
    # Overtime needs its required limits in force: rules that are switched on and off never share one rule.
    on = rule['kind'] != 'allocation_overtime' or set(rule['required_policy_ids']) <= ctx['caps']
    return _key(rule['kind'], d['severity'], d['strength'], d['exception_allowed'], d['workspace_id'], rest, where, on, field,
                bool(_people(scope)) if concept is not None else None, concept, _used_settings(d))


def _own(row):
    """A line's own settings that are set."""
    return {k: v for k, v in (row.get('settings') or {}).items() if v is not None}


def _used_settings(definition):
    """The settings a rule's lines set for themselves: rules whose lines differ in what they set stay apart (a
    companion cover, whose lines apply when someone is on duty, never joins a staffing table)."""
    return sorted({k for r in definition.get('per_person') or [] for k in _own(r)})


def _lines(pool, ctx):
    buckets = {}
    for p in pool:
        d, scope, rule = p['definition'], p['definition']['scope'], p['definition']['rule']
        people = _people(scope)
        if d['per_person'] or rule['kind'] not in (PER_PERSON_KINDS if people else LINE_KINDS):
            continue
        # Staffing levels are named after their sessions; other rules must share a name (without names and dates).
        concept = '' if rule['kind'] in COVERAGE else _concept(p, ctx)
        buckets.setdefault(_settings(p, ctx, _number_field(d), concept), []).append(p)
    for members in buckets.values():
        if len(members) > 1:
            yield from _line_chunks(members, ctx)


def _words(name):
    """The words of a rule name that say what it is about (their first five letters: "clinic" and "clinics" are one)."""
    return {w.lower()[:5] for w in re.findall(r'[^\W\d_][\w\-/]+', name) if len(w) >= 3 and w.lower() not in STOP_WORDS}


def _kinds(pool, ctx):
    """Rules under different names that are one rule for different lines: the same settings, and names that share a
    word (so an attendance target never joins a clinic placement). Rules grouped before join too, line by line.
    Mandatory restrictions (not protected time) are all one idea whatever their names, "keep these people off these
    sessions": they join into one table of exclusions, lines that overlap included (a session that breaks two lines
    is reported by each, as two clinics of one restriction are)."""
    buckets = {}
    for p in pool:
        d, rule = p['definition'], p['definition']['rule']
        if rule['kind'] not in LINE_KINDS or d['overrides'] or d['per_person'] and not d['listed_only']:
            continue
        if rule['kind'] == 'restriction' and re.search(PROTECTED_TIME, p['name'], re.I):
            continue
        buckets.setdefault(_settings(p, ctx, _number_field(d), None), []).append(p)
    for members in buckets.values():
        if len(members) < 2:
            continue
        exclusions = members[0]['definition']['rule']['kind'] == 'restriction' and members[0]['definition']['severity'] == 'mandatory'
        if exclusions:
            yield from _line_chunks(members, ctx, overlap=True)
            continue
        # Names that share a word belong together (a chain of shared words joins them all).
        words = [_words(_concept(p, ctx)) for p in members]
        parent = list(range(len(members)))

        def root(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        for i, j in itertools.combinations(range(len(members)), 2):
            if words[i] & words[j]:
                parent[root(i)] = root(j)
        together = {}
        for i, p in enumerate(members):
            together.setdefault(root(i), []).append(p)
        for group in together.values():
            if len(group) > 1:
                yield from _line_chunks(group, ctx)


def _alignment(rule, week_start, exceptions=False):
    """A complete aligned plain week can join weekly quotas; partial blocks retain their own denominator.
    A quota that allows exceptions keeps its blocks
    (an exception covers one whole block)."""
    if rule['window_alignment'] != 'rolling' or rule['window_days'] or exceptions:
        return rule['window_alignment'], rule['window_days']
    s, e = _day(rule['starts_on']), _day(rule['ends_on'])
    if not rule['prorate_department_days'] and s.weekday() == week_start and (e - s).days == 7:
        return 'calendar_week', None
    return 'rolling', None


def _ranks(scope):
    return scope.get('category_ids') or ([scope['category_id']] if scope.get('category_id') else [])


def _services(scope):
    return scope.get('service_ids') or ([scope['service_id']] if scope.get('service_id') else [])


def _narrowing(p, vary):
    """What one rule narrows, as line fields, for the fields that differ between the rules being joined."""
    scope, line = p['definition']['scope'], {}
    if 'programme_id' in vary and scope.get('programme_id'):
        line['programme_id'] = scope['programme_id']
    if 'ranks' in vary and _ranks(scope):
        line['category_ids'] = _ranks(scope)
    if 'services' in vary and _services(scope):
        line['service_ids'] = _services(scope)
    if 'assignment_type_id' in vary and scope.get('assignment_type_id'):
        line['assignment_type_ids'] = [scope['assignment_type_id']]
    if 'weekdays' in vary and sorted(scope['weekdays']) != list(range(7)):
        line['weekdays'] = scope['weekdays']
    if 'times' in vary and (scope['start_time'], scope['end_time']) != ('00:00', '24:00'):
        line.update(start_time=scope['start_time'], end_time=scope['end_time'])
    return line


def _rows(p, vary, field):
    """A rule as lines of the joined rule: (line, its dates, its number). A rule without lines is one line per person
    (or one line); a rule for its lines only gives its lines, with its own settings that differ written on each. None
    when a line cannot say the same (a duty type on the rule and other duty types on its line)."""
    d, scope = p['definition'], p['definition']['scope']
    number = d['rule'].get(field) if field else None
    base, span = _narrowing(p, vary), _span(p)
    if not d['per_person']:
        return [({**base, 'person_id': pid}, span, number) for pid in _people(scope)] or [(base, span, number)]
    rows = []
    for r in d['per_person']:
        line = {k: r[k] for k in LINE_FIELDS if r.get(k) not in (None, [])}
        if _own(r):
            line['settings'] = _own(r)
        if 'assignment_type_ids' in line and 'assignment_type_ids' in base:  # both hold: the rule's one duty type
            if not set(base['assignment_type_ids']) <= set(line['assignment_type_ids']):
                return None
            line['assignment_type_ids'] = base['assignment_type_ids']
        for k, v in base.items():  # a line's own clinics, days, hours and ranks replace the rule's
            line.setdefault(k, v)
        if 'start_time' in line and 'end_time' not in line:
            line['end_time'] = base['end_time']
        s = max(str(r['starts_on']), span[0]) if r.get('starts_on') else span[0]
        e = min(str(r['ends_on']), span[1]) if r.get('ends_on') and span[1] else str(r['ends_on']) if r.get('ends_on') else span[1]
        rows.append((line, (s, e), r['amount'] if r.get('amount') is not None else number))
    return rows


def _apart(a, b):
    """Two lines (with their dates) can never check the same person on the same session."""
    (x, (s1, e1)), (y, (s2, e2)) = a, b
    for who in ('person_id', 'programme_id'):
        if x.get(who) and y.get(who) and x[who] != y[who]:
            return True
    if e1 and s2 >= e1 or e2 and s1 >= e2:
        return True
    for many in ('weekdays', 'service_ids', 'assignment_type_ids', 'category_ids'):
        if x.get(many) is not None and y.get(many) is not None and not set(x[many]) & set(y[many]):
            return True
    if x.get('start_time') and y.get('start_time') and (x['end_time'] <= y['start_time'] or y['end_time'] <= x['start_time']):
        return True
    return False


def _varying(members):
    def varies(get):
        return len({_key(get(p['definition']['scope'])) for p in members}) > 1
    vary = {k for k in ('programme_id', 'assignment_type_id', 'weekdays') if varies(lambda s, k=k: s.get(k))}
    if varies(_services):
        vary.add('services')
    if varies(_ranks):
        vary.add('ranks')
    if varies(lambda s: (s['start_time'], s['end_time'])):
        vary.add('times')
    return vary


def _line_chunks(members, ctx, overlap=False):
    kind = members[0]['definition']['rule']['kind']
    field, quota = _number_field(members[0]['definition']), kind == 'block_quota'
    members = sorted(members, key=lambda p: (_span(p)[0], p['name']))
    vary = _varying(members)
    chunks = []
    for p in members:
        rows = _rows(p, vary, field)
        if rows is None:
            continue
        span = _span(p)
        for chunk in chunks:
            fits = (sum(len(m['_ids']) for m in chunk['members']) + len(p['_ids']) <= MAX_MERGED and len(chunk['rows']) + len(rows) <= MAX_LINES
                    and (not quota or (_day(span[1]) - _day(chunk['first'])).days <= 366))
            if fits and quota:
                fits = not any(_key(r, s) in chunk['blocks'] for r, s, _ in rows)
            elif fits and not overlap:
                fits = all(_apart((r, s), (o, t)) for r, s, _ in rows for o, t, _ in chunk['rows'])
            if fits and kind == 'allocation_overtime':
                fits = len(chunk['required'] | set(p['definition']['rule']['required_policy_ids'])) <= MAX_REQUIRED
            if fits:
                break
        else:
            chunk = {'members': [], 'rows': [], 'blocks': set(), 'first': span[0], 'required': set()}
            chunks.append(chunk)
        chunk['members'].append(p)
        chunk['rows'] += rows
        chunk['blocks'].update(_key(r, s) for r, s, _ in rows)
        if kind == 'allocation_overtime':
            chunk['required'] |= set(p['definition']['rule']['required_policy_ids'])
    for chunk in chunks:
        if len(chunk['members']) > 1:
            yield chunk['members'], _joined(chunk['members'], vary, ctx)


def _joined(members, vary, ctx):
    """The rule for these rules' lines only."""
    first_definition = members[0]['definition']
    kind = first_definition['rule']['kind']
    field, quota = _number_field(first_definition), kind == 'block_quota'
    # An allocation order says each clinic's place in the order on its line: every line has its own number.
    order = field != PER_PERSON_FIELD.get(kind)
    rule = dict(first_definition['rule'])
    if quota:
        rule['window_alignment'], rule['window_days'] = _alignment(rule, ctx['week_start'], first_definition['exception_allowed'])
    rows = [row for p in members for row in _rows(p, vary, field)]
    amount = (0 if order else Counter(n for _, _, n in rows).most_common(1)[0][0]) if field else None
    if field:
        rule[field] = amount
    if kind == 'allocation_overtime':
        rule['required_policy_ids'] = list(dict.fromkeys(i for p in members for i in p['definition']['rule']['required_policy_ids']))
    spans = [_span(p) for p in members]
    if quota:
        rule.update(starts_on=min(s for s, _ in spans), ends_on=max(e for _, e in spans))
    scope = {k: v for k, v in first_definition['scope'].items() if k not in PEOPLE}
    neutral = {'programme_id': {'programme_id': None}, 'assignment_type_id': {'assignment_type_id': None}, 'weekdays': {'weekdays': list(range(7))},
               'services': {'service_id': None, 'service_ids': []}, 'times': {'start_time': '00:00', 'end_time': '24:00'},
               'ranks': {'category_id': None, 'category_ids': []}}
    for k in vary:
        scope.update(neutral[k])
    payload = _payload(members, ctx, scope=scope, rule=rule, listed_only=True, per_person=[], overrides=[], replaces_id=None,
                       line_number=field if order else None)
    first, last = (rule['starts_on'], rule['ends_on']) if quota else (payload['starts_on'], payload['ends_on'])
    # Each line: who, its own number and narrowing, and its dates (a quota's block); one line per old rule and person.
    found = {}
    for line, span, n in rows:
        line = dict(line)
        if field and (order or n != amount):
            line['amount'] = n
        found.setdefault(_key(line), (line, []))[1].append(span)
    if kind == 'allocation_overtime':
        found = _offers(found)
    spans = [(line, _union(blocks) if kind in DAY_LOCAL else blocks) for line, blocks in found.values()]
    plain = quota and rule['window_alignment'] == 'rolling' and not rule['window_days']
    repeat = _repeat_lines([b for _, b in spans], ctx['week_start']) if plain and not payload['definition']['exception_allowed'] else None
    if repeat:
        rule.update(repeat['rule'])
        spans = [(line, runs) for (line, _), runs in zip(spans, repeat['runs'])]
    elif quota and not plain:
        spans = [(line, _joined_blocks(blocks, rule, ctx['week_start'])) for line, blocks in spans]
    lines = [{**line, **({'starts_on': s} if s != first else {}), **({'ends_on': e} if e != last else {})}
             for line, blocks in spans for s, e in blocks]
    order_key = (lambda r: (-r.get('amount', 0), _key(r))) if order else (
        lambda r: _key(r.get('person_id') or '', r.get('programme_id') or '', r.get('starts_on') or '', r))
    payload['definition']['per_person'] = sorted(lines, key=order_key)
    payload['name'] = _common_name(members, ctx, payload['definition'])[:160]
    return payload


def _joined_blocks(spans, rule, week_start):
    """Back-to-back spans of a repeating quota joined where the joined span repeats into exactly the same blocks."""
    def blocks(s, e):
        return list(window_blocks(_day(s), _day(e), rule['window_alignment'], rule['window_days'], week_start))
    out = []
    for s, e in sorted(spans):
        if out and out[-1][1] == s and blocks(out[-1][0], e) == blocks(*out[-1]) + blocks(s, e):
            out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def _offers(found):
    """Overtime offers are a yes per session, so one person's lines for the same dates join their days, then their duty
    types (Day shift on Thursday and on Friday: one line, Thursday and Friday)."""
    for field in ('weekdays', 'assignment_type_ids'):
        joined = {}
        for line, blocks in found.values():
            if line.get(field) is None:
                joined[_key(line, blocks)] = (line, blocks)
                continue
            key = _key({k: v for k, v in line.items() if k != field}, sorted(blocks, key=str))
            if key in joined:
                joined[key][0][field] = sorted(set(joined[key][0][field]) | set(line[field]))
            else:
                joined[key] = ({**line}, blocks)
        found = joined
    return found


def _union(spans):
    """Date spans (end None: open) joined where they meet or overlap."""
    out = []
    for s, e in sorted(spans, key=lambda x: x[0]):
        if out and (out[-1][1] is None or s <= out[-1][1]):
            out[-1][1] = None if out[-1][1] is None or e is None else max(out[-1][1], e)
        else:
            out.append([s, e])
    return [tuple(x) for x in out]


ALIGNMENTS = ('calendar_week', 'calendar_month', 'fixed')


def _repeat(blocks, alignment, days, week_start):
    """Whether back-to-back blocks are exactly one repeating quota's blocks over their whole span."""
    want = [(_day(s), _day(e)) for s, e in sorted(blocks)]
    if any(a[1] != b[0] for a, b in zip(want, want[1:])) or alignment == 'fixed' and (want[-1][1] - want[0][0]).days < days:
        return False
    return list(window_blocks(want[0][0], want[-1][1], alignment, days, week_start)) == want


def _runs(blocks):
    """Blocks split where they stop being back to back."""
    out = []
    for s, e in sorted(blocks):
        if out and out[-1][-1][1] == s:
            out[-1].append((s, e))
        else:
            out.append([(s, e)])
    return out


def _repeat_lines(block_lists, week_start):
    """One repeating window (each week, month or N days) that turns every list of blocks into whole runs, when some
    run has two or more blocks: {rule: window settings, runs: per list, the (start, end) of each run}."""
    runs = [_runs(blocks) for blocks in block_lists]
    if not any(len(run) > 1 for rs in runs for run in rs):
        return None
    lengths = {(_day(e) - _day(s)).days for rs in runs for run in rs for s, e in run}
    for alignment in ALIGNMENTS:
        days = max(lengths) if alignment == 'fixed' else None
        if all(_repeat(run, alignment, days, week_start) for rs in runs for run in rs):
            return {'rule': {'window_alignment': alignment, 'window_days': days},
                    'runs': [[(run[0][0], run[-1][1]) for run in rs] for rs in runs]}
    return None


# Proof -----------------------------------------------------------------------------------------------------------------

def _flat(lines):
    """Groups that must all hold inside lines that must all hold are those lines (and a one-line group is its line)."""
    out = []
    for line in lines:
        if 'conditions' in line and line.get('count') != 'each' and (line.get('match', 'all') == 'all' or len(line['conditions']) == 1):
            out += _flat(line['conditions'])
        else:
            out.append(line)
    return out


def _canonical_scope(scope):
    """Who and what a rule copy checks, as a sorted set of requirement lines plus its days and hours."""
    lines = []
    for line in _flat(_requirements(scope)):
        if 'conditions' in line:
            lines.append(json.dumps(line, sort_keys=True))
        elif (line.get('value') is not None and line.get('op', 'any') not in {'any', 'all', 'none'}
              or absolute_values(line) is None):
            lines.append(json.dumps(line, sort_keys=True))
        else:
            selected = absolute_values(line)
            values = sorted({v.lower() for v in selected} if line['field'] == 'tags' else set(selected))
            lines.append(json.dumps([line['field'], line.get('op', 'any'), values]
                                    + ([sorted(set(line['entry_names']))] if line.get('entry_names') else [])
                                    + (['each'] if line.get('count') == 'each' else [])))
    # Each-node paths and multiplicity identify independent counting obligations.
    requirements = lines if has_each(scope.get('conditions')) else sorted(set(lines))
    return requirements, sorted(scope.get('weekdays', range(7))), scope.get('start_time', '00:00'), scope.get('end_time', '24:00')


def expanded(policies, id_map=None, week_start=0):
    """Every rule copy the checks read, one atom per person (and clinic, for restrictions), with the dates it holds and
    rule ids mapped through `id_map` (old id -> group id). Two sets of rules check the same things when these are equal."""
    return expanded_copies(active_rules([{**p, 'definition': normal(p['definition'])} for p in policies]), id_map, week_start)


def _who_wins(copies, line, id_map):
    """An internal line (a person's own line, a more specific catalogue line) as what it reads: the people and dates of
    the lines that replace this one, so a rule's own lines and a catalogue rule's lines compare equal."""
    values = absolute_values(line) or ()
    if line['field'] == OWN_LINE:
        found = {(c['template_line']['person_id'], str(c.get('starts_on')), str(c.get('ends_on'))) for c in copies
                 if c['id'] in values and (c.get('template_line') or {}).get('person_id')}
        return ['own', sorted(found)]
    index = source_index(copies)[1]
    from .conditions import outranking_source
    sources = [outranking_source(value) for value in values]
    guarded = sorted({json.dumps(_level_meaning(copies, guard), sort_keys=True)
                      for _, guard in sources if guard})
    winners = [index[key] for key, guard in sources if guard is None and key in index]
    people = [_listed_people(c['definition']['scope']) for c in winners]
    if winners and all(c['whole_person'] and p is not None and len(p) == 1 for c, p in zip(winners, people)):
        ordinary = ['own', sorted({(next(iter(p)), str(c.get('starts_on')), str(c.get('ends_on'))) for p, c in zip(people, winners)})]
    else:
        ordinary = ['outranked', sorted({json.dumps([_canonical_scope(_person_scope(c['definition']['scope'])),
            str(c.get('starts_on')), str(c.get('ends_on'))]) for c in winners})]
    return ['guarded', ordinary, guarded] if guarded else ordinary


def _level_meaning(copies, value):
    """A resolved-level selector by its meaning, independent of policy/line IDs and source ordering."""
    key, number, level, window = level_selector(value)
    index = source_index(copies)[1]
    mine = index[key]
    setting = mine['inherit'][number]
    def source(c):
        return [_canonical_scope(_person_scope(c['definition']['scope'])), c.get('starts_on'), c.get('ends_on'), c['tier']]
    candidates = sorted({json.dumps([*source(index[k]), index[k]['numbers'][number].get('level', 'mandatory')], sort_keys=True)
                         for k in setting['sources']})
    return [source(mine), number, level, setting['fallback'], list(window or setting.get('block') or ()), candidates]


def _listed_people(scope):
    from .validation import named_people
    return named_people(scope)


def _person_scope(scope):
    """The people side of a scope (sessions, days and hours left out)."""
    from .conditions import SESSION_FIELDS
    keep = {k: v for k, v in scope.items() if k not in SESSION_KEYS + ('weekdays', 'start_time', 'end_time', 'conditions')}
    tree = scope.get('conditions')
    if tree:
        lines = [c for c in _flat(tree['conditions'] if tree.get('count') != 'each' and tree.get('match', 'all') == 'all' else [tree])
                 if 'conditions' in c or c['field'] not in SESSION_FIELDS | {OUTRANKED, OWN_LINE, INHERITED_LEVEL}]
        if lines:
            keep['conditions'] = {'match': 'all', 'conditions': lines}
    return keep


def _canonical_copy(c, copies, id_map):
    """A copy as it reads: internal lines resolved, defaults that change nothing left out (a mandatory rule has no
    strength; an empty setting is no setting)."""
    d = c['definition']
    scope = dict(d['scope'])
    tree = scope.get('conditions')
    if tree:
        lines = []
        for line in _flat(tree['conditions'] if tree.get('count') != 'each' and tree.get('match', 'all') == 'all' else [tree]):
            if 'conditions' in line or absolute_values(line) is None:
                lines.append(line)
            elif line['field'] in (OWN_LINE, OUTRANKED):
                lines.append({'field': '~replaced_by', 'op': 'none', 'values': [json.dumps(_who_wins(copies, line, id_map))]})
            elif line['field'] == OVERRIDDEN:
                lines.append({**{key: value for key, value in line.items() if key != 'value'},
                              'values': sorted(id_map.get(v, v) for v in absolute_values(line))})
            elif line['field'] == INHERITED_LEVEL:
                lines.append({'field': '~inherited_level', 'op': line.get('op', 'any'),
                              'values': sorted(json.dumps(_level_meaning(copies, value), sort_keys=True)
                                               for value in absolute_values(line))})
            else:
                lines.append(line)
        scope['conditions'] = {'match': 'all', 'conditions': lines} if lines else None
    rule = d['rule']
    return {**c, 'definition': {**d, 'scope': scope, 'rule': rule, 'strength': None if d['severity'] == 'mandatory' else d.get('strength')}}


def expanded_copies(copies, id_map=None, week_start=0, select=None):
    """`expanded` over rule copies as `active_rules` gives them; `select` keeps some copies (the rest still say who
    replaces whom)."""
    from .quotas import applicable_policies
    id_map = id_map or {}
    copies = [_canonical_copy(c, copies, id_map) for c in copies if select is None or select(c)]
    quotas = [c for c in copies if c['definition']['rule']['kind'] == 'block_quota']
    if quotas:
        # Repeating blocks read as the blocks they are.
        lo = min(c['definition']['rule']['starts_on'] for c in quotas)
        hi = max(c['definition']['rule']['ends_on'] for c in quotas)
        workspace = {'department_id': quotas[0].get('department_id'), 'starts_on': lo, 'ends_on': hi}
        quotas = applicable_policies({'workspace': workspace, 'organisation': {'week_start': week_start}, 'policies': quotas})
    spans = {}
    for c in [c for c in copies if c['definition']['rule']['kind'] != 'block_quota'] + quotas:
        d = c['definition']
        rule = {k: v for k, v in d['rule'].items() if v is not None and k != 'required_policy_ids'}
        # A supervision limit is checked supervisor by supervisor, so its people read one by one too.
        per_person, offer = rule['kind'] in PER_PERSON_KINDS | {'supervision_limit', 'spread_evenly'}, rule['kind'] == 'allocation_overtime'
        lines, days, start, end = _canonical_scope(d['scope'])
        split, rest = [], []
        for text in lines:
            item = json.loads(text)
            if (isinstance(item, list) and len(item) == 3 and item[1] == 'any' and (per_person and item[0] == 'person_ids'
                                                                 or rule['kind'] == 'restriction' and item[0] == 'service_ids'
                                                                 or offer and item[0] == 'assignment_type_ids')):
                split.append(item)
            else:
                rest.append(text)
        atoms = [[]]
        for field, _, values in split:
            atoms = [a + [json.dumps([field, 'any', [v]])] for a in atoms for v in values]
        if rule['kind'] == 'block_quota':
            when = (rule['starts_on'], rule['ends_on'])  # a block is one check, known by its dates
        else:
            when = (str(c.get('starts_on') or '0000-01-01'), str(c.get('ends_on') or '9999-12-31'))
        for atom in atoms:
            for day in ([[x] for x in days] if offer else [days]):  # an overtime offer is a yes per session: day by day
                head = _key(id_map.get(c['id'], c['id']), d['severity'], d.get('strength'), d.get('exception_allowed'), d.get('workspace_id'),
                            rule, sorted(rest + atom), day, start, end)
                spans.setdefault(head, []).append(when)
    out = {}
    for head, items in spans.items():
        if json.loads(head)[5]['kind'] == 'block_quota':
            out[head] = sorted(set(items))
            continue
        merged = []
        for a, b in sorted(items):  # the same check twice, or back to back, is one check
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        out[head] = [tuple(m) for m in merged]
    return out


def mapped_citation_finding_key(f, id_map, *, canonicalize_goals=True):
    """Grouping equivalence must preserve each explicitly mapped source rule, not just equal outcomes."""
    from .fairness import parse_quota_goal_id

    def mapped(value, key=''):
        if isinstance(value, dict):
            return {k: mapped(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [mapped(v, key.removesuffix('s')) for v in value]
        if key in {'goal_id', 'source_goal_id'} and isinstance(value, str):
            if not canonicalize_goals:
                return value
            if value in id_map:
                return id_map[value]
            parsed = parse_quota_goal_id(value)
            if parsed is not None:
                source, _digest = parsed
                if source in id_map or source in id_map.values():
                    return id_map.get(source, source)
            source, separator, suffix = value.partition(':')
            if separator and source in id_map:
                return f'{id_map[source]}:{suffix}'
        return id_map.get(value, value) if key in {'goal_id', 'policy_id', 'rule_id'} else value

    # Copy provenance is new after grouping; model atoms above compare its actual level and scope.
    detail = {k: v for k, v in (f.get('detail') or {}).items() if 'name' not in k and k not in {'line', 'slot', 'rule_severity', 'rule_strength', 'rule_tier'}}
    if canonicalize_goals and f.get('code') == 'BALANCE_GOAL' and isinstance(detail.get('goal_id'), str):
        goal_id = detail['goal_id']
        parsed = parse_quota_goal_id(goal_id)
        source = parsed[0] if parsed is not None else goal_id
        if source == f.get('rule_id'):
            detail['goal_id'] = id_map.get(f.get('rule_id'), f.get('rule_id'))
    return (f['code'], id_map.get(f.get('rule_id'), f.get('rule_id')), f.get('shift_id'), f.get('person_id'), f.get('severity'),
            str(f.get('start')), str(f.get('end')), json.dumps(mapped(detail), sort_keys=True, default=str))


def quota_key(r, id_map):
    from .conditions import counting_key
    return (id_map.get(r['policy_id'], r['policy_id']), r['person_id'], r['starts_on'], r['ends_on'], r['metric'], r['comparison'],
            r['amount'], r['actual'], r['status'], r['severity'], counting_key(r))


def same_checks(before, after, id_map):
    """Differences between two validator results (findings and quota rows) after mapping old rule ids to group ids.
    A finding a rule raised twice for the same session (two clinics of one restriction) counts once."""
    from .fairness import quota_goal_id, quota_target_key
    from .validation import finding_observations

    def quota_goals(result):
        return {quota_target_key(row): row for row in (result.get('quota_report') or {}).get('rows', [])
                if row['comparison'] == 'target' and row['status'] != 'unresolved'}

    # Quota goal hashes include the policy id; rebuild only aliases witnessed by both reports.
    finding_ids = dict(id_map)
    after_goals = quota_goals(after)
    for key, row in quota_goals(before).items():
        if row['policy_id'] in id_map:
            policy_id = id_map[row['policy_id']]
            mapped_key = quota_target_key({**row, 'policy_id': policy_id})
            if mapped_key in after_goals:
                finding_ids[quota_goal_id(row['policy_id'], key)] = quota_goal_id(policy_id, mapped_key)

    def quota(result):
        return sorted(quota_key(r, id_map) for r in (result.get('quota_report') or {}).get('rows', []))
    quota_equal = quota(before) == quota(after)
    # Keep a changed quota key visible even when its BALANCE_GOAL source id is mapped.

    def findings(result):
        return {mapped_citation_finding_key(f, finding_ids, canonicalize_goals=quota_equal)
                for f in finding_observations(result)}

    a, b = findings(before), findings(after)
    return {'findings_only_before': sorted(a - b, key=str), 'findings_only_after': sorted(b - a, key=str),
            'quota_equal': quota_equal, 'score_before': (before.get('score') or {}).get('score'),
            'score_after': (after.get('score') or {}).get('score')}


def canonical_model(data):
    """The validated structural CP-SAT model, without searching for a schedule: build status, sorted constraints,
    sorted variable identities and sorted objective. Equal variable protos retain distinct occurrence identities;
    their reordering can fail closed rather than erase which helper a constraint uses. A ready model can be infeasible."""
    from . import cp_sat
    from .solver_provenance import policy_id
    models = []
    result = cp_sat.generate(data, 5, feasibility_only=True, _on_model=models.append)
    if result['status'] != 'model_ready':
        return result['status'], [], [], []
    [model] = models
    def structural_constraint(constraint):
        ident = policy_id(constraint.name)
        if ident is None or constraint.name == 'policy:' + ident:
            return str(constraint)
        copied = type(constraint)()
        copied.copy_from(constraint)
        copied.name = 'policy:' + ident  # Diagnostic occurrence metadata does not change a model inequality.
        return str(copied)

    constraints = [structural_constraint(c) for c in model.proto.constraints]
    raw = [str(v) for v in model.proto.variables]

    def name(index, labels, self_index=None):
        positive = index if index >= 0 else -index - 1
        text = 'self' if positive == self_index else labels[positive].strip()
        return f"<{text}{'' if index >= 0 else '!'}>"

    references = re.compile(r'(vars|literals|enforcement_literal|exprs|target|index): (-?\d+)')

    def label(text, labels, self_index=None):
        return references.sub(lambda mt: f"{mt.group(1)}: {name(int(mt.group(2)), labels, self_index)}", text)

    # Grouping can reorder identical-named presence helpers. Their defining max expressions distinguish them
    # without changing compiler order. Still keep separate occurrences, and compare every full constraint below.
    definitions = {}
    for constraint in model.proto.constraints:
        if constraint.has_lin_max() and len(constraint.lin_max.target.vars) == 1:
            index = constraint.lin_max.target.vars[0]
            definitions.setdefault(index if index >= 0 else -index - 1, []).append(label(str(constraint.lin_max), raw))
    def identities():
        variables, occurrences = [], Counter()
        for index, variable in enumerate(raw):
            text = json.dumps([variable, sorted(definitions.get(index, []))])
            variables.append(json.dumps([text, occurrences[text]]))
            occurrences[text] += 1
        return variables

    variables = identities()
    # Advisory cover, rest and transition rules can reorder identical leaves. Their defining inequalities
    # identify them using the already-labelled presence helpers. Additional attachments stay occurrence-sensitive.
    uses = defaultdict(set)
    for ci, constraint in enumerate(constraints):
        for match in references.finditer(constraint):
            index = int(match.group(2))
            uses[index if index >= 0 else -index - 1].add(ci)
    leaves = {i for i, variable in enumerate(model.proto.variables)
              if variable.name in {'preference_deficit', 'rest_preference', 'transition_preference'}}
    objective_variables = {i if i >= 0 else -i - 1 for i in model.proto.objective.vars}
    for index in leaves:
        if index in objective_variables:
            continue
        if len(uses[index]) == 1:
            [ci] = uses[index]
            constraint = model.proto.constraints[ci]
            if constraint.has_linear() and set(constraint.linear.vars) & leaves == {index}:
                definitions[index] = [label(constraints[ci], variables, self_index=index)]
        elif len(uses[index]) == 2 and list(model.proto.variables[index].domain) == [0, 1]:
            pair = [model.proto.constraints[ci] for ci in uses[index]]
            plain = next((c for c in pair if c.has_linear() and not c.enforcement_literal), None)
            reverse = next((c for c in pair if c.has_linear() and list(c.enforcement_literal) == [index]), None)
            if plain is None or reverse is None:
                continue
            terms = dict(zip(plain.linear.vars, plain.linear.coeffs))
            coefficient = terms.pop(index, None)
            # Exact violation truth: expression <= bound + flag; flag => expression >= bound + 1.
            if (coefficient == -1 and set(terms).isdisjoint(leaves)
                    and len(plain.linear.domain) == 2 and plain.linear.domain[0] == -(2 ** 63)
                    and list(reverse.linear.domain) == [plain.linear.domain[1] + 1, 2 ** 63 - 1]
                    and terms == dict(zip(reverse.linear.vars, reverse.linear.coeffs))):
                definitions[index] = sorted(label(constraints[ci], variables, self_index=index) for ci in uses[index])
    variables = identities()
    objective = model.proto.objective
    terms = sorted((name(v, variables), c) for v, c in zip(objective.vars, objective.coeffs))
    return result['status'], sorted(label(c, variables) for c in constraints), sorted(variables), [terms, objective.offset, objective.scaling_factor]


def same_model(before, after, id_map):
    """Whether two validated models are the same once old rule ids read as the rules that replace them."""
    # A preflight rejection or invalid model supplies no structural proof, even when both statuses match.
    if before[0] != 'model_ready' or after[0] != 'model_ready':
        return False
    if id_map:
        pattern = re.compile('|'.join(map(re.escape, sorted(id_map, key=len, reverse=True))))
        rename = lambda text: pattern.sub(lambda mt: id_map[mt.group(0)], text)  # noqa: E731
        before = (before[0], sorted(rename(c) for c in before[1]), sorted(rename(v) for v in before[2]),
                  [sorted((rename(n), c) for n, c in before[3][0]), *before[3][1:]])
    # Rule-id remapping must preserve a one-to-one identity for every referenced variable.
    if any(len(variables) != len(set(variables)) for variables in (before[2], after[2])):
        return False
    return before == after



# Changes that are not identical: proposed on their own, with what changes --

SESSION_KEYS = ('service_id', 'service_ids', 'location_id', 'assignment_type_id')


def _weekend(days):
    """(first day, length) when the days are one run of 1-3 days in a row (Friday and Saturday: (4, 2)), else None."""
    days = sorted(set(days))
    if not 0 < len(days) <= 3:
        return None
    for first in days:
        if sorted((first + i) % 7 for i in range(len(days))) == days:
            return first, len(days)
    return None


def goal_conversions(policies, services=None):
    """Allocation preferences that balance, as the Balance goals that replace their balancing. Balance goals are the one
    fairness mechanism: they show on Fairness, score under fairness and can count
    history. This changes what auto-schedule aims for, so each is its own proposal with the difference in words (`was`,
    `now`). A preference that only balances is replaced (activating the goal retires it; retiring the goal brings it
    back); one that also sets a priority stays (`keeps`), and its balancing is set to none by whoever reviews it.
    `services` maps clinic ids to {'mode': 'physical' | 'virtual'}."""
    services = services or {}
    waiting = {i for p in policies if p['status'] == 'proposed' for i in p['definition'].get('merged_ids') or []}
    out = []
    for p in policies:
        if p['status'] != 'active' or not p.get('active', True) or p['id'] in waiting:
            continue
        try:
            d = normal(p['definition'])
        except ValueError:
            continue
        rule, scope = d['rule'], d['scope']
        if (rule['kind'] != 'allocation_preference' or rule['balance_by'] == 'none' or d['per_person'] or d['overrides']
                or rule['supervisor_relationship'] != 'any'):
            continue
        if (scope['start_time'], scope['end_time']) != ('00:00', '24:00') or scope.get('location_id'):
            continue  # a goal counts whole duties anywhere: hours or a place would change what it counts
        # Clinics and duty types the preference picks, flat or as "is any of" lines of its conditions (joined by and).
        tree, kept = scope.get('conditions'), []
        clinics, types = _services(scope), [scope['assignment_type_id']] if scope.get('assignment_type_id') else []
        for line in (tree or {}).get('conditions', []):
            if ('conditions' not in line and line['field'] in ('service_ids', 'assignment_type_ids')
                    and line.get('op', 'any') in {'any', 'is'} and tree['match'] == 'all'
                    and absolute_values(line) is not None):
                (clinics if line['field'] == 'service_ids' else types).extend(
                    v for v in absolute_values(line) if v not in clinics + types)
            elif 'conditions' not in line and line['field'] == 'location_ids' or 'conditions' in line and has_session_lines(line):
                break
            else:
                kept.append(line)
        else:
            days = scope['weekdays']
            keeps = bool(rule['priority'] or rule['session_priority'] or rule['overtime_priority'])
            goal = {'kind': 'balance_goal', 'metric': 'duties', 'aim': 'equal_share', 'share_by': 'available_days',
                    **({'period': 'rolling', 'period_days': rule['balance_window_days']} if rule['balance_window'] == 'rolling' else {'period': 'rota'})}
            was, now = [], []
            where = list(clinics)  # who: the people who may work at the preference's clinics
            if rule['balance_by'] == 'service_mode':
                clinics = [c for c in clinics if (services.get(c) or {}).get('mode') == rule['balance_target_mode']]
                if not clinics:
                    continue
                was.append(f"each person's duties aimed at {rule['balance_target_share']:.0%} {rule['balance_target_mode']}")
                now.append(f"{rule['balance_target_mode']} duties shared fairly between people")
            elif rule['balance_by'] in ('supervisor', 'supervisor_group'):
                goal.update(metric='pair_exposure', aim='min_per_pair', amount=1, pairing='supervised',
                            **({'partners': {'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': rule['supervisor_ids']}]}}
                               if rule['supervisor_ids'] else {}))
                if not rule['supervisor_ids']:
                    continue  # a goal names its partners
                was.append('fewer repeated duties with the same supervisor' + (' of the named group' if rule['balance_by'] == 'supervisor_group' else ''))
                now.append('each person is supervised at least once by each named supervisor')
            else:
                was.append('fewer repeated duties per person (a rising cost for the 2nd, 3rd and 4th)')
                now.append('each person’s duties aim for their fair share of the days they are available')
            if len(days) < 7:
                weekend = _weekend(days)
                if not weekend or goal['metric'] != 'duties':
                    continue
                goal.update(metric='weekends', weekend_start=weekend[0], weekend_days=weekend[1])
                now.append('counts weekends worked (a weekend with any duty counts once)')
            if types:
                goal['assignment_type_ids'] = types
                if clinics and rule['balance_by'] != 'service_mode':
                    now.append('counts that duty type at every clinic (a goal counts duty types or clinics, not both)')
            elif clinics:
                goal['service_ids'] = clinics
            if rule['balance_window'] == 'week':
                was.append('balanced within each week')
                now.append('balanced over the whole rota')
            was.append(f'weight {rule["balance_weight"]:g} among preferences; not scored and no findings')
            now.append('scored under fairness and shown on Fairness; a warning when someone is outside the band')
            strength = 'medium' if rule['balance_weight'] >= 1 else 'low'
            people = {k: v for k, v in scope.items() if k not in SESSION_KEYS + ('weekdays', 'start_time', 'end_time', 'conditions')}
            lines = list(kept)
            if where:  # a goal shares among the people it names: those who can work there, as the preference only met them
                lines.append({'field': 'eligible_service_ids', 'op': 'any', 'values': where})
                now.append('shared among the people who can work at those clinics')
            people['conditions'] = {'match': 'all', 'conditions': lines} if lines else None
            if keeps:
                now.append('the preference stays for its priority: set its balancing to none once the goal is active')
            payload = {'name': p['name'], 'owner': p['owner'], 'source': 'Converted from an allocation balance', 'active': True,
                       'starts_on': str(p['starts_on']), 'ends_on': str(p['ends_on']) if p['ends_on'] else None,
                       'definition': {'scope': people, 'severity': 'advisory', 'strength': strength, 'rule': goal,
                                      'merged_ids': [] if keeps else [p['id']]}}
            try:
                PolicyInput.model_validate(payload)
            except ValueError:
                continue
            out.append({'shape': 'goal', 'rule_ids': [p['id']], 'kind': 'balance_goal', 'name': p['name'], 'lines': 0, 'people': 0,
                        'amounts': None, 'default_amount': None, 'starts_on': payload['starts_on'], 'ends_on': payload['ends_on'],
                        'switched_off': False, 'keeps': keeps, 'payload': payload, 'was': _sentence(was), 'now': _sentence(now)})
    return out


def _covers(rule, old, week_start):
    """Whether `rule` checks everything `old` checked (every check, person, session and date of it)."""
    mine = expanded([rule], None, week_start)
    theirs = expanded([{**old, 'status': 'active', 'active': True}], {old['id']: rule['id']}, week_start)
    for head, spans in theirs.items():
        have = mine.get(head)
        if have is None:
            return False
        if json.loads(head)[5]['kind'] == 'block_quota':
            if not set(spans) <= set(have):
                return False
        elif not all(any(a <= s and e <= b for a, b in have) for s, e in spans):
            return False
    return True


def overtime_switch_on(policies, week_start=0):
    """Overtime offers switched off because limits they need were retired, pointed at the limits in force that check
    at least as much (the rule that replaced each one: grouped, rebuilt or a new version). Overtime is offered again,
    bounded by those limits: a behaviour change, proposed as a new version of each offer rule.
    Each: {shape: 'overtime', rule_ids: [offer id], name, payload, mapped: {old: new}, missing: [old ids]}."""
    active = [p for p in policies if p['status'] == 'active' and p.get('active', True)]
    caps = [p for p in active if is_cap(p)]
    in_force = {p['id'] for p in caps} | {i for p in caps for i in p['definition'].get('merged_ids') or []}
    by_id = {p['id']: p for p in policies}
    waiting = {i for p in policies if p['status'] == 'proposed' for i in p['definition'].get('merged_ids') or []}
    out = []
    for p in active:
        rule = p['definition']['rule']
        if rule['kind'] != 'allocation_overtime' or p['id'] in waiting:
            continue
        off = [i for i in rule.get('required_policy_ids') or [] if i not in in_force]
        if not off:
            continue
        mapped, missing = {}, []
        for i in off:
            old = by_id.get(i)
            found = next((c for c in caps if old and c['definition']['rule']['kind'] == old['definition']['rule']['kind']
                          and _covers(c, old, week_start)), None) if old else None
            if found:
                mapped[i] = found['id']
            else:
                missing.append(i)
        keep = [i for i in rule['required_policy_ids'] if i in in_force]
        required = list(dict.fromkeys(keep + list(mapped.values()) + missing))
        # The switched-off rule is replaced (merged_ids): retiring the new one brings it back, switched off as it was.
        definition = {**normal(p['definition']), 'rule': {**rule, 'required_policy_ids': required}, 'replaces_id': None, 'merged_ids': [p['id']]}
        payload = {'name': p['name'], 'owner': p['owner'], 'source': 'Overtime switched back on with the limits in force', 'active': True,
                   'starts_on': str(p['starts_on']), 'ends_on': str(p['ends_on']) if p['ends_on'] else None, 'definition': definition}
        names = sorted({by_id[i]['name'] for i in required if i in by_id and i not in missing})
        out.append({'shape': 'overtime', 'rule_ids': [p['id']], 'kind': 'allocation_overtime', 'name': p['name'],
                    'lines': len(definition.get('per_person') or []),
                    'people': len({r['person_id'] for r in definition.get('per_person') or [] if r.get('person_id')}),
                    'amounts': None, 'default_amount': None, 'starts_on': payload['starts_on'], 'ends_on': payload['ends_on'],
                    'switched_off': bool(missing), 'payload': payload, 'mapped': mapped, 'missing': missing, 'limits': names,
                    'was': f'Switched off: {len(off)} of the limits it needs are retired',
                    'now': ('Offered again, only within ' + ', '.join(names)) if not missing
                    else f'Still off: {len(missing)} limits it needs have no rule in force that checks as much'})
    return out


def _sentence(parts):
    text = '; '.join(parts)
    return text[:1].upper() + text[1:]


# Phase 3 ----------------------------------------------------------------
# What phases 1 and 2 left: rules of one idea whose lines need their own settings (supervision limits, companion covers,
# an allocation order's supervised lines), a limit that is one more line of a weekly ceiling, and rules that have ended.
# Every proposal is compared with the rules it replaces (`expanded`): identical ones are groups, the rest are changes
# with what differs, proposed only when asked for.

# Scope fields a line can say; a rule that uses any other stays as it is.
LINE_SCOPE = {'person_id', 'person_ids', 'programme_id', 'category_id', 'category_ids', 'service_id', 'service_ids',
              'assignment_type_id', 'weekdays', 'start_time', 'end_time'}


def _plain_scope(p):
    d = p['definition']
    return not d['overrides'] and all(v in (None, [], '') for k, v in d['scope'].items() if k not in LINE_SCOPE)


def _last_day(value, back=1):
    """A date as people read it ("18 Sep 2026"); an end date (not included) reads as its last day."""
    from datetime import timedelta
    day = _day(value) - timedelta(days=back)
    return f"{day.day} {day:%b %Y}"


def ended(p, today, rotas):
    """A rule whose dates are wholly past (its end date, not included, is today or earlier) and that no rota covers
    (`rotas`: (starts_on, ends_on) of every rota that is not discarded)."""
    end = str(p['ends_on']) if p.get('ends_on') else None
    if not end or end > str(today):
        return False
    start = str(p['starts_on'])
    return not any(str(s) < end and start < str(e) for s, e in rotas)


def duplicates(policies, held=(), week_start=0):
    """Active rules that are exact copies of another active rule (the same kind, definition and dates; names may
    differ): each copy beyond the first is retired by an approver, as an ended rule is (Restore in History brings it
    back). The rule kept is a grouped one when there is one (retiring a grouped rule would bring back its old rules),
    else the oldest. Proof: the copies and the kept rule check exactly the same people, sessions and dates
    (`expanded`, with the copies' ids read as the kept rule's); what changes is that a break was reported, and an
    advisory one counted in the score and by auto-schedule, once per copy. `held`: rules a person exception names
    (never retired here)."""
    waiting = {i for p in policies if p['status'] == 'proposed' for i in p['definition'].get('merged_ids') or []}
    sets = {}
    for p in policies:
        if p['status'] != 'active' or not p.get('active', True) or p['id'] in waiting:
            continue
        try:
            d = normal(p['definition'])
        except ValueError:
            continue
        sets.setdefault(_key(p['kind'], {**d, 'merged_ids': [], 'replaces_id': None}, str(p['starts_on']), str(p['ends_on'])), []).append(p)
    out = []
    for members in sets.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda p: (not p['definition'].get('merged_ids'), str(p.get('created_at') or ''), p['id']))
        keep = members[0]
        extras = [p for p in members[1:] if not p['definition'].get('merged_ids') and p['id'] not in held]
        if not extras or expanded(members, {p['id']: keep['id'] for p in members}, week_start) != expanded([keep], None, week_start):
            continue
        advisory = keep['definition']['severity'] != 'mandatory'
        for p in extras:
            out.append({'shape': 'duplicate', 'retire': True, 'rule_ids': [p['id']], 'kept_id': keep['id'], 'kept_name': keep['name'],
                        'kind': p['kind'], 'name': p['name'], 'lines': 0, 'people': 0, 'amounts': None, 'default_amount': None,
                        'starts_on': str(p['starts_on']), 'ends_on': str(p['ends_on']) if p['ends_on'] else None, 'switched_off': False,
                        'was': f"The same rule as {keep['name']} (the same people, sessions, numbers and dates): a break is reported twice"
                               + (', and counted twice in the score and by auto-schedule' if advisory else ''),
                        'now': f"Retired: {keep['name']} checks exactly the same (Restore in History brings it back)"})
    return out


def _with_settings(members, ctx, keys, field, rule, always=False, number=None):
    """One rule for its lines only: every member's who, sessions, dates and number as lines, and the member's
    settings among `keys` on its lines (those that are set, or differ from `rule`, the joined rule's settings).
    `number(member, n)` gives a member's line number; `always`: every line states its number."""
    vary = _varying(members)
    found = {}
    for m in members:
        rows = _rows(m, vary, field)
        if rows is None:
            return None
        mine = m['definition']['rule']
        own = {k: mine[k] for k in keys if mine.get(k) not in (None, [], 0) or mine.get(k) != rule.get(k)}
        for line, span, n in rows:
            line = dict(line)
            settings = {**own, **line.pop('settings', {})}
            if settings:
                line['settings'] = settings
            n = number(m, n) if number else n
            if field and n is not None and (always or n != rule.get(field)):
                line['amount'] = n
            found.setdefault(_key(line), (line, []))[1].append(span)
    starts = min(str(m['starts_on']) for m in members)
    ends = None if any(not m['ends_on'] for m in members) else max(str(m['ends_on']) for m in members)
    lines = [{**line, **({'starts_on': s} if s != starts else {}), **({'ends_on': e} if e != ends else {})}
             for line, spans in found.values() for s, e in _union(spans)]
    scope = {k: v for k, v in members[0]['definition']['scope'].items() if k not in PEOPLE}
    neutral = {'programme_id': {'programme_id': None}, 'assignment_type_id': {'assignment_type_id': None}, 'weekdays': {'weekdays': list(range(7))},
               'services': {'service_id': None, 'service_ids': []}, 'times': {'start_time': '00:00', 'end_time': '24:00'},
               'ranks': {'category_id': None, 'category_ids': []}}
    for k in vary:
        scope.update(neutral[k])
    order = (lambda r: (-(r.get('amount') or 0), _key(r))) if always else (lambda r: _key(r.get('person_id') or '', r.get('starts_on') or '', r))
    payload = _payload(members, ctx, scope=scope, rule=dict(rule), listed_only=True, per_person=sorted(lines, key=order), overrides=[],
                       replaces_id=None, calendar_entry_id=None, line_number=field if field != PER_PERSON_FIELD.get(rule['kind']) else None)
    payload.update(starts_on=starts, ends_on=ends)
    return payload


def _bucketed(items, key):
    out = {}
    for p in items:
        d = p['definition']
        out.setdefault(_key(d['severity'], d['strength'], d['exception_allowed'], d['workspace_id'], key(p)), []).append(p)
    return [members for members in out.values() if len(members) > 1]


def _range(pool, ctx):
    """A minimum and a maximum of the same quota (the same people, sessions, blocks and settings) as one range:
    "between 6 and 7 session units a week". Identical: a range is checked as its minimum and its maximum."""
    def rest(p):
        d = p['definition']
        return _key({**d, 'rule': {k: v for k, v in d['rule'].items() if k not in ('comparison', 'amount')}, 'merged_ids': []},
                    str(p['starts_on']), str(p['ends_on']))
    cands = [p for p in pool if p['kind'] == 'block_quota' and p['definition']['rule']['comparison'] in ('minimum', 'maximum')
             and not any(r.get('amount') is not None for r in p['definition']['per_person'])]
    for members in _bucketed(cands, rest):
        lows = [p for p in members if p['definition']['rule']['comparison'] == 'minimum']
        highs = [p for p in members if p['definition']['rule']['comparison'] == 'maximum']
        if len(lows) != 1 or len(highs) != 1 or lows[0]['definition']['rule']['amount'] > highs[0]['definition']['rule']['amount']:
            continue
        low, high = lows[0], highs[0]
        rule = {**low['definition']['rule'], 'comparison': 'range', 'maximum_amount': high['definition']['rule']['amount']}
        shared = _shared_words([rule_stem(p['name'], ctx['people']) for p in (low, high)])
        payload = {'name': (f'{shared} range' if shared else low['name'])[:160], 'owner': low['owner'], 'source': 'Rule simplification',
                   'active': True, 'starts_on': str(low['starts_on']), 'ends_on': str(low['ends_on']) if low['ends_on'] else None,
                   'definition': {**low['definition'], 'rule': rule, 'merged_ids': []}}
        yield ([low, high], payload, [], f"Two rules: at least {low['definition']['rule']['amount']:g} and at most {high['definition']['rule']['amount']:g}",
               f"One range rule: between {low['definition']['rule']['amount']:g} and {high['definition']['rule']['amount']:g}")


def _supervision(pool, ctx):
    """Every supervision limit (who may supervise which trainee ranks, and how many) as one table: a line per
    supervisor and clinics, with its trainee ranks and capacity. Identical: each line is one of the limits."""
    keys = LINE_SETTINGS['supervision_limit']
    for members in _bucketed([p for p in pool if p['kind'] == 'supervision_limit' and _plain_scope(p)], lambda p: None):
        common = Counter(_key({k: m['definition']['rule'][k] for k in keys}) for m in members).most_common(1)[0][0]
        rule = next(m['definition']['rule'] for m in members if _key({k: m['definition']['rule'][k] for k in keys}) == common)
        payload = _with_settings(members, ctx, keys, 'maximum', rule)
        if payload:
            payload['name'] = 'Supervision limits'
            yield (members, payload, [], 'Separate supervision limits: ' + ', '.join(m['name'] for m in members),
                   'One rule: a line per supervisor and clinics, with the trainee ranks and how many')


def _companions(pool, ctx):
    """Staffing levels that apply while a named person is on duty ("when Dr X works, at least 2 seniors"): one rule, a
    line per person on duty. A line that asks for fewer of the same people at the same sessions than another line
    with the same trigger is implied by it and dropped (a change: that shortfall was reported twice)."""
    people = ctx['labels'].get('people', {})
    cands = [p for p in pool if p['kind'] == 'minimum_cover' and p['definition']['rule']['when_person_ids'] and _plain_scope(p)]
    for members in _bucketed(cands, lambda p: p['definition']['rule']['aggregation']):
        rule = {**members[0]['definition']['rule'], 'when_person_ids': []}
        payload = _with_settings(members, ctx, ['when_person_ids'], 'minimum', rule)
        if not payload:
            continue
        payload['name'] = 'Companion cover' if payload['definition']['severity'] == 'mandatory' else 'Attendance with named people'
        strongest, dropped = {}, []
        for line in payload['definition']['per_person']:
            same = _key({k: v for k, v in line.items() if k != 'amount'})
            if same in strongest:
                weaker, kept = sorted([strongest[same], line], key=lambda r: r.get('amount') or rule['minimum'])
                dropped.append((weaker, kept))
                strongest[same] = kept
            else:
                strongest[same] = line
        payload['definition']['per_person'] = [line for line in payload['definition']['per_person'] if any(line is k for k in strongest.values())]

        def who(line):
            return ', '.join(people.get(i, 'a named person') for i in line['settings']['when_person_ids'])
        was = [f"{len(members)} rules: " + '; '.join(m['name'] for m in members)]
        now = ['one rule, a line per person on duty']
        for weaker, kept in dropped:
            was.append(f"when {who(weaker)} is on duty, at least {weaker.get('amount') or rule['minimum']} (a rule of its own)")
            now.append(f"that line is dropped: at least {kept.get('amount') or rule['minimum']} of the same people at the same sessions "
                       "already requires it (a session short of it was reported by both rules; now once)")
        yield members, payload, [], _sentence(was), _sentence(now)


def _order(pool, ctx):
    """Allocation preferences that only raise the priority of some sessions (optionally those named supervisors
    lead) join the allocation order as lines. A preference that also balances exposure to supervisors keeps its
    priority as a line and its balancing becomes a Balance goal (a change)."""
    def where(p):
        return {k: v for k, v in p['definition']['scope'].items() if k not in SESSION}
    for order in [p for p in pool if p['kind'] == 'allocation_preference' and p['definition']['line_number'] == 'session_priority'
                  and p['definition']['listed_only'] and _plain_scope(p)]:
        d = order['definition']
        joiners, extra, was, now = [], [], [], []
        for p in pool:
            pd, rule = p['definition'], p['definition']['rule']
            if (p is order or p['kind'] != 'allocation_preference' or pd['per_person'] or not _plain_scope(p)
                    or (pd['severity'], pd['strength'], pd['exception_allowed'], pd['workspace_id']) != (d['severity'], d['strength'], d['exception_allowed'], d['workspace_id'])
                    or where(p) != where(order) or rule['overtime_priority'] or rule['supervisor_relationship'] != 'any'
                    or not (rule['priority'] or rule['session_priority'])):
                continue
            score = rule['priority'] + rule['session_priority']
            if rule['balance_by'] != 'none':
                goal = next(iter(goal_conversions([{**p, 'status': 'active'}], ctx['services'])), None)
                if goal is None:
                    continue
                extra.append({**goal['payload'], 'definition': {**goal['payload']['definition'], 'merged_ids': [p['id']]}})  # either one's undo brings the preference back
                goal_now = goal['now'].split('; the preference stays')[0]
                was.append(f"{p['name']}: priority {score}, and {goal['was'][:1].lower() + goal['was'][1:]}")
                now.append(f"its priority is a line of {order['name']} ({score}); its balancing is a Balance goal: "
                           + goal_now[:1].lower() + goal_now[1:])
            elif rule['priority']:
                was.append(f"{p['name']}: person priority {rule['priority']}")
                now.append(f"session priority {score} on its line (the same weight for auto-schedule; the older quick planner ranks sessions by it)")
            joiners.append(p)
        if not joiners:
            continue
        members = [order] + joiners

        def number(m, n):
            return n if m is order else m['definition']['rule']['priority'] + m['definition']['rule']['session_priority']
        payload = _with_settings(members, ctx, ['supervisor_ids'], 'session_priority', d['rule'], always=True, number=number)
        if payload:
            payload['name'] = order['name']
            yield members, payload, extra, _sentence(was or ['separate preferences']), _sentence(now or ['lines of the allocation order'])


def _ceiling(pool, ctx):
    """A weekly duty limit for some people and clinics that a weekly duty ceiling already has a line for (the same
    clinics and number) becomes one more line of that ceiling (a change: it is reported as the ceiling, and its
    weeks are calendar weeks cut at its dates)."""
    quotas = [q for q in pool if q['kind'] == 'block_quota' and q['definition']['listed_only'] and not q['definition']['exception_allowed']
              and q['definition']['rule']['metric'] == 'duties' and q['definition']['rule']['comparison'] == 'maximum'
              and q['definition']['rule']['window_alignment'] == 'calendar_week' and q['definition']['rule']['overtime'] == 'include'
              and not q['definition']['rule']['participation'] and not q['definition']['rule']['assignment_type_ids']]
    taken = set()
    for lim in [p for p in pool if p['kind'] == 'max_duties' and not p['definition']['per_person'] and _plain_scope(p) and p['ends_on']]:
        d, rule, scope = lim['definition'], lim['definition']['rule'], lim['definition']['scope']
        if (rule['window'], rule['group_by'], rule['include_overtime']) != ('week', 'all', True) or not _services(scope):
            continue
        for q in quotas:
            qd, qr = q['definition'], q['definition']['rule']
            if q['id'] in taken or (qd['severity'], qd['strength'], qd['workspace_id']) != (d['severity'], d['strength'], d['workspace_id']):
                continue
            if not (qr['starts_on'] <= str(lim['starts_on']) and str(lim['ends_on']) <= qr['ends_on']):
                continue
            if not any(set(r.get('service_ids') or []) == set(_services(scope)) and (r['amount'] if r.get('amount') is not None else qr['amount']) == rule['maximum']
                       for r in qd['per_person']):
                continue
            line = _narrowing(lim, {'programme_id', 'ranks', 'services', 'assignment_type_id', 'weekdays', 'times'})
            lines = [{**line, 'person_id': pid} for pid in _people(scope)] or [line]
            lines = [{**r, 'starts_on': str(lim['starts_on']), 'ends_on': str(lim['ends_on']),
                      **({'amount': float(rule['maximum'])} if rule['maximum'] != qr['amount'] else {})} for r in lines]
            payload = {'name': q['name'], 'owner': q['owner'], 'source': 'Rule simplification', 'active': True,
                       'starts_on': str(q['starts_on']), 'ends_on': str(q['ends_on']) if q['ends_on'] else None,
                       'definition': {**qd, 'per_person': qd['per_person'] + lines, 'merged_ids': []}}
            taken.add(q['id'])
            yield ([q, lim], payload, [],
                   f"{lim['name']}: at most {rule['maximum']} a week at those clinics, a rule of its own (reported as maximum duties); "
                   f"{q['name']} has a line with the same clinics and number",
                   f"a line of {q['name']} for the same people, clinics and dates, reported as that ceiling; "
                   f"its weeks are calendar weeks cut at {lim['starts_on']} and {lim['ends_on']}")
            break


def simplify(policies, labels=None, week_start=0, held=(), today=None, rotas=(), services=None):
    """Phase 3 proposals for the active rules in `policies` (every status, as `plan`). `rotas`: (starts_on, ends_on)
    of every rota not discarded; `services`: clinic ids to {'mode': ...}. Returns (groups, changes): groups are proven
    identical (the same checks, `expanded`); changes carry `was` and `now`, and ended rules `retire: True`."""
    labels, today = labels or {}, str(today or clock_now().date())
    waiting = {i for p in policies if p['status'] == 'proposed' for i in p['definition'].get('merged_ids') or []}
    groups, changes, pool = [], [], []
    for p in policies:
        if p['status'] != 'active' or not p.get('active', True) or p['id'] in set(held) | waiting:
            continue
        if p['definition']['rule'].get('repeat'):
            continue  # Repeating obligations retain their explicit validity and counting configuration.
        if ended(p, today, rotas):
            changes.append({'shape': 'ended', 'retire': True, 'rule_ids': [p['id']], 'kind': p['kind'], 'name': p['name'], 'lines': 0,
                            'people': 0, 'amounts': None, 'default_amount': None, 'starts_on': str(p['starts_on']), 'ends_on': str(p['ends_on']),
                            'switched_off': False, 'was': f"In force {_last_day(p['starts_on'], 0)} to {_last_day(p['ends_on'])}",
                            'now': 'Retired: its dates are past and no rota covers them (Restore in History brings it back)'})
        elif _groupable(p, waiting) or _joinable(p, waiting):
            try:
                pool.append({**p, 'definition': normal(p['definition']), '_ids': [p['id']], '_shapes': []})
            except ValueError:
                continue
    ctx = {'caps': set(), 'week_start': week_start, 'labels': labels, 'services': services or {},
           'people': list(labels.get('people', {}).values()),
           'words': [n for kind in ('people', 'programmes', 'services') for n in labels.get(kind, {}).values()]}
    for shape, finder in (('range', _range), ('supervision', _supervision), ('companions', _companions), ('order', _order), ('ceiling', _ceiling)):
        for members, payload, extra, was, now in list(finder(pool, ctx)):
            ids = [m['id'] for m in members]
            if not set(ids) <= {p['id'] for p in pool}:
                continue
            payload['definition']['merged_ids'] = ids
            try:
                for item in [payload] + extra:
                    PolicyInput.model_validate(item)
            except ValueError:
                continue
            probe = {**payload, 'id': '~probe', 'status': 'active', 'active': True, 'kind': members[0]['kind'], 'department_id': members[0].get('department_id')}
            same = not extra and expanded(members, {i: '~probe' for i in ids}, week_start) == expanded([probe], None, week_start)
            lines = payload['definition']['per_person']
            item = {'shape': shape, 'rule_ids': ids, 'kind': members[0]['kind'], 'name': payload['name'], 'lines': len(lines),
                    'people': len({r['person_id'] for r in lines if r.get('person_id')}), 'amounts': None, 'default_amount': None,
                    'starts_on': payload['starts_on'], 'ends_on': payload['ends_on'], 'switched_off': False, 'payload': payload}
            if same:
                groups.append(item)
            else:
                changes.append({**item, 'was': was, 'now': now, 'extra': extra})
            pool = [p for p in pool if p['id'] not in ids]
    return groups, changes
