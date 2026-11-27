"""Bounded combined-candidate checks against both canonical disposable databases."""
import pytest

from packages.contracts import schemas as s
from .conftest import grant_branding_editor
from . import test_branding_configuration as branding
from . import test_generation_population as population
from . import test_shared_search as shared
from . import test_presentation_contract_completions as presentation


@pytest.fixture(params=['h', pytest.param('pg', marks=pytest.mark.postgres)], ids=['sqlite', 'postgres'])
def both(request):
    return request.getfixturevalue(request.param)


BRANDING_CASES = (
    branding.test_public_default_and_saved_projection_are_minimal_and_independent_of_departments,
    branding.test_branding_is_revision_guarded_audited_and_undoable_without_rewriting_history,
    presentation.test_saved_short_name_survives_omitted_input_and_undo,
)


@pytest.mark.parametrize('case', [
    *BRANDING_CASES,
    shared.test_search_and_layer_options_persist_through_versioned_saved_view_paths,
    population.test_contract_scope_references_empty_selection_and_legacy_omission,
    population.test_matching_worker_source_is_required_before_scheduler_execution,
], ids=lambda case: case.__name__)
def test_existing_small_paths_on_both_databases(both, case):
    if case in BRANDING_CASES:
        grant_branding_editor(both)
    case(both)


@pytest.mark.parametrize('action', ['apply', 'continue'])
def test_fresh_population_on_both_databases(both, action):
    population.test_fresh_population_data_reconciles_apply_and_preserves_frozen_continue(both, action)


@pytest.mark.parametrize('engine', ['legacy', 'cp_sat'])
def test_two_pass_population_on_both_databases(both, engine):
    population.test_two_pass_current_data_preserves_all_prior_fields_and_undo(both, engine)


def test_search_options_filter_real_bulk_rows_before_count_and_page(both):
    h = both
    first = h.configure('people', {'display_name': 'Cedar Acceptance', 'tags': ['Cedar intake']})['record']
    second = h.configure('people', {'display_name': 'Cedar Acceptance extra'})['record']
    # Give both records department membership through the public command path.
    for p in (first, second):
        h.command('coordinator', 'membership.create', {'person_id': p['id'],
            'category_id': h.seed['category'], 'starts_on': '2020-01-01'})
    def read(options, q='Cedar Acceptance', offset=0):
        response = h.client('coordinator').post('/api/bulk/rows', json={
            'selection': {'collection': 'people', 'where': {'q': q, 'search_options': options}},
            'columns': ['display_name'], 'limit': 1, 'offset': offset})
        assert response.status_code == 200, response.text
        return response.json()
    partial = read({'scope': 'self'})
    assert partial['total'] == 2 and partial['has_more']
    assert read({'scope': 'self'}, offset=1)['has_more'] is False
    exact = read({'scope': 'self', 'match': 'exact'})
    assert exact['total'] == 1 and exact['rows'][0]['id'] == first['id']
    reference = read({'scope': 'self', 'reference': {'kind': 'people', 'id': first['id']}}, q='stale label')
    assert reference['total'] == 1 and reference['rows'][0]['id'] == first['id']


@pytest.mark.parametrize('role', [r for r in s.ROLE_NAMES if r in {'admin', 'coordinator', 'staff'}])
def test_session_reference_source_access_for_affected_roles(both, role):
    # The Schedule offers these references to planners; read-only staff never get a directory offer.
    response = both.client(role).get('/api/search/references', params={
        'collection': 'sessions', 'kind': 'people', 'q': 'Staff 01', 'limit': 1})
    assert response.status_code == (403 if role == 'staff' else 200), response.text
    if response.status_code == 200:
        assert len(response.json()['items']) <= 1
        assert set(response.json()) == {'items', 'has_more'}


@pytest.mark.parametrize('fmt,media_type', [('PNG', 'image/png'), ('JPEG', 'image/jpeg'), ('WEBP', 'image/webp')])
def test_raster_history_on_both_databases(both, fmt, media_type):
    grant_branding_editor(both)
    branding.test_raster_uploads_strip_metadata_keep_local_refs_and_preserve_history_assets(both, fmt, media_type)
