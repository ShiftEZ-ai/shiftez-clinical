"""The rule catalogue: one entry per idea.

A catalogue rule is one policy row per entry; its lines say who, where, when and the numbers. Each number a line may
set writes one or more *slots* of the old rule kinds every check reads (`packages/domain/rule_lines.py` builds them):
`{kind, field, fixed, advisory}` — the old kind, the field the number sets (None: a rule without a number, `apply`),
settings that number always has (a quota maximum is `comparison: maximum`), and whether it is a preference only. When a
number has several slots, a line's effective settings choose: the first slot whose kind has settings, else the first.
"""
from copy import deepcopy
from typing import get_args, get_origin

from .schemas import (NEVER_OVERRIDDEN, PATTERN_WISH_KINDS, WORKLOAD_COUNT_KINDS, Conditions, RuleLine, RulePrimitive,
                      WorkloadPeriod, WishKind, GenerationNotApplied)

FAMILIES = (('staffing', 'Staffing on a session'), ('workload', 'Workload over time'), ('pattern', 'Work pattern'),
            ('gaps', 'Gaps between duties'), ('together', 'Who works together'), ('protected', 'Protected time'),
            ('preferences', 'Preferences & fairness'))
LEVELS = ('mandatory', 'critical', 'high', 'medium', 'low')
ADVISORY = LEVELS[1:]
# The numbers a line can set, in the order checks read them (a quota's minimums, then maximums, then targets).
NUMBER_ORDER = ('min', 'max', 'target', 'priority', 'session_priority', 'apply')
LINE_COMPARISONS = ('minimum', 'exact', 'maximum', 'range', 'target')


def line_comparison(numbers):
    """The one comparison vocabulary shared by authoring and wording.

    Exact is canonicalised from equal mandatory minimum and maximum values; stored
    lines continue to use the existing min/max shape for backwards compatibility.
    """
    numbers = numbers or {}
    minimum, maximum = numbers.get('min'), numbers.get('max')
    minimum_value = minimum.get('value') if minimum else None
    maximum_value = maximum.get('value') if maximum else None
    target_value = (numbers.get('target') or {}).get('value')
    if minimum_value is not None and maximum_value is not None and maximum_value == minimum_value \
            and (minimum.get('level') or 'mandatory') == 'mandatory' and (maximum.get('level') or 'mandatory') == 'mandatory':
        return 'exact'
    if minimum_value is not None and maximum_value is not None:
        return 'range'
    if minimum_value is not None:
        return 'minimum'
    if maximum_value is not None:
        return 'maximum'
    if target_value is not None:
        return 'target'
    return None


def number_range_errors(numbers):
    """Catalogue bounds, shared by authoring and inherited-line diagnostics."""
    ordered = [(key, numbers[key]) for key in ('min', 'target', 'max') if numbers.get(key) is not None]
    names = {'min': 'minimum', 'target': 'target', 'max': 'maximum'}
    return [(upper, f'The {names[upper]} is below the {names[lower]}: no session could meet it.')
            for (lower, a), (upper, b) in zip(ordered, ordered[1:]) if a > b]


def staffing_pool_message(name, required, eligible):
    return (f'Rule cannot be met: {name} needs {required:g} ' + ('person' if required == 1 else 'people') +
            f'; only {eligible} ' + ('matches' if eligible == 1 else 'match') + ' the eligible pool')


def _slot(kind, field=None, advisory=False, **fixed):
    return {'kind': kind, 'field': field, 'fixed': fixed, 'advisory': advisory}


def _quota(metric=None):
    """A block quota's minimum, maximum and target (one check each, as a range always was); metric None: a setting."""
    fixed = {'metric': metric} if metric else {}
    return {'min': [_slot('block_quota', 'amount', comparison='minimum', **fixed)],
            'max': [_slot('block_quota', 'amount', comparison='maximum', **fixed)],
            'target': [_slot('block_quota', 'amount', True, comparison='target', **fixed)]}


def _with(numbers, **more):
    """Numbers with extra slots tried first (a limit kind before the quota that can say the same)."""
    return {k: more.get(k, []) + numbers.get(k, []) for k in dict.fromkeys([*more, *numbers])}


def _entry(id, family, name, description, unit, numbers, levels=LEVELS, settings=None):
    """`default_line`, `default_settings` and `needs` are set from FIRST below."""
    return {'id': id, 'family': family, 'name': name, 'description': description, 'unit': unit,
            'numbers': {k: numbers[k] for k in NUMBER_ORDER if k in numbers}, 'levels': list(levels),
            'default_line': {}, 'default_settings': settings or {}, 'needs': {},
            'fix_template': {'sentence': '{people_subject} {need} a review of ' + name.lower() + '.',
                             'meta': '{dates} · {rules}'}}


_COVER = {'min': [_slot('minimum_cover', 'minimum')], 'max': [_slot('maximum_participants', 'maximum')],
          'target': [_slot('minimum_cover', 'minimum', True)]}
_WEEKLY = {'block_quota': {'window_alignment': 'calendar_week'}}



RULE_CATALOGUE = (
    _entry('team_leads', 'staffing', 'Team leads', 'Qualified named leaders for a duty.', 'people',
           {'min': [_slot('team_lead_requirement', 'minimum')],
            'max': [_slot('team_lead_requirement', 'maximum')]}, ('mandatory',)),
    _entry('people_per_session', 'staffing', 'People per session', 'How many people a session needs and holds.', 'people',
           _COVER),
    _entry('kind_per_session', 'staffing', 'People of a kind per session',
           'How many people of a rank, skill or programme a session needs and holds (who says who counts).', 'people',
           _COVER),
    _entry('supervision', 'staffing', 'Supervision', 'Which trainees a supervisor may take, and how many.', 'trainees',
           {'max': [_slot('supervision_limit', 'maximum', mode='capacity')], 'apply': [_slot('supervision_limit', mode='allowed_ranks')]},
           ('mandatory',)),
    _entry('duties_per_week', 'workload', 'Duties per week', 'How many duties a person works each week.', 'duties',
           _with(_quota('duties'), max=[_slot('max_duties', 'maximum', window='week')]), settings=_WEEKLY),
    _entry('duties_per_month', 'workload', 'Duties per month', 'How many duties a person works each calendar month.', 'duties', _quota('duties'),
           settings={'block_quota': {'window_alignment': 'calendar_month'}}),
    _entry('duties_per_period', 'workload', 'Duties per period', 'How many duties a person works in a day, a rolling run of days or a block.', 'duties',
           _with(_quota('duties'), max=[_slot('max_duties', 'maximum')])),
    _entry('hours_per_week', 'workload', 'Hours per week', 'How many hours a person works each week.', 'hours',
           _with(_quota('scheduled_hours'), max=[_slot('max_hours', 'hours', window='week')]), settings=_WEEKLY),
    _entry('hours_per_period', 'workload', 'Hours per period', 'How many hours a person works in a day, a rolling run of days or a block.', 'hours',
           _with(_quota('scheduled_hours'), max=[_slot('max_hours', 'hours')])),
    _entry('session_units_per_week', 'workload', 'Session units per week', 'How many session units a person works each week.', 'units',
           _quota('session_units'), settings=_WEEKLY),
    _entry('other_counts_per_period', 'workload', 'Other counts per period',
           'Session units, reserved hours, workload or training credit and lead turns over a period.', 'units', _quota(),
           settings={'block_quota': {'metric': 'session_units'}}),
    _entry('days_in_a_row', 'pattern', 'Days in a row', 'How many days in a row a person works.', 'days',
           {'min': [_slot('run_length', 'minimum', True)], 'max': [_slot('max_consecutive_days', 'maximum'), _slot('run_length', 'maximum', True)]}),
    _entry('days_off_together', 'pattern', 'Days off together', 'How many days off a person has together.', 'days',
           {'min': [_slot('days_off_run', 'minimum', True)]}, ADVISORY),
    _entry('weekends_in_a_row', 'pattern', 'Weekends in a row', 'How many weekends in a row a person works.', 'weekends',
           {'max': [_slot('max_consecutive_weekends', 'maximum')]}),
    _entry('weekend_shape', 'pattern', 'Weekend shape', 'Whole weekends, or the same duties on each weekend day.', None,
           {'apply': [_slot('weekend_shape', advisory=True)]}, ADVISORY),
    _entry('shift_order', 'pattern', 'Shift order', 'The next day’s duty is not earlier (early → late → night).', None,
           {'apply': [_slot('shift_succession', advisory=True, mode='forward')]}, ADVISORY),
    _entry('rest_between_duties', 'gaps', 'Rest between duties', 'How many hours of rest a person has between duties.', 'hours',
           {'min': [_slot('min_rest', 'hours')]}),
    _entry('not_after', 'gaps', 'Not after', 'A duty type is not followed by another (next day, or within days).', None,
           {'apply': [_slot('forbidden_transition'), _slot('shift_succession', advisory=True, mode='avoid')]}),
    _entry('rest_after_series', 'gaps', 'Rest after a series', 'Days off after a run of night (or other) duties.', None,
           {'apply': [_slot('shift_succession', advisory=True, mode='rest_after_series')]}, ADVISORY),
    _entry('travel_between_places', 'gaps', 'Travel between places', 'Minutes between duties at two places.', 'minutes',
           {'min': [_slot('travel_buffer', 'minutes')]}),
    _entry('together_apart', 'together', 'Together or apart', 'Named people work the same days (or sessions), or not.', None,
           {'apply': [_slot('together_apart', advisory=True)]}, ADVISORY),
    _entry('companion_cover', 'together', 'Companion cover', 'When named people are on duty, a session needs more people.', 'people',
           {'min': [_slot('minimum_cover', 'minimum')], 'target': [_slot('minimum_cover', 'minimum', True)]}),
    _entry('protected_time', 'protected', 'Protected time', 'Academic and protected time: no duties.', None,
           {'apply': [_slot('restriction')]}),
    _entry('keep_off', 'protected', 'Keep off', 'No duties for these people at these times or places.', None,
           {'apply': [_slot('restriction')]}),
    _entry('allocation_order', 'preferences', 'Allocation order', 'Which sessions are filled first.', 'priority',
           {'session_priority': [_slot('allocation_preference', 'session_priority', True)],
            'priority': [_slot('allocation_preference', 'priority', True)]}, ADVISORY),
    _entry('preferences', 'preferences', 'Preferred people', 'Who is preferred for which sessions or supervisors.', 'priority',
           {'priority': [_slot('allocation_preference', 'priority', True)],
            'session_priority': [_slot('allocation_preference', 'session_priority', True)]}, ADVISORY),
    _entry('overtime_offers', 'preferences', 'Overtime offers', 'Who is offered overtime, and under which limits.', None,
           {'apply': [_slot('allocation_overtime', advisory=True)]}, ADVISORY),
    _entry('fair_share', 'preferences', 'Fair share',
           'Share eligible workload, duties, hours, lead turns, supervision, overtime, weekends or pairings fairly; adjust shares and tolerance per line.', None,
           {'target': [_slot('balance_goal', 'amount', True)], 'apply': [_slot('balance_goal', advisory=True)]}, (*ADVISORY, 'off')),
)
CATALOGUE_BY_ID = {e['id']: e for e in RULE_CATALOGUE}

# F5's omissions describe selection, not final validation; supported advisory inputs stay out of this list.
GREEDY_NOT_APPLIED = [item.model_dump(mode='json') for item in (
    GenerationNotApplied(input='wishes', kinds=[kind for kind in get_args(WishKind) if kind not in PATTERN_WISH_KINDS],
        message='Dated wishes are not used to select assignments.'),
    GenerationNotApplied(input='work_patterns', kinds=sorted({slot['kind']
        for entry in RULE_CATALOGUE if entry['family'] in {'pattern', 'gaps', 'together'}
        for slots in entry['numbers'].values() for slot in slots
        if slot['advisory'] and slot['kind'] != 'minimum_cover'}),
        message='Work patterns are not used to select assignments.'),
    GenerationNotApplied(input='fairness_targets', kinds=sorted({slot['kind']
        for slots in CATALOGUE_BY_ID['fair_share']['numbers'].values() for slot in slots}),
          message='Ordinary fairness targets are used only when “Also used by the basic scheduler” is enabled; configured '
                  'share, weekly spread and pair limits are used; explicit lead-turn goals are still used.'),
    GenerationNotApplied(input='advisory_limits', kinds=sorted({slot['kind']
        for entry in RULE_CATALOGUE if 'mandatory' in entry['levels']
        for slots in entry['numbers'].values() for slot in slots
        if not slot['advisory'] and slot['kind'] not in {'minimum_cover', 'supervision_limit', 'team_lead_requirement'}}),
        message='Advisory limits are not used to select assignments; for block quotas this means advisory maxima only. '
                'Quota minima and targets, coverage needs, allocation preferences and mandatory limits are still used.'),
)]

def _line(key, value=None, level='mandatory'):
    return {'numbers': {key: {'value': value, 'level': level}}}


_LIMIT_FIRST = {'max_duties': {}, 'block_quota': {'window_alignment': 'calendar_week'}}
# What turning an entry on starts with: its first line, its settings, and the settings the department must choose (its
# records: duty types, places, people). A maximum alone is a limit (no block dates); a line with a minimum or target is
# a quota, whose block the line's dates give (rule.propose fills a year from the start when a line has none).
FIRST = {
    'team_leads': ({'numbers': {key: {'value': 1, 'level': 'mandatory'} for key in ('min', 'max')}}, None, {}),
    'people_per_session': (_line('min', 1), None, {}), 'kind_per_session': (_line('min', 1), None, {}),
    'supervision': (_line('max', 2), None, {}),
    'duties_per_week': (_line('max', 5), _LIMIT_FIRST, {}),
    'duties_per_month': (_line('max', 20), None, {}),
    'duties_per_period': (_line('max', 1), {'max_duties': {'window': 'day'}}, {}),
    'hours_per_week': (_line('max', 48), {'max_hours': {}, 'block_quota': {'window_alignment': 'calendar_week'}}, {}),
    'hours_per_period': (_line('max', 12), {'max_hours': {'window': 'day'}}, {}),
    'session_units_per_week': (_line('max', 10), None, {}), 'other_counts_per_period': (_line('max', 10), None, {}),
    'days_in_a_row': (_line('max', 6), None, {}), 'days_off_together': (_line('min', 2, 'medium'), None, {}),
    'weekends_in_a_row': (_line('max', 1), None, {}), 'weekend_shape': (_line('apply', level='medium'), None, {}),
    'shift_order': (_line('apply', level='medium'), None, {'shift_succession': ['type_order']}),
    'rest_between_duties': (_line('min', 11), None, {}),
    'not_after': (_line('apply'), None, {'forbidden_transition': ['from_type_ids', 'to_type_ids']}),
    'rest_after_series': (_line('apply', level='medium'), None, {'shift_succession': ['from_type_ids']}),
    'travel_between_places': (_line('min', 30), None, {'travel_buffer': ['from_location_id', 'to_location_id']}),
    'together_apart': (_line('apply', level='medium'), None, {'together_apart': ['mode', 'person_ids']}),
    'companion_cover': (_line('min', 1), None, {'minimum_cover': ['when_person_ids']}),
    'protected_time': (_line('apply'), None, {}), 'keep_off': (_line('apply'), None, {}),
    'allocation_order': (_line('session_priority', 1, 'medium'), {'allocation_preference': {'priority': 0}}, {}), 'preferences': (_line('priority', 1, 'medium'), None, {}),
    'overtime_offers': ({**_line('apply', level='medium'),
                        'settings': {'allocation_overtime': {'volunteer_required': True}}}, None, {}),
    'fair_share': (_line('apply', level='medium'), {'balance_goal': {'metric': 'duties'}}, {}),
}
for _id, (_first, _settings, _needs) in FIRST.items():
    CATALOGUE_BY_ID[_id].update(default_line=_first, needs=_needs)
    if _settings is not None:
        CATALOGUE_BY_ID[_id]['default_settings'] = _settings
# Suggestions for a new overtime line only; existing goals and ordinary fair-share defaults keep their settings.
CATALOGUE_BY_ID['fair_share']['suggested_lines'] = {
    metric: {'default_line': _line('apply', level='medium'),
             'default_settings': {'balance_goal': {'metric': metric, 'period': 'quarter', 'share_by': 'offered'}}}
    for metric in ('overtime_duties', 'overtime_hours')}
_MODELS = {get_args(m.model_fields['kind'].annotation)[0]: m for m in get_args(get_args(RulePrimitive)[0])}
# Embedded selector fields and their list cardinality come from the primitive contracts, never their names.
PRIMITIVE_CONDITION_FIELDS = {
    kind: {name: get_origin(field.annotation) is list for name, field in model.model_fields.items()
           if field.annotation is Conditions or Conditions in get_args(field.annotation)}
    for kind, model in _MODELS.items()}


def entry_kinds(entry):
    """The old kinds an entry's numbers write, in slot order."""
    return list(dict.fromkeys(s['kind'] for slots in entry['numbers'].values() for s in slots))


def slot_for_number(entry, key, rule, line, keys):
    """Which old kind a number writes: one the line sets, else one another of its numbers can only write (a quota's
    maximum beside its minimum), else one the rule sets, else the first."""
    slots = entry['numbers'][key]
    binding = (line.get('source_bindings') or {}).get(key)
    if binding:
        kind = binding['primitive_kind']
        matches = [s for s in slots if s['kind'] == kind]
        if not matches:
            raise ValueError(f"{entry['name']}: {key} cannot retain the {kind} source slot")
        return matches[0]
    forced = {entry['numbers'][k][0]['kind'] for k in keys if len(entry['numbers'][k]) == 1}
    return max(slots, key=lambda s: (s['kind'] in (line.get('settings') or {}), s['kind'] in forced,
                                     s['kind'] in (rule.get('settings') or {}), -slots.index(s)))


def safety_slot_policy(entry, key, rule, line):
    """Resolve a catalogue number to its contract-declared safety merge policy."""
    from .schemas import safety_merge_policy
    slot = slot_for_number(entry, key, rule, line, line.get('numbers') or {})
    return slot['kind'], slot.get('field'), safety_merge_policy(slot['kind'], slot.get('field'))


def _line_settings(entry):
    """Per old kind, the settings a rule or a line may set (everything but the kind, its numbers and fixed settings)."""
    out = {}
    for kind in entry_kinds(entry):
        slots = [s for ss in entry['numbers'].values() for s in ss if s['kind'] == kind]
        taken = {s['field'] for s in slots} | {k for s in slots for k in s['fixed']}
        out[kind] = [f for f in _MODELS[kind].model_fields if f != 'kind' and f not in taken]
    return out


for _e in RULE_CATALOGUE:
    # Safety entries never let one line outrank another: every matching line applies and the strictest binds.
    _e['safety'] = any(k in NEVER_OVERRIDDEN for k in entry_kinds(_e))
    _e['line_settings'] = _line_settings(_e)
    if _e['id'] == 'team_leads':
        _e['setting_labels'] = {'continuity': 'Continuity', 'count_scope': 'Count leaders'}
        _e['setting_help'] = {'count_scope': 'Count leaders per duty, or across these concurrent duties together.'}
        _e['setting_option_labels'] = {'count_scope': {
            'duty': 'Per duty', 'concurrent_duties': 'Across these concurrent duties together'}}
    if _e['id'] == 'fair_share':
        _e['setting_labels'] = {'metric': 'What to count', 'comparison': 'Groups'}
        _e['setting_option_labels'] = {'comparison': {
            'within_group': 'Each group on its own', 'pooled': 'Pool these groups together'}}
    _kinds = set(entry_kinds(_e))
    _e['count_sides'] = (['where'] if _kinds & (WORKLOAD_COUNT_KINDS | {'forbidden_transition'}) else
                         ['who', 'where'] if _kinds & {'minimum_cover', 'maximum_participants', 'supervision_limit', 'spread_evenly', 'together_apart'} else [])


# Historical policies/snapshots keep the v1 ids. Authoring v2 selects this registry explicitly.
CATALOGUE_VERSION = 2
CATALOGUE_MAPPING = {e['id']: e['id'] for e in RULE_CATALOGUE}
CATALOGUE_MAPPING.update(kind_per_session='people_per_session', rest_between_duties='duty_spacing',
                         not_after='duty_spacing', keep_off='workload_limit', protected_time='workload_limit',
                         overtime_offers=None)
# Explicit person/calendar protected time becomes data; groups remain rules.
PERSON_CALENDAR_FACT_MAPPING = {'protected_time': 'protected_time'}
CATALOGUE_MAPPING.update({id: 'workload_limit' for id in (
    'duties_per_week', 'duties_per_month', 'duties_per_period', 'hours_per_week', 'hours_per_period',
    'session_units_per_week', 'other_counts_per_period')})
CATALOGUE_MAPPING['team_leads'] = 'team_leads'

_V2_SAME = ('team_leads', 'people_per_session', 'supervision', 'days_in_a_row', 'days_off_together', 'weekends_in_a_row',
            'weekend_shape', 'shift_order', 'rest_after_series', 'travel_between_places', 'together_apart',
            'companion_cover', 'allocation_order', 'preferences', 'fair_share')
_V2_ENTRIES = {id: deepcopy(CATALOGUE_BY_ID[id]) for id in _V2_SAME}
_V2_ENTRIES['spread_evenly'] = _entry(
    'spread_evenly', 'staffing', 'Spread evenly',
    'Distribute selected members evenly across supported holders within a bounded period.', None,
    {'max': [_slot('spread_evenly', 'maximum_gap')]})
_V2_ENTRIES['spread_evenly'].update(
    default_line=_line('max', 1, level='medium'),
    default_settings={'spread_evenly': {'holder': 'supervisors', 'within': 'session', 'mode': 'levelled'}},
    setting_labels={'holder': 'Across', 'within': 'Within', 'mode': 'How even', 'maximum_gap': 'Maximum gap'},
    setting_help={'maximum_gap': 'Levelled means a maximum gap of one; a supervision adjustment may only tighten the configured gap.'},
    setting_option_labels={'holder': {'supervisors': 'Supervisors in each session'},
                           'within': {'session': 'Each session'},
                           'mode': {'levelled': 'Levelled', 'maximum_gap': 'Maximum gap'}})
_V2_ENTRIES['workload_limit'] = _entry(
    'workload_limit', 'workload', 'Workload limit', 'Counts over time, or no duties at the selected times and places.',
    None, _with(_quota(), max=[_slot('max_duties', 'maximum'), _slot('max_hours', 'hours')],
                apply=[_slot('restriction')]))
_V2_ENTRIES['workload_limit'].update(
    default_line=_line('max', 5), default_settings={'max_duties': {'window': 'week'}},
    measures=list(get_args(_MODELS['block_quota'].model_fields['metric'].annotation)),
    periods=list(get_args(WorkloadPeriod.model_fields['kind'].annotation)),
    modes=list(get_args(get_args(RuleLine.model_fields['mode'].annotation)[0])))
_V2_ENTRIES['duty_spacing'] = _entry(
    'duty_spacing', 'gaps', 'Rest between duties', 'Elapsed rest, next-day combinations, or preferred duty spacing.',
    None, {'min': [_slot('min_rest', 'hours')],
           'apply': [_slot('forbidden_transition'), _slot('shift_succession', advisory=True, mode='avoid')]})
_V2_ENTRIES['duty_spacing'].update(default_line=_line('min', 11),
    modes=['elapsed_rest', 'next_day_combination', 'avoid_within_days'])

# These labels are the shared questions used by the Rules page, the explainer and future "why" views.
# Keep them here with the catalogue; a frontend must never copy this list.
QUESTION_GROUPS = {
    'who_where': 'A Who may work where',
    'staffing': 'B Staffing in each session',
    'supervision': 'C Supervision',
    'workload_person': 'D Workload per person',
    'workload_group': 'E Workload limits for ranks/groups',
    'preferences': 'F Fairness and preferences',
}
QUESTION_GROUP_ORDER = tuple(QUESTION_GROUPS.values())
_V2_QUESTION_GROUPS = {
    'team_leads': 'staffing', 'people_per_session': 'staffing', 'supervision': 'supervision', 'spread_evenly': 'supervision',
    'workload_limit': 'workload_person', 'days_in_a_row': 'workload_person',
    'days_off_together': 'workload_person', 'weekends_in_a_row': 'workload_person',
    'weekend_shape': 'workload_person', 'shift_order': 'workload_person',
    'duty_spacing': 'workload_person', 'rest_after_series': 'workload_person',
    'travel_between_places': 'workload_person', 'together_apart': 'workload_person',
    'companion_cover': 'staffing', 'allocation_order': 'preferences',
    'preferences': 'preferences', 'fair_share': 'preferences',
}
assert set(_V2_QUESTION_GROUPS) == set(_V2_ENTRIES)
for _id, _group in _V2_QUESTION_GROUPS.items():
    _V2_ENTRIES[_id]['question_group'] = QUESTION_GROUPS[_group]

# One v2 entry deliberately replaces several historical ideas.  These mappings keep the
# old rule lines in the question they answer, while the base entry remains reusable.
_V2_ENTRIES['workload_limit']['question_group_aliases'] = {
    'protected_time': QUESTION_GROUPS['who_where'],
    'keep_off': QUESTION_GROUPS['who_where'],
}
_V2_ENTRIES['workload_limit']['question_group_by_slot'] = {
    # A no-duties restriction answers “who may work where”; it is not a D/E
    # workload count even though both use the Workload limit catalogue entry.
    'restriction': QUESTION_GROUPS['who_where'],
}
_V2_ENTRIES['workload_limit']['question_group_by_slot_level'] = {
    # Legacy weekly clinical targets are advisory block quotas without a v2
    # catalogue id; keep those preference facts in F while mandatory quotas
    # continue through the D/E scope routing above.
    'block_quota': {'advisory': QUESTION_GROUPS['preferences']},
}
_V2_ENTRIES['preferences']['question_group_aliases'] = {
    'overtime_offers': QUESTION_GROUPS['preferences'],
}
for _id in ('workload_limit', 'days_in_a_row', 'days_off_together', 'weekends_in_a_row',
            'weekend_shape', 'shift_order', 'duty_spacing', 'rest_after_series',
            'travel_between_places', 'together_apart'):
    _V2_ENTRIES[_id]['question_group_by_scope'] = {
        'person': QUESTION_GROUPS['workload_person'],
        'group': QUESTION_GROUPS['workload_group'],
    }
RULE_CATALOGUE_V2 = tuple(_V2_ENTRIES[id] for id in (
    'team_leads', 'people_per_session', 'supervision', 'spread_evenly', 'workload_limit', 'days_in_a_row', 'days_off_together',
    'weekends_in_a_row', 'weekend_shape', 'shift_order', 'duty_spacing', 'rest_after_series',
    'travel_between_places', 'together_apart', 'companion_cover', 'allocation_order', 'preferences', 'fair_share'))
V2_CATALOGUE_BY_ID = {e['id']: e for e in RULE_CATALOGUE_V2}
CATALOGUE_REGISTRIES = {1: CATALOGUE_BY_ID, 2: V2_CATALOGUE_BY_ID}


def catalogue_counts():
    """Counts exposed by the catalogue SSOT, including retained historical ids."""
    return {'historical': len(CATALOGUE_BY_ID),
            'v2': len(RULE_CATALOGUE_V2),
            'authorable': sum(bool(entry.get('authorable')) for entry in RULE_CATALOGUE_V2)}


# Presentation belongs to the catalogue, alongside each idea's numbers and source kinds.
_FIX_SENTENCES = {
    'team_leads': '{sessions_subject} need qualified team leads.',
    'people_per_session': '{coverage_sentence}',
    'kind_per_session': '{coverage_sentence}',
    'companion_cover': '{coverage_sentence}',
    'supervision': '{people_subject} {need} suitable supervision.',
    'spread_evenly': '{people_subject} {need} a more even distribution.',
    'workload_limit': '{people_subject} {quota_phrase}.',
    'days_in_a_row': '{people_subject} {need} a different run of working days.',
    'days_off_together': '{people_subject} {need} more days off together.',
    'weekends_in_a_row': '{people_subject} {need} fewer consecutive weekends.',
    'weekend_shape': '{people_subject} {need} a different weekend pattern.',
    'shift_order': '{people_subject} {need} a different order of duties.',
    'duty_spacing': '{people_subject} {need} more space between duties.',
    'rest_between_duties': '{people_subject} {need} more rest between duties.',
    'not_after': '{people_subject} {need} a different next duty.',
    'rest_after_series': '{people_subject} {need} more rest after a run of duties.',
    'travel_between_places': '{people_subject} {need} more time to travel between duties.',
    'together_apart': '{people_subject} {need} different working partners.',
    'protected_time': '{people_subject} {have} duties during protected time.',
    'keep_off': '{people_subject} {have} duties at a time they should be off.',
    'allocation_order': 'Some sessions could be filled in a better order.',
    'preferences': 'Some duties could better match people’s preferences.',
    'overtime_offers': 'Some overtime duties need a different placement.',
    'fair_share': 'Work could be shared more evenly.',
}
FIX_LIST_WORDS = {
    'weekend': 'Weekend', 'weekday': 'Weekday', 'person': 'person', 'people': 'people',
    'one_missing': 'a {ordinal} {who}', 'many_missing': '{count} more {who}',
    'detail_missing': '{sessions} · each has {have} of {need} {who}',
    'detail_sessions': '{sessions} affected', 'detail_people': '{people} affected',
    'session': 'session', 'sessions': 'sessions',
    'no_candidates': 'No eligible people are recorded for these sessions',
    'locked': 'Unlock the session before assigning someone',
    'week_hours': '{actual:g} hours this week',
    'weekend_run': '{ordinal} weekend in a row',
    'workload': '{actual:g} of {amount:g} {unit}{period}',
    'this_month': ' this month', 'this_week': ' this week', 'in_window': ' in this period',
    'generation_fallback': 'The schedule was found, but its detailed Fix list could not be prepared.',
}
_FIX_FACT_SENTENCES = {
    'people_per_session': {'missing': '{where} need {missing}',
                           'eligibility': 'No {who_label} is set up for {where}'},
    'kind_per_session': {'missing': '{where} need {missing}',
                         'eligibility': 'No {who_label} is set up for {where}'},
    'supervision': {'supervision': '{person} has no supervisor in {where}'},
    'workload_limit': {'workload': '{person} has {actual:g} duties; most {group} have {typical:g}'},
}
for _registry in CATALOGUE_REGISTRIES.values():
    for _id, _e in _registry.items():
        _key = 'workload_limit' if _e['family'] == 'workload' else _id
        if _key in _FIX_SENTENCES:
            _e['fix_template']['sentence'] = _FIX_SENTENCES[_key]
        _e['fix_template'].update(_FIX_FACT_SENTENCES.get(_key, {}))
for _e in RULE_CATALOGUE_V2:
    _e['catalogue_version'] = 2
    _e['authorable'] = True
    _e['replaces'] = [id for id, target in CATALOGUE_MAPPING.items() if target == _e['id']]
    _e['safety'] = any(k in NEVER_OVERRIDDEN for k in entry_kinds(_e))
    _e['mixed_safety'] = _e['safety'] and any(k not in NEVER_OVERRIDDEN for k in entry_kinds(_e))
    _e['line_settings'] = _line_settings(_e)
    for _key in ('setting_labels', 'setting_help', 'setting_option_labels'):
        if _key in CATALOGUE_BY_ID.get(_e['id'], {}):
            _e[_key] = deepcopy(CATALOGUE_BY_ID[_e['id']][_key])
    if 'allocation_preference' in _e['line_settings']:
        # The opposite legacy priority is a fallback of the same obligation, not another source numeric slot.
        _e['line_settings']['allocation_preference'] += list(dict.fromkeys(
            slot['field'] for slots in _e['numbers'].values() for slot in slots
            if slot['kind'] == 'allocation_preference' and slot['field']))
    _kinds = set(entry_kinds(_e))
    _e['count_sides'] = (['where'] if _kinds & (WORKLOAD_COUNT_KINDS | {'forbidden_transition'}) else
                         ['who', 'where'] if _kinds & {'minimum_cover', 'maximum_participants', 'supervision_limit', 'spread_evenly', 'together_apart'} else [])
for _e in RULE_CATALOGUE:
    _e.update(catalogue_version=1, authorable=_e['id'] == 'team_leads', replacement_id=CATALOGUE_MAPPING[_e['id']])


def catalogue_entry(id, catalogue_version=1):
    """Decode the recorded representation, never silently rename a historical id."""
    return CATALOGUE_REGISTRIES.get(catalogue_version, {}).get(id)


def question_group_entry(rule_id, catalogue_version=1):
    """Return the v2 catalogue entry that owns a rule's explanation group.

    ``overtime_offers`` deliberately has no v2 authorable entry because it is a
    person fact, but its explanation still belongs to Preferred people.  Resolve
    that alias from the catalogue metadata instead of treating an unmapped id as
    group A.
    """
    entry = catalogue_entry(rule_id, catalogue_version)
    if catalogue_version == 2:
        return entry
    mapped = CATALOGUE_MAPPING.get(rule_id)
    if mapped:
        return catalogue_entry(mapped, 2)
    return next((candidate for candidate in RULE_CATALOGUE_V2
                 if rule_id in (candidate.get('question_group_aliases') or {})), None)


def question_group_for_effective_kind(kind, *, severity='mandatory', scope='group'):
    """Resolve a legacy primitive copy through v2 slot metadata.

    Historical primitive policies do not carry a catalogue id. Their slot kind
    still has one canonical v2 owner, so the explainer can classify those copies
    without a second frontend or report mapping.
    """
    if not kind:
        return None
    candidates = [entry for entry in RULE_CATALOGUE_V2 if kind in entry_kinds(entry)]
    if not candidates:
        return None
    entry = candidates[0]
    by_level = entry.get('question_group_by_slot_level') or {}
    level_group = (by_level.get(kind) or {}).get('advisory' if severity != 'mandatory' else 'mandatory')
    if level_group:
        return level_group
    by_slot = entry.get('question_group_by_slot') or {}
    if kind in by_slot:
        return by_slot[kind]
    by_scope = entry.get('question_group_by_scope') or {}
    if by_scope:
        return by_scope.get(scope, entry.get('question_group'))
    return entry.get('question_group')


def workload_fields(rule, line):
    """Typed editor fields derived from the same primitive settings used by slot selection."""
    entry = catalogue_entry(rule['id'], rule.get('catalogue_version', 1))
    if not entry or rule['id'] != 'workload_limit':
        return {}
    keys = list(line.get('numbers') or {})
    kinds = [slot_for_number(entry, key, rule, line, keys)['kind'] for key in keys]
    if 'restriction' in kinds:
        return {'mode': 'no_duties', 'measure': None, 'period': None}
    if not kinds:
        return {}
    kind = kinds[0]
    settings = {**(rule.get('settings') or {}).get(kind, {}), **(line.get('settings') or {}).get(kind, {})}
    if kind in ('max_duties', 'max_hours'):
        window = settings.get('window', 'week')
        return {'mode': 'count', 'measure': 'duties' if kind == 'max_duties' else 'scheduled_hours',
                'period': {'kind': 'calendar_week' if window == 'week' else window,
                           **({'days': settings.get('window_days', 7)} if window == 'rolling' else {})}}
    alignment = settings.get('window_alignment', 'rolling')
    period = {'kind': alignment if alignment in ('calendar_week', 'calendar_month') or settings.get('window_days') else 'block',
              **({'repeat': settings.get('repeat', False)} if 'repeat' in settings or
                 not line.get('source_bindings') and (settings.get('ends_on') or line.get('ends_on')) else {}),
              **({'days': settings['window_days']} if settings.get('window_days') else {}),
              **({'anchor_on': settings['window_anchor_on']} if settings.get('window_anchor_on') else {}),
              **({'prorate_partial_windows': True} if settings.get('prorate_partial_windows') else {})}
    for field in ('starts_on', 'ends_on'):
        if field in settings:
            period[field] = settings[field]
        elif field == 'ends_on' and settings.get('repeat'):
            period[field] = None
        elif line.get(field):
            period[field] = line[field]
    return {'mode': 'count', 'measure': settings.get('metric'), 'period': period}


def workload_settings(measure, period, settings, keys, bindings=None):
    """Translate typed write fields to canonical settings; conflicting dual specifications are refused."""
    out = deepcopy(settings or {})
    keyset = set(keys)
    if 'apply' in keyset:
        raise ValueError('No-duties restrictions have no measure or quota period')
    bound = {b['primitive_kind'] for b in (bindings or {}).values()}
    if bound:
        kinds = bound
    else:
        kind = ('block_quota' if 'block_quota' in out or 'min' in keyset or 'target' in keyset
                or period['kind'] not in ('day', 'calendar_week', 'rolling') or (period.get('days') or 0) > 31 else
                'max_duties' if measure == 'duties' else 'max_hours' if measure == 'scheduled_hours' else 'block_quota')
        kinds = {kind}
        # An editor changes the count fields; compatible options follow it, never a second operative count kind.
        for previous in ('max_duties', 'max_hours', 'block_quota'):
            if previous == kind or previous not in out:
                continue
            values = out.pop(previous)
            if set(values) & {'metric', 'window', 'window_days', 'window_alignment', 'window_anchor_on',
                              'prorate_partial_windows', 'starts_on', 'ends_on'}:
                raise ValueError('Measure/period conflict with the canonical workload settings')
            chosen = out.setdefault(kind, {})
            for name, value in values.items():
                if name == 'group_by':
                    if value != _MODELS[previous].model_fields[name].default:
                        raise ValueError(f'{kind} cannot preserve {previous} {name}; change that option first')
                    continue
                if name == 'include_overtime' and kind == 'block_quota':
                    from .schemas import _validate_primitive_value
                    name, value = 'overtime', 'include' if _validate_primitive_value(previous, name, value) else 'exclude'
                if name not in _MODELS[kind].model_fields:
                    raise ValueError(f'{kind} cannot preserve {previous} {name}; change that option first')
                if name in chosen and chosen[name] != value:
                    raise ValueError('Measure/period conflict with the canonical workload settings')
                chosen[name] = value
    for kind in sorted(kinds):
        if kind in ('max_duties', 'max_hours'):
            wanted = 'duties' if kind == 'max_duties' else 'scheduled_hours'
            if measure != wanted or period['kind'] not in ('day', 'calendar_week', 'rolling'):
                raise ValueError(f'{kind} cannot represent the selected measure and period')
            translated = {'window': 'week' if period['kind'] == 'calendar_week' else period['kind']}
            if period.get('days') is not None:
                translated['window_days'] = period['days']
            if (period.get('starts_on') or period.get('ends_on')) and 'block_quota' not in kinds:
                raise ValueError('Limit effective dates belong to the line, not a quota period')
        elif kind == 'block_quota':
            alignment = period['kind']
            translated = {'metric': measure, 'window_alignment': 'rolling' if alignment in ('day', 'block') else alignment}
            if alignment == 'day' or period.get('days') is not None:
                translated['window_days'] = 1 if alignment == 'day' else period['days']
            if 'anchor_on' in period:
                translated['window_anchor_on'] = period['anchor_on']
            if 'prorate_partial_windows' in period:
                translated['prorate_partial_windows'] = period['prorate_partial_windows']
            if 'repeat' in period:
                translated['repeat'] = period['repeat']
            elif not bindings and period.get('ends_on'):
                translated['repeat'] = out.get(kind, {}).get('repeat', False)
            translated.update({f: period[f] for f in ('starts_on', 'ends_on') if f in period})
        else:
            raise ValueError(f'{kind} is not a workload count')
        chosen = out.setdefault(kind, {})
        if any(f in chosen and chosen[f] != v for f, v in translated.items()):
            raise ValueError('Measure/period conflict with the canonical workload settings')
        chosen.update(translated)
    return out
