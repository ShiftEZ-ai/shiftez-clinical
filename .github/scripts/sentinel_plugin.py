"""Trusted collection recorder. Candidate plugins still execute; this is a tripwire."""
import json
import os
from pathlib import Path


def pytest_collection_finish(session):
    target = os.environ.get("TRUSTED_COLLECTION_OUTPUT")
    if target:
        Path(target).write_text(json.dumps(sorted(item.nodeid for item in session.items)) + "\n", encoding="utf-8")
