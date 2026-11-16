"""Initial leave catalogue and conservative legacy-label matching (never infer a type from private notes)."""

DEFAULT_LEAVE_TYPES = (
    ('Annual', 'ANNUAL', 'teal'),
    ('Sick', 'SICK', 'coral'),
    ('Study/Conference', 'STUDY', 'blue'),
    ('Emergency', 'EMERG', 'amber'),
    ('Other', 'OTHER', 'slate'),
)

LEGACY_CODES = {
    'annual leave': 'ANNUAL', 'sick leave': 'SICK', 'study': 'STUDY', 'study leave': 'STUDY',
    'conference': 'STUDY', 'conference leave': 'STUDY', 'study/conference leave': 'STUDY',
    'emergency leave': 'EMERG', 'other leave': 'OTHER',
}


def matching_type(text, types):
    """Only a unique exact name/code or known legacy label matches; ambiguity returns None."""
    label = (text or '').strip().casefold()
    matches = [t for t in types if label and label in (t['name'].casefold(), t['short_code'].casefold())]
    if not matches and label in LEGACY_CODES:
        matches = [t for t in types if t['short_code'] == LEGACY_CODES[label]]
    return matches[0] if len(matches) == 1 else None
