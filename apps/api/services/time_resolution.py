"""Resolve a save's local wall times in the department timezone together."""
from datetime import date

from apps.api.errors import require
from packages.domain.time import local_stamp


def resolve(local, zone):
    require(len(local) == 16 and local[10] == 'T', 'LOCAL_TIME_INVALID',
            'Use a complete local date and minute')
    return local_stamp(date.fromisoformat(local[:10]), local[11:], zone).isoformat()
