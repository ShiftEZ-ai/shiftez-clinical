"""Pre-existing email work must still respect a save made without notification."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.worker import email_delivery
from .test_email_delivery import configuration, dispatch, evidence, queued, source


def disable_notification(h, parent_id):
    with transaction(h.app.state.engine, write=True) as db:
        parent = db.get(m.Outbox, parent_id)
        parent.payload = {**parent.payload, "notify_inbox": False}


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_legacy_nonnotifying_announcement_cannot_queue_email(request, database):
    h = request.getfixturevalue(database)
    configuration(h)
    _, parent_id = source(h)
    disable_notification(h, parent_id)
    assert queued(h, parent_id) == 0
    assert evidence(h) == []
    with transaction(h.app.state.engine) as db:
        assert email_delivery._source(db, db.get(m.Outbox, parent_id)) is None


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_already_queued_nonnotifying_announcement_email_is_ineligible(request, database, monkeypatch):
    h = request.getfixturevalue(database)
    configuration(h)
    _, parent_id = source(h)
    assert queued(h, parent_id) == 1  # Old parents without the flag retain their actual-send semantics.
    disable_notification(h, parent_id)
    sent = []
    monkeypatch.setattr(email_delivery, "send", lambda db, delivery, *args: sent.append(delivery.address))
    dispatch(h)
    assert sent == []
    assert [(state, code, attempts) for state, code, _, attempts in evidence(h)] == [
        ("failed", "STALE_EMAIL_SOURCE", 0)]
    with transaction(h.app.state.engine) as db:
        parent = db.get(m.Outbox, parent_id)
        assert email_delivery._source(db, parent) is None
        with pytest.raises(email_delivery.DeliveryError, match="STALE_EMAIL_SOURCE"):
            delivery = db.scalar(select(m.EmailDelivery))
            email_delivery._eligible(db, delivery, parent, contexts={}, identity_ids={delivery.identity_id})
