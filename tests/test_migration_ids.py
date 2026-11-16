"""Alembic stores revision ids in VARCHAR(32) on PostgreSQL; SQLite would not notice a longer one."""
import re
from pathlib import Path


def test_revision_ids_fit_postgres_version_column():
    for path in Path('migrations/versions').glob('*.py'):
        rev = re.search(r'^revision = ["\']([^"\']+)', path.read_text(encoding='utf-8'), re.M).group(1)
        assert len(rev) <= 32, f'{path.name}: revision id {rev!r} is {len(rev)} characters'
