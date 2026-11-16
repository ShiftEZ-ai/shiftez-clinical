"""Removing duplicate draft enrichment preserves current display facts and issued evidence."""
from copy import deepcopy

import pytest

from apps.api import models as m
from apps.api.db import transaction
from .test_workspace_baselines_live import BACKENDS


@BACKENDS
@pytest.mark.parametrize("published", [False, True], ids=["draft", "published"])
def test_people_display_facts_survive_enrichment_cleanup_without_rewriting_publication(request, backend, published):
    h = request.getfixturevalue(backend)
    workspace = h.shift(h.workspace("People enrichment contract"))["workspace"]
    if published:
        result = h.publish(workspace)
        workspace = result["workspace"]
        with transaction(h.app.state.engine) as db:
            issued = deepcopy(db.get(m.Publication, result["publication_id"]).snapshot)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, h.seed["people"][0]).display_name = "Updated synthetic display name"
    assert h.snapshot(workspace)["people"][h.seed["people"][0]]["display_name"] == "Updated synthetic display name"
    if published:
        with transaction(h.app.state.engine) as db:
            assert db.get(m.Publication, result["publication_id"]).snapshot == issued
