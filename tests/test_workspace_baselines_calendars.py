"""Department working-day facts remain live without refreshing draft settings."""
import pytest

from apps.api import models as m, snapshots
from apps.api.db import transaction
from .test_department_quota_calendar import settings
from .test_quota_proration_api import partial_rota, rows_for
from .test_resources_calendars import calendar
from .test_workspace_baselines_live import BACKENDS


SETTINGS = pytest.mark.parametrize("setting", ["weekend_days", "operating_calendar_id"])


def fixture(h, setting):
    replacement = [5, 6] if setting == "weekend_days" else calendar(h, week=[
        {"weekday": 4, "windows": [{"start_time": "00:00", "end_time": "24:00"}]}])["id"]
    draft, policy, _, _ = partial_rota(h, selected=False)
    return draft, policy, replacement


@BACKENDS
@SETTINGS
def test_live_working_days_change_the_draft_prorated_minimum(request, backend, setting):
    h = request.getfixturevalue(backend)
    draft, policy, replacement = fixture(h, setting)
    settings(h, **{setting: replacement})
    checked = h.lifecycle(draft, "validate")["validation"]
    assert rows_for(checked, policy)["minimum"]["amount"] == (3 if setting == "weekend_days" else 6)


@BACKENDS
@SETTINGS
def test_live_working_days_change_the_draft_fingerprint(request, backend, setting):
    h = request.getfixturevalue(backend)
    draft, _, replacement = fixture(h, setting)
    before = h.detail(draft)["current_fingerprint"]
    settings(h, **{setting: replacement})
    assert h.detail(draft)["current_fingerprint"] != before


@BACKENDS
@SETTINGS
def test_live_working_days_do_not_require_a_refresh_decision(request, backend, setting):
    h = request.getfixturevalue(backend)
    draft, _, replacement = fixture(h, setting)
    settings(h, **{setting: replacement})
    preview = h.command("coordinator", "workspace.baseline.refresh.preview", {}, draft["id"], draft["revision"])
    assert not {row["source"] for row in preview["changes"]} & {"score", "auto", "display"}


@BACKENDS
@SETTINGS
def test_live_working_days_make_a_warm_draft_check_stale(request, backend, setting, monkeypatch):
    h = request.getfixturevalue(backend)
    draft, _, replacement = fixture(h, setting)
    draft = h.lifecycle(draft, "validate")["workspace"]
    assert h.detail(draft)["workspace"]["validation_current"]
    with transaction(h.app.state.engine) as db:
        workspace = db.get(m.Workspace, draft["id"])
        token = workspace.validation["fingerprint"]
        with monkeypatch.context() as warm:
            warm.setattr(snapshots, "domain_snapshot", lambda *a, **kw: pytest.fail("Warm check rebuilt its snapshot"))
            assert snapshots.fingerprints(db, [workspace])[workspace.id] == token
    settings(h, **{setting: replacement})
    assert not h.detail(draft)["workspace"]["validation_current"]
