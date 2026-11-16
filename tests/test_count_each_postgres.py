"""Count definitions and frozen per-bucket evidence on the disposable PostgreSQL backend."""
import pytest

from . import test_condition_count_contracts as admission
from .test_rule_conditions import line, tree

pytestmark = pytest.mark.postgres


def test_postgres_condition_admission_and_legacy_storage(pg):
    admission.test_policy_default_count_storage_is_byte_equivalent_to_old_tree(pg)
    admission.test_has_all_of_person_tags_and_staffing_each_are_admitted_by_api(pg)
    for mode in ('location_all', 'person_all', 'person_each', 'person_group_each', 'none_count'):
        admission.test_invalid_condition_count_and_has_all_of_return_422(pg, mode)


def test_postgres_publication_keeps_each_quota_buckets_and_frozen_counts(pg):
    person = pg.seed['people'][0]
    clinics = pg.seed['services']
    scope = {'person_id': person, 'conditions': tree({**line('service_ids', *clinics), 'count': 'each'})}
    policy = pg.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
                        'metric': 'duties', 'comparison': 'maximum', 'amount': 6}, scope)
    workspace = pg.shift(pg.workspace(), people=[person])['workspace']
    workspace = pg.shift(workspace, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00',
                         people=[person], service=1)['workspace']
    issued = pg.publish(workspace)
    path = '/api/reports/' + issued['publication_id']
    report = pg.get('coordinator', path)['quotas']
    rows = [r for r in report['rows'] if r['policy_id'] == policy['id']]
    assert len(rows) == 2 and {r['actual'] for r in rows} == {1}
    assert {r['count_bucket'][-1][1] for r in rows} == set(clinics)
    pg.policy({'kind': 'max_duties', 'maximum': 1}, {'person_id': person})
    assert pg.get('coordinator', path)['quotas'] == report
    personal = pg.get('staff', path)['quotas']
    assert {r['person_id'] for r in personal['rows']} == {person}
    assert {r['count_bucket'][-1][1] for r in personal['rows']} == set(clinics)
