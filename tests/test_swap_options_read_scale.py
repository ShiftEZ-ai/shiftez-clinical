"""Offer sheets read each publication once even when a person has many future duties."""
import pytest
from sqlalchemy import event

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.swaps import summary
from packages.domain.validation import check_contracts
from tests.test_backend_scale import measure, scale_department
from tests.test_performance_budget import python_budget
from tests.test_staff_swaps import ordered_clock


@pytest.mark.postgres
def test_department_swap_options_bound_publication_reads(pg, monkeypatch):
    with ordered_clock(monkeypatch):
        workspace, data, _, _ = scale_department(pg)
        with transaction(pg.app.state.engine, write=True) as db:
            stored = db.get(m.Workspace, workspace['id'])
            stored.status = 'published'
            pub = m.Publication(workspace_id=stored.id, department_id=stored.department_id, sequence=1,
                starts_on=stored.starts_on, ends_on=stored.ends_on, current=True, published_by=stored.created_by,
                snapshot=data, fingerprint=data['fingerprint'],
                validation={'contracts': check_contracts(), 'synthetic_evidence': 'evidence ' * 100_000})
            db.add(pub)
            db.flush()
            assignments = sorted((p for s in data['shifts'] for p in s['participants']
                                  if p['person_id'] == pg.seed['people'][0]), key=lambda p: (p['start'], p['id']))
            expected = [summary(pub, row['id']) for row in assignments]
        pg.client('staff')
        statements = []
        def read(_connection, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)
        event.listen(pg.app.state.engine, 'before_cursor_execute', read)
        try:
            result, measured = measure(pg, 'swap-options', lambda: pg.get('staff', '/api/swaps/options'))
        finally:
            event.remove(pg.app.state.engine, 'before_cursor_execute', read)
        assert result['assignments'] == expected and len(expected) == 28
        assert result['assignment_id'] == assignments[0]['id']
        assert not result['assignments_partial']
        assert all(r['eligible'] is None for r in result['recipients'])
        # A bounded query count alone cannot detect a 200-fold JSON transfer in one join.
        snapshot_reads = [q for q in statements if 'publications.snapshot' in q]
        assert len(snapshot_reads) == 1 and 'JOIN' not in snapshot_reads[0], snapshot_reads
        assert all('publications.validation' not in q for q in statements)
        assert measured['sql'].get('SELECT', 0) <= 20, measured
        return locals()


@pytest.mark.postgres
@pytest.mark.cpu_budget
def test_department_swap_options_cpu_budget(pg, monkeypatch):
    state = test_department_swap_options_bound_publication_reads(pg, monkeypatch)
    assert state['measured']['cpu_seconds'] < python_budget(1), state['measured']


@pytest.mark.postgres
@pytest.mark.wall_benchmark
def test_department_swap_options_wall_budget(pg, monkeypatch):
    state = test_department_swap_options_bound_publication_reads(pg, monkeypatch)
    assert state['measured']['wall_seconds'] < python_budget(3), state['measured']
