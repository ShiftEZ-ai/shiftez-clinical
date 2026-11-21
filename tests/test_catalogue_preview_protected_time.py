"""A configuration preview sees protected time that the proposed change itself creates."""
import pytest

from apps.api import models as m
from apps.api.db import transaction
from .test_catalogue_preview import preview
from .test_contact_preview_perf import change, person
from .test_event_calendar import event_row


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_person_tag_preview_shows_new_blocking_event_restriction(h):
    with transaction(h.app.state.engine, write=True) as db:
        tag = m.Tag(department_id=h.seed['department_id'], name='Teaching attendees', labels=['people'])
        db.add(tag)
        db.flush()
        tag_id = tag.id
    # The blocking event's audience is the tag, which has no members yet.
    event_row(h, settings={'blocks_duties': True}, audience={'where': {
        'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': [tag_id]}]}})
    h.shift(h.workspace())
    row = person(h)
    result = preview(h, change(row, tags=[*row.get('tags', []), 'Teaching attendees']))
    [impact] = result['impacts']
    assert 'RESTRICTION' in {f['code'] for f in impact['introduced']}
    assert (impact['before_error_count'], impact['after_error_count']) == (0, 1)
