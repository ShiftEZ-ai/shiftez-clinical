"""Rolled-back planning preparation; only exact current evidence may replay under the writer lock."""
import pickle
from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import event
from apps.api import models as m
from apps.api.errors import require
from apps.api.snapshots import snapshot_hash
from packages.domain.input_manifest import manifest

# These handlers have no external side effects; their database preparation always rolls back.
KINDS = {'workspace.create', 'workspace.rule_adjust', 'workspace.target_override', 'workspace.undo', 'workspace.redo',
         'workspace.publish', 'shift.create', 'shift.update', 'shift.action', 'shift.copy',
         'draft.move', 'draft.swap', 'series.expand', 'series.reconcile',
         'workspace.merge.preview', 'workspace.merge.apply',
         'publication.change', 'publication.change.undo', 'publication.changes.submit'}


def command_id(db):
    state = db.info.get('_planning_preparation') if db is not None else None
    if state is None:
        return m.uid()
    state['next_id'] += 1
    return str(uuid5(NAMESPACE_URL, state['seed'] + ':' + str(state['next_id'])))


def command_time(db):
    state = db.info.get('_planning_preparation') if db is not None else None
    return state['at'] if state else m.now()


def bind(db, plan, *, replay=False):
    db.info['_planning_preparation'] = {**plan, 'snapshots': plan.setdefault('snapshots', {}),
                                       'inputs': plan.setdefault('inputs', {}),
                                       'input_tokens': plan.setdefault('input_tokens', {}),
                                       'validation_baselines': plan.setdefault('validation_baselines', {}),
                                       'prepared_snapshots': plan.setdefault('prepared_snapshots', {}),
                                       'summary_cache': plan.setdefault('summary_cache', {}),
                                       'next_snapshot': 0,
                                       'pending_checks': [], 'next_id': 0, 'next_check': 0, 'replay': replay}
    def assign_id(_db, row):
        if isinstance(row, m.Workspace) and row.id is None:
            row.id = command_id(db)
    event.listen(db, 'after_attach', assign_id)


def restore_replay(plan):
    # A replay attempt owns each restored graph. If the transaction retries,
    # consumed entries fall back to the original detached bytes.
    plan['restored_snapshots'] = {index: pickle.loads(entry[1])
                                  for index, entry in plan.get('prepared_snapshots', {}).items()}
    plan['restored_checks'] = {index: pickle.loads(result)
                               for index, (_, result) in enumerate(plan['checks'])}


def command_compute(db, kind, data, compute, *, options=None, final=False, input_token=None, defer=False,
                    refresh_on_replay=False):
    state = db.info.get('_planning_preparation')
    if state is None:
        return compute()
    partial = data.get('_partial')
    key = snapshot_hash({'kind': kind, 'manifest': input_token or manifest(data)['token'], 'focus': sorted(data['focus_ids']),
        'options': options, 'quota_subjects': sorted(data['quota_subjects']) if 'quota_subjects' in data else None,
        'partial': {**{k: sorted(partial[k]) for k in ('shifts', 'people')},
                    'complete': partial.get('complete'), 'previous': partial.get('previous')} if partial else None})
    index = state['next_check']
    state['next_check'] += 1
    if index == len(state['checks']) + len(state['pending_checks']) and not state['replay']:
        if defer:
            state['pending_checks'].append((key, compute))
            return None
        raise ComputePreparation(key, compute, final=final, pending=state['pending_checks'])
    current = index < len(state['checks']) and key == state['checks'][index][0]
    if state['replay'] and refresh_on_replay and not current:
        return compute()
    require(current, 'STALE_INPUTS',
            'The rota or its scheduling inputs changed during preparation; refresh and apply the edit again', 409,
            {'recoverable': True})
    computed = state['checks'][index][1]
    if state.get('kind') == 'shift.update':
        restored = state.get('restored_checks') or {}
        return restored.pop(index) if index in restored else pickle.loads(computed)
    return deepcopy(computed)


def command_evaluate(db, data, **kwargs):
    from . import planning
    if check := db.info.get('_check_budget'):
        data = {**data, '_check_budget': check}
    return command_compute(db, 'validation', data, lambda: planning.evaluate(data, **kwargs), options=kwargs)


class ComputePreparation(Exception):
    def __init__(self, key, compute, *, final=False, pending=()):
        self.key, self.compute, self.final, self.pending = key, compute, final, pending


class RollbackPreparation(Exception):
    pass
