"""The indexed preview may cache dates only while picks change seats, not their dated inputs."""
from datetime import date
from random import Random

import pytest

from apps.api.services import staffing
from apps.api.services.issues import best_replacements
from packages.domain import conditions, fairness
from packages.domain.time import aware
from packages.domain.validation import PERSON_ROWS
from tests.scheduler_departments import department
from tests.test_replace_ranking import reference_replacements, staffed, with_person_rules


class DatedInputs(dict):
    """Fail if date calculations start reading mutable seats or a candidate's filtered rule pool."""
    fields = {*PERSON_ROWS, 'organisation', 'workspace', 'cohorts', 'categories', 'capabilities', 'programmes', 'rotation_types'}

    def __getitem__(self, key):
        assert key in self.fields, f'Date cache depends on a reduced or mutable field: {key}'
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key == '_person_rows':  # Compare uncached calculations from the guarded complete rows.
            return None
        assert key in self.fields, f'Date cache depends on a reduced or mutable field: {key}'
        return super().get(key, default)


def test_preview_date_cache_reads_only_complete_immutable_dated_inputs(h, monkeypatch):
    data, _ = department(h, 91, people=12, days=4, rules='full', leave_rate=.1)
    data = with_person_rules(staffed(data, Random(9)), Random(10))
    targets = {(s['id'], p['person_id']): ['test'] for s in data['shifts'][:4] for p in s['participants']}
    expected = reference_replacements(data, targets, set(data['people']))
    original = staffing.evaluate
    checked = []
    baselines = []

    def check_complete(view, **kwargs):
        if 'person_rule_subjects' not in view:
            baselines.append(view.get('_staffing_baseline'))
            return original(view, **kwargs)
        for pid in view['person_rule_subjects']:
            for name in PERSON_ROWS:
                assert [r for r in view.get(name, []) if r['person_id'] == pid] == [
                    r for r in data.get(name, []) if r['person_id'] == pid], (name, pid)
            static = DatedInputs(view)
            a, b = aware(data['shifts'][0]['start']), aware(data['shifts'][-1]['end'])
            assert conditions.change_points(view, pid, a, b) == [
                a, *(point for point in conditions._change_points(static, pid) if a < point < b)]
            first, last = date(2026, 10, 4), date(2026, 10, 8)
            assert fairness.member_days(view, pid, first, last) == fairness._member_days(static, pid, first, last)
            checked.append(pid)
        return original(view, **kwargs)

    monkeypatch.setattr(staffing, 'evaluate', check_complete)
    assert best_replacements(data, targets, set(data['people'])) == expected
    assert checked and baselines


@pytest.mark.parametrize('change', ['missing', 'interval'])
def test_indexed_preview_refuses_a_seat_changed_outside_move_seat(h, change):
    data, _ = department(h, 92, people=6, days=2, rules='none', leave_rate=0)
    data = staffed(data, Random(3))
    shift = data['shifts'][0]
    seat = shift['participants'][0]
    index = staffing.people_index(data)
    if change == 'missing':
        index['duties'][seat['person_id']] = []
    else:
        seat['end'] = shift['start']
    before = dict(seat)
    with pytest.raises(AssertionError, match='indexed seat'):
        staffing.move_seat(index, shift, seat, h.seed['people'][1])
    assert seat == before
