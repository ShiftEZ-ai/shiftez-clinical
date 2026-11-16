"""Pin the complete source build before application imports; edited processes cannot issue reusable proof."""
import hashlib
import os
import platform
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _source_digest():
    paths = sorted(path for folder in ('apps/api', 'apps/worker', 'packages')
                   for path in (ROOT / folder).rglob('*.py') if '__pycache__' not in path.parts)
    paths += sorted((ROOT / 'requirements').glob('*.lock'))
    if not paths:
        raise OSError('Proof source unavailable')
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(ROOT).as_posix().encode() + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()


# API and worker import this first, while their qualified source is frozen. Never repin a running process.
try:
    _STARTUP_SOURCE = _source_digest()
    _STARTUP_DEPENDENCIES = {name: version(name) for name in
        ('ortools', 'pydantic', 'pydantic-core', 'SQLAlchemy', 'fastapi', 'numpy', 'protobuf')}
    _STARTUP_BUILD = {name: os.getenv(name, '') for name in
        ('ROTA_BUILD_COMMIT', 'ROTA_BUILD_DATE', 'ROTA_IMAGE_DIGEST')}
except (OSError, PackageNotFoundError):
    _STARTUP_SOURCE = None


def identity():
    """Source-only constant/default edits invalidate already loaded bytecode as well as changed functions."""
    try:
        if _STARTUP_SOURCE is None or _source_digest() != _STARTUP_SOURCE:
            return None
    except OSError:
        return None
    from packages.domain import fairness, generation, rule_migration_proof, score, solver_rules, validation
    return {'source_digest': _STARTUP_SOURCE,
            'python': platform.python_version(), 'implementation': sys.implementation.cache_tag,
            'optimize': sys.flags.optimize, 'dependencies': _STARTUP_DEPENDENCIES, 'build': _STARTUP_BUILD,
            'proof_version': rule_migration_proof.VERSION, 'compiler': solver_rules.COMPILER_VERSION,
            'validator': validation.ENGINE_VERSION, 'greedy': generation.ALGORITHM_VERSION,
            'score': score.VERSION, 'fairness': fairness.VERSION}
