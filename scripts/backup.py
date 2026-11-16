#!/usr/bin/env python3
"""Explicit, non-overwriting logical backup/restore. Not a production PITR service.

Archives and manifests contain sensitive institutional material. Store outside the
repository in approved encrypted storage. Restore only trusted authenticated backups.
"""
import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.api.settings import secret_from_env
from sqlalchemy.engine import make_url

SCHEMA=1

def sha256(path:Path)->str:
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def reserve(path:Path):
    path.parent.mkdir(parents=True,exist_ok=True)
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    return os.fdopen(descriptor,'wb')

def sqlite_check(path):
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        integrity=db.execute('PRAGMA integrity_check').fetchall()
        foreign=db.execute('PRAGMA foreign_key_check').fetchall()
        if integrity!=[('ok',)] or foreign:raise RuntimeError('SQLite integrity/foreign-key verification failed')
        heads=db.execute('SELECT version_num FROM alembic_version').fetchall()
        if len(heads)!=1:raise RuntimeError('A backup must have exactly one recorded migration head')
        # Closed identifier inventory. Earlier schemas are backed up without
        # inventing absent tables; current snapshots count every new evidence set.
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        tracked=('publications','audit_events','outbox','assignments','shifts','definition_history',
                 'review_routes','review_delegations','review_decisions','swap_requests','swap_events',
                 'planner_views','operating_calendars','resource_pools','recurring_series','feedback_reports')
        return {'schema_head':heads[0][0],'counts':{t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in tracked if t in tables}}

def sqlite_copy(source:Path,target:Path):
    if not source.is_file():raise FileNotFoundError('Source database does not exist')
    with reserve(target):pass
    try:
        with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)) as src, closing(sqlite3.connect(target)) as dst:src.backup(dst)
        return sqlite_check(target)
    except BaseException:
        target.unlink(missing_ok=True)
        raise

def pg_environment(url):
    """Explicit target only: ambient PGHOSTADDR/PGSERVICE/options cannot redirect it."""
    parsed=make_url(url)
    if parsed.get_backend_name()!='postgresql' or not all((parsed.host,parsed.username,parsed.database)):
        raise ValueError('An explicit PostgreSQL host, user and database are required')
    if '=' in parsed.database or parsed.database.startswith(('postgresql://','postgres://')):
        raise ValueError('A literal database name, not an embedded libpq connection string, is required')
    options={'sslmode':'PGSSLMODE','sslrootcert':'PGSSLROOTCERT','sslcert':'PGSSLCERT',
             'sslkey':'PGSSLKEY','options':'PGOPTIONS','connect_timeout':'PGCONNECT_TIMEOUT'}
    if set(parsed.query)-set(options):
        raise ValueError('Unsupported PostgreSQL utility URL option; do not silently change connection meaning')
    if any(not isinstance(value,str) for value in parsed.query.values()):
        raise ValueError('Repeated PostgreSQL utility URL options are unsupported')
    # Utilities do not need application/OIDC credentials or other inherited secrets.
    allowed={'PATH','HOME','LANG','LC_ALL','LC_CTYPE','LC_MESSAGES','TZ','TMPDIR','SYSTEMROOT'}
    env={key:value for key,value in os.environ.items() if key in allowed}
    env.update({'PGHOST':parsed.host,'PGPORT':str(parsed.port or 5432),'PGUSER':parsed.username,
                'PGDATABASE':parsed.database,'PGPASSFILE':os.devnull,'PGCONNECT_TIMEOUT':'10'})
    for key,value in parsed.query.items():env[options[key]]=value
    return env


@contextmanager
def pg_credentials(url):
    """Use a private temporary libpq password file, not argv or PGPASSWORD.

    Parent environment contains no inherited PG connection settings. TLS/client
    certificates are allowed only through the explicitly selected URL options.
    Host tmp storage must be approved; no logical backup qualifies that storage.
    """
    env=pg_environment(url);parsed=make_url(url);filename=None
    try:
        if parsed.password is not None:
            def escape(value):
                if any(c in value for c in ('\n','\r','\x00')):raise ValueError('PostgreSQL password-file fields must be single line')
                return value.replace('\\','\\\\').replace(':','\\:')
            line=':'.join(escape(value) for value in
                (env['PGHOST'],env['PGPORT'],env['PGDATABASE'],env['PGUSER'],parsed.password))+'\n'
            descriptor,filename=tempfile.mkstemp(prefix='rota-pgpass-')
            with os.fdopen(descriptor,'w') as stream:stream.write(line)
            env['PGPASSFILE']=filename
        yield env
    finally:
        if filename is not None:Path(filename).unlink(missing_ok=True)


def backup(url:str,target:Path):
    target=target.resolve();manifest=target.with_name(target.name+'.manifest.json')
    if target.exists() or manifest.exists():raise FileExistsError('Backup or manifest already exists; choose a new path')
    parsed=make_url(url);metadata={}
    if parsed.get_backend_name()=='sqlite':
        if not parsed.database or parsed.database==':memory:':raise ValueError('A durable SQLite database file is required')
        metadata=sqlite_copy(Path(parsed.database),target);kind='sqlite'
    elif parsed.get_backend_name()=='postgresql':
        kind='postgresql-custom'
        try:
            with reserve(target) as output, pg_credentials(url) as env:
                subprocess.run(['pg_dump','--no-password','--format=custom','--no-owner','--no-privileges'],env=env,stdout=output,stderr=subprocess.PIPE,check=True,timeout=3600)
        except BaseException:target.unlink(missing_ok=True);raise
    else:raise ValueError('Unsupported database')
    data={'manifest_version':SCHEMA,'kind':kind,'sha256':sha256(target),'bytes':target.stat().st_size,'created_at':datetime.now(timezone.utc).isoformat(),'snapshot':metadata,'scope':'Database logical snapshot only; not object storage, identity provider, encryption keys, global PostgreSQL roles or WAL/PITR.'}
    with reserve(manifest) as out:out.write((json.dumps(data,indent=2)+'\n').encode())
    return data

def restore(archive:Path,url:str,trusted_source:bool=False):
    if not trusted_source:raise ValueError('Explicit trusted-source approval is required; a checksum alone does not authenticate a backup')
    manifest=json.loads(archive.with_name(archive.name+'.manifest.json').read_text())
    if manifest.get('manifest_version')!=SCHEMA or sha256(archive)!=manifest.get('sha256'):raise ValueError('Backup manifest version or checksum mismatch')
    parsed=make_url(url)
    if manifest['kind']=='sqlite' and parsed.get_backend_name()=='sqlite':
        if not parsed.database or parsed.database==':memory:':raise ValueError('A new durable target file is required')
        target=Path(parsed.database);original=sqlite_copy(archive,target)
        try:
            with closing(sqlite3.connect(target)) as db:
                # A recovered database must not resurrect old sessions or OIDC flows.
                db.execute('UPDATE sessions SET revoked=1')
                db.execute('UPDATE oidc_flows SET used=1')
                db.commit()
            result=sqlite_check(target)
        except BaseException:target.unlink(missing_ok=True);raise
        if result['counts']!=original['counts']:raise RuntimeError('Restored evidence counts differ')
        return {**result,'sessions_revoked':True,'oidc_flows_consumed':True}
    if manifest['kind']=='postgresql-custom' and parsed.get_backend_name()=='postgresql':
        with pg_credentials(url) as env:
            # Require a separately created empty database, never drop or overwrite a running installation.
            query="SELECT count(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema');"
            response=subprocess.run(['psql','--no-password','--no-psqlrc','--tuples-only','--no-align','--set','ON_ERROR_STOP=1','--command',query],env=env,capture_output=True,text=True,check=True,timeout=60)
            if response.stdout.strip()!='0':raise ValueError('Refusing to restore into a database containing tables')
            subprocess.run(['pg_restore','--no-password','--exit-on-error','--single-transaction','--no-owner','--no-privileges','--dbname',env['PGDATABASE'],str(archive.resolve())],env=env,capture_output=True,check=True,timeout=3600)
            subprocess.run(['psql','--no-password','--no-psqlrc','--set','ON_ERROR_STOP=1','--command','BEGIN; UPDATE sessions SET revoked=true; UPDATE oidc_flows SET used=true; COMMIT;'],env=env,capture_output=True,check=True,timeout=60)
            return {'sessions_revoked':True,'oidc_flows_consumed':True,'warning':'Run migration readiness, count/report reconciliation, audit/outbox and restore acceptance gates before enabling traffic.'}
    raise ValueError('Backup kind and target database do not match')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['backup','restore'])
    parser.add_argument('archive',type=Path)
    parser.add_argument('--database-url',default=secret_from_env('DATABASE_URL'))
    parser.add_argument('--trusted-source',action='store_true')
    args=parser.parse_args()
    if not args.database_url:parser.error('Set DATABASE_URL; avoid putting passwords in command-line history')
    try:result=backup(args.database_url,args.archive) if args.action=='backup' else restore(args.archive,args.database_url,args.trusted_source)
    except subprocess.CalledProcessError as exc:raise SystemExit('Database utility failed. Inspect protected operator logs; no credentials or raw database errors are printed. Exit '+str(exc.returncode))
    print(json.dumps(result,indent=2))
if __name__=='__main__':main()
