"""Versioned semantic currency, projected from the canonical evaluator's snapshot.

Re-resolving the snapshot detects inserted/deleted sources as well as row edits.
Counters and labels are deliberately absent; source identity remains for explanation.
"""
from datetime import date, timedelta
from functools import lru_cache
import hashlib
import json

from .conditions import active_rules
from .serialization import json_default
from .time import aware, local_midnight
from packages.contracts.schemas import GENERATION_RUNTIME_OPTIONS, GenerationOptions, PolicyDefinition

FORMAT = 3
# Explicit input fields: adding an evaluator input requires extending this contract.
FIELDS = {
    'organisation': ('id', 'timezone', 'week_start'),
    'department': ('id', 'operating_calendar_id', 'weekend_days'),
    'workspace': ('id', 'department_id', 'starts_on', 'ends_on', 'origin', 'base_publication_id',
                  'replaces_publication_id', 'rule_adjustments', 'target_overrides'),
    'people': ('id', 'active', 'tags'),
    'tags': ('id', 'name', 'labels', 'members'),
    'memberships': ('id', 'department_id', 'person_id', 'category_id', 'starts_on', 'ends_on'),
    'eligibilities': ('id', 'department_id', 'person_id', 'service_id', 'starts_on', 'ends_on'),
    'grants': ('id', 'department_id', 'person_id', 'capability_id', 'service_id', 'starts_on', 'ends_on', 'revoked'),
    'rotations': ('id', 'department_id', 'person_id', 'rotation_type_id', 'cohort_id', 'service_id', 'mentor_id',
                  'attachment_link', 'department_available', 'starts_on', 'ends_on'),
    'programme_memberships': ('id', 'department_id', 'person_id', 'programme_id', 'starts_on', 'ends_on', 'external', 'home_programme'),
    'mentorships': ('id', 'department_id', 'person_id', 'mentor_id', 'programme_id', 'starts_on', 'ends_on',
                    'assignment_type_ids', 'service_ids', 'strength'),
    'availability': ('id', 'person_id', 'kind', 'start', 'end', 'counts_as_unavailable', 'retracted'),
    'rotation_leave_availability': ('id', 'person_id', 'kind', 'start', 'end', 'counts_as_unavailable', 'leave_type_id', 'retracted'),
    'closures': ('id', 'location_id', 'start', 'end'),
    'locations': ('id', 'department_id', 'location_type_id', 'parent_id', 'calendar_id', 'capacity', 'tags', 'active', 'starts_on', 'ends_on'),
    'services': ('id', 'department_id', 'mode', 'calendar_id', 'tags', 'active', 'starts_on', 'ends_on'),
    'categories': ('id', 'department_id', 'tags', 'active', 'starts_on', 'ends_on'),
    'capabilities': ('id', 'department_id', 'tags', 'active', 'starts_on', 'ends_on'),
    'location_types': ('id', 'department_id', 'tags', 'active', 'starts_on', 'ends_on'),
    'types': ('id', 'department_id', 'definition', 'tags', 'active', 'starts_on', 'ends_on'),
    'operating_calendars': ('id', 'department_id', 'definition', 'active', 'starts_on', 'ends_on'),
    'resource_pools': ('id', 'department_id', 'capacity', 'location_id', 'calendar_id', 'active', 'starts_on', 'ends_on'),
    'programmes': ('id', 'department_id', 'tags', 'active', 'starts_on', 'ends_on'),
    'rotation_types': ('id', 'department_id', 'leave_policy', 'tags', 'active', 'starts_on', 'ends_on'),
    'cohorts': ('id', 'department_id', 'programme_id', 'starts_on', 'ends_on', 'tags', 'active'),
    'recurring_series': ('id', 'department_id', 'name', 'description', 'assignment_type_id', 'location_id',
                         'definition', 'active', 'starts_on', 'ends_on'),
    'exceptions': ('id', 'policy_id', 'policy_revision', 'person_id', 'starts_on', 'ends_on', 'status', 'approved_by'),
    'scheduling_facts': ('id', 'department_id', 'kind', 'person_id', 'calendar_entry_id', 'status', 'on',
                         'replaces_id', 'replaces_revision', 'starts_on', 'ends_on', 'data', 'retired_at'),
    'type_lead_ownership': ('id', 'department_id', 'assignment_type_id', 'source_type_revision', 'source_history_id',
                            'source_lead_hash', 'initial_policy_id', 'initial_line_id', 'effective_on'),
    'wishes': ('id', 'person_id', 'first_day', 'last_day', 'kind', 'assignment_type_id', 'limit', 'weekend_start',
               'weekend_days', 'points', 'repeat', 'weight', 'multiplier', 'standing_wish_id'),
}
SHIFT_FIELDS = ('id', 'lineage_id', 'revision', 'workspace_id', 'assignment_type_id', 'type_revision', 'location_id', 'start', 'end', 'cancelled',
                'locked', 'attributes', 'service_ids', 'series_id', 'series_revision', 'series_snapshot', 'occurrence_on',
                'series_modified', 'template_skipped', 'closed', 'usual_assignee_unavailability', 'series_state_frozen')
PARTICIPANT_FIELDS = ('id', 'person_id', 'start', 'end', 'participation', 'overtime', 'fixed', 'supervisor_person_id',
                      'team_lead', 'segments')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=json_default).encode()).hexdigest()


def project(row, fields):
    return {key: row[key] for key in fields if key in row}


def shift_input(row):
    return {**project(row, SHIFT_FIELDS), 'type_snapshot': project(row.get('type_snapshot') or {},
            ('id', 'department_id', 'definition', 'active', 'starts_on', 'ends_on')),
            'participants': sorted((project(p, PARTICIPANT_FIELDS) for p in row['participants']), key=lambda p: p['id'])}


def toolchain(quota_policy_limit):
    from .validation import ENGINE_VERSION
    from .solver_rules import COMPILER_VERSION
    from .score import VERSION as SCORE_VERSION
    from .fairness import VERSION as FAIRNESS_VERSION
    from .quotas import METRIC_VERSION as QUOTA_VERSION
    return {'input_manifest': FORMAT, 'validator': ENGINE_VERSION, 'compiler': COMPILER_VERSION, 'score': SCORE_VERSION,
            'fairness': FAIRNESS_VERSION, 'quota': QUOTA_VERSION, 'quota_policy_limit': quota_policy_limit}


def feasibility_token(data):
    value = data.get('input_manifest') or manifest(data)
    return digest({'format': value['format'], 'purpose': 'feasibility',
                   **{key: value['components'][key] for key in ('content', 'mandatory_inputs', 'toolchain')}})


def generation_input(value):
    return GenerationOptions.model_validate(value or {}).model_dump(mode='json', exclude=GENERATION_RUNTIME_OPTIONS)


def _policy_input(row):
    # The resolver emits the same definition contract the compiler validates. Read the contract's
    # fields directly so adding an input to it also adds that input to currency.
    value = project(row, ('id', 'department_id', 'starts_on', 'ends_on', 'active', 'status', 'replaces_id',
        'count_bucket', '_count_scope', '_quota_full_window', '_protected_interval', '_overtime_fact',
        '_overtime_request', '_overtime_source', '_overtime_dates', '_fair_share_group', '_period_override', '_comparison_scope'))
    value['definition'] = project(row['definition'], PolicyDefinition.model_fields)
    if line := row.get('template_line'):
        value['template_line'] = line
    for field in ('_balance_sources', '_balance_broad'):
        if field in row:
            value[field] = [_policy_input(source) for source in row[field]]
    if '_balance_people' in row:
        value['_balance_people'] = {person: [_policy_input(source) for source in sources]
                                   for person, sources in row['_balance_people'].items()}
    return value


# Keep a catalogue's worth of current and captured definitions reusable across a list read.
@lru_cache(maxsize=1024)
def _catalogue_definition_issues(text):
    from .solver_rules import definition_issues

    return tuple((issue['field'], issue['code']) for issue in definition_issues(json.loads(text), historical=True))


def manifest(data, *, content_revision=None):
    period, zone = data['workspace'], data['organisation']['timezone']
    focus = set(data['focus_ids'])
    lo = local_midnight(date.fromisoformat(str(period['starts_on'])), zone) - timedelta(days=32)
    hi = local_midnight(date.fromisoformat(str(period['ends_on'])), zone) + timedelta(days=32)
    if data.get('quota_context'):
        lo, hi = aware(data['quota_context']['start']), aware(data['quota_context']['end'])
    first, last = lo.date().isoformat(), hi.date().isoformat()
    def dated(row):
        return (not row.get('starts_on') or str(row['starts_on']) < last) and (
            not row.get('ends_on') or str(row['ends_on']) > first)
    departments = {period['department_id']}
    departments.update(data['types'][s['assignment_type_id']]['department_id'] for s in data['shifts'])
    people = {r['person_id'] for r in data['memberships'] if r['department_id'] in departments and dated(r)}
    people.update(p['person_id'] for s in data['shifts'] for p in s['participants'])
    # Fairness leave accounting can extend the objective horizon for the selected population.
    lo = min([lo, *[aware(r['start']) for r in data.get('rotation_leave_availability', []) if r['person_id'] in people]])
    first = lo.date().isoformat()

    def selected(row):
        return (not row.get('person_id') or row['person_id'] in people) and (
            not row.get('department_id') or row['department_id'] in departments) and dated(row)

    def approved_exceptions(context):
        return [row for row in context.get('exceptions', []) if row.get('status') == 'approved' and selected(row)]

    def catalogue_definition_issues(context):
        issues = {}
        for row in context['policies']:
            if row['status'] != 'active' or not row.get('active', True):
                continue
            definition = row['definition']
            rule = definition['rule']
            if rule['kind'] != 'catalogue' or not selected(row):
                continue
            # The compiler also validates the parent definition. Expansion already covers the lines.
            envelope = {**definition, 'rule': {**rule, 'lines': []}}
            if found := _catalogue_definition_issues(json.dumps(envelope, sort_keys=True, default=json_default)):
                issues[row['id']] = [list(issue) for issue in found]
        return issues

    def policy_sources(context, exceptions):
        # The binding revision is operative for exceptions and quota overrides pinned to a rule version.
        exception_ids = {row['policy_id'] for row in exceptions}
        override_ids = {row['policy_id'] for row in period.get('target_overrides') or []}
        registries, copies, lead_revisions = set(), {}, {}
        for row in active_rules(context['policies'], context=context):
            if not selected(row):
                continue
            value = _policy_input(row)
            definition = value['definition']
            rule = definition['rule']
            if row['id'] in exception_ids:
                value['exception_binding_revision'] = row.get('revision')
            if row['id'] in override_ids and rule['kind'] == 'block_quota':
                value['target_binding_revision'] = row.get('revision')
            if rule['kind'] == 'team_lead_requirement' and rule.get('count_scope') == 'concurrent_duties':
                from .leadership import _requirement_key
                _, policy_id, revision, *line_identity = _requirement_key({'policy': row})
                versions = lead_revisions.setdefault((policy_id, *line_identity), {})
                # The leadership reader compares versions for equality, so uniform counter bumps preserve the group.
                value['leadership_revision_group'] = versions.setdefault(revision, len(versions))
            line = value.get('template_line', {})
            key = row['id'] + ':' + digest([line.get('id', line.get('line')), line.get('slot'),
                rule['kind'], rule.get('comparison'), definition['severity'], definition.get('strength')])
            # A bound default can fan out into several scopes with the same source identity.
            copies[key] = copies.get(key, 0) + 1
            if copies[key] > 1:
                key += ':' + str(copies[key])
            registry = row.get('_catalogue_sources')
            if registry is not None and id(registry) not in registries:
                registries.add(id(registry))
                for source_id, source in registry.items():
                    if dated(source):
                        yield 'rule_lines', source_id, source, {**source, 'id': row['id']}
            yield 'policies', key, value, row

    sources, components = {}, {'content': {}, 'mandatory_inputs': {}, 'objective_inputs': {}, 'toolchain': {}}
    def add(group, kind, key, value, source=None):
        token = digest(value)
        components[group][kind + ':' + str(key)] = token
        row = source if source is not None else (value if isinstance(value, dict) else {})
        sources[kind + ':' + str(key)] = {'kind': kind, 'id': str(key), 'source_id': str(row.get('standing_wish_id') or row.get('pattern_id') or row.get('event_occurrence_id') or row.get('id', key)), 'hash': token,
            **{k: row[k] for k in ('person_id', 'first_day', 'last_day', 'starts_on', 'ends_on', 'start', 'end', 'kind', 'created_by') if k in row}}
        sources[kind + ':' + str(key)]['kind'] = kind
        # The same explicit projection owns semantic currency and sanitized before/after values.
        sources[kind + ':' + str(key)]['facts'] = value if isinstance(value, dict) else {'value': value}
        if kind == 'rule_lines':
            sources[kind + ':' + str(key)]['source_kind'] = 'policies'
        if row.get('pattern_id'):
            sources[kind + ':' + str(key)]['source_kind'] = 'availability_patterns'
        if row.get('event_occurrence_id'):
            sources[kind + ':' + str(key)]['source_kind'] = 'event_occurrences'
        if kind == 'wishes':
            sources[kind + ':' + str(key)]['wish_kind'] = row.get('kind')
    for kind in ('organisation', 'department'):
        facts = project(data[kind], FIELDS[kind])
        # An explicit calendar owns working days; weekends are quota inputs only for the fallback.
        if kind == 'department' and data[kind].get('operating_calendar_id') is not None:
            facts.pop('weekend_days', None)
        add('mandatory_inputs', kind, data[kind]['id'], facts)
    add('content', 'workspace', period['id'], {**project(period, FIELDS['workspace']),
        'content_revision': period['content_revision'] if content_revision is None else content_revision})
    for row in data['shifts']:
        add('content' if row['id'] in focus else 'mandatory_inputs', 'shifts', row['id'], shift_input(row), row)
    for kind, fields in FIELDS.items():
        if kind in ('organisation', 'department', 'workspace'):
            continue
        rows = data.get(kind, {})
        rows = rows.values() if isinstance(rows, dict) else rows
        for row in rows:
            if kind == 'people' and row['id'] not in people or row.get('person_id') and row['person_id'] not in people:
                continue
            if row.get('department_id') and row['department_id'] not in departments or not dated(row):
                continue
            if kind == 'closures' and not (aware(row['start']) < hi and aware(row['end']) > lo):
                continue
            if kind == 'exceptions' and row.get('status') != 'approved':
                continue
            add('objective_inputs' if kind == 'wishes' else 'mandatory_inputs', kind, row['id'], project(row, fields), row)
    # The resolver owns catalogue lines, precedence and safety; do not hash retired parent blobs.
    for kind, key, value, row in policy_sources(data, approved_exceptions(data)):
        group = 'objective_inputs' if kind == 'policies' and value['definition']['severity'] == 'advisory' else 'mandatory_inputs'
        add(group, kind, key, value, row)
    if issues := catalogue_definition_issues(data):
        add('mandatory_inputs', 'policy_definition_issues', 'policy_definition_issues', issues)
    if data.get('applied_planning_population') is not None:
        add('mandatory_inputs', 'applied_planning_population', period['id'], data['applied_planning_population'])
    for key in ('quota_context', 'score_baseline'):
        add('mandatory_inputs', key, key, data.get(key))
    # Draft safety evaluation keeps non-safety inputs frozen, but any approved
    # live rule/exception/grant/leave change invalidates checks bound to it.
    if (live := data.get('_live_safety_staleness')) is not None:
        exceptions = approved_exceptions(live)
        context = {**data, **live, '_catalogue_v2_materialized': False}
        # Apply distinguishes governed rule drift from independently approved person facts.
        governed = {row['id'] for row in live['policies']}
        policies, rule_lines = [], []
        for kind, key, value, row in policy_sources(context, exceptions):
            if row['id'] in governed:
                (policies if kind == 'policies' else rule_lines).append({**value, 'copy_key': key})
        issues = catalogue_definition_issues(context)
        add('mandatory_inputs', 'live_safety', 'live_safety', {
            'policies': sorted(policies, key=lambda row: row['copy_key']),
            'rule_lines': sorted(rule_lines, key=lambda row: (row['copy_key'], digest(row))),
            'exceptions': [project(row, FIELDS['exceptions']) for row in exceptions],
            **({'policy_definition_issues': issues} if issues else {}),
            **project(live, ('grants', 'availability', 'scheduling_facts'))})
    for key, row in data.get('date_sets', {}).items():
        if row.get('department_id') in departments:
            add('mandatory_inputs', 'date_sets', key, project(row, ('id', 'department_id', 'active', 'entries')), row)
    for key in ('fairness_history', 'fairness_strengths', 'fairness_semantics', 'fairness_history_start', 'score_context'):
        add('objective_inputs', key, key, data.get(key))
    add('objective_inputs', 'generation_options', 'generation_options', generation_input(data.get('generation_options')),
        {'id': period['department_id']})
    components['toolchain'] = toolchain(data.get('quota_policy_limit'))
    hashes = {key: digest(value) for key, value in components.items()}
    dirty_hint = {'input_revision': data['organisation'].get('input_revision'),
                  'availability_revision': data['organisation'].get('availability_revision'),
                  'content_revision': period['content_revision'] if content_revision is None else content_revision,
                  'baseline': [(data.get('baseline') or {}).get('id'),
                               (data.get('baseline') or {}).get('snapshot_hash')
                               or (data.get('baseline') or {}).get('hash')]}
    if data.get('_live_safety_departments') is not None:
        dirty_hint['department_revisions'] = sorted(
            [row.get('id'), row.get('revision'), row.get('catalogue_revision')]
            for row in data['_live_safety_departments'])
    return {'format': FORMAT, 'components': hashes, 'token': digest({'format': FORMAT, **hashes}),
            'dirty_hint': dirty_hint,
            'sources': sources, 'selection': {'workspace_id': period['id'], 'start': lo.isoformat(), 'end': hi.isoformat(),
                                            'department_ids': sorted(departments), 'person_ids': sorted(people)}}
