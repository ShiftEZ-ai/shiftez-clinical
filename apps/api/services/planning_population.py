"""Derive the rota audience from active Apply receipts, without loading job blobs."""
from sqlalchemy import select
from apps.api import models as m
from packages.domain.generation_scope import union_population


def applied_population(db, workspace, data):
    keys = ('generation_scope', 'planning_population', 'generation_session_ids')
    columns = [*(m.Job.read_metadata['input_snapshot', key] for key in keys),
               m.Job.read_metadata['result', 'reconciliation', 'population_recorded'],
               m.Job.read_metadata['result', 'reconciliation', 'population_people']]
    keys += ('population_recorded', 'population_people')
    rows = db.execute(select(*columns).where(
        m.Job.workspace_id == workspace.id, m.Job.kind == 'generation', m.Job.status == 'applied')
        .execution_options(yield_per=100))
    first = next(rows, None)
    if first is None:
        return ...
    from itertools import chain
    try:
        return union_population(data, (dict(zip(keys, row)) for row in chain([first], rows)))
    finally:
        rows.close()
