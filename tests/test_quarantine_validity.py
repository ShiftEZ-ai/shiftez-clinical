"""One visible gate for malformed or expired reviewed quarantines."""
from datetime import datetime, timezone
from pathlib import Path

from tests.quarantine import entries


def test_reviewed_quarantine_is_valid_and_current():
    entries(Path(__file__).with_name('quarantine.json'), datetime.now(timezone.utc).date())  # real clock: quarantine expiry is wall-clock policy
