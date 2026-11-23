"""Apply the repository privacy patterns to a Docker image archive before publication."""

from collections import Counter
import json
import sys
import tarfile


def scan_image(path, findings):
    hits = Counter()

    def scan(text):
        hits.update(pattern for _, pattern in findings(text))

    def scan_file(file):
        for line in file:
            scan(line.decode("utf-8", "replace"))

    with tarfile.open(path) as image:
        with image.extractfile("manifest.json") as file:
            manifest, = json.load(file)
        with image.extractfile(manifest["Config"]) as file:
            scan_file(file)
        if not manifest["Layers"]:
            raise ValueError("Image archive has no layers")
        # Removed files still ship in earlier layers. Read the archives without extracting them.
        for name in manifest["Layers"]:
            with image.extractfile(name) as file, tarfile.open(fileobj=file, mode="r|*") as layer:
                for member in layer:
                    scan(member.name)
                    if member.issym() or member.islnk():
                        scan(member.linkname)
                    if member.isfile():
                        with layer.extractfile(member) as content:
                            scan_file(content)
    return hits


def main(path):
    try:
        from privacy_check import findings

        hits = scan_image(path, findings)
    except (ImportError, KeyError, OSError, TypeError, ValueError, tarfile.TarError):
        print("Image privacy scan failed to read its inputs.", file=sys.stderr)
        return 2
    for pattern, count in sorted(hits.items()):
        print(f"{pattern}: {count}")
    print(f"Image privacy scan: {sum(hits.values())} hits")
    return int(bool(hits))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: scan_public_image.py IMAGE_ARCHIVE")
    sys.exit(main(sys.argv[1]))
