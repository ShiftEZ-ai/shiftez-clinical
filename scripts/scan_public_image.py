"""Scan the image config and all project layers, including files removed by later layers."""

import base64
from collections import Counter, defaultdict
import csv
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import sys
import tarfile


DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
RECORD = re.compile(r"usr/local/lib/python[^/]+/site-packages/[^/]+[.]dist-info/RECORD")
EXCEPTIONS = Path(__file__).with_name("public_image_exceptions.json")


def archive_file(archive, name):
    member = archive.getmember(name)
    if not member.isfile():
        raise ValueError("Expected a regular archive member")
    return archive.extractfile(member)


def diff_ids(config):
    root = config["rootfs"]
    layers = root["diff_ids"]
    if root["type"] != "layers" or not isinstance(layers, list) or not layers or not all(
            DIGEST.fullmatch(digest) for digest in layers):
        raise ValueError("Invalid rootfs diff IDs")
    return layers


def installed_wheel_hashes(layer, members):
    hashes = defaultdict(set)
    for member in members:
        name = member.name.removeprefix("./")
        if not RECORD.fullmatch(name):
            continue
        if not member.isfile():
            raise ValueError("Expected a regular wheel RECORD")
        root = str(PurePosixPath(name).parent.parent)
        with io.TextIOWrapper(layer.extractfile(member), encoding="utf-8", newline="") as record:
            for relative, digest, size in csv.reader(record, strict=True):
                path = posixpath.normpath(posixpath.join(root, relative))
                if re.fullmatch(r"sha256=[A-Za-z0-9_-]{43}", digest) and path.startswith(root + "/"):
                    hashes[path].add((digest[7:], int(size)))
    return hashes


def scan_image(path, base, image_id, findings, exceptions):
    hits = Counter()
    totals = Counter()
    base_dpkg_lines = defaultdict(set)

    def scan(text, allowed=(), upstream_lines=()):
        for number, pattern in findings(text):
            excepted = pattern in allowed or number in upstream_lines
            hits[pattern] += not excepted
            totals["excepted_hits"] += excepted

    base_ids = diff_ids(base)
    with tarfile.open(path) as image:
        names = image.getnames()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate image archive member")
        with archive_file(image, "manifest.json") as file:
            manifest, = json.load(file)
        with archive_file(image, manifest["Config"]) as file:
            config_bytes = file.read()
        if image_id != "sha256:" + hashlib.sha256(config_bytes).hexdigest():
            raise ValueError("Saved config does not match local image ID")
        config = json.loads(config_bytes)
        image_ids = diff_ids(config)
        layers = manifest["Layers"]
        if (not isinstance(layers, list) or len(layers) != len(image_ids)
                or image_ids[:len(base_ids)] != base_ids
                or any(config[key] != base[key] for key in ("os", "architecture"))):
            raise ValueError("Saved layers do not match the pinned runtime base")
        scan(config_bytes.decode("utf-8"))
        totals.update(scanned_files=1, scanned_bytes=len(config_bytes))

        for i, (name, expected) in enumerate(zip(layers, image_ids, strict=True)):
            with archive_file(image, name) as raw:
                if raw.peek(2).startswith(b"\x1f\x8b"):
                    with gzip.GzipFile(fileobj=raw) as content:
                        digest = hashlib.file_digest(content, "sha256")
                else:
                    digest = hashlib.file_digest(raw, "sha256")
            if expected != "sha256:" + digest.hexdigest():
                raise ValueError("Layer contents do not match the config")

            with archive_file(image, name) as raw, tarfile.open(fileobj=raw, mode="r:*") as layer:
                members = layer.getmembers()
                if i < len(base_ids):
                    for member in members:
                        if member.isfile():
                            totals.update(skipped_files=1, skipped_bytes=member.size)
                            name = member.name.removeprefix("./")
                            if name.startswith("var/lib/dpkg/"):
                                with layer.extractfile(member) as content:
                                    base_dpkg_lines[name].update(content.read().split(b"\n"))
                    continue

                # RECORDs from an install layer cannot exempt changes added by a later layer.
                wheel_hashes = installed_wheel_hashes(layer, members)
                for member in members:
                    scan(member.name)
                    if member.issym() or member.islnk():
                        scan(member.linkname)
                    if not member.isfile():
                        continue
                    name = member.name.removeprefix("./")
                    with layer.extractfile(member) as content:
                        data = content.read()
                    digest = hashlib.sha256(data)
                    wheel_digest = base64.urlsafe_b64encode(digest.digest()).decode().rstrip("=")
                    if not RECORD.fullmatch(name) and (wheel_digest, len(data)) in wheel_hashes.get(name, ()):
                        totals.update(skipped_files=1, skipped_bytes=len(data))
                        continue
                    allowed = exceptions.get(name, {}).get(digest.hexdigest(), ())
                    upstream_lines = ()
                    if name in base_dpkg_lines:
                        # Compare raw lines before decoding, using the privacy check's LF boundary.
                        upstream_lines = {number for number, line in enumerate(data.split(b"\n"), 1)
                                          if line in base_dpkg_lines[name]}
                    scan(data.decode("utf-8", "replace"), allowed, upstream_lines)
                    totals.update(scanned_files=1, scanned_bytes=len(data))
    return +hits, totals


def main(path, base_path, image_id):
    try:
        from privacy_check import findings

        base = json.loads(Path(base_path).read_bytes())
        exceptions = json.loads(EXCEPTIONS.read_bytes())
        hits, totals = scan_image(path, base, image_id, findings, exceptions)
    except (ImportError, KeyError, OSError, TypeError, ValueError, EOFError, csv.Error, tarfile.TarError):
        print("Image privacy scan could not verify or read its inputs.", file=sys.stderr)
        return 2
    for pattern, count in sorted(hits.items()):
        print(f"{pattern}: {count}")
    print(f"Image privacy scan: {sum(hits.values())} hits")
    print(" ".join(f"{key}={totals[key]}" for key in (
        "scanned_files", "scanned_bytes", "skipped_files", "skipped_bytes", "excepted_hits")))
    return int(bool(hits))


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit("Usage: scan_public_image.py IMAGE_ARCHIVE BASE_CONFIG IMAGE_ID")
    sys.exit(main(*sys.argv[1:]))
