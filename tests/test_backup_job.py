"""Scheduled backup job and restore drill without a real PostgreSQL server."""
from datetime import datetime, timedelta, timezone
import json
import subprocess
import pytest
from sqlalchemy import text
from apps.api.schema import supported_revision
from scripts import backup_job, restore_drill

URL='postgresql+psycopg://rota:s3cret-pw@127.0.0.1:5432/rota'

def touch_archive(directory,stamp):
    path=directory/f"rota-{stamp.strftime(backup_job.STAMP)}.dump"
    for item in backup_job.companions(path):item.write_text('x')
    return path

def test_retention_keeps_latest_daily_and_weekly_and_ignores_foreign_files(tmp_path):
    start=datetime(2026,9,26,2,tzinfo=timezone.utc)
    made=[touch_archive(tmp_path,start-timedelta(days=d)) for d in range(60)]
    extra=touch_archive(tmp_path,start-timedelta(hours=1))  # second run on the same day: older one goes
    (tmp_path/'notes.dump').write_text('keep');(tmp_path/'rota-bad.dump').write_text('keep')
    removed=backup_job.prune(tmp_path,7,4)
    left={p.name for _,p in backup_job.archives(tmp_path)}
    assert {p.name for p in made[:7]}<=left and extra.name not in left and len(left)==9  # 7 daily + 2 older weekly
    assert len(removed)==52 and not any((tmp_path/n).exists() or (tmp_path/(n+'.sha256')).exists() for n in removed)
    assert (tmp_path/'notes.dump').exists() and (tmp_path/'rota-bad.dump').exists()
    with pytest.raises(ValueError):backup_job.prune(tmp_path,0,4)

def fake_utilities(listing='1; 2 TABLE public identities rota\n',fail=None):
    def run(argv,**kw):
        if argv[0]==fail:raise subprocess.CalledProcessError(1,argv)
        if argv[0]=='pg_dump':kw['stdout'].write(b'PGDMP fake archive');return subprocess.CompletedProcess(argv,0)
        assert argv[:2]==['pg_restore','--list'] and 'PGPASSFILE' not in kw['env']
        return subprocess.CompletedProcess(argv,0,listing,'')
    return run

def test_job_writes_verified_archive_sha_and_prunes(tmp_path,monkeypatch):
    monkeypatch.setattr(subprocess,'run',fake_utilities())
    old=touch_archive(tmp_path,datetime(2026,1,1,tzinfo=timezone.utc))
    result=backup_job.run(URL,tmp_path,1,0,now=datetime(2026,9,26,2,tzinfo=timezone.utc))
    archive=tmp_path/'rota-20260926T020000Z.dump'
    assert result['archive']==archive.name and result['toc_entries']==1 and result['pruned']==[old.name]
    assert (tmp_path/(archive.name+'.sha256')).read_text()==f"{result['sha256']}  {archive.name}\n"
    assert json.loads((tmp_path/(archive.name+'.manifest.json')).read_text())['sha256']==result['sha256']
    assert 's3cret' not in json.dumps(result)

@pytest.mark.parametrize('suffix',[None,'','.manifest.json','.sha256','overlap'],ids=['complete','archive','manifest','checksum','overlap'])
def test_same_second_collision_preserves_existing_backup(tmp_path,monkeypatch,suffix):
    monkeypatch.setattr(subprocess,'run',fake_utilities())
    stamp=datetime(2026,9,26,2,tzinfo=timezone.utc)
    result=backup_job.run(URL,tmp_path,1,0,now=stamp)
    archive=tmp_path/result['archive']
    if suffix not in (None,'overlap'):
        for item in backup_job.companions(archive):
            if item.name!=archive.name+suffix:item.unlink()
    touch_archive(tmp_path,datetime(2026,1,1,tzinfo=timezone.utc))
    before={p.name:p.read_bytes() for p in tmp_path.iterdir()}
    if suffix=='overlap':
        winner={item:item.read_bytes() for item in backup_job.companions(archive)}
        for item in winner:item.unlink()
        reserve=backup_job.reserve
        def claim_after_winner(path):
            # Finish the winner between the existence check and the exclusive claim.
            for item,data in winner.items():item.write_bytes(data)
            return reserve(path)
        monkeypatch.setattr(backup_job,'reserve',claim_after_winner)
    with pytest.raises(FileExistsError):backup_job.run(URL,tmp_path,1,0,now=stamp)
    assert {p.name:p.read_bytes() for p in tmp_path.iterdir()}==before

@pytest.mark.parametrize('fake',[fake_utilities(fail='pg_restore'),fake_utilities(listing=';\n; header only\n')])
def test_failed_verification_removes_partial_output_and_prunes_nothing(tmp_path,monkeypatch,fake):
    monkeypatch.setattr(subprocess,'run',fake)
    old=touch_archive(tmp_path,datetime(2026,1,1,tzinfo=timezone.utc))
    with pytest.raises((subprocess.CalledProcessError,RuntimeError)):backup_job.run(URL,tmp_path,1,0)
    assert sorted(p.name for p in tmp_path.iterdir())==sorted(p.name for p in backup_job.companions(old))

def test_drill_refuses_source_and_reports_head(tmp_path,monkeypatch):
    with pytest.raises(ValueError):restore_drill.drill(tmp_path/'a.dump',URL,URL.replace('s3cret-pw','other'))
    monkeypatch.setattr(restore_drill,'restore',lambda *a,**k:{'sessions_revoked':True})
    monkeypatch.setattr(restore_drill,'inspect',lambda url:{'head_ok':False,'tables_ok':True,'migration_head':['old'],'expected_head':'new','counts':{}})
    result=restore_drill.drill(tmp_path/'a.dump',URL.replace('/rota','/rota_drill'),URL)
    assert result['ok'] is False and result['archive']=='a.dump' and 's3cret' not in json.dumps(result)






@pytest.mark.parametrize('missing', ['feedback_reports', 'publications', 'audit_events'])
def test_drill_rejects_missing_image_tables_even_with_matching_head(h, tmp_path, monkeypatch, missing):
    with h.app.state.engine.begin() as db:
        db.execute(text('DROP TABLE ' + missing))
    monkeypatch.setattr(restore_drill, 'restore', lambda *a, **k: {'sessions_revoked': True})
    result = restore_drill.drill(tmp_path / 'a.dump', h.settings.database_url)
    assert result['migration_head'] == [result['expected_head']]
    assert result['head_ok'] and not result['tables_ok'] and not result['ok']
    assert result['missing_tables'] == [missing] and missing not in result['counts']


@pytest.mark.parametrize('heads', [[], ['0066_ux_audit_b1'], [supported_revision(), 'unresolved_branch']])
def test_drill_rejects_absent_older_or_multiple_heads(h, tmp_path, monkeypatch, heads):
    with h.app.state.engine.begin() as db:
        db.execute(text('DELETE FROM alembic_version'))
        for head in heads:
            db.execute(text('INSERT INTO alembic_version (version_num) VALUES (:head)'), {'head': head})
    monkeypatch.setattr(restore_drill, 'restore', lambda *a, **k: {'sessions_revoked': True})
    result = restore_drill.drill(tmp_path / 'a.dump', h.settings.database_url)
    assert not result['head_ok'] and result['tables_ok'] and not result['ok']
    assert result['missing_tables'] == []
