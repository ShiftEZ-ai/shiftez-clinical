"""The dated person-row indexes stay aligned between models and migration."""
import importlib

from sqlalchemy.schema import CreateIndex

from apps.api import models as m


def test_person_window_indexes_match_migration():
    migration = importlib.import_module("migrations.versions.0142_person_window_indexes")
    expected = {
        "ix_availability_department_person_window": (m.Availability.__table__,
            ("department_id", "person_id", "start", "end", "retracted")),
        "ix_rotations_department_person_window": (m.Rotation.__table__,
            ("department_id", "person_id", "starts_on", "ends_on")),
        "ix_availability_patterns_department_person_window": (m.AvailabilityPattern.__table__,
            ("department_id", "person_id", "starts_on", "ends_on", "status")),
    }
    model_indexes = {index.name: index for table, _ in expected.values() for index in table.indexes}
    assert set(expected) <= set(model_indexes)
    for name, (table, columns) in expected.items():
        assert tuple(column.name for column in model_indexes[name].columns) == columns
        assert name in migration.__dict__["upgrade"].__code__.co_consts
        assert name in str(CreateIndex(model_indexes[name]).compile())
