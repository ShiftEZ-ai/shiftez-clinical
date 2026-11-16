"""Fictional identities and deterministic expected-result governance fixtures."""
from datetime import date
from apps.api import models as m
from apps.api.db import transaction


def identity(h, username, roles, person=None, department=None):
    with transaction(h.app.state.engine, write=True) as db:
        row = m.Identity(issuer="local-development", subject=username, display_name="Fictional " + username,
                         password_hash=db.get(m.Identity, h.seed["identities"]["staff"]).password_hash, person_id=person)
        db.add(row); db.flush()
        for role in roles:
            db.add(m.RoleBinding(identity_id=row.id, department_id=department or h.seed["department_id"],
                                role=role, starts_on=date(2020, 1, 1)))
        h.seed["identities"][username] = row.id
    return row.id


def developer(h):
    """The owner's set-up: department Admin plus Developer (system work: feedback triage, audit log, Operations)."""
    if 'developer' not in h.seed['identities']:
        identity(h, 'developer', ['admin', 'operator'])
    return 'developer'


def route(h, steps=None, urgent=None, start="2026-10-01", end=None, activate=True):
    definition = {"steps": steps or [{"key": "clinical", "label": "Clinical review", "role": "approver"}]}
    if urgent is not None:
        definition["urgent_steps"] = urgent
    row = h.command("admin", "review_route.create", {"name": "Constructed review route", "starts_on": start,
        "ends_on": end, "definition": definition, "reason": "Synthetic configuration, not hospital policy"})["route"]
    if activate:
        row = h.command("approver", "review_route.activate", {"reason": "Independent activation"}, row["id"], row["revision"])["route"]
    return row


def transition(h, row, action, role="staff", payload=None, status=200, key=None):
    return h.command(role, "swap." + action, payload or {"reason": "Explicit synthetic consent decision"},
                     row["id"], row["revision"], status=status, key=key)


def swap_automation(h, prepare, publish):
    """Switch the department's automatic swap preparation and publication (both on by default)."""
    me = h.get("admin", "/api/me")
    org, dept = me["organisation"], me["department"]
    h.command("admin", "department.settings", {"department_name": dept["name"], "department_revision": dept["revision"],
              "organisation_name": org["name"], "timezone": org["timezone"], "week_start": org["week_start"],
              "swap_auto_prepare": prepare, "swap_auto_publish": publish, "reason": "Synthetic swap automation"},
              dept["id"], org["revision"])
