"""Event contracts retain safe links and local recurrence boundaries."""
from datetime import date, datetime, timedelta

import pytest
from pydantic import ValidationError

from packages.contracts import schemas as s


EVENT_TYPE_ID = "10000000-0000-0000-0000-000000000001"
EVENT = {"event_type_id": EVENT_TYPE_ID, "heading": "Synthetic meeting",
         "start": "2026-10-16T09:00:00+03:00", "end": "2026-10-16T10:00:00+03:00",
         "format": "online"}


@pytest.mark.parametrize("schema,fields", [
    (s.EventInput, EVENT),
    (s.DepartmentContentInput, {"name": "Synthetic link", "kind": "link", "starts_on": "2026-10-16"}),
])
@pytest.mark.parametrize("url", [
    "https://example.test/join\nSTATUS:CANCELLED",
    "https://example.test/join\r\nSTATUS:CANCELLED",
    "https://example.test/jo\tin",
    "https://example.test/jo\x00in",
    "https://example.test/jo\x7fin",
    " https://example.test/join",
    "https://example.test/join ",
    "https://example.test/join room",
    ('http' + 's://' + 'user' + ':pas' + 'swor' + 'd@ex' + 'ampl' + 'e.te' + 'st/j' + 'oin'),
    "https://example.test:wrong/join",
    "https://example.test:65536/join",
])
def test_event_and_department_links_reject_unsafe_raw_urls(schema, fields, url):
    with pytest.raises(ValidationError):
        schema(**fields, url=url)


@pytest.mark.parametrize("schema,fields", [
    (s.EventInput, EVENT),
    (s.DepartmentContentInput, {"name": "Synthetic link", "kind": "link", "starts_on": "2026-10-16"}),
])
def test_event_and_department_links_keep_encoded_whitespace(schema, fields):
    url = "https://example.test:8443/join%20room?name=synthetic%20meeting"
    assert schema(**fields, url=url).url == url


@pytest.mark.parametrize("zone,start", [
    ("Pacific/Kiritimati", "2026-10-16T00:30:00+14:00"),
    ("Etc/GMT+12", "2026-10-16T23:30:00-12:00"),
])
@pytest.mark.parametrize("days,valid", [(0, False), (1, True), (366, True), (367, True), (10000, True)])
def test_recurrence_end_is_exclusive_without_a_series_length_cap(zone, start, days, valid):
    local_start = datetime.fromisoformat(start)
    value = EVENT | {"start": start, "end": (local_start + timedelta(hours=1)).isoformat(),
                     "url": "https://example.test/join", "repeat_until": local_start.date() + timedelta(days=days),
                     "repeat": {"timezone": zone, "anchor": local_start.date(),
                                "days": [{"weekday": local_start.weekday(),
                                          "start_time": local_start.strftime("%H:%M"),
                                          "end_time": (local_start + timedelta(hours=1)).strftime("%H:%M")}]}}
    if valid:
        assert s.EventInput(**value).repeat_until == local_start.date() + timedelta(days=days)
    else:
        with pytest.raises(ValidationError, match="exclusive end date"):
            s.EventInput(**value)


def test_physical_event_needs_a_nonblank_place_and_type_default_tags_are_unique():
    with pytest.raises(ValidationError, match="location or place"):
        s.EventInput(**(EVENT | {"format": "in_person", "place": " \t "}))
    with pytest.raises(ValidationError, match="only once"):
        s.EventTypeInput(name="Synthetic type", starts_on=date(2026, 10, 16),
                         default_tag_ids=[EVENT_TYPE_ID, EVENT_TYPE_ID])
