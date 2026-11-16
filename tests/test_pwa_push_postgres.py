"""Production-dialect migration, consent concurrency, leasing and policy regression coverage."""
import pytest
from tests import test_pwa_push as cases
from tests.test_pwa_push import ready

pytestmark = [pytest.mark.postgres, pytest.mark.parametrize('h', ['postgresql'], indirect=True)]


def test_postgres_subscription_consent_ownership_and_logout(ready):
    cases.test_subscription_consent_ownership_refresh_rotation_redaction_and_logout(ready)


def test_postgres_policy_history_immutable_and_undo(ready):
    cases.test_policy_versions_conflicts_undo_immutability_and_email_projection(ready)


def test_postgres_lease_recovery_fencing_and_retry_expiry(ready):
    cases.test_jwt_reuse_lease_fencing_ttl_and_undo_cancel(ready)


def test_postgres_concurrent_subscription_and_command_security(ready):
    cases.test_no_auth_csrf_cross_origin_and_installation_races(ready)


def test_postgres_send_current_privacy_and_access(ready, monkeypatch):
    cases.test_send_reprojects_privacy_and_uses_no_database_transaction(ready, monkeypatch)


@pytest.mark.parametrize('status', [201, 404, 410, 429, 500, 403, 413])
def test_postgres_outbox_results_leave_inbox_authoritative(ready, status):
    cases.test_outbox_acceptance_expiry_retries_and_pause(ready, status)


def test_postgres_department_concurrency_includes_outstanding_leases(ready):
    cases.test_department_concurrency_covers_outstanding_worker_leases(ready)
