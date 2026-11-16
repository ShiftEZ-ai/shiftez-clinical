"""The rules-cleanup census is deterministic source evidence, never a write plan."""
import copy

from packages.domain.rule_catalogue_migration import after_snapshot, census, plan_v2


DEPARTMENT = '00000000-0000-0000-0000-000000000001'
PERSON = '00000000-0000-0000-0000-000000000010'


def legacy_cap(*, status='active'):
    return {'id': '00000000-0000-0000-0000-000000000100', 'kind': 'max_duties', 'name': 'Legacy cap',
            'status': status, 'active': status == 'active', 'revision': 3, 'owner': 'Synthetic department',
            'source': 'Synthetic source', 'starts_on': '2026-01-01', 'ends_on': None,
            'definition': {'rule': {'kind': 'max_duties', 'maximum': 5, 'window': 'week'},
                           'scope': {'person_id': PERSON}, 'per_person': [], 'severity': 'mandatory'}}


def test_census_is_deterministic_and_binds_source_and_line_evidence():
    source = legacy_cap()
    refs = {source['id']: {'definition_history_ids': ['history-3'], 'audit_event_ids': ['audit-7'],
                           'rule_source_binding_ids': ['binding-2'], 'migration_proposal_ids': ['conversion-1']}}
    first = census([source], audit_refs=refs)
    second = census([copy.deepcopy(source)], audit_refs=refs)
    assert first == second
    assert first['hash']
    assert first['sources'][0]['source_policy_id'] == source['id']
    assert first['sources'][0]['source_hash']
    assert first['lines'][0]['source_line_id'] in first['sources'][0]['line_ids']
    assert first['lines'][0]['line_hash']
    assert first['lines'][0]['audit_refs'] == refs[source['id']]
    assert first['lines'][0]['scope']['person_ids'] == [PERSON]
    assert first['lines'][0]['strengths'] == {'max': 'mandatory'}


def test_census_keeps_active_and_proposed_rows_and_retains_superseded_history():
    active = legacy_cap()
    proposed = {**legacy_cap(status='proposed'), 'id': '00000000-0000-0000-0000-000000000101', 'revision': 4}
    retired = {**legacy_cap(status='retired'), 'id': '00000000-0000-0000-0000-000000000102', 'active': False}
    result = census([retired, proposed, active], audit_refs={retired['id']: {'rule_source_binding_ids': ['binding-1']}})
    assert [row['source_policy_id'] for row in result['sources']] == [active['id'], proposed['id']]
    assert result['counts'] == {'sources': 2, 'lines': 2, 'history_sources': 1, 'active_sources': 1,
                                'proposed_sources': 1, 'superseded_history': 1}
    assert result['history'][0]['source_policy_id'] == retired['id']
    assert result['history'][0]['classification'] == 'superseded'


def test_census_uses_shared_duplicate_authority_and_preserves_mandatory_safety():
    line = {'id': 'line-1', 'numbers': {'max': {'value': 5, 'level': 'mandatory'}},
            'settings': {'max_duties': {'window': 'week'}}}
    duplicate = {**line, 'id': 'line-2'}
    source = {'id': '00000000-0000-0000-0000-000000000104', 'kind': 'catalogue', 'name': 'Duplicate lines',
              'status': 'active', 'active': True, 'revision': 1, 'owner': 'Synthetic department',
              'source': 'Synthetic source', 'starts_on': '2026-01-01', 'ends_on': None,
              'definition': {'rule': {'kind': 'catalogue', 'id': 'workload_limit', 'catalogue_version': 2,
                                      'lines': [line, duplicate]}}}
    duplicate_result = census([source])
    assert {row['classification'] for row in duplicate_result['lines']} == {'duplicate_or_overlap'}
    assert all(row['duplicate'] for row in duplicate_result['lines'])

    safety = {**legacy_cap(), 'id': '00000000-0000-0000-0000-000000000105', 'kind': 'restriction',
              'name': 'Keep off', 'definition': {'rule': {'kind': 'restriction'}, 'scope': {'person_id': PERSON},
                                                  'per_person': [], 'severity': 'mandatory'}}
    safety_result = census([safety])
    assert safety_result['lines'][0]['mandatory_safety']


def test_mandatory_safety_is_recorded_and_survives_conversion_projection():
    source = {**legacy_cap(), 'id': '00000000-0000-0000-0000-000000000103', 'name': 'Keep off'}
    source['kind'] = 'restriction'
    source['definition'] = {'rule': {'kind': 'restriction'}, 'scope': {'person_id': PERSON},
                            'per_person': [], 'severity': 'mandatory'}
    result = census([source])
    assert result['lines'][0]['mandatory_safety']
    proposal = plan_v2([source], department_id=DEPARTMENT)
    assert not proposal['blocked'], proposal['blocked']
    line = proposal['targets'][0]['payload']['definition']['rule']['lines'][0]
    assert line['numbers']['apply']['level'] == 'mandatory'
    assert line['source_bindings']['apply']['policy_id'] == source['id']


def test_conversion_projection_preserves_source_ids_and_does_not_delete_input_rows():
    source = legacy_cap()
    before = {'policies': [copy.deepcopy(source)], 'scheduling_facts': [], 'source_bindings': []}
    original = copy.deepcopy(before)
    proposal = plan_v2(before['policies'], department_id=DEPARTMENT)
    assert not proposal['blocked'], proposal['blocked']
    after = after_snapshot(before, proposal)
    assert before == original
    assert proposal['bindings'][0]['source_policy_id'] == source['id']
    assert proposal['bindings'][0]['source_revision'] == source['revision']
    assert source['id'] not in {row['id'] for row in after['policies']}
    assert after['source_bindings'][0]['source_policy_id'] == source['id']
