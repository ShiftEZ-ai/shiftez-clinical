"""Guard snapshot history that relies on its source row for invalidation."""
from collections import defaultdict
from contextlib import contextmanager

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from apps.api import models as m
from apps.api.db import transaction
from . import test_bulk_fact_transition_limits as bulk_facts
from . import test_overtime_volunteer as overtime
from . import test_rule_catalogue_v2_api as conversions
from . import test_scheduling_facts_api as facts
from .test_requests_search import both  # noqa: F401


HISTORY_KINDS = {m.SchedulingFact: 'scheduling_fact', m.CatalogueConversion: 'catalogue_conversion'}


@contextmanager
def history_write_guard(engine):
    writes, histories = defaultdict(dict), defaultdict(list)
    observed, unpaired, older = set(), [], []

    def flushed(db, _context):
        if db.get_bind().engine is not engine:
            return
        tx = db.get_nested_transaction() or db.get_transaction()
        for row in db.new | db.dirty:
            if type(row) in HISTORY_KINDS and (row in db.new or db.is_modified(row, include_collections=False)):
                writes[tx][HISTORY_KINDS[type(row)], row.id] = row.revision
        for row in db.deleted:
            if type(row) in HISTORY_KINDS:
                writes[tx][HISTORY_KINDS[type(row)], row.id] = None
        for row in db.new:
            if isinstance(row, m.DefinitionHistory) and row.kind in HISTORY_KINDS.values():
                histories[tx].append((row.kind, row.object_id, row.object_revision, row.batch_id is not None))

    def committed(db):
        if db.get_bind().engine is not engine:
            return
        tx = db.get_nested_transaction() or db.get_transaction()
        if tx.parent is not None:
            # Successful savepoints belong to the enclosing transaction; rollbacks discard their writes.
            writes[tx.parent].update(writes[tx])
            histories[tx.parent].extend(histories[tx])
        else:
            # The stamping bump is per transaction.
            for kind, object_id, history_revision, batched in histories[tx]:
                observed.add((kind, batched))
                revision = writes[tx].get((kind, object_id))
                if revision is None:
                    unpaired.append((kind, object_id, history_revision))
                elif revision < history_revision:
                    older.append((kind, object_id, history_revision, revision))

    def ended(_db, tx):
        writes.pop(tx, None)
        histories.pop(tx, None)

    event.listen(Session, 'after_flush', flushed)
    event.listen(Session, 'after_commit', committed)
    event.listen(Session, 'after_transaction_end', ended)
    try:
        yield observed
        assert not unpaired, f'History without a matching object write in its transaction: {unpaired}'
        assert not older, f'History is newer than the committed object revision: {older}'
    finally:
        event.remove(Session, 'after_transaction_end', ended)
        event.remove(Session, 'after_commit', committed)
        event.remove(Session, 'after_flush', flushed)


def convert_personal_limit(h):
    h.policy({'kind': 'max_duties', 'maximum': 4, 'window': 'week'}, {'person_id': h.seed['people'][0]})
    change = conversions.proposed(h)
    active = h.command('approver', 'rule.catalogue.activate', {'reason': 'Review the department conversion'},
                       change['id'], change['revision'])['record']
    assert active['plan']['scheduling_facts']
    h.command('approver', 'rule.catalogue.undo', {'reason': 'Restore the original rules'},
              active['id'], active['revision'])


@pytest.mark.parametrize(('scenario', 'expected'), [
    pytest.param(facts.test_fact_waits_for_independent_decision_then_undo_restores_predecessor,
                 {('scheduling_fact', False)}, id='fact'),
    pytest.param(bulk_facts.test_bulk_fact_chain_inverse_binds_predecessor_revision_after_an_earlier_inverse,
                 {('scheduling_fact', False), ('scheduling_fact', True)}, id='bulk-fact-chain'),
    pytest.param(convert_personal_limit, {('catalogue_conversion', False), ('scheduling_fact', False)}, id='conversion'),
    pytest.param(conversions.test_stale_inputs_decline_and_idempotency,
                 {('catalogue_conversion', False)}, id='conversion-decline'),
    pytest.param(lambda h: overtime.test_staff_can_withdraw_until_submission_and_undo_restores_previous_state(h, True),
                 {('scheduling_fact', False)}, id='overtime-withdraw-restore'),
    pytest.param(lambda h: overtime.test_bulk_decide_and_undo_keep_all_overtime_fact_effects_atomic(h, 'approve'),
                 {('scheduling_fact', False)}, id='overtime-batch'),
])
def test_scheduling_history_is_written_with_its_source(both, scenario, expected):
    with history_write_guard(both.app.state.engine) as observed:
        scenario(both)
    assert expected <= observed


@pytest.mark.parametrize(('write_object', 'message'), [
    pytest.param(False, 'History without a matching object write', id='history-only'),
    pytest.param(True, 'History is newer than the committed object revision', id='future-revision'),
])
def test_scheduling_history_guard_rejects_invalid_inserts(both, write_object, message):
    engine = both.app.state.engine
    with pytest.raises(AssertionError, match=message), history_write_guard(engine):
        fact = both.command('coordinator', 'person.workload.propose', facts.workload(both))['fact']
        with transaction(engine, write=True) as db:
            if write_object:
                db.get(m.SchedulingFact, fact['id']).revision += 1
            db.add(m.DefinitionHistory(department_id=both.seed['department_id'], kind='scheduling_fact',
                object_id=fact['id'], object_revision=fact['revision'] + 2, snapshot=fact,
                actor_id=both.seed['identities']['approver'], reason='Probe an invalid history write'))
