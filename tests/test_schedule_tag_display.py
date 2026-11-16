"""Tag presentation is saved configuration, independent of clinical tag evaluation."""
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import event

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s


def test_tag_display_defaults_and_metadata_preserve_existing_views():
    for value in ({}, {'colour_tags': None}, {'colour_tags': []}):
        view = s.PlannerViewSettings(**value).model_dump(mode='json')
        assert 'colour_tags' not in view and view['colour_by'] == 'type'
    assert s.ViewProperty(key='tags').on is False
    assert s.ViewProperty(key='tags', on=True, form='text').on is True
    assert s.VIEW_PROPERTY_DEFINITIONS['tags'] == {'label': 'Person tags', 'forms': ('chip', 'text')}
    assert s.PLANNER_COLOUR_TAG_LIMIT == s.VIEW_CHOICE_LIMIT


@pytest.mark.parametrize('invalid', [
    ['Teaching'], [str(uuid4())] * 2, [''], [True],
    [str(uuid4()) for _ in range(s.PLANNER_COLOUR_TAG_LIMIT + 1)],
])
def test_tag_colour_priority_requires_unique_bounded_stable_ids(invalid):
    with pytest.raises(ValidationError):
        s.PlannerViewSettings(colour_by='tag', colour_tags=invalid)


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('legacy', [False, True])
def test_tag_priority_rename_reset_and_revision_checked_undo(h, legacy):
    tags = [h.command('coordinator', 'catalogue.create', {'catalogue': 'tags',
        'value': {'name': name, 'labels': ['people'], 'colour': colour},
        'reason': 'Fictional presentation fixture'})['record']
        for name, colour in (('Teaching', 'violet'), ('Clinical', 'blue'))]
    ids = [tag['id'] for tag in reversed(tags)]
    prefix, endpoint = ('planner_view', '/api/planner/views') if legacy else (
        'table_view', '/api/table-views?table=planner')

    def payload(settings):
        return {'name': 'Synthetic tag colour priorities',
                'definition': settings if legacy else {'planner': settings},
                **({} if legacy else {'table_key': 'planner'})}

    def settings(view):
        return view['definition'] if legacy else view['definition']['planner']

    def read(view):
        return next(row for row in h.get('coordinator', endpoint)['views'] if row['id'] == view['id'])

    before = h.get('coordinator', '/api/pulse')['input_revision']
    choices = {'colour_by': 'tag', 'colour_tags': ids,
               'properties': [{'key': 'tags', 'on': True, 'form': 'chip'}]}
    view = h.command('coordinator', prefix + '.create', payload(choices))['view']
    assert settings(read(view))['colour_tags'] == ids
    changed = h.command('coordinator', prefix + '.update', payload({**choices, 'colour_tags': ids[::-1]}),
                        view['id'], view['revision'])
    assert settings(changed['view'])['colour_tags'] == ids[::-1]
    undo = h.command('coordinator', prefix + '.update', changed['previous_view'], view['id'], 2)['view']
    assert settings(read(undo))['colour_tags'] == ids
    h.command('coordinator', prefix + '.update', payload(choices), view['id'], 1, status=409)
    reset = h.command('coordinator', prefix + '.update', payload({**choices, 'colour_tags': []}),
                      view['id'], 3)['view']
    assert 'colour_tags' not in settings(read(reset))
    restored = h.command('coordinator', prefix + '.update', payload(choices), view['id'], 4)['view']
    assert h.get('coordinator', '/api/pulse')['input_revision'] == before
    h.command('coordinator', 'catalogue.update', {'catalogue': 'tags',
        'value': {'name': 'Renamed teaching', 'labels': ['people'], 'colour': 'teal'},
        'reason': 'Fictional rename'}, tags[0]['id'], tags[0]['revision'])
    assert settings(read(restored))['colour_tags'] == ids


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('legacy', [False, True])
def test_unknown_or_foreign_colour_tags_cannot_write_or_update_a_view(h, legacy):
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed['organisation_id'], name='Foreign synthetic tags')
        db.add(department)
        db.flush()
        tag = m.Tag(department_id=department.id, name='Private tag', labels=['people'])
        db.add(tag)
        db.flush()
        foreign = tag.id
    prefix, endpoint = ('planner_view', '/api/planner/views') if legacy else (
        'table_view', '/api/table-views?table=planner')

    def payload(ids):
        settings = {'colour_by': 'tag', 'colour_tags': ids}
        return {'name': 'Unknown tag selection', 'definition': settings if legacy else {'planner': settings},
                **({} if legacy else {'table_key': 'planner'})}

    old = h.command('coordinator', prefix + '.create', payload([]))['view']
    for invalid in ([str(uuid4())], [foreign]):
        h.command('coordinator', prefix + '.create', payload(invalid), status=404)
        h.command('coordinator', prefix + '.update', payload(invalid), old['id'], 1, status=404)
    assert h.get('coordinator', endpoint)['views'] == [old]


def test_tag_reference_validation_is_one_batch_for_a_multi_tag_view(h):
    with transaction(h.app.state.engine, write=True) as db:
        tags = [m.Tag(department_id=h.seed['department_id'], name='Synthetic ' + str(i), labels=['people'])
                for i in range(12)]
        db.add_all(tags)
        db.flush()
        ids = [tag.id for tag in tags]
    reads = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if 'SELECT tags.id' in statement and 'FROM tags' in statement:
            reads.append(statement)

    event.listen(h.app.state.engine, 'before_cursor_execute', capture)
    try:
        saved = h.command('coordinator', 'table_view.create', {'table_key': 'planner', 'name': 'Tag batch',
            'definition': {'planner': {'colour_by': 'tag', 'colour_tags': ids}}})['view']
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', capture)
    assert saved['definition']['planner']['colour_tags'] == ids
    assert len(reads) == 1
