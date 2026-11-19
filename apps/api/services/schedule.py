"""The schedule in force, in one place: publication effective ranges and overlaps between rotas.

A current publication is in force on [starts_on, effective_until or ends_on). A session belongs to the rota in force on
the day it starts, so an overnight session that began the night before a replacement date stays with the old rota.
Every reader of published duties (validation context, calendars, feeds, the published rota, fairness and wish ledgers,
swaps) goes through `current_publications` and `in_force_shifts`.
"""
from datetime import date, timedelta
from types import SimpleNamespace
from sqlalchemy import and_, func, select
from sqlalchemy.orm import defer
from apps.api import models as m
from apps.api.errors import require
from packages.domain.time import aware, local_midnight

LIVE = ("draft", "submitted", "approved")  # rotas still being planned or reviewed
DAY = timedelta(days=1)


def until(pub):
    """First day the publication is no longer in force."""
    return pub.effective_until or pub.ends_on


def zone_of(db, department_id):
    return db.get(m.Organisation, db.get(m.Department, department_id).organisation_id).timezone


def in_force_shifts(pub, shifts, zone, before=None):
    """The publication's sessions (snapshot dicts or Shift rows) in force: starting before its effective end, or before
    `before` (the first day of a replacement that is being planned or checked)."""
    ends = [d for d in (pub.effective_until, before) if d]
    if not ends:
        return list(shifts)
    limit = local_midnight(min(ends), zone)
    return [s for s in shifts if aware(s["start"] if isinstance(s, dict) else s.start) < limit]


def in_force_filters(publication, start=None, end=None):
    """SQL predicates for a current publication's half-open in-force window."""
    filters = []
    if start:
        filters.append(func.coalesce(publication.effective_until, publication.ends_on) > start)
    if end:
        filters.append(publication.starts_on < end)
    return filters


def in_force_clause(pub, zone, before=None):
    """`in_force_shifts` as a condition on Shift rows (for queries over many publications)."""
    ends = [d for d in (pub.effective_until, before) if d]
    same = m.Shift.workspace_id == pub.workspace_id
    return and_(same, m.Shift.start < local_midnight(min(ends), zone)) if ends else same


def frozen_wish_worked(snapshot):
    """Publication-time official work, or None for legacy evidence that predates its capture."""
    if 'wish_worked' not in snapshot:
        return None
    return {pid: {date.fromisoformat(day): set(types) for day, types in days.items()}
            for pid, days in snapshot['wish_worked'].items()}


def in_force_snapshot(db, pub):
    """The publication's snapshot as in force (duties and wishes trimmed). The stored snapshot never changes."""
    if not pub.effective_until:
        return pub.snapshot
    zone = pub.snapshot.get("timezone") or zone_of(db, pub.department_id)
    result = {**pub.snapshot, "shifts": in_force_shifts(pub, pub.snapshot.get("shifts", []), zone),
              "effective_until": pub.effective_until.isoformat()}
    if 'wishes' in result:
        from packages.domain.patterns import wish_evaluations, wish_shape, wish_worked
        from packages.domain.wish_reasons import pattern_reasons
        workspace = result.get('workspace') or {'starts_on': pub.starts_on.isoformat(),
                                               'ends_on': pub.ends_on.isoformat()}
        data = {'organisation': {'timezone': zone},
                'workspace': {**workspace, 'ends_on': result['effective_until']},
                'shifts': result['shifts'], 'wishes': result['wishes']}
        worked = frozen_wish_worked(result)
        if worked is None:
            worked = wish_worked(data)
        else:
            start, end = date.fromisoformat(workspace['starts_on']), pub.effective_until
            result['wish_worked'] = {pid: {day.isoformat(): sorted(types) for day, types in days.items()
                                           if start <= day < end} for pid, days in worked.items()}
        outcomes = wish_evaluations(data, worked)
        wishes = []
        for wish in result['wishes']:
            if wish['id'] not in outcomes:
                continue
            honoured, _, missed = outcomes[wish['id']]
            row = {**wish, 'outcome': 'granted' if honoured == 1 else 'declined' if honoured == 0 else 'partly'}
            if 'outcome_reasons' in wish:
                row['outcome_reasons'] = ([] if honoured == 1 else pattern_reasons(wish, data, worked)
                    if wish_shape(wish) is not None else [reason for reason in wish['outcome_reasons'] or []
                                                       if reason['day'] in {day.isoformat() for day in missed}])
            wishes.append(row)
        result['wishes'] = wishes
    return result


def current_publications(db, department_ids, start=None, end=None, light=False, workspace_ids=None):
    """Current publications whose in-force days meet [start, end), oldest first. `light` skips the snapshots."""
    P = m.Publication
    query = select(P).join(m.Workspace, m.Workspace.id == P.workspace_id).where(
        P.department_id.in_(list(department_ids)), P.current.is_(True), m.Workspace.archived_at.is_(None))
    if workspace_ids is not None:
        query = query.where(P.workspace_id.in_(workspace_ids))
    query = query.where(*in_force_filters(P, start, end))
    if light:
        query = query.options(defer(P.snapshot), defer(P.validation))
    return list(db.scalars(query.order_by(P.starts_on, P.sequence)))


def window(db, workspace):
    """The days a rota would be in force: an amendment of a partly replaced rota ends where that one does."""
    base = db.get(m.Publication, workspace.base_publication_id) if workspace.base_publication_id else None
    end = min(workspace.ends_on, base.effective_until) if base is not None and base.effective_until else workspace.ends_on
    return workspace.starts_on, end


def acknowledged(workspace, pub):
    """The rota knowingly overlaps this publication: it amends it, or replaces it from its own first day."""
    return pub.id == workspace.base_publication_id or (
        pub.id == workspace.replaces_publication_id and pub.starts_on < workspace.starts_on < until(pub))


def span(a, b, c, d):
    return {"starts_on": max(a, c).isoformat(), "ends_on": min(b, d).isoformat()}


def overlaps(db, department_id, workspace):
    """What a rota (a saved draft, or the dates of one about to be created) overlaps, and the ways out.

    publications: current ones in force on its days (`relation`: amends / replaces / conflict); drafts: other rotas being
    planned for the same days (competing, allowed, comparable); conflict: an overlap nobody decided on yet, which
    publishing refuses; options: Start after it (default), Replace from a date, Change the published rota (amendment).
    """
    from .workspace_access import shared_clause
    start, end = window(db, workspace)
    W = m.Workspace
    pubs = current_publications(db, [department_id], start, end, light=True)
    names = dict(db.execute(select(W.id, W.name).where(W.id.in_([p.workspace_id for p in pubs]))).all()) if pubs else {}
    rows = [{"id": p.id, "sequence": p.sequence, "name": names.get(p.workspace_id, f"Version {p.sequence}"),
             "workspace_id": p.workspace_id, "starts_on": p.starts_on.isoformat(), "ends_on": p.ends_on.isoformat(),
             "effective_until": p.effective_until.isoformat() if p.effective_until else None,
             "overlap": span(start, end, p.starts_on, until(p)),
             "relation": "amends" if p.id == workspace.base_publication_id else "replaces" if acknowledged(workspace, p) else "conflict"}
            for p in pubs]
    drafts = db.execute(select(W.id, W.name, W.status, W.starts_on, W.ends_on).where(
        W.department_id == department_id, W.archived_at.is_(None), W.status.in_(LIVE), shared_clause(W),
        ~W.id.in_(select(m.PublicationChangeList.workspace_id)),
        W.starts_on < end, W.ends_on > start, W.id != (getattr(workspace, "id", None) or "")).order_by(W.starts_on, W.name))
    others = [{"id": i, "name": n, "status": st, "starts_on": a.isoformat(), "ends_on": b.isoformat(), "overlap": span(start, end, a, b)}
              for i, n, st, a, b in drafts]
    conflict = any(r["relation"] == "conflict" for r in rows)
    return {"starts_on": start.isoformat(), "ends_on": end.isoformat(), "publications": rows, "drafts": others,
            "conflict": conflict, "options": [] if workspace.base_publication_id else options(workspace, pubs)}


def options(workspace, pubs):
    """The three ways to place a rota beside published ones, with the dates each would use."""
    if not pubs:
        return []
    last, earlier = pubs[-1], pubs[:-1]
    after = max(until(p) for p in pubs)
    # Replace from a date: the latest-starting rota stays in force until that date; earlier ones must have ended.
    earliest = max([last.starts_on + DAY] + [until(p) for p in earlier])
    latest = min(until(last), workspace.ends_on) - DAY
    suggested = min(max(workspace.starts_on, earliest), latest)
    return [
        {"choice": "start_after", "default": True, "available": after < workspace.ends_on,
         "starts_on": after.isoformat(), "ends_on": workspace.ends_on.isoformat(),
         "why": None if after < workspace.ends_on else "The published rota already covers every day of this one"},
        {"choice": "replace_from", "default": False, "available": earliest <= latest, "publication_id": last.id,
         "starts_on": suggested.isoformat(), "earliest": earliest.isoformat(), "latest": latest.isoformat(),
         "ends_on": workspace.ends_on.isoformat(),
         "why": None if earliest <= latest else "No day inside the published rota is left to replace from"},
        {"choice": "amend", "default": False, "available": len(pubs) == 1, "publication_id": last.id,
         "starts_on": last.starts_on.isoformat(), "ends_on": last.ends_on.isoformat(),
         "why": None if len(pubs) == 1 else "This rota overlaps more than one published rota; change them one at a time"},
    ]


def candidate(starts_on, ends_on, base_publication_id=None, replaces_publication_id=None):
    """The dates of a rota about to be created, shaped like a workspace for `overlaps`."""
    return SimpleNamespace(id=None, starts_on=starts_on, ends_on=ends_on, base_publication_id=base_publication_id,
                           replaces_publication_id=replaces_publication_id)


def check_replacement(db, workspace):
    """A replacement trims exactly one rota: it started earlier and is still in force on this rota's first day, and no
    other published rota is in force on this rota's days."""
    pub = db.get(m.Publication, workspace.replaces_publication_id)
    require(pub is not None and pub.department_id == workspace.department_id, "NOT_FOUND", "Published rota not found", 404)
    require(workspace.base_publication_id is None, "REPLACEMENT_SCOPE", "A rota either changes a published one or replaces it from a date, not both")
    require(pub.current and pub.starts_on < workspace.starts_on < until(pub), "REPLACEMENT_SCOPE",
            "Replace from a day inside the published rota, after its first day", 409)
    others = [p for p in current_publications(db, [workspace.department_id], workspace.starts_on, workspace.ends_on, light=True) if p.id != pub.id]
    require(not others, "REPLACEMENT_SCOPE", "Another published rota is in force on these days; a replacement can replace one rota", 409)
    return pub


def make_room(db, workspace):
    """At publish: refuse an undecided overlap, then supersede the amended rota or trim the replaced one.
    Returns (superseded publication or None, trimmed publication or None, effective_until for the new publication)."""
    start, end = window(db, workspace)
    found = current_publications(db, [workspace.department_id], start, end, light=True)
    if workspace.base_publication_id:
        require([p.id for p in found] == [workspace.base_publication_id], "PUBLICATION_HEAD_CHANGED",
                "The published rota this changes was changed or replaced meanwhile; start again from the current one", 409,
                overlaps(db, workspace.department_id, workspace))
        old = found[0]
        if old.effective_until:
            # An amendment of a partly replaced rota stays within the days it is still in force.
            zone = zone_of(db, workspace.department_id)
            outside = db.scalar(select(func.count()).select_from(m.Shift).where(
                m.Shift.workspace_id == workspace.id, m.Shift.start >= local_midnight(old.effective_until, zone)))
            require(not outside, "REPLACED_DAYS", f"{outside} session{'s are' if outside != 1 else ' is'} on days a later rota "
                    f"replaced from {old.effective_until.isoformat()}; remove them before publishing", 409)
        old.current = False
        db.get(m.Workspace, old.workspace_id).status = "superseded"
        db.flush()  # before the new row: the database refuses two current rotas on one day
        return old, None, old.effective_until
    if workspace.replaces_publication_id:
        require(any(p.id == workspace.replaces_publication_id for p in found), "PUBLICATION_HEAD_CHANGED",
                "The published rota this replaces was changed or replaced meanwhile; choose again", 409,
                overlaps(db, workspace.department_id, workspace))
        old = check_replacement(db, workspace)
        old.effective_until = workspace.starts_on
        db.flush()
        return None, old, None
    require(not found, "OVERLAPPING_PUBLICATION", "These days already have a published rota; choose how this rota fits", 409,
            overlaps(db, workspace.department_id, workspace))
    return None, None, None


def flags(db, department_id, rows):
    """Per rota in a list: competing drafts for the same days and published rotas it overlaps without a decision.
    shortcut: compares every live rota with every other (tens per department); index by date if that grows."""
    from .workspace_access import shared_clause
    live = [r for r in rows if r.status in LIVE and r.archived_at is None]
    if not live:
        return {}
    lo, hi = min(r.starts_on for r in live), max(r.ends_on for r in live)
    W = m.Workspace
    drafts = list(db.execute(select(W.id, W.starts_on, W.ends_on).where(
        W.department_id == department_id, W.archived_at.is_(None), W.status.in_(LIVE), shared_clause(W),
        ~W.id.in_(select(m.PublicationChangeList.workspace_id)), W.starts_on < hi, W.ends_on > lo)))
    pubs = current_publications(db, [department_id], lo, hi, light=True)
    result = {}
    for r in live:
        start, end = window(db, r)
        result[r.id] = {"drafts": [i for i, a, b in drafts if i != r.id and a < end and start < b],
                        "publications": [p.id for p in pubs if p.starts_on < end and start < until(p) and not acknowledged(r, p)]}
    return result
