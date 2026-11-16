"""The explicit GitHub adapter never leaks remote errors or accepts an unrelated URL."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from .clock_support import freeze_clock
from apps.api.errors import Problem
from apps.api.services import feedback


def test_github_adapter_posts_only_the_reviewed_issue_to_the_configured_repository(monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(201, json={"html_url": "https://github.com/example/feedback/issues/7"},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(feedback.httpx, "post", post)
    assert feedback.create_github_issue("example/feedback", "fictional-test-token", "Reviewed title", "Reviewed body") \
        == "https://github.com/example/feedback/issues/7"
    url, options = calls[0]
    assert url == "https://api.github.com/repos/example/feedback/issues"
    assert options["json"] == {"title": "Reviewed title", "body": "Reviewed body"}
    assert options["headers"]["Authorization"] == "Bearer fictional-test-token"
    assert options["timeout"] == 15 and "follow_redirects" not in options


@pytest.mark.parametrize("status,body", [
    (200, {"html_url": "https://github.com/example/feedback/issues/7"}),
    (201, {"html_url": "https://attacker.invalid/collect"}),
    (201, {"html_url": "https://github.com/other/repository/issues/7"}),
    (201, {"html_url": "https://github.com/example/feedback/issues/7?secret=private"}),
    (201, {"html_url": None}), (201, []),
    *[(status, {"message": "fictional-test-token"}) for status in (500, 502, 503, 504)],
])
def test_github_adapter_rejects_invalid_responses_without_remote_content(monkeypatch, status, body):
    monkeypatch.setattr(feedback.httpx, "post", lambda url, **kwargs: httpx.Response(
        status, json=body, request=httpx.Request("POST", url)))
    with pytest.raises(Problem) as caught:
        feedback.create_github_issue("example/feedback", "fictional-test-token", "Title", "Body")
    assert caught.value.status == 502 and "fictional-test-token" not in str(caught.value)
    assert "private" not in str(caught.value)
    assert caught.value.code == "FEEDBACK_GITHUB_UNCERTAIN"
    assert "could not be confirmed" in caught.value.message and "before exporting again" in caught.value.message


@pytest.mark.parametrize("repo", ["https://attacker.invalid", "example/feedback/../../other", "example/feedback?token=x", "one",
                                   "example/.", "example/..", "./feedback", "../feedback"])
def test_github_adapter_invalid_repository_is_rejected_before_network(monkeypatch, repo):
    def no_network(*args, **kwargs):
        pytest.fail("An invalid repository must not make a request")

    monkeypatch.setattr(feedback.httpx, "post", no_network)
    with pytest.raises(Problem) as caught:
        feedback.create_github_issue(repo, "fictional-test-token", "Title", "Body")
    assert caught.value.status == 409


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError])
def test_github_adapter_transport_error_requires_checking_remote_outcome_before_retry(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error("fictional-test-token and remote private data")

    monkeypatch.setattr(feedback.httpx, "post", fail)
    with pytest.raises(Problem) as caught:
        feedback.create_github_issue("example/feedback", "fictional-test-token", "Title", "Body")
    assert caught.value.status == 502 and "fictional-test-token" not in str(caught.value)
    assert caught.value.code == "FEEDBACK_GITHUB_UNCERTAIN"
    assert "could not be confirmed" in caught.value.message
    assert "Check the configured repository before exporting again" in caught.value.message
    assert "failed" not in caught.value.message.lower()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 422, 429, 499])
def test_github_adapter_create_rejections_are_definite_and_do_not_expose_remote_content(monkeypatch, status):
    monkeypatch.setattr(feedback.httpx, "post", lambda url, **kwargs: httpx.Response(
        status, json={"message": "fictional-test-token and private upstream content"}, request=httpx.Request("POST", url)))
    with pytest.raises(Problem) as caught:
        feedback.create_github_issue("example/feedback", "fictional-test-token", "Title", "Body")
    assert caught.value.status == 422
    assert caught.value.code == ("FEEDBACK_GITHUB_FORBIDDEN" if status == 403 else "FEEDBACK_GITHUB_REJECTED")
    assert "fictional-test-token" not in caught.value.message and "private upstream content" not in caught.value.message


@pytest.mark.parametrize("route,expected", [
    ("#system/feedback?person=private", "/system/feedback"),
    ("#/people/FictionalPatient?token=private", "/people/[record]"),
    ("/people/FictionalPatient%3Ftoken=private", "/people/[record]"),
    ("#FictionalPatient", "/[record]"),
])
def test_export_route_preserves_only_safe_page_tokens(route, expected):
    assert feedback._safe_route(route) == expected


@pytest.mark.parametrize("message,expected", [
    ("Failed to fetch", "Failed to fetch"),
    ("TypeError: FictionalPatient alice@example.invalid", "TypeError: [details redacted]"),
    ("FictionalPatient has a clinical condition", "[free-text error redacted]"),
])
def test_export_errors_fail_closed_on_unstructured_text(message, expected):
    assert feedback._safe_error(message) == expected


@pytest.mark.parametrize("remaining,expected", [(3600, 3600), (3599.5, 3600), (1.0005, 2), (0.001, 1), (3600.5, 3600)])
def test_hourly_limit_retry_after_rounds_up_and_stays_within_one_hour(monkeypatch, remaining, expected):
    moment = freeze_clock(monkeypatch)
    db = Mock()
    db.execute.return_value.one.return_value = (10, moment + timedelta(seconds=remaining - 3600))
    actor = SimpleNamespace(person_id="fictional-person", identity_id="fictional-identity")

    with pytest.raises(Problem) as caught:
        feedback.create(db, actor, {"kind": "question", "message": "Synthetic question", "route": "#home"}, None)

    assert caught.value.status == 429 and caught.value.code == "RATE_LIMITED"
    assert caught.value.details == {"retry_after": expected}
    db.add.assert_not_called()
