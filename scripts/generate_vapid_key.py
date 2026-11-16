#!/usr/bin/env python3
"""Create an environment-owned VAPID scalar in an exclusive protected file; print public metadata only."""
import argparse
import base64
import json
import os
from pathlib import Path
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def generate(path, key_id):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('Use an absolute protected secret path')
    private = ec.generate_private_key(ec.SECP256R1())
    encoded = base64.urlsafe_b64encode(private.private_numbers().private_value.to_bytes(32, 'big')).rstrip(b'=')
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        output.write(encoded + b'\n')
        output.flush()
        os.fsync(output.fileno())
    public = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {'key_id': key_id, 'public_key': base64.urlsafe_b64encode(public).rstrip(b'=').decode()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private-file', required=True, type=Path)
    parser.add_argument('--key-id', default=str(uuid.uuid4()))
    args = parser.parse_args()
    try:
        public = generate(args.private_file, args.key_id)
    except (OSError, ValueError):
        parser.exit(1, 'Key creation failed; use a new absolute path in an existing protected directory.\n')
    print(json.dumps(public))


if __name__ == '__main__':
    main()
