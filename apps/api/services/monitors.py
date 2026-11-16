"""Authenticated external monitor evidence and bounded host status observations."""
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
from sqlalchemy import select
from apps.api import models as m
from apps.api.auth import digest, require_permission
from apps.api.errors import Problem, require
from apps.api.serialization import json_value
from packages.contracts import schemas as s


def integration(db, actor):
    require_permission(actor, "integrations.monitor")
    row = db.get(m.MonitorIntegration, actor.department_id)
    return {"configured": bool(row and row.token_hash)}


def change_token(db, actor, data, correlation_id):
    integration(db, actor)
    token = secrets.token_urlsafe(32) if data.rotate else None
    row = db.get(m.MonitorIntegration, actor.department_id)
    if row is None:
        row = m.MonitorIntegration(department_id=actor.department_id)
        db.add(row)
    row.token_hash = digest(token) if token else None
    db.add(m.AuditEvent(department_id=actor.department_id, actor_id=actor.identity_id,
                       action="monitor.token_changed", target_id=actor.department_id, reason=data.reason,
                       detail={"configured": bool(token)}, correlation_id=correlation_id))
    return {"configured": bool(token), "token": token}


def authenticate(db, authorization):
    require(bool(authorization), "MONITOR_TOKEN_REQUIRED", "A monitor token is required", 401)
    scheme, _, token = authorization.partition(" ")
    candidate = digest(token)
    row = db.scalar(select(m.MonitorIntegration).where(m.MonitorIntegration.token_hash == candidate))
    # Compare fixed-size digests even for unknown tokens.
    matches = secrets.compare_digest(candidate, row.token_hash if row else "0" * 64)
    require(scheme.lower() == "bearer" and bool(token) and matches and row is not None,
            "MONITOR_TOKEN_INVALID", "The monitor token is invalid", 403)
    return row.department_id


def parse_payload(raw):
    def invalid_constant(_):
        raise ValueError("Non-finite JSON number")
    try:
        payload = json.loads(raw, parse_constant=invalid_constant)
        require(isinstance(payload, dict), "MONITOR_EVENT_INVALID", "A JSON monitor event is required")
        if "monitor_status" in payload and not ("source" in payload and "state" in payload):
            require(payload.get("monitor_status") in ("online", "offline")
                    and isinstance(payload.get("monitor_id"), str) and 1 <= len(payload["monitor_id"]) <= 160
                    and type(payload.get("timestamp")) is int and payload["timestamp"] >= 0,
                    "MONITOR_EVENT_INVALID", "Invalid HetrixTools uptime event")
            source, state, event_id = "hetrixtools", {"online": "up", "offline": "down"}[payload["monitor_status"]], None
        else:
            event = s.MonitorEvent.model_validate(payload)
            source, state, event_id = event.source, event.state, event.event_id
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        require(len(canonical.encode("utf-8")) <= s.MONITOR_LIMITS["body_bytes"],
                "BODY_TOO_LARGE", "Request body exceeds the supported size", 413)
    except (ValueError, RecursionError, UnicodeError):
        raise Problem(422, "MONITOR_EVENT_INVALID", "Invalid JSON monitor event") from None
    key = json.dumps([source, event_id], separators=(",", ":")) if event_id else canonical
    return payload, source, state, hashlib.sha256(key.encode()).hexdigest()


def receive(db, department_id, parsed):
    payload, source, state, key = parsed
    row = db.scalar(select(m.MonitorIncident).where(m.MonitorIncident.department_id == department_id,
                                                  m.MonitorIncident.event_key == key))
    if row is not None:
        require(row.raw_payload == payload, "MONITOR_EVENT_CONFLICT", "This event ID already has different evidence", 409)
        return {"accepted": True, "duplicate": True}
    db.add(m.MonitorIncident(department_id=department_id, source=source, state=state, raw_payload=payload, event_key=key))
    return {"accepted": True, "duplicate": False}


def incidents(db, actor, offset):
    require_permission(actor, "system.operations")
    rows = list(db.scalars(select(m.MonitorIncident).where(m.MonitorIncident.department_id == actor.department_id)
                          .order_by(m.MonitorIncident.received_at.desc(), m.MonitorIncident.seq.desc())
                          .offset(offset).limit(s.MONITOR_LIMITS["page_size"] + 1)))
    return {"incidents": [{"id": row.id, "source": row.source, "state": row.state,
                           "received_at": json_value(row.received_at), "raw_payload": row.raw_payload}
                          for row in rows[:s.MONITOR_LIMITS["page_size"]]],
            "next_offset": offset + s.MONITOR_LIMITS["page_size"] if len(rows) > s.MONITOR_LIMITS["page_size"] else None}


def status_files(actor, directory, lines):
    require_permission(actor, "system.operations")

    def read(name, tail=False):
        descriptor = None
        try:
            path = Path(directory) / name
            if path.is_symlink():
                raise OSError("Not a status file")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("Not a status file")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                start = max(0, info.st_size - s.MONITOR_LIMITS["status_bytes"]) if tail else 0
                stream.seek(start)
                raw = stream.read(s.MONITOR_LIMITS["status_bytes"])
                if start:
                    raw = raw.partition(b"\n")[2]  # Do not present a partial oldest line as a complete alert.
                return raw.decode("utf-8", errors="replace")
        except FileNotFoundError:
            return None
        except OSError:
            raise Problem(503, "STATUS_UNAVAILABLE", "Host status files could not be read") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    health, alerts = read("health.txt"), read("alerts.log", tail=True)
    return {"health": health, "alerts": list(deque((alerts or "").splitlines(), maxlen=lines))}
