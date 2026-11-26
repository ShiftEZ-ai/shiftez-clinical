"""Bounded coordinator decision support; never authority, assignment or consent."""

import copy
from hashlib import sha256
from packages.domain.recurrence import inactive
from packages.domain import leadership
import uuid
from sqlalchemy import select
from apps.api import models as m
from apps.api.auth import require_role
from apps.api.errors import require
from apps.api.snapshots import domain_snapshot, snapshot_hash
from packages.contracts import schemas as s
from packages.domain.solver_summary import REASON_KIND  # reason tags shared with the auto-schedule summary
from packages.domain.assignment_override import assignment_override_allowed
from packages.domain.conditions import counting_key, counting_views
from packages.domain.validation import evaluate, finding_observations, prepare_assignment_trials
from .common import scoped, expected
from .planner_tools import PLANNER_ROLES, assignment_stamp

MAX_SCOPE_SHIFTS = 1000
MAX_DIRECTORY_MATCHES = 10000
MAX_FINDINGS = 100
FILL_OPTIONS_JOB_KIND = 'fill_options'


def best_order_key(week_hours, rota_hours, person_id, session_id):
    """Rank rounded weekly/rota hours, then a per-session stable shuffle of person ids."""
    return round(week_hours, 1), round(rota_hours, 1), sha256(f'{session_id}:{person_id}'.encode()).hexdigest()


def _candidate_finding_key(finding):
    """Keep the complete checked constraint identity, including session-only findings."""
    detail = finding.get('detail') or {}
    # Counts are the observation's measure, not a new constraint. Keep the rule,
    # line/slot, interval and counting bucket so a changed safety constraint still
    # compares against the same baseline observation.
    detail_identity = {key: value for key, value in detail.items() if key not in {
        'actual', 'amount', 'difference', 'required', 'available', 'maximum', 'scheduled',
        'required_hours', 'actual_hours', 'capacity', 'scheduled_sessions', 'trainees',
        'qualified_supervisors', 'available_upper_bound', 'available_eligible', 'display'}}
    # Window and duty-pair findings cite a focused duty; their window or explicit pair identifies the obligation.
    cited = (finding.get('code') in {'MAX_DUTIES', 'MAX_HOURS', 'CONSECUTIVE_DAYS', 'CONSECUTIVE_WEEKENDS'}
             or {'previous_shift_id', 'next_shift_id'} <= detail.keys()
             or {'from_shift_id', 'to_shift_id'} <= detail.keys())
    shift_id = None if cited else finding.get('shift_id')
    return (finding.get('code'), finding.get('rule_id'), shift_id, finding.get('person_id'),
            finding.get('start'), finding.get('end'), detail.get('line'), detail.get('slot'),
            counting_key(detail), snapshot_hash(detail_identity))


def _new_candidate_findings(before, after):
    """Return new/worsened observations; existing baseline errors do not block a candidate."""
    from packages.domain.feasibility import compare_findings
    from packages.domain.generation import COVERAGE_CODES
    before_rows = list(finding_observations(before))
    after_rows = list(finding_observations(after))
    coverage = lambda row: row.get('code') in COVERAGE_CODES and row.get('severity') == 'error'
    def coverage_rows(rows):
        for row in rows:
            if not coverage(row):
                continue
            detail = row.get('detail') or {}
            if row['code'] == 'SUPERVISION_GAP' and {'required', 'available'} <= detail.keys():
                # Raw supervision counts duplicate the canonical measures, not the constraint identity.
                row = {**row, 'detail': {key: value for key, value in detail.items()
                                        if key not in {'trainees', 'qualified_supervisors'}}}
            yield row
    coverage_before = list(coverage_rows(before_rows))
    coverage_after = list(coverage_rows(after_rows))
    originals = {_candidate_finding_key(row): row for row in after_rows if coverage(row)}
    # Coverage intervals may split or merge when a leave boundary disappears. Compare their measures over the
    # complete constraint span so a partial improvement is not mistaken for a new safety problem.
    interval_comparison = compare_findings(
        {'status': 'candidate_feasible', 'findings': coverage_before, 'diagnostics': [], 'candidate_evidence': []},
        {'status': 'candidate_feasible', 'findings': coverage_after, 'diagnostics': [], 'candidate_evidence': []},
        protect_intervals=True)
    remaining = {(_candidate_finding_key(f), f.get('severity')): f
                 for f in before_rows
                 if not coverage(f) and f.get('severity') in {'error', 'warning'}}
    new = []
    for finding in after_rows:
        if finding.get('severity') not in {'error', 'warning'}:
            continue
        if coverage(finding):
            continue
        key = (_candidate_finding_key(finding), finding.get('severity'))
        if key in remaining:
            old = remaining.pop(key)
            if finding['severity'] == 'error' and compare_findings(
                    {'findings': [{**old, 'shift_id': key[0][2]}]},
                    {'findings': [{**finding, 'shift_id': key[0][2]}]})['new_or_worse_findings']:
                new.append(finding)
        else:
            new.append(finding)
    return [*new, *({**originals[_candidate_finding_key(row)], 'change': row['change']}
                   for row in interval_comparison['new_or_worse_findings'])]


def candidate_status(findings):
    """Use one status vocabulary for every candidate trial consumer."""
    return 'blocked' if any(finding['severity'] == 'error' for finding in findings) else 'warn' if findings else 'ok'


def _current_validation(workspace, source, options):
    """Return complete stored evidence only when its evaluator contract is current.

    Why/fill-options are read-only decision support.  A current rota check is
    already the authoritative result of the same validator, so repeating a
    full-rota evaluation here only adds latency.  Partial, stale, or legacy
    evidence deliberately falls back to the normal evaluator.
    """
    from .rota_summary import reusable_check
    check = getattr(workspace, 'validation', None)
    if not reusable_check(check, source.get('fingerprint'), options):
        return None
    if {row['shift_id'] for row in check.get('coverage', ())} != set(source.get('focus_ids', ())):
        return None
    return copy.deepcopy(check)


def _trial_supervision_commitments(data, shift, participants):
    """Update one session in the prepared commitment index without rescanning the rota."""
    from packages.domain.time import aware
    base = data.get('_supervision_commitments')
    if base is None:
        from packages.domain.validation import supervision_commitments
        return supervision_commitments({**data, 'shifts': [dict(row, participants=participants)
            if row['id'] == shift['id'] else row for row in data['shifts']]})
    people = {row['person_id'] for row in shift['participants']} | {row['person_id'] for row in participants}
    result = {pid: ([(sid, lo, hi) for sid, lo, hi in rows if sid != shift['id']]
                    if pid in people else rows)
              for pid, rows in base.items()}
    for participant in participants:
        result.setdefault(participant['person_id'], []).append(
            (shift['id'], aware(participant['start']), aware(participant['end'])))
    return result


def suggestions(db, actor, value: s.StaffingSuggestionsInput) -> dict:
    require_role(actor, 'coordinator')
    workspace = scoped(db, m.Workspace, value.workspace_id, actor)
    expected(workspace, value.expected_revision)
    require(workspace.status == 'draft', 'WORKFLOW_STATE', 'Staffing suggestions apply only to a draft', 409)
    source = domain_snapshot(db, workspace)
    from .published_changes import validation_scope
    validation_scope(db, workspace, source)
    source.pop('_published_session_checks', None)
    source['focus_ids'] = [row['id'] for row in source['shifts'] if row.get('workspace_id') == workspace.id]
    require(len(source['focus_ids']) <= MAX_SCOPE_SHIFTS, 'STAFFING_SCOPE_LIMIT',
            f'Staffing evaluation is bounded to {MAX_SCOPE_SHIFTS} candidate/current sessions. Narrow the planning period.', 409)
    shift = next((row for row in source['shifts'] if row['id'] == value.shift_id and row['id'] in source['focus_ids']), None)
    require(shift is not None, 'NOT_FOUND', 'Session not found in this draft', 404)
    source = {**source, 'shifts': [{**row, 'closed': False, 'series_state_frozen': True} if row['id'] == shift['id'] else row for row in source['shifts']]}
    shift = next(row for row in source['shifts'] if row['id'] == value.shift_id)
    require(not shift['locked'] and not shift['cancelled'], 'ASSIGNMENT_LOCKED',
            'Explicitly unlock or reopen this draft session before staffing it', 409)
    replaced = None
    if value.replace_assignment_id:
        replaced = next((row for row in shift['participants'] if row['id'] == value.replace_assignment_id), None)
        require(replaced is not None, 'NOT_FOUND', 'Participant not found in this session', 404)
        slot = s.ParticipationInterval.model_validate({key: replaced[key] for key in
            s.ParticipationInterval.model_fields if key in replaced})
    else:
        require(len(shift['participants']) < 100, 'PARTICIPANT_LIMIT', 'This duty already has the supported maximum participants', 409)
        slot = value.slot or s.ParticipationInterval(start=shift['start'], end=shift['end'])
    # Reuse the actual write contract, including full containment and duplicate limits.
    input_keys = ('assignment_type_id', 'location_id', 'service_ids', 'attributes', 'start', 'end')
    people = {row['person_id'] for row in source['memberships'] if row['department_id'] == actor.department_id}
    already_assigned = {row['person_id'] for row in shift['participants']}
    from .search import text_matches
    if value.search_options:
        require(value.search_options.scope in {'all', 'self'} and (not value.search_options.reference or
            value.search_options.reference.kind == 'people'), 'SEARCH_SCOPE', 'Staffing searches people by their own name', 422)
    def named(row):
        if value.search_options is None:
            return value.search.casefold() in row['display_name'].casefold()
        reference = value.search_options.reference
        return row['id'] == reference.id if reference else text_matches([row['display_name']], value.search, value.search_options)
    matches = sorted((row for pid, row in source['people'].items()
        if pid in people and pid not in already_assigned and named(row)),
        key=lambda row: (row['display_name'].casefold(), row['id']))
    require(len(matches) <= MAX_DIRECTORY_MATCHES, 'STAFFING_DIRECTORY_LIMIT',
            'Too many people match the supported directory. Refine the name search; no truncated result is certified.', 409)
    # Validate the proposed slot even when no directory candidate matches.
    require(slot.start >= s.InstantInterval(start=shift['start'], end=shift['end']).start
            and slot.end <= s.InstantInterval(start=shift['start'], end=shift['end']).end,
            'PARTICIPATION_OUTSIDE_SHIFT', 'Proposed participation must lie inside this session')
    require(slot.start == s.InstantInterval(start=shift['start'], end=shift['end']).start
            and slot.end == s.InstantInterval(start=shift['start'], end=shift['end']).end,
            'FULL_ASSIGNMENT_REQUIRED', 'Staffing options must cover the complete assignment; configure a separate shorter duty when needed')
    source = prepare_assignment_trials(source)
    session_cache = {}
    baseline = evaluate(source, session_cache=session_cache)
    rows = []
    index = people_index(source)
    for candidate in matches[value.offset:value.offset + value.limit]:
        person_id = candidate['id']
        participant = s.ParticipantInput(person_id=person_id, **slot.model_dump())
        base = [row for row in shift['participants'] if not replaced or row['id'] != replaced['id']]
        planned = supervision_plan(index, shift, base, {**participant.model_dump(mode='json'),
            'id': 'candidate-' + person_id, 'shift_id': shift['id']}, {}, retain=bool(participant.supervisor_person_id))
        plan = planned['supervision'] if planned else None
        if plan and plan['proposal']:
            participant = s.ParticipantInput.model_validate(plan['proposal'])
        participants = [{key: row[key] for key in ('person_id', 'start', 'end', 'participation', 'overtime', 'segments') if key in row}
                        for row in base] + [participant.model_dump()]
        s.ShiftInput.model_validate({**{key: shift[key] for key in input_keys}, 'participants': participants})
        # No speculative SQL writes, jobs, receipts, history or notification intent.
        target = {**shift, 'participants': base + [{**participant.model_dump(mode='json'),
            'id': str(uuid.uuid5(uuid.NAMESPACE_URL, source['fingerprint'] + shift['id'] + person_id)), 'shift_id': shift['id']}]}
        trial = {**source, 'shifts': [target if row['id'] == shift['id'] else row for row in source['shifts']]}
        validation_scope(db, workspace, trial, own=[r for r in trial['shifts'] if r.get('workspace_id') == workspace.id])
        result = evaluate(trial, session_cache=session_cache)
        relevant = [finding for finding in result['findings']
                    if finding['person_id'] == person_id or finding['shift_id'] == shift['id']]
        rows.append({'person_id': person_id, 'display_name': candidate['display_name'],
            **({'supervision': plan} if plan else {}),
            'whole_candidate_valid': result['valid'] and not (plan and plan['proposal'] is None), 'mandatory_findings_remaining': result['error_count'],
            'advisory_findings_remaining': result['warning_count'],
            'person_blockers': sum(f['severity'] == 'error' and f['person_id'] == person_id for f in result['findings']),
            'findings': relevant[:MAX_FINDINGS], 'findings_total': len(relevant), 'findings_partial': len(relevant) > MAX_FINDINGS})
    return {'workspace_id': workspace.id, 'workspace_revision': workspace.revision, 'shift_id': shift['id'],
        'replace_assignment_id': value.replace_assignment_id, 'slot': slot.model_dump(mode='json'),
        'source_fingerprint': source['fingerprint'], 'request_fingerprint': snapshot_hash(value.model_dump(mode='json')),
        'baseline_mandatory_findings': baseline['error_count'], 'evaluated_at': m.now().isoformat(),
        'candidates': rows, 'total': len(matches), 'offset': value.offset, 'limit': value.limit,
        'has_more': value.offset + len(rows) < len(matches), 'evaluated_count': len(rows),
        'order': 'alphabetical display name, then stable person ID; not a fairness or suitability ranking',
        'basis': 'Current recorded membership, eligibility, capability, availability, official work and shared validator. '
                 'Unknown source completeness is not proof of availability. Remaining findings are not waived. '
                 'Choosing a person changes nothing until an ordinary exact draft preview/save; publication still requires independent review.'}



QUALIFICATION_TEXT = {'PERSON_INACTIVE': 'Not an active person record.',
                      'MEMBERSHIP_MISSING': 'No department membership on this date.',
                      'RANK_NOT_ALLOWED': 'Their rank is not allowed on this duty.'}
# One short, private-safe sentence per person blocker: candidate rows and the reason refusal both say it.
BLOCKER_TEXT = {
    'EXTERNAL_ROTATION': 'Away on a rotation during this duty.',
    'UNAVAILABLE': 'Known unavailability overlaps this duty.',
    'RESTRICTION': 'A protected scheduling restriction applies.',
    'TIME_OVERLAP': 'Already working another duty at this time.',
    'MIN_REST': 'Minimum rest would be breached.',
    'TRAVEL_BUFFER': 'Required travel rest would be breached.',
    'FORBIDDEN_TRANSITION': 'This next-day duty combination is not allowed.',
    'MAX_DUTIES': 'A mandatory limit of 0 duties applies.',
    'MAX_HOURS': 'A mandatory limit of 0 hours applies.',
    'QUOTA_ABOVE_MAXIMUM': 'A mandatory limit of 0 applies.',
    'USUAL_ASSIGNEES_FIXED': 'This session keeps its usual assignees.',
}


def blocker_text(code):
    return BLOCKER_TEXT.get(code, 'This person cannot be assigned to this duty.')


def exclusion_text(code):
    """One sentence for any candidate exclusion: eligibility first, then the person blockers."""
    return QUALIFICATION_TEXT.get(code) or blocker_text(code)


def fill_options(db, actor, value: s.FillOptionsInput, *, progress=None, cancelled=None) -> dict:
    """Everyone who could take one place in a draft session, checked with the shared validator.

    Each person is evaluated on a reduced copy of the rota that keeps everything their own rules can see:
    all of their duties, every session overlapping this one and the people on those sessions, with the
    session and their neighbouring duties in focus. Only findings that adding them creates are reported,
    so existing problems elsewhere do not hide a good fit. Decision support only: nothing is saved, and
    assigning still goes through the exact draft preview and save.
    """
    require_role(actor, 'coordinator')
    workspace = scoped(db, m.Workspace, value.workspace_id, actor)
    expected(workspace, value.expected_revision)
    require(workspace.status == 'draft', 'WORKFLOW_STATE', 'Staffing options apply only to a draft', 409)
    stamp = assignment_stamp(db, workspace, value.shift_id)
    source = domain_snapshot(db, workspace)
    from .published_changes import validation_scope
    validation_scope(db, workspace, source)
    source.pop('_published_session_checks', None)
    source['focus_ids'] = [row['id'] for row in source['shifts'] if row.get('workspace_id') == workspace.id]
    require(len(source['focus_ids']) <= MAX_SCOPE_SHIFTS * 2, 'STAFFING_SCOPE_LIMIT', 'Narrow the planning period to use staffing options.', 409)
    shift = next((row for row in source['shifts'] if row['id'] == value.shift_id and row['id'] in source['focus_ids']), None)
    require(shift is not None, 'NOT_FOUND', 'Session not found in this draft', 404)
    require(not shift['locked'] and not shift['cancelled'], 'ASSIGNMENT_LOCKED', 'Unlock or reopen this session before staffing it', 409)
    current_validation = _current_validation(workspace, source, source['department'].get('generation_defaults'))
    if value.finding_key is not None and workspace.validation and current_validation is None:
        require(False, 'ISSUE_GONE',
                'This issue is no longer current on this session; reload the rota check.', 409)
    replaced = None
    if value.replace_assignment_id:
        replaced = next((row for row in shift['participants'] if row['id'] == value.replace_assignment_id), None)
        require(replaced is not None, 'NOT_FOUND', 'Participant not found in this session', 404)
    finding = None
    source = {**source, 'shifts': [{**row, 'closed': False, 'series_state_frozen': True} if row['id'] == shift['id'] else row for row in source['shifts']]}
    shift = next(row for row in source['shifts'] if row['id'] == value.shift_id)
    if value.finding_key is not None:
        from .issues import issue_key  # issues also imports check_people
        def reaches(finding):
            for row in finding_observations({'findings': [finding]}):
                ids = {row.get('shift_id'), *((row.get('detail') or {}).get('affected_shift_ids') or [])}
                for hint in row.get('fix_hints') or []:
                    ids.update((hint.get('prefill') or {}).get('shift_ids') or [])
                if shift['id'] in ids or (row['code'] in {'BALANCE_GOAL', 'BALANCE_PAIR'} and any(
                        p['person_id'] == row.get('person_id') for p in shift['participants'])):
                    return True
            return False
        checked = current_validation or evaluate(source, explain_findings=False)
        finding = next((f for f in checked['findings']
                        if issue_key(f) == value.finding_key and reaches(f)), None)
        require(finding is not None, 'ISSUE_GONE',
                'This issue is no longer current on this session; reload the rota check.', 409)
    department_people = {row['person_id'] for row in source['memberships'] if row['department_id'] == actor.department_id}
    # A publication's change list takes no override (publication.change has no reason for one); a draft's reasoned
    # assign (planning.write_shift) accepts one for anything but eligibility, so its fill panel offers it (RUL-E04).
    change_list = db.scalar(select(m.PublicationChangeList.id).where(m.PublicationChangeList.workspace_id == workspace.id)) is not None
    # SW3-A-15: a double booking or a safety rule's breach is never assigned over, even with a reason;
    # the check would only block it again as a Safety must-fix.
    hard = {'TIME_OVERLAP', 'UNAVAILABLE', 'EXTERNAL_ROTATION'} if change_list else {'TIME_OVERLAP'}
    from packages.domain.conditions import active_rules
    # Per expanded line and number, as the Fix list reads it (rota_summary.fix_list_context): a mixed rule's safety
    # number blocks, its other numbers keep the reasoned override.
    safety = {(p['id'], (p.get('template_line') or {}).get('line'), (p.get('template_line') or {}).get('slot'))
              for p in active_rules(source['policies'], context=source) if s.safety_rule(p['definition'])}
    order = {'ok': 0, 'warn': 1, 'blocked': 2}
    from .planning_groups import unrestricted
    from .workspace_access import access_record
    # SW3-A-10: say before anyone is picked that replacing a fixed assignment here is the Rota Coordinator's (guard_changes).
    fixed_refusal = ('Only the Rota Coordinator can change fixed assignments'
                     if replaced is not None and replaced.get('fixed') and not unrestricted(db, actor, access_record(db, workspace)) else None)
    base = {'workspace_id': workspace.id, 'workspace_revision': workspace.revision, 'shift_id': shift['id'],
            **({'assignment_stamp': stamp} if stamp else {}),
            'replace_assignment_id': value.replace_assignment_id, 'replace_refusal': fixed_refusal,
            'slot': fill_slot(shift, replaced), 'source_fingerprint': source['fingerprint'],
            'evaluated_at': m.now().isoformat(),
            'basis': 'Checked against every active rule for this session and each person’s neighbouring duties. '
                     'Best fit first: no new issues, then fewest hours this week. Nothing is saved until you assign.'}
    finalized = 0

    def ready_rows(rows):
        nonlocal finalized
        from packages.domain.finding_causes import EDIT_NON_REFUSING_CODES
        for row in rows[finalized:]:
            row['override_allowed'] = not any(f['severity'] == 'error' and f['code'] not in EDIT_NON_REFUSING_CODES and ((f['code'] in hard and f['code'] not in {'UNAVAILABLE', 'EXTERNAL_ROTATION'}) or f['kind'] == 'NOT ELIGIBLE'
                or not assignment_override_allowed(source, {
                    'person_id': row['person_id'],
                    **f, 'start': f.get('start') or base['slot']['start'],
                    'end': f.get('end') or base['slot']['end']}, change_list=change_list)
                or (f.get('rule_id') is not None and
                    (f['rule_id'], (f.get('detail') or {}).get('line'), (f.get('detail') or {}).get('slot')) in safety))
                for f in row['findings'])
        finalized = len(rows)
        # Every page uses the final row facts and the same best-fit ordering.
        return sorted(rows, key=lambda r: (order[r['status']], any(f['kind'] == 'NOT ELIGIBLE' for f in r['findings']),
                                          *best_order_key(r['week_hours'], r['rota_hours'], r['person_id'], shift['id'])))

    def page(rows, total):
        if progress:
            progress({**base, 'provisional_page': True, 'evaluated_count': len(rows), 'total': total,
                      'candidates': ready_rows(rows)})

    rows, _ = check_people(source, shift, replaced, department_people, finding=finding,
                          supervisor_person_id=value.supervisor_person_id, include_reason_short=True,
                          progress=page, cancelled=cancelled)
    return {**base, 'evaluated_at': m.now().isoformat(), 'candidates': ready_rows(rows)}


def fill_slot(shift, replaced=None):
    """The exact participation checked and assigned, including a replacement's partial times."""
    slot = {'start': shift['start'], 'end': shift['end'], 'participation': 'staff', 'overtime': False, 'segments': []}
    return {key: replaced.get(key, value) for key, value in slot.items()} if replaced else slot


def fill_options_job_scope(db, actor, value: s.FillOptionsInput):
    """Validate the cheap request boundary used before a fill-options worker job is queued."""
    require_role(actor, 'coordinator')
    workspace = scoped(db, m.Workspace, value.workspace_id, actor)
    expected(workspace, value.expected_revision)
    require(workspace.status == 'draft', 'WORKFLOW_STATE', 'Staffing options apply only to a draft', 409)
    require(workspace.archived_at is None, 'WORKFLOW_STATE', 'This draft was discarded; restore it first', 409)
    source = domain_snapshot(db, workspace)
    from .published_changes import validation_scope
    validation_scope(db, workspace, source)
    source.pop('_published_session_checks', None)
    source['focus_ids'] = [row['id'] for row in source['shifts'] if row.get('workspace_id') == workspace.id]
    require(len(source['focus_ids']) <= MAX_SCOPE_SHIFTS * 2, 'STAFFING_SCOPE_LIMIT',
            'Narrow the planning period to use staffing options.', 409)
    shift = next((row for row in source['shifts'] if row['id'] == value.shift_id and row['id'] in source['focus_ids']), None)
    require(shift is not None, 'NOT_FOUND', 'Session not found in this draft', 404)
    require(not shift['locked'] and not shift['cancelled'], 'ASSIGNMENT_LOCKED',
            'Unlock or reopen this session before staffing it', 409)
    if value.replace_assignment_id:
        require(any(row['id'] == value.replace_assignment_id for row in shift['participants']),
                'NOT_FOUND', 'Participant not found in this session', 404)
    return workspace, source['fingerprint']


def people_index(source):
    """What check_people reads about everyone, worked out once per rota: the live sessions and their times, each
    person's duties in session order with their times, each person's dated records and the active rules. Resolve
    issues' Replace checks many places on one rota and keeps the index current with `move_seat`."""
    from packages.domain.time import aware
    from packages.domain.quotas import MAX_POLICIES, MAX_ROWS, applicable_policies
    from packages.domain.validation import NOT_PERSON_RULES, PERSON_ROWS, index_person_rows, named_people, supervision_commitments
    overrides = any(p['definition'].get('overrides') for p in source['policies'] if p['status'] == 'active' and p.get('active', True))
    fast = prepare_assignment_trials(source)
    for key in ('_restriction_interval_cache', '_scope_matches_cache',
                '_coverage_availability_cache', '_candidate_absence_cache',
                '_qualification_failures_cache'):
        # Keep measured trial reuse inside this request-local snapshot only.
        fast[key] = {}
    # A read-only caller may have carried a private prepared index from an earlier snapshot; facts must be current.
    fast['_person_rows'] = index_person_rows(fast)
    fast['_supervision_commitments'] = supervision_commitments(fast)
    source = fast
    live = [row for row in source['shifts'] if not inactive(row)]
    duties = {}
    for row in live:
        for p in row['participants']:
            duties.setdefault(p['person_id'], []).append((row, p, aware(p['start']), aware(p['end'])))
    for mine in duties.values():
        mine.sort(key=lambda d: d[0]['start'])  # stable: session order within the same start
    rows_of = {name: {} for name in PERSON_ROWS}
    for name in PERSON_ROWS:
        for row in source.get(name, []):
            rows_of[name].setdefault(row['person_id'], []).append(row)
    rules = fast['policies']
    # Every check below carries each person's complete dated records, so what they hold when (conditions.change_points,
    # fairness.member_days) is worked out once per person: `dates`.
    # A check without cover reports a person's own rules, restrictions and quotas only for the people they name (the
    # validator's per-person loop, quota subjects): one person's check can leave out those naming only others. Quotas
    # only while the department's quotas are within the quota check's bounds (over them, it reports that instead), and
    # not when a rule overrides another: fairness goals read the rules before overrides, which copies already apply.
    quotas = applicable_policies(source) if source.get('workspace') else []
    bounded = len({p['id'] for p in quotas}) <= source.get('quota_policy_limit', MAX_POLICIES) and len(quotas) <= MAX_ROWS
    own = {'block_quota'} if bounded else set()
    personal = None if overrides else [(rule, named_people(rule['definition']['scope'])
                                        if rule['definition']['rule']['kind'] in own | {'restriction'}
                                        or rule['definition']['rule']['kind'] not in NOT_PERSON_RULES else None) for rule in rules]
    return {'live': live, 'order': {row['id']: i for i, row in enumerate(live)},
            'times': {row['id']: (aware(row['start']), aware(row['end'])) for row in live},
            'duties': duties, 'rows_of': rows_of, 'personal': personal, 'loads': {}, 'dates': {},
            'fast': fast}


def move_seat(index, shift, seat, person_id, supervisor_person_id=None):
    """Put `person_id` in `seat` of `shift` (a fresh place: no lead or fixing; only an explicitly checked supervisor) and keep `index` current."""
    from packages.domain.time import aware
    old = seat['person_id']
    entry = next((d for d in index['duties'].get(old, ()) if d[1] is seat), None)
    assert entry is not None and entry[0] is shift and entry[2:] == (aware(seat['start']), aware(seat['end'])), \
        'Replace preview requires an indexed seat whose interval has not changed'
    index['duties'][old].remove(entry)
    seat.update(person_id=person_id, supervisor_person_id=supervisor_person_id, team_lead=False, fixed=False)
    mine = index['duties'].setdefault(person_id, [])
    mine.append(entry)
    mine.sort(key=lambda d: (d[0]['start'], index['order'][d[0]['id']]))
    index['loads'].pop(old, None)
    index['loads'].pop(person_id, None)
    from packages.domain.validation import supervision_commitments
    index['fast']['_supervision_commitments'] = supervision_commitments(index['fast'])
    index.pop('supervision_baseline', None)
    index.pop('supervision_pool', None)
    index.pop('replacement_baseline_session_cache', None)
    index.pop('replacement_quota_baseline', None)
    index.pop('replacement_assignment_signature', None)
    for name in ('replacement_person_baselines', 'replacement_weekend_baselines'):
        personal = index.get(name, {})
        for key in list(personal):
            if key[0] in {old, person_id}:
                personal.pop(key)


def supervision_plan(index, shift, kept, candidate, info, *, selected=None, retain=False, resolution_keys=None, require_proposal=False):
    """One named association, checked in the complete roster; never move any other seat."""
    from packages.domain.feasibility import compare_findings
    from packages.domain.generation import citation_finding_key
    from packages.domain.validation import (qualified_supervisor, rule_coverage_people, supervision_commitments,
                                             trainee_intervals, policy_applies)
    data = index['fast']
    intervals = trainee_intervals(data, shift, candidate)
    if not intervals:
        return None
    cache = index.setdefault('supervision_session_cache', {})
    trial_cache = index.setdefault('supervision_trial_cache', {})

    def trial(participant, *, pairing_only=False):
        proposed = {**data, 'shifts': [dict(row, participants=kept + [participant])
                    if row['id'] == shift['id'] else row for row in data['shifts']]}
        proposed['_supervision_commitments'] = _trial_supervision_commitments(data, shift, kept + [participant])
        proposed['_reuse_supervision_commitments'] = True
        if pairing_only:
            # Alternate associations need session capacity and this trainee's named-supervisor rules, not
            # department-wide fairness/shapes. The selected placement still receives one complete check.
            proposed.update(_partial={'shifts': {shift['id']}, 'people': {candidate['person_id']}},
                            quota_subjects={candidate['person_id']})
            checked = evaluate(proposed, explain_findings=False, trial_cache=trial_cache)
            return {'findings': [*checked['session_findings'], *checked['person_findings']]}
        return evaluate(proposed, session_cache=cache, explain_findings=False, trial_cache=trial_cache)

    def proposal(participant):
        return s.ParticipantInput.model_validate({key: participant[key] for key in s.ParticipantInput.model_fields
                                                 if key in participant}).model_dump(mode='json')

    choices = []
    if not retain:
        # Same attendance, eligibility, restrictions, grant and independent-commitment helpers as validation.
        pool = index.setdefault('supervision_pool', {})
        key = shift['id'], tuple(p['id'] for p in kept), tuple(intervals)
        if key not in pool:
            context = {**data, 'shifts': [dict(row, participants=kept) if row['id'] == shift['id'] else row
                                        for row in data['shifts']]}
            context['_supervision_commitments'] = supervision_commitments(context)
            target = dict(shift, participants=kept)
            pool[key] = set.intersection(*({p['person_id'] for p in rule_coverage_people(context, target, a, b)
                if qualified_supervisor(context, target, p, a, b)} for a, b in intervals))
        supervisors = pool[key] - {candidate['person_id']}
        from packages.domain.supervision_levelling import levelled_order, session_spread_policies
        balanced = any(session_spread_policies(
            data['policies'],
            lambda policy, member, duty, lo, hi: policy_applies(data, policy, member, duty, lo, hi),
            candidate['person_id'], shift, lo, hi)
                       for lo, hi in intervals)
        ordered = levelled_order(supervisors, [p.get('supervisor_person_id') for p in kept]) if balanced else sorted(
            supervisors, key=lambda pid: (data['people'][pid]['display_name'].casefold(), pid))
        if require_proposal and len(ordered) == 1:
            # Best mode needs only the complete trial below for the sole possible pairing; blocked rows are discarded.
            pid = ordered[0]
            choices.append({'supervisor_person_id': pid, 'display_name': data['people'][pid]['display_name'],
                            'proposal': proposal({**candidate, 'supervisor_person_id': pid})})
            ordered = []
        unpaired = trial(candidate, pairing_only=True) if ordered else None
        for pid in ordered:
            named = {**candidate, 'supervisor_person_id': pid}
            result = trial(named, pairing_only=True)
            # Capacity findings belong to the supervisor; whole-roster comparison must see them too.
            if compare_findings(unpaired, result, protect_intervals=True)['new_or_worse_findings'] or any(
                    f['code'] in {'SUPERVISION_PAIRING', 'ATTACHMENT_PAIRING'}
                    and f.get('person_id') == candidate['person_id'] and f.get('shift_id') == shift['id']
                    for f in finding_observations(result)):
                continue
            choices.append({'supervisor_person_id': pid, 'display_name': data['people'][pid]['display_name'],
                            'proposal': proposal(named)})
    chosen = (next((c for c in choices if c['supervisor_person_id'] == selected), None) if selected
              else choices[0] if len(choices) == 1 else None)
    if require_proposal and not retain and chosen is None:
        # Best-first never selects an unpaired/ambiguous proposal. It needs no whole-rota verdict for that refusal.
        return {**info, 'supervision': {'proposal': None}}
    if 'supervision_baseline' not in index:
        index['supervision_baseline'] = evaluate(data, session_cache=cache, explain_findings=False, trial_cache=trial_cache)
    baseline = index['supervision_baseline']
    result = trial(candidate if retain or chosen is None else {**candidate, 'supervisor_person_id': chosen['supervisor_person_id']})
    errors = compare_findings(baseline, result, protect_intervals=True)['new_or_worse_findings']
    remaining = [f for f in finding_observations(result)
                 if f['severity'] == 'error' and f.get('shift_id') == shift['id']]
    old = {citation_finding_key(f) for f in finding_observations(baseline)}
    warnings = [f for f in finding_observations(result) if f['severity'] == 'warning' and citation_finding_key(f) not in old]
    required = not retain and not selected and len(choices) > 1
    if required:
        # Without a choice the offered trial remains unpaired; this is an explicit decision, not a rule waiver.
        errors = [f for f in errors if not (f['code'] in {'SUPERVISION_PAIRING', 'ATTACHMENT_PAIRING'}
                  and f.get('person_id') == candidate['person_id'] and f.get('shift_id') == shift['id'])]
        warnings.append({'code': 'SUPERVISOR_CHOICE_REQUIRED', 'message': 'Choose a named supervisor before assigning.',
                         'severity': 'warning', 'rule_id': None})
    if selected and chosen is None and not retain:
        errors.append({'code': 'SUPERVISOR_SELECTION_INVALID', 'message': 'This supervisor cannot take this pairing; choose a current option.',
                       'severity': 'error', 'rule_id': None})
    if not errors and not required and remaining:
        warnings.append({'code': 'SESSION_FINDINGS_REMAIN', 'severity': 'warning', 'rule_id': None,
                         'message': 'Other blocking findings remain on this session; review its current check.'})
    reasons = [{**{k: f[k] for k in ('code', 'severity', 'rule_id', 'message', 'detail', 'start', 'end', 'person_id', 'shift_id') if k in f},
                'kind': REASON_KIND.get(f['code'], 'RULE')} for f in [*errors, *warnings]]
    status = 'blocked' if errors else 'warn' if warnings else 'ok'
    if resolution_keys is not None:
        from packages.domain.feasibility import _comparison_parts
        info['resolves'] = None if required else not any(_comparison_parts(f)[0] in resolution_keys
                                                       for f in finding_observations(result))
    return {**info, 'status': status, 'fills': status != 'blocked' and not required and not remaining and
            not any(r['status'] == 'gap' for r in result['coverage'] if r['shift_id'] == shift['id']),
            'findings': reasons[:12], 'supervision': {'choices': choices, 'choice_required': required,
                'proposal': proposal(candidate) if retain else chosen['proposal'] if chosen else None}}


def check_people(source, shift, replaced, candidates, best=False, index=None, *, retain_assignment=False, finding=None,
                 supervisor_person_id=None, include_reason_short=False, session_safety=True, progress=None, cancelled=None):
    """Each of `candidates` (not already on the session) checked for one place on it: eligibility, then only the
    findings adding them creates (see fill_options). Returns (rows in name order, the slot they would take).

    `best`: only the best available person, in fill options' order (no new finding, then only advisory ones; fewest
    hours this week, then in the rota (both rounded to tenths), then a per-session stable shuffle of person ids),
    stopping at the first with no new finding. Returns ([that row] or [], slot); never someone a mandatory rule blocks.
    Resolve issues' Replace.
    People the auto-schedule's own cheap tests rule out (not qualified, away, restricted or on leave then) are skipped
    before the full check, as the auto-schedule skips them.

    `index`: `people_index(source)` when the caller checks many places on the same rota (kept current by `move_seat`).
    `retain_assignment`: Why compares the same seat, including its named supervisor and lead flag.
    `finding`: a server-validated current session issue; resolves is unknown unless the existing covered trial
    checks all its constraints. Only session-local minimum cover is supported for additions, not replacements."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    from packages.domain.time import aware
    from packages.domain.validation import (qualification_failures, categories_for, PERSON_ROWS, index_person_rows,
        policy_group_tiers, rotation_blocks, available_for_coverage, blocking_restrictions, policy_applies,
        workload_selected, workload_gaps, workload_weekend_violations, participation_start_day, exception_for,
        effective_type_definition)
    from packages.domain.validation import supervision_commitments
    from packages.domain.feasibility import compare_findings
    index = index or people_index(source)
    live, times, duties, rows_of = index['live'], index['times'], index['duties'], index['rows_of']
    kept = [p for p in shift['participants'] if not replaced or p['id'] != replaced['id']]
    slot = fill_slot(shift, replaced)
    start, end = aware(shift['start']), aware(shift['end'])
    # Shared cover and capacity only involve sessions overlapping this one; the candidate's own rules use their own duties.
    near = {row['id'] for row in live if times[row['id']][0] < end and start < times[row['id']][1]}
    zone = ZoneInfo(source['organisation']['timezone'])
    local = start.astimezone(zone)
    week_lo = (local - timedelta(days=(local.weekday() - source['organisation'].get('week_start', 0)) % 7)).replace(hour=0, minute=0, second=0, microsecond=0)
    week_hi = week_lo + timedelta(days=7)
    definition = effective_type_definition(source, shift)
    services = source.get('services') or {}
    quota_lo = (start.astimezone(zone) - timedelta(days=1)).date().isoformat()
    quota_hi = (end.astimezone(zone) + timedelta(days=1)).date().isoformat()
    assigned = {p['person_id'] for p in kept} | ({replaced['person_id']} if replaced else set())
    resolution_keys = None
    if finding is not None and replaced is None:
        from packages.domain.feasibility import _comparison_parts
        observations = list(finding_observations({'findings': [finding]}))
        # Pooled cover changes its anchor/affected sessions with focus; this reduced trial cannot certify it.
        if all(f['code'] == 'SCOPED_COVERAGE_GAP' and f['shift_id'] == shift['id']
               and not (f.get('detail') or {}).get('affected_shift_ids') for f in observations) and not any(
                p['id'] == finding['rule_id'] and p['definition']['rule'].get('selection_order')
                for p in index['fast']['policies']):
            # Ordered populations inspect unassigned people's facts too, which this reduced trial omits.
            resolution_keys = {_comparison_parts(f)[0] for f in observations}

    named = sorted(set(candidates) - assigned, key=lambda pid: (source['people'][pid]['display_name'].casefold(), pid))
    # Assignment-only evidence is keyed by each person's current duty signature;
    # keep it across replacement pairs while the index remains request-local.
    trial_cache = index.setdefault('replacement_trial_cache', {})
    baseline_session_cache = index.setdefault('replacement_baseline_session_cache', {})
    policy_applies_cache = index['fast'].setdefault('_policy_applies_cache', {})
    session_caches = index.setdefault('replacement_session_caches', {})

    def session_cache_for(policies):
        key = tuple(id(policy) for policy in policies)
        return session_caches.setdefault(key, {})
    baseline = None
    quota_subjects = frozenset(source.get('quota_subjects', source['people']))

    def safety_quota_baseline():
        """Reuse quotas only for the same subjects and assignment content."""
        if 'replacement_assignment_signature' not in index:
            index['replacement_assignment_signature'] = tuple((row['id'], repr(row['participants'])) for row in live)
        key = quota_subjects, index['replacement_assignment_signature']
        cache = index.setdefault('replacement_quota_baseline', {})
        if key not in cache:
            data = {**index['fast'], 'focus_ids': list(index['fast']['focus_ids']),
                    '_reuse_supervision_commitments': True, 'quota_subjects': set(quota_subjects)}
            cache[key] = evaluate({**data, '_staffing_baseline': 'quota_baseline'},
                                  quota_only=True, explain_findings=False, trial_cache=trial_cache)
        return cache[key]

    def with_person_baseline(checked, pid, mine):
        """Existing findings on the candidate's other duties are not caused by this placement."""
        cache = index.setdefault('replacement_person_baselines', {})
        key = pid, tuple((row['id'], repr(row['participants'])) for row, _, _, _ in mine)
        if key not in cache:
            own = {row['id']: row for row, _, _, _ in mine}
            data = {**index['fast'], 'shifts': list(own.values()), 'focus_ids': list(own),
                    'person_rule_subjects': {pid}, 'skip_coverage': True, '_skip_staffing_quota': True,
                    '_allow_session_cache_with_skip_coverage': True, '_reuse_supervision_commitments': True}
            data['_supervision_commitments'] = supervision_commitments(data)
            cache[key] = (evaluate({**data, '_staffing_baseline': 'person_baseline'},
                                  explain_findings=False, session_cache=baseline_session_cache, trial_cache=trial_cache)
                          if own else {'findings': []})
        findings = {(_candidate_finding_key(f), f['severity']): f
                    for result in (checked, cache[key]) for f in finding_observations(result)}
        return {**checked, 'findings': list(findings.values())}

    def safety_baseline():
        """Compute one complete session baseline only when a candidate needs it."""
        nonlocal baseline
        if index.get('supervision_baseline') is not None:
            return index['supervision_baseline']
        if baseline is None:
            # Keep the replaced seat in the baseline so a lost lead or other session safety
            # fact is visible; candidate trials remain scoped to the candidate's own duties.
            baseline_participants = [dict(row) for row in shift['participants']]
            baseline_ids = near | {shift['id']}
            baseline_shifts = [dict(row, participants=baseline_participants) if row['id'] == shift['id'] else row
                               for row in index['fast']['shifts'] if row['id'] in baseline_ids]
            baseline_data = {**index['fast'], 'shifts': baseline_shifts, 'focus_ids': sorted(baseline_ids),
                             '_skip_staffing_quota': True,
                             '_supervision_commitments': supervision_commitments(
                                 {**index['fast'], 'shifts': baseline_shifts}),
                             '_reuse_supervision_commitments': True}
            # The private marker is test-only instrumentation; validation ignores unknown snapshot keys.
            baseline = evaluate({**baseline_data, '_staffing_baseline': 'baseline',
                                 'focus_ids': [shift['id']], 'skip_coverage': False},
                                explain_findings=False, trial_cache=trial_cache)
            quota = safety_quota_baseline()
            baseline = {**baseline, 'findings': [*baseline['findings'], *quota['findings']],
                        'quota_report': quota['quota_report']}
        return baseline

    from packages.domain.conditions import active_rules
    active_policy_ids = {policy['id'] for policy in active_rules(source['policies'], context=source)}
    safety_policies = [policy for policy in index['fast']['policies'] if policy['id'] in active_policy_ids
                       if policy['definition'].get('severity', 'mandatory') == 'mandatory'
                       and policy['definition']['rule']['kind'] in {'min_rest', 'travel_buffer', 'forbidden_transition'}]
    # shortcut: retain the full trial when counting rules are active so their independent finding is preserved; remove
    # this fallback when a shared cheap counting precheck exists.
    counting_policies_present = any(policy['definition']['rule']['kind'] in {
        'block_quota', 'max_hours', 'max_duties', 'max_consecutive_days', 'max_consecutive_weekends'}
        and policy['definition'].get('severity', 'mandatory') == 'mandatory'
        for policy in index['fast']['policies'])
    weekend_policies = [policy for policy in index['fast']['_counted_policies']
                        if policy['id'] in active_policy_ids and policy['definition']['rule']['kind'] == 'max_consecutive_weekends'
                        and policy['definition'].get('severity', 'mandatory') == 'mandatory'] if best else []
    # Session-level safety is strict by default. Person score and Why pass an explicit
    # false value only because their complete placement trials re-add those findings
    # before scoring or presenting the final opportunity.
    strict_session_safety = session_safety

    def cheap_exclusion(pid, mine, qualification=None):
        """Return one canonical hard exclusion before a candidate reaches the full validator trial."""
        failures = qualification if qualification is not None else qualification_failures(
            index['fast'], pid, shift, definition, start, end)
        if failures:
            return failures[0]
        if rotation_blocks(index['fast'], pid, start, end):
            return {'code': 'EXTERNAL_ROTATION'}
        if not available_for_coverage(index['fast'], pid, start, end):
            return {'code': 'UNAVAILABLE'}
        restriction = next(blocking_restrictions(index['fast'], pid, shift, start, end), None)
        if restriction is not None:
            return {'code': 'RESTRICTION', 'rule_id': restriction['id']}
        if any(a < end and start < b for _, _, a, b in mine):
            return {'code': 'TIME_OVERLAP'}
        candidate = {**slot, 'person_id': pid, 'fixed': False, 'id': 'candidate-' + pid, 'shift_id': shift['id']}
        entries = sorted([*mine, (shift, candidate, start, end)], key=lambda row: (row[2], row[3], row[0]['id']))
        zone = source['organisation']['timezone']
        for policy in safety_policies:
            rule = policy['definition']['rule']
            if not policy_applies(index['fast'], policy, pid, shift, start, end) or exception_for(index['fast'], policy, pid, start, end):
                continue
            if counting_policies_present and not best:
                continue
            if rule['kind'] in {'min_rest', 'travel_buffer'}:
                selected, _ = workload_selected(index['fast'], policy, pid, entries)
                for gap in workload_gaps(index['fast'], rule, selected, {shift['id']}):
                    if gap['actual_hours'] < gap['required_hours']:
                        previous, current = gap['previous'], gap['current']
                        return {'code': 'MIN_REST' if rule['kind'] == 'min_rest' else 'TRAVEL_BUFFER',
                                'rule_id': policy['id'], 'start': gap['start'].isoformat(),
                                'end': gap['end'].isoformat(),
                                'detail': {'required_hours': gap['required_hours'],
                                           'actual_hours': gap['actual_hours'],
                                           'previous_shift_id': previous['id'],
                                           'next_shift_id': current['id']}}
            else:
                from_ids, to_ids = set(rule.get('from_type_ids', [])), set(rule.get('to_type_ids', []))
                candidate_day = participation_start_day(candidate, zone)
                for other, participant, other_start, other_end in entries:
                    if other['id'] == shift['id']:
                        continue
                    other_day = participation_start_day(participant, zone)
                    if (shift['assignment_type_id'] in to_ids and other['assignment_type_id'] in from_ids
                            and candidate_day == other_day + timedelta(days=1)) or (
                            shift['assignment_type_id'] in from_ids and other['assignment_type_id'] in to_ids
                            and other_day == candidate_day + timedelta(days=1)):
                        from_id, to_id, detail_start, detail_end = (
                            (other['id'], shift['id'], other_start, end)
                            if shift['assignment_type_id'] in to_ids
                            else (shift['id'], other['id'], start, other_end))
                        return {'code': 'FORBIDDEN_TRANSITION', 'rule_id': policy['id'],
                                'start': detail_start.isoformat(), 'end': detail_end.isoformat(),
                                'detail': {'from_shift_id': from_id, 'to_shift_id': to_id}}
        for policy in weekend_policies:
            if not policy_applies(index['fast'], policy, pid, shift, start, end):
                continue
            selected, _ = workload_selected(index['fast'], policy, pid, entries)
            proposed = list(workload_weekend_violations(index['fast'], policy, pid, selected, {shift['id']}))
            if not proposed:
                continue
            key = (pid, policy['id'], repr(policy.get('template_line')), counting_key(policy),
                   tuple((row['id'], repr(person)) for row, person, _, _ in mine))
            known = index.setdefault('replacement_weekend_baselines', {})
            if key not in known:
                selected, _ = workload_selected(index['fast'], policy, pid, mine)
                known[key] = {window for window in workload_weekend_violations(
                    index['fast'], policy, pid, selected, {row['id'] for row, _, _, _ in mine})
                    if not exception_for(index['fast'], policy, pid, window[0], window[1])}
            if any(window not in known[key] and not exception_for(index['fast'], policy, pid, window[0], window[1])
                   for window in proposed):
                return {'code': 'CONSECUTIVE_WEEKENDS', 'rule_id': policy['id']}
        return None

    def pairing_blocker(candidate):
        """Skip a best-mode trial only when it provably creates an unpaired trainee obligation."""
        from packages.domain.validation import (trainee_intervals, rule_coverage_people, qualified_supervisor,
                                                pairing_obligations)
        if not best or not strict_session_safety or not definition.get('supervision'):
            return None
        target = dict(shift, participants=kept + [candidate])
        data = {**index['fast'], '_supervision_commitments': _trial_supervision_commitments(index['fast'], shift, target['participants'])}
        for trainee in kept:
            pairing = trainee.get('supervisor_person_id')
            if pairing and (not replaced or pairing != replaced['person_id']):
                continue
            for lo, hi in trainee_intervals(data, target, trainee):
                lo, hi = max(lo, aware(candidate['start'])), min(hi, aware(candidate['end']))
                if lo >= hi:
                    continue
                available = rule_coverage_people(data, target, lo, hi)
                if not any(row['person_id'] == trainee['person_id'] for row in available):
                    continue
                supervisor_ids = {row['person_id'] for row in available if qualified_supervisor(data, target, row, lo, hi)}
                obligations = [(message, policy) for message, policy in pairing_obligations(
                    data, target, trainee, supervisor_ids, lo, hi, data['_counted_policies'])
                    if policy is None or policy['definition'].get('severity', 'mandatory') == 'mandatory']
                if not obligations:
                    continue
                existing = {f.get('rule_id') for f in finding_observations(safety_baseline())
                            if f['code'] == 'SUPERVISION_PAIRING' and f.get('person_id') == trainee['person_id']}
                new_obligations = [(message, policy) for message, policy in obligations
                                   if (policy['id'] if policy else None) not in existing]
                if not new_obligations:
                    continue  # Existing or interval-sensitive obligations retain the full covered comparison.
                message, limit = new_obligations[0]
                return {'code': 'SUPERVISION_PAIRING', 'severity': 'error', 'rule_id': limit['id'] if limit else None,
                        'message': message,
                        'person_id': trainee['person_id'], 'shift_id': shift['id'],
                        'start': lo.isoformat(), 'end': hi.isoformat()}
        return None

    def check(pid, mine, week, hours, qualification=None, exclusion=None, *, cover=False):
        from packages.domain.fix_list_facts import candidate_reason_short
        def explained(row, report=None):
            if not include_reason_short:
                return row
            return {**row, 'reason_short': candidate_reason_short(row, (report or {}).get('rows', []),
                timezone=source['organisation']['timezone'], types=source.get('types'))}
        def reason(finding):
            row = {'code': finding['code'], 'kind': REASON_KIND.get(finding['code'], 'RULE'),
                   'severity': finding['severity'], 'rule_id': finding['rule_id'],
                   'message': finding['message'], 'detail': finding.get('detail'),
                   'start': finding['start'], 'end': finding['end']}
            # Preserve session-only safety identity; candidate-local rows keep the
            # existing response shape used by the planner and replacement views.
            if finding.get('person_id') is None:
                row.update(person_id=None, shift_id=finding.get('shift_id'))
            return row
        person = source['people'][pid]
        prev = [d[3] for d in mine if d[3] <= start]
        nxt = [d[2] for d in mine if d[2] >= end]
        info = {'person_id': pid, 'display_name': person['display_name'], 'rota_hours': round(hours, 1), 'week_hours': round(week, 1),
                'rest_before_hours': round((start - max(prev)).total_seconds() / 3600, 1) if prev else None,
                'rest_after_hours': round((min(nxt) - end).total_seconds() / 3600, 1) if nxt else None,
                'can_lead': leadership.qualified(index['fast'], pid, shift, start, end)}
        if finding is not None:
            info['resolves'] = None
        failures = None if exclusion is not None else (qualification_failures(index['fast'], pid, shift, definition, start, end)
                                                       if qualification is None else qualification)
        if exclusion is not None:
            code = exclusion['code']
            eligibility = {'PERSON_INACTIVE', 'MEMBERSHIP_MISSING', 'RANK_NOT_ALLOWED', 'ELIGIBILITY_MISSING'}
            message = ((QUALIFICATION_TEXT.get(code) or 'Not eligible for this duty.') if code in eligibility
                       else blocker_text(code))
            exclusion_finding = {'code': code, 'kind': 'NOT ELIGIBLE' if code in eligibility else REASON_KIND.get(code, 'RULE'),
                                 'severity': 'error', 'rule_id': exclusion.get('rule_id'), 'message': message,
                                 'start': exclusion.get('start', shift['start']), 'end': exclusion.get('end', shift['end'])}
            if exclusion.get('detail') is not None:
                exclusion_finding['detail'] = exclusion['detail']
            return explained({**info, 'status': 'blocked', 'fills': False, 'findings': [exclusion_finding]})
        if failures:
            reasons = [{'code': f['code'], 'kind': 'NOT ELIGIBLE', 'severity': 'error', 'rule_id': None,
                        'message': QUALIFICATION_TEXT.get(f['code'])
                        or 'Not eligible for ' + (services.get(f.get('service_id')) or {}).get('name', 'this service') + '.'}
                       for f in failures]
            return explained({**info, 'status': 'blocked', 'fills': False, 'findings': reasons})
        before = [d[0]['id'] for d in mine if d[0]['start'] < shift['start']][-1:]
        after = [d[0]['id'] for d in mine if d[0]['start'] > shift['start']][:1]
        own = {d[0]['id'] for d in mine}
        keep = [row for row in live if row['id'] in near or row['id'] in own or row['id'] == shift['id']]
        people = {pid} | {p['person_id'] for row in keep for p in row['participants']}
        candidate = {**slot, 'person_id': pid, 'fixed': False,
                     'supervisor_person_id': replaced.get('supervisor_person_id') if retain_assignment and replaced else None,
                     'team_lead': bool(replaced.get('team_lead')) if retain_assignment and replaced else False,
                     'id': 'candidate-' + pid, 'shift_id': shift['id']}
        planned = supervision_plan(index, shift, kept, candidate, info, selected=supervisor_person_id,
                                   retain=retain_assignment, resolution_keys=resolution_keys, require_proposal=best)
        if planned is not None:
            return explained(planned)
        blocker = pairing_blocker(candidate)
        if blocker is not None:
            return explained({**info, 'status': candidate_status([blocker]), 'fills': False, 'findings': [reason(blocker)]})
        reduced = {**index['fast'], 'focus_ids': [shift['id'], *before, *after], 'quota_subjects': {pid}, 'person_rule_subjects': {pid},
                   '_allow_session_cache_with_skip_coverage': True, 'skip_coverage': True,
                   'quota_dates': (quota_lo, quota_hi), '_person_dates_cache': index['dates'],
                   '_fair_share_eligibility_policies': index['fast']['policies'],
                   **{name: [row for person in sorted(people) for row in rows_of[name].get(person, ())]
                      for name in PERSON_ROWS if name in source}}
        if index['personal'] is not None:  # their own rules only (see people_index); the check with cover reads them all
            reduced['policies'] = [rule for rule, named in index['personal'] if named is None or pid in named]
        reduced.update(_active_policies=reduced['policies'], _counted_policies=counting_views(reduced, reduced['policies']),
                       _person_rows=index_person_rows(reduced), _policy_group_tiers=policy_group_tiers(reduced['policies']),
                       _workload_normalization_cache={}, _distribution_cache={}, _allocation_match_cache={}, _policy_applies_cache=policy_applies_cache,
                       _staffing_selection_cache={})
        base_trial = {**reduced, 'shifts': keep}
        candidate_participants = kept + [candidate]
        candidate_commitments = _trial_supervision_commitments(
            {**base_trial, '_supervision_commitments': supervision_commitments(base_trial)},
            shift, candidate_participants)
        trial_data = {**reduced, '_supervision_commitments': candidate_commitments,
                      '_reuse_supervision_commitments': True,
                      'shifts': [dict(row, participants=candidate_participants)
                                 if row['id'] == shift['id'] else row for row in keep]}
        baseline = with_person_baseline(safety_baseline(), pid, mine)
        trial = evaluate(trial_data,
                         explain_findings=False, session_cache=session_cache_for(reduced['policies']),
                         trial_cache=trial_cache)
        new = _new_candidate_findings(baseline, trial)
        if not strict_session_safety:
            new = [finding for finding in new if finding.get('person_id') is not None]
        reasons = [reason(f) for f in new]
        status = candidate_status(new)
        fills = False
        supervision = definition.get('supervision') or {}
        trainee = bool(supervision) and bool(set(supervision.get('trainee_category_ids', [])) & categories_for(index['fast'], pid, start, end))
        # Recheck session/global safety with coverage enabled; reduced candidate checks
        # intentionally skip this expensive path but cannot certify lead/cover safety.
        if trainee or status != 'blocked' and (not best or status == 'ok' or cover):
            covered_baseline = with_person_baseline(safety_baseline(), pid, mine)
            full = {'policies': index['fast']['policies'], '_active_policies': index['fast']['policies'],
                    '_counted_policies': index['fast']['_counted_policies'], 'skip_coverage': False,
                    '_skip_staffing_quota': True,
                    'focus_ids': [shift['id']], '_policy_group_tiers': index['fast']['_policy_group_tiers'],
                    '_staffing_selection_cache': {}}
            covered = evaluate({**trial_data, **full},
                               explain_findings=False, session_cache=session_cache_for(full['policies']),
                               trial_cache=trial_cache)
            # Reduced trials already report candidate-local advisories. The full pass
            # contributes every new mandatory/safety observation, including session-only
            # errors such as LEAD_GAP, without changing the existing advisory ranking.
            covered_new = ([finding for finding in _new_candidate_findings(covered_baseline, covered)
                            if finding.get('severity') == 'error'] if strict_session_safety else [])
            seen = {(_candidate_finding_key(f), f.get('severity')) for f in new}
            for covered_finding in covered_new:
                key = (_candidate_finding_key(covered_finding), covered_finding.get('severity'))
                if key not in seen:
                    new.append(covered_finding)
                    seen.add(key)
            reasons = [reason(f) for f in new]
            status = candidate_status(new)
            # A role-only gap is not an open seat; a candidate who is blocked never "fills" (as in the other fill path).
            fills = status != 'blocked' and not any(r.get('places_open', 0) > 0 for r in covered['coverage'])
            if resolution_keys is not None:
                # Compare every line/bucket over the session: a deficit moving to another subinterval still remains.
                info['resolves'] = not any(_comparison_parts(f)[0] in resolution_keys
                                          for f in finding_observations(covered))
        return explained({**info, 'status': status, 'fills': fills, 'findings': reasons[:12]}, trial.get('quota_report'))

    def load(pid):
        """(their duties in time order, hours this week, hours in the rota), kept per person and week until they move."""
        mine, known = duties.get(pid, []), index['loads'].setdefault(pid, {})
        if week_lo not in known:
            known[week_lo] = (sum((min(b, week_hi) - max(a, week_lo)).total_seconds() for _, _, a, b in mine if a < week_hi and week_lo < b) / 3600,
                              sum((b - a).total_seconds() for _, _, a, b in mine) / 3600)
        return mine, *known[week_lo]

    if best:
        loads = {pid: load(pid) for pid in named}
        fallback = []
        ordered = sorted(named, key=lambda pid: best_order_key(loads[pid][1], loads[pid][2], pid, shift['id']))
        eligible = [pid for pid in ordered if cheap_exclusion(pid, loads[pid][0]) is None]
        quota_subjects = frozenset(eligible)
        for pid in eligible:
            row = check(pid, *loads[pid])
            if row.get('supervision') is not None and row['supervision']['proposal'] is None:
                continue
            if row['status'] == 'ok':
                return [row], slot
            if row['status'] == 'warn':
                fallback.append(pid)
        for pid in fallback:
            row = check(pid, *loads[pid], cover=True)
            if row.get('supervision') is not None and row['supervision']['proposal'] is None:
                continue
            if row['status'] != 'blocked':
                return [row], slot
        return [], slot
    # Mandatory eligibility exclusions are authoritative and independent of the trial result. Resolve them once
    # before entering the expensive candidate validation loop; an excluded person must never reach evaluate().
    from packages.domain.solver_control import Cancelled
    rows = []
    for pid in named:
        if cancelled and cancelled():
            raise Cancelled()
        mine, week, hours = load(pid)
        exclusion = cheap_exclusion(pid, mine, qualification=None)
        # Keep an explicit supervision plan visible even when the trainee has
        # an independent live absence/restriction; the final trial still blocks
        # the placement on that factual finding.
        if exclusion and exclusion['code'] in {'UNAVAILABLE', 'RESTRICTION', 'EXTERNAL_ROTATION'} and definition.get('supervision'):
            exclusion = None
        rows.append(check(pid, mine, week, hours, exclusion=exclusion) if exclusion else check(pid, mine, week, hours))
        if progress and len(rows) % 20 == 0:
            progress(rows, len(named))
    if progress and rows:
        progress(rows, len(named))
    return rows, slot


WHY_SCORED = 12  # selected/session-mode row effects per request
WHY_SCORE_WORK = 2_000_000  # estimated snapshot work across full validator/scorer trials
# shortcut: retain at most 16 revision-keyed Why answers; increase only with a measured memory budget.
WHY_CACHE_LIMIT = 16
_WHY_SOURCE_CACHE = {}
_WHY_RESULT_CACHE = {}


def why_runner_up(source, shift, selected, checked, validation, options, selected_score):
    """Rank only this session's eligible alternatives, with one exact trial per alternative."""
    import json
    from packages.domain.generation import COVERAGE_CODES, citation_finding_key
    from packages.domain.score import schedule_score
    from packages.domain.time import aware
    from packages.domain.validation import supervision_commitments
    existing = {citation_finding_key(f) for f in finding_observations(validation) if f['severity'] == 'error'}
    upper = ('required', 'scheduled', 'trainees', 'maximum')
    lower = ('available', 'qualified_supervisors')

    def cover_key(f):
        detail = f.get('detail') or {}
        # Preserve every constraint identity (including capability and service), excluding only changing counts.
        identity = {k: v for k, v in detail.items()
                    if k not in ('scheduled', 'trainees', *lower, 'derived_findings', 'available_person_ids')}
        return tuple(f.get(k) for k in ('code', 'person_id', 'shift_id', 'rule_id')) + (json.dumps(identity, sort_keys=True),)

    cover = {}
    for f in finding_observations(validation):
        if f['severity'] == 'error' and f['code'] in COVERAGE_CODES:
            cover.setdefault(cover_key(f), []).append((aware(f['start']), aware(f['end']), f.get('detail') or {}))
    for intervals in cover.values():
        intervals.sort(key=lambda interval: (interval[0], interval[1]))

    def new_error(f):
        if f['severity'] != 'error' or citation_finding_key(f) in existing:
            return False
        if f['code'] not in COVERAGE_CODES:
            return True
        new, cursor, end = f.get('detail') or {}, aware(f['start']), aware(f['end'])
        # Removing an unavailable person can merge coverage intervals. Compare the complete new span.
        for lo, hi, old in cover.get(cover_key(f), []):
            if hi <= cursor:
                continue
            if lo > cursor:
                break
            if any(new.get(k, 0) > old.get(k, 0) for k in upper) or any(new.get(k, 0) < old.get(k, 0) for k in lower):
                return True
            cursor = max(cursor, hi)
            if cursor >= end:
                return False
        return True

    source = {**source, '_participation_cache': {}}
    best, session_cache, trial_cache = None, {}, {}
    for row in checked:
        if row['person_id'] == selected_score['person_id'] or row['status'] == 'blocked':
            continue
        seat = {**(selected or {'start': shift['start'], 'end': shift['end'], 'participation': 'staff', 'overtime': False}),
                **((row.get('supervision') or {}).get('proposal') or {}),
                'id': 'why-' + row['person_id'], 'shift_id': shift['id'], 'person_id': row['person_id'], 'fixed': False}
        people = [p for p in shift['participants'] if selected is None or p['id'] != selected['id']] + [seat]
        trial = {**source, 'shifts': [dict(s, participants=people) if s['id'] == shift['id'] else s for s in source['shifts']]}
        trial['_supervision_commitments'] = supervision_commitments(trial)
        result = evaluate(trial, session_cache=session_cache, trial_cache=trial_cache, explain_findings=False)
        if any(new_error(f) for f in finding_observations(result)):
            continue
        score = schedule_score(trial, result, options, limit=0)
        rank = (-score['score'], -score['uncapped_score'], row['person_id'])
        if best is None or rank < best[0]:
            best = (rank, row, score)
    if best is None:
        return None
    _, row, score = best
    return {'person_id': row['person_id'], 'display_name': row['display_name'],
            'point_difference': round(selected_score['score'] - score['score'], 1),
            'uncapped_point_difference': round(selected_score['uncapped_score'] - score['uncapped_score'], 1)}


def prepare_why(db, actor, value: s.WhyInput):
    """Read the explanation's inputs so the caller can close the transaction before pure work."""
    require_role(actor, *PLANNER_ROLES)
    workspace = scoped(db, m.Workspace, value.workspace_id, actor)
    expected(workspace, value.expected_revision)
    department = db.get(m.Department, workspace.department_id)
    organisation = db.get(m.Organisation, department.organisation_id)
    source_key = (workspace.id, workspace.revision, workspace.content_revision, organisation.revision,
                  organisation.input_revision, organisation.availability_revision, department.revision, department.catalogue_revision)
    source = copy.deepcopy(_WHY_SOURCE_CACHE.get(source_key)) if source_key in _WHY_SOURCE_CACHE else None
    if source is None:
        source = domain_snapshot(db, workspace)
        _WHY_SOURCE_CACHE[source_key] = copy.deepcopy(source)
        if len(_WHY_SOURCE_CACHE) > WHY_CACHE_LIMIT:
            _WHY_SOURCE_CACHE.pop(next(iter(_WHY_SOURCE_CACHE)))
    require(len(source['focus_ids']) <= MAX_SCOPE_SHIFTS * 2, 'STAFFING_SCOPE_LIMIT', 'Narrow the planning period to see why.', 409)
    # Keep the evidence out of the public result, but let the pure Why phase
    # reuse it instead of evaluating the complete rota again.
    defaults = source['department'].get('generation_defaults')
    source['_why_validation'] = _current_validation(workspace, source, defaults)
    return source, defaults


def why(source, options, value: s.WhyInput) -> dict:
    """The planner's Why tab: for one session everyone in the department, or for one person each of the given sessions,
    sorted into assigned / free / blocked by a rule / unavailable / not eligible with the reasons fill options gives
    (the shared validator; an absence never says why) and, for the first few assigned or free, the score effect.
    Read-only, for anyone who may read the rota's rules; nothing is saved."""
    option_payload = options.model_dump(mode='json') if hasattr(options, 'model_dump') else options
    cache_key = (source.get('fingerprint'), tuple(value.shift_ids), value.person_id, value.runner_up,
                 snapshot_hash(option_payload), WHY_SCORED, WHY_SCORE_WORK)
    if cache_key in _WHY_RESULT_CACHE:
        return copy.deepcopy(_WHY_RESULT_CACHE[cache_key])
    from packages.domain.score import placement_effects, schedule_score
    from packages.domain.validation import index_person_rows
    from packages.domain.solver_summary import WHY_GROUPS, why_group
    workspace = source['workspace']
    focus = set(source['focus_ids'])
    by_id = {row['id']: row for row in source['shifts'] if row['id'] in focus}
    require(all(sid in by_id for sid in value.shift_ids), 'NOT_FOUND', 'Session not found in this rota', 404)
    department_people = {row['person_id'] for row in source['memberships'] if row['department_id'] == workspace['department_id']}
    require(value.person_id is None or value.person_id in department_people, 'NOT_FOUND', 'Person not found in this department', 404)
    require(len(department_people) <= MAX_DIRECTORY_MATCHES, 'STAFFING_DIRECTORY_LIMIT', 'Narrow the department to see why.', 409)
    # Assignment trials share request-local fact caches. Keep the complete rota for global normalisation.
    source = {**source, '_person_rows': index_person_rows(source), '_person_dates_cache': {}, '_scope_interval_cache': {},
              '_line_index_cache': {}, '_bucket_keys_cache': {}, '_workload_normalization_cache': {}, '_distribution_cache': {},
              '_allocation_match_cache': {}, '_policy_applies_cache': {}, '_policy_applies_people': True}
    validation = copy.deepcopy(source.get('_why_validation')) if source.get('_why_validation') else None
    if validation and {row['shift_id'] for row in validation.get('coverage', ())} != focus:
        validation = None
    if value.runner_up and value.person_id:
        # Runner-up safety compares alternatives with the exact current source; a stored check can be stale when
        # an input snapshot was refreshed without a matching publication check.
        # shortcut: bypass cached validation for runner-up scoring until the snapshot writer and check writer share
        # one atomic input/check receipt; remove when that receipt is persisted.
        validation = None
    base_score = None
    wanted = {value.person_id} if value.person_id else department_people
    budget, work, sessions, index = WHY_SCORED, WHY_SCORE_WORK, [], people_index(source)
    source = index['fast']
    cost = (len(source['shifts']) + sum(len(s['participants']) for s in source['shifts'])
            + len(source['people']) * (len(index['fast']['policies']) + 1) + len(source.get('wishes') or []))
    for shift in sorted((by_id[sid] for sid in dict.fromkeys(value.shift_ids)), key=lambda row: (row['start'], row['id'])):
        if inactive(shift):
            sessions.append({'shift_id': shift['id'], 'cancelled': shift['cancelled'], 'closed': shift.get('closed', False), 'rows': [],
                             'score_status': 'not_applicable', 'runner_up': None})
            continue
        rows = [{'person_id': p['person_id'], 'display_name': source['people'][p['person_id']]['display_name'],
                 'group': 'assigned', 'assignment_id': p['id'], 'team_lead': bool(p.get('team_lead')),
                 'participation': p.get('participation', 'staff'), 'supervisor_person_id': p.get('supervisor_person_id'), 'findings': []}
                for p in shift['participants'] if p['person_id'] in wanted and p['person_id'] in source['people']]
        checked, _ = check_people(source, shift, None, wanted, index=index)
        rows += [{**row, 'group': why_group(row)} for row in checked]
        rows.sort(key=lambda r: (WHY_GROUPS.index(r['group']),
                                *best_order_key(r.get('week_hours', 0), r.get('rota_hours', 0), r['person_id'], shift['id'])))
        scored = [r['person_id'] for r in rows if (value.person_id or r['group'] in {'assigned', 'free'})
                  and not (r.get('supervision') and r['supervision']['proposal'] is None)][:budget]
        runner_up, status = None, 'not_applicable'
        if scored:
            selected = next((p for p in shift['participants'] if p['person_id'] == value.person_id), None)
            trials = len(scored) + (base_score is None)
            if trials * cost > work:
                sessions.append({'shift_id': shift['id'], 'cancelled': False, 'rows': rows,
                                 'score_status': 'work_limit', 'runner_up': None})
                continue
            work -= trials * cost
            budget -= len(scored)
            if base_score is None:
                if validation is None:
                    validation = evaluate(source, explain_findings=False, session_parts=True)
                base_score = schedule_score(source, validation, options, limit=0)
            start, end = index['times'][shift['id']]
            # Recheck simultaneous cover/supervision peers; every person and department report stays complete.
            trial_source = {**source, '_partial': {
                'shifts': {sid for sid, (a, b) in index['times'].items() if sid in focus and a < end and start < b},
                'people': set(source['people']), 'complete': True, 'previous': validation}}
            effects = placement_effects(trial_source, validation, shift['id'], scored, options, base_score=base_score,
                proposals={r['person_id']: r['supervision']['proposal'] for r in rows
                           if r.get('supervision') and r['supervision']['proposal'] is not None})
            for row in rows:
                if row['person_id'] in effects:
                    row['effect'] = effects[row['person_id']]
            status = 'complete'
            if value.runner_up and value.person_id:
                alternatives, _ = check_people(source, shift, selected, department_people, index=index, retain_assignment=True)
                alternatives = [r for r in alternatives if r['person_id'] != value.person_id and r['status'] != 'blocked']
                # Reserve every eligible alternative, never certify the best of a truncated candidate set.
                if len(alternatives) * cost > work:
                    status = 'work_limit'
                else:
                    work -= len(alternatives) * cost
                    effect = effects[value.person_id]
                    runner_up = why_runner_up(trial_source, shift, selected, alternatives, validation, options,
                        {'person_id': value.person_id, 'score': effect['score_with'],
                         'uncapped_score': effect['uncapped_score_with']})
        if any((value.person_id or r['group'] in {'assigned', 'free'}) and 'effect' not in r for r in rows):
            status = 'work_limit'
        sessions.append({'shift_id': shift['id'], 'cancelled': False, 'rows': rows,
                         'score_status': status, 'runner_up': runner_up})
    result = {'workspace_id': workspace['id'], 'workspace_revision': workspace['revision'], 'source_fingerprint': source['fingerprint'],
            'person_id': value.person_id, 'evaluated_at': m.now().isoformat(), 'sessions': sessions, 'scored_limit': WHY_SCORED,
            'basis': 'Every active rule is checked for each person on this session and their neighbouring duties. '
                     'Score effects compare the current rota with and without them across all five score components, '
                     'plus the separate publication gate. Saved rotas have no stability baseline. '
                     'Runner-up compares the best eligible alternative in the same place, by score then uncapped score '
                     'then person ID; a positive difference favours the selected person. Work-limited comparisons '
                     'have no runner-up. Nothing is saved.'}
    _WHY_RESULT_CACHE[cache_key] = copy.deepcopy(result)
    if len(_WHY_RESULT_CACHE) > WHY_CACHE_LIMIT:
        _WHY_RESULT_CACHE.pop(next(iter(_WHY_RESULT_CACHE)))
    return result
