"""The directory field list has one source shared by the directory view."""
import inspect


def test_directory_fields_has_one_definition_and_shared_consumers(h):
    from apps.api.services import leavers

    directory = next(route.endpoint for route in h.app.routes if route.path == "/api/directory")
    directory_fields = inspect.getclosurevars(directory).nonlocals["DIRECTORY_FIELDS"]
    assert leavers.ANONYMISED_FIELDS is not directory_fields
