"""Coverage-place lower bounds, shared by interval, session and rota projections."""

from .time import aware


COVERAGE_CONTRACT = 'cover-places-v2'

COVERAGE_PLACE_CODES = frozenset({'COVERAGE_GAP', 'SCOPED_COVERAGE_GAP', 'CAPABILITY_GAP',
                                'DISTINCT_PERSON_GAP', 'SUPERVISION_GAP', 'SUPERVISION_LEVEL', 'OPEN_EXTRA_COVER'})


def coverage_places(row, requirements=()):
    """Strongest minimum and shortage; overlapping requirements can use the same person.

    These are coverage bounds, not a claim about the minimum number of staffing edits.
    Take peaks across intervals in rota_summary, never sum interval or rule deficits.
    """
    if row['status'] in {'cancelled', 'closed'}:
        return dict(places_needed=0, places_filled=0, places_open=0)
    required = row.get('required', int(row['status'] == 'gap'))
    needed = max(required, row.get('places_needed', 0))
    missing = max(0, required - row.get('available_eligible', 0), row.get('places_open', 0))
    for detail in requirements:
        if detail.get('required') is None or detail.get('available') is None:
            continue
        needed = max(needed, detail['required'])
        missing = max(missing, detail['required'] - detail['available'])
    return dict(places_needed=needed, places_filled=needed - missing, places_open=missing)


def coverage_rows(check):
    """Enrich older retained checks without mutating evidence or inventing missing requirements."""
    from .validation import finding_observations
    by_shift = {}
    for finding in finding_observations(check):
        if finding['code'] in COVERAGE_PLACE_CODES and (finding['severity'] == 'error'
                                                       or finding['code'] == 'OPEN_EXTRA_COVER'):
            by_shift.setdefault(finding.get('shift_id'), []).append(finding)
    result = []
    for row in check.get('coverage', []):
        requirements = []
        for finding in by_shift.get(row['shift_id'], ()):
            if all(x.get('start') and x.get('end') for x in (row, finding)) and not (
                    aware(row['start']) < aware(finding['end']) and aware(finding['start']) < aware(row['end'])):
                continue
            requirements.append(finding.get('detail') or {})
        result.append({**row, **coverage_places(row, requirements)})
    return result


def session_open_places(check, shift_id):
    """Open cover on a session, including a shared pool the session can contribute to."""
    from .validation import finding_observations
    direct = [row['places_open'] for row in coverage_rows(check) if row['shift_id'] == shift_id]
    pooled = [f.get('detail') or {} for f in finding_observations(check)
              if f['code'] == 'SCOPED_COVERAGE_GAP' and f['severity'] == 'error'
              and shift_id in (f.get('detail') or {}).get('affected_shift_ids', [])]
    shared = coverage_places({'status': 'covered', 'required': 0, 'available_eligible': 0}, pooled)
    return max([shared['places_open'], *direct])
