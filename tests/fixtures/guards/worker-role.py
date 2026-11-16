"""Synthetic worker-role provisioning fixture."""
from pathlib import Path
import re

def role_sql(directory: Path) -> str:
    url = (directory / "worker_database_url.txt").read_text(encoding="ascii").strip()
    match = re.fullmatch(r"postgresql\+psycopg://rota_worker:([0-9a-f]{64})@localhost:5432/synthetic", url)
    if match is None:
        raise SystemExit("Invalid worker database URL; expected the protected generated worker credential")
    # The password reaches psql only over stdin; it never enters process arguments or container logs.
    return """BEGIN;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rota_worker') THEN
        CREATE ROLE rota_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
    END IF;
END $$;
ALTER ROLE rota_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '""" + match[1] + "';\nCOMMIT;\n"

