"""Bounded property values using the existing contact, fairness and availability readers."""
from datetime import timedelta

from sqlalchemy import func, select

from apps.api import models as m
from apps.api.serialization import record
from packages.contracts import schemas as s
from . import contacts, fairness


def read(db, actor, value):
    people = [record(row) for row in db.scalars(select(m.Person).where(
        m.Person.organisation_id == actor.organisation_id, m.Person.id.in_(value.person_ids)))]
    result = {row['id']: {} for row in people}
    keys = set(value.keys)
    if not people:
        return result
    if any(key.startswith('person.contact:') for key in keys):
        for pid, rows in contacts.visible_contacts(db, actor, people).items():
            for row in rows:
                key = 'person.contact:' + row['contact_type_id']
                if key in keys:
                    # Keep this family byte-for-byte compatible with PersonCard's
                    # permission-filtered VisibleContact contract.
                    result[pid][key] = s.VisibleContact.model_validate(row).model_dump(mode='json', exclude_none=True)
    workload = {key.split(':', 1)[1] for key in keys if key.startswith('person.workload:')}
    workload &= s.PERSON_PROPERTY_DEFINITIONS['workload']['members'].keys()
    allowed = set(result) if actor.permits(*s.PERMISSIONS['fairness.read_team']) else (
        {actor.person_id} & result.keys() if actor.permits('staff') else set())
    if workload and allowed:
        first, last = fairness.period(value.period.starts_on if value.period else None,
                                      value.period.ends_on if value.period else None)
        for metric in sorted(workload):
            if metric in fairness.f.WISH_METRICS:
                W = m.WishLedger
                totals = dict(db.execute(select(W.person_id, func.sum(W.value)).where(
                    W.department_id == actor.department_id, W.person_id.in_(allowed), W.metric == metric,
                    W.period_start >= first, W.period_start < last).group_by(W.person_id)).all())
            else:
                rule = fairness.metric_rule(metric, [], [])
                ledger_keys = fairness.f.keys_for(rule)
                totals = (fairness.weekends(db, actor.department_id, ledger_keys, first, last, allowed, rule)
                          if metric == 'weekends' else
                          fairness.sums(db, actor.department_id, ledger_keys, first, last, allowed))
            for pid in allowed:
                result[pid]['person.workload:' + metric] = round(float(totals.get(pid, 0)), 4)
    if 'person.away' in keys:
        allowed = set(result) if actor.permits('coordinator', 'approver') else (
            {actor.person_id} & result.keys() if actor.permits('staff') else set())
        if allowed:
            from .availability import visible_record_clause
            from .availability_patterns import expand
            now = m.now()
            A = m.Availability
            away = set(db.scalars(select(A.person_id).where(A.department_id == actor.department_id,
                A.person_id.in_(allowed), A.start <= now, A.end > now,
                A.retracted.is_(False), A.counts_as_unavailable.is_(True), visible_record_clause()).distinct()))
            zone = db.get(m.Organisation, actor.organisation_id).timezone
            away.update(row['person_id'] for row in expand(db, [actor.department_id], list(allowed),
                                                        now, now + timedelta(microseconds=1), zone))
            for pid in allowed:
                result[pid]['person.away'] = pid in away
    return result
