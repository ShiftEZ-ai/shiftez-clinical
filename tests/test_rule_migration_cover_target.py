"""Advisory cover uses the existing target slot without changing checks or losing its score breach."""
import copy
import uuid

import pytest

from packages.domain.conditions import active_rules
from packages.domain.rule_catalogue_migration import id_map, plan
from packages.domain.rule_groups import expanded
from packages.domain.rule_lines import duplicates, outranking
from packages.domain.score import schedule_score
from packages.domain.validation import evaluate

from .test_rule_simplification import WEEK_START, rule
from .test_rule_templates import activate, propose


@pytest.mark.parametrize('ranked', [False, True])
def test_advisory_cover_joins_existing_hard_cover_as_target_without_changing_checks(ranked):
    scope = {'category_ids': [str(uuid.uuid4())]} if ranked else {}
    hard = rule('Required staffing', {'kind': 'minimum_cover', 'minimum': 1}, scope, exception_allowed=False)
    [first], report = plan([hard], {}, WEEK_START, 'd')
    assert report == []
    # Safety kinds are always mandatory. An authored target still joins a converted hard minimum.
    existing = copy.deepcopy(first['row'])
    existing['id'] = str(uuid.uuid4())
    target = existing['definition']['rule']['lines'][0]
    target.update(id=str(uuid.uuid4()), numbers={'target': {'value': 3, 'level': 'high'}})
    before = [existing, hard]
    proposals, report = plan(before, {}, WEEK_START, 'd')
    assert report == []
    [merged] = proposals
    assert merged['catalogue_id'] == ('kind_per_session' if ranked else 'people_per_session')
    assert merged['replaces_id'] == existing['id']
    assert merged['rule_ids'] == [hard['id']]
    catalogue = merged['payload']['definition']['rule']
    numbers = [line['numbers'] for line in catalogue['lines']]
    assert {'min': {'value': 1, 'level': 'mandatory'}} in numbers
    assert {'target': {'value': 3, 'level': 'high'}} in numbers
    assert not duplicates(catalogue) and not outranking(merged['row'])
    assert expanded(before, id_map(proposals), WEEK_START) == expanded([merged['row']], None, WEEK_START)
    copies = active_rules([merged['row']])
    assert {(p['definition']['severity'], p['definition']['strength'], p['definition']['rule']['minimum'],
             p['definition']['exception_allowed']) for p in copies} == {
                 ('mandatory', 'medium', 1, False), ('advisory', 'high', 3, False)}


def test_cover_migration_does_not_silently_discard_an_historical_exception_flag():
    hard = rule('Required staffing', {'kind': 'minimum_cover', 'minimum': 1}, exception_allowed=False)
    [first], report = plan([hard], {}, WEEK_START, 'd')
    assert report == []
    existing = {**first['row'], 'id': str(uuid.uuid4())}
    # Historical-read compatibility is not permission to admit new safety-rule exceptions.
    preferred = rule('Historical staffing preference', {'kind': 'minimum_cover', 'minimum': 3},
                     severity='advisory', exception_allowed=True)
    proposals, report = plan([existing, preferred], {}, WEEK_START, 'd')
    assert not proposals
    assert {row['rule_id'] for row in report} == {preferred['id']}


def test_cover_target_migration_api_proof_preserves_warning_and_advisory_score(h):
    activate(h, propose(h, {'kind': 'minimum_cover', 'minimum': 1},
                        {'category_ids': [h.seed['category']]}, name='Required staffing'))
    # Safety kinds are always mandatory. Keep the explicitly advisory target through conversion.
    preferred = h.cover_target(3, strength='high')
    w = h.shift(h.workspace('Cover target migration'))['workspace']
    before = h.snapshot(w)
    active_before = {p['id']: p for p in h.get('approver', '/api/policies?status=active')['policies']}
    before_check = evaluate(before)
    assert any(f['rule_id'] == preferred['id'] and f['severity'] == 'warning' and
               f['code'] == 'SCOPED_COVERAGE_GAP' for f in before_check['findings'])
    preview = h.get('coordinator', '/api/rules/catalogue/migration?proof=true')
    assert preview['moving'] == 1 and preview['kept'] == 0 and preview['report'] == []
    assert preview['proof']['identical'], preview['proof']
    assert preview['proof']['rotas'] and all(r['model_equal'] and r['quota_equal'] and r['solver_inputs_equal']
                                          for r in preview['proof']['rotas'])
    proposed = preview['proposals']
    moved = id_map(proposed)
    after = copy.deepcopy(before)
    after['policies'] = [p for p in after['policies'] if p['id'] not in moved] + [p['row'] for p in proposed]
    after_check = evaluate(after)
    assert any(f['rule_id'] == moved.get(preferred['id'], preferred['id']) and f['severity'] == 'warning' and
               f['code'] == 'SCOPED_COVERAGE_GAP' for f in after_check['findings'])
    old_score = schedule_score(before, before_check)['components']['rules']
    new_score = schedule_score(after, after_check)['components']['rules']
    assert old_score['value'] < 1 and old_score['lost'] > 0
    assert new_score['value'] == pytest.approx(old_score['value'])
    assert new_score['lost'] > 0  # a mandatory sibling cannot make the target warning disappear from the score
    # Proof stays read-only; no policy proposal, activation or retirement occurred.
    active = h.get('approver', '/api/policies?status=active')['policies']
    assert {p['id']: p for p in active} == active_before
