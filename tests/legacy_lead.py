"""Explicit pre-governance fixtures; current API writes may never create legacy leadership."""
from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from apps.api.services.scoring_defaults import seed
from packages.contracts.schemas import TypeDefinition


def historical_type(h, value):
    value = {**value, 'definition': dict(value['definition'])}
    definition = TypeDefinition.model_validate(value['definition']).model_dump(mode='json')
    value['definition'].pop('lead', None)
    created = h.configure('assignment_types', value)['record']
    return set_historical_lead(h, created['id'], definition.get('lead'))


def set_historical_lead(h, type_id, lead):
    """Only simulate the historical import/maintenance path, never present-day operator authoring."""
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.AssignmentType, type_id)
        version_historical_lead(db, row, lead, h.seed['identities']['admin'])
        seed(db, row.department_id, types=[row], workload=False)
        return record(row)


def version_historical_lead(db, row, lead, actor_id):
    """Append a coherent imported revision without changing retained type evidence."""
    row.definition = TypeDefinition.model_validate({**row.definition, 'lead': lead}).model_dump(mode='json')
    row.revision += 1
    db.flush()
    db.add(m.DefinitionHistory(department_id=row.department_id, kind='assignment_types', object_id=row.id,
        object_revision=row.revision, snapshot=record(row), actor_id=actor_id,
        reason='Synthetic retained legacy leadership revision'))
    db.flush()
