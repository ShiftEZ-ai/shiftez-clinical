from packages.domain.solver_rules import COMPILER_VERSION
from apps.api.services.jobs import application_recheckable


def _job(**updates):
    snapshot = {'generation_engine': 'cp_sat', 'compiler_version': COMPILER_VERSION,
                'generation_scope': 'all', 'generation_scope_source': 'source'}
    result = {'algorithm_version': COMPILER_VERSION,
              'generation_scope': 'all', 'generation_scope_source': 'source'}
    snapshot.update(updates.pop('snapshot', {}))
    result.update(updates.pop('result', {}))
    return snapshot, result


def test_apply_recheck_decision_is_shared_for_input_and_scheduler_staleness():
    assert application_recheckable(*_job(), 'source')
    assert application_recheckable(*_job(snapshot={'generation_scope_source': 'new-source'}), 'source') is False
    assert application_recheckable(*_job(result={'algorithm_version': 'old'}), 'source') is False


def test_read_metadata_projection_decides_recheck_like_the_frozen_input():
    from apps.api.serialization import job_read_metadata
    from packages.domain.input_manifest import digest, toolchain
    for snapshot, result in (_job(), _job(result={'algorithm_version': 'old'}),
                             _job(snapshot={'quota_policy_limit': 7, 'input_manifest': {
                                 'components': {'toolchain': digest(toolchain(7))}}}),
                             _job(snapshot={'quota_policy_limit': 7, 'input_manifest': {
                                 'components': {'toolchain': digest(toolchain(8))}}})):
        projection = job_read_metadata(snapshot, result)
        assert (application_recheckable(projection['input_snapshot'], projection['result'], 'source')
                is application_recheckable(snapshot, result, 'source'))
