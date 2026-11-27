"""Derive leave entitlements from dated allowances and approved availability."""
from collections import defaultdict
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from apps.api import models as m
from apps.api.auth import require_role
from apps.api.errors import require
from packages.contracts import schemas as s
from packages.domain.time import aware, local_midnight
from .independence import today
from .leave_disclosure import leave_wording


BALANCE_LEAVE_KINDS = ('approved_leave', 'recorded_leave')


def _policy(db, department_id):
    return db.scalar(select(m.LeaveBalancePolicy).where(m.LeaveBalancePolicy.department_id == department_id))


def settings(db, actor):
    require_role(actor, 'admin')
    row = _policy(db, actor.department_id)
    config = s.LeaveBalanceConfiguration.model_validate(row.definition) if row else s.LeaveBalanceConfiguration()
    first = year_start(config, today(db, actor.department_id))
    return s.LeaveBalanceSettings(revision=row.revision if row else None,
        configuration=config, default_starts_on_min=_default_start_min(first))


def _default_start_min(first):
    return first.replace(year=first.year - 1) + timedelta(days=1) if first.year > 1 else None


def _defaults(config):
    lines = defaultdict(list)
    for line in config.allowances:
        if line.person_id is None:
            lines[line.leave_type_id].append(line)
    return lines


def _default_changes(db, actor, previous, config, *, undo):
    before, after = _defaults(previous), _defaults(config)
    first = year_start(previous, today(db, actor.department_id))
    earliest = _default_start_min(first)
    undo_setup = False
    if undo and any(type_id not in after and any(line.starts_on is None for line in lines)
                    for type_id, lines in before.items()):
        event = m.AuditEvent
        saved_at = db.scalar(select(event.created_at).where(event.department_id == actor.department_id,
            event.action == 'department.leave_balance_settings').order_by(event.seq.desc()).limit(1))
        zone = db.get(m.Organisation, actor.organisation_id).timezone
        undo_setup = saved_at is not None and aware(saved_at) >= local_midnight(first, zone)
    changes = []
    for type_id in sorted(before.keys() | after.keys()):
        old_lines, new_lines = before[type_id], after[type_id]
        old_days = {line.starts_on: line.days for line in old_lines}
        new_days = {line.starts_on: line.days for line in new_lines}
        for starts_on in sorted(old_days.keys() | new_days.keys(), key=lambda day: (day is not None, day or date.min)):
            if old_days.get(starts_on) == new_days.get(starts_on):
                continue
            closed = earliest is not None and (starts_on is None or starts_on < earliest)
            # Undated defaults from initial setup remain compatible with the existing Undo.
            legacy_setup = starts_on is None and (not old_lines or undo_setup and not new_lines)
            require(not closed or legacy_setup, 'LEAVE_ALLOWANCE_HISTORY',
                    'Keep allowances for closed leave years. Add an allowance with a later start date.', 409)
            before_days, after_days = old_days.get(starts_on), new_days.get(starts_on)
            effective = None
            if starts_on is not None:
                effective = year_start(config, starts_on)
                if effective < starts_on:
                    require(effective.year < date.max.year, 'LEAVE_BALANCE_DATES', 'Choose a supported leave year')
                    effective = effective.replace(year=effective.year + 1)
                before_days, after_days = _allowance(old_lines, effective), _allowance(new_lines, effective)
            changes.append({'leave_type_id': type_id, 'starts_on': starts_on.isoformat() if starts_on else None,
                            'effective_year_start': effective.isoformat() if effective else None,
                            'before_days': before_days, 'after_days': after_days})
    if changes:
        type_ids = {line['leave_type_id'] for line in changes}
        names = dict(db.execute(select(m.LeaveType.id, m.LeaveType.name).where(
            m.LeaveType.department_id == actor.department_id, m.LeaveType.id.in_(type_ids))).all())
        for line in changes:
            line['leave_type_name'] = names.get(line['leave_type_id'])
    return changes


def update(db, actor, command):
    require_role(actor, 'admin')
    change = s.LeaveBalanceSettingsChange.model_validate(command.payload)
    row = _policy(db, actor.department_id)
    require((row.revision if row else None) == command.expected_revision, 'STALE_REVISION',
            'Reload the current leave allowance settings', 409)
    previous = s.LeaveBalanceConfiguration.model_validate(row.definition) if row else s.LeaveBalanceConfiguration()
    config = change.configuration
    undo = config is None
    if undo:
        require(row is not None and row.previous_definition is not None, 'NOTHING_TO_UNDO',
                'There are no earlier leave allowances to restore', 409)
        config = s.LeaveBalanceConfiguration.model_validate(row.previous_definition)
    else:
        types = {line.leave_type_id for line in config.allowances}
        available = set(db.scalars(select(m.LeaveType.id).where(
            m.LeaveType.department_id == actor.department_id, m.LeaveType.id.in_(types)))) if types else set()
        require(available == types, 'LEAVE_TYPE_NOT_FOUND', 'Choose leave types in this department', 404)
        people = {line.person_id for line in config.allowances if line.person_id is not None}
        member = m.Membership
        members = set(db.scalars(select(member.person_id).where(member.department_id == actor.department_id,
            member.person_id.in_(people), member.withdrawn.is_(False)))) if people else set()
        require(members == people, 'PERSON_NOT_IN_DEPARTMENT', 'Choose people in this department', 404)
    changes = _default_changes(db, actor, previous, config, undo=undo)
    definition = config.model_dump(mode='json')
    if row:
        row.previous_definition, row.definition = row.definition, definition
        row.revision += 1
    else:
        row = m.LeaveBalancePolicy(department_id=actor.department_id, definition=definition,
                                   previous_definition=s.LeaveBalanceConfiguration().model_dump(mode='json'))
        db.add(row)
    db.flush()
    return {'record': {'id': row.id, 'revision': row.revision},
            'leave_allowance_changes': changes,
            'undo': {'kind': 'department.leave_balance_settings', 'expected_revision': row.revision,
                     'payload': {'configuration': None, 'reason': 'Undo'}}}


def scrub_for_person(db, organisation_id, person_id):
    policy = m.LeaveBalancePolicy
    changed = 0
    for row in db.scalars(select(policy).join(m.Department, policy.department_id == m.Department.id)
                          .where(m.Department.organisation_id == organisation_id)):
        removed = False
        for attribute in ('definition', 'previous_definition'):
            definition = getattr(row, attribute)
            if definition is None:
                continue
            lines = definition.get('allowances', [])
            kept = [line for line in lines if line.get('person_id') != person_id]
            if len(kept) != len(lines):
                setattr(row, attribute, {**definition, 'allowances': kept})
                removed = True
        if removed:
            row.revision += 1
            changed += 1
    return changed


def year_start(config, day):
    start = date(day.year, config.year_start_month, config.year_start_day)
    return start if start <= day else start.replace(year=start.year - 1)


def _days(intervals, first, after, zone):
    days = set()
    for start, end in intervals:
        first_day = aware(start).astimezone(zone).date()
        last_day = (aware(end) - timedelta(microseconds=1)).astimezone(zone).date()
        days.update(range(max(first, first_day).toordinal(), min(after.toordinal(), last_day.toordinal() + 1)))
    return days


def _allowance(lines, first):
    applicable = (line for line in lines if line.starts_on is None or line.starts_on <= first)
    current = max(applicable, key=lambda line: (line.person_id is not None, line.starts_on is not None,
                                               line.starts_on or date.min), default=None)
    return current.days if current else None


def balances(db, actor, person_id, *, request=None, policy=None):
    if policy is None:
        policy = _policy(db, actor.department_id)
    if not policy:
        return []
    config = s.LeaveBalanceConfiguration.model_validate(policy.definition)
    by_type = defaultdict(list)
    for line in config.allowances:
        if line.person_id in (None, person_id) and (request is None or line.leave_type_id == request.leave_type_id):
            by_type[line.leave_type_id].append(line)
    if not by_type:
        return []
    zone = db.get(m.Organisation, actor.organisation_id).timezone
    zone_info = ZoneInfo(zone)
    if request is None:
        first_day = last_day = today(db, actor.department_id)
    else:
        first_day = aware(request.start).astimezone(zone_info).date()
        last_day = (aware(request.end) - timedelta(microseconds=1)).astimezone(zone_info).date()
    require(1 < first_day.year and last_day.year < 9999, 'LEAVE_BALANCE_DATES',
            'Choose leave within the supported leave years')
    first, last = year_start(config, first_day), year_start(config, last_day)
    next_year = 1 if request is not None and config.carry_over_days and last.year < 9998 else 0
    years = [first.replace(year=year) for year in range(first.year, last.year + 1 + next_year)]
    earliest = first.replace(year=max(1, first.year - 1))
    latest = years[-1].replace(year=years[-1].year + 1)
    A = m.Availability
    # Use committed approval records, including earlier approvals flushed in this transaction.
    usage = defaultdict(list)
    for key, start, end in db.execute(select(A.leave_type_id, A.start, A.end).where(
            A.department_id == actor.department_id, A.person_id == person_id, A.kind.in_(BALANCE_LEAVE_KINDS),
            A.retracted.is_(False), A.leave_type_id.in_(by_type), A.start < local_midnight(latest, zone),
            A.end > local_midnight(earliest, zone))):
        usage[key].append((start, end))
    result = []
    requested_interval = [(request.start, request.end)] if request is not None else []
    department = db.get(m.Department, actor.department_id) if request is not None else None
    for key, name in db.execute(select(m.LeaveType.id, m.LeaveType.name).where(
            m.LeaveType.department_id == actor.department_id, m.LeaveType.id.in_(by_type)).order_by(m.LeaveType.sort_order, m.LeaveType.name)):
        lines, intervals = by_type[key], usage[key]
        visible_type = (leave_wording(department, actor, request, name).get('leave_type_id')
                        if request is not None else key)
        for starts in years:
            amount = _allowance(lines, starts)
            if amount is None:
                continue
            ends = starts.replace(year=starts.year + 1)
            carried = projected_carry = 0
            if config.carry_over_days and starts.year > 1:
                previous = starts.replace(year=starts.year - 1)
                previous_amount = _allowance(lines, previous)
                if previous_amount is not None:
                    previous_used = _days(intervals, previous, starts, zone_info)
                    prior_request = _days(requested_interval, previous, starts, zone_info)
                    unused = previous_amount - len(previous_used)
                    carried = min(config.carry_over_days, max(0, unused))
                    projected_carry = min(config.carry_over_days, max(0, unused - len(prior_request - previous_used)))
            if starts > last and projected_carry == carried:
                continue
            used = _days(intervals, starts, ends, zone_info)
            remaining = amount + carried - len(used)
            requested = _days(requested_interval, starts, ends, zone_info)
            result.append(s.LeaveBalance(leave_type_id=visible_type, leave_type_name=name if visible_type else 'Leave',
                starts_on=starts, ends_on=ends, allowance_days=amount, carried_days=carried,
                used_days=len(used), remaining_days=remaining,
                projected_days=amount + projected_carry - len(used | requested) if request is not None else None))
    return result


def _overdraws(db, actor, request, policy):
    if policy is None:
        return []
    db.flush()
    return [balance for balance in balances(db, actor, request.person_id, request=request, policy=policy)
            if balance.projected_days is not None and balance.projected_days < 0]


def require_undo_balance(db, actor, absence):
    require(not _overdraws(db, actor, absence, _policy(db, actor.department_id)), 'LEAVE_BALANCE_UNDO',
            'Restoring this leave would leave a negative balance. Submit a new request for approval.', 409)


def confirm_approval(db, actor, request, confirmation):
    from apps.api.snapshots import snapshot_hash

    policy = _policy(db, actor.department_id)
    negative = [balance.model_dump(mode='json') for balance in _overdraws(db, actor, request, policy)]
    if negative:
        proof = snapshot_hash([request.id, request.revision, policy.revision, negative])
        message = 'Approving this request would leave a negative balance.'
        if db.info.get('bulk'):
            message += ' Confirm the approval in Requests.'
        require(confirmation == proof, 'LEAVE_BALANCE_CONFIRM', message, 409,
                {'balances': negative, 'balance_confirmation': proof})
    return negative
