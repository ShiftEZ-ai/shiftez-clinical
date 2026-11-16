"""Rule read authority covers picker fragments and every alternate rule reader."""
from datetime import date, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from .governance_support import identity


SECRET = 'Synthetic confidential exception explanation'
PROPOSAL = 'Synthetic confidential proposed rule'
TAG = 'Synthetic rule tag'
pytestmark = pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)


@pytest.fixture
def private_rules(h):
    for role in s.ROLE_NAMES:
        if role not in h.seed['identities']:
            identity(h, role, [role])
    active = h.policy({'kind': 'max_duties', 'maximum': 3}, exception=True)
    tag = h.configure('tags', {'name': TAG, 'labels': ['people', 'policies']})['record']
    proposed = h.command('coordinator', 'rule.propose', {
        'catalogue_id': 'duties_per_week', 'lines': [
            {'id': str(uuid4()), 'who': {'conditions': [{'field': 'person_ids', 'values': [tag['id']]}]},
             'numbers': {'max': {'value': 5}}}],
        'reason': PROPOSAL})['record']
    exception = h.command('coordinator', 'exception.create', {
        'policy_id': active['id'], 'person_id': h.seed['people'][0],
        'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'reason': SECRET})['record']
    with transaction(h.app.state.engine, write=True) as db:
        retired = m.Policy(department_id=h.seed['department_id'], name=PROPOSAL,
            kind=active['kind'], definition=active['definition'], status='retired',
            owner=active['owner'], source=PROPOSAL, starts_on=date(2020, 1, 1),
            created_by=h.seed['identities']['coordinator'])
        db.add(retired)
        db.add(m.AuditEvent(department_id=h.seed['department_id'],
            actor_id=h.seed['identities']['coordinator'], action='exception.create',
            target_id=exception['id'], reason=SECRET, detail={'person_id': exception['person_id']},
            correlation_id='synthetic-rule-privacy'))
        db.add(m.AuditEvent(department_id=h.seed['department_id'],
            actor_id=h.seed['identities']['coordinator'], action='bulk.apply',
            target_id=exception['id'], reason=SECRET, detail={'item_kinds': ['exception.create']},
            correlation_id='synthetic-rule-privacy'))
        db.flush()
        retired_id = retired.id
        db.add_all([m.TagMember(department_id=h.seed['department_id'], tag_id=tag['id'],
            kind='policies', member_id=policy_id) for policy_id in (active['id'], proposed['id'], retired_id)])
    return active, proposed, exception, retired_id


def rows(client, collection, **selection):
    return client.post('/api/bulk/rows', json={'selection': {'collection': collection, **selection}})


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_every_role_rule_read_paths(h, private_rules, role):
    active, proposed, exception, retired_id = private_rules
    client = h.client(role)
    roles = set(client.get('/api/me').json()['roles'])
    reads = bool(roles.intersection(s.PERMISSIONS['rules.read']))
    catalogue = bool(roles.intersection(s.PERMISSIONS['catalogue.read']))
    audit = bool(roles.intersection(s.PERMISSIONS['audit.read']))
    privileged = h.client('auditor').get('/api/catalogues').json()
    known = ','.join(privileged['_versions'].values())
    for query in ('', '?include=exceptions', '?include=tags&include=tag_members', '?known=' + known):
        response = client.get('/api/catalogues' + query)
        assert response.status_code == (200 if catalogue else 403), response.text
        if not catalogue:
            continue
        body = response.json()
        exceptions = body.get('exceptions', privileged['exceptions'] if reads else [])
        assert any(row['id'] == exception['id'] and row['person_id'] == exception['person_id']
                   and row['reason'] == SECRET for row in exceptions) == reads
        if not reads:
            assert body.get('exceptions', []) == []
            assert SECRET not in response.text and exception['id'] not in response.text
        if 'policies' in body:
            assert (proposed['id'] in {row['id'] for row in body['policies']}) == reads
            assert (active['id'] in {row['id'] for row in body['policies']}) == reads
        if 'tags' in body:
            tag = next(row for row in body['tags'] if row['name'] == TAG)
            assert tag['used_by'] == {'rules': 1 if reads else 0, 'lines': 1 if reads else 0}
        if 'tag_members' in body:
            members = {row['member_id'] for row in body['tag_members'] if row['kind'] == 'policies'}
            assert (active['id'] in members) == reads
            assert (proposed['id'] in members) == reads
            assert (retired_id in members) == reads
        if query == '':
            own_known = ','.join(body['_versions'].values())
            repeat = client.get('/api/catalogues', params={'known': own_known})
            assert repeat.status_code == 200 and set(repeat.json()) == {'_versions'}
            conditional = client.get('/api/catalogues', headers={'if-none-match': response.headers['etag']})
            assert conditional.status_code == 304
    for include in (None, 'exceptions', 'policies'):
        params = {'counts_only': 'true', **({'include': include} if include else {})}
        response = client.get('/api/catalogues', params=params)
        assert response.status_code == (200 if catalogue else 403), response.text
        if catalogue:
            body = response.json()
            if include != 'policies':
                assert body['counts']['exceptions'] == (1 if reads else 0)
            expected = h.client('auditor').get('/api/catalogues', params=params).json()
            if include != 'exceptions':
                assert body['counts']['policies'] == (expected['counts']['policies'] if reads else body['rules_in_force'])
            assert body['rules_in_force'] == expected['rules_in_force'] > 0
            assert SECRET not in response.text and exception['id'] not in response.text
    for status, identity in (('active', active['id']), ('proposed', proposed['id']), ('retired', retired_id)):
        response = client.get('/api/policies', params={'status': status, 'effective': 'true'})
        assert response.status_code == (200 if reads else 403), response.text
        if reads:
            assert identity in {row['id'] for row in response.json()['policies']}
    for version in (1, 2):
        response = client.get('/api/rules/catalogue', params={'version': version})
        assert response.status_code == (200 if reads else 403), response.text
        if reads:
            assert (proposed['id'] in response.text) == (reads and version == 1)
    response = client.get('/api/rules/catalogue/duties_per_week/history')
    assert response.status_code == (200 if reads else 403), response.text
    if reads:
        assert proposed['id'] in response.text
    response = client.get('/api/policies/reach')
    assert response.status_code == (200 if reads else 403), response.text
    if reads:
        assert proposed['id'] in response.json()['reach']
    for collection, identity in (('exceptions', exception['id']), ('policies', proposed['id']), ('policy_lines', proposed['id'])):
        response = rows(client, collection)
        allowed = reads
        assert response.status_code == (200 if allowed else 403), response.text
        if allowed:
            assert (identity in response.text) == reads
            if collection == 'exceptions':
                assert SECRET in response.text and exception['person_id'] in response.text
        resolution = client.post('/api/bulk/resolve', json={'collection': collection, 'include_ids': [identity]})
        assert resolution.status_code == (200 if allowed else 403), resolution.text
    response = rows(client, 'policies', where={'q': proposed['name']})
    assert response.status_code == (200 if reads else 403), response.text
    if reads:
        assert (proposed['id'] in response.text) == reads
    response = client.get('/api/search/references', params={
        'collection': 'exceptions', 'kind': 'people', 'q': 'Synthetic'})
    assert response.status_code == (200 if reads else 403), response.text
    selection = {'collection': 'exceptions', 'include_ids': [exception['id']]}
    payload = {'selection': selection, 'item_kind': 'exception.end', 'reason': 'Synthetic privacy check'}
    response = client.post('/api/commands/preview', json={'kind': 'bulk.apply', 'payload': payload})
    if not reads:
        assert response.status_code == 403 and SECRET not in response.text
        h.command(role, 'bulk.apply', payload, status=403)
    else:
        assert response.status_code == 200, response.text
    for path in ('/api/audit?people=false', '/api/audit?fold=true&people=false',
                 '/api/audit?q=' + SECRET, '/api/audit.csv'):
        response = client.get(path)
        assert response.status_code == (200 if audit else 403), response.text
        if audit:
            assert (SECRET in response.text) == reads
            if not reads or '.csv' not in path:
                assert (exception['id'] in response.text) == reads
    response = rows(client, 'history')
    assert response.status_code == (200 if audit else 403), response.text
    if audit:
        assert (SECRET in response.text) == reads
        assert (exception['id'] in response.text) == reads


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_saved_rule_payload_rechecks_current_read_permission(h, private_rules, role):
    active, _, exception, _ = private_rules
    client = h.client(role)
    reads = bool(set(client.get('/api/me').json()['roles']).intersection(s.PERMISSIONS['rules.read']))
    with transaction(h.app.state.engine, write=True) as db:
        stage = m.BulkStaged(department_id=h.seed['department_id'], owner_id=h.seed['identities'][role],
            expires_at=m.now() + timedelta(days=1), payload={'item_kind': 'exception.create', 'reason': SECRET,
                'items': [{'payload': {'policy_id': active['id'], 'person_id': exception['person_id'],
                    'reason': SECRET, 'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}}]})
        db.add(stage)
        db.flush()
        stage_id = stage.id
    response = client.get('/api/bulk/staged')
    assert response.status_code == 200, response.text
    assert (stage_id in response.text) == reads
    assert (SECRET in response.text) == reads
    if not reads:
        response = client.post(f'/api/bulk/staged/{stage_id}/reopen')
        assert response.status_code == 404 and SECRET not in response.text


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_publication_reader_filters_retained_proposals(h, private_rules, role, monkeypatch, tmp_path):
    from apps.api import main
    from apps.api.services import planning
    web = tmp_path / 'web'
    web.mkdir()
    (web / 'index.html').write_text('<script type="module" src="/assets/' + '0' * 64 + '/app.js"></script>')
    monkeypatch.setattr(main, 'WEB', web)
    active, proposed, _, _ = private_rules
    active = {**active, 'definition': {**active['definition'], 'rule': {
        'kind': 'catalogue', 'id': 'duties_per_week', 'lines': [{'id': str(uuid4()),
            'who': {'conditions': [{'field': 'person_ids', 'values': [h.seed['people'][0]]}]},
            'note': SECRET, 'numbers': {'max': {'value': 3}}}]}}}
    build = planning.publication_snapshot

    def snapshot(*args, **kwargs):
        result = build(*args, **kwargs)
        result['leadership']['policies'] = [active, proposed]
        return result

    monkeypatch.setattr(planning, 'publication_snapshot', snapshot)
    shift = h.shift(h.workspace(), people=h.seed['people'][:1])
    publication_id = h.publish(shift['workspace'])['publication_id']
    client = h.client(role)
    roles = set(client.get('/api/me').json()['roles'])
    allowed = bool(roles.intersection(('staff', *s.PERMISSIONS['publications.read_team'])))
    reads = bool(roles.intersection(s.PERMISSIONS['rules.read']))
    response = client.get('/api/publications/' + publication_id)
    assert response.status_code == (200 if allowed else 403), response.text
    if allowed:
        policies = response.json()['snapshot']['leadership']['policies']
        assert (active['id'] in {row['id'] for row in policies}) == reads
        assert (proposed['id'] in {row['id'] for row in policies}) == reads
        assert (SECRET in response.text) == reads
    with transaction(h.app.state.engine) as db:
        assert proposed['id'] in {row['id'] for row in db.get(m.Publication, publication_id).snapshot['leadership']['policies']}
    for format in ('json', 'csv', 'assignments', 'quotas', 'ics', 'print'):
        response = client.get('/api/reports/' + publication_id,
                              params={'format': format, 'person_id': h.seed['people'][0]})
        expected_status = 404 if allowed and format == 'quotas' else 200 if allowed else 403
        assert response.status_code == expected_status, (format, response.text)
        assert proposed['id'] not in response.text and SECRET not in response.text


def test_permission_changes_replace_fragment_versions_and_do_not_query_exceptions(h, private_rules, monkeypatch):
    client = h.client('credentialer')
    # Keep the role set identical: permission itself must bind both caches and validators.
    monkeypatch.setitem(s.PERMISSIONS, 'rules.read', (*s.PERMISSIONS['rules.read'], 'credentialer'))
    full = client.get('/api/catalogues?include=exceptions&include=policies')
    assert full.status_code == 200 and SECRET in full.text
    full_rule_catalogue = client.get('/api/rules/catalogue')
    assert full_rule_catalogue.status_code == 200
    assert rows(client, 'policy_lines').status_code == 200
    monkeypatch.setitem(s.PERMISSIONS, 'rules.read', tuple(r for r in s.PERMISSIONS['rules.read'] if r != 'credentialer'))
    statements = []
    def capture(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)
    event.listen(h.app.state.engine, 'before_cursor_execute', capture)
    try:
        limited = client.get('/api/catalogues',
            params={'include': ['exceptions', 'policies'], 'known': ','.join(full.json()['_versions'].values())},
            headers={'if-none-match': full.headers['etag']})
        assert client.get('/api/rules/catalogue',
            headers={'if-none-match': full_rule_catalogue.headers['etag']}).status_code == 403
        assert rows(client, 'policy_lines').status_code == 403
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', capture)
    assert limited.status_code == 200, limited.text
    assert limited.json()['exceptions'] == [] and SECRET not in limited.text
    assert not any('from policies' in statement.lower() or 'policy_exceptions' in statement.lower()
                   for statement in statements)
    for kind in ('exceptions', 'policies'):
        assert limited.json()['_versions'][kind] != full.json()['_versions'][kind]
    assert limited.headers['etag'] != full.headers['etag']
    assert limited.json()['policies'] == []


def test_identical_empty_fragments_have_permission_scoped_versions(h, monkeypatch):
    monkeypatch.setitem(s.PERMISSIONS, 'rules.read', tuple(role for role in s.PERMISSIONS['rules.read'] if role != 'credentialer'))
    params = {'include': ['exceptions', 'policies']}
    full = h.client('auditor').get('/api/catalogues', params=params).json()
    limited = h.client('credentialer').get('/api/catalogues', params=params).json()
    for kind in ('exceptions', 'policies'):
        assert limited[kind] == []
        if kind == 'exceptions':
            assert full[kind] == []
        assert full['_versions'][kind] != limited['_versions'][kind]
