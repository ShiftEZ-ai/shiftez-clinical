"""SW3-D-22: an audit entry says whose record changed, search finds what is shown, and the CSV carries the same words."""
import csv
import io
from urllib.parse import quote

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.audit import audit_summary, summary_actions


def _rank(h):
    with transaction(h.app.state.engine) as db:
        person = db.get(m.Person, h.seed['people'][0]).display_name
        rank = db.get(m.Category, h.seed['trainee_category']).name
    row = h.command('coordinator', 'membership.create', {'person_id': h.seed['people'][0],
                    'category_id': h.seed['trainee_category'], 'starts_on': '2028-07-01'})['record']
    return row, person, rank


def events(h, q):
    return h.get('auditor', '/api/audit?people=false&q=' + quote(q))['events']


def test_summary_is_one_sentence_from_the_catalogue():
    assert audit_summary('membership.create') == 'added a rank'
    assert audit_summary('membership.create', 3) == 'added 3 ranks'
    assert audit_summary('eligibility.end', subject='Dr A: Clinic') == 'ended an eligibility: Dr A: Clinic'
    assert {'membership.create', 'membership.end'} <= set(summary_actions('rank'))


def test_rank_entry_names_its_person_and_rank_and_is_found_by_them(h):
    row, person, rank = _rank(h)
    expected = f'added a rank: {person}: {rank} from 2028-07-01'
    entry = next(e for e in events(h, '') if e['action'] == 'membership.create' and e['summary'] == expected)
    for text in ('added a rank', person.split()[-1], rank):
        assert any(e['id'] == entry['id'] for e in events(h, text)), text


def test_withdrawn_record_keeps_its_subject_and_the_csv_matches_the_screen(h):
    row, person, rank = _rank(h)
    h.command('coordinator', 'membership.withdraw', {'reason': 'Recorded for the wrong year'}, row['id'], row['revision'])
    shown = {e['action']: e['summary'] for e in events(h, person) if e['action'].startswith('membership.')}
    assert shown['membership.withdraw'] == f'withdrew a rank: {person}: {rank} from 2028-07-01'
    response = h.client('auditor').get('/api/audit.csv?q=' + quote(person))
    assert response.status_code == 200, response.text
    rows = [r for r in csv.DictReader(io.StringIO(response.text)) if r.get('action', '').startswith('membership.')]
    assert {r['action']: r['what_changed'] for r in rows} == shown


def test_a_removed_persons_history_no_longer_names_them(h):
    row, person, rank = _rank(h)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, h.seed['people'][0]).display_name = 'Former staff member 1'
    summaries = [e['summary'] for e in events(h, '') if e['action'] == 'membership.create']
    assert f'added a rank: Former staff member 1: {rank} from 2028-07-01' in summaries
    assert not [text for text in summaries if person in text]
