"""Score-check work cannot hold the scheduling/email loop or inherit worker secrets."""
import os
import subprocess
import sys
import threading

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import score_check_isolation as isolation, score_check_provider as provider
from apps.worker import main, score_check as worker


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_blocked_extraction_does_not_delay_solves_or_email(request, backend, monkeypatch):
    from tests.test_score_check_ai import queued_file
    from tests.test_worker_email_fairness import backlog, remaining
    from apps.worker import email_delivery
    h = request.getfixturevalue(backend)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).score_check_enabled = True
    _profile, _batch, _source, extraction_id = queued_file(h)
    backlog(h, department_count=1, per_department=1)
    draft = h.workspace()
    job = h.command('coordinator', 'job.create', {'workspace_id': draft['id'], 'kind': 'generation'},
                    revision=draft['revision'])['job']
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    active, maximum = [0], [0]
    def blocked(*args, **kwargs):
        active[0] += 1
        maximum[0] = max(maximum[0], active[0])
        entered.set()
        assert release.wait(20), 'Test did not release its synthetic extraction'
        active[0] -= 1
        stopped.set()
        raise provider.ProviderError('SCORE_PROVIDER_UNAVAILABLE')
    monkeypatch.setattr(provider, 'extract', blocked)
    monkeypatch.setattr(email_delivery, 'send', lambda *args: None)
    loop = threading.Thread(target=worker.run_loop, args=(h.app.state.engine, h.settings, stopped))
    loop.start()
    try:
        assert entered.wait(10)
        assert main.process_one(h.app.state.engine, settings=h.settings,
            scheduler=lambda *args, **kwargs: {'status': 'candidate_feasible', 'additions': [], 'removals': []})
        assert remaining(h) == 0
        with transaction(h.app.state.engine) as db:
            assert db.get(m.Job, job['id']).status == 'succeeded'
            assert db.get(m.ScoreCheckExtractionJob, extraction_id).state == 'running'
    finally:
        release.set()
        stopped.set()
        loop.join(timeout=10)
    assert not loop.is_alive() and maximum[0] == 1


def test_child_environment_is_allowlisted_and_contains_no_worker_secrets(monkeypatch):
    for key in ('DATABASE_URL', 'DATABASE_URL_FILE', 'FMUC_DATA_KEY', 'FMUC_DATA_KEY_FILE',
                'OIDC_CLIENT_SECRET', 'AWS_SECRET_ACCESS_KEY', 'HTTP_PROXY', 'PYTHONPATH'):
        monkeypatch.setenv(key, 'synthetic-secret-must-not-cross')
    def inspect_child(*args, **kwargs):
        assert set(kwargs['env']) <= {'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'}
        assert 'synthetic-secret-must-not-cross' not in repr(kwargs['env'])
        raise OSError('Synthetic launch refusal')
    monkeypatch.setattr(isolation, '_privilege_options', lambda: {})
    monkeypatch.setattr(isolation.subprocess, 'Popen', inspect_child)
    with pytest.raises(isolation.IsolationError, match='unavailable'):
        isolation.run('media', {}, timeout=2)


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux prctl and POSIX subprocess credentials; Windows has no restricted token')
@pytest.mark.parametrize('separate', [False, True])
def test_linux_child_cannot_read_parent_environ_and_has_expected_credentials(separate):
    dockerfile = (isolation.ROOT / 'infra/Dockerfile').read_text(encoding='utf-8')
    [worker_user] = [line.split()[1] for line in dockerfile.splitlines() if line.startswith('USER ')]
    worker_uid, worker_gid = map(int, worker_user.split(':'))
    assert worker_uid > 0 and worker_gid > 0, 'The worker image must use non-root credentials'
    capabilities = '-all,+setuid,+setgid' if separate else '-all'
    launcher = ['sudo', '-n'] if os.geteuid() else []
    launcher += ['setpriv', '--reuid', str(worker_uid), '--regid', str(worker_gid), '--clear-groups',
                 f'--bounding-set={capabilities}', f'--inh-caps={capabilities}', f'--ambient-caps={capabilities}', '--']
    source_root = os.getenv('CI_UNIT_APP_SOURCE', str(isolation.ROOT))
    code = f'''
import json, os, subprocess, sys
sys.path.insert(0, {source_root!r})
from apps.api.services import score_check_isolation as isolation
assert (os.geteuid(), os.getegid(), os.getgroups()) == ({worker_uid}, {worker_gid}, [])
with open('/proc/self/status', encoding='utf-8') as status:
    effective = next(int(line.split()[1], 16) for line in status if line.startswith('CapEff:'))
assert effective == {((1 << 6) | (1 << 7)) if separate else 0}  # CAP_SETGID and CAP_SETUID
os.environ['ROTA_ENV'] = {'production' if separate else 'test'!r}
if {separate!r}:
    import pwd
    account = pwd.getpwnam('nobody')
    os.environ['ROTA_SCORE_CHECK_UID'] = str(account.pw_uid)
    os.environ['ROTA_SCORE_CHECK_GID'] = str(account.pw_gid)
options = isolation._privilege_options()
child = """import json, os
try:
    open('/proc/%s/environ' % os.getppid(), 'rb').read()
except PermissionError:
    print(json.dumps([True, os.getuid(), os.getgid(), os.getgroups()]))
else:
    print(json.dumps([False, os.getuid(), os.getgid(), os.getgroups()]))
"""
result = subprocess.run([sys.executable, '-I', '-c', child], cwd='/tmp', capture_output=True, timeout=5, check=True,
                        env={{}}, **options)
denied, uid, gid, groups = json.loads(result.stdout)
assert denied
if {separate!r}:
    assert uid == account.pw_uid and gid == account.pw_gid and groups == []
print('protected')
'''
    result = subprocess.run([*launcher, sys.executable, '-I', '-B', '-c', code],
                            cwd='/tmp', env={}, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout.strip() == b'protected'


@pytest.mark.parametrize('uid,gid', [('0', '1'), ('1', '0'), ('1', None), ('bad', '2'), ('1', '2')])
def test_invalid_or_nonseparate_child_credentials_fail_closed(monkeypatch, uid, gid):
    monkeypatch.setattr(isolation.sys, 'platform', 'linux')
    monkeypatch.setattr(isolation, 'protect_parent', lambda: None)
    monkeypatch.setattr(os, 'geteuid', lambda: 1, raising=False)
    monkeypatch.setattr(os, 'getegid', lambda: 2, raising=False)
    monkeypatch.setenv('ROTA_SCORE_CHECK_UID', uid)
    if gid is None:
        monkeypatch.delenv('ROTA_SCORE_CHECK_GID', raising=False)
    else:
        monkeypatch.setenv('ROTA_SCORE_CHECK_GID', gid)
    with pytest.raises(OSError):
        isolation._privilege_options()


def test_root_requires_explicit_child_credentials_and_nonroot_warns(monkeypatch, caplog):
    monkeypatch.setattr(isolation.sys, 'platform', 'linux')
    monkeypatch.setattr(isolation, 'protect_parent', lambda: None)
    monkeypatch.delenv('ROTA_SCORE_CHECK_UID', raising=False)
    monkeypatch.delenv('ROTA_SCORE_CHECK_GID', raising=False)
    monkeypatch.setattr(os, 'geteuid', lambda: 0, raising=False)
    with pytest.raises(OSError):
        isolation._privilege_options()
    monkeypatch.setattr(os, 'geteuid', lambda: 10001)
    monkeypatch.setattr(isolation, '_warned_same_uid', False)
    assert isolation._privilege_options() == {}
    assert 'score_check_child_same_uid' in caplog.text


def test_configured_child_credentials_clear_supplementary_groups(monkeypatch):
    monkeypatch.setattr(isolation.sys, 'platform', 'linux')
    monkeypatch.setattr(isolation, 'protect_parent', lambda: None)
    monkeypatch.setattr(os, 'geteuid', lambda: 10001, raising=False)
    monkeypatch.setattr(os, 'getegid', lambda: 10001, raising=False)
    monkeypatch.setenv('ROTA_SCORE_CHECK_UID', '10002')
    monkeypatch.setenv('ROTA_SCORE_CHECK_GID', '10002')
    assert isolation._privilege_options() == {'user': 10002, 'group': 10002, 'extra_groups': []}


def test_parent_protection_failure_refuses_linux_launch(monkeypatch):
    import ctypes
    from unittest.mock import Mock
    monkeypatch.setattr(isolation.sys, 'platform', 'linux')
    monkeypatch.setattr(ctypes, 'CDLL', Mock(return_value=Mock(prctl=Mock(return_value=-1))))
    with pytest.raises(OSError, match='Cannot protect'):
        isolation.protect_parent()


def test_child_refuses_readable_parent_before_parsers(monkeypatch):
    import io
    monkeypatch.setattr(isolation.sys, 'platform', 'linux')
    monkeypatch.setattr('builtins.open', lambda *args, **kwargs: io.BytesIO(b'synthetic-secret'))
    with pytest.raises(OSError, match='parent secrets'):
        isolation._check_parent()


@pytest.mark.parametrize('environment', [None, '', 'production'])
@pytest.mark.parametrize('operation', ['media', 'request'])
def test_production_without_separate_credentials_refuses_launch_and_reports_isolation_unavailable(
        monkeypatch, environment, operation):
    monkeypatch.setattr(isolation.sys, 'platform', 'linux')
    monkeypatch.setattr(isolation, 'protect_parent', lambda: None)
    monkeypatch.setattr(os, 'geteuid', lambda: 10001, raising=False)
    monkeypatch.setenv('ROTA_DEPLOYMENT', 'dev')  # A display badge never relaxes the runtime boundary.
    if environment is None:
        monkeypatch.delenv('ROTA_ENV', raising=False)
    else:
        monkeypatch.setenv('ROTA_ENV', environment)
    for key in ('ROTA_SCORE_CHECK_UID', 'ROTA_SCORE_CHECK_GID'):
        monkeypatch.delenv(key, raising=False)
    def forbidden(*args, **kwargs):
        pytest.fail('Unconfigured production isolation launched a child')
    monkeypatch.setattr(isolation.subprocess, 'Popen', forbidden)
    with pytest.raises(OSError, match='isolation unavailable'):
        isolation._privilege_options()
    with pytest.raises(provider.ProviderError) as failure:
        if operation == 'media':
            provider.prepare_file('synthetic-file', 'synthetic.pdf', b'%PDF-')
        else:
            provider._send('https://example.test', {}, {}, 2, False, None)
    assert failure.value.code == 'SCORE_ISOLATION_UNAVAILABLE'
    assert 'isolation is unavailable' in str(failure.value)


def test_production_rejects_platform_without_child_credential_separation(monkeypatch):
    monkeypatch.setattr(isolation.sys, 'platform', 'win32')
    monkeypatch.setattr(isolation, 'protect_parent', lambda: None)
    monkeypatch.setenv('ROTA_ENV', 'production')
    monkeypatch.setenv('ROTA_SCORE_CHECK_UID', '10002')
    monkeypatch.setenv('ROTA_SCORE_CHECK_GID', '10002')
    with pytest.raises(OSError, match='production requires Linux'):
        isolation._privilege_options()


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_production_media_refusal_is_clear_in_api_validation_and_worker_job(request, backend, monkeypatch):
    from sqlalchemy import func, select
    from apps.api.errors import Problem
    from apps.api.services import score_check
    from tests.test_score_check_ai import actor, queued_file
    h = request.getfixturevalue(backend)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).score_check_enabled = True
    _profile, _batch, source_id, job_id = queued_file(h, file_name='synthetic.pdf', content=b'%PDF-')
    monkeypatch.setenv('ROTA_ENV', 'production')
    for key in ('ROTA_SCORE_CHECK_UID', 'ROTA_SCORE_CHECK_GID'):
        monkeypatch.delenv(key, raising=False)
    def forbidden(*args, **kwargs):
        pytest.fail('Production media refusal must precede process launch or provider traffic')
    monkeypatch.setattr(isolation.subprocess, 'Popen', forbidden)
    monkeypatch.setattr(provider, 'extract', forbidden)
    with transaction(h.app.state.engine) as db:
        source = db.get(m.ImportSource, source_id)
        value = {'files': [{'file_ref': source.file_ref, 'layout': 'other', 'rows_read': 0, 'confidence': 0}]}
        with pytest.raises(Problem) as failure:
            score_check.validate_extraction(db, actor(h, db, 'coordinator'), source, value)
        assert failure.value.status == 503 and failure.value.code == 'SCORE_ISOLATION_UNAVAILABLE'
    assert worker.process_one(h.app.state.engine, h.settings)
    with transaction(h.app.state.engine) as db:
        job = db.get(m.ScoreCheckExtractionJob, job_id)
        assert job.state == 'failed' and job.error_code == 'SCORE_ISOLATION_UNAVAILABLE'
        assert db.scalar(select(func.count()).select_from(m.AiCall)) == 0
