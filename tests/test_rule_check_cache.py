"""Catalogue caches live only for immutable checks and share inherited timelines across source lines."""
import gc
import weakref
from zoneinfo import ZoneInfo

from packages.domain import conditions
from packages.domain.conditions import active_rules, change_points, source_index
from packages.domain.time import aware
from packages.domain.validation import index_person_rows, policy_applies
from .test_rule_conditions import data as condition_data
from .test_rule_dynamic_scale import dynamic_department


def test_check_indexes_are_reclaimed_between_checks():
    class Probe:
        pass

    data = dynamic_department()
    references = []
    for _ in range(4):
        probe = Probe()
        view = {**data, 'probe': probe, 'policies': active_rules(data['policies']),
                '_line_index_cache': {}, '_person_dates_cache': {}, '_person_rows': index_person_rows(data)}
        references.append(weakref.ref(probe))
        # A blank-strength person line reads the nearest rank line over its quota window.
        copy_rule = next(p for p in view['policies'] if p.get('template_line', {}).get('line') == 10)
        pid = next(iter(copy_rule['definition']['scope']['person_ids']))
        shift = next(s for s in data['shifts'] if s['start'].startswith('2026-10-02')
                     and s['service_ids'] == copy_rule['definition']['scope']['service_ids'])
        a, b = aware(shift['start']), aware(shift['end'])
        policy_applies(view, copy_rule, pid, shift, a, b)
        assert view['_line_index_cache'] and '_line_index_cache' not in data
        del copy_rule, view, probe
    gc.collect()
    assert all(ref() is None for ref in references)
    assert not hasattr(conditions, '_LINE_INDEX')


def test_source_descriptors_are_built_once_per_check_and_do_not_retain_snapshots(monkeypatch):
    from packages.domain import rule_lines

    data = dynamic_department()
    policies = data['policies']
    calls = []
    original = rule_lines.source_lines
    def counted(policy):
        calls.append(policy)
        return original(policy)
    monkeypatch.setattr(rule_lines, 'source_lines', counted)
    cache = {}
    first = source_index(policies, cache)
    assert source_index(policies, cache) is first
    assert calls == policies
    # New checks and in-place edits to the stored rule never reuse an earlier check's index.
    policies[0]['definition']['rule']['lines'][0]['numbers']['max']['level'] = 'critical'
    later = source_index(policies, {})
    assert later is not first and later[1] != first[1]


def test_change_points_ignore_unheld_definitions_but_keep_every_held_dimension():
    data = condition_data()
    data['_person_dates_cache'], data['_person_rows'] = {}, index_person_rows(data)
    for name in ('categories', 'capabilities', 'programmes', 'rotation_types', 'cohorts'):
        data[name]['unheld'] = {'starts_on': '2026-10-13', 'ends_on': '2026-10-14'}
    data['categories']['R2']['starts_on'] = '2026-10-12'
    data['capabilities']['S']['ends_on'] = '2026-10-15'
    data['programmes']['P']['ends_on'] = '2026-10-16'
    data['rotation_types']['RT']['ends_on'] = '2026-10-17'
    data['cohorts']['C']['ends_on'] = '2026-10-18'
    a, b = map(aware, ('2026-10-11T00:00:00+03:00', '2026-10-19T00:00:00+03:00'))
    points = change_points(data, 'p', a, b)
    assert [p.astimezone(ZoneInfo('Asia/Riyadh')).day for p in points] == [11, 12, 15, 16, 17, 18]
    assert change_points(data, 'q', a, b) == [a]


def test_person_rule_api_starts_a_fresh_check_after_a_person_fact_changes(h):
    from .test_rule_lines import line, person
    from .test_rule_lines_inherit import n
    from .test_rule_catalogue_api import approve, propose

    pid = h.seed['people'][0]
    mine = line(who=person(pid), numbers={'max': n(1, None)})
    lines = [
        line(numbers={'max': n(5)}),
        line(who={'conditions': [{'field': 'tags', 'values': ['priority']}]}, numbers={'max': n(5, 'high')}), mine]
    saved = approve(h, propose(h, 'duties_per_week', lines,
                    acknowledged=[lines[0]['id'], lines[1]['id']])['record'])
    endpoint = f'/api/policies/applies?person_id={pid}&day=2026-10-04'
    def strengths():
        return {row['own_line']['id']: row['severity'] for row in h.get('coordinator', endpoint)['rules']
                if row['policy_id'] == saved['id']}
    for _ in range(4):
        assert strengths() == {mine['id']: 'mandatory'}
    current = next(row for row in h.get('admin', '/api/catalogues')['people'] if row['id'] == pid)
    h.configure('people', {key: current[key] for key in ('display_name', 'active', 'title', 'short_code', 'colour')}
                | {'tags': ['priority']}, pid, current['revision'])
    # Proposal freezes the person's inherited strength; the newly matching tag line must still appear immediately.
    assert strengths() == {mine['id']: 'mandatory', lines[1]['id']: 'advisory'}
