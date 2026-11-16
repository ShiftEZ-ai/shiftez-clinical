"""Transaction-bound invalidations through one PostgreSQL LISTEN connection per API process.

The bounded history is a hint, not shared authority: a gap always requests a full refresh.
Generation topics retain their coordinator/workspace subscription boundary.
"""
import asyncio
from collections import OrderedDict, deque
import json
import logging
import threading

from pydantic import ValidationError
from sqlalchemy import text
from packages.contracts.schemas import DraftEditDelta, LiveChange
from .settings import Settings

CHANNEL = "rota_changes"
log = logging.getLogger("rota.live")
_loop: asyncio.AbstractEventLoop | None = None
_events: dict[str, asyncio.Event] = {}
_waiters: dict[asyncio.Event, int] = {}
_versions: dict[str, int] = {}
_changes: OrderedDict[str, deque] = OrderedDict()
_sequence = 0
_evicted_version = 0
_history_size = Settings.live_history_size
_history_topics = Settings.live_history_topics
_stop = threading.Event()
_draining: asyncio.Event | None = None


def attach(loop):
    global _loop, _stop, _draining
    _stop.set()
    _clear_history()
    # A retired listener must never be revived by a later app clearing its stop event.
    _stop = threading.Event()
    _loop = loop
    _draining = asyncio.Event() if loop is not None else None


def _clear_history():
    global _sequence, _evicted_version
    for event in _events.values():
        event.set()
    _events.clear()
    _waiters.clear()
    _versions.clear()
    _changes.clear()
    _sequence = _evicted_version = 0


def _schedule(loop, lifetime, callback, *args):
    if loop is None or loop.is_closed() or lifetime.is_set():
        return

    def deliver():
        if _loop is loop and _stop is lifetime and not lifetime.is_set():
            callback(*args)

    try:
        loop.call_soon_threadsafe(deliver)
    except RuntimeError:  # Shutdown can close the loop between checking and enqueueing.
        pass


def configure(settings):
    global _history_size, _history_topics
    _history_size, _history_topics = settings.live_history_size, settings.live_history_topics


def sequence():
    return _sequence


def version(department_id):
    if isinstance(department_id, tuple):
        return tuple(version(topic) for topic in department_id)
    return _versions.get(department_id, _evicted_version)


def generation_topic(department_id, workspace_id=None):
    return f'generation:{department_id}' + (f':{workspace_id}' if workspace_id else '')


# Routing describes reader dependencies, not configurable clinical policy. Record IDs stay private.
# The normal worker claims job outboxes when this scope changes; push-only workers listen to every scope.
WORKER_JOB_WAKE_SCOPE = 'jobs'
COMMAND_SCOPES = {
    'workspace': ('workspace',), 'shift': ('workspace',), 'issues': ('workspace',),
    'draft': ('workspace',), 'generation': ('workspace', 'jobs'), 'series': ('workspace', 'catalogue'),
    'publication': ('publication', 'workspace', 'requests', 'swaps', 'availability'),
    'policy': ('rules',), 'rule': ('rules',), 'exception': ('rules',),
    'catalogue': ('catalogue',), 'membership': ('people', 'catalogue'),
    'programme_membership': ('people', 'catalogue'),
    'eligibility': ('people', 'catalogue'), 'grant': ('people', 'catalogue'),
    'tag': ('catalogue', 'people'), 'person': ('people', 'rules', 'access'),
    'calendar': ('rules',), 'availability': ('availability', 'requests'),
    'availability_pattern': ('availability', 'requests'), 'wish': ('availability', 'requests'),
    'rotation': ('catalogue', 'availability', 'requests'), 'timeline': ('catalogue', 'availability', 'requests'),
    'trainee_note': ('people',), 'request': ('requests', 'availability'),
    'swap': ('swaps', 'requests', 'workspace', 'publication'),
    'notification': ('inbox',), 'outbox': ('inbox',), 'push': ('inbox', 'settings'),
    'pwa': ('inbox', 'settings'), 'event': ('catalogue', 'availability', 'inbox'),
    'event_types': ('catalogue',), 'department_content': ('inbox', 'catalogue'),
    'job': ('jobs',), 'score_import': ('jobs', 'workspace'), 'score_check': ('jobs', 'workspace'),
    'ai_provider': ('settings', 'jobs'), 'staff_import': ('people', 'catalogue', 'access'),
    'import': ('workspace', 'catalogue'), 'date_set': ('catalogue',), 'closure': ('catalogue',),
    'planning': ('settings', 'workspace'), 'planning_group': ('settings', 'workspace'),
    'score': ('settings', 'workspace'),  # score.settings(.rollback): scoring weights → configuration and every rota score
    'table_view': ('settings',), 'planner_view': ('settings',), 'department': ('settings', 'access'),
    'branding': ('settings',), 'email': ('settings', 'inbox'), 'integration': ('settings',),
    'calendar_feed': ('settings',),
    'identity': ('access',), 'role': ('access',), 'account': ('access',), 'session': ('access',),
    'self_approval': ('access',), 'review_route': ('access', 'workspace'),
    'review_delegation': ('access', 'workspace'), 'absence_report': ('settings', 'requests'),
    'feedback': ('feedback',),
}


def _workspace_edit_fields(result):
    """Return the bounded committed edit reply, or no patch for full-read fallback."""
    if not isinstance(result, dict) or not isinstance(result.get('delta'), dict):
        return {}
    workspace = result.get('workspace')
    try:
        delta = DraftEditDelta.model_validate(result['delta'])
    except (TypeError, ValueError):
        return {}
    if not isinstance(workspace, dict) or not workspace.get('id'):
        return {}
    if not isinstance(workspace.get('revision'), int) or not isinstance(workspace.get('content_revision'), int):
        return {}
    if (delta.stale or delta.findings is None or delta.coverage is None
            or delta.base_revision >= delta.revision or delta.revision != workspace['revision']):
        return {}
    if any(not isinstance(result.get(key), dict) for key in ('validation', 'health', 'fix_list_delta')):
        return {}
    return {key: value for key in ('delta', 'workspace', 'validation', 'health', 'fix_list_delta')
            if (value := (delta.model_dump(mode='json') if key == 'delta' else result.get(key))) is not None}


def _attach_workspace_edit(changes, result):
    fields = _workspace_edit_fields(result)
    if not fields:
        return changes
    workspace_id = (result.get('workspace') or {}).get('id')
    return [{**change, **fields} if change.get('scope') == 'workspace' and change.get('id') == workspace_id else change
            for change in changes]


def command_changes(kind, payload=None, result=None, *, target_id=None):
    """Resolve all reader scopes once at the command bus, including mixed bulk replies."""
    payload, result = payload or {}, result or {}
    if kind == 'bulk.apply':
        kinds = result.get('item_kinds') or [payload.get('item_kind')]
        changes = []
        for item_kind in kinds:
            found = command_changes(item_kind or '', {})
            if found is None:
                return None
            changes.extend(found)
        return _attach_workspace_edit(_deduplicate(changes), result) if changes else None
    if kind in {'bulk.stage', 'bulk.staged.drop'}:
        scopes = ('settings',)
    else:
        scopes = COMMAND_SCOPES.get(kind.partition('.')[0])
    if scopes is None:
        return None
    if isinstance(result.get('job'), dict):
        scopes = (*scopes, 'jobs')
    if kind.startswith(('notification.policy.', 'notification.preferences.')):
        scopes = (*scopes, 'settings')
    if kind in {'workspace.publish', 'workspace.approve', 'workspace.reopen', 'workspace.undo', 'workspace.redo'}:
        scopes = (*scopes, 'publication', 'swaps', 'requests', 'availability')
    if kind.startswith('catalogue.'):
        catalogue = payload.get('catalogue')
        if catalogue == 'people':
            scopes = (*scopes, 'people')
        elif catalogue in {'events', 'department_content'}:
            scopes = (*scopes, 'inbox', 'availability')
        elif catalogue in {'rotations', 'programme_calendar_entries'}:
            scopes = (*scopes, 'availability', 'requests')
    # Use the owning record from the reply: a shift/assignment ID is not a workspace ID.
    workspace = (result.get('workspace') or {}).get('id') or payload.get('workspace_id')
    if not workspace and kind.startswith(('workspace.', 'draft.')):
        workspace = target_id
    identifiers = {
        'catalogue': payload.get('catalogue'),
        'workspace': workspace,
        'jobs': (result.get('job') or {}).get('id') or payload.get('job_id')
            or (target_id if kind.startswith('job.') else None),
        'publication': result.get('publication_id') or payload.get('publication_id')
            or (target_id if kind.startswith('publication.') else None),
        'people': payload.get('person_id') or (target_id if kind.startswith('person.') else None),
        'availability': payload.get('person_id') or (target_id if kind.startswith('availability.') else None),
        'requests': (result.get('request') or {}).get('id') or payload.get('request_id')
            or (target_id if kind.startswith('request.') else None),
        'swaps': (result.get('swap') or {}).get('id') or payload.get('swap_id')
            or (target_id if kind.startswith('swap.') else None),
        'access': target_id if kind.startswith(('identity.', 'role.', 'account.', 'session.', 'grant.', 'self_approval.')) else None,
        'inbox': (result.get('notification') or {}).get('id')
            or (target_id if kind.startswith(('notification.', 'outbox.')) else None),
        'feedback': target_id if kind.startswith('feedback.') else None,
    }
    return _attach_workspace_edit(
        [{'scope': scope, 'id': identifiers.get(scope)} for scope in dict.fromkeys(scopes)], result)


def job_topic(department_id, job_id):
    return f'job:{department_id}:{job_id}'


def subscription_topics(state, workspace_id=None, job_ids=()):
    department = state['department_id']
    # Coordinators receive the private generation stream even before a rota is
    # selected; a selected rota adds its own topic so another rota cannot wake
    # this view. Staff stay on the department stream only.
    return (department, *([generation_topic(department, workspace_id)]
        if 'coordinator' in state['roles'] and (workspace_id or not job_ids) else []),
        *(job_topic(department, job_id) for job_id in job_ids))


def _bump(topic, change=None):
    global _sequence, _evicted_version
    previous = _versions.get(topic, _sequence)
    _sequence += 1
    _versions[topic] = _sequence
    ring = _changes.setdefault(topic, deque(maxlen=_history_size))
    ring.append((previous, _sequence, change))
    _changes.move_to_end(topic)
    while len(_changes) > _history_topics:
        expired, _ = _changes.popitem(last=False)
        # Even a subscriber that started at zero must notice a first event evicted before its next read.
        _evicted_version = max(_evicted_version, _versions.pop(expired, 0))
    event = _events.pop(topic, None)
    if event:
        event.set()


def _change(scope, id, job_id=None, *, delta=None, workspace=None, validation=None, health=None, fix_list_delta=None):
    if scope is None:
        return None
    if delta is not None and scope == 'workspace':
        fields = _workspace_edit_fields({'delta': delta, 'workspace': workspace, 'validation': validation,
                                         'health': health, 'fix_list_delta': fix_list_delta})
        if fields:
            delta, workspace, validation, health, fix_list_delta = (
                fields.get('delta'), fields.get('workspace'), fields.get('validation'),
                fields.get('health'), fields.get('fix_list_delta'))
        else:
            delta = workspace = validation = health = fix_list_delta = None
    elif scope != 'workspace':
        delta = workspace = validation = health = fix_list_delta = None
    try:
        value = LiveChange(scope=scope, id=id, job_id=job_id, delta=delta, workspace=workspace,
                           validation=validation, health=health, fix_list_delta=fix_list_delta).model_dump(mode='json')
        for key in ('job_id', 'delta', 'workspace', 'validation', 'health', 'fix_list_delta'):
            if value.get(key) is None:
                value.pop(key, None)
        return value
    except ValidationError:
        return None


def changed(department_id, *, scope=None, id=None, job_id=None, delta=None, workspace=None, validation=None,
            health=None, fix_list_delta=None):
    """Wake this process after commit; unknown scope requests the existing full refresh."""
    if not department_id:
        return
    loop, lifetime = _loop, _stop
    change = _change(scope, id, job_id, delta=delta, workspace=workspace, validation=validation,
                     health=health, fix_list_delta=fix_list_delta)
    _schedule(loop, lifetime, _bump, department_id, change)
    # A generation workspace topic also wakes the coordinator's generation view,
    # while retaining the workspace-specific history and never touching staff's department topic.
    if department_id.startswith('generation:') and department_id.count(':') == 2:
        _schedule(loop, lifetime, _bump, department_id.rsplit(':', 1)[0], change)


def receive(payload):
    """Accept scoped JSON and legacy department-only NOTIFYs during rolling upgrades."""
    try:
        value = json.loads(payload)
    except (TypeError, ValueError):
        changed(payload)
        return
    if isinstance(value, dict) and isinstance(value.get('department_id'), str):
        changed(value['department_id'], scope=value.get('scope'), id=value.get('id'), job_id=value.get('job_id'),
                delta=value.get('delta'), workspace=value.get('workspace'), validation=value.get('validation'),
                health=value.get('health'), fix_list_delta=value.get('fix_list_delta'))
    elif isinstance(value, str):
        changed(value)
    else:
        _schedule(_loop, _stop, invalidate)


def invalidate():
    """A listener reconnect may have missed commits, even if a later notification is scoped."""
    for topic in set(_changes) | set(_events):
        _bump(topic)


def detach(loop):
    global _loop, _draining, _history_size, _history_topics
    if _loop is loop:
        _stop.set()
        _loop = None
        _clear_history()
        _draining = None
        _history_size, _history_topics = Settings.live_history_size, Settings.live_history_topics


def begin_draining():
    """Wake API streams for process shutdown; the listener lifetime remains separate."""
    if _draining is not None:
        _draining.set()


def is_draining():
    return _draining is not None and _draining.is_set()


def activity(db, department_id):
    """The existing rota/request revision aggregate, also returned in command replies."""
    from sqlalchemy import func, select
    from . import models as m
    tables = (m.Workspace, m.SwapRequest, m.RequestRecord, m.Availability, m.AvailabilityPattern, m.StaffWish)
    parts = [select((func.coalesce(func.sum(table.revision), 0) + func.count()).label("value")).where(
        table.department_id == department_id) for table in tables]
    # Audience completion changes Requests without changing immutable staff consent,
    # and one UNION keeps the pulse bounded to a single database round trip.
    parts.append(select((func.coalesce(func.sum(m.Job.revision), 0) + func.count()).label("value")).where(
        m.Job.department_id == department_id, m.Job.kind == "swap_audience"))
    parts_union = parts[0].union_all(*parts[1:]).subquery("parts_union")
    return int(db.scalar(select(func.coalesce(func.sum(parts_union.c.value), 0)).select_from(parts_union)) or 0)


def signal(db, department_id, *, scope=None, id=None, job_id=None, delta=None, workspace=None, validation=None,
           health=None, fix_list_delta=None):
    """Queue a compact committed notification on the reader topic for its place."""
    if not department_id:
        return
    change = _change(scope, id, job_id, delta=delta, workspace=workspace, validation=validation,
                     health=health, fix_list_delta=fix_list_delta)
    # Writers must name a contract scope. Legacy/unrecognised writes are
    # recovered by the periodic pulse; they must never publish an unknown
    # change that widens every reader's refresh.
    if change is None:
        return
    topic = job_topic(department_id, id) if scope == 'jobs' and id and not str(department_id).startswith('job:') else department_id
    if db.bind.dialect.name != 'postgresql':
        db.info.setdefault('_live_changes', []).append((topic, change))
        return
    payload = json.dumps({'department_id': topic, **change}, separators=(',', ':'))
    # PostgreSQL NOTIFY's protocol limit is 8000 bytes, including its terminating byte.
    if len(payload.encode()) >= 8000:
        # Keep the wake-up when the bounded patch cannot fit; the reader then
        # uses its existing authoritative workspace read.
        change = _change(scope, id, job_id)
        if change is None:
            return
        payload = json.dumps({'department_id': topic, **change}, separators=(',', ':'))
        if len(payload.encode()) >= 8000:
            return
    db.execute(text('SELECT pg_notify(:channel, :payload)'), {'channel': CHANNEL, 'payload': payload})


def _deduplicate(changes):
    whole = {change['scope'] for change in changes if change.get('id') is None}
    deduped = {}
    for change in changes:
        key = (change['scope'], change.get('id'))
        if change.get('id') is None or change['scope'] not in whole:
            deduped[key] = change
    return list(deduped.values())


def changes_since(topics, seen, through=None):
    """Read the exact captured interval; gaps, evictions and unscoped writers return null."""
    if isinstance(topics, str):
        topics, seen = (topics,), (seen,)
        through = (through,) if through is not None else None
    through = through if through is not None else version(topics)
    changes = []
    for topic, start, end in zip(topics, seen, through):
        if start == end:
            continue
        ring = _changes.get(topic)
        if start > end or not ring or ring[0][0] > start:
            return None
        for _, number, change in ring:
            if start < number <= end:
                if change is None:
                    return None
                changes.append(change)
    return _deduplicate(changes)


async def wait(department_id, seen, timeout):
    """Return on a subscribed change, process drain, or the session recheck deadline."""
    if version(department_id) != seen:
        return
    topics = department_id if isinstance(department_id, tuple) else (department_id,)
    events = [_events.setdefault(topic, asyncio.Event()) for topic in topics]
    for event in events:
        _waiters[event] = _waiters.get(event, 0) + 1
    tasks = [asyncio.create_task(event.wait()) for event in events]
    if _draining is not None:
        tasks.append(asyncio.create_task(_draining.wait()))
    try:
        await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for topic, event in zip(topics, events):
            remaining = _waiters.get(event, 1) - 1
            if remaining:
                _waiters[event] = remaining
            else:
                _waiters.pop(event, None)
                if _events.get(topic) is event:
                    _events.pop(topic, None)


def listen(database_url):
    """One LISTEN connection per API process, reconnecting with backoff. PostgreSQL only."""
    if not database_url.startswith("postgresql"):
        return
    from sqlalchemy.engine import make_url
    import psycopg
    conninfo = make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    loop, lifetime = _loop, _stop

    def run():
        delay = 1
        while not lifetime.is_set():
            try:
                with psycopg.connect(conninfo, autocommit=True) as conn:
                    conn.execute(f"LISTEN {CHANNEL}")
                    _schedule(loop, lifetime, invalidate)
                    delay = 1
                    while not lifetime.is_set():
                        for note in conn.notifies(timeout=5):
                            _schedule(loop, lifetime, receive, note.payload)
            except Exception as exc:
                log.warning("live_listener_reconnect", extra={"error_code": type(exc).__name__})
                lifetime.wait(delay)
                delay = min(delay * 2, 60)

    threading.Thread(target=run, name="live-listener", daemon=True).start()
