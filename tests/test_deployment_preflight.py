"""Real local settings/files/SQLite checks; PG/containers have separate real gates."""
from dataclasses import replace
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import pytest
from sqlalchemy import text
from apps.api.db import transaction
from apps.api.settings import Settings, secret_from_env, MAX_SECRET_BYTES
from scripts import backup, preflight

ROOT = Path(__file__).resolve().parents[1]


def clean_env(monkeypatch):
    for key in ('DATABASE_URL','DATABASE_URL_FILE','OIDC_CLIENT_SECRET','OIDC_CLIENT_SECRET_FILE','ROTA_ENV','PUBLIC_ORIGIN','ROTA_IMAGE','ROTA_ALLOW_DISPOSABLE_TEST_DB','ROTA_ALLOW_TEST_ROLES','FMUC_DATA_KEY','FMUC_DATA_KEY_FILE'):
        monkeypatch.delenv(key,raising=False)


def production(**changes):
    return replace(Settings(environment='production',database_url='postgresql+psycopg://runtime:fictional@db.invalid/rota',
        public_origin='https://rota.invalid',oidc_issuer='https://id.invalid/realms/rota',oidc_client_id='rota',
        oidc_client_secret='fictional-secret',oidc_redirect_uri='https://rota.invalid/auth/callback',oidc_allowed_hosts='id.invalid'),**changes)


def test_worker_preflight_uses_worker_startup_contract_without_api_secrets(monkeypatch):
    clean_env(monkeypatch)
    settings = production(identity_users_file='', fmuc_data_key='', password_login_enabled=False)
    api = preflight.inspect(settings, require_production=True)
    worker = preflight.inspect(settings, require_production=True, worker=True)
    assert next(c for c in api['checks'] if c['check'] == 'configuration')['status'] == 'failed'
    assert next(c for c in worker['checks'] if c['check'] == 'configuration')['status'] == 'passed'
    assert api['runtime_role'] == 'api' and worker['runtime_role'] == 'worker'
    assert not worker['connection_requested'] and not worker['production_ready']
    assert not preflight.inspect(replace(settings, database_url='sqlite:///not-created.db'), worker=True)['passed']


@pytest.mark.parametrize('ending',['','\n','\r\n'])
def test_files_load_exact_secrets_with_single_optional_line_ending(tmp_path,monkeypatch,ending):
    clean_env(monkeypatch)
    path=tmp_path/'secret';path.write_text('value:with spaces'+ending);path.chmod(0o600)
    monkeypatch.setenv('OIDC_CLIENT_SECRET_FILE',str(path));monkeypatch.setenv('ROTA_ENV','production')
    assert secret_from_env('OIDC_CLIENT_SECRET')=='value:with spaces'
    assert Settings.from_env().oidc_client_secret=='value:with spaces'


@pytest.mark.parametrize('value',['', 'x\ny', 'x\r', 'x\x00y','x'*(MAX_SECRET_BYTES+1)])
def test_empty_multiline_control_and_oversized_files_fail_without_content(tmp_path,monkeypatch,value):
    clean_env(monkeypatch);path=tmp_path/'secret';path.write_text(value)
    monkeypatch.setenv('DATABASE_URL_FILE',str(path))
    with pytest.raises(RuntimeError) as error:secret_from_env('DATABASE_URL')
    assert str(path) not in str(error.value)


@pytest.mark.parametrize('kind',['missing','directory','symlink','fifo','relative','invalid_utf8','world_readable'])
def test_nonregular_unsafe_or_unprotected_production_file_is_refused(tmp_path,monkeypatch,kind):
    clean_env(monkeypatch);path=tmp_path/'secret'
    monkeypatch.setenv('ROTA_ENV','production')
    if kind=='directory':path.mkdir()
    elif kind=='symlink':
        target=tmp_path/'actual';target.write_text('private');target.chmod(0o600);path.symlink_to(target)
    elif kind=='fifo':os.mkfifo(path,0o600)
    elif kind=='relative':path=Path('relative')
    elif kind=='invalid_utf8':path.write_bytes(b'\xff');path.chmod(0o600)
    elif kind=='world_readable':path.write_text('private');path.chmod(0o644)
    monkeypatch.setenv('DATABASE_URL_FILE',str(path))
    with pytest.raises(RuntimeError):secret_from_env('DATABASE_URL')


def test_group_readable_mounted_secret_and_conflicting_direct_setting(tmp_path,monkeypatch):
    clean_env(monkeypatch);path=tmp_path/'secret';path.write_text('private');path.chmod(0o440)
    monkeypatch.setenv('ROTA_ENV','production');monkeypatch.setenv('DATABASE_URL_FILE',str(path))
    assert secret_from_env('DATABASE_URL')=='private'
    monkeypatch.setenv('DATABASE_URL','')
    with pytest.raises(RuntimeError,match='never both'):Settings.from_env()


def test_repr_omits_credentials_and_direct_environment_still_works(monkeypatch):
    clean_env(monkeypatch);monkeypatch.setenv('DATABASE_URL','sqlite:///fictional.db');monkeypatch.setenv('OIDC_CLIENT_SECRET','fictional-secret')
    settings=Settings.from_env();assert settings.database_url=='sqlite:///fictional.db'
    assert 'fictional-secret' not in repr(settings) and 'fictional.db' not in repr(settings)
    assert 'fictional-secret' not in repr(production()) and 'runtime:fictional' not in repr(production())


@pytest.mark.parametrize('origin',[('https://name:secret' + chr(64) + 'rota.invalid'),'https://@rota.invalid','https://rota.invalid?q=x',
    'https://rota.invalid?','https://rota.invalid#','https://rota.invalid/path',' https://rota.invalid',
    'https://rota.invalid\n','https://rota.invalid:bad','https://rota.invalid:99999','https://[bad','https://rota.invalid\\other'])
def test_origin_validation_rejects_ambiguous_sensitive_or_nonorigin_values(origin):
    with pytest.raises(RuntimeError):production(public_origin=origin).check()


def test_catalogue_cache_entries_are_loaded_and_bounded(monkeypatch):
    monkeypatch.setenv('ROTA_CATALOGUE_CACHE_ENTRIES', '7')
    assert Settings.from_env().catalogue_cache_entries == 7
    replace(Settings(environment='test'), catalogue_cache_entries=1).check()
    with pytest.raises(RuntimeError, match='ROTA_CATALOGUE_CACHE_ENTRIES'):
        replace(Settings(environment='test'), catalogue_cache_entries=0).check()
    with pytest.raises(RuntimeError, match='ROTA_CATALOGUE_CACHE_ENTRIES'):
        replace(Settings(environment='test'), catalogue_cache_entries=4097).check()


@pytest.mark.parametrize('issuer',['http://id.invalid',('https://name:secret' + chr(64) + 'id.invalid'),'https://id.invalid?query=x','https://id.invalid#part'])
def test_oidc_trust_url_has_no_embedded_credentials_queries_or_fragments(issuer):
    from packages.contracts.schemas import AuthSettings
    with pytest.raises(ValueError):AuthSettings(issuer=issuer)


def test_production_requires_managed_passwords_and_refuses_sqlite_secret_file_shortcut(tmp_path,monkeypatch):
    clean_env(monkeypatch)
    users = tmp_path / 'users.json'; users.write_text('{"users": {}}', encoding='utf-8')
    production(identity_users_file=str(users), fmuc_data_key='01' * 32).check()
    path=tmp_path/'dburl';path.write_text('sqlite:///example.db');path.chmod(0o600)
    monkeypatch.setenv('DATABASE_URL_FILE',str(path));monkeypatch.setenv('ROTA_ENV','production')
    with pytest.raises(RuntimeError,match='PostgreSQL'):Settings.from_env().check()


def test_local_preflight_real_readonly_current_schema_and_no_audit_changes(h):
    from .test_catalogue_preview import state
    before=state(h);result=preflight.inspect(h.settings,connect=True)
    assert result['passed'],result
    assert not result['production_ready'] and state(h)==before
    assert next(c for c in result['checks'] if c['check']=='database_migration')['status']=='passed'
    assert next(c for c in result['checks'] if c['check']=='runtime_privileges')['status']=='not_applicable'
    assert str(h.settings.database_url) not in json.dumps(result)


def test_readonly_preflight_does_not_create_missing_database_or_parent(tmp_path):
    path=tmp_path/'never-create'/'missing.db'
    result=preflight.inspect(Settings(environment='test',database_url='sqlite:///'+str(path)),connect=True)
    assert not result['passed'] and not path.exists() and not path.parent.exists()


def test_preflight_fails_stale_schema_and_never_migrates(h):
    with transaction(h.app.state.engine,write=True) as db:db.execute(text("UPDATE alembic_version SET version_num='older'"))
    result=preflight.inspect(h.settings,connect=True);assert not result['passed']
    with h.app.state.engine.connect() as db:assert db.scalar(text('SELECT version_num FROM alembic_version'))=='older'


def test_preflight_rejects_missing_evidence_even_with_the_current_revision(h):
    with h.app.state.engine.begin() as db:
        db.execute(text('ALTER TABLE reference_imports RENAME TO synthetic_missing_evidence'))
    result = preflight.inspect(h.settings, connect=True)
    checks = {row['check']: row for row in result['checks']}
    assert checks['database_migration']['status'] == 'passed'
    assert checks['database_tables']['status'] == 'failed'
    assert 'reference_imports' in checks['database_tables']['detail']
    assert not result['passed']


def test_configuration_only_does_not_connect_and_is_not_acceptance(h,monkeypatch):
    def forbidden(*a,**kw):raise AssertionError('No database connections permitted for this check')
    monkeypatch.setattr(preflight,'create_engine',forbidden)
    result=preflight.inspect(h.settings,connect=False)
    assert result['passed'] and not result['connection_requested'] and not result['production_ready']
    assert any(c['status']=='not_run' for c in result['checks'])
    assert not preflight.inspect(h.settings,require_production=True)['passed']
    assert not preflight.inspect(h.settings,release_image='repo/app:latest')['passed']
    assert preflight.inspect(h.settings,release_image='registry.invalid/app@sha256:'+'a'*64)['passed']


@pytest.mark.parametrize('key', ['', 'malformed-synthetic-key', 'ab' * 32], ids=['missing', 'invalid', 'valid'])
def test_production_password_key_preflight_rejects_missing_or_invalid_keys_without_connecting(tmp_path, monkeypatch, key):
    clean_env(monkeypatch)
    users = tmp_path / 'users.yml'
    users.write_text('{}', encoding='utf-8')
    settings = production(password_login_enabled=True, identity_users_file=str(users),
                          oidc_issuer='https://rota.invalid/authelia', fmuc_data_key=key)
    connections = []
    def forbidden(*a, **kw):
        connections.append(True)
        raise AssertionError('A configuration refusal must not connect to the database')
    monkeypatch.setattr(preflight, 'create_engine', forbidden)
    valid = key == 'ab' * 32
    result = preflight.inspect(settings, connect=not valid, require_production=True)
    assert not connections
    configuration = next(row for row in result['checks'] if row['check'] == 'configuration')
    assert (configuration['status'] == 'passed') == valid
    if not valid:
        assert not result['passed'] and 'FMUC_DATA_KEY' in configuration['detail']
        assert next(row for row in result['checks'] if row['check'] == 'database_connection_and_checks')['status'] == 'not_run'
    encoded = json.dumps(result)
    assert str(users) not in encoded and settings.database_url not in encoded
    if key:
        assert key not in encoded


@pytest.mark.parametrize('enabled', [False, True])
def test_production_password_key_preflight_preserves_fixed_mode(tmp_path, monkeypatch, enabled):
    # Fixed mode stays supported with a key; sign-in event and rate-limit keys are keyed hashes (#930), so a keyless
    # production cannot store them safely and is refused at startup, naming the setting only.
    clean_env(monkeypatch)
    users = tmp_path / 'users.yml'
    users.write_text('{}', encoding='utf-8')
    settings = production(password_login_enabled=enabled, password_login_progressive_delay=False,
                          identity_users_file=str(users), oidc_issuer='https://rota.invalid/authelia')
    keyless = next(row for row in preflight.inspect(settings, require_production=True)['checks']
                   if row['check'] == 'configuration')
    assert keyless['status'] == 'failed' and keyless['detail'] == 'FMUC_DATA_KEY is required for sign-in in production'
    keyed = preflight.inspect(replace(settings, fmuc_data_key='01' * 32), require_production=True)
    assert next(row for row in keyed['checks'] if row['check'] == 'configuration')['status'] == 'passed'


@pytest.mark.parametrize('enabled', [False, True])
def test_production_keyless_password_preflight_refusal_does_not_depend_on_legacy_flag(tmp_path, monkeypatch, enabled):
    clean_env(monkeypatch)
    users = tmp_path / 'users.yml'
    users.write_text('{}', encoding='utf-8')
    settings = production(password_login_enabled=enabled, identity_users_file=str(users))
    result = preflight.inspect(settings, require_production=True)
    configuration = next(row for row in result['checks'] if row['check'] == 'configuration')
    assert configuration['status'] == 'failed' and not result['passed']
    assert configuration['detail'] == 'FMUC_DATA_KEY is required for progressive password sign-in'


@pytest.mark.parametrize('failure', ['missing', 'invalid', 'secret-file'])
def test_cli_key_configuration_refusal_is_visible_on_stderr_without_secret_values(tmp_path, monkeypatch, capsys, failure):
    clean_env(monkeypatch)
    users = tmp_path / 'private-users.yml'
    users.write_text('{}', encoding='utf-8')
    secret = 'malformed-synthetic-key'
    settings = production(password_login_enabled=True, identity_users_file=str(users),
                          oidc_issuer='https://rota.invalid/authelia', fmuc_data_key=secret if failure == 'invalid' else '')
    def load():
        if failure == 'secret-file':
            raise RuntimeError(f'Unreadable {users}: {secret}')
        return settings
    monkeypatch.setattr(preflight.Settings, 'from_env', load)
    monkeypatch.setattr(sys, 'argv', ['preflight.py', '--require-production'])
    with pytest.raises(SystemExit) as refusal:
        preflight.main()
    assert refusal.value.code == 1
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert not result['passed']
    assert ('Environment or secret-file loading failed' if failure == 'secret-file' else 'FMUC_DATA_KEY') in output.err
    assert secret not in output.out + output.err and str(users) not in output.out + output.err
    assert settings.database_url not in output.out + output.err


def test_cli_uses_secret_file_and_private_nonoverwriting_report(h,tmp_path,monkeypatch):
    clean_env(monkeypatch);path=tmp_path/'url';path.write_text(h.settings.database_url);path.chmod(0o600)
    env={**os.environ,'DATABASE_URL_FILE':str(path),'ROTA_ENV':'test'};out=tmp_path/'preflight.json'
    command=[sys.executable,str(ROOT/'scripts/preflight.py'),'--connect','--output',str(out)]
    first=subprocess.run(command,env=env,capture_output=True,text=True,timeout=20)
    assert first.returncode==0,first.stdout+first.stderr
    assert stat.S_IMODE(out.stat().st_mode)==0o600
    assert json.loads(out.read_text())['passed']
    before=out.read_bytes();again=subprocess.run(command,env=env,capture_output=True,text=True,timeout=20)
    assert again.returncode!=0 and out.read_bytes()==before
    assert h.settings.database_url not in first.stdout and str(path) not in first.stdout


def test_backup_utilities_cannot_inherit_another_database_or_password(monkeypatch):
    for key in ('PGPASSWORD','PGSERVICE','PGSERVICEFILE','PGHOSTADDR','PGOPTIONS','PGPASSFILE','PGSSLMODE'):
        monkeypatch.setenv(key,'UNAPPROVED-AMBIENT')
    monkeypatch.setenv('DATABASE_URL','postgresql+psycopg://other:OTHER-SECRET@wrong/wrong')
    monkeypatch.setenv('OIDC_CLIENT_SECRET','OTHER-SECRET')
    monkeypatch.setenv('UNRELATED_API_TOKEN','OTHER-SECRET')
    env=backup.pg_environment('postgresql+psycopg://named:secret@db.invalid:5433/rota?sslmode=verify-full&connect_timeout=7')
    assert env['PGHOST']=='db.invalid' and env['PGPORT']=='5433' and env['PGDATABASE']=='rota' and env['PGUSER']=='named'
    assert env['PGSSLMODE']=='verify-full' and env['PGCONNECT_TIMEOUT']=='7'
    assert env['PGPASSFILE']==os.devnull and 'PGPASSWORD' not in env
    assert 'OTHER-SECRET' not in str(env)
    assert not any(v=='UNAPPROVED-AMBIENT' for k,v in env.items() if k.startswith('PG'))


@pytest.mark.parametrize('url',['postgresql+psycopg:///rota','postgresql+psycopg://u@host/rota?service=other',
    'postgresql+psycopg://u@host/rota?sslmode=require&sslmode=disable'])
def test_ambiguous_or_unsupported_utility_target_is_refused(url):
    with pytest.raises(ValueError):backup.pg_environment(url)


def test_pg_password_file_is_escaped_private_and_removed_even_on_failure():
    filename=None
    with pytest.raises(RuntimeError,match='synthetic'):
        with backup.pg_credentials('postgresql+psycopg://user:colon%3Aslash%5Cend@db.invalid/rota') as env:
            filename=Path(env['PGPASSFILE']);assert filename.exists() and stat.S_IMODE(filename.stat().st_mode)==0o600
            assert filename.read_text()=='db.invalid:5432:rota:user:colon\\:slash\\\\end\n'
            assert 'PGPASSWORD' not in env
            raise RuntimeError('synthetic utility error')
    assert not filename.exists()


def test_pg_dump_subprocess_receives_private_file_not_credentials_in_argv_or_env(tmp_path,monkeypatch):
    """A subprocess contract fixture, NOT execution of PostgreSQL or pg_dump."""
    used=[]
    def fixture_run(argv,**kw):
        assert argv==['pg_dump','--no-password','--format=custom','--no-owner','--no-privileges']
        assert 'PGPASSWORD' not in kw['env'];path=Path(kw['env']['PGPASSFILE']);used.append(path)
        assert path.exists();kw['stdout'].write(b'synthetic-custom-dump-for-subprocess-contract-only')
    monkeypatch.setattr(backup.subprocess,'run',fixture_run)
    data=backup.backup('postgresql+psycopg://named:secret@db.invalid/rota',tmp_path/'dump')
    assert data['kind']=='postgresql-custom' and not used[0].exists()
    assert 'secret' not in json.dumps(data)


@pytest.mark.parametrize('action,environment',[('init-demo','production'),('init-institution','test')])
def test_invalid_bootstrap_request_cannot_migrate_or_create_a_database(tmp_path,monkeypatch,action,environment):
    clean_env(monkeypatch);path=tmp_path/'must-not-be-created.db'
    result=subprocess.run([sys.executable,str(ROOT/'scripts/manage.py'),action],
        env={**os.environ,'ROTA_ENV':environment,'DATABASE_URL':'sqlite:///'+str(path)},capture_output=True,text=True,timeout=20)
    assert result.returncode!=0 and not path.exists()


def test_explicit_database_url_file_is_shared_by_migration_and_backup_clis(tmp_path,monkeypatch):
    clean_env(monkeypatch);db=tmp_path/'migrated.db';secret=tmp_path/'db-url';secret.write_text('sqlite:///'+str(db));secret.chmod(0o600)
    env={**os.environ,'ROTA_ENV':'test','DATABASE_URL_FILE':str(secret)}
    migrated=subprocess.run([sys.executable,str(ROOT/'scripts/manage.py'),'migrate'],env=env,capture_output=True,text=True,timeout=90)
    assert migrated.returncode==0,migrated.stdout+migrated.stderr
    assert db.exists()
    dumped=subprocess.run([sys.executable,str(ROOT/'scripts/backup.py'),'backup',str(tmp_path/'logical.dump')],env=env,capture_output=True,text=True,timeout=20)
    assert dumped.returncode==0,dumped.stdout+dumped.stderr
    from apps.api.schema import supported_revision
    assert json.loads(dumped.stdout)['snapshot']['schema_head']==supported_revision()


def test_production_migrate_cannot_silently_create_a_sqlite_installation(tmp_path,monkeypatch):
    clean_env(monkeypatch);path=tmp_path/'not-production.db'
    result=subprocess.run([sys.executable,str(ROOT/'scripts/manage.py'),'migrate'],
        env={**os.environ,'ROTA_ENV':'production','DATABASE_URL':'sqlite:///'+str(path)},capture_output=True,text=True,timeout=20)
    assert result.returncode!=0 and not path.exists()
    assert 'PostgreSQL' in result.stderr
