"""A reusable proof names the frozen source actually loaded by API and worker processes."""
import importlib.util
import json
import os
import subprocess
import sys

import pytest

from apps.api.services import catalogue_proof_identity as build_identity


@pytest.mark.parametrize('before,after', [('CAP = 59\n', 'CAP = 50\n'),
                                        ('class Limits:\n    maximum = 59\n', 'class Limits:\n    maximum = 50\n')])
def test_constant_and_schema_default_edits_invalidate_loaded_build(tmp_path, monkeypatch, before, after):
    source = tmp_path / 'apps' / 'api' / 'limits.py'
    source.parent.mkdir(parents=True)
    source.write_text(before)
    spec = importlib.util.spec_from_file_location('synthetic_limits', source)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    monkeypatch.setattr(build_identity, 'ROOT', tmp_path)
    monkeypatch.setattr(build_identity, '_STARTUP_SOURCE', build_identity._source_digest())
    original = build_identity.identity()
    assert original is not None
    source.write_text(after)
    assert (loaded.CAP if hasattr(loaded, 'CAP') else loaded.Limits.maximum) == 59
    assert build_identity.identity() is None
    # A newly started process pins different bytes; an old completed receipt cannot match it.
    monkeypatch.setattr(build_identity, '_STARTUP_SOURCE', build_identity._source_digest())
    assert build_identity.identity()['source_digest'] != original['source_digest']


def test_api_and_worker_bind_the_same_complete_startup_build():
    env = {**os.environ, 'ROTA_ENV': 'test', 'DATABASE_URL': 'sqlite:///:memory:'}
    results = []
    for surface in ('apps.api.main', 'apps.worker.main'):
        code = ('import ' + surface + '; import json; '
                'from apps.api.services.catalogue_proof_identity import identity; print(json.dumps(identity(), sort_keys=True))')
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, env=env, check=True)
        results.append(json.loads(result.stdout.splitlines()[-1]))
    assert results[0] is not None and results[0] == results[1]
    assert results[0]['source_digest'] and results[0]['dependencies']['ortools']
