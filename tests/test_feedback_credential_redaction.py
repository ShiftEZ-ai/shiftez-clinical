"""Feedback previews and saved outcomes never return the active GitHub credential."""
import json
import uuid
from datetime import date

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.commands import idempotency_hash_input
from apps.api.db import transaction
from apps.api.services import feedback, github_integrations
from apps.api.snapshots import snapshot_hash
from packages.contracts.schemas import Command


KEY = "17" * 32
REPO = "example/fictional-feedback"
ISSUE = "https://github.com/" + REPO + "/issues/7"
OPAQUE_TOKEN = "FictionalOpaqueCredentialForRedactionABCD"
FALLBACK_TOKEN = "FictionalServerFallbackCredentialWXYZ"
PAT_TOKENS = (('ghp_' + 'Fict' + 'iona' + 'lAdj' + 'acen' + 'tCre' + 'dent' + 'ialA' + 'BCD'), ('gith' + 'ub_p' + 'at_F' + 'icti' + 'onal' + 'Adja' + 'cent' + 'Cred' + 'enti' + 'alWX' + 'YZ'))
STATELESS_TOKEN = (('ghs_' + '1234' + '56_e' + 'yJhb' + 'GciO' + 'iJSU' + 'zI1N' + 'iJ9.' + 'eyJm' + 'bXVj' + 'X3By' + 'aXZh' + 'dGVf' + 'cGF5' + 'bG9h' + 'ZCI6' + 'InN5' + 'bnRo' + 'ZXRp' + 'YyJ9' + '.Syn' + 'thet' + 'ic_P' + 'riva' + 'te-S' + 'igna' + 'ture' + '_Onl' + 'y123' + '4567' + '890'))


def setup_connection(h, monkeypatch, token, *, extra_message=""):
    object.__setattr__(h.settings, "fmuc_data_key", KEY)
    monkeypatch.setattr(github_integrations, "check_repository", lambda repo, value: (True, None))
    h.command("operator", "integration.github.connect", {"repo": REPO, "token": token})
    return h.command("staff", "feedback.create", {
        "kind": "problem", "message": "copied=x" + token + extra_message, "route": "#planner",
    })["id"]


def assert_private_outcome(h, report_id, *tokens, create=False):
    key = str(uuid.uuid4())
    result = h.command("operator", "feedback.export_github", {
        "id": report_id, "include_message": True, "create_issue": create,
    }, key=key)
    receipt = h.get("operator", "/api/commands/" + key)
    replay = h.command("operator", "feedback.export_github", {
        "id": report_id, "include_message": True, "create_issue": create,
    }, key=key)
    with transaction(h.app.state.engine) as db:
        stored = db.scalar(select(m.CommandReceipt).where(m.CommandReceipt.key == key)).result
    for outcome in (result, receipt["result"], replay, stored):
        assert all(token not in json.dumps(outcome) for token in tokens)
        assert "\\[redacted\\]" in outcome["body"]
    return result


def test_app_credential_is_scrubbed_from_preview_and_existing_issue_retrieval(h, monkeypatch):
    object.__setattr__(h.settings, "feedback_github_token", FALLBACK_TOKEN)
    object.__setattr__(h.settings, "feedback_github_repo", "fallback/fictional-feedback")
    report_id = setup_connection(h, monkeypatch, OPAQUE_TOKEN, extra_message=" fallback=x" + FALLBACK_TOKEN)
    tokens = (OPAQUE_TOKEN, FALLBACK_TOKEN)
    sent = []

    def create_issue(repo, token, title, body):
        assert repo == REPO and token == OPAQUE_TOKEN
        assert all(value not in body for value in tokens)
        sent.append(body)
        return ISSUE

    monkeypatch.setattr(feedback, "create_github_issue", create_issue)
    preview = assert_private_outcome(h, report_id, *tokens)
    assert preview["configured"] and preview["github_issue_url"] is None and not sent
    created = assert_private_outcome(h, report_id, *tokens, create=True)
    assert created["github_issue_url"] == ISSUE and sent == [preview["body"]] == [created["body"]]
    assert assert_private_outcome(h, report_id, *tokens)["github_issue_url"] == ISSUE
    assert assert_private_outcome(h, report_id, *tokens, create=True)["github_issue_url"] == ISSUE
    assert len(sent) == 1


@pytest.mark.parametrize("token", PAT_TOKENS)
@pytest.mark.parametrize("lost_key,code", [("", "INTEGRATION_KEY_MISSING"), ("29" * 32, "INTEGRATION_KEY_INVALID")])
@pytest.mark.parametrize("suffix", ["", "é"], ids=["plain", "unicode_suffix"])
def test_key_loss_keeps_previews_private_and_available_but_new_issue_creation_fails_closed(
        h, monkeypatch, token, lost_key, code, suffix):
    report_id = setup_connection(h, monkeypatch, token, extra_message=suffix)
    object.__setattr__(h.settings, "fmuc_data_key", lost_key)

    def no_issue(*args, **kwargs):
        pytest.fail("An unavailable encryption key must prevent sending an issue")

    monkeypatch.setattr(feedback, "create_github_issue", no_issue)
    preview = assert_private_outcome(h, report_id, token)
    assert preview["configured"] and preview["github_issue_url"] is None
    rejected = h.command("operator", "feedback.export_github", {
        "id": report_id, "include_message": True, "create_issue": True,
    }, status=409)
    assert rejected["error"]["code"] == code
    assert h.get("operator", "/api/feedback/" + report_id)["github_issue_url"] is None


@pytest.mark.parametrize("lost_key", ["", "29" * 32])
def test_existing_issue_retrieval_stays_private_and_available_after_key_loss(h, monkeypatch, lost_key):
    token = PAT_TOKENS[0]
    report_id = setup_connection(h, monkeypatch, token)
    monkeypatch.setattr(feedback, "create_github_issue", lambda *args: ISSUE)
    assert assert_private_outcome(h, report_id, token, create=True)["github_issue_url"] == ISSUE
    object.__setattr__(h.settings, "fmuc_data_key", lost_key)

    def no_issue(*args, **kwargs):
        pytest.fail("Retrieving an existing issue must not send another issue")

    monkeypatch.setattr(feedback, "create_github_issue", no_issue)
    assert assert_private_outcome(h, report_id, token)["github_issue_url"] == ISSUE
    assert assert_private_outcome(h, report_id, token, create=True)["github_issue_url"] == ISSUE


def legacy_receipt(h, report_id, *tokens):
    h.client("operator")
    command = Command(kind="feedback.export_github", payload={"id": report_id, "include_message": True})
    key = str(uuid.uuid4())
    text = " ".join("raw=x" + token + "é escaped=x" + token.replace("_", "\\_") + "é" for token in tokens)
    original = {"title": "Legacy " + text, "body": text + "\nRota report: https://example.invalid/#system/feedback",
                "github_issue_url": ISSUE, "configured": True, "receipt_id": str(uuid.uuid4()),
                "activity": {"revision": 7}}
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.CommandReceipt(id=original["receipt_id"], department_id=h.seed["department_id"],
            actor_id=h.seed["identities"]["operator"], command_kind=command.kind, required_roles=["admin"],
            key=key, payload_hash=snapshot_hash(idempotency_hash_input(command)), result=original))
    return key, command.payload, original


@pytest.mark.parametrize("lost_key", [None, "", "29" * 32], ids=["available", "missing", "changed"])
def test_legacy_feedback_receipts_project_raw_and_markdown_secrets_without_rewriting_evidence(h, monkeypatch, lost_key):
    token = "Fictional_Opaque_CredentialABCD" if lost_key is None else PAT_TOKENS[1]
    object.__setattr__(h.settings, "feedback_github_token", FALLBACK_TOKEN)
    report_id = setup_connection(h, monkeypatch, token)
    key, payload, original = legacy_receipt(h, report_id, token, FALLBACK_TOKEN)
    if lost_key is not None:
        object.__setattr__(h.settings, "fmuc_data_key", lost_key)

    def no_issue(*args, **kwargs):
        pytest.fail("Historical outcome retrieval must not send an issue")

    monkeypatch.setattr(feedback, "create_github_issue", no_issue)
    replay = h.command("operator", "feedback.export_github", payload, key=key)
    recovered = h.get("operator", "/api/commands/" + key)["result"]
    assert replay == recovered
    for field in ("title", "body"):
        rendered = replay[field].replace("\\_", "_")
        assert all(secret not in rendered for secret in (token, FALLBACK_TOKEN))
        assert "redacted" in replay[field]
    assert replay["body"].endswith("\nRota report: https://example.invalid/#system/feedback")
    assert {k: v for k, v in replay.items() if k not in {"title", "body"}} == {
        k: v for k, v in original.items() if k not in {"title", "body"}}
    with transaction(h.app.state.engine) as db:
        assert db.get(m.CommandReceipt, original["receipt_id"]).result == original


@pytest.mark.parametrize("isolation", ["current_role", "department"])
def test_legacy_feedback_receipt_authorization_precedes_secret_projection(h, monkeypatch, isolation):
    report_id = setup_connection(h, monkeypatch, OPAQUE_TOKEN)
    key, payload, original = legacy_receipt(h, report_id, OPAQUE_TOKEN)
    with transaction(h.app.state.engine, write=True) as db:
        identity_id = h.seed["identities"]["operator"]
        if isolation == "current_role":
            binding = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == identity_id,
                                m.RoleBinding.role == "operator"))
            binding.role = "auditor"
        else:
            department = db.get(m.Department, h.seed["department_id"])
            other = m.Department(organisation_id=department.organisation_id, name="Fictional independent department")
            db.add(other)
            db.flush()
            db.add(m.RoleBinding(identity_id=identity_id, department_id=other.id, role="operator", starts_on=date(2020, 1, 1)))
            for session in db.scalars(select(m.Session).where(m.Session.identity_id == identity_id)):
                session.department_id = other.id

    def no_decrypt(*args, **kwargs):
        pytest.fail("An unauthorized receipt must not resolve any integration credential")

    monkeypatch.setattr(github_integrations, "decrypt", no_decrypt)
    h.get("operator", "/api/commands/" + key, status=403 if isolation == "current_role" else 404)
    h.command("operator", "feedback.export_github", payload, key=key, status=403 if isolation == "current_role" else 409)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.CommandReceipt, original["receipt_id"]).result == original


@pytest.mark.parametrize('lost_key', [None, '', '29' * 32], ids=['available', 'missing', 'changed'])
def test_unknown_stateless_app_token_is_fully_redacted_from_previews_and_historical_outcomes(h, monkeypatch, lost_key):
    setup_connection(h, monkeypatch, OPAQUE_TOKEN)
    message = 'raw=' + STATELESS_TOKEN + ' escaped=' + STATELESS_TOKEN.replace('_', r'\_')
    report = h.command('staff', 'feedback.create', {'kind': 'problem', 'message': message, 'route': '#planner'})['id']
    legacy_key, legacy_payload, original = legacy_receipt(h, report, STATELESS_TOKEN)
    if lost_key is not None:
        object.__setattr__(h.settings, 'fmuc_data_key', lost_key)
    payload = {'id': report, 'include_message': True}
    key = str(uuid.uuid4())
    preview = h.command('operator', 'feedback.export_github', payload, key=key)
    outcomes = [preview, h.get('operator', '/api/commands/' + key)['result'],
                h.command('operator', 'feedback.export_github', payload, key=key),
                h.get('operator', '/api/commands/' + legacy_key)['result'],
                h.command('operator', 'feedback.export_github', legacy_payload, key=legacy_key)]
    if lost_key is None:
        sent = []
        monkeypatch.setattr(feedback, 'create_github_issue', lambda repo, token, title, body: sent.append(body) or ISSUE)
        outcomes.append(h.command('operator', 'feedback.export_github', {**payload, 'create_issue': True}))
        assert len(sent) == 1
        outcomes.append({'body': sent[0]})
    for outcome in outcomes:
        text = json.dumps(outcome).replace(r'\\_', '_')
        assert all(part not in text for part in STATELESS_TOKEN.split('.')), outcome
    with transaction(h.app.state.engine) as db:
        assert db.get(m.CommandReceipt, original['receipt_id']).result == original
