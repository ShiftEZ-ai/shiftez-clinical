"""Named and open offers share one bounded backlog per proposer."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.db import transaction
from .governance_support import identity, transition
from .test_open_swaps import both, payload  # noqa: F401
from .test_staff_swaps import fixed_clock  # noqa: F401


@pytest.mark.parametrize("open_offer", [False, True], ids=["named", "open"])
def test_swap_limit_counts_named_and_open_outstanding_offers(both, open_offer):
    h = both
    identity(h, "staff_two", ["staff"], h.seed["people"][1])
    workspace = h.workspace(end="2026-10-31")
    for day in range(26):
        start = datetime(2026, 10, 4, 5, tzinfo=timezone.utc) + timedelta(days=day)
        workspace = h.shift(workspace, start.isoformat(), (start + timedelta(hours=4)).isoformat())["workspace"]
    publication = h.publish(workspace)["publication_id"]
    snapshot = h.get("coordinator", "/api/publications/" + publication)["snapshot"]
    duties = [shift["participants"][0]["id"] for shift in snapshot["shifts"]]
    with transaction(h.app.state.engine, write=True) as db:
        common = dict(kind="give", department_id=h.seed["department_id"], publication_id=publication,
            proposer_id=h.seed["identities"]["staff"], proposer_person_id=h.seed["people"][0],
            expires_at=m.now() + timedelta(days=1), reason="Synthetic outstanding invitation")
        for index, duty in enumerate(duties[:24]):
            db.add(m.SwapRequest(**common, first_assignment_id=duty, open_offer=index % 2 == 0,
                recipient_person_id=None if index % 2 == 0 else h.seed["people"][1]))
        db.add(m.SwapRequest(**common, first_assignment_id=duties[24], open_offer=True, status="withdrawn"))
        db.add(m.SwapRequest(**{**common, "expires_at": m.now() - timedelta(seconds=1)},
            first_assignment_id=duties[24], open_offer=True))
        db.add(m.SwapRequest(**{**common, "proposer_id": h.seed["identities"]["staff_two"],
            "proposer_person_id": h.seed["people"][1]}, first_assignment_id=duties[24],
            recipient_person_id=h.seed["people"][0]))
    recipient = None if open_offer else h.seed["people"][1]
    value = {**payload(publication, duties[24], "give"), "recipient_person_id": recipient}
    row = h.command("staff", "swap.offer", value)["swap"]
    with transaction(h.app.state.engine) as db:
        models = (m.SwapRequest, m.SwapEvent, m.Outbox, m.Job)
        before = [db.scalar(select(func.count()).select_from(model)) for model in models]
    value["assignment_id"] = duties[25]
    assert h.command("staff", "swap.offer", value, status=409)["error"]["code"] == "SWAP_LIMIT"
    with transaction(h.app.state.engine) as db:
        assert [db.scalar(select(func.count()).select_from(model)) for model in models] == before
    transition(h, row, "withdraw")
    assert h.command("staff", "swap.offer", value)["swap"]["status"] == "offered"
