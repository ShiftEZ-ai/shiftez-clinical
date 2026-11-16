"""Rolled-back planning preparation; only exact current evidence may replay under the writer lock."""
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
                                       'next_id': 0, 'next_check': 0, 'replay': replay}
    def assign_id(_db, row):
        if isinstance(row, m.Workspace) and row.id is None:
            row.id = command_id(db)
    event.listen(db, 'after_attach', assign_id)


def command_compute(db, kind, data, compute, *, options=None, final=False):
    state = db.info.get('_planning_preparation')
    if state is None:
        return compute()
    partial = data.get('_partial')
    key = snapshot_hash({'kind': kind, 'manifest': manifest(data)['token'], 'focus': sorted(data['focus_ids']),
        'options': options, 'quota_subjects': sorted(data['quota_subjects']) if 'quota_subjects' in data else None,
        'partial': {**{k: sorted(partial[k]) for k in ('shifts', 'people')},
                    'complete': partial.get('complete'), 'previous': partial.get('previous')} if partial else None})
    index = state['next_check']
    state['next_check'] += 1
    if index == len(state['checks']) and not state['replay']:
        raise ComputePreparation(key, compute, final=final)
    require(index < len(state['checks']) and key == state['checks'][index][0], 'STALE_INPUTS',
            'The rota or its scheduling inputs changed during preparation; refresh and apply the edit again', 409,
            {'recoverable': True})
    return deepcopy(state['checks'][index][1])


def command_evaluate(db, data, **kwargs):
    from . import planning
    if check := db.info.get('_check_budget'):
        data = {**data, '_check_budget': check}
    return command_compute(db, 'validation', data, lambda: planning.evaluate(data, **kwargs), options=kwargs)


class ComputePreparation(Exception):
    def __init__(self, key, compute, *, final=False):
        self.key, self.compute, self.final = key, compute, final


class RollbackPreparation(Exception):
    pass
