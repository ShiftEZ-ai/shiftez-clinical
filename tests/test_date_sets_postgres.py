"""The same checked date-set journeys on real migrated PostgreSQL JSON storage."""
import pytest

from . import test_date_sets_api as journeys


@pytest.mark.postgres
@pytest.mark.parametrize('journey', [
    journeys.test_set_entry_crud_revisions_audit_and_undo,
    journeys.test_hijri_adjustment_only_changes_its_native_year_and_can_be_cleared,
    journeys.test_admin_can_preview_inactive_sets_without_activating_them,
    journeys.test_invalid_hijri_start_is_rejected_on_write_and_old_stored_entries_are_isolated,
    journeys.test_imports_are_atomic_checked_and_undoable_and_copy_keeps_source,
    journeys.test_yearly_ical_metadata_survives_storage_legacy_edits_and_undo,
    journeys.test_undo_of_metadata_addition_clears_it_on_older_stored_entries,
    journeys.test_publication_date_sets_are_frozen_while_operational_snapshot_is_current,
    journeys.test_date_set_reference_names_can_remain_in_another_selected_set_or_entry,
    journeys.test_retired_rules_and_inactive_calendars_do_not_block_date_set_changes,
    journeys.test_policy_restore_rejects_a_missing_date_set_without_disclosing_its_id,
    journeys.test_an_inactive_date_set_cannot_preserve_a_rules_entry_name,
    journeys.test_all_date_set_references_need_named_entries_in_every_selected_set,
], ids=lambda journey: journey.__name__.removeprefix('test_'))
def test_date_set_persistence_journey(pg, journey):
    journey(pg)


@pytest.mark.postgres
@pytest.mark.parametrize('action', ['rename', 'entry_delete', 'set_delete', 'catalogue_update', 'bulk_update'])
def test_date_set_reference_changes_are_checked_on_postgres(pg, action):
    journeys.test_date_set_changes_preserve_rule_and_calendar_references(pg, action)


@pytest.mark.postgres
@pytest.mark.parametrize('grouped', [False, True], ids=['explicit', 'grouped'])
@pytest.mark.parametrize('action', ['rename', 'entry_delete', 'set_delete', 'all_rename'])
def test_policy_restoration_references_on_postgres(pg, grouped, action):
    journeys.test_policy_restoration_rechecks_date_set_references(pg, grouped, action)


@pytest.mark.postgres
@pytest.mark.parametrize('fact_status', ['proposed', 'active'])
@pytest.mark.parametrize('action', ['rename', 'entry_delete', 'set_delete', 'catalogue_update', 'deactivate', 'bulk_update'])
def test_fact_reference_changes_on_postgres(pg, fact_status, action):
    journeys.test_date_set_changes_preserve_fact_references(pg, fact_status, action)


@pytest.mark.postgres
@pytest.mark.parametrize('predecessor', [False, True], ids=['retired', 'predecessor'])
@pytest.mark.parametrize('action', ['rename', 'entry_delete', 'set_delete', 'deactivate', 'all_rename', 'missing_set'])
def test_fact_restoration_references_on_postgres(pg, predecessor, action):
    journeys.test_fact_restoration_rechecks_date_set_references(pg, predecessor, action)


@pytest.mark.postgres
@pytest.mark.parametrize('action', ['activate', 'propose'])
def test_fact_admission_references_on_postgres(pg, action):
    journeys.test_fact_admission_rejects_inactive_date_sets(pg, action)


