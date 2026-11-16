"""Optional email delivery and encrypted, revocable delegated mailbox consent.

Forward-only additions; existing receipts and deliveries remain untouched.
"""
from alembic import op
import sqlalchemy as sa

revision = '0087_email_delivery'
down_revision = '0082_events'
branch_labels = depends_on = None


def records():
    return [sa.Column('id', sa.String(36), primary_key=True),
            sa.Column('revision', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('department_id', sa.String(36), sa.ForeignKey('departments.id'), nullable=False, index=True)]


def upgrade():
    op.add_column('people', sa.Column('work_email', sa.String(254), nullable=False, server_default=''))
    op.add_column('departments', sa.Column('email_settings', sa.JSON(), nullable=False, server_default='{}'))
    op.add_column('event_types', sa.Column('also_email', sa.Boolean(), nullable=False, server_default=sa.false()))
    for table in ('events', 'department_content'):
        op.add_column(table, sa.Column('also_email', sa.Boolean()))
        op.add_column(table, sa.Column('email_sender', sa.String(16)))
        # Add the nullable reference in place; rebuilding a parent table breaks existing receipt foreign keys.
        if op.get_bind().dialect.name == 'sqlite':
            op.execute(f'ALTER TABLE {table} ADD COLUMN email_sender_identity_id VARCHAR(36) '
                       f'CONSTRAINT fk_{table}_email_sender REFERENCES identities(id)')
        else:
            op.add_column(table, sa.Column('email_sender_identity_id', sa.String(36),
                          sa.ForeignKey('identities.id', name='fk_' + table + '_email_sender')))
    op.create_table('mailbox_connections', *records(),
        sa.Column('identity_id', sa.String(36), sa.ForeignKey('identities.id'), nullable=False),
        sa.Column('encrypted_refresh_token', sa.Text(), nullable=False),
        sa.Column('configuration_hash', sa.String(64), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('identity_id', 'department_id'))
    op.create_table('mailbox_oauth_flows', *records(),
        sa.Column('identity_id', sa.String(36), sa.ForeignKey('identities.id'), nullable=False),
        sa.Column('session_id', sa.String(36), sa.ForeignKey('sessions.id'), nullable=False),
        sa.Column('state_hash', sa.String(64), nullable=False, unique=True),
        sa.Column('encrypted_verifier', sa.Text(), nullable=False),
        sa.Column('configuration_hash', sa.String(64), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table('email_deliveries', *records(),
        sa.Column('outbox_id', sa.String(36), sa.ForeignKey('outbox.id'), nullable=False, unique=True),
        sa.Column('source_outbox_id', sa.String(36), sa.ForeignKey('outbox.id'), nullable=False, index=True),
        sa.Column('receipt_id', sa.String(36), sa.ForeignKey('announcement_receipts.id'), index=True),
        sa.Column('identity_id', sa.String(36), sa.ForeignKey('identities.id')),
        sa.Column('person_id', sa.String(36), sa.ForeignKey('people.id'), index=True),
        sa.Column('event_occurrence_id', sa.String(36), sa.ForeignKey('event_occurrences.id'), index=True),
        sa.Column('sender_identity_id', sa.String(36), sa.ForeignKey('identities.id')),
        sa.Column('sender_mode', sa.String(16), nullable=False),
        sa.Column('address', sa.String(254), nullable=False),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('error_code', sa.String(80)),
        sa.Column('sent_at', sa.DateTime(timezone=True)),
        sa.UniqueConstraint('source_outbox_id', 'receipt_id'),
        sa.CheckConstraint('identity_id IS NOT NULL OR person_id IS NOT NULL'),
        sa.CheckConstraint("sender_mode IN ('smtp','microsoft')"),
        sa.CheckConstraint("state IN ('queued','sent','failed','draft')"))


def downgrade():
    raise RuntimeError('Forward-only: retain email and receipt evidence')
