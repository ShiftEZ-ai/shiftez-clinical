#!/usr/bin/env python3
"""Scheduled PostgreSQL backup: pg_dump custom archive, verify, sha256, prune.

Logical nightly snapshots only (RPO = schedule interval); not WAL/PITR.
Settings: DATABASE_URL or DATABASE_URL_FILE, ROTA_BACKUP_DIR,
ROTA_BACKUP_KEEP_DAILY (default 7), ROTA_BACKUP_KEEP_WEEKLY (default 4),
ROTA_BACKUP_KEEP_ALL_DAYS (default 7): every archive of the last N days is kept, so a pre-deploy backup
(scripts/deploy.sh) survives even when a later run on the same day would otherwise replace it.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.api.settings import secret_from_env
from scripts.backup import backup, reserve, sha256

NAME=re.compile(r'^rota-(\d{8}T\d{6}Z)\.dump$')
STAMP='%Y%m%dT%H%M%SZ'

def archives(directory:Path):
    """(timestamp, path) newest first; only names this job writes are ever touched."""
    found=[(datetime.strptime(m.group(1),STAMP).replace(tzinfo=timezone.utc),p) for p in directory.iterdir() if (m:=NAME.match(p.name))]
    return sorted(found,reverse=True)

def keep_set(found,keep_daily:int,keep_weekly:int,keep_all_days:int=0):
    """Every archive of the newest K days, plus the newest of each of the latest N days and M ISO weeks."""
    keep,days,weeks=set(),set(),set()
    for stamp,path in found:
        if found[0][0]-stamp<timedelta(days=keep_all_days):keep.add(path)
        day,week=stamp.date(),stamp.isocalendar()[:2]
        if day not in days and len(days)<keep_daily:days.add(day);keep.add(path)
        if week not in weeks and len(weeks)<keep_weekly:weeks.add(week);keep.add(path)
    return keep

def companions(path:Path):
    return [path,path.with_name(path.name+'.manifest.json'),path.with_name(path.name+'.sha256')]

def prune(directory:Path,keep_daily:int,keep_weekly:int,keep_all_days:int=0):
    if keep_daily<1:raise ValueError('Keep at least one daily backup')
    found=archives(directory);keep=keep_set(found,keep_daily,keep_weekly,keep_all_days);removed=[]
    for _,path in found:
        if path not in keep:
            for item in companions(path):item.unlink(missing_ok=True)
            removed.append(path.name)
    return removed

def verify(path:Path):
    # pg_restore --list reads the archive TOC without a database: catches truncated/corrupt dumps.
    listing=subprocess.run(['pg_restore','--list',str(path)],env={k:v for k,v in os.environ.items() if k in ('PATH','SYSTEMROOT','LANG')},capture_output=True,text=True,check=True,timeout=600)
    entries=sum(1 for line in listing.stdout.splitlines() if line and not line.startswith(';'))
    if not entries:raise RuntimeError('Backup archive lists no entries')
    return entries

def run(url:str,directory:Path,keep_daily:int,keep_weekly:int,now=None,keep_all_days:int=0):
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=directory/f"rota-{(now or datetime.now(timezone.utc)).strftime(STAMP)}.dump"
    if any(item.exists() or item.is_symlink() for item in companions(target)):
        raise FileExistsError('Backup or companion already exists; choose a new timestamp')
    # Claim this stamp before cleanup so a competing job cannot remove the winner.
    checksum=reserve(target.with_name(target.name+'.sha256'))
    try:
        with checksum:
            manifest=backup(url,target)
            entries=verify(target)
            digest=sha256(target)
            if digest!=manifest['sha256']:raise RuntimeError('Archive changed after it was written')
            # sha256sum -c format, so off-server copies can be checked with standard tools.
            checksum.write(f'{digest}  {target.name}\n'.encode())
    except BaseException:
        for item in companions(target):item.unlink(missing_ok=True)
        raise
    # Prune only after this run succeeded, so a failing job never eats good backups.
    removed=prune(directory,keep_daily,keep_weekly,keep_all_days)
    return {'ok':True,'archive':target.name,'bytes':manifest['bytes'],'sha256':digest,'toc_entries':entries,'pruned':removed}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,default=os.getenv('ROTA_BACKUP_DIR'))
    parser.add_argument('--keep-daily',type=int,default=int(os.getenv('ROTA_BACKUP_KEEP_DAILY','7')))
    parser.add_argument('--keep-weekly',type=int,default=int(os.getenv('ROTA_BACKUP_KEEP_WEEKLY','4')))
    parser.add_argument('--keep-all-days',type=int,default=int(os.getenv('ROTA_BACKUP_KEEP_ALL_DAYS','7')))
    args=parser.parse_args()
    url=secret_from_env('DATABASE_URL')
    if not url or not args.directory:parser.error('Set DATABASE_URL(_FILE) and ROTA_BACKUP_DIR (or --directory)')
    try:result=run(url,args.directory,args.keep_daily,args.keep_weekly,keep_all_days=args.keep_all_days)
    except subprocess.CalledProcessError as exc:
        print(json.dumps({'ok':False,'error':'database utility failed','command':exc.cmd[0],'exit':exc.returncode}));raise SystemExit(1)
    except Exception as exc:
        print(json.dumps({'ok':False,'error':type(exc).__name__}));raise SystemExit(1)
    print(json.dumps(result))
if __name__=='__main__':main()
