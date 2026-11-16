"""Validity changes are named in the existing draft findings until refresh."""
import pytest

from apps.api import models as m
from apps.api.db import transaction
from .test_workspace_baselines_live import BACKENDS, SOURCES, FIELDS, change, target


def notices(h, draft):
    checked = h.lifecycle(draft, "validate")
    return [row for row in checked["validation"]["findings"] if row["code"] == "CATALOGUE_VALIDITY_CHANGED"]


@BACKENDS
@SOURCES
@FIELDS
@pytest.mark.parametrize("direction", ["retired", "re-activated"])
def test_draft_names_a_validity_toggle_and_clears_it_after_refresh(request, backend, source, field, closed, direction):
    h = request.getfixturevalue(backend)
    cls, ident, _ = target(h, source)
    with transaction(h.app.state.engine) as db:
        row = db.get(cls, ident)
        original, name = getattr(row, field), row.name
    change(h, cls, ident, field, closed if direction == "re-activated" else original)
    draft = h.shift(h.workspace("Named live validity changes"), location=1)["workspace"]
    label = {"types": "Duty type", "services": "Service", "locations": "Place", "location_types": "Place type"}[source]
    change(h, cls, ident, field, original if direction == "re-activated" else closed)
    appeared = {row["message"] for row in notices(h, draft)}
    draft = h.refresh_draft_baseline(draft)
    cleared = {row["message"] for row in notices(h, draft)}
    assert (appeared, cleared) == ({f"{label} {name} {direction} since this draft was made"}, set())


@BACKENDS
def test_an_unchanged_draft_has_no_validity_change_finding(request, backend):
    h = request.getfixturevalue(backend)
    assert not notices(h, h.shift(h.workspace("Unchanged validity"))["workspace"])


def test_catalogue_notices_have_distinct_issue_keys_for_each_record_and_kind():
    from packages.domain.rota_summary import issue_key
    findings = [{"code": "CATALOGUE_VALIDITY_CHANGED", "detail": {"catalogue": source, "record_id": ident}}
                for source, ident in (("locations", "one"), ("locations", "two"), ("types", "one"))]
    assert len({issue_key(finding) for finding in findings}) == 3
