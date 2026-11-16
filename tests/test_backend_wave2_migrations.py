"""The backend integration has one forward-only head and an explicit projection backfill review."""
from sqlalchemy import inspect

from apps.api import models as m
from apps.api.schema import supported_revision
from migrations import safety
from scripts.manage import alembic_config
from alembic.script import ScriptDirectory


def test_backend_read_backfill_is_detected_and_declared():
    migration = safety.VERSIONS / '0117_backend_read_indexes.py'
    assert safety.findings(migration)
    assert safety.marker(migration) and not safety.unreviewed(migration)
    assert safety.needs_confirmation(migration)


def test_occurrence_index_is_additive_and_present_in_migrated_schema(h):
    migration = safety.VERSIONS / '0118_shift_occurrence_index.py'
    scripts = ScriptDirectory.from_config(alembic_config('sqlite://'))
    assert '0118_shift_occurrence_index' in {revision.revision for revision in scripts.iterate_revisions(supported_revision(), 'base')}
    assert safety.findings(migration) == []
    indexes = {index['name']: index['column_names'] for index in inspect(h.app.state.engine).get_indexes('shifts')}
    assert indexes['ix_shifts_occurrence_on'] == ['occurrence_on']
    assert any(index.name == 'ix_shifts_occurrence_on' and list(index.columns.keys()) == ['occurrence_on']
               for index in m.Shift.__table__.indexes)
