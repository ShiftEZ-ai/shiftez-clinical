"""Publication export availability describes issued evidence, including personal empty reports."""
import copy
import csv
import io

import pytest
from sqlalchemy import event, inspect, select

from apps.api import models as m
from apps.api.db import transaction
from .test_block_quotas import quota


@pytest.fixture(params=["h", pytest.param("pg", marks=pytest.mark.postgres)], ids=["sqlite", "postgres"])
def both(request):
    return request.getfixturevalue(request.param)


def test_publication_metadata_matches_issued_reports_without_loading_snapshots(both):
    h = both
    legacy = h.publish(h.shift(h.workspace("Before commitments"))["workspace"])["publication_id"]
    # The staff member is on the rota but has no commitment rows: their issued CSV is still available.
    policy = quota(h, scope={"person_id": h.seed["people"][1]}, comparison="target", amount=2, severity="advisory")
    issued = h.publish(h.shift(h.workspace("With commitments", "2026-10-11", "2026-10-18"),
                               start="2026-10-11T08:00:00+03:00", end="2026-10-11T12:00:00+03:00")["workspace"])["publication_id"]
    with transaction(h.app.state.engine) as db:
        original = {p.id: copy.deepcopy(p.snapshot) for p in db.scalars(select(m.Publication))}

    def assert_deferred(publication, context):
        assert {"snapshot", "validation"} <= inspect(publication).unloaded

    def reject_snapshot_refresh(publication, context, attributes):
        assert not {"snapshot", "validation"}.intersection(attributes or ())

    # Reading availability must not hydrate full issued snapshots or checks, even for team roles.
    event.listen(m.Publication, "load", assert_deferred)
    event.listen(m.Publication, "refresh", reject_snapshot_refresh)
    try:
        for role in ("admin", "coordinator", "approver", "auditor", "staff"):
            rows = h.get(role, "/api/publications")["publications"]
            assert {p["id"]: p["has_quota_report"] for p in rows} == {legacy: False, issued: True}
            assert all(type(p["has_quota_report"]) is bool for p in rows)
            assert {p["name"] for p in rows} == {"Before commitments", "With commitments"}
            assert all(not {"snapshot", "validation", "quota_report"}.intersection(p) for p in rows)
        h.get("credentialer", "/api/publications", status=403)
    finally:
        event.remove(m.Publication, "load", assert_deferred)
        event.remove(m.Publication, "refresh", reject_snapshot_refresh)

    for role in ("coordinator", "staff"):
        unavailable = h.client(role).get("/api/reports/" + legacy + "?format=quotas")
        assert unavailable.status_code == 404
        assert unavailable.json()["error"]["code"] == "QUOTA_REPORT_NOT_ISSUED"
        available = h.client(role).get("/api/reports/" + issued + "?format=quotas")
        assert available.status_code == 200
        assert len(list(csv.DictReader(io.StringIO(available.text)))) == (0 if role == "staff" else 1)

    h.command("approver", "policy.retire", {"reason": "Synthetic retirement after publication"}, policy["id"], policy["revision"])
    other = h.publish(h.shift(h.workspace("Other staff only", "2026-10-18", "2026-10-25"),
                              start="2026-10-18T08:00:00+03:00", end="2026-10-18T12:00:00+03:00",
                              people=[h.seed["people"][1]])["workspace"])["publication_id"]
    assert {p["id"]: p["has_quota_report"] for p in h.get("staff", "/api/publications")["publications"]} == {
        legacy: False, issued: True}
    assert {p["id"]: p["has_quota_report"] for p in h.get("coordinator", "/api/publications")["publications"]} == {
        legacy: False, issued: True, other: False}
    with transaction(h.app.state.engine) as db:
        assert {p.id: p.snapshot for p in db.scalars(select(m.Publication).where(m.Publication.id.in_(original)))} == original
