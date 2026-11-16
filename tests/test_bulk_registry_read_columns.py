"""A command input is a bulk read column only when its serialized record carries it."""
import pytest

from packages.contracts import schemas as s


@pytest.mark.parametrize("collection", sorted(s.BULK_COLLECTIONS))
def test_registry_read_columns_exclude_write_only_and_unserialized_contract_fields(collection):
    entry = s.BULK_COLLECTIONS[collection]
    if not entry["schema"]:
        return
    cls = getattr(s, entry["schema"])
    schema = cls.model_json_schema()
    schema = schema.get("$defs", {}).get(cls.__name__, schema)
    excluded = {name for name, field in cls.model_fields.items()
                if field.exclude or schema["properties"][name].get("writeOnly")}
    assert not excluded & entry["columns"].keys(), collection


@pytest.mark.parametrize("collection,names", [
    ("department_content", {"notify"}),
    ("policies", {"expected_policy_id", "expected_revision", "acknowledged_overlaps"}),
    ("scheduling_facts", {"expected_revision"}),
    ("policy_lines", {"mode", "measure", "period"}),
])
def test_input_only_fields_stay_in_the_write_contract_but_not_bulk_read_columns(collection, names):
    entry = s.BULK_COLLECTIONS[collection]
    assert names <= getattr(s, entry["schema"]).model_fields.keys()
    assert not names & entry["columns"].keys()
    if collection == "policy_lines":
        assert {"who", "where", "numbers", "starts_on", "ends_on"} <= entry["columns"].keys()
