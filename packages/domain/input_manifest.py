"""Versioned semantic currency, projected from the canonical evaluator's snapshot.

Re-resolving the snapshot detects inserted/deleted sources as well as row edits.
Counters and labels are deliberately absent; source identity remains for explanation.
"""
from datetime import date, timedelta
import hashlib
import json

from .conditions import active_rules
from .serialization import json_default
from .time import aware, local_midnight
from packages.contracts.schemas import GENERATION_RUNTIME_OPTIONS, GenerationOptions

FORMAT = 2
# Explicit input fields: adding an evaluator input requires extending this contract.
FIELDS = {
    'organisation': ('id', 'timezone', 'week_start'),
    'department': ('id', 'operating_calendar_id', 'weekend_days'),
    'workspace': ('id', 'department_id', 'starts_on', 'ends_on', 'origin', 'base_publication_id',
                  'replaces_publication_id', 'rule_adjustments', 'target_overrides'),
    'people': ('id', 'active', 'inactive_from', 'tags'),
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
    'types': ('id', 'department_id', 'definition', 'draft_safety', 'tags', 'active', 'starts_on', 'ends_on'),
    'operating_calendars': ('id', 'department_id', 'definition', 'active', 'starts_on', 'ends_on'),
    'date_sets': ('id', 'department_id', 'name', 'entries', 'active'),
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


def manifest(data, *, content_revision=None, include_sources=True):
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
    sources, components = {}, {'content': {}, 'mandatory_inputs': {}, 'objective_inputs': {}, 'toolchain': {}}
    def add(group, kind, key, value, source=None):
        source_key = kind + ':' + str(key)
        token = digest(value)
        components[group][source_key] = token
        if not include_sources:
            return
        row = source if source is not None else (value if isinstance(value, dict) else {})
        sources[source_key] = entry = {'kind': kind, 'id': str(key), 'source_id': str(row.get('standing_wish_id') or row.get('pattern_id') or row.get('event_occurrence_id') or row.get('id', key)), 'hash': token,
            **{k: row[k] for k in ('person_id', 'first_day', 'last_day', 'starts_on', 'ends_on', 'start', 'end', 'kind', 'created_by') if k in row}}
        entry['kind'] = kind
        # The same explicit projection owns semantic currency and sanitized before/after values.
        entry['facts'] = value if isinstance(value, dict) else {'value': value}
        if kind == 'rule_lines':
            entry['source_kind'] = 'policies'
        if row.get('pattern_id'):
            entry['source_kind'] = 'availability_patterns'
        if row.get('event_occurrence_id'):
            entry['source_kind'] = 'event_occurrences'
        if kind == 'wishes':
            entry['wish_kind'] = row.get('kind')
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
    # An effective parent capacity can bind outside the ordinary catalogue date window.
    from .validation import place_capacity_inputs
    capacity = place_capacity_inputs(data, data['shifts'])
    own_capacity = {ident: row for ident, row in capacity['duties'].items() if ident in focus}
    if own_capacity:
        add('content', 'place_capacity', 'own_duties', own_capacity)
    if capacity['limits']:
        external_capacity = {ident: row for ident, row in capacity['duties'].items() if ident not in focus}
        add('mandatory_inputs', 'place_capacity', 'place_capacity',
            {'duties': external_capacity, 'limits': capacity['limits']})
    # The resolver owns catalogue lines, precedence and safety; do not hash retired parent blobs.
    registries = set()
    bound_exceptions = {e['policy_id'] for e in data.get('exceptions', []) if e.get('status') == 'approved'}
    for row in active_rules(data['policies'], context=data):
        if row['department_id'] not in departments or not dated(row):
            continue
        value = project(row, ('id', 'revision', 'department_id', 'starts_on', 'ends_on', 'active', 'definition',
            'template_line', 'count_bucket', '_count_scope', '_quota_full_window', '_protected_interval',
            '_live_zero', '_legacy_own_lines', '_overtime_fact', '_overtime_request', '_overtime_source', '_overtime_dates',
            '_balance_sources', '_balance_people', '_balance_broad'))
        if row['id'] in bound_exceptions:
            value['exception_binding_revision'] = row.get('revision')
        line = row.get('template_line') or {}
        key = row['id'] + ':' + digest([line.get('id', line.get('line')), line.get('slot'),
            row['definition']['rule']['kind'], row['definition']['rule'].get('comparison'), row['definition']['severity']])
        registry = row.get('_catalogue_sources')
        if registry is not None and id(registry) not in registries:
            registries.add(id(registry))
            for source_id, source in registry.items():
                if dated(source):
                    add('mandatory_inputs', 'rule_lines', source_id, source, {**source, 'id': row['id']})
        add('mandatory_inputs' if row['definition']['severity'] == 'mandatory' else 'objective_inputs', 'policies', key, value, row)
    if data.get('applied_planning_population') is not None:
        add('mandatory_inputs', 'applied_planning_population', period['id'], data['applied_planning_population'])
    for key in ('quota_context', 'score_baseline'):
        add('mandatory_inputs', key, key, data.get(key))
    # Draft safety evaluation keeps non-safety inputs frozen, but any approved
    # live rule/exception/grant/leave change invalidates checks bound to it.
    if data.get('_live_safety_staleness') is not None:
        add('mandatory_inputs', 'live_safety', 'live_safety', data['_live_safety_staleness'])
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
    if basis := data.get('_draft_policy_basis'):
        dirty_hint['draft_policy_basis'] = basis
    if data.get('_live_safety_departments') is not None:
        dirty_hint['department_revisions'] = sorted(
            [row.get('id'), row.get('revision'), row.get('catalogue_revision')]
            for row in data['_live_safety_departments'])
    base = {'format': FORMAT, 'components': hashes, 'token': digest({'format': FORMAT, **hashes}),
            'dirty_hint': dirty_hint,
            'sources': sources, 'selection': {'workspace_id': period['id'], 'start': lo.isoformat(), 'end': hi.isoformat(),
                                            'department_ids': sorted(departments), 'person_ids': sorted(people)}}
    if own_capacity:
        base['own_place_capacity'] = components['content']['place_capacity:own_duties']
    return bind_generation_admission(base, data)


def bind_generation_admission(base, data):
    """Bind the group run while preserving the draft manifest used by checks."""
    if 'planning_population' not in data or not base:
        return base
    admission = project(data, ('planning_group_versions', 'planning_context', 'planning_population',
                               'generation_scope', 'lookahead'))
    components = {**base['components'], 'generation_admission': digest(admission)}
    return {**base, 'components': components, 'token': digest({'format': base['format'], **components})}
