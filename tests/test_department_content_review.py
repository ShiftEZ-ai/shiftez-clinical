"""Links cannot require acknowledgement or displace required notices; departed receipts stay idle."""
from datetime import date, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import retry_write, transaction
from apps.api.services import announcements, audiences
from apps.worker import main as worker
from tests.outbox_support import dispatch_batch_after_undo, process_one_after_undo as process_one
from packages.contracts.schemas import DepartmentContentInput
from .test_announcement_receipts import notice, receipts


BACKENDS = ['h', pytest.param('pg', marks=pytest.mark.postgres)]


def link_value(**changes):
    return {'name': 'Synthetic department link', 'kind': 'link', 'url': 'https://example.test/department',
        'starts_on': '2020-01-01', 'audience': {'roles': ['staff']}, **changes}


def legacy_required_links(h):
    with transaction(h.app.state.engine, write=True) as db:
        links = [m.DepartmentContent(department_id=h.seed['department_id'], name=f'A legacy link {index}',
            kind='link', url=f'https://example.test/legacy/{index}', starts_on=date(2020, 1, 1),
            audience={'roles': ['staff']}, importance='must_acknowledge') for index in range(4)]
        db.add_all(links)
        db.flush()
        ids = [row.id for row in links]
    required = notice(h, {'roles': ['staff']}, name='Z required Home-only announcement', importance='must_acknowledge')
    return ids, required


@pytest.mark.parametrize('importance', ['normal', 'important', 'must_acknowledge'])
def test_shared_contract_allows_only_announcement_acknowledgements(importance):
    value = link_value(importance=importance)
    if importance == 'must_acknowledge':
        with pytest.raises(ValidationError):
            DepartmentContentInput.model_validate(value)
        assert DepartmentContentInput.model_validate({**value, 'kind': 'announcement'}).importance == importance
    else:
        assert DepartmentContentInput.model_validate(value).importance == importance


@pytest.mark.parametrize('backend', BACKENDS)
def test_link_create_and_update_reject_required_acknowledgement_without_changing_the_record(request, backend):
    h = request.getfixturevalue(backend)
    invalid = link_value(importance='must_acknowledge')
    reply = h.configure('department_content', invalid, role='coordinator', status=422)
    assert reply['error']['code'] == 'SCHEMA_INVALID'
    row = h.configure('department_content', link_value(), role='coordinator')['record']
    reply = h.configure('department_content', invalid, row['id'], row['revision'], role='coordinator', status=422)
    assert reply['error']['code'] == 'SCHEMA_INVALID'
    current = next(item for item in h.get('coordinator', '/api/catalogues')['department_content'] if item['id'] == row['id'])
    assert current['revision'] == row['revision'] and current['importance'] == 'normal'
    for importance in ('important', 'normal'):
        current = h.configure('department_content', link_value(importance=importance),
            current['id'], current['revision'], role='coordinator')['record']
        assert current['importance'] == importance and current['kind'] == 'link'


@pytest.mark.parametrize('backend', BACKENDS)
def test_legacy_required_links_do_not_displace_a_required_home_only_announcement(request, backend):
    h = request.getfixturevalue(backend)
    links, required = legacy_required_links(h)
    first = h.get('staff', '/api/content?page_size=4')
    assert first['total'] == 5 and len(first['items']) == 4
    assert first['items'][0]['id'] == required['id']
    assert first['items'][0]['acknowledgement_required'] is True
    all_rows = h.get('staff', '/api/content?page_size=5')['items']
    assert {item['id'] for item in all_rows if item['kind'] == 'link'} == set(links)
    assert all(item['acknowledgement_required'] is False and item['receipt'] is None
        for item in all_rows if item['kind'] == 'link')
    with transaction(h.app.state.engine) as db:
        assert list(db.scalars(select(m.Outbox.id))) == []


@pytest.mark.parametrize('backend', BACKENDS)
@pytest.mark.parametrize('importance', ['normal', 'must_acknowledge'])
def test_departed_pending_recipient_does_not_reload_empty_audiences_or_consume_reminder_cooldown(request, backend, importance, monkeypatch):
    h = request.getfixturevalue(backend)
    row = notice(h, {'roles': ['staff']}, importance=importance)
    engine = h.app.state.engine
    process_one(engine)
    with transaction(engine, write=True) as db:
        day = announcements.today(db, h.seed['department_id'])
        db.get(m.Department, h.seed['department_id']).announcement_auto_reminder_days = 1
        binding = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == h.seed['identities']['staff'],
            m.RoleBinding.role == 'staff'))
        binding.revoked = True
        binding_id = binding.id
        original_outbox = set(db.scalars(select(m.Outbox.id)))
        original_receipts = list(db.execute(select(m.AnnouncementReceipt.id, m.AnnouncementReceipt.revision,
            m.AnnouncementReceipt.delivered_at, m.AnnouncementReceipt.read_at, m.AnnouncementReceipt.acknowledged_at)
            .where(m.AnnouncementReceipt.content_id == row['id'])))
        assert len(original_receipts) == 1 and original_receipts[0].read_at is None
        original_reminded = db.get(m.DepartmentContent, row['id']).last_reminded_at
    assert receipts(h, row['id'])['receipts'][0]['in_audience'] is False
    local_day = [day]
    monkeypatch.setattr(announcements, 'today', lambda db, department_id: local_day[0])
    contexts, evaluations, signals = [], [], []
    recipient_context, recipients = audiences.recipient_context, audiences.recipients

    def context(*args, **kwargs):
        contexts.append(local_day[0])
        return recipient_context(*args, **kwargs)

    def audience(*args, **kwargs):
        evaluations.append(local_day[0])
        return recipients(*args, **kwargs)

    monkeypatch.setattr(audiences, 'recipient_context', context)
    monkeypatch.setattr(audiences, 'recipients', audience)
    monkeypatch.setattr(worker.live, 'signal', lambda db, department_id, **change: signals.append(department_id))
    assert retry_write(engine, dispatch_batch_after_undo) == set()
    assert contexts == evaluations == [day]
    for _ in range(2):
        assert retry_write(engine, dispatch_batch_after_undo) == set()
    assert contexts == evaluations == [day]
    assert signals == []
    with transaction(engine) as db:
        content = db.get(m.DepartmentContent, row['id'])
        assert content.reminder_empty_checked_on == day
        assert content.last_reminded_at == original_reminded
        assert set(db.scalars(select(m.Outbox.id))) == original_outbox
        assert list(db.execute(select(m.AnnouncementReceipt.id, m.AnnouncementReceipt.revision,
            m.AnnouncementReceipt.delivered_at, m.AnnouncementReceipt.read_at, m.AnnouncementReceipt.acknowledged_at)
            .where(m.AnnouncementReceipt.content_id == row['id']))) == original_receipts
    local_day[0] += timedelta(days=1)
    assert retry_write(engine, dispatch_batch_after_undo) == set()
    assert contexts == evaluations == [day, local_day[0]]
    assert signals == []
    with transaction(engine, write=True) as db:
        content = db.get(m.DepartmentContent, row['id'])
        assert content.reminder_empty_checked_on == local_day[0]
        assert content.last_reminded_at == original_reminded
        db.get(m.RoleBinding, binding_id).revoked = False
    reminder = h.command('coordinator', 'department_content.remind', {'reason': 'Recipient rejoined; remind now'},
        row['id'], row['revision'])
    assert reminder['reminded'] == 1 and reminder['record']['last_reminded_at']


def test_announcement_edit_rechecks_an_empty_reminder_without_waiting_another_day(h):
    row = notice(h, {'roles': ['staff']})
    engine = h.app.state.engine
    with transaction(engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).announcement_auto_reminder_days = 1
        db.get(m.DepartmentContent, row['id']).reminder_empty_checked_on = announcements.today(db, h.seed['department_id'])
    updated = h.configure('department_content', {'name': row['name'], 'description': 'New audience',
        'starts_on': row['starts_on'], 'audience': {'everyone': True}}, row['id'], row['revision'], role='coordinator')['record']
    assert updated['reminder_empty_checked_on'] is None
    assert updated['last_reminded_at'] is None
    assert retry_write(engine, dispatch_batch_after_undo) == {h.seed['department_id']}
    with transaction(engine) as db:
        assert db.get(m.DepartmentContent, row['id']).last_reminded_at is not None
