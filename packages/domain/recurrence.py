from datetime import date, timedelta
from packages.contracts.schemas import SeriesDefinition
from packages.domain.time import aware, local_stamp, local_midnight, union_hours, duration_hours, overlaps

SERIES_STATE_FIELDS = ('closed', 'closure_reason', 'usual_assignee_unavailability')


def stored_series_definition(definition):
    # Missing flags in stored series predate the fixed-assignee default for new series.
    return SeriesDefinition.model_validate({'usual_assignees_fixed': False, **definition})


def series_definition(shift):
    return (shift.get('series_snapshot') or {}).get('definition') or {}


def usual_assignee_ids(shift):
    return frozenset(series_definition(shift).get('default_person_ids', []))


def inactive(shift):
    return shift.get('cancelled', False) or shift.get('closed', False)


def usual_assignee_fixed(shift, person_id):
    return series_definition(shift).get('usual_assignees_fixed', False) and person_id in usual_assignee_ids(shift)


def series_candidate_allowed(data, shift, person_id):
    cache = data.get('_series_candidate_cache')
    key = (shift['id'], person_id)
    if cache is not None and key in cache:
        return cache[key]
    if not series_definition(shift).get('usual_assignees_fixed', False) or person_id in usual_assignee_ids(shift):
        result = True
    else:
        from .validation import participation_for
        result = participation_for(data, person_id, shift, aware(shift['start']), aware(shift['end'])) == 'trainee'
    if cache is not None:
        cache[key] = result
    return result


def usual_assignee_unavailability(data, shift):
    """Recorded absence of configured usual people, independent of closure and assignment choices."""
    from .validation import person_rows
    usual = usual_assignee_ids(shift)
    a, b = aware(shift['start']), aware(shift['end'])
    zone = data['organisation']['timezone']
    absent = []
    for pid in sorted(usual):
        intervals, codes = [], set()
        for row in person_rows(data, 'availability', pid):
            lo, hi = aware(row['start']), aware(row['end'])
            if overlaps(a, b, lo, hi):
                intervals.append((max(a, lo), min(b, hi)))
                codes.add('UNAVAILABLE')
        for row in person_rows(data, 'rotations', pid):
            if row.get('department_available', True):
                continue
            lo = local_midnight(date.fromisoformat(row['starts_on']), zone)
            hi = local_midnight(date.fromisoformat(row['ends_on']), zone) if row.get('ends_on') else b
            if overlaps(a, b, lo, hi):
                intervals.append((max(a, lo), min(b, hi)))
                codes.add('EXTERNAL_ROTATION')
        if intervals:
            absent.append({'person_id': pid, 'codes': sorted(codes),
                           'fully_unavailable': union_hours(intervals) >= duration_hours(a, b) - 1e-9})
    return absent


def series_state(data, shift):
    definition = series_definition(shift)
    usual = usual_assignee_ids(shift)
    if not usual or not (definition.get('usual_assignees_fixed') or definition.get('close_when_all_usual_unavailable')):
        return {'closed': False, 'closure_reason': None, 'usual_assignee_unavailability': []}
    absent = usual_assignee_unavailability(data, shift)
    # shortcut: `close_when_all_usual_unavailable` remains in versioned definitions for
    # compatibility, but an absence never removes a required place. Closing is an
    # explicit coordinator action (`shift.action`/`publication.change`), so this
    # derived state is informational only and must stay open.
    return {'closed': False, 'closure_reason': None, 'usual_assignee_unavailability': absent}


def with_series_state(data, *, cache=False):
    from .validation import PERSON_ROWS, index_person_rows
    def dynamic(shift):
        definition = series_definition(shift)
        return not shift.get('series_state_frozen') and (definition.get('usual_assignees_fixed')
                or definition.get('close_when_all_usual_unavailable')
                or shift.get('usual_assignee_unavailability') or shift.get('closure_reason') == 'usual_assignees_unavailable')
    previous = data.get('_series_state_cache')
    reuse = cache or previous is not None
    if previous is None and not any(dynamic(shift) for shift in data['shifts']):
        return data
    # Index rows retain their dictionaries; only membership/rekeying needs a new index, while absence edits need new states.
    row_key = tuple(tuple((id(row), row['person_id']) for row in data.get(name, ())) for name in PERSON_ROWS) if reuse else None
    absence_key = (data['organisation']['timezone'],
                   tuple((row['person_id'], row['start'], row['end']) for row in data.get('availability', ())),
                   tuple((row['person_id'], row.get('department_available', True), row['starts_on'], row.get('ends_on'))
                         for row in data.get('rotations', ()))) if reuse else None
    indexed = (previous['person_rows'] if previous is not None and previous['row_key'] == row_key
               else index_person_rows(data))
    states = (previous['states'] if previous is not None and previous['absence_key'] == absence_key else {})
    context = {**data, '_person_rows': indexed}
    additions, shifts = {}, []
    for shift in data['shifts']:
        if not dynamic(shift):
            shifts.append(shift)
            continue
        definition = series_definition(shift)
        key = (shift['start'], shift['end'], shift.get('cancelled', False), tuple(definition.get('default_person_ids', ())),
               bool(definition.get('usual_assignees_fixed')), bool(definition.get('close_when_all_usual_unavailable')))
        state = states.get(key)
        if state is None:
            if key not in additions:
                additions[key] = series_state(context, shift)
            state = additions[key]
        shifts.append({**shift, **state} if any(shift.get(field) != state[field] for field in SERIES_STATE_FIELDS) else shift)
    result = {**data, 'shifts': shifts}
    if not reuse:
        return result
    return {**result, '_person_rows': indexed, '_series_state_cache': {
        'row_key': row_key, 'absence_key': absence_key, 'person_rows': indexed,
        'states': {**states, **additions} if additions else states}}

MAX_PREVIEW_DAYS = 366

def expand(definition: SeriesDefinition, starts_on: date, ends_on: date | None) -> list[dict]:
    # An ongoing template has a bounded one-year preview; drafts pass their own end.
    ends_on = ends_on or starts_on + timedelta(days=MAX_PREVIEW_DAYS)
    if not 0 < (ends_on - starts_on).days <= MAX_PREVIEW_DAYS:
        raise ValueError(f"Recurrence preview is bounded to {MAX_PREVIEW_DAYS} days")
    by_day = {d.weekday: d for d in definition.days}
    excluded = set(definition.excluded_dates)
    results = []
    for offset in range((ends_on - starts_on).days):
        day = starts_on + timedelta(days=offset)
        timing = by_day.get(day.weekday())
        # Anchor is explicit, not calculated from today or the requested viewport.
        week = (day - definition.anchor).days // 7
        if timing is None or day in excluded or week % definition.interval_weeks:
            continue
        occurrence = definition.month_occurrence
        if occurrence == -1 and (day + timedelta(days=7)).month == day.month:
            continue
        if occurrence is not None and occurrence != -1 and (day.day - 1) // 7 + 1 != occurrence:
            continue
        start = local_stamp(day, timing.start_time, definition.timezone)
        end_day = day + timedelta(days=1) if timing.end_time != "24:00" and timing.end_time < timing.start_time else day
        end = local_stamp(end_day, timing.end_time, definition.timezone)
        results.append({"start": start, "end": end})
    return results
