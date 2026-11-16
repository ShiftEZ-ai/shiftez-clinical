"""Public build-artifact identity shared by the packager and HTTP asset server."""
import hashlib
import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / 'apps/web/dist'


def bundle_directory(web=WEB):
    """The index owns the running bundle's identity; never fall back to an unversioned copy."""
    versions = re.findall(r'<script type="module" src="/assets/([0-9a-f]{64})/app\.js"',
                          (web / 'index.html').read_text(encoding='utf-8'))
    if len(versions) != 1:
        raise ValueError('Expected one versioned web module entry point')
    return web / 'assets' / versions[0]


def available_bundle_version(web=WEB):
    """API-only instances have no frontend identity to announce to open tabs."""
    try:
        return bundle_directory(web).name
    except (FileNotFoundError, ValueError):
        return None


def asset_url(name, web=WEB):
    """Public URL for a trusted bundle-relative filename (branding, print and release probes)."""
    return '/' + (bundle_directory(web) / name).relative_to(web).as_posix()


def versioned_asset(path):
    version, separator, _ = path.lstrip('/').partition('/')
    return bool(separator and re.fullmatch('[0-9a-f]{' + str(hashlib.sha256().digest_size * 2) + '}', version))


def bundle_version(assets):
    digest = hashlib.sha256()
    for path in sorted(p for p in assets.rglob('*') if p.is_file()):
        if path.is_symlink():
            raise ValueError('Public build assets must be regular files')
        digest.update(path.relative_to(assets).as_posix().encode() + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()
