"""Feedback context is bounded navigation metadata, shared with the generated client."""
import pytest
from pydantic import ValidationError
from packages.contracts import schemas as s
from scripts.generate_contracts import render


@pytest.mark.parametrize("route", ["#home", "#schedule/week/2026-10-01?edit=private", "/home", "#/home"])
def test_app_hash_routes_are_supported(route):
    assert s.FeedbackCreate(kind="question", message="Where is this?", route=route).route == route


@pytest.mark.parametrize("route", ["https://example.com", "//example.com", "#//example.com", "/home\\unsafe"])
def test_external_routes_are_refused(route):
    with pytest.raises(ValidationError):
        s.FeedbackCreate(kind="question", message="Where?", route=route)


def test_recent_routes_are_optional_and_accept_ten_bounded_paths():
    assert s.FeedbackCreate(kind="question", message="Where?", route="#home").recent_routes == []
    routes = ["#home"] * 9 + ["#/" + "x" * 1998]
    value = s.FeedbackCreate(kind="question", message="Where?", route="#home", recent_routes=routes)
    assert value.recent_routes == routes


@pytest.mark.parametrize("routes", [
    ["#home"] * 11, ["#/" + "x" * 1999], [""], [1], [True], [None], [{"route": "#home"}],
    None, "#home", ["https://example.com"], ["//example.com"], ["#//example.com"],
    ["/home\\unsafe"], ["#home\nprivate"],
])
def test_recent_routes_refuse_unbounded_nonstring_or_unsafe_items(routes):
    with pytest.raises(ValidationError):
        s.FeedbackCreate(kind="question", message="Where?", route="#home", recent_routes=routes)


@pytest.mark.parametrize("state", [{"records": []}, {"filters": {"password": "private"}},
                                   {"filters": ["x" * 16384]}, {"filters": float("nan")}])
def test_record_contents_and_unbounded_state_are_refused(state):
    with pytest.raises(ValidationError):
        s.FeedbackCreate(kind="problem", message="Failed", route="#home", view_state=state)


@pytest.mark.parametrize("state", [
    {"filters": {"people": ["synthetic-person-id"], "status": ["new"]}, "sort": ["status", "asc"]},
    {"active_view": "week", "grouping": "people", "selection_count": 0, "open_panel": None},
    {"active_view": "page/tab:list", "group_by": "status", "sort": ["kind", "asc"], "layout": "table"},
    {"filters": [{"field": "status", "operator": "in", "values": ["new", "triaged"]}]},
])
def test_navigation_choices_accept_shared_filters_without_confusing_them_with_records(state):
    assert s.FeedbackCreate(kind="question", message="Where?", route="#home", view_state=state).view_state == state


@pytest.mark.parametrize("state", [
    {"active_view": {"diagnosis": "private"}}, {"filters": {"status": [{"diagnosis": "private"}]}},
    {"filters": [{"value": {"diagnosis": "private"}}]}, {"dates": {"start": {"diagnosis": "private"}}},
    {"selection_count": -1}, {"selection_count": True}, {"selection_count": 1.5},
])
def test_navigation_rejects_record_objects_and_invalid_selection_counts(state):
    with pytest.raises(ValidationError):
        s.FeedbackCreate(kind="question", message="Where?", route="#home", view_state=state)


def test_metadata_marks_and_generated_contracts():
    value = s.FeedbackCreate(kind="confusing", message="This label", route="#home",
                             view_state={"filters": [{"field": "status", "value": "new"}], "selection_count": 3},
                             mark={"x": .1, "y": .2, "w": .3, "h": .4})
    assert value.mark.x == .1
    with pytest.raises(ValidationError):
        s.FeedbackMark(x=.8, y=0, w=.3, h=.1)
    source, schemas = render()
    assert "export type FeedbackCreate" in source and "FEEDBACK_LIMITS" in source
    assert schemas["FeedbackExport"]["properties"]["include_message"]["default"] is False
    assert schemas["FeedbackExport"]["properties"]["create_issue"]["default"] is False
    assert {"reporter_name", "duplicates_count"} <= set(schemas["FeedbackRow"]["required"])
    assert schemas["FeedbackRow"]["properties"]["duplicates_count"]["minimum"] == 0
    for name in ("FeedbackCreate", "FeedbackRow"):
        routes = schemas[name]["properties"]["recent_routes"]
        assert "recent_routes" not in schemas[name]["required"]
        assert routes["type"] == "array" and routes["maxItems"] == 10
        assert routes["items"]["type"] == "string" and routes["items"]["maxLength"] == 2000
    assert '"recent_routes"?: Array<string>' in source


def test_optional_github_settings_are_validated_and_token_repr_is_private(monkeypatch):
    from apps.api.settings import Settings
    for name in ("FEEDBACK_GITHUB_TOKEN", "FEEDBACK_GITHUB_TOKEN_FILE", "FEEDBACK_GITHUB_REPO"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env()
    settings.check()
    assert not settings.feedback_github_token and not settings.feedback_github_repo
    monkeypatch.setenv("FEEDBACK_GITHUB_TOKEN", "fictional-secret-token")
    with pytest.raises(RuntimeError, match="requires FEEDBACK_GITHUB_REPO"):
        Settings.from_env().check()
    monkeypatch.setenv("FEEDBACK_GITHUB_REPO", " example/feedback ")
    settings = Settings.from_env()
    settings.check()
    assert settings.feedback_github_repo == "example/feedback"
    assert "fictional-secret-token" not in repr(settings)
    for repo in ("example/..", "./feedback", "https://attacker.invalid/repo"):
        monkeypatch.setenv("FEEDBACK_GITHUB_REPO", repo)
        with pytest.raises(RuntimeError, match="owner/repository"):
            Settings.from_env().check()


def test_data_key_is_optional_validated_hidden_and_loaded_through_secret_helper(monkeypatch):
    from apps.api import settings as config
    key = "23" * 32
    monkeypatch.delenv("FMUC_DATA_KEY_FILE", raising=False)
    monkeypatch.delenv("FMUC_DATA_KEY", raising=False)
    assert config.data_key_from_secret("") is None
    assert not config.Settings.from_env().fmuc_data_key
    monkeypatch.setenv("FMUC_DATA_KEY", key)
    settings = config.Settings.from_env()
    assert config.data_key_from_secret(settings.fmuc_data_key) == bytes.fromhex(key)
    assert key not in repr(settings)
    assert "token-value" not in repr(s.GithubConnect(repo="example/feedback", token="token-value"))
    assert "token-value" not in repr(s.Command(kind="integration.github.connect", payload={"token": "token-value"}))
    for invalid in ("short", "ff" * 31, "gg" * 32, key + "\n"):
        with pytest.raises(RuntimeError, match="64 hexadecimal") as rejected:
            config.Settings(fmuc_data_key=invalid).check()
        assert invalid not in str(rejected.value)
    monkeypatch.setenv("FMUC_DATA_KEY_FILE", "/protected/key")
    with pytest.raises(RuntimeError, match="never both"):
        config.Settings.from_env()


def test_github_types_are_in_generated_and_runtime_contracts(h):
    _, schemas = render()
    exposed = h.get("admin", "/api/contracts")["schemas"]
    for name in ("GithubIntegrationStatus", "GithubConnect", "GithubDisconnect", "GithubTest"):
        assert name in schemas and name in exposed
    assert set(schemas["GithubConnect"]["required"]) == {"repo", "token"}
    assert set(schemas["GithubDisconnect"]["required"]) == {"revision"}
