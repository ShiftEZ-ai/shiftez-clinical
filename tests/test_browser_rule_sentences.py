"""The one sentence builder for rules (rule_view.ts ruleSentence: the Rules row, the sheet title, the schedule's rule panel,
history) and the value pill and numbers across a rule and its lines (limitPill, numbersBlock), run in the real client."""
import pytest

from .test_browser import browser, live_server, sign_in  # noqa: F401

pytestmark = pytest.mark.browser

NAMES = {'c-cons': 'Consultant', 'c-res': 'Resident', 'c-r1': 'Resident · R1', 'c-r2': 'R2', 's-phys': 'Physical ZZ', 's-virt': 'Virtual ZZ',
         's-fm': 'AA Clinic', 't-day': 'Day shift', 't-night': 'Night on-call', 'l-a': 'Main building', 'l-b': 'Annex', 'p-a': 'Dr. Woodward', 'p-b': 'Dr. Puxepine'}


def run(page, script, arg):
    return page.evaluate("async ([arg, names]) => {const m = await window.importBundle('rule_view.js'); const name = (kind, id) => names[id] || id; "
                         + script + '}', [arg, NAMES])


def scope(**fields):
    return {'conditions': {'match': 'all', 'conditions': [{'field': f, 'op': 'any', 'values': v} for f, v in fields.items()]}}


SENTENCES = [
    # Who (a rank as a noun, else "people") and where (duty types, clinics) in words; no "matching this scope".
    ({'kind': 'minimum_cover', 'minimum': 1}, scope(category_ids=['c-cons'], service_ids=['s-phys', 's-virt']),
     'At least 1 consultant at Physical ZZ & Virtual ZZ.'),
    ({'kind': 'minimum_cover', 'minimum': 2, 'aggregation': 'service_group'}, scope(service_ids=['s-phys', 's-virt'], assignment_type_ids=['t-day']),
     'At least 2 people on Day shift at Physical ZZ & Virtual ZZ together.'),
    ({'kind': 'minimum_cover', 'minimum': 2}, {}, 'At least 2 people per session.'),
    ({'kind': 'minimum_cover', 'minimum': '1–2'}, scope(category_ids=['c-r1']), 'At least 1–2 people per session.'),
    ({'kind': 'maximum_participants', 'maximum': 3, 'aggregation': 'assignment'}, scope(category_ids=['c-res'], service_ids=['s-phys']),
     'At most 3 residents at Physical ZZ per session.'),
    ({'kind': 'maximum_participants', 'maximum': '0–3', 'aggregation': 'assignment'}, scope(category_ids=['c-r1', 'c-r2'], service_ids=['s-phys']),
     'At most 0–3 people at Physical ZZ per session.'),
    ({'kind': 'maximum_participants', 'maximum': 2, 'aggregation': 'service', 'overflow_maximum': 1}, scope(service_ids=['s-fm']),
     'Normally at most 2 people at AA Clinic per clinic at a time.'),
    ({'kind': 'maximum_participants', 'maximum': 1, 'aggregation': 'assignment'}, scope(location_ids=['l-a']), 'At most 1 person in Main building per session.'),
    ({'kind': 'min_rest', 'hours': 11}, {}, 'At least 11 hours rest between duties.'),
    ({'kind': 'min_rest', 'hours': 1}, {}, 'At least 1 hour rest between duties.'),
    ({'kind': 'max_hours', 'hours': 6, 'window': 'day', 'include_overtime': True}, {}, 'At most 6 hours per day.'),
    ({'kind': 'max_hours', 'hours': 60, 'window': 'rolling', 'window_days': 7}, {}, 'At most 60 hours in any 7 days.'),
    ({'kind': 'max_hours', 'hours': 12, 'window': 'rolling', 'window_days': 1}, {}, 'At most 12 hours per day.'),
    ({'kind': 'max_duties', 'maximum': 1, 'window': 'week'}, {}, 'At most 1 duty per week.'),
    ({'kind': 'max_duties', 'maximum': '4–6', 'window': 'week', 'group_by': 'weekday'}, {}, 'At most 4–6 duties on each weekday per week.'),
    ({'kind': 'max_consecutive_days', 'maximum': 6}, {}, 'At most 6 days in a row.'),
    ({'kind': 'max_consecutive_weekends', 'maximum': 1, 'weekend_start': 4, 'weekend_days': 2}, {}, 'At most 1 weekend in a row.'),
    ({'kind': 'forbidden_transition', 'from_type_ids': ['t-night'], 'to_type_ids': ['t-day']}, {}, 'No Day shift the day after Night on-call.'),
    ({'kind': 'restriction', 'weekdays': [4], 'start_time': '08:00', 'end_time': '12:00'}, {}, 'No duties on Friday, 08:00–12:00.'),
    ({'kind': 'restriction', 'weekdays': [0, 1, 2, 3, 4, 5, 6], 'start_time': '22:00', 'end_time': '24:00'}, {}, 'No duties every day, 22:00–24:00.'),
    ({'kind': 'travel_buffer', 'minutes': 30, 'from_location_id': 'l-a', 'to_location_id': 'l-b'}, {}, 'At least 30 minutes to travel from Main building to Annex.'),
    ({'kind': 'supervision_limit', 'maximum': 3, 'trainee_category_ids': ['c-r1']}, {}, 'At most 3 trainees per supervisor (Resident · R1).'),
    ({'kind': 'supervision_limit', 'mode': 'allowed_ranks', 'maximum': 1, 'trainee_category_ids': ['c-r1', 'c-r2']}, {}, 'Supervisors supervise only Resident · R1, R2.'),
    # Quotas: the period in words, singular and plural ("each day", never "in any 1 days").
    ({'kind': 'block_quota', 'metric': 'duties', 'comparison': 'target', 'amount': '1–2', 'window_alignment': 'rolling', 'window_days': 1}, {},
     'Aim for 1–2 duties each day.'),
    ({'kind': 'block_quota', 'metric': 'duties', 'comparison': 'minimum', 'amount': 1, 'window_alignment': 'fixed', 'window_days': 1}, {}, 'At least 1 duty each day.'),
    ({'kind': 'block_quota', 'metric': 'duties', 'comparison': 'maximum', 'amount': 4, 'window_alignment': 'calendar_month', 'assignment_type_ids': ['t-night']}, {},
     'At most 4 Night on-call duties each month.'),
    ({'kind': 'block_quota', 'metric': 'session_units', 'comparison': 'range', 'amount': 6, 'maximum_amount': 8, 'target_amount': 7, 'window_alignment': 'calendar_week'}, {},
     'Between 6 and 8 session units each week · aim for 7.'),
    ({'kind': 'block_quota', 'metric': 'duties', 'comparison': 'minimum', 'amount': 2, 'window_alignment': 'rolling', 'window_days': 14}, {}, 'At least 2 duties in any 14 days.'),
    ({'kind': 'block_quota', 'metric': 'duties', 'comparison': 'minimum', 'amount': 2}, {}, 'At least 2 duties over the whole period.'),
    ({'kind': 'run_length', 'minimum': 2, 'maximum': 5}, {}, 'Prefer at least 2 and at most 5 working days in a row.'),
    ({'kind': 'days_off_run', 'minimum': 1}, {}, 'Prefer days off in blocks of at least 1 day.'),
    ({'kind': 'weekend_shape', 'shape': 'complete', 'weekend_start': 4, 'weekend_days': 2}, {}, 'Prefer whole weekends on or off.'),
    ({'kind': 'shift_succession', 'mode': 'avoid', 'from_type_ids': ['t-night'], 'to_type_ids': ['t-day'], 'within_days': 1}, {}, 'Avoid Day shift within 1 day after Night on-call.'),
    ({'kind': 'together_apart', 'mode': 'apart', 'level': 'day', 'person_ids': ['p-a', 'p-b']}, {}, 'Prefer Dr. Woodward, Dr. Puxepine not on duty on the same day.'),
    ({'kind': 'allocation_preference', 'priority': 25, 'session_priority': 40, 'overtime_priority': 0, 'supervisor_relationship': 'mentor'}, {},
     'Allocation priority 25 · session priority 40 · assigned mentor only.'),
    ({'kind': 'allocation_overtime'}, scope(service_ids=['s-phys']), 'Offer overtime at Physical ZZ after regular candidates.'),
]


def test_one_short_sentence_per_rule_kind(browser, live_server):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        got = run(page, 'return arg.map(([rule, scope]) => m.ruleSentence(rule, {scope}, name));', [[r, s] for r, s, _ in SENTENCES])
        assert got == [want for _, _, want in SENTENCES]
        for text in got:
            assert 'matching this scope' not in text and 'simultaneous' not in text and 'in any 1 day' not in text
        # What the sentence leaves out is in the sheet's details.
        details = run(page, "return arg.map(rule => m.ruleDetails(rule, 'Asia/Riyadh'));",
                      [{'kind': 'max_hours', 'include_overtime': False}, {'kind': 'restriction'},
                       {'kind': 'max_consecutive_weekends', 'weekend_start': 4, 'weekend_days': 2}, {'kind': 'min_rest'}])
        assert details == [['Planned overtime does not count.'], ['Times are in Asia/Riyadh.'], ['The weekend starts on Friday and lasts 2 days.'], []]
        # An allocation order lists its sessions first to last.
        order = run(page, 'return m.ruleSentence({kind:"allocation_preference"}, arg, name);',
                    {'line_number': 'session_priority', 'per_person': [{'amount': 1, 'service_ids': ['s-fm']}, {'amount': 2, 'service_ids': ['s-phys']}]})
        assert order == 'Fills sessions in this order: Physical ZZ → AA Clinic.'
    finally:
        context.close()


def test_value_pill_and_numbers_span_the_rule_and_its_lines(browser, live_server):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        def pill(*definitions):
            return run(page, 'return m.limitPill(arg.flatMap(d => m.allLimits(d)));', list(definitions))
        assert pill({'rule': {'kind': 'minimum_cover', 'minimum': 2}}) == '≥ 2'
        # Lines with other numbers: the range across the rule and its lines, never just the rule's own number.
        assert pill({'rule': {'kind': 'minimum_cover', 'minimum': 1}, 'per_person': [{'person_id': 'p-a', 'amount': 2}]}) == '≥ 1–2'
        assert pill({'rule': {'kind': 'maximum_participants', 'maximum': 0}, 'per_person': [{'category_ids': ['c-r1'], 'amount': 3}, {'category_ids': ['c-r2'], 'amount': 2}]}) == '≤ 0–3'
        # A rule that applies only through its lines, each with its own number: just theirs.
        assert pill({'rule': {'kind': 'max_duties', 'maximum': 5}, 'listed_only': True, 'per_person': [{'programme_id': 'x', 'amount': 4}]}) == '≤ 4 duties'
        assert pill({'rule': {'kind': 'max_hours', 'hours': 6}}) == '≤ 6 h'
        assert pill({'rule': {'kind': 'min_rest', 'hours': 11}}) == '≥ 11 h'
        assert pill({'rule': {'kind': 'max_consecutive_days', 'maximum': 6}}) == '≤ 6 days'
        quota = {'kind': 'block_quota', 'metric': 'duties', 'window_alignment': 'calendar_week'}
        assert pill({'rule': {**quota, 'comparison': 'range', 'amount': 2, 'maximum_amount': 6, 'target_amount': 4}}) == '2 – 6 → 4 duties'
        assert pill({'rule': {**quota, 'comparison': 'minimum', 'amount': 2, 'target_amount': 3}}) == '≥ 2 → 3 duties'
        assert pill({'rule': {**quota, 'comparison': 'maximum', 'amount': 6, 'target_amount': 4}}) == '≤ 6 → 4 duties'
        assert pill({'rule': {**quota, 'comparison': 'target', 'amount': 3}}) == '→ 3 duties'
        assert pill({'rule': {**quota, 'comparison': 'target', 'amount': 1}, 'per_person': [{'person_id': 'p-a', 'amount': 2}]}) == '→ 1–2 duties'
        assert pill({'rule': {'kind': 'run_length', 'minimum': 2}}) == ''
        # Copies of one rule (an older family) read as one range too.
        assert pill({'rule': {'kind': 'max_duties', 'maximum': 4}}, {'rule': {'kind': 'max_duties', 'maximum': 6}}) == '≤ 4–6 duties'
        # The sheet's numbers: a rule whose own maximum is 0 and whose lines carry up to 3 shows 0–3, varying by line.
        numbers = run(page, """const b = m.numbersBlock(m.allLimits(arg), 'per session');
          return {bar: b.querySelector('.rl-range')?.getAttribute('aria-label') ?? null, ticks: [...b.querySelectorAll('.rl-tick-label')].map(t => t.textContent),
                  cards: [...b.querySelectorAll('.rl-num')].map(c => c.textContent)};""",
                      {'rule': {'kind': 'maximum_participants', 'maximum': 0}, 'per_person': [{'category_ids': ['c-r1'], 'amount': 3}]})
        assert numbers == {'bar': 'No minimum, maximum 0 to 3, no target', 'ticks': ['3'],
                           'cards': ['Minimum—no lower limit', 'Maximum0–3people per session · varies by line', 'Target—no target']}
        # Nothing meaningful to draw (every number 0): no bar, only the cards.
        empty = run(page, "return m.numbersBlock(m.allLimits(arg), 'per session').querySelector('.rl-range') === null;",
                    {'rule': {'kind': 'maximum_participants', 'maximum': 0}})
        assert empty
    finally:
        context.close()
