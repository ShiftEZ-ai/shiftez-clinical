"""Every declared exclusion must preserve freshly acquired scheduling currency."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import Boolean, Date, DateTime, Integer, JSON, String, bindparam, select, update
from sqlalchemy.orm import Session

from apps.api import models as m, snapshots
from apps.api.db import transaction
from packages.contracts.schemas import AutoscheduleAnimationSettings, GenerationOptions
from packages.domain.input_manifest import FIELDS, manifest
from tests.conftest import TEST_PASSWORD, postgres_harness, sqlite_harness
from apps.api.identity_store import hash_password
from tests.test_events import event, event_type, occurrences


WRITERS = ('flush', 'statement', 'core', 'bulk')
WORKSPACE_READ_FIELDS = frozenset(FIELDS['workspace'])
EXCLUSIONS = [pytest.param(cls, exclusion, id=f'{cls.__name__}.{exclusion}')
              for _name, cls, _category, _scope, _shape, excluded in snapshots.snapshot_input_specs()
              if cls is not m.Workspace
              for exclusion in sorted(excluded)]
WORKSPACE_CASES = [pytest.param(field, writer, id=f'{writer}-Workspace.{field}')
                   for field in sorted(set(m.Workspace.__table__.columns.keys()) - WORKSPACE_READ_FIELDS)
                   for writer in WRITERS]


@pytest.fixture(scope='module', params=['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)])
def oracle_context(request, tmp_path_factory):
    if request.param == 'postgresql':
        harness = postgres_harness(request.getfixturevalue('postgres_worker_url'), request.getfixturevalue('postgres_template'))
    else:
        harness = sqlite_harness(tmp_path_factory.mktemp('exclusion-oracle'), request.getfixturevalue('database_template'))
    h = next(harness)
    try:
        h.policy({'kind': 'min_rest', 'hours': 1})
        published = []
        for first, last in [('2026-09-20', '2026-09-27'), ('2026-10-04', '2026-10-11')]:
            workspace = h.shift(h.workspace('Oracle external schedule', start=first, end=last),
                start=first + 'T08:00:00+03:00', end=first + 'T12:00:00+03:00')['workspace']
            published.append(h.publish(workspace)['workspace'])
        with transaction(h.app.state.engine, write=True) as db:
            identity = m.Identity(id=m.uid(), issuer='local-development', subject='oracle-' + m.uid(),
                                  display_name='Synthetic oracle identity', person_id=h.seed['people'][0])
            binding = m.RoleBinding(id=m.uid(), identity_id=identity.id, department_id=h.seed['department_id'],
                                    role='approver', starts_on=date(2020, 1, 1))
            pattern = m.AvailabilityPattern(id=m.uid(), department_id=h.seed['department_id'], person_id=h.seed['people'][1],
                weekdays=[6], start_time='09:00', end_time='10:00', starts_on=date(2026, 10, 1),
                status='active', created_by=h.seed['identities']['staff'])
            db.add_all([identity, binding, pattern])
            db.add_all(m.Department(organisation_id=h.seed['organisation_id'], name=f'Synthetic oracle department {n}') for n in (1, 2))
            db.add_all(m.ReviewRoute(department_id=h.seed['department_id'], name=f'Synthetic oracle review route {n}',
                purpose='swap', starts_on=date(2020, 1, 1), status='active',
                definition={'steps': [{'key': 'review', 'label': 'Review', 'role': 'approver'}]},
                created_by=h.seed['identities']['admin']) for n in (1, 2))
        meeting_type = event_type(h, blocks_duties=True, attendance='optional', rsvp=True,
                                  default_audience={'roles': ['approver']})
        meeting = event(h, meeting_type)
        other_meeting = event(h, meeting_type, start='2026-10-05T09:00:00+03:00', end='2026-10-05T10:00:00+03:00')
        occurrence_ids = {row['event_id']: row['id'] for row in occurrences(h)}
        occurrence_id = occurrence_ids[meeting['id']]
        with transaction(h.app.state.engine, write=True) as db:
            receipt = db.scalars(select(m.AnnouncementReceipt).where(m.AnnouncementReceipt.event_occurrence_id == occurrence_id,
                                m.AnnouncementReceipt.identity_id == identity.id)).first()
            if receipt is None:
                receipt = m.AnnouncementReceipt(identity_id=identity.id, person_id=h.seed['people'][0],
                                                event_occurrence_id=occurrence_id)
                db.add(receipt)
            receipt.response = 'going'
            db.flush()
            receipt_id = receipt.id
        target = h.shift(h.workspace('Oracle current schedule'), people=[])['workspace']
        with transaction(h.app.state.engine, write=True) as db:
            db.add_all(m.WorkspaceAccessVersion(workspace_id=target['id'], revision=revision,
                visibility='shared', purpose='alternative', actor_id=h.seed['identities']['admin'], reason='Synthetic oracle access')
                for revision in (20, 21))
        yield h, target['id'], {m.Organisation: h.seed['organisation_id'], m.Department: h.seed['department_id'],
            m.Workspace: target['id'], m.Person: h.seed['people'][0], m.Identity: identity.id, m.RoleBinding: binding.id,
            m.AvailabilityPattern: pattern.id, m.AnnouncementReceipt: receipt_id,
            m.EventOccurrence: occurrence_ids[other_meeting['id']]}
    finally:
        next(harness, None)


def fresh_currency(connection, target, organisation, *, workspace_scope=False, recorded=None):
    with Session(bind=connection, join_transaction_mode='create_savepoint') as db:
        workspace = db.get(m.Workspace, target)
        if workspace_scope:
            data = snapshots.domain_snapshot(db, workspace)
            content_revision = snapshots.currency_hint(db, workspace)['content_revision']
            reused = (snapshots.cheap_fingerprints(db, [workspace], checks={target: {'input_manifest': recorded}})
                      .get(target) if recorded is not None else None)
            return manifest(data, content_revision=0)['token'], (content_revision,), data['input_manifest'], reused
        token = snapshots.fingerprint(db, workspace)
        org = db.get(m.Organisation, organisation)
        return token, (org.input_revision, org.availability_revision), None, None


def runtime_values(defaults, key):
    schema = GenerationOptions.model_json_schema()['properties'][key]
    choices = [None, 0, 1, 2, 10, 20, 0.1, 0.5, '09:00', '10:00', '2026-10-04T10:00:00Z']
    for branch in (schema, *schema.get('anyOf', [])):
        choices.extend(branch.get('enum', []))
    valid = []
    for value in choices:
        try:
            normalized = GenerationOptions.model_validate({**defaults, key: value}).model_dump(mode='json')[key]
        except (ValueError, TypeError):
            continue
        if normalized not in valid:
            valid.append(normalized)
    assert len(valid) >= 2, f'NO_SAFE_SYNTHETIC_VALUES generation_defaults.{key}'
    return valid[:2]


def safe_values(db, cls, ident, exclusion):
    row = db.get(cls, ident)
    name, _, key = exclusion.partition('.')
    current = deepcopy(getattr(row, name))
    column = cls.__table__.c[name]
    if key:
        return [{**(current or {}), key: value} for value in runtime_values(current or {}, key)]
    if cls is m.Department and name == 'display_settings':
        return [{**(current or {}), 'autoschedule_animation': AutoscheduleAnimationSettings(mode=mode).model_dump(mode='json')}
                for mode in ('off', 'roll')]
    if column.foreign_keys:
        target = next(iter(column.foreign_keys)).column
        query = select(target)
        if target.table is m.WorkspaceAccessVersion.__table__:
            query = query.where(m.WorkspaceAccessVersion.workspace_id == ident)
        if target.table is m.Publication.__table__:
            query = query.where(m.Publication.workspace_id != ident)
        values = list(db.scalars(query.order_by(target).limit(3)))
        assert len(values) >= 2, f'NO_SAFE_SYNTHETIC_VALUES {cls.__name__}.{name}: fewer than two valid referenced identities'
        return values[:2]
    if isinstance(column.type, DateTime):
        return [datetime(2026, 10, day, 12, tzinfo=timezone.utc) for day in (2, 3)]
    if isinstance(column.type, Date):
        return [date(2026, 10, day) for day in (2, 3)]
    if isinstance(column.type, Integer):
        if name == 'policy_retire_approvals_required':
            return [2, 3]
        return [(current or 0) + delta for delta in (2, 3)]
    if isinstance(column.type, Boolean):
        return [True, False]
    if isinstance(column.type, JSON):
        if cls is m.Workspace and name == 'accepted_issues':
            return [[{'key': f'oracle-{n}', 'by': row.last_edited_by, 'at': m.now().isoformat(),
                      'reason': 'Synthetic accepted advisory'}] for n in (1, 2)]
        if cls is m.Department and name == 'swap_kinds_allowed':
            return [['exchange'], ['give']]
        if isinstance(current, list):
            return [[*deepcopy(current), {'oracle': n}] for n in (1, 2)]
        return [{**(current or {}), 'oracle': n} for n in (1, 2)]
    if isinstance(column.type, String):
        if name == 'announcement_send_time':
            return ['08:00', '09:00']
        if name == 'leave_reason_display':
            return ['none', 'details']
        if name == 'id':
            return [m.uid(), m.uid()]
        if cls is m.Workspace and name == 'status':
            return ['submitted', 'approved']
        if cls is m.Workspace and name == 'urgency':
            return ['urgent', 'routine']
        if name.endswith('fingerprint'):
            return [letter * 64 for letter in ('a', 'b')]
        if name == 'password_hash':
            return [hash_password(TEST_PASSWORD + suffix) for suffix in ('-a', '-b')]
        return ['oracle-a', 'oracle-b']
    pytest.fail(f'NO_SAFE_SYNTHETIC_VALUES {cls.__name__}.{name}: unsupported column type {type(column.type).__name__}')


def write_value(db, cls, ident, name, value, writer):
    changes = {name: deepcopy(value)}
    if cls is m.Identity and name in {'subject', 'subject_normalized'}:
        changes = {'subject': value, 'subject_normalized': value.casefold()}
    if writer == 'flush':
        row = db.get(cls, ident)
        for column, replacement in changes.items():
            setattr(row, column, replacement)
    elif writer in {'statement', 'core'}:
        target = cls.__table__ if writer == 'core' else cls
        column = target.c.id if writer == 'core' else cls.id
        db.execute(update(target).where(column == ident).values(**changes))
    elif name == 'id':
        db.execute(update(cls).values(id=bindparam('replacement_id')),
                   [{'id': ident, 'replacement_id': value}], execution_options={'synchronize_session': False})
    else:
        db.execute(update(cls), [{'id': ident, **changes}])
    db.commit()
    return value if name == 'id' else ident


def measure_values(context, cls, exclusion, writer, *, values=None, ident=None, fallback=False):
    h, target, rows = context
    assert cls in rows, f'NO_SAFE_SYNTHETIC_ROW {cls.__name__}: add a representative fixture for this registered reader'
    ident = rows[cls] if ident is None else ident
    name = exclusion.partition('.')[0]
    results = []
    with transaction(h.app.state.engine, write=True) as outer:
        connection = outer.connection()
        try:
            if fallback:
                with Session(bind=connection, join_transaction_mode='create_savepoint') as db:
                    db.get(m.Department, rows[m.Department]).operating_calendar_id = None
                    db.commit()
            if cls is m.AnnouncementReceipt and name == 'content_id':
                # A receipt belongs to either an event or a notice, never both.
                with Session(bind=connection, join_transaction_mode='create_savepoint') as db:
                    notices = [m.DepartmentContent(id=m.uid(), department_id=rows[m.Department],
                        name=f'Synthetic oracle notice {n}', starts_on=date(2026, 10, 1)) for n in range(3)]
                    db.add_all(notices)
                    db.flush()
                    ident, values = m.uid(), [notice.id for notice in notices[1:]]
                    db.add(m.AnnouncementReceipt(id=ident, content_id=notices[0].id,
                        identity_id=h.seed['identities']['staff']))
                    db.commit()
            with Session(bind=connection, join_transaction_mode='create_savepoint') as db:
                values = values if values is not None else safe_values(db, cls, ident, exclusion)
                if getattr(db.get(cls, ident), name) == values[0]:
                    values = list(reversed(values))
            assert values[0] != values[1], f'NO_DISTINCT_SYNTHETIC_VALUES {cls.__name__}.{exclusion}'
            for index, value in enumerate(values):
                workspace_scope = cls is m.Workspace
                before, revisions, recorded, _ = fresh_currency(connection, target, rows[m.Organisation],
                                                                  workspace_scope=workspace_scope)
                with Session(bind=connection, join_transaction_mode='create_savepoint') as db:
                    ident = write_value(db, cls, ident, name, value, writer)
                if cls is m.Workspace and name == 'id':
                    target = ident
                after, current, updated_manifest, reused = fresh_currency(connection, target, rows[m.Organisation],
                    workspace_scope=workspace_scope, recorded=recorded if name != 'id' else None)
                expected = list(revisions)
                if cls is m.Organisation and name in {'input_revision', 'availability_revision'}:
                    # A direct counter assignment is the baseline; detect any additional listener increment.
                    expected[('input_revision', 'availability_revision').index(name)] = value
                if workspace_scope and name == 'content_revision':
                    # A direct revision assignment changes the raw token but must not trigger a second stamp.
                    expected[0] = value
                with Session(bind=connection, join_transaction_mode='create_savepoint') as db:
                    actual = getattr(db.get(cls, ident), name)
                    assert actual == value or isinstance(actual, datetime) and actual.replace(tzinfo=timezone.utc) == value, (
                        f'WRITE_NOT_PERSISTED {cls.__name__}.{exclusion} {writer} value={index}')
                results.append({'field': f'{cls.__name__}.{exclusion}', 'writer': writer, 'value': index,
                    'before': before, 'after': after, 'fingerprint_equal': before == after,
                    'revisions': revisions, 'current': current, 'automatic_bump': tuple(expected) != current,
                    'raw_before': recorded['token'] if recorded else before,
                    'raw_after': updated_manifest['token'] if updated_manifest else after,
                    'reused_stored_fingerprint': reused,
                    'paired_subject_write': cls is m.Identity and name in {'subject', 'subject_normalized'}})
        finally:
            outer.rollback()
    return results


@pytest.mark.parametrize('writer', WRITERS)
def test_display_settings_full_fingerprint_oracle(oracle_context, writer, record_property):
    results = measure_values(oracle_context, m.Department, 'display_settings', writer)
    record_property('oracle', json.dumps(results))
    assert all(row['fingerprint_equal'] for row in results), results


@pytest.mark.parametrize('cls,exclusion', EXCLUSIONS)
@pytest.mark.parametrize('writer', WRITERS)
def test_every_declared_exclusion_has_fingerprint_and_revision_equivalence(oracle_context, cls, exclusion, writer, record_property):
    results = measure_values(oracle_context, cls, exclusion, writer)
    record_property('oracle', json.dumps(results))
    for row in results:
        assert row['fingerprint_equal'] and not row['automatic_bump'], row
        assert row['fingerprint_equal'] == (not row['automatic_bump']), row


@pytest.mark.parametrize('field,writer', WORKSPACE_CASES)
def test_workspace_metadata_preserves_own_input_currency(oracle_context, field, writer, record_property):
    results = measure_values(oracle_context, m.Workspace, field, writer)
    record_property('oracle', json.dumps(results))
    for row in results:
        assert row['fingerprint_equal'], row
        assert not row['automatic_bump'], row
        if field != 'content_revision':
            assert row['raw_before'] == row['raw_after'], row


@pytest.mark.parametrize('writer', WRITERS)
@pytest.mark.parametrize('cls,field,values', [
    (m.Department, 'operating_calendar_id', [None, 'restore_calendar']),
    (m.Organisation, 'timezone', ['UTC', 'America/New_York']),
    (m.Identity, 'disabled', [True, False]),
    (m.RoleBinding, 'role', ['coordinator', 'approver']),
    (m.AvailabilityPattern, 'end_time', ['11:00', '12:00']),
    (m.AnnouncementReceipt, 'response', ['not_going', 'going']),
    (m.AnnouncementReceipt, 'person_id', None),
    (m.AnnouncementReceipt, 'event_occurrence_id', None),
], ids=lambda value: value.__name__ if isinstance(value, type) else None)
def test_read_fields_change_full_fingerprint_and_revision(oracle_context, cls, field, values, writer, record_property):
    if cls is m.Department:
        with transaction(oracle_context[0].app.state.engine) as db:
            values = [None, db.get(m.Department, oracle_context[2][cls]).operating_calendar_id]
    if cls is m.AnnouncementReceipt and values is None:
        h, _target, rows = oracle_context
        with transaction(h.app.state.engine) as db:
            receipt = db.get(m.AnnouncementReceipt, rows[cls])
            alternate = rows[m.EventOccurrence] if field == 'event_occurrence_id' else None
            values = [alternate, getattr(receipt, field)]
    results = measure_values(oracle_context, cls, field, writer, values=values)
    record_property('oracle', json.dumps(results))
    assert all(not row['fingerprint_equal'] and row['automatic_bump'] for row in results), results


@pytest.mark.parametrize('writer', WRITERS)
@pytest.mark.parametrize('fallback', [False, True])
def test_weekend_fallback_fingerprint_and_revision_oracle(oracle_context, writer, fallback, record_property):
    results = measure_values(oracle_context, m.Department, 'weekend_days', writer,
                             values=[[0, 1], [1, 2]], fallback=fallback)
    record_property('oracle', json.dumps(results))
    assert all(row['fingerprint_equal'] == (not fallback) and row['automatic_bump'] == fallback for row in results), results
