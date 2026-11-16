"""Every ORM validation writer must keep the deferred stored-check stamp current.

The AST check cannot see in-place JSON mutation of ``validation`` without
reassignment, Core/bulk UPDATE or raw SQL writing the column, or a
``validation=`` keyword in a model constructor. Today all 15 writers are ORM
attribute assignments kept by the after_insert/after_update mapper listener
(``apps/api/models.py`` ~1055-1059 -> ``workspace_reads.store_check``).
"""

import ast
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import defer

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.read_projections import CANONICAL_AUDIENCE, source_identity
from packages.domain.input_manifest import digest


ROOT = Path(__file__).parents[1]
EXPECTED_WRITERS = {
    ('apps/api/services/acknowledged_gaps.py', 'record_decision', 1),
    ('apps/api/services/planning.py', 'archive', 1),
    ('apps/api/services/planning.py', 'changed', 1),
    ('apps/api/services/planning.py', 'defer_complete_check', 1),
    ('apps/api/services/planning.py', 'lifecycle', 6),
    ('apps/api/services/planning.py', 'revalidate', 2),
    ('apps/api/services/reviews.py', 'withdraw', 1),
    ('apps/api/workspace_baselines.py', 'refresh', 1),
    ('apps/worker/main.py', 'finish_job', 1),
}


def _validation_writers():
    writers = set()
    for root in ('apps/api', 'apps/worker', 'packages'):
        for path in (ROOT / root).rglob('*.py'):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                visitor = _ValidationAssignmentCounter()
                for statement in node.body:
                    visitor.visit(statement)
                if visitor.count:
                    writers.add((path.relative_to(ROOT).as_posix(), node.name, visitor.count))
    return writers


class _ValidationAssignmentCounter(ast.NodeVisitor):
    def __init__(self):
        self.count = 0

    def visit_FunctionDef(self, node):
        return

    def visit_AsyncFunctionDef(self, node):
        return

    def visit_ClassDef(self, node):
        return

    def visit_Assign(self, node):
        if any(isinstance(target, ast.Attribute) and target.attr == 'validation' for target in node.targets):
            self.count += 1
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        if isinstance(node.target, ast.Attribute) and node.target.attr == 'validation':
            self.count += 1
        self.generic_visit(node)

    def visit_AugAssign(self, node):
        if isinstance(node.target, ast.Attribute) and node.target.attr == 'validation':
            self.count += 1
        self.generic_visit(node)


def test_workspace_validation_writer_inventory_is_complete():
    assert _validation_writers() == EXPECTED_WRITERS


@pytest.mark.parametrize('writer', sorted(EXPECTED_WRITERS))
@pytest.mark.postgres
def test_deferred_stored_check_stamp_follows_every_validation_writer(pg, writer):
    workspace = pg.workspace(f'Validation stamp {writer[0]}:{writer[1]}')
    payload = {'writer': f'{writer[0]}:{writer[1]}'}
    with transaction(pg.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, workspace['id'])
        row.validation = payload

    with transaction(pg.app.state.engine) as db:
        full = db.get(m.Workspace, workspace['id'])
        expected = digest([source_identity(db), full.revision, full.accepted_issues, full.validation])
    with transaction(pg.app.state.engine) as db:
        deferred = db.scalar(select(m.Workspace).options(defer(m.Workspace.validation)).where(
            m.Workspace.id == workspace['id']))
        stamp = db.scalar(select(m.WorkspaceReadProjection.stamp).where(
            m.WorkspaceReadProjection.workspace_id == workspace['id'],
            m.WorkspaceReadProjection.audience == CANONICAL_AUDIENCE,
            m.WorkspaceReadProjection.kind == 'stored_check'))
        assert 'validation' not in deferred.__dict__
        assert stamp == expected
