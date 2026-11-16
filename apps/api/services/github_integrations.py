"""Department GitHub credentials: explicit status projection and authenticated encryption only."""
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import undefer

from packages.contracts import schemas as s
from packages.domain.time import aware
from .. import models as m
from ..auth import require_permission
from ..errors import Problem, require
from ..settings import data_key_from_secret
from .common import expected

KIND = "github_issues"
REJECTED_MESSAGE = "GitHub could not verify this token and repository. Check repository access and that Issues is enabled."


@dataclass(frozen=True)
class RepositoryCheck:
    """Secret-free result tied to the authenticated context and the checked credential's lifetime."""
    identity_id: str
    department_id: str
    credential_id: str | None
    revision: int | None
    ok: bool
    expiry: datetime | None


def github_headers(token):
    return {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2026-03-10"}


def credential(db, department_id, *, include_token=False):
    query = select(m.IntegrationCredential).where(
        m.IntegrationCredential.department_id == department_id, m.IntegrationCredential.kind == KIND)
    if include_token:
        query = query.options(undefer(m.IntegrationCredential.token_ciphertext))
    return db.scalar(query)


def status(db, actor, settings):
    require_permission(actor, "integrations.github")
    row = credential(db, actor.department_id)
    fallback = bool(settings.feedback_github_token and settings.feedback_github_repo)
    return s.GithubIntegrationStatus(
        connected=bool(row or fallback), source="app" if row else "env" if fallback else None,
        repo=row.repo if row else settings.feedback_github_repo if fallback else None,
        token_hint=row.token_hint if row else settings.feedback_github_token[-4:] if fallback and len(settings.feedback_github_token) > 4 else None,
        connected_by_name=db.get(m.Identity, row.connected_by).display_name if row else None,
        connected_at=aware(row.connected_at) if row else None,
        last_tested_at=aware(row.last_tested_at) if row and row.last_tested_at else None,
        last_test_ok=row.last_test_ok if row else None,
        last_used_at=aware(row.last_used_at) if row and row.last_used_at else None,
        token_expires_at=aware(row.token_expires_at) if row and row.token_expires_at else None,
        key_configured=bool(settings.fmuc_data_key), revision=row.revision if row else None,
    ).model_dump(mode="json")


def _cipher(settings):
    key = data_key_from_secret(settings.fmuc_data_key)
    require(key is not None, "INTEGRATION_KEY_MISSING", "The server needs FMUC_DATA_KEY before storing or using app credentials", 409)
    return AESGCM(key)


def _aad(row):
    return (row.department_id + row.kind).encode("utf-8")


def decrypt(row, settings):
    cipher = _cipher(settings)
    try:
        data = row.token_ciphertext
        return cipher.decrypt(data[:12], data[12:], _aad(row)).decode("utf-8")
    except (InvalidTag, ValueError, UnicodeError):
        raise Problem(409, "INTEGRATION_KEY_INVALID", "The saved GitHub credential cannot be decrypted. Restore the original data key or replace the connection.") from None


def check_repository(repo, token):
    """Do not inspect `permissions`: it describes the user's role, not a fine-grained PAT's scopes."""
    expiry = None
    try:
        response = httpx.get(f"https://api.github.com/repos/{repo}", headers=github_headers(token), timeout=15)
        header = response.headers.get("github-authentication-token-expiration")
        if header:
            try:
                expiry = parsedate_to_datetime(header)
            except (ValueError, TypeError, OverflowError):
                try:
                    normalized = re.sub(r" (UTC|[+-]\d{2}:?\d{2})$",
                                        lambda match: "+00:00" if match[1] == "UTC" else match[1], header)
                    expiry = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    pass
            if expiry is not None:
                expiry = expiry.replace(tzinfo=expiry.tzinfo or timezone.utc).astimezone(timezone.utc)
        if response.status_code != 200:
            return False, expiry
        data = response.json()
        return isinstance(data, dict) and data.get("has_issues") is True, expiry
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return False, None


def prepare_check(db, actor, command, settings):
    """Validate and copy the request in a short read transaction; its caller closes it before the GET."""
    require_permission(actor, "integrations.github")
    require(command.target_id is None, "INTEGRATION_TARGET", "The integration belongs to the current department")
    row = credential(db, actor.department_id, include_token=True)
    if command.kind == "integration.github.connect":
        value = s.GithubConnect.model_validate(command.payload)
        _cipher(settings)
        if row:
            expected(row, command.expected_revision)
        else:
            require(command.expected_revision is None, "REVISION_CONFLICT", "The GitHub connection changed; reload its status", 409)
        repo, token = value.repo, value.token
    else:
        require(command.kind == "integration.github.test", "COMMAND_UNKNOWN", "Unknown integration command")
        s.GithubTest.model_validate(command.payload)
        require(row is not None, "INTEGRATION_NOT_CONNECTED", "Connect GitHub in the app before testing it", 409)
        if command.expected_revision is not None:
            expected(row, command.expected_revision)
        repo, token = row.repo, decrypt(row, settings)
    return {"identity_id": actor.identity_id, "department_id": actor.department_id,
            "credential_id": row.id if row else None, "revision": row.revision if row else None}, repo, token


def command(db, actor, command, settings, *, checked=None):
    require_permission(actor, "integrations.github")
    require(command.target_id is None, "INTEGRATION_TARGET", "The integration belongs to the current department")
    row = credential(db, actor.department_id)
    if command.kind in {"integration.github.connect", "integration.github.test"}:
        require(checked is not None, "INTEGRATION_CHECK_REQUIRED", "Check the GitHub repository before saving", 409)
        require((checked.identity_id, checked.department_id) == (actor.identity_id, actor.department_id),
                "INTEGRATION_CONTEXT_CHANGED", "The signed-in department changed; check the connection again", 409)
        require((checked.credential_id, checked.revision) == ((row.id, row.revision) if row else (None, None)),
                "STALE_REVISION", "The GitHub connection changed during its check; reload and check again", 409)
    if command.kind == "integration.github.connect":
        value = s.GithubConnect.model_validate(command.payload)
        cipher = _cipher(settings)
        if row:
            expected(row, command.expected_revision)
        else:
            require(command.expected_revision is None, "REVISION_CONFLICT", "The GitHub connection changed; reload its status", 409)
        expiry = checked.expiry
        require(checked.ok, "INTEGRATION_GITHUB_REJECTED", REJECTED_MESSAGE, 422)
        if row is None:
            # Receipts survive revocation: an old revision must not match a later connection's lifetime.
            previous = db.scalar(select(func.max(m.CommandReceipt.result["revision"].as_integer())).where(
                m.CommandReceipt.department_id == actor.department_id,
                m.CommandReceipt.command_kind.in_(("integration.github.connect", "integration.github.test")))) or 0
            row = m.IntegrationCredential(department_id=actor.department_id, kind=KIND, revision=previous + 1)
            db.add(row)
        else:
            row.revision += 1
        nonce = os.urandom(12)
        row.repo, row.token_hint = value.repo, value.token[-4:]
        row.token_ciphertext = nonce + cipher.encrypt(nonce, value.token.encode("utf-8"), _aad(row))
        row.connected_by, row.connected_at = actor.identity_id, m.now()
        row.last_tested_at, row.last_test_ok = m.now(), True
        row.token_expires_at, row.last_used_at = expiry, None
    elif command.kind == "integration.github.test":
        s.GithubTest.model_validate(command.payload)
        require(row is not None, "INTEGRATION_NOT_CONNECTED", "Connect GitHub in the app before testing it", 409)
        if command.expected_revision is not None:
            expected(row, command.expected_revision)
        row.last_test_ok, expiry = checked.ok, checked.expiry
        if expiry is not None:
            row.token_expires_at = expiry
        row.last_tested_at, row.revision = m.now(), row.revision + 1
    else:
        require(command.kind == "integration.github.disconnect", "COMMAND_UNKNOWN", "Unknown integration command")
        value = s.GithubDisconnect.model_validate(command.payload)
        require(command.expected_revision is None or command.expected_revision == value.revision,
                "INTEGRATION_REVISION", "The command revision must match the payload")
        require(row is not None, "INTEGRATION_NOT_CONNECTED", "No app GitHub connection to disconnect", 409)
        expected(row, value.revision)
        db.delete(row)  # Revoke credential bytes; command receipt and audit evidence stay.
    db.flush()
    return status(db, actor, settings)
