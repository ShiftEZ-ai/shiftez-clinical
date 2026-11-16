"""Public timestamps retain a present, timezone-aware instant across typed and record replies."""
from datetime import datetime
import re

import pytest


def assert_same_instant(actual, expected):
    instants = []
    for value in (actual, expected):
        assert isinstance(value, str) and re.fullmatch(
            r'\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:[Zz]|[+-]\d{2}:\d{2})', value), value
        instant = datetime.fromisoformat(value.upper())
        assert instant.utcoffset() is not None, value
        instants.append(instant)
    assert instants[0] == instants[1], (actual, expected)


@pytest.mark.parametrize('other', [
    '2026-10-03T06:20:16.787001+00:00',
    '2026-10-03T09:20:16.787001+03:00',
    '2026-10-03T06:20:16.787001Z',
])
def test_timestamp_comparison_keeps_the_exact_instant_across_offsets(other):
    assert_same_instant('2026-10-03T06:20:16.787001Z', other)


@pytest.mark.parametrize('invalid', [None, '', 0, '2026-10-03',
    '2026-10-03T06:20:16.787001', '2026-10-03T06:20:16.787001+0000',
    '2026-10-03T06:20:16.787002Z', '2026-10-03T06:20:16.787001+03:00',
    '2026-10-03T06:20:16.7870011Z'])
@pytest.mark.parametrize('side', [0, 1])
def test_timestamp_comparison_refuses_missing_naive_or_different_instants(invalid, side):
    values = ['2026-10-03T06:20:16.787001Z'] * 2
    values[side] = invalid
    with pytest.raises(AssertionError):
        assert_same_instant(*values)
