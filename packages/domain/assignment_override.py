"""Shared classification for reasoned manual assignment overrides."""

from .fairness import AWAY
from .time import aware, overlaps

REASON_ALLOWED_AVAILABILITY = AWAY | {'protected_time'}
ZERO_LIMIT_FINDING_CODES = frozenset({'QUOTA_ABOVE_MAXIMUM', 'MAX_DUTIES', 'MAX_HOURS'})


def _mandatory_zero_limit(finding):
    # Read the limit the validator applied (the resolved catalogue line, prorated), not the
    # policy's top-level definition: per-person/rank lines carry their own numbers.
    detail = finding.get('detail') or {}
    limit = detail.get('amount') if finding.get('code') == 'QUOTA_ABOVE_MAXIMUM' else detail.get('maximum')
    return finding.get('severity') == 'error' and limit is not None and limit == 0


def assignment_reason_allowed(data, finding):
    """Whether a typed reason may admit this exact fixed-list manual blocker."""
    code = finding.get('code')
    person_id = finding.get('person_id')
    start, end = finding.get('start'), finding.get('end')
    if not person_id or not start or not end:
        return False
    start, end = aware(start), aware(end)
    if code in ZERO_LIMIT_FINDING_CODES:
        return _mandatory_zero_limit(finding)
    if code == 'EXTERNAL_ROTATION':
        from .validation import rotation_blocks  # the validator's own away-rotation test (organisation timezone)
        return rotation_blocks(data, person_id, start, end)
    if code != 'UNAVAILABLE':
        return False
    # Every overlapping record must be admissible (an external commitment beside protected time stays a
    # blocker), whichever record raised the finding; a named record must be one of them and admissible too.
    rows = [row for row in data.get('availability', []) if row.get('person_id') == person_id
            and overlaps(start, end, aware(row['start']), aware(row['end']))]
    named = finding.get('availability_id')
    return (bool(rows) and all(row.get('kind') in REASON_ALLOWED_AVAILABILITY for row in rows)
            and (named is None or any(row.get('id') == named for row in rows)))


def assignment_override_allowed(data, finding, *, change_list=False):
    """Whether this workspace's assign command may admit the finding with a reason."""
    return not change_list and assignment_reason_allowed(data, finding)
