"""A "Can't make it" reason reads as the server disclosed it (apps/web/src/published_changes_api.ts `absenceWords`, used
by Requests, its panel, the published-changes feed and Home): the contract's category as its word, any other text as
sent, the note after it, and nothing left over (no lone "·") when the department's leave_reason_display sent less."""
import pytest

from .test_client_impact_and_drag import run_with_listeners


@pytest.mark.parametrize(('reason', 'note', 'shown'), [
    (None, None, ''),                                         # none: nothing sent
    ('', '', ''),                                             # empty strings
    ('  ', ' ', ''),
    ('sick', None, 'Sick'),                                   # type: the category only
    ('emergency', '', 'Emergency'),
    ('other', None, 'Other'),                                 # type: a legacy free reason reads Other
    ('SICK', None, 'Sick'),                                   # a category in any case reads as itself
    ('sick', 'Fictional fever', 'Sick · Fictional fever'),    # details / the reporter: category + note
    ('Fictional family matter', 'Seen at noon', 'Fictional family matter · Seen at noon'),  # details: a legacy reason as sent
    (None, 'Only a note', 'Only a note'),
    ('constructor', None, 'constructor'),                     # not a category: never a prototype member
])
def test_absence_words(client_modules, reason, note, shown):
    assert run_with_listeners(client_modules, 'published_changes_api.js', 'm.absenceWords(input.reason,input.note)',
                              {'reason': reason, 'note': note}) == shown


def test_every_contract_category_has_its_word(client_modules):
    from packages.contracts.schemas import ABSENCE_CATEGORIES, ABSENCE_CATEGORY_LABELS
    shown = run_with_listeners(client_modules, 'published_changes_api.js',
                               'input.map(c=>m.absenceWords(c))', list(ABSENCE_CATEGORIES))
    assert shown == [ABSENCE_CATEGORY_LABELS[c] for c in ABSENCE_CATEGORIES]
