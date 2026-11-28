"""Presentation additions retain legacy serializations and shared media security limits."""
from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from packages.contracts import schemas as s
from apps.api.services import branding, score_check_provider


def test_legacy_branding_defaults_and_saved_public_short_name():
    assert s.BrandingSettings.model_validate({}).short_name == 'Rota'
    row = SimpleNamespace(data={'short_name': 'Rota'}, revision=7)
    db = SimpleNamespace(get=lambda *args: row)
    assert branding.public(db)['short_name'] == 'Rota'
    value = s.BrandingInput(app_name='Rota', institution_name='Hospital', reason='Update name')
    assert 'short_name' not in value.model_dump(exclude_unset=True)
    assert s.BrandingInput(**{**value.model_dump(), 'short_name': 'Clinic'}).short_name == 'Clinic'
    with pytest.raises(ValidationError):
        s.BrandingInput(**{**value.model_dump(), 'short_name': ''})


def test_media_limits_have_one_owner_and_preserve_values():
    assert branding.IMAGE_FORMATS is s.BRANDING_IMAGE_FORMATS
    assert s.BRANDING_IMAGE_LIMITS is s.MEDIA_LIMITS
    assert branding.MAX_FILE_BYTES == score_check_provider.MAX_FILE_BYTES == s.SCORE_CHECK_LIMITS['file_bytes'] == 10485760
    assert branding.MAX_IMAGE_PIXELS == score_check_provider.MAX_IMAGE_PIXELS == 16000000
    assert set(s.BRANDING_IMAGE_FORMATS) == {'image/png', 'image/jpeg', 'image/webp'}


def test_optional_layers_omit_legacy_null_and_preserve_explicit_settings():
    assert 'layers' not in s.PlannerViewSettings().model_dump(mode='json')
    assert 'layers' not in s.PlannerViewSettings(layers=None).model_dump(mode='json')
    layers = {'draft': False, 'leave': False, 'mine': True, 'mode': 'overlay', 'rotations': False}
    view = s.TableViewDefinition(planner={'layers': layers})
    assert view.model_dump(mode='json')['planner']['layers'] == layers
    assert s.PLANNER_LAYER_DEFAULTS == s.PlannerLayers().model_dump()
    assert s.PlannerViewSettings(layers={}).layers.model_dump() == {'draft': True, 'leave': True, 'mine': False, 'mode': 'edit', 'rotations': True}
    for invalid in ({'draft': 'false'}, {'leave': 1}, {'mine': None}, {'mode': 'other'}, {'rotations': 'no'}, {'workspace_id': 'any'}):
        with pytest.raises(ValidationError):
            s.PlannerViewSettings(layers=invalid)


@pytest.mark.usefixtures('branding_editor')
def test_saved_short_name_survives_omitted_input_and_undo(h):
    payload = {'app_name':'Rota','institution_name':'Hospital','reason':'Choose presentation'}
    h.command('admin','branding.update',{**payload,'short_name':'Clinic'}, revision=1)
    saved = h.command('admin','branding.update',{**payload,'app_name':'Department rota'}, revision=2)
    assert saved['branding']['short_name'] == 'Clinic'
    assert h.get('staff','/api/branding')['short_name'] == 'Clinic'
    undo = h.command('admin','branding.undo', {'reason':'Restore presentation'}, revision=3)
    assert undo['settings']['short_name'] == 'Clinic'
    assert undo['branding']['app_name'] == 'Rota'
