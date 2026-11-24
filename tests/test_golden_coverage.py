"""Executed golden assertions, their binding counts, and the remaining atlas gaps."""
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
import subprocess
from typing import get_args

import pytest

from packages.contracts import schemas as s
from packages.domain.solver_rules import SUPPORTED_RULES

ROOT = Path(__file__).resolve().parents[1]
GAPS_PATH = Path('tests/scenarios/golden/v1/known-gaps.json')
ORIGINAL_GAPS_BLOB = '6e393706e88aae648156135164de114f4573deb3'
ORIGINAL_UNRECORDED_GAPS = (
    'kind.balance_goal.comparison',
    'kind.balance_goal.period_months',
    'kind.spread_evenly',
    'kind.spread_evenly.holder',
    'kind.spread_evenly.maximum_gap',
    'kind.spread_evenly.mode',
    'kind.spread_evenly.within',
    'kind.team_lead_requirement.count_scope',
)


def coverage_inventory():
    models = get_args(get_args(s.RulePrimitive)[0])
    kinds = {get_args(model.model_fields['kind'].annotation)[0]: model for model in models}
    inventory = {'kind.' + kind for kind in set(kinds) | SUPPORTED_RULES}
    inventory.update('kind.' + kind + '.' + field
                     for kind, model in kinds.items() for field in model.model_fields if field != 'kind')
    inventory.update('dimension.' + field
                     for field in s.SCOPE_FIELDS | (s.Scope.model_fields.keys() - {'conditions'}))
    inventory.update('condition.' + field for field in get_args(s.ConditionField))
    return inventory


def coverage_report(scenarios, receipts):
    inventory, rows, bindings = coverage_inventory(), [], []
    for spec in scenarios:
        sid = spec['id']
        receipt = receipts[sid]
        assert spec['covers'], (sid, 'scenario has no coverage assertions')
        for item, assertions in spec['covers'].items():
            assert item in inventory, (sid, 'unknown coverage item', item)
            assert assertions and not isinstance(assertions, str), (sid, item, 'name assertion IDs as a list')
            for assertion in assertions:
                assert assertion in spec['expect'], (sid, item, 'missing hand-written expectation', assertion)
                assert assertion in receipt['assertions'], (sid, item, 'assertion did not execute', assertion)
                rows.append({'item': item, 'scenario': sid, 'assertion': assertion})
        assert spec['expect']['binding_rules'], (sid, 'scenario has no focal rule')
        for rule in spec['expect']['binding_rules']:
            count = receipt['bindings'].get(rule, 0)
            assert isinstance(count, int) and not isinstance(count, bool) and count > 0, (
                sid, rule, 'scenario not sensitive: binding count must be above 0')
            bindings.append({'scenario': sid, 'rule': rule, 'count': count})
    covered = {row['item'] for row in rows}
    return {'coverage': rows, 'bindings': bindings, 'gaps': sorted(inventory - covered)}


def _git(root, *args):
    return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True,
                          text=True, encoding='utf-8').stdout.strip()


def _default_remote_ref(root):
    if 'GITHUB_EVENT_PATH' in os.environ:
        spec = spec_from_file_location('pytest_ci', ROOT / '.github/scripts/pytest_ci.py')
        wrapper = module_from_spec(spec)
        spec.loader.exec_module(wrapper)
        ref = wrapper.default_ref()
        if ref is None:
            raise ValueError('The GitHub event must name the repository default branch')
        return 'refs/remotes/origin/' + ref.removeprefix('refs/heads/')
    try:
        return _git(root, 'symbolic-ref', '--quiet', 'refs/remotes/origin/HEAD')
    except subprocess.CalledProcessError as error:
        error.add_note('Set the local default branch with git remote set-head origin --auto after fetching.')
        raise


def baseline_known_gaps(root=ROOT):
    base = _git(root, 'merge-base', 'HEAD', _default_remote_ref(root))
    entry = _git(root, 'ls-tree', base, '--', GAPS_PATH.as_posix())
    if not entry:
        # The first reviewed introduction establishes the baseline; later PRs may only shrink it.
        return None
    known = json.loads(_git(root, 'show', base + ':' + GAPS_PATH.as_posix()))['gaps']
    # shortcut: adjust only the original gap file until the corrected list lands.
    if entry.split()[2] == ORIGINAL_GAPS_BLOB:
        known = sorted([*known, *ORIGINAL_UNRECORDED_GAPS])
    return known


def enforce_gap_ratchet(report, known, baseline):
    assert known == sorted(set(known)), 'Known golden gaps must be unique and sorted'
    if baseline is not None:
        assert set(known) <= set(baseline), ('Known golden gap list grew', sorted(set(known) - set(baseline)))
    assert known == report['gaps'], ('Known golden gaps must equal current gaps',
                                     sorted(set(known) ^ set(report['gaps'])))


def test_golden_coverage(golden_receipts, record_property):
    from scripts.golden_scenarios import load_pack

    report = coverage_report(load_pack(), golden_receipts)
    known = json.loads((ROOT / GAPS_PATH).read_text(encoding='utf-8'))['gaps']
    baseline = baseline_known_gaps()
    enforce_gap_ratchet(report, known, baseline)
    record_property('golden_coverage', json.dumps(report, sort_keys=True))
    print('\nGolden coverage: kind/field/dimension -> scenario -> executed assertion')
    for row in report['coverage']:
        print(f"{row['item']} -> {row['scenario']} -> {row['assertion']}")
    print('Binding counts (observed counterfactual differences):')
    for row in report['bindings']:
        print(f"{row['scenario']}/{row['rule']}: {row['count']}")
    print(f"Remaining known gaps ({len(report['gaps'])}, ratcheted against known-gaps.json):")
    for gap in report['gaps']:
        print(gap)
    if baseline is None:
        print('First introduction: this reviewed known-gap list establishes the baseline.')
    # TODO: enforce an empty gap set once the full atlas and interaction pack are reviewed.


def test_coverage_rejects_unexecuted_assertions_and_zero_bindings():
    spec = {'id': 'example', 'covers': {'kind.min_rest.hours': ['boundary']},
            'expect': {'boundary': {'limit': 11}, 'binding_rules': ['rest']}}
    receipt = {'assertions': {'boundary'}, 'bindings': {'rest': 1}}
    assert coverage_report([spec], {'example': receipt})['bindings'][0]['count'] == 1
    with pytest.raises(AssertionError, match='assertion did not execute'):
        coverage_report([spec], {'example': {**receipt, 'assertions': set()}})
    with pytest.raises(AssertionError, match='scenario not sensitive'):
        coverage_report([spec], {'example': {**receipt, 'bindings': {'rest': 0}}})


@pytest.mark.parametrize('new_item', ['kind.new_rule', 'kind.min_rest.new_field'])
def test_gap_ratchet_rejects_new_uncovered_contract_items(monkeypatch, new_item):
    known = sorted(coverage_inventory())
    monkeypatch.setattr(__name__ + '.coverage_inventory', lambda: set(known) | {new_item})
    report = coverage_report([], {})
    with pytest.raises(AssertionError, match='Known golden gaps must equal current gaps'):
        enforce_gap_ratchet(report, known, known)


@pytest.mark.parametrize('baseline', [None, ['kind.min_rest.hours']])
def test_gap_ratchet_requires_removing_newly_covered_gaps(baseline):
    with pytest.raises(AssertionError, match='Known golden gaps must equal current gaps'):
        enforce_gap_ratchet({'gaps': []}, ['kind.min_rest.hours'], baseline)


def test_gap_ratchet_baseline_bootstrap_and_no_expansion(tmp_path, monkeypatch):
    monkeypatch.delenv('GITHUB_EVENT_PATH', raising=False)
    default = 'primary'
    remote_ref = f'refs/remotes/origin/{default}'
    _git(tmp_path, 'init', '--quiet')
    _git(tmp_path, 'config', 'user.name', 'Golden baseline test')
    _git(tmp_path, 'config', 'user.email', 'golden@example.invalid')
    _git(tmp_path, 'config', 'commit.gpgsign', 'false')
    _git(tmp_path, 'config', 'core.hooksPath', str(tmp_path / 'no-hooks'))
    _git(tmp_path, 'commit', '--quiet', '--allow-empty', '-m', 'Before golden baseline')
    with pytest.raises(subprocess.CalledProcessError, match='git remote set-head origin --auto'):
        baseline_known_gaps(tmp_path)
    _git(tmp_path, 'update-ref', remote_ref, 'HEAD')
    _git(tmp_path, 'symbolic-ref', 'refs/remotes/origin/HEAD', remote_ref)
    assert baseline_known_gaps(tmp_path) is None
    known = ['kind.min_rest.hours']
    enforce_gap_ratchet({'gaps': known}, known, None)
    path = tmp_path / GAPS_PATH
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'gaps': known}), encoding='utf-8')
    _git(tmp_path, 'add', GAPS_PATH.as_posix())
    _git(tmp_path, 'commit', '--quiet', '-m', 'Reviewed initial golden gaps')
    _git(tmp_path, 'update-ref', remote_ref, 'HEAD')
    baseline = baseline_known_gaps(tmp_path)
    assert baseline == known
    event = tmp_path / 'event.json'
    event.write_text(json.dumps({'repository': {'default_branch': default}}), encoding='utf-8')
    monkeypatch.setenv('GITHUB_EVENT_PATH', str(event))
    _git(tmp_path, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/another')
    assert baseline_known_gaps(tmp_path) == known
    enforce_gap_ratchet({'gaps': []}, [], baseline)
    grown = sorted(known + ['kind.new_rule'])
    with pytest.raises(AssertionError, match='Known golden gap list grew'):
        enforce_gap_ratchet({'gaps': grown}, grown, baseline)


def test_gap_ratchet_git_failure_is_not_baseline_bootstrap(tmp_path, monkeypatch):
    monkeypatch.delenv('GITHUB_EVENT_PATH', raising=False)
    monkeypatch.setenv('GIT_CEILING_DIRECTORIES', str(tmp_path.parent))
    with pytest.raises(subprocess.CalledProcessError):
        baseline_known_gaps(tmp_path)
