"""Pipeline regression: code-owner review is retired, so no CODEOWNERS file may come back unnoticed."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_no_code_owners_file_is_configured():
    # Owner decision 22 Nov, item 6: protected pipeline files land with one full-diff review plus tests.
    for place in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
        assert not (ROOT / place).exists(), place
