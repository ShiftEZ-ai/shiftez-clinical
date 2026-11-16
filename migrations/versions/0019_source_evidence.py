"""Protect imported evidence and its original provenance from rewriting."""
from alembic import op

revision = '0019_source_evidence'
down_revision = '0018_request_rota'
branch_labels = depends_on = None


def upgrade():
    for table in ('reference_imports', 'source_mappings'):
        if op.get_bind().dialect.name == 'sqlite':
            for action in ('UPDATE', 'DELETE'):
                op.execute(f"CREATE TRIGGER {table}_{action.lower()}_guard BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'immutable evidence'); END")
        else:
            op.execute(f"CREATE TRIGGER {table}_guard BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION rota_immutable_evidence()")


def downgrade():
    raise RuntimeError('Forward-only: restore a compatible database/image pair')
