"""The product names no one kind of institution (AGENTS.md, product rules): the web client and the contracts it is
built from say "institutional sign-in", "work phone" and "organisation", never a word for one kind of institution."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORD = re.compile(r'\bhospitals?\b', re.IGNORECASE)


def test_client_and_contracts_name_no_kind_of_institution():
    files = [*(ROOT / 'apps' / 'web' / 'src').glob('*.ts'), *(ROOT / 'packages' / 'contracts').glob('*.py'),
             ROOT / 'packages' / 'contracts' / 'public-schemas.json', ROOT / 'apps' / 'api' / 'oidc.py']
    found = [f'{p.relative_to(ROOT).as_posix()} line {n}' for p in files
             for n, line in enumerate(p.read_text(encoding='utf-8').splitlines(), 1) if WORD.search(line)]
    assert len(files) > 3 and not found, found
