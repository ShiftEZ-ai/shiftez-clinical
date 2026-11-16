#!/usr/bin/env python3
"""Restore drill: restore the newest (or a named) archive into an EMPTY scratch
database, check the migration head and sanity counts, print JSON pass/fail.

Scratch target: ROTA_DRILL_DATABASE_URL or ROTA_DRILL_DATABASE_URL_FILE. Archives:
ROTA_BACKUP_DIR. The restore refuses a populated target, so it cannot overwrite
the running installation; recreate the scratch database before each drill.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.api.settings import secret_from_env
from scripts.backup import restore
from scripts.backup_job import archives
from sqlalchemy.engine import make_url

COUNTED=('identities','workspaces','publications','departments','categories','programmes','operating_calendars',
         'locations','services','assignment_types','policies','audit_events','feedback_reports')

def target_key(url):
    parsed=make_url(url);return (parsed.host,parsed.port or 5432,parsed.database)

def inspect(url):
    from sqlalchemy import create_engine, inspect as inspect_schema, text
    from apps.api.models import Base
    from apps.api.schema import supported_revision
    # Ops scripts can be newer than their base app image; its models define required tables.
    required=set(Base.metadata.tables)
    engine=create_engine(url)
    try:
        with engine.connect() as db:
            heads=list(db.scalars(text('SELECT version_num FROM alembic_version')))
            tables=set(inspect_schema(db).get_table_names())
            counts={t:db.scalar(text('SELECT count(*) FROM '+t)) for t in COUNTED if t in tables}
    finally:engine.dispose()
    expected=supported_revision()
    missing=sorted(required-tables)
    return {'migration_head':heads,'expected_head':expected,'head_ok':heads==[expected],
            'tables_ok':not missing,'missing_tables':missing,'counts':counts}

def drill(archive:Path,url:str,source_url:str=''):
    if source_url and target_key(source_url)==target_key(url):raise ValueError('Drill target is the source database')
    started=time.monotonic()
    restored=restore(archive,url,trusted_source=True)
    checks=inspect(url)
    return {'ok':checks['head_ok'] and checks['tables_ok'],'archive':archive.name,'restore_seconds':round(time.monotonic()-started,1),
            'sessions_revoked':restored['sessions_revoked'],**checks}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive',type=Path,nargs='?',help='Default: newest rota-*.dump in ROTA_BACKUP_DIR')
    parser.add_argument('--directory',type=Path,default=os.getenv('ROTA_BACKUP_DIR'))
    args=parser.parse_args()
    url=secret_from_env('ROTA_DRILL_DATABASE_URL')
    if not url:parser.error('Set ROTA_DRILL_DATABASE_URL(_FILE) to an empty scratch database')
    archive=args.archive
    if archive is None:
        found=archives(args.directory) if args.directory else []
        if not found:parser.error('No archive named and none found in ROTA_BACKUP_DIR')
        archive=found[0][1]
    try:result=drill(archive,url,secret_from_env('DATABASE_URL'))
    except subprocess.CalledProcessError as exc:
        result={'ok':False,'archive':archive.name,'error':'database utility failed','command':exc.cmd[0],'exit':exc.returncode}
    except Exception as exc:
        result={'ok':False,'archive':archive.name,'error':type(exc).__name__}
    print(json.dumps(result))
    raise SystemExit(0 if result['ok'] else 1)
if __name__=='__main__':main()
