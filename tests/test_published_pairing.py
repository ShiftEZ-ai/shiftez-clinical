"""Published amendments and staff swaps retain session pairing and recheck the resulting safety facts."""
from types import SimpleNamespace

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import swaps
from .test_published_changes import change

pytestmark = pytest.mark.postgres


@pytest.fixture
def h(pg):
    return pg


def paired_publication(h, move=None):
    trainee, supervisor, replacement = h.seed['people'][:3]
    typ = h.configure('assignment_types', {'name': 'Named supervision', 'starts_on': '2020-01-01',
        'definition': {'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
            'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1, 'require_pairing': True}}})['record']
    def participants(day, lead, learner):
        start, end = f'2026-10-{day}T08:00:00+03:00', f'2026-10-{day}T12:00:00+03:00'
        return start, end, [
            {'person_id': learner, 'start': start, 'end': end, 'participation': 'trainee', 'supervisor_person_id': lead},
            {'person_id': lead, 'start': start, 'end': end, 'participation': 'supervisor'}]
    start, end, people = participants('04', supervisor, trainee)
    w = h.shift(h.workspace(), start, end, typ=typ['id'], people=people)['workspace']
    start, end, people = participants('05', replacement, trainee)
    if move:
        people = [people[1]]
        if move == 'move_with_supervisor':
            people[0]['person_id'] = supervisor
    w = h.shift(w, start, end, typ=typ['id'], people=people)['workspace']
    pub = h.publish(w)['publication_id']
    return pub, h.get('coordinator', '/api/publications/' + pub)['snapshot']['shifts']


def assignment(shift, participation):
    return next(p for p in shift['participants'] if p['participation'] == participation)


@pytest.mark.parametrize('kind', ['replace_supervisor', 'replace_trainee', 'swap_supervisors', 'move_trainee', 'move_with_supervisor'])
def test_published_pairing_survives_changes(h, kind):
    pub, shifts = paired_publication(h, move=kind if kind.startswith('move') else None)
    source, target = shifts
    supervisor = assignment(source, 'supervisor')
    trainee = assignment(source, 'trainee')
    replacement = assignment(target, 'supervisor')
    values = {'assignment_id': supervisor['id'], 'person_id': replacement['person_id']}
    if kind == 'replace_trainee':
        values.update(assignment_id=trainee['id'])
    elif kind == 'swap_supervisors':
        values.update(kind='swap', target_shift_id=target['id'], target_assignment_id=replacement['id'], person_id=None)
    elif kind.startswith('move'):
        values.update(kind='move', assignment_id=trainee['id'], target_shift_id=target['id'], person_id=None)
    result = change(h, pub, source, **values)
    rows = h.detail(result['workspace'])['shifts']
    first = next(sh for sh in rows if sh['start'] == source['start'])
    if kind.startswith('move'):
        moved = next(sh for sh in rows if sh['start'] == target['start'])
        assert any(p['supervisor_person_id'] == supervisor['person_id'] for p in moved['participants'])
        if kind == 'move_trainee':
            assert not result['validation']['valid']
            assert 'SUPERVISION_PAIRING' in {f['code'] for f in result['validation']['findings']}
        else:
            assert result['validation']['valid']
    else:
        expected = supervisor['person_id'] if kind == 'replace_trainee' else replacement['person_id']
        assert assignment(first, 'trainee')['supervisor_person_id'] == expected
        assert result['validation']['valid']
        if kind == 'swap_supervisors':
            second = next(sh for sh in rows if sh['start'] == target['start'])
            assert assignment(second, 'trainee')['supervisor_person_id'] == supervisor['person_id']
    assert h.get('coordinator', '/api/publications/' + pub)['snapshot']['shifts'] == shifts


def test_ineligible_supervisor_is_flagged_and_cannot_submit(h):
    pub, shifts = paired_publication(h)
    person = h.configure('people', {'display_name': 'Unqualified replacement'})['record']
    result = change(h, pub, shifts[0], assignment_id=assignment(shifts[0], 'supervisor')['id'], person_id=person['id'])
    assert 'SUPERVISION_PAIRING' in {f['code'] for f in result['validation']['findings']}
    assert result['change']['must_fix']
    h.command('coordinator', 'publication.changes.submit', {'reason': 'Attempt unsafe pairing'},
              target=pub, revision=result['workspace']['revision'], status=422)


def test_staff_swap_preview_and_prepared_content_share_pairing(h):
    pub, shifts = paired_publication(h)
    one, two = [assignment(sh, 'supervisor') for sh in shifts]
    with transaction(h.app.state.engine) as db:
        publication = db.get(m.Publication, pub)
        assert swaps.Eligibility(db, publication).pair(one['id'], two['id'])['eligible']
        expected = swaps.expected_content(db, publication, SimpleNamespace(
            first_assignment_id=one['id'], second_assignment_id=two['id'],
            recipient_person_id=two['person_id'], proposer_person_id=one['person_id'], kind='exchange'))
    w = h.workspace(base=pub)
    rows = h.detail(w)['shifts']
    result = h.command('coordinator', 'draft.swap', {'workspace_id': w['id'],
        'first_assignment_id': assignment(rows[0], 'supervisor')['id'],
        'second_assignment_id': assignment(rows[1], 'supervisor')['id'], 'reason': 'Swap qualified supervisors'}, revision=w['revision'])
    assert result['validation']['valid']
    assert swaps.semantic_content(h.detail(result['workspace'])['shifts']) == expected


def test_replacement_enforces_configured_supervisor_capacity(h):
    pub, shifts = paired_publication(h)
    replacement = assignment(shifts[1], 'supervisor')['person_id']
    h.policy({'kind': 'supervision_limit', 'mode': 'capacity', 'maximum': 0}, {'person_id': replacement})
    result = change(h, pub, shifts[0], assignment_id=assignment(shifts[0], 'supervisor')['id'], person_id=replacement)
    assert 'SUPERVISOR_SCOPED_CAPACITY' in {f['code'] for f in result['validation']['findings']}
    h.command('coordinator', 'publication.changes.submit', {'reason': 'Attempt excess supervision'},
              target=pub, revision=result['workspace']['revision'], status=422)
