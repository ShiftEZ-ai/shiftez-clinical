"""Shared JSON date encoding for snapshots, replies and semantic input digests."""
from datetime import date, datetime

from .time import aware


def json_default(value):
    """JSON's fallback for typed dates; unknown values remain errors, never guessed strings."""
    if isinstance(value, datetime):
        return aware(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f'Object of type {type(value).__name__} is not JSON serializable')


PLAIN = frozenset((str, int, float, bool, type(None)))


def json_value(value, *, copy_containers=True):
    """Normalize dates without mutation; replies may reuse ready branches, snapshots copy by default."""
    # Plain values (most of a rota's records) are kept without a call each: a month's sessions hold ~0.5M of them.
    if type(value) in PLAIN:
        return value
    if isinstance(value, date):
        return json_default(value)
    if not copy_containers and isinstance(value, (dict, list)):
        result = value
        items = value.items() if isinstance(value, dict) else enumerate(value)
        for key, child in items:
            converted = child if type(child) in PLAIN else json_value(child, copy_containers=False)
            if converted is not child:
                if result is value:
                    result = value.copy()
                result[key] = converted
        return result
    if isinstance(value, dict):
        return {k: v if type(v) in PLAIN else json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [v if type(v) in PLAIN else json_value(v, copy_containers=copy_containers) for v in value]
    return value
