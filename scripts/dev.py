#!/usr/bin/env python3
"""Foreground development API + worker supervisor. Never seeds or migrates data."""
import argparse
import getpass
import os
from pathlib import Path
import signal
import secrets
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from apps.api.settings import Settings, secret_from_env
from apps.api.data_crypto import data_key_from_secret

def configure_development_key():
    if os.environ.get("ROTA_ENV") != "development":
        return
    if not any(name in os.environ for name in ("FMUC_DATA_KEY", "FMUC_DATA_KEY_FILE")):
        path = ROOT / ".run" / "development" / "fmuc_data_key"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            try:
                with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                    if os.name == "nt":
                        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r",
                                        f"{getpass.getuser()}:F"], check=True, capture_output=True)
                    stream.write(secrets.token_hex(32))
            except BaseException:
                path.unlink()
                raise
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 128:
            raise RuntimeError("Invalid development FMUC_DATA_KEY file; restore it before starting")
        value = path.read_text(encoding="ascii").removesuffix("\n").removesuffix("\r")
        if not data_key_from_secret(value):
            raise RuntimeError("Empty development FMUC_DATA_KEY file; restore it before starting")
        # Children inherit the value; the local file also works on Windows without Linux secret-file flags.
        os.environ["FMUC_DATA_KEY"] = value
    data_key_from_secret(secret_from_env("FMUC_DATA_KEY", optional=True))  # Validates; raises on malformed keys.


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8000)
    args=parser.parse_args()
    if not 1024<=args.port<=65535:parser.error('Use an unprivileged port between 1024 and 65535')
    os.environ.setdefault('ROTA_ENV','development')  # this supervisor is development-only; an unset ROTA_ENV elsewhere means production
    configure_development_key()
    settings=Settings.from_env()
    if settings.environment=='production':parser.error('Use the reviewed deployment process, not the development supervisor, for production')
    if not (ROOT/'apps/web/dist/index.html').exists():parser.error('Run python scripts/build_web.py first')
    env={**os.environ,'ROTA_ENV':settings.environment,'DATABASE_URL':settings.database_url,'PUBLIC_ORIGIN':os.environ.get('PUBLIC_ORIGIN') or f'http://127.0.0.1:{args.port}'}
    # SQLite live notifications are process-local; PostgreSQL fans out through LISTEN/NOTIFY.
    env.setdefault('WEB_CONCURRENCY', '2' if settings.database_url.startswith('postgresql') else '1')
    processes=[]
    def stop(*_):
        for child in processes:
            if child.poll() is None:child.terminate()
    signal.signal(signal.SIGTERM,stop)
    try:
        for command in [[sys.executable,'-m','uvicorn','apps.api.main:app','--host','127.0.0.1','--port',str(args.port),'--no-access-log'],[sys.executable,'-m','apps.worker.main']]:
            processes.append(subprocess.Popen(command,cwd=ROOT,env=env))
        print(f'Development only: http://127.0.0.1:{args.port} — API and worker run in this foreground process.',flush=True)
        while all(p.poll() is None for p in processes):time.sleep(.25)
        raise SystemExit(next((p.returncode for p in processes if p.poll() is not None),1))
    except KeyboardInterrupt:pass
    finally:
        stop()
        for child in processes:
            try:child.wait(timeout=10)
            except subprocess.TimeoutExpired:child.kill();child.wait()
if __name__=='__main__':main()
