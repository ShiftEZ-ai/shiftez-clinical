"""Versioned short labels and the shared department rank order."""
from sqlalchemy import select
from apps.api import models as m
from apps.api.db import transaction


def test_versioned_short_names_legacy_updates_and_catalogue_rank_order(h):
    for kind, value in (
        ('tags', {'name': 'Synthetic area', 'labels': ['services']}),
        ('rotation_types', {'name': 'Synthetic rotation', 'starts_on': '2026-01-01'}),
        ('leave_types', {'name': 'Synthetic leave', 'short_code': 'SYN'})):
        made = h.configure(kind, {**value, 'short_name': 'Syn'})['record']
        changed = h.configure(kind, value, made['id'], made['revision'])['record']
        assert changed['short_name'] == 'Syn' and changed['revision'] == 2
        cleared = h.configure(kind, {**value, 'short_name': ''}, changed['id'], changed['revision'])['record']
        assert cleared['short_name'] == ''
        with transaction(h.app.state.engine) as db:
            histories = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == made['id']).order_by(m.DefinitionHistory.object_revision)))
            assert [r.snapshot['short_name'] for r in histories] == ['Syn', 'Syn', '']
    first = h.configure('categories', {'name': 'Z first', 'starts_on': '2026-01-01', 'sort_order': 1})['record']
    h.configure('categories', {'name': 'A last', 'starts_on': '2026-01-01', 'sort_order': 99})
    saved = h.configure('categories', {'name': 'Z first', 'starts_on': '2026-01-01'}, first['id'], first['revision'])['record']
    assert saved['sort_order'] == 1
    categories = h.get('coordinator', '/api/catalogues')['categories']
    assert categories[0]['id'] == first['id']
