"""Audit runs keep every change reachable while hydrating only the requested run page."""
import ast
from datetime import datetime, timezone
from pathlib import Path
import re
from zoneinfo import ZoneInfo
import pytest

from sqlalchemy import event, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import audit_log
from packages.contracts import schemas as s
from packages.contracts.audit import AUDIT_ACTION_CATALOGUE, AUDIT_GROUPS, audit_summary


def _add(h, values):
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all([m.AuditEvent(**{'department_id': h.seed['department_id'],
            'actor_id': h.seed['identities']['coordinator'], 'action': 'shift.update',
            'target_id': m.uid(), 'reason': 'Synthetic audit scale change', 'detail': {},
            'correlation_id': 'synthetic-audit-run', **value}) for value in values])


def _query(h):
    return (select(m.AuditEvent, m.Identity.display_name)
        .outerjoin(m.Identity, m.Identity.id == m.AuditEvent.actor_id)
        .where(m.AuditEvent.department_id == h.seed['department_id'], m.AuditEvent.action == 'shift.update')
        .order_by(m.AuditEvent.created_at.desc(), m.AuditEvent.id.desc()))


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_scale_run_is_one_page_and_one_hydrated_event(scale_harness, database):
    h = scale_harness(database)
    _add(h, [{'created_at': datetime(2026, 10, 3, 6, tzinfo=timezone.utc)} for _ in range(5000)])
    with transaction(h.app.state.engine) as db:
        loaded = []
        event.listen(db, 'loaded_as_persistent', lambda _, row: loaded.append(row))
        page = audit_log.folded(db, _query(h), ZoneInfo('Asia/Riyadh'), 100)
        # Deterministic row budget applies unchanged on slower CI hosts.
        assert len(loaded) == 1
        assert len(page['events']) == 1 and page['events'][0]['count'] == 5000
        assert page['changes'] == 5000 and page['has_more'] is False and page['next_cursor'] is None
        run = page['events'][0]
        expanded = list(db.execute(audit_log.expand(_query(h), run['run_id'])))
        assert len(expanded) == 5000


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_cursor_preserves_whole_consecutive_runs_and_stable_timestamp_ties(request, database):
    h = request.getfixturevalue(database)
    ids = [f'00000000-0000-0000-0000-{i:012d}' for i in range(1, 8)]
    stamp = datetime(2026, 10, 3, 6, tzinfo=timezone.utc)
    _add(h, [{'id': ident, 'created_at': stamp,
        'reason': 'Different reason splits identical runs' if i == 3 else 'Synthetic audit scale change'}
        for i, ident in enumerate(ids)])
    with transaction(h.app.state.engine) as db:
        cursor, counts, seen = None, [], []
        while True:
            page = audit_log.folded(db, _query(h), ZoneInfo('Asia/Riyadh'), 1, cursor)
            run = page['events'][0]
            counts.append(run['count'])
            seen.extend(row.id for row, _ in db.execute(audit_log.expand(_query(h), run['run_id'])))
            if not page['has_more']:
                break
            cursor = page['next_cursor']
        assert counts == [3, 1, 3]
        assert seen == list(reversed(ids))


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_runs_use_department_local_day(request, database):
    h = request.getfixturevalue(database)
    _add(h, [{'created_at': datetime.fromisoformat(stamp)} for stamp in (
        '2026-10-03T20:59:00+00:00', '2026-10-03T21:01:00+00:00', '2026-10-03T22:01:00+00:00')])
    with transaction(h.app.state.engine) as db:
        assert [row['count'] for row in audit_log.folded(db, _query(h), ZoneInfo('Asia/Riyadh'), 100)['events']] == [2, 1]
        assert [row['count'] for row in audit_log.folded(db, _query(h), ZoneInfo('America/New_York'), 100)['events']] == [3]


def test_runs_keep_catalogues_separate(h):
    _add(h, [{'id': f'00000000-0000-0000-0000-{i:012d}',
        'created_at': datetime(2026, 10, 3, 6, tzinfo=timezone.utc), 'detail': {'catalogue': kind}}
        for i, kind in enumerate(('services', 'services', 'locations'), 1)])
    with transaction(h.app.state.engine) as db:
        assert [row['count'] for row in audit_log.folded(db, _query(h), ZoneInfo('Asia/Riyadh'), 100)['events']] == [1, 2]


def test_action_catalogue_covers_command_branches_and_literal_audit_actions():
    actions = set()
    for path in Path('apps/api').rglob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and (isinstance(node.left, ast.Name) and node.left.id == 'kind'
                    or isinstance(node.left, ast.Attribute) and node.left.attr == 'kind'):
                for value in node.comparators:
                    actions.update(child.value for child in ast.walk(value)
                        if isinstance(child, ast.Constant) and isinstance(child.value, str) and '.' in child.value)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'AuditEvent':
                actions.update(value.value.value for value in node.keywords
                    if value.arg == 'action' and isinstance(value.value, ast.Constant))
        for function in (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)):
            prefixes = {}
            for assignment in (node for node in ast.walk(function) if isinstance(node, ast.Assign)):
                value = assignment.value
                if (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
                        and value.func.attr == 'removeprefix' and isinstance(value.func.value, ast.Attribute)
                        and value.func.value.attr == 'kind' and len(value.args) == 1
                        and isinstance(value.args[0], ast.Constant) and isinstance(value.args[0].value, str)):
                    prefixes.update({target.id: value.args[0].value for target in assignment.targets
                                     if isinstance(target, ast.Name)})
            if prefixes:
                for node in ast.walk(function):
                    if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id in prefixes:
                        actions.update(prefixes[node.left.id] + child.value
                            for value in node.comparators for child in ast.walk(value)
                            if isinstance(child, ast.Constant) and isinstance(child.value, str))
    assert actions - AUDIT_ACTION_CATALOGUE.keys() == set()
    assert all(row['noun'] and row['plural'] and row['verb'] and row['group'] in AUDIT_GROUPS
        for row in AUDIT_ACTION_CATALOGUE.values())


def test_refused_and_invitation_audit_words_are_plain_and_object_aware():
    refused = AUDIT_ACTION_CATALOGUE['command.refused']
    assert refused['noun'] == 'Refused changes' and refused['verb'] == 'was refused a change'
    assert audit_summary('command.refused', subject='Auditor role for Demo (test)') == \
        'was refused a change: Auditor role for Demo (test)'
    assert AUDIT_ACTION_CATALOGUE['account.invite_accepted']['verb'] == 'accepted their invitation'
    assert AUDIT_ACTION_CATALOGUE['publication.changes.prepare']['verb'] == 'prepared a rota change for'


@pytest.mark.parametrize('action', ['policy.retire', 'policy.retire_pending', 'policy.restore', 'policy.restore_pending'])
def test_policy_transition_audit_wording_matches_the_client(action):
    source = Path('apps/web/src/app.ts').read_text(encoding='utf-8')
    verbs = re.search(r'const AUDIT_VERB:[^=]+=(\{.*?\});', source, re.S).group(1)
    wording = dict(re.findall(r"(\w+):'([^']*)'", verbs))
    assert wording[action.split('.')[-1]] == AUDIT_ACTION_CATALOGUE[action]['verb']


def test_access_subject_is_snapshotted_and_historical_lookup_is_batched(h):
    person = h.seed['identities']['staff']
    value = {'identity_id': person, 'role': 'coordinator', 'starts_on': '2026-10-04', 'ends_on': '2026-11-01'}
    with transaction(h.app.state.engine) as db:
        detail = audit_log.subject_detail(db, 'role.grant', {'record': value})
        assert detail['target_label'].startswith('Coordinator to ')
        assert 'from 2026-10-04 until 2026-11-01' in detail['target_label']
        rows = [{'kind': 'role', 'action': 'role.grant', 'target_id': None, 'detail': detail}]
        assert audit_log.subjects(db, h.seed['department_id'], rows)[0]['target_label'] == detail['target_label']
        binding = db.scalar(select(m.RoleBinding).where(m.RoleBinding.department_id == h.seed['department_id'],
            m.RoleBinding.identity_id == person, m.RoleBinding.role == 'staff'))
        loaded = []
        event.listen(db, 'loaded_as_persistent', lambda _, row: loaded.append(row))
        rows = [{'kind': 'role', 'action': 'role.grant', 'target_id': binding.id, 'detail': {}} for _ in range(100)]
        audit_log.subjects(db, h.seed['department_id'], rows)
        assert len({row['target_label'] for row in rows}) == 1 and rows[0]['target_label'].startswith('Staff to ')
        assert len(loaded) <= 1


def test_audit_api_pages_runs_expands_changes_and_rejects_invalid_cursors(h):
    _add(h, [{'created_at': datetime(2026, 10, 3, 6, tzinfo=timezone.utc)} for _ in range(201)])
    page = h.get('auditor', '/api/audit?fold=true&people=false&action=shift')
    s.AuditPage.model_validate(page)
    assert len(page['events']) == 1 and page['events'][0]['count'] == 201
    assert page['changes'] == 201 and page['has_more'] is False
    token = page['events'][0]['run_id']
    raw = h.get('auditor', '/api/audit?people=false&action=shift&run_id=' + token)
    s.AuditPage.model_validate(raw)
    assert len(raw['events']) == 100 and raw['has_more'] is True
    assert 'count' not in raw['events'][0]
    tail = h.get('auditor', '/api/audit?people=false&action=shift&offset=200&run_id=' + token)
    assert len(tail['events']) == 1 and tail['has_more'] is False
    for query in ('fold=true&cursor=broken', 'fold=true&cursor=e30', 'cursor=broken',
            'fold=true&run_id=broken', 'fold=true&offset=1', 'run_id=broken'):
        h.get('auditor', '/api/audit?' + query, status=422)
    stamp = '2026-10-03T06:00:00+00:00'
    ident = '00000000-0000-0000-0000-000000000001'
    for invalid in (1, [], {}, None):
        h.get('auditor', '/api/audit?fold=true&cursor=' + audit_log._token([stamp, invalid]), status=422)
        h.get('auditor', '/api/audit?run_id=' + audit_log._token([stamp, ident, stamp, invalid]), status=422)
    h.get('staff', '/api/audit?fold=true', status=403)


def test_access_command_snapshot_and_old_receipt_subject(h):
    h.command('admin', 'role.grant', {'identity_id': h.seed['identities']['staff'],
        'role': 'coordinator', 'starts_on': '2026-10-04', 'ends_on': '2026-11-01',
        'reason': 'Synthetic access audit regression'})['record']
    granted = next(row for row in h.get('auditor', '/api/audit?action=role')['events']
        if row['action'] == 'role.grant')
    assert 'Coordinator to Staff' in granted['target_label']
    assert granted['target_label'] == granted['detail']['target_label']
    assert 'from 2026-10-04 until 2026-11-01' in granted['target_label']
    with transaction(h.app.state.engine) as db:
        legacy = {'kind': 'role', 'action': 'role.grant', 'target_id': None,
            'detail': {'receipt_id': granted['detail']['receipt_id']}}
        assert audit_log.subjects(db, h.seed['department_id'], [legacy])[0]['target_label'] == granted['target_label']


@pytest.mark.parametrize(('kind', 'key', 'value', 'expected'), [
    ('review_route.create', 'route', {'name': 'Independent review'}, 'Independent review'),
    ('identity.disable', 'record', {'display_name': 'Synthetic sign-in (test)'}, 'Synthetic sign-in (test)'),
    ('account.reset_link', 'record', {'display_name': 'Synthetic sign-in (test)'}, 'Synthetic sign-in (test)'),
    ('self_approval.grant', 'permission', {'role': 'approver', 'actions': ['request.decide'],
        'starts_on': '2026-10-04'}, 'Approver: request.decide from 2026-10-04'),
])
def test_access_event_subject_kinds(h, kind, key, value, expected):
    with transaction(h.app.state.engine) as db:
        assert audit_log.subject_detail(db, kind, {key: value}) == {'target_label': expected}


@pytest.mark.parametrize('kind', ['role.grant', 'identity.disable'])
def test_bulk_access_event_subject_survives_a_later_identity_rename(h, kind):
    identity_id = h.seed['identities']['staff']
    with transaction(h.app.state.engine) as db:
        identity = db.get(m.Identity, identity_id)
        name, revision = identity.display_name, identity.revision
    item = {'payload': {'identity_id': identity_id, 'role': 'coordinator',
        'starts_on': '2026-10-04', 'ends_on': '2026-11-01'}} if kind == 'role.grant' else {
        'target_id': identity_id, 'expected_revision': revision}
    result = h.command('admin', 'bulk.apply', {'item_kind': kind, 'items': [item],
        'reason': 'Synthetic access history snapshot'})
    head = kind.split('.', 1)[0]
    original = next(row for row in h.get('auditor', '/api/audit?action=' + head)['events']
        if row['detail'].get('batch_id') == result['batch_id'])
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, identity_id).display_name = 'Renamed synthetic account (test)'
    after = next(row for row in h.get('auditor', '/api/audit?action=' + head)['events'] if row['id'] == original['id'])
    assert after['target_label'] == original['target_label']
    assert name in after['target_label'] and after['detail']['target_label'] == after['target_label']
    assert not {'subject', 'issuer', 'password_hash'} & after.keys()
    assert not {'subject', 'issuer', 'password_hash'} & after['detail'].keys()
    if kind == 'identity.disable':
        assert after['target_label'] == name
