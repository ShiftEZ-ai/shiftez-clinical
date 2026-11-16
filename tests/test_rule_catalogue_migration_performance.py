"""Migration resolves immutable conflicts once, then proves the surviving candidate unchanged."""
from uuid import uuid4

import pytest

from packages.domain import rule_catalogue_migration as migration
from .test_rule_catalogue_migration import same
from .test_rule_simplification import WEEK_START, rule


@pytest.mark.parametrize('people', [163, 652])
def test_catalogue_migration_resolves_department_conflicts_once(monkeypatch, people):
    # The audit's active-rule shape: a department cap and one more specific cap per person.
    broad = rule('Department hours', {'kind': 'max_hours', 'hours': 168})
    personal = [rule(f'Person {n:04} hours', {'kind': 'max_hours', 'hours': 168}, {'person_id': str(uuid4())})
                for n in range(people)]
    rules, resolved = [broad, *personal], []
    original = migration.active_rules

    def active(policies):
        copies = original(policies)
        resolved.append(len(copies))
        return copies

    monkeypatch.setattr(migration, 'active_rules', active)
    proposals, report = migration.plan(rules, {}, WEEK_START, 'd')
    assert [proposal['rule_ids'] for proposal in proposals] == [[broad['id']]]
    assert {row['rule_id'] for row in report} == {p['id'] for p in personal}
    assert all('“Department hours”' in row['reason'] and 'unresolved outranking' in row['reason'] for row in report)
    assert same(rules, proposals)
    # Deterministic CI budget: resolve the original candidate and the final candidate, never every removed rule.
    assert len(resolved) <= 2 and sum(resolved) <= people + 2, resolved


def test_catalogue_migration_prunes_duplicate_edges_before_precedence():
    broad = rule('A department hours', {'kind': 'max_hours', 'hours': 168})
    duplicate = rule('B duplicate department hours', {'kind': 'max_hours', 'hours': 160})
    person = str(uuid4())
    personal = rule('C personal hours', {'kind': 'max_hours', 'hours': 80}, {'person_id': person})
    twice = rule('D duplicate personal hours', {'kind': 'max_hours', 'hours': 60}, {'person_id': person})
    rules = [broad, duplicate, personal, twice]
    proposals, report = migration.plan(rules, {}, WEEK_START, 'd')
    assert [proposal['rule_ids'] for proposal in proposals] == [[broad['id']]]
    reasons = {row['name']: row['reason'] for row in report}
    assert 'same line as one of “A department hours”' in reasons[duplicate['name']]
    assert 'same line as one of “C personal hours”' in reasons[twice['name']]
    assert 'replace a line of “A department hours”' in reasons[personal['name']]
    assert same(rules, proposals)


def test_catalogue_migration_keeps_precedence_removal_order():
    category = rule('A category hours', {'kind': 'max_hours', 'hours': 120}, {'category_id': str(uuid4())})
    personal = rule('B personal hours', {'kind': 'max_hours', 'hours': 80}, {'person_id': str(uuid4())})
    broad = rule('Z department hours', {'kind': 'max_hours', 'hours': 168})
    rules = [category, personal, broad]
    proposals, report = migration.plan(rules, {}, WEEK_START, 'd')
    assert [proposal['rule_ids'] for proposal in proposals] == [[broad['id']]]
    reasons = {row['name']: row['reason'] for row in report}
    assert 'replace a line of “A category hours”' in reasons[personal['name']]
    assert 'replace a line of “Z department hours”' in reasons[category['name']]
    assert same(rules, proposals)


def test_catalogue_migration_retains_own_lines_when_their_cross_conflict_is_removed():
    first, second, category = str(uuid4()), str(uuid4()), str(uuid4())
    own = rule('A cap with own line', {'kind': 'max_hours', 'hours': 168},
               per_person=[{'person_id': first, 'amount': 80}])
    middle = rule('B category hours', {'kind': 'max_hours', 'hours': 120}, {'category_id': category})
    personal = rule('C personal hours', {'kind': 'max_hours', 'hours': 60}, {'person_id': second})
    duplicate = rule('D duplicate category hours', {'kind': 'max_hours', 'hours': 100}, {'category_id': category})
    rules = [own, middle, personal, duplicate]
    proposals, report = migration.plan(rules, {}, WEEK_START, 'd')
    assert [proposal['rule_ids'] for proposal in proposals] == [[own['id']]]
    assert len(proposals[0]['payload']['definition']['rule']['lines']) == 2
    reasons = {row['name']: row['reason'] for row in report}
    assert 'same line as one of “B category hours”' in reasons[duplicate['name']]
    assert all('replace a line of “A cap with own line”' in reasons[p['name']] for p in (middle, personal))
    assert same(rules, proposals)
