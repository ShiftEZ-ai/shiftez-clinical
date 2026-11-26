"""Tags for everything: tag records with members by reference, read by the one condition evaluator."""
import copy
import json
import uuid

import pytest
from sqlalchemy import select, text

from apps.api import models as m
from apps.api.db import make_engine, transaction
from packages.domain.conditions import tag_scope, with_tags
from packages.domain.validation import scope_matches
from .test_rule_conditions import OCT, SHIFT, data, line, tree

TAGS = {'VIP': {'name': 'VIP_Clinics', 'labels': ['services'], 'members': {'services': ['clinic']}},
        'WING': {'name': 'Wings', 'labels': ['locations'], 'members': {'locations': ['wing']}},
        'DUTY': {'name': 'Long days', 'labels': ['assignment_types'], 'members': {'assignment_types': ['T']}},
        'PROG': {'name': 'Trainees', 'labels': ['programmes'], 'members': {'programmes': ['P']}},
        'TEAM': {'name': 'Charge', 'labels': ['people'], 'members': {'people': ['q']}},
        'NONE': {'name': 'Empty', 'labels': ['services', 'people'], 'members': {}}}


def tagged(**changes):
    d = data()
    d['tags'] = copy.deepcopy(TAGS)
    for tag, members in changes.items():
        d['tags'][tag]['members'] = members
    return with_tags(d)


@pytest.mark.parametrize('field,tag,person,hit', [
    ('service_ids', 'VIP', 'p', True),            # the session's clinic is a member
    ('location_ids', 'WING', 'p', True),          # the session's room is inside a member place
    ('assignment_type_ids', 'DUTY', 'p', True),
    ('programme_ids', 'PROG', 'p', True),         # p is in programme P in October
    ('person_ids', 'TEAM', 'q', True),
    ('person_ids', 'TEAM', 'p', False),
    ('tags', 'TEAM', 'q', True),                  # a tags line may name the tag itself
    ('tags', 'TEAM', 'p', False),
    ('service_ids', 'NONE', 'p', False),          # a tag without members matches nobody
])
def test_a_tag_in_a_line_matches_its_members_of_that_kind(field, tag, person, hit):
    d = tagged()
    assert scope_matches(d, tag_scope(d, {'conditions': tree(line(field, tag))}), person, SHIFT, *OCT) is hit
    assert scope_matches(d, tag_scope(d, {'conditions': tree(line(field, tag, op='none'))}), person, SHIFT, *OCT) is not hit


def test_members_are_read_live_and_resolution_is_idempotent():
    rule = {'id': 'r', 'status': 'active', 'definition': {'scope': {'conditions': tree(line('service_ids', 'VIP'))}, 'per_person': [
        {'person_id': 'p', 'service_ids': ['VIP']}]}}
    d = data()
    d['tags'], d['policies'] = copy.deepcopy(TAGS), [rule]
    once = with_tags(d)['policies']
    assert once[0]['definition']['scope']['conditions']['conditions'][0]['values'] == ['VIP', 'clinic']
    assert once[0]['definition']['per_person'][0]['service_ids'] == ['VIP', 'clinic']
    assert with_tags({**d, 'policies': once})['policies'] == once
    assert rule['definition']['scope']['conditions']['conditions'][0]['values'] == ['VIP']  # the stored rule is untouched
    # The clinic leaves the tag: the same rule no longer reaches its sessions.
    later = tagged(VIP={})
    assert not scope_matches(later, tag_scope(later, {'conditions': tree(line('service_ids', 'VIP'))}), 'p', SHIFT, *OCT)


def test_people_hold_their_tags_names_alongside_the_older_free_text():
    d = tagged()
    assert d['people']['q']['tags'] == ['Charge'] and d['people']['p']['tags'] == ['Night']
    assert scope_matches(d, {'conditions': tree(line('tags', 'charge'))}, 'q', SHIFT, *OCT)  # a rule naming it as text


# ---- the server: snapshots, people's tags kept in step, checks --------------------------------------------------

def rows(h, cls, **where):
    with transaction(h.app.state.engine) as db:
        return [{c.name: getattr(r, c.name) for c in cls.__table__.columns} for r in db.scalars(select(cls).filter_by(**where))]


def person(h, n=0):
    return next(p for p in h.get('admin', '/api/catalogues')['people'] if p['id'] == h.seed['people'][n])


def save_tags(h, n, names, status=200):
    p = person(h, n)
    value = {k: p[k] for k in ('display_name', 'active', 'title', 'short_code', 'colour')} | {'tags': names}
    return h.configure('people', value, p['id'], p['revision'], status=status)


def test_saving_a_persons_tags_sets_their_memberships(h):
    save_tags(h, 0, ['Night', 'Teaching'])
    save_tags(h, 1, ['night'])  # the same tag whatever the case
    tags = {t['name']: t for t in rows(h, m.Tag)}
    assert set(tags) == {'Night', 'Teaching'} and tags['Night']['labels'] == ['people']
    members = {(r['tag_id'], r['member_id']) for r in rows(h, m.TagMember, kind='people')}
    assert members == {(tags['Night']['id'], h.seed['people'][0]), (tags['Teaching']['id'], h.seed['people'][0]),
                       (tags['Night']['id'], h.seed['people'][1])}
    save_tags(h, 0, ['Teaching'])
    assert (tags['Night']['id'], h.seed['people'][0]) not in {(r['tag_id'], r['member_id']) for r in rows(h, m.TagMember)}
    assert h.get('admin', '/api/catalogues')['tags'][0]['name'] in {'Night', 'Teaching'}  # listed with the catalogues


def test_tag_catalogue_checks_names_labels_and_renames_people(h):
    save_tags(h, 0, ['Night'])
    [tag] = rows(h, m.Tag)
    value = {'name': 'Teaching', 'labels': ['people']}
    h.configure('tags', value)
    h.configure('tags', {**value, 'name': 'teaching'}, status=409)  # unique whatever the case
    h.configure('tags', {**value, 'labels': ['people', 'people']}, status=422)
    h.configure('tags', {'name': 'Night', 'labels': ['services']}, tag['id'], tag['revision'], status=409)  # has people
    renamed = h.configure('tags', {'name': 'Nights', 'labels': ['people', 'services'], 'colour': 'violet'}, tag['id'], tag['revision'],
                          role='coordinator')
    assert renamed['previous_value']['name'] == 'Night'  # Undo puts it back through the same write
    assert person(h)['tags'] == ['Nights']
    # A rule that names the tag as text keeps the name until it names the tag itself.
    h.policy({'kind': 'min_rest', 'hours': 11}, {'conditions': tree(line('tags', 'nights'))})
    h.configure('tags', {'name': 'Late', 'labels': ['people', 'services']}, tag['id'], tag['revision'] + 1, status=409)


def test_rules_reach_tag_members_in_the_validator_and_follow_membership(h):
    with transaction(h.app.state.engine, write=True) as db:
        tag = m.Tag(department_id=h.seed['department_id'], name='VIP_Clinics', labels=['services'])
        db.add(tag)
        db.flush()
        db.add(m.TagMember(department_id=tag.department_id, tag_id=tag.id, kind='services', member_id=h.seed['services'][1]))
        tag_id = tag.id
    h.command('coordinator', 'policy.create', {'name': 'Wrong kind', 'owner': 'Test', 'source': 'Synthetic', 'starts_on': '2020-01-01',
              'definition': {'rule': {'kind': 'min_rest', 'hours': 11}, 'scope': {'conditions': tree(line('person_ids', tag_id))},
                             'severity': 'mandatory'}}, status=422)
    rule = h.policy({'kind': 'restriction', 'weekdays': list(range(7)), 'start_time': '00:00', 'end_time': '24:00'},
                    {'conditions': tree(line('service_ids', tag_id))})
    w = h.shift(h.workspace(), service=0)['workspace']
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', service=1, seed_restricted=True)['workspace']

    def blocked():
        found = h.lifecycle(h.detail(w)['workspace'], 'validate')['workspace']['validation']['findings']
        days = {s['id']: s['start'][:10] for s in h.detail(w)['shifts']}
        return sorted(days[f['shift_id']] for f in found if f.get('rule_id') == rule['id'])
    assert blocked() == ['2026-10-05']
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.TagMember(department_id=h.seed['department_id'], tag_id=tag_id, kind='services', member_id=h.seed['services'][0]))
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
    assert blocked() == ['2026-10-04', '2026-10-05']


def members(h, tag, add=(), remove=(), role='coordinator', status=200):
    payload = {'add': [{'kind': k, 'id': i} for k, i in add], 'remove': [{'kind': k, 'id': i} for k, i in remove], 'reason': 'Synthetic tag change'}
    return h.command(role, 'tag.members', payload, tag['id'], tag['revision'], status=status)


def test_membership_command_checks_kinds_keeps_people_in_step_and_undoes(h):
    tag = h.configure('tags', {'name': 'VIP', 'labels': ['people', 'services']}, role='coordinator')['record']
    clinic, person_id = h.seed['services'][0], h.seed['people'][0]
    members(h, tag, add=[('locations', h.seed['locations'][0])], status=422)        # the tag does not label places
    members(h, tag, add=[('services', str(uuid.uuid4()))], status=404)              # not a record of this department
    members(h, tag, add=[('services', clinic)], role='staff', status=403)
    done = members(h, tag, add=[('services', clinic), ('people', person_id)])
    assert len(done['added']) == 2 and done['record']['revision'] == tag['revision'] + 1
    listed = h.get('coordinator', '/api/catalogues')['tag_members']                  # the app holds members with the lists
    assert {(r['kind'], r['member_id']) for r in listed if r['tag_id'] == tag['id']} == {('services', clinic), ('people', person_id)}
    assert person(h)['tags'] == ['VIP']                                             # the older field follows
    members(h, tag, add=[('services', clinic)], status=409)                         # stale revision
    again = members(h, done['record'], add=[('services', clinic)])                  # already in: nothing changes
    assert again['added'] == [] and again['record']['revision'] == done['record']['revision']
    undo = members(h, done['record'], remove=[(r['kind'], r['id']) for r in done['added']])
    assert len(undo['removed']) == 2 and rows(h, m.TagMember, tag_id=tag['id']) == [] and person(h)['tags'] == []
    kinds = {r['kind'] for r in rows(h, m.DefinitionHistory) if r['object_id'] == tag['id'] or r['kind'].startswith('tag.')}
    assert kinds == {'tags', 'tag.member_add', 'tag.member_remove'}                 # every step audited


def test_preview_counts_rule_lines_reach_views_and_draft_sessions(h):
    tag = h.configure('tags', {'name': 'Night team', 'labels': ['people', 'services']})['record']
    first, second = h.seed['people'][:2]
    tag = members(h, tag, add=[('people', first)])['record']
    rule = h.policy({'kind': 'min_rest', 'hours': 11}, {'conditions': tree(line('person_ids', tag['id']))})
    h.policy({'kind': 'min_rest', 'hours': 10}, {'conditions': tree(line('tags', 'night TEAM'.lower()))})  # by its name
    h.shift(h.workspace(), people=[second], service=1)
    h.command('coordinator', 'table_view.create', {'table_key': 'people', 'name': 'Night team only',
              'definition': {'filters': {'tags': [tag['id']]}}})
    h.command('coordinator', 'table_view.create', {'table_key': 'catalogue-services', 'name': 'By its chip',
              'definition': {'filters': {'tags': ['#Night team']}}})
    out = h.client('coordinator').post(f"/api/tags/{tag['id']}/preview", json={'add': [{'kind': 'people', 'id': second}],
                                                                                 'remove': [{'kind': 'people', 'id': first}]}).json()
    assert out['rule_lines'] == 2 and len(out['rules']) == 2
    mine = next(r for r in out['rules'] if r['policy_id'] == rule['id'])
    assert (mine['people_before'], mine['people_after']) == (1, 1) and mine['lines'] == 1
    assert sorted(v['name'] for v in out['saved_views']) == ['By its chip', 'Night team only']
    assert (out['draft_sessions'], out['drafts']) == (1, 1)                          # the second person's session
    assert rows(h, m.TagMember, tag_id=tag['id'])[0]['member_id'] == first           # nothing saved
    grow = h.client('coordinator').post(f"/api/tags/{tag['id']}/preview", json={'add': [{'kind': 'people', 'id': second}]}).json()
    assert next(r for r in grow['rules'] if r['policy_id'] == rule['id'])['people_after'] == 2
    assert h.client('staff').post(f"/api/tags/{tag['id']}/preview", json={'add': [{'kind': 'people', 'id': second}]}).status_code == 403
    assert h.client('coordinator').post(f"/api/tags/{tag['id']}/preview", json={}).status_code == 422


def test_who_may_lead_cannot_name_a_tag(h):
    """A session keeps its duty type as it was, outside the rules' tag resolution, so a lead condition names people."""
    tag = h.configure('tags', {'name': 'Leads', 'labels': ['people']})['record']
    typ = next(t for t in h.get('admin', '/api/catalogues')['assignment_types'] if t['id'] == h.seed['type'])
    value = {k: typ[k] for k in ('name', 'description', 'starts_on', 'ends_on', 'active')}
    value['definition'] = {**typ['definition'], 'lead': {'minimum': 1, 'conditions': tree(line('person_ids', tag['id']))}}
    h.configure('assignment_types', value, typ['id'], typ['revision'], status=422)
    value['definition']['lead']['conditions'] = tree(line('person_ids', h.seed['people'][0]))
    h.configure('assignment_types', value, typ['id'], typ['revision'], status=422)  # Leadership is authored in Rules


def test_migration_copies_people_tags_into_tag_records(tmp_path):
    """Data safety: 0054 adds two tables and copies each person's tags; people.tags keeps every value."""
    from .migration_support import add_historical_admin

    from alembic import command
    from scripts.manage import alembic_config, migrate
    url = 'sqlite:///' + str(tmp_path / 'before.db')
    command.upgrade(alembic_config(url), '0053_person_pager')
    engine = make_engine(url)
    ids = {k: str(uuid.uuid4()) for k in ('org', 'd1', 'd2', 'cat')} | {p: 'person-' + p for p in 'abc'}  # a first: 'Night' is spelled by a
    people = {'a': ['Night', 'Locum'], 'b': ['night', ' ', 'Teaching'], 'c': ['Locum']}
    with engine.begin() as c:
        c.execute(text("INSERT INTO organisations (id, revision, created_at, name, timezone, week_start, input_revision, availability_revision) "
                       "VALUES (:id, 1, '2026-01-01', 'Org', 'Asia/Riyadh', 6, 1, 1)"), {'id': ids['org']})
        for d in ('d1', 'd2'):
            c.execute(text("INSERT INTO departments (id, revision, created_at, organisation_id, name, generation_defaults) "
                           "VALUES (:id, 1, '2026-01-01', :o, :n, '{}')"), {'id': ids[d], 'o': ids['org'], 'n': d})
        c.execute(text("INSERT INTO categories (id, revision, created_at, department_id, name, description, starts_on, active) "
                       "VALUES (:id, 1, '2026-01-01', :d, 'Rank', '', '2020-01-01', 1)"), {'id': ids['cat'], 'd': ids['d1']})
        for p, tags in people.items():
            c.execute(text("INSERT INTO people (id, revision, created_at, organisation_id, display_name, active, tags) "
                           "VALUES (:id, 1, '2026-01-01', :o, :n, 1, :t)"), {'id': ids[p], 'o': ids['org'], 'n': p, 't': json.dumps(tags)})
        for p in ('a', 'b'):  # c holds no rank: their tags go to both departments
            c.execute(text("INSERT INTO memberships (id, revision, created_at, department_id, person_id, category_id, starts_on) "
                           "VALUES (:id, 1, '2026-01-01', :d, :p, :c, '2020-01-01')"), {'id': str(uuid.uuid4()), 'd': ids['d1'], 'p': ids[p], 'c': ids['cat']})
        add_historical_admin(c, ids['d1'])
        before = c.execute(text('SELECT id, tags FROM people ORDER BY id')).fetchall()
    engine.dispose()
    migrate(url)
    engine = make_engine(url)
    try:
        with engine.begin() as c:
            assert c.execute(text('SELECT id, tags FROM people ORDER BY id')).fetchall() == before
            tags = {(r[0], r[1]): r[2] for r in c.execute(text('SELECT department_id, name, labels FROM tags'))}
            assert set(tags) == {(ids['d1'], 'Night'), (ids['d1'], 'Locum'), (ids['d1'], 'Teaching'), (ids['d2'], 'Locum')}
            assert all(json.loads(v) == ['people'] for v in tags.values())
            members = c.execute(text('SELECT t.department_id, t.name, m.member_id FROM tag_members m JOIN tags t ON t.id = m.tag_id '
                                     "WHERE m.kind = 'people'")).fetchall()
            expected = {(ids['d1'], 'Night', ids['a']), (ids['d1'], 'Locum', ids['a']), (ids['d1'], 'Night', ids['b']),
                        (ids['d1'], 'Teaching', ids['b']), (ids['d1'], 'Locum', ids['c']), (ids['d2'], 'Locum', ids['c'])}
            assert set(members) == expected and len(members) == len(expected)
    finally:
        engine.dispose()


def test_the_tag_list_carries_how_many_rule_lines_use_each_tag(h):
    tag = h.configure('tags', {'name': 'Night', 'labels': ['people', 'services']})['record']
    h.configure('tags', {'name': 'Spare', 'labels': ['people']})
    h.policy({'kind': 'min_rest', 'hours': 11}, {'conditions': tree(line('person_ids', tag['id']), line('service_ids', tag['id']))})
    h.policy({'kind': 'min_rest', 'hours': 10}, {'conditions': tree(line('tags', 'night'))})  # by its name
    listed = {t['name']: t['used_by'] for t in h.get('approver', '/api/catalogues')['tags']}
    assert listed == {'Night': {'lines': 3, 'rules': 2}, 'Spare': {'lines': 0, 'rules': 0}}


def test_fairness_filters_and_announcement_audiences_read_tags_as_members(h):
    from tests.outbox_support import process_one_after_undo as process_one
    from .governance_support import identity
    from .test_announcements_operations import announce, inbox
    staff = h.get('staff', '/api/me')['person_id']
    other = next(p for p in h.seed['people'] if p != staff)
    tag = h.configure('tags', {'name': 'Night team', 'labels': ['people']})['record']
    tag = members(h, tag, add=[('people', staff)])['record']
    who = json.dumps(tree(line('person_ids', tag['id'])))
    people = h.get('coordinator', '/api/fairness/distribution?metric=duties&starts_on=2026-10-01&ends_on=2026-11-01&who=' + who)['people']
    assert [r['person_id'] for r in people] == [staff]
    identity(h, 'colleague', ['staff'], other)
    row = announce(h, {'everyone': False, 'where': tree(line('tags', tag['id']))}, name='Night team briefing')
    process_one(h.app.state.engine)
    assert any(n['title'].endswith('Night team briefing') for n in inbox(h, 'staff'))
    assert not any(n['title'].endswith('Night team briefing') for n in inbox(h, 'colleague'))
    # The overview (one viewer, many notices) reads the same way.
    assert row['id'] in [i['id'] for i in h.get('staff', '/api/content')['items']]
    assert row['id'] not in [i['id'] for i in h.get('colleague', '/api/content')['items']]
