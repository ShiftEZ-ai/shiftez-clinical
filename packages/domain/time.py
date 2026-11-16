"""All intervals are start-inclusive, end-exclusive. Dates end at local midnight."""
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

UTC = timezone.utc
_offset = timedelta(0)


def now() -> datetime:
    """The current instant (UTC): the one clock the app reads (tests/test_clock_guard.py keeps it that way). Tests pin
    it (tests/conftest.py) so fixtures with fixed dates keep their place relative to "today" however many days pass."""
    return datetime.now(UTC) + _offset


def pin_clock(instant: datetime | str) -> None:
    """Tests only: from this moment now() reads `instant`, and time still flows."""
    global _offset
    _offset = aware(instant) - datetime.now(UTC)


def aware(value: datetime | str) -> datetime:
    if isinstance(value, str):
        return _parsed(value)
    if value.tzinfo is UTC:
        return value  # already normalised (datetimes are immutable)
    # SQLite strips stored timezone metadata; all persisted instants are UTC.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@lru_cache(maxsize=65536)
def _parsed(value: str) -> datetime:
    # Snapshots repeat the same few thousand instants; parse each once.
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def overlaps(a: datetime, b: datetime, c: datetime, d: datetime) -> bool:
    return aware(a) < aware(d) and aware(c) < aware(b)


@lru_cache(maxsize=4096)
def local_midnight(day: date, zone: str) -> datetime:
    return datetime.combine(day, time(), ZoneInfo(zone)).astimezone(UTC)


@lru_cache(maxsize=4096)
def local_stamp(day: date, clock: str, zone: str) -> datetime:
    """Normalize 24:00. Reject ambiguous/nonexistent local times rather than guess."""
    if clock == "24:00":
        day, clock = day + timedelta(days=1), "00:00"
    t = time.fromisoformat(clock)
    naive = datetime.combine(day, t)
    z = ZoneInfo(zone)
    first, second = naive.replace(tzinfo=z, fold=0), naive.replace(tzinfo=z, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ValueError("Ambiguous or nonexistent local wall time; use an explicit dated instant")
    if first.astimezone(UTC).astimezone(z).replace(tzinfo=None) != naive:
        raise ValueError("Nonexistent local wall time")
    return first.astimezone(UTC)


def effective(record, start: datetime, end: datetime, zone: str) -> bool:
    return (getattr(record, "active", True)
            and local_midnight(record.starts_on, zone) <= aware(start)
            and (record.ends_on is None or aware(end) <= local_midnight(record.ends_on, zone)))


def duration_hours(start: datetime, end: datetime) -> float:
    return (aware(end) - aware(start)).total_seconds() / 3600


def union_hours(intervals: list[tuple[datetime, datetime]]) -> float:
    merged: list[list[datetime]] = []
    for start, end in sorted((aware(a), aware(b)) for a, b in intervals if aware(a) < aware(b)):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return sum(duration_hours(a, b) for a, b in merged)


def configured_duration_matches(definition: dict, start: datetime, end: datetime, zone: str) -> bool:
    """Configured clocks define a complete duty; elapsed hours still govern rest/workload."""
    timing = definition.get('default_timing')
    if not timing:
        return True
    a, b = (int(t[:2]) * 60 + int(t[3:]) for t in (timing['start_time'], timing['end_time']))
    minutes = (b - a) % 1440 or 1440
    local = ZoneInfo(zone)
    span = aware(end).astimezone(local).replace(tzinfo=None) - aware(start).astimezone(local).replace(tzinfo=None)
    return span.total_seconds() == minutes * 60
