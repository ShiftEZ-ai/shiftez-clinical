"""Reuse only immutable root scenarios; all test databases and browser contexts stay private."""
import copy
from functools import wraps


# These objects belong to the fresh Harness, not to a seeded scenario.  Everything else is
# seed-owned state and must survive a cache hit exactly as it did after the cache miss.
RUNTIME_ATTRIBUTES = frozenset({'settings', 'app', 'clients', 'cached_scenario', 'browser_auth'})


def capture_state(h):
    return {key: copy.deepcopy(value) for key, value in h.__dict__.items() if key not in RUNTIME_ATTRIBUTES}


def restore_state(h, state):
    for key in tuple(h.__dict__):
        if key not in RUNTIME_ATTRIBUTES:
            delattr(h, key)
    for key, value in state.items():
        setattr(h, key, copy.deepcopy(value))


def seeded(build):
    @wraps(build)
    def prepare(h, *args, **kw):
        cache = getattr(h, 'cached_scenario', None)
        return cache(build, *args, **kw) if cache else build(h, *args, **kw)
    return prepare


def authenticated(context, origin, cookies):
    if not cookies:
        return False
    context.add_cookies([{'name': name, 'value': value, 'url': origin} for name, value in cookies.items()])
    if context.request.get(origin + '/api/me').ok:
        return True
    context.clear_cookies()
    return False
