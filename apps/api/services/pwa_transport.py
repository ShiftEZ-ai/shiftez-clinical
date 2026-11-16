"""Small standards-library adapter; capabilities never enter logs or exception messages."""
import base64
import binascii
import ipaddress
import json
import re
import socket
import os
import struct
from packages.domain.time import now

import jwt
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from apps.api.errors import Problem, require

# RFC 8291/8030 and Apple interoperability bounds, not tenant settings.
MAX_WIRE_BYTES = 4096
TOPIC = re.compile(r'^[A-Za-z0-9_-]{1,32}$')


def b64(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]+', value) is not None,
            'PUSH_KEY_INVALID', 'Invalid push key encoding', 422)
    try:
        return base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
    except (ValueError, binascii.Error):
        raise Problem(422, 'PUSH_KEY_INVALID', 'Invalid push key encoding') from None


def validate_keys(keys):
    point, auth = b64(keys['p256dh']), b64(keys['auth'])
    try:
        if len(point) != 65 or point[0] != 4 or len(auth) != 16:
            raise ValueError
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    except ValueError:
        raise Problem(422, 'PUSH_KEY_INVALID', 'Invalid P-256 point or authentication secret') from None


def endpoint_origin(endpoint, config):
    try:
        p = urlsplit(endpoint)
        host = p.hostname or ''
        if (p.scheme != 'https' or not host or p.username is not None or p.password is not None
                or p.fragment or p.port not in (None, 443) or '\\' in endpoint
                or any(c.isspace() or ord(c) < 32 for c in endpoint)):
            raise ValueError
        # Full returned path/query are opaque and are sent unchanged.
        if not (host in config.provider_hosts or any(host.endswith('.' + suffix) for suffix in config.provider_suffixes)):
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError
        return 'https://' + host
    except ValueError:
        raise Problem(422, 'PUSH_ENDPOINT_INVALID', 'Use an approved HTTPS push-provider endpoint') from None


def keyring(settings):
    try:
        raw = json.loads(settings.vapid_draining_keys) if settings.vapid_draining_keys else {}
        if not isinstance(raw, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in raw.items()):
            return {}
        if settings.vapid_private_key:
            raw[settings.vapid_key_id] = settings.vapid_private_key
        return {key: ec.derive_private_key(int.from_bytes(b64(value), "big"), ec.SECP256R1()) for key, value in raw.items()}
    except Exception:
        return {}


def public_key(settings):
    key = keyring(settings).get(settings.vapid_key_id)
    if key is None:
        return None
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(public).rstrip(b'=').decode()


def authorization(settings, key_id, origin, expires_at):
    key = keyring(settings).get(key_id)
    require(key is not None, 'PUSH_KEY_UNAVAILABLE', 'The subscribing key is unavailable', 503)
    p = urlsplit(settings.vapid_subject)
    require((p.scheme == 'mailto' and '@' in p.path) or (p.scheme == 'https' and p.hostname),
            'PUSH_CONTACT_REQUIRED', 'Configure a public VAPID contact', 503)
    require(int(now().timestamp()) < expires_at <= int(now().timestamp()) + 86400,
            'PUSH_VAPID_EXPIRY', 'VAPID expiration must be within 24 hours')
    token = jwt.encode({'aud': origin, 'sub': settings.vapid_subject, 'exp': expires_at}, key, algorithm='ES256')
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return 'vapid t=' + token + ', k=' + base64.urlsafe_b64encode(public).rstrip(b'=').decode()


def encrypt(keys, payload, *, private_key=None, salt=None):
    """RFC 8291 §3.3–4 single aes128gcm record; cryptographic primitives stay in cryptography."""
    validate_keys(keys)
    private_key = private_key or ec.generate_private_key(ec.SECP256R1())
    salt = salt or os.urandom(16)
    public = private_key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    ua = b64(keys['p256dh'])
    shared = private_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua))
    ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=b64(keys['auth']),
               info=b'WebPush: info\x00' + ua + public).derive(shared)
    key = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt, info=b'Content-Encoding: aes128gcm\x00').derive(ikm)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt, info=b'Content-Encoding: nonce\x00').derive(ikm)
    return salt + struct.pack('!IB', 4096, len(public)) + public + AESGCM(key).encrypt(nonce, payload + b'\x02', None)


def encode(subscription, payload, authorization_value, *, ttl, topic, urgency):
    require(TOPIC.fullmatch(topic) is not None, 'PUSH_TOPIC_INVALID', 'Invalid push topic')
    require(0 <= ttl <= 2592000, 'PUSH_TTL_INVALID', 'Invalid push lifetime')
    require(urgency in ('very-low', 'low', 'normal', 'high'), 'PUSH_URGENCY_INVALID', 'Invalid urgency')
    encrypted = encrypt(subscription['keys'], payload)
    require(len(encrypted) <= MAX_WIRE_BYTES, 'PUSH_PAYLOAD_LIMIT', 'Encrypted push exceeds the provider ceiling')
    return encrypted, {'Authorization': authorization_value, 'TTL': str(ttl), 'Topic': topic,
        'Urgency': urgency, 'Content-Encoding': 'aes128gcm', 'Content-Type': 'application/octet-stream'}


class PinnedTransport(httpx.BaseTransport):
    """Pin public DNS results while preserving Host and TLS SNI; never follow a redirect."""
    def __init__(self):
        self.inner = httpx.HTTPTransport(retries=0, trust_env=False)

    def handle_request(self, request):
        try:
            host = request.url.host
            peers = [ipaddress.ip_address(row[4][0]) for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]
            if not peers or any(not (getattr(ip, 'ipv4_mapped', None) or ip).is_global or ip.is_multicast for ip in peers):
                raise ValueError
            pinned = httpx.Request(request.method, request.url.copy_with(host=str(peers[0])),
                headers=request.headers, stream=request.stream,
                extensions={**request.extensions, 'sni_hostname': host})
            return self.inner.handle_request(pinned)
        except (OSError, ValueError):
            raise httpx.ConnectError('PUSH_PROVIDER_DNS') from None

    def close(self):
        self.inner.close()


def post(endpoint, body, headers, timeout):
    with httpx.Client(timeout=timeout, transport=PinnedTransport(), follow_redirects=False, trust_env=False) as client:
        # Stream responses: the untrusted provider body is neither retained nor logged.
        with client.stream('POST', endpoint, content=body, headers=headers) as response:
            return response.status_code, response.headers.get('Retry-After')
