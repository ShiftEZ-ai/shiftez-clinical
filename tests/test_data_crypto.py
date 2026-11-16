"""One data key and AES-GCM envelope serve mailbox and GitHub credentials."""
import base64
from dataclasses import replace
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import pytest

from apps.api import data_crypto
from apps.api.settings import Settings


KEY = bytes(range(32))


@pytest.mark.parametrize("encoded", [KEY.hex(), base64.b64encode(KEY).decode(),
                                    base64.urlsafe_b64encode(KEY).decode()])
def test_settings_data_key_accepts_existing_encodings_and_is_not_disclosed(monkeypatch, encoded):
    monkeypatch.delenv("FMUC_DATA_KEY_FILE", raising=False)
    monkeypatch.setenv("FMUC_DATA_KEY", encoded)
    settings = Settings.from_env()
    assert settings.fmuc_data_key == encoded
    assert data_crypto.data_key_from_secret(settings.fmuc_data_key) == KEY
    assert encoded not in repr(settings)
    monkeypatch.setenv("FMUC_DATA_KEY", "invalid-key-after-settings-was-loaded")
    value = data_crypto.encrypt_secret("synthetic-secret", "mailbox:owner", settings)
    assert data_crypto.decrypt_secret(value, "mailbox:owner", settings) == "synthetic-secret"


def test_settings_loads_data_key_file_and_rejects_ambiguous_source(monkeypatch, tmp_path):
    key_file = tmp_path / "data-key"
    key_file.write_text(KEY.hex() + "\n", encoding="utf-8")
    monkeypatch.setenv("ROTA_ENV", "test")
    monkeypatch.delenv("FMUC_DATA_KEY", raising=False)
    monkeypatch.setenv("FMUC_DATA_KEY_FILE", str(key_file))
    # Windows lacks these Linux flags; test the existing bounded regular-file loader.
    for flag in ("O_NOFOLLOW", "O_NONBLOCK"):
        if not hasattr(os, flag):
            monkeypatch.setattr(os, flag, 0, raising=False)
    assert Settings.from_env().fmuc_data_key == KEY.hex()
    monkeypatch.setenv("FMUC_DATA_KEY", KEY.hex())
    with pytest.raises(RuntimeError, match="never both"):
        Settings.from_env()


def test_shared_crypto_reads_existing_github_and_mailbox_envelopes():
    settings = Settings(fmuc_data_key=KEY.hex())
    nonce = bytes(range(12))
    contexts = [b"departmentgithub_issues", b"rota:mailbox:department:identity"]
    for context in contexts:
        original = nonce + AESGCM(KEY).encrypt(nonce, b"synthetic-secret", context)
        assert data_crypto.decrypt(original, context, settings) == "synthetic-secret"
        encoded = base64.urlsafe_b64encode(original).decode()
        assert data_crypto.decrypt_secret(encoded, context.decode(), settings) == "synthetic-secret"
        replacement = data_crypto.encrypt("synthetic-secret", context, settings)
        assert AESGCM(KEY).decrypt(replacement[:12], replacement[12:], context) == b"synthetic-secret"
        assert replacement != data_crypto.encrypt("synthetic-secret", context, settings)
        with pytest.raises(RuntimeError, match="cannot be decrypted"):
            data_crypto.decrypt(replacement, b"another-owner", settings)
        with pytest.raises(RuntimeError, match="cannot be decrypted"):
            data_crypto.decrypt(replacement, context, replace(settings, fmuc_data_key=bytes(reversed(KEY)).hex()))


@pytest.mark.parametrize("encoded", ["bad-private-key", "ab" * 31, base64.b64encode(b"short").decode()])
def test_settings_check_refuses_invalid_data_keys_without_echoing_them(encoded):
    with pytest.raises(RuntimeError, match="32-byte key") as problem:
        Settings(fmuc_data_key=encoded).check()
    assert encoded not in str(problem.value)


def test_missing_key_is_optional_until_a_secret_is_used():
    assert data_crypto.data_key_from_secret("") is None
    Settings().check()
    for operation, value in ((data_crypto.encrypt, "synthetic-secret"), (data_crypto.decrypt, b"invalid")):
        with pytest.raises(RuntimeError, match="FMUC_DATA_KEY"):
            operation(value, b"owner", Settings())
