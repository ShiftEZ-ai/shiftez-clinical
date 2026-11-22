"""Decline receipts cover standing occurrences and observed period shapes without changing their meaning."""
import copy
from uuid import uuid4

import pytest

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import DeclinedWish, JobResult
from packages.domain.patterns import wish_outcomes
from packages.domain.validation import evaluate
from packages.domain.wish_reasons import declined_wishes
from tests.test_pattern_wishes import CASES, month, pattern_wish
from tests.test_staff_wishes import wish
from tests.test_standing_wishes import standing


@pytest.mark.parametrize('kind,limit,broken,compliant', CASES)
def test_pattern_receipt_describes_actual_shape_without_dated_trials(h, monkeypatch, kind, limit, broken, compliant):
    data, person = month(h, broken)
    saved = pattern_wish(person, kind, limit, id=str(uuid4()))
    data['wishes'] = [saved]
    validation = evaluate(data)
    monkeypatch.setattr('packages.domain.validation.evaluate',
                        lambda *_a, **_k: pytest.fail('A period shape has no single dated placement trial'))
    [receipt] = declined_wishes(data, validation)
    assert receipt['wish_id'] == saved['id'] and receipt['honoured'] == wish_outcomes(data)[saved['id']][0] == 0
    assert receipt['reasons'] and all(reason['kind'] == 'tradeoff' and reason['code'] for reason in receipt['reasons'])
    assert all('this pattern was not honoured' in reason['message'] for reason in receipt['reasons'])
    assert all('shift_id' not in reason and 'rule_id' not in reason and 'competing_wishes' not in reason
               for reason in receipt['reasons'])
    assert DeclinedWish.model_validate(receipt)
    template = next(shift['participants'] for shift in data['shifts'] if shift['participants'])
    for shift in data['shifts']:
        shift['participants'] = [{**participant, 'start': shift['start'], 'end': shift['end']}
                                 for participant in template] if int(shift['start'][8:10]) in compliant else []
    assert wish_outcomes(data)[saved['id']][0] == 1
    assert declined_wishes(data, validation) == []


def test_honoured_patterns_do_not_displace_dated_competitors_from_the_shortlist(h):
    person, other = h.seed['people'][:2]
    lower = wish(h, '2026-10-04', kind='prefer_on')['wish']
    higher = wish(h, '2026-10-04', kind='prefer_on', points=4, person=other, role='coordinator')['wish']
    typ = h.configure('assignment_types', {'name': 'One competing place', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']['id']
    workspace = h.shift(h.workspace(), people=[other], typ=typ)['workspace']
    data = h.snapshot(workspace)
    for kind, limit in [('max_days_in_row', 20), ('min_days_off_together', 1),
                        ('max_weekends_in_row', 20), ('whole_weekends', None)]:
        data['wishes'].append(pattern_wish(other, kind, limit, id=str(uuid4()), weight=100,
                                          first_day='2026-10-04', last_day='2026-10-04'))
    receipt = next(row for row in declined_wishes(data, evaluate(data)) if row['wish_id'] == lower['id'])
    assert receipt['reasons'][0]['kind'] == 'competing_wishes'
    assert receipt['reasons'][0]['competing_wishes'][0]['wish_id'] == higher['id']


def test_published_standing_reasons_belong_to_frozen_occurrences_only(h):
    person = h.seed['people'][0]
    parent = standing(h, weekdays=[6, 1], points=2)['wish']
    workspace = h.shift(h.workspace(), people=[person])['workspace']
    data = h.snapshot(workspace)
    [receipt] = declined_wishes(data, evaluate(data))
    assert receipt['wish_id'].startswith('standing:') and receipt['wish_id'] != parent['id']
    assert JobResult.model_validate({'status': 'candidate_feasible', 'declined_wishes': [receipt]})
    publication_id = h.publish(workspace)['publication_id']
    with transaction(h.app.state.engine) as db:
        publication = db.get(m.Publication, publication_id)
        original = copy.deepcopy(publication.snapshot)
        frozen = {row['first_day']: row for row in original['wishes']}
        assert frozen['2026-10-04']['outcome'] == 'declined'
        assert frozen['2026-10-04']['outcome_reasons'] == receipt['reasons']
        assert frozen['2026-10-06']['outcome'] == 'granted'
        assert frozen['2026-10-06']['outcome_reasons'] == []
        row = db.get(m.StaffWish, parent['id'])
        assert row.outcome is row.outcome_reasons is row.publication_id is None
    h.command('staff', 'wish.withdraw', {'reason': 'Remove future defaults'}, parent['id'], parent['revision'])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Publication, publication_id).snapshot == original


def test_empty_applied_receipt_survives_submit_approve_and_publish(h, monkeypatch):
    wish(h, '2026-10-04', kind='prefer_on')
    workspace = h.shift(h.workspace())['workspace']
    monkeypatch.setattr('apps.api.services.wishes._applied_reasons', lambda *_a: [])
    submitted = h.lifecycle(workspace, 'submit')['workspace']
    approved = h.lifecycle(submitted, 'approve', 'approver')['workspace']
    assert submitted['validation']['wish_receipt'] == approved['validation']['wish_receipt'] == []
    monkeypatch.setattr('packages.domain.wish_reasons.declined_wishes',
                        lambda *_a, **_k: pytest.fail('An empty immutable receipt is valid evidence'))
    publication_id = h.lifecycle(approved, 'publish')['publication_id']
    with transaction(h.app.state.engine) as db:
        publication = db.get(m.Publication, publication_id)
        assert publication.validation['wish_receipt'] == []
        assert publication.snapshot['wishes'][0]['outcome_reasons'] == []


@pytest.mark.parametrize('invalidate', ['reopen', 'inputs'])
def test_applied_receipt_is_not_carried_after_reopen_or_input_change(h, monkeypatch, invalidate):
    wish(h, '2026-10-04', kind='prefer_on')
    workspace = h.shift(h.workspace())['workspace']
    monkeypatch.setattr('apps.api.services.wishes._applied_reasons', lambda *_a: [])
    submitted = h.lifecycle(workspace, 'submit')['workspace']
    assert submitted['validation']['wish_receipt'] == []
    if invalidate == 'reopen':
        result = h.lifecycle(submitted, 'reopen')['workspace']
        assert result.get('validation') is None
        with transaction(h.app.state.engine) as db:
            assert db.get(m.Workspace, workspace['id']).validation is None
    else:
        with transaction(h.app.state.engine) as db:
            location = db.get(m.Location, h.seed['locations'][0])
            identity, revision = location.id, location.revision
            value = {'name': 'Synthetic changed check input', 'location_type_id': location.location_type_id,
                     'starts_on': location.starts_on.isoformat()}
        h.configure('locations', value, identity, revision)
        result = h.lifecycle(submitted, 'validate')['workspace']
        assert result['validation']['fingerprint'] != submitted['validation']['fingerprint']
        assert 'wish_receipt' not in result['validation']


def test_replacement_recomputes_pattern_breaches_and_keeps_original_reasons(h):
    person = h.seed['people'][0]
    saved = wish(h, '2026-10-01', last='2026-10-31', kind='max_days_in_row', limit=1)['wish']
    workspace = h.shift(h.workspace(), people=[person])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00', people=[person])['workspace']
    publication_id = h.publish(workspace)['publication_id']
    original = h.get('coordinator', '/api/publications/' + publication_id)['snapshot']
    assert original['wishes'][0]['outcome'] == 'declined' and original['wishes'][0]['outcome_reasons']
    with transaction(h.app.state.engine) as db:
        stored_original = copy.deepcopy(db.get(m.Publication, publication_id).snapshot)
    replacement = h.command('coordinator', 'workspace.create', {'name': 'Replace after one work day',
        'starts_on': '2026-10-05', 'ends_on': '2026-10-11', 'replaces_publication_id': publication_id})['workspace']
    replacement = h.shift(replacement, start='2026-10-05T08:00:00+03:00',
                          end='2026-10-05T12:00:00+03:00', people=[person])['workspace']
    h.publish(replacement)
    prior = h.get('coordinator', '/api/publications/' + publication_id)['snapshot']
    [trimmed] = prior['wishes']
    assert trimmed['id'] == saved['id'] and trimmed['outcome'] == 'granted' and trimmed['outcome_reasons'] == []
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Publication, publication_id).snapshot == stored_original
