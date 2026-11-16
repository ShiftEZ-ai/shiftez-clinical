"""Real PostgreSQL variants of canonical semantic cases; no SQLite substitution."""
import pytest
from . import test_published_operational as published
from . import test_manual_supervision_staffing as manual
from . import test_planning_context as context
from . import test_series_cancel_targets as cancellation

pytestmark = pytest.mark.postgres


def test_published_absence_preserves_evidence_and_role_privacy(pg):
    published.test_all_roles_only_receive_authorized_warnings_and_actions(pg)


def test_published_read_projection_preserves_issued_facts(pg):
    published.test_publish_then_approved_absence_has_one_current_task_and_frozen_evidence(pg, 'pattern')


def test_manual_full_roster_checked_write(pg):
    manual.test_missing_association_blocks_but_manual_plan_and_exact_write_pass(pg)


def test_context_read_projection_is_dated_incomplete(pg, monkeypatch):
    context.test_readiness_is_period_bound_incomplete_and_does_not_revalidate(pg, monkeypatch)


def test_amendment_target_matches_new_ids_after_roster_change(pg):
    published.test_amendment_target_ignores_changed_roster_and_keeps_issued_approval(pg)


def test_amendment_target_never_guesses_between_duplicate_demands(pg):
    published.test_duplicate_demands_are_ambiguous_beyond_home_page_and_person_filter(pg, 'both')


def test_changed_amendment_target_refreshes_source_tags(pg):
    published.test_changed_or_missing_amendment_demand_has_no_invented_target_and_refreshes_tags(pg, 'retime')


@pytest.mark.parametrize('kind', ['unavailable', 'external_rotation'])
def test_one_sided_cancellation_preserves_unrelated_duty_and_undo(pg, kind):
    cancellation.test_one_sided_conflict_cancels_only_its_duty_and_undo_preserves_other(pg, kind)
