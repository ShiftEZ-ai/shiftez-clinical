"""Shared AES-GCM envelopes for persisted integration and mailbox secrets."""
import base64
import binascii
import re
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def data_key_from_secret(value: str) -> bytes | None:
    if not value:
        return None
    try:
        key = (bytes.fromhex(value) if re.fullmatch(r"[0-9a-fA-F]{64}", value)
               else base64.b64decode(value, altchars=b"-_", validate=True))
        if len(key) != 32:
            raise ValueError
    except (ValueError, binascii.Error):
        raise RuntimeError("FMUC_DATA_KEY must be a 32-byte key encoded as base64 or 64 hexadecimal characters") from None
    return key


def _cipher(settings):
    key = data_key_from_secret(settings.fmuc_data_key)
    if key is None:
        raise RuntimeError("FMUC_DATA_KEY requires a 32-byte key before storing or using secrets")
    return AESGCM(key)


def encrypt(value: str, context: bytes, settings) -> bytes:
    nonce = secrets.token_bytes(12)
    return nonce + _cipher(settings).encrypt(nonce, value.encode(), context)


def decrypt(value: bytes, context: bytes, settings) -> str:
    try:
        return _cipher(settings).decrypt(value[:12], value[12:], context).decode()
    except (InvalidTag, ValueError, UnicodeError):
        raise RuntimeError("Stored secret cannot be decrypted; restore the data key or reconnect") from None


def encrypt_secret(value: str, context: str, settings) -> str:
    return base64.urlsafe_b64encode(encrypt(value, context.encode(), settings)).decode()


def decrypt_secret(value: str, context: str, settings) -> str:
    try:
        envelope = base64.b64decode(value, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        raise RuntimeError("Stored secret cannot be decrypted; restore the data key or reconnect") from None
    return decrypt(envelope, context.encode(), settings)
