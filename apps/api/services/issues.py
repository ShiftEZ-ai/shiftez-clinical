"""Schedule › Resolve issues: one action on many findings of a draft's rota check.

An issue is one finding of the check stored on the draft, named by `issue_key` (the web client builds the same key in
apps/web/src/issues.ts). Each batch is ONE command: one change set (so one Undo), one audit event, one full re-check.
Its reply carries what the page patches in place: the sessions it touched, the sessions it removed and the issues that
came and went, so the page never reloads the rota.

Replace puts, in each issue's place, the best available person exactly as Pick someone ranks them
(staffing.check_people with best=True: never anyone a mandatory rule blocks; no new finding first; fewest hours this
week, then in the rota, then a per-session stable shuffle of person ids). Issues are taken in session order and each
pick is placed before the next, so later picks see earlier ones. The plan is deterministic for the same rota, so Apply recomputes it (or reuses the
preview's, worked out from the same inputs) and refuses when it differs from the preview the coordinator saw (`plan_hash`).
"""
import copy
from sqlalchemy import select
from apps.api import models as m
from apps.api.auth import require_role
from apps.api.errors import require
from apps.api.snapshots import domain_snapshot, fingerprint, snapshot_hash, shift_records, workspace_content
from packages.contracts import schemas as s
from packages.domain.validation import evaluate
from packages.domain.feasibility import compare_findings
from packages.domain.rota_summary import normalized_check, issue_key
from .common import scoped, expected
from .staffing import check_people, move_seat, people_index, MAX_SCOPE_SHIFTS
from . import planning
from .planning_groups import require_change_authority

def stored_findings(workspace):
    check = normalized_check(workspace.validation)
    require(check is not None, 'NOT_CHECKED', 'Check the rota before resolving its issues', 409)
    return {issue_key(f): f for f in check.get('findings', [])}


def chosen(db, workspace, keys):
    found = stored_findings(workspace)
    if workspace.validation.get('fingerprint') != fingerprint(db, workspace):
        # Organisation revisions include unrelated edits; only a changed full check invalidates these issues.
        fresh = evaluate(domain_snapshot(db, workspace))['findings']
        stored = normalized_check(workspace.validation)['findings']
        def evidence(value):
            # Candidate explanations may change while the checked breach stays the same.
            if isinstance(value, dict):
                return {key: evidence(item) for key, item in value.items() if key not in {'cause', 'fix_hints'}}
            if isinstance(value, list):
                return [evidence(item) for item in value]
            return value
        require(sorted(evidence(fresh), key=snapshot_hash) == sorted(evidence(stored), key=snapshot_hash), 'STALE_CHECK',
                'The rota issues changed. Check the rota again and select its current issues before resolving them.', 409)
    gone = sum(k not in found for k in keys)
    require(not gone, 'ISSUE_GONE', f'{gone} of the selected issues {"is" if gone == 1 else "are"} no longer in the rota check; '
            'reload the list', 409)
    return [found[k] for k in dict.fromkeys(keys)]


def seats(findings):
    """(session, person) → the issue keys about that person on that session, in key order."""
    out = {}
    for f in findings:
        if f.get('shift_id') and f.get('person_id'):
            out.setdefault((f['shift_id'], f['person_id']), []).append(issue_key(f))
    return out


# The plans worked out lately, by everything they are worked out from: Apply after a preview of the same rota and
# selection reuses the preview's plan instead of ranking every place again (best_replacements is pure).
# shortcut: per process, up to 16 plans (then cleared); a miss (a restart, another process) only works the plan out again.
PLANS = {}


def plan_replacements(db, actor, workspace, keys, findings=None):
    """Who would take each selected person's place (see `best_replacements`) and the plan's hash."""
    before = workspace_content(db, workspace)
    stored = stored_findings(workspace)
    selected = seats([stored[k] for k in keys if k in stored] if findings is None else findings)
    require_change_authority(db, actor, workspace, before, after={**before, 'shifts': [
        {**row, 'participants': [p for p in row['participants'] if (row['id'], p['person_id']) not in selected]}
        for row in before['shifts']]})
    targets = seats(chosen(db, workspace, keys) if findings is None else findings)
    source = domain_snapshot(db, workspace)
    require(len(source['shifts']) <= MAX_SCOPE_SHIFTS * 2, 'STAFFING_SCOPE_LIMIT', 'Narrow the planning period to replace people.', 409)
    people = {row['person_id'] for row in source['memberships'] if row['department_id'] == actor.department_id}
    inputs = snapshot_hash({'source': source, 'people': sorted(people), 'targets': sorted([*k, v] for k, v in targets.items())})
    rows = PLANS.get(inputs)
    if rows is None:
        rows = best_replacements(source, targets, people)
        if len(PLANS) >= 16:
            PLANS.clear()
        PLANS[inputs] = rows
    rows = copy.deepcopy(rows)  # the kept plan never changes with a reply
    return rows, snapshot_hash({'workspace_id': workspace.id, 'content_revision': workspace.content_revision, 'plan': rows})


def best_replacements(source, targets, people):
    """One row per (session, person) of `targets` ({(shift_id, person_id): keys}), in session order: the best available
    of `people` for that place, or `replacement_id` None (stays open) with why. Pure: `source` is not changed."""
    focus = set(source['focus_ids'])  # picks are placed in copies of these sessions' seats
    shifts = {row['id']: dict(row, participants=[dict(p) for p in row['participants']]) for row in source['shifts'] if row['id'] in focus}
    work = {**source, 'shifts': [shifts.get(row['id'], row) for row in source['shifts']],
            '_resolver_clock_cache': {}}
    # Only calendar clocks survive candidate changes; membership intersections belong to each evaluation.
    work.pop('_scope_interval_cache', None)
    index = people_index(work)  # worked out once; each pick keeps it current
    rows = []
    for shift_id, person_id in sorted(targets, key=lambda t: (shifts[t[0]]['start'] if t[0] in shifts else '', *t)):
        shift = shifts.get(shift_id)
        seat = next((p for p in shift['participants'] if p['person_id'] == person_id), None) if shift else None
        row = {'keys': targets[(shift_id, person_id)], 'shift_id': shift_id, 'person_id': person_id,
               'assignment_id': seat['id'] if seat else None, 'replacement_id': None, 'replacement_name': None,
               'status': 'open', 'why': None, 'findings': []}
        if seat is None:
            row['why'] = 'They are no longer on this session'
        elif shift['locked'] or shift['cancelled']:
            row['why'] = 'The session is locked or cancelled'
        else:
            best, _ = check_people(work, shift, seat, people, best=True, index=index)
            if not best:
                row['why'] = 'No one free who no rule stops'
            else:
                pick = best[0]
                row.update(replacement_id=pick['person_id'], replacement_name=pick['display_name'], status=pick['status'],
                           findings=pick['findings'])
                supervisor = ((pick.get('supervision') or {}).get('proposal') or {}).get('supervisor_person_id')
                if supervisor:
                    row['supervisor_person_id'] = supervisor
                move_seat(index, shift, seat, pick['person_id'], supervisor)
        rows.append(row)
    return rows


def preview(db, actor, value: s.IssuePreviewInput) -> dict:
    require_role(actor, 'coordinator')
    workspace = scoped(db, m.Workspace, value.workspace_id, actor)
    expected(workspace, value.expected_revision)
    require(workspace.status == 'draft' and workspace.archived_at is None, 'WORKFLOW_STATE', 'Only a draft can be changed', 409)
    plan, plan_hash = plan_replacements(db, actor, workspace, value.keys)
    found = sum(r['replacement_id'] is not None for r in plan)
    return {'workspace_id': workspace.id, 'workspace_revision': workspace.revision, 'plan': plan, 'plan_hash': plan_hash,
            'replaceable': found, 'open': len(plan) - found,
            'basis': 'Best available first, as Pick someone ranks them: no new issue, then fewest hours this week. '
                     'Nobody a mandatory rule stops is chosen. Nothing is saved until you apply.'}


def batch(db, actor, command):
    value = s.IssueBatch.model_validate(command.payload)
    workspace = planning.draft(db, actor, value.workspace_id, command.expected_revision)
    before = workspace_content(db, workspace)
    stored = stored_findings(workspace)
    selected = seats([stored[k] for k in value.keys if k in stored])
    after = {**before, 'shifts': [
        {**row, 'participants': [p for p in row['participants'] if (row['id'], p['person_id']) not in selected]}
        for row in before['shifts']]}
    require_change_authority(db, actor, workspace, before, after=after,
                             session_definitions=value.action == 'remove')
    before_keys = set(stored_findings(workspace))
    baseline = normalized_check(workspace.validation)
    findings = chosen(db, workspace, value.keys)
    touched, removed, plan, rows = set(), [], None, {}

    def sessions(ids):
        rows = planning.selected_shifts(db, workspace, ids)
        locked = sum(r.locked for r in rows.values())
        require(not locked, 'ASSIGNMENT_LOCKED', f'{locked} of the selected sessions {"is" if locked == 1 else "are"} locked; unlock them first', 409)
        return rows

    if value.action == 'replace':
        plan, plan_hash = plan_replacements(db, actor, workspace, value.keys, findings=findings)
        require(plan_hash == value.plan_hash, 'PLAN_CHANGED', 'The rota changed since the preview; preview the replacements again', 409)
        chosen_rows = [r for r in plan if r['replacement_id']]
        require(chosen_rows, 'NOTHING_TO_REPLACE', 'No one free who no rule stops for the selected issues', 409)
        rows = sessions({r['shift_id'] for r in chosen_rows})
        assignments = {row.id: row for chunk in planning.chunks(sorted({r['assignment_id'] for r in chosen_rows}))
                       for row in db.scalars(select(m.Assignment).where(m.Assignment.id.in_(chunk),
                                                                      m.Assignment.shift_id.in_(rows)))}
        planning.prime_shift_batch(db, actor, {}, ((m.Person, {r['replacement_id'] for r in chosen_rows}),))
        for r in chosen_rows:
            seat = assignments.get(r['assignment_id'])
            require(seat is not None and seat.shift_id == r['shift_id'] and seat.person_id == r['person_id'],
                    'NOT_FOUND', 'Assignment not found', 404)
            seat.person_id, seat.supervisor_person_id, seat.team_lead, seat.fixed = r['replacement_id'], r.get('supervisor_person_id'), False, False
            for key, field in planning.assignment_source(actor, 'manual').items():
                setattr(seat, key, field)
            seat.revision += 1
            touched.add(r['shift_id'])
    elif value.action == 'unassign':
        pairs = seats(findings)
        require(pairs, 'NOTHING_TO_UNASSIGN', 'None of the selected issues is about a person on a session', 409)
        rows = sessions({sid for sid, _ in pairs})
        planning.remove_participants(db, pairs)
        touched.update(rows)
    elif value.action == 'remove':
        ids = {f['shift_id'] for f in findings if f.get('shift_id')}
        require(ids, 'NOTHING_TO_REMOVE', 'None of the selected issues is about a session', 409)
        rows = sessions(ids)
        planning.remove_shifts(db, rows)
        removed = sorted(rows)
    else:  # accept
        blocking = sum(f['severity'] == 'error' for f in findings)
        require(not blocking, 'ACCEPT_ADVISORY_ONLY', f'Only advisory issues can be accepted; {blocking} selected '
                f'{"is" if blocking == 1 else "are"} mandatory', 409)
        have = {a['key'] for a in workspace.accepted_issues or []}
        at = m.now().isoformat()
        workspace.accepted_issues = [*(workspace.accepted_issues or []),
                                     *({'key': issue_key(f), 'by': actor.identity_id, 'at': at} for f in findings if issue_key(f) not in have)]
    for sid in touched:
        row = rows[sid]
        if row.series_id:
            row.series_modified = True
        row.revision += 1
    result = planning.changed(db, actor, workspace, before, value.reason, content_changed=value.action != 'accept')
    after = {issue_key(f): f for f in result['validation']['findings']}
    if plan:  # the full check confirms the reduced one: nobody placed may break a mandatory rule there
        broken = compare_findings(baseline, result['validation'], protect_intervals=True)['new_or_worse_findings']
        require(not broken, 'REPLACEMENT_BLOCKED', 'A replacement would break a mandatory rule; nothing was changed', 409)
    return {**result, 'plan': plan, 'shifts': shift_records(db, [rows[sid] for sid in sorted(touched)]), 'removed_shift_ids': removed,
            'issues': issue_changes(before_keys, after)}


def issue_changes(before_keys, after):
    """The issues that came and went: `before_keys` the keys checked before, `after` {key: finding} now."""
    return {'added': [f for k, f in after.items() if k not in before_keys], 'removed': sorted(before_keys - set(after))}


def exceptions(db, actor, command):
    """Exception requests for many issues: each checked as `exception.create`; safety rules never."""
    from .policies import create_exception
    value = s.ExceptionBatch.model_validate(command.payload)
    records = []
    for item in value.items:
        policy = scoped(db, m.Policy, item.policy_id, actor)
        require(not s.safety_rule(policy.definition), 'SAFETY_RULE', f'“{policy.name}” keeps people safe, so it cannot have an exception', 409)
        records.append(create_exception(db, actor, s.ExceptionInput(**item.model_dump(), reason=value.reason)))
    return {'records': records}
