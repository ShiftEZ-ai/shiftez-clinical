"""Guard the content revision that keeps a rota's scheduling check current."""
from collections import Counter, defaultdict
from contextlib import contextmanager

import pytest
from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session

from apps.api import models as m
from apps.api.db import transaction
from packages.domain.input_manifest import FIELDS
from . import test_be5_rota_adjustments as adjustments
from . import test_block_quotas as quotas
from . import test_p2_groups as groups
from . import test_schedule_overlaps as overlaps
from .test_requests_search import both  # noqa: F401


MANIFEST_FIELDS = (set(FIELDS['workspace']) - {'id', 'department_id'}) | {'starts_on', 'ends_on'}
TRACKED_FIELDS = MANIFEST_FIELDS | {'content_revision'}


@contextmanager
def workspace_revision_guard(engine):
    before, after = defaultdict(dict), defaultdict(dict)
    observed, violations = Counter(), []
    table = m.Workspace.__table__
    select_state = select(table.c.id, *(table.c[field] for field in sorted(TRACKED_FIELDS)))

    def states(db, ids):
        if not ids:
            return {}
        rows = db.connection().execute(select_state.where(table.c.id.in_(ids))).mappings()
        return {row['id']: row for row in rows}

    def started_flush(db, _context, _instances):
        if db.get_bind().engine is not engine:
            return
        tx = db.get_nested_transaction() or db.get_transaction()
        tracked = {row.id for row in db.dirty if isinstance(row, m.Workspace)
                   and any(inspect(row).attrs[field].history.has_changes() for field in TRACKED_FIELDS)}
        before[tx].update(states(db, tracked - before[tx].keys()))

    def flushed(db, _context):
        if db.get_bind().engine is not engine:
            return
        tx = db.get_nested_transaction() or db.get_transaction()
        for row in db.new:
            if isinstance(row, m.Workspace):
                before[tx].setdefault(row.id, None)
        after[tx] = states(db, before[tx].keys())

    def committed(db):
        if db.get_bind().engine is not engine:
            return
        tx = db.get_nested_transaction() or db.get_transaction()
        if tx.parent is not None:
            for workspace_id, original in before[tx].items():
                before[tx.parent].setdefault(workspace_id, original)
                after[tx.parent][workspace_id] = after[tx].get(workspace_id)
            return
        # A merge preview can restore temporary field changes before commit.
        for workspace_id, original in before[tx].items():
            current = after[tx].get(workspace_id)
            if original is None or current is None:
                continue
            changed = {field for field in MANIFEST_FIELDS if current[field] != original[field]}
            observed.update(changed)
            if changed and current['content_revision'] <= original['content_revision']:
                violations.append((workspace_id, sorted(changed), original['content_revision'], current['content_revision']))

    def ended(_db, tx):
        before.pop(tx, None)
        after.pop(tx, None)

    listeners = {'before_flush': started_flush, 'after_flush': flushed,
                 'after_commit': committed, 'after_transaction_end': ended}
    for name, callback in listeners.items():
        event.listen(Session, name, callback)
    try:
        yield observed
        assert not violations, f'Workspace manifest fields changed without advancing content_revision: {violations}'
    finally:
        for name, callback in listeners.items():
            event.remove(Session, name, callback)


@pytest.mark.parametrize(('scenario', 'minimum'), [
    pytest.param(adjustments.test_multiple_scopes_coexist_replace_by_scope_and_clear_only_the_selected_scope,
                 {'rule_adjustments': 1}, id='rule-adjustment'),
    pytest.param(quotas.test_rota_target_override_undo_review_publication_and_privacy,
                 {'target_overrides': 2}, id='target-override-and-undo'),
    pytest.param(overlaps.test_choosing_on_an_existing_draft_start_after_replace_from_and_undo,
                 {'starts_on': 3, 'replaces_publication_id': 1}, id='overlap-choices-and-undo'),
    pytest.param(groups.test_scoped_adjustment_merge_preserves_justification_and_rejection_returns_whole_rota,
                 {'rule_adjustments': 4}, id='merge-and-return'),
])
def test_planning_commands_advance_workspace_content_revision(both, scenario, minimum):
    with workspace_revision_guard(both.app.state.engine) as observed:
        scenario(both)
    assert observed >= Counter(minimum)


def test_workspace_revision_guard_rejects_unstamped_update(both):
    workspace = both.workspace()
    engine = both.app.state.engine
    with (
        pytest.raises(AssertionError, match='Workspace manifest fields changed without advancing content_revision'),
        workspace_revision_guard(engine),
    ):
        with transaction(engine, write=True) as db:
            db.get(m.Workspace, workspace['id']).origin = 'auto'
