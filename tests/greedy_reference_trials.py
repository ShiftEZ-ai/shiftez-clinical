"""Pre-index greedy trial orchestration retained for the live parity test.

Both paths call the canonical validator. This copy exercises the previous
whole-input scans without depending on Git history during test collection.
"""

from packages.domain.conditions import counting_key
from packages.domain.generation import OWN_DUTY_CODES
from packages.domain.recurrence import inactive
from packages.domain.time import aware
from packages.domain.validation import evaluate


def own_duty_blocked(data, shift, participant, own, known):
    """Would adding this participant give them an error only their own duties decide (OWN_DUTY_CODES)? Checked by the
    shared validator on just their duties and this session (as the fill options check a candidate): milliseconds instead
    of a whole-rota check. Only ever used to skip a trial that the whole-rota check would refuse too. `known` keeps
    each person's errors without the trial until their duties change."""
    pid = participant['person_id']
    kept = {s['id']: s for s in [*own, shift]}
    reduced = {**data, 'shifts': list(kept.values()), 'focus_ids': [i for i in data['focus_ids'] if i in kept],
               '_partial': {'shifts': set(kept), 'people': {pid}}, 'quota_subjects': set(), 'skip_coverage': True}

    def errors():
        result = evaluate(reduced, explain_findings=False)
        return {(f['code'], f['rule_id'], f['start'], f['end'], f['shift_id'], counting_key(f.get('detail')))
                for f in [*result['session_findings'], *result['person_findings']]
                if f['person_id'] == pid and f['severity'] == 'error' and f['code'] in OWN_DUTY_CODES}
    if pid not in known:
        known[pid] = errors()
    shift['participants'].append(participant)
    try:
        return bool(errors() - known[pid])
    finally:
        shift['participants'].pop()


class Recheck:
    """The run's whole-rota check kept in parts (each session's own checks, each person's), so a trial re-checks only the
    sessions it touches and the person it moves. Every part comes from the shared validator, computed exactly as a whole
    check computes it (`_partial` in validation.evaluate); what a part leaves out (shapes and goals, advisory only;
    resources and session setup, which do not depend on who works) cannot change a decision. The candidate is still
    checked whole at the end. tests/test_generation_recheck.py compares every trial with a whole check."""

    def __init__(self, data):
        self.data = data
        self.prepared = data  # The current driver passes this unchanged input to the old precheck.
        focus = set(data['focus_ids'])
        live = [s for s in data['shifts'] if not inactive(s) and s['id'] in focus]
        self.spans = [(aware(s['start']), aware(s['end']), s['id']) for s in live]
        people = set(data['people']) | {p['person_id'] for s in data['shifts'] for p in s['participants']}
        self.parts = self._check({s['id'] for s in live}, people)

    def _check(self, shift_ids, people):
        r = evaluate({**self.data, '_partial': {'shifts': shift_ids, 'people': people}}, explain_findings=False)
        sessions, persons, rows = {i: [] for i in shift_ids}, {i: [] for i in [*people, None]}, {i: [] for i in people}
        for f in r['session_findings']:
            sessions[f['shift_id']].append(f)
        for f in r['person_findings']:
            persons.setdefault(f['person_id'], []).append(f)
        for row in (r['quota_report'] or {}).get('rows', []):
            rows[row['person_id']].append(row)
        return sessions, persons, rows

    def touched(self, changed):
        """The changed sessions and every session of this rota overlapping one (pooled cover and clinic-wide limits
        count the people on sessions at the same time)."""
        from packages.domain.validation_scope import affected_sessions
        return affected_sessions(self.data['shifts'], changed, spans=self.spans) | {
            s['id'] for s in changed if any(i == s['id'] for _, _, i in self.spans)}

    def result(self, parts=None):
        merged = [{**whole, **part} for whole, part in zip(self.parts, parts or ({}, {}, {}))]
        return {'findings': [f for group in (merged[0], merged[1]) for items in group.values() for f in items],
                'quota_report': {'rows': [r for items in merged[2].values() for r in items]}, '_parts': parts}

    def trial(self, changed, people):
        return self.result(self._check(self.touched(changed), set(people)))

    def accept(self, result):
        for whole, part in zip(self.parts, result['_parts'] or ({}, {}, {})):
            whole.update(part)
