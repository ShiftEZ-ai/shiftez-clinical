"""Indexed Unicode-casefolded login aliases; original identifiers remain unchanged."""
from alembic import op
import sqlalchemy as sa

revision = "0121_identifier_lookup"
down_revision = "0120_auth_configuration"
branch_labels = depends_on = None
# Python's Unicode casefold is intentionally outside the static SQL proof allowlist.
SAFETY_CLASS = "destructive"
DESTRUCTIVE_REVIEWED = (
    "Only add nullable derived lookup columns and nonunique indexes. Backfill each new column only while NULL, "
    "from the same row's existing identifier using Python Unicode casefold; never modify an original identifier, "
    "identity binding, password, session, publication or audit record. Forward-only rollback preserves all data.")


def upgrade():
    op.add_column("accounts", sa.Column("username_normalized", sa.String(192), nullable=True))
    op.add_column("accounts", sa.Column("email_normalized", sa.String(762), nullable=True))
    op.add_column("identities", sa.Column("subject_normalized", sa.String(765), nullable=True))
    accounts = sa.table("accounts", sa.column("id"), sa.column("username"), sa.column("email"),
                        sa.column("username_normalized"), sa.column("email_normalized"))
    identities = sa.table("identities", sa.column("id"), sa.column("subject"), sa.column("subject_normalized"))
    connection = op.get_bind()
    for row in connection.execute(sa.select(accounts.c.id, accounts.c.username, accounts.c.email)):
        connection.execute(sa.update(accounts).where(accounts.c.id == row.id, accounts.c.username_normalized.is_(None))
                           .values(username_normalized=row.username.casefold()))
        connection.execute(sa.update(accounts).where(accounts.c.id == row.id, accounts.c.email_normalized.is_(None))
                           .values(email_normalized=row.email.casefold()))
    for row in connection.execute(sa.select(identities.c.id, identities.c.subject)):
        connection.execute(sa.update(identities).where(identities.c.id == row.id, identities.c.subject_normalized.is_(None))
                           .values(subject_normalized=row.subject.casefold()))
    op.create_index("ix_accounts_username_normalized", "accounts", ["username_normalized"])
    op.create_index("ix_accounts_email_normalized", "accounts", ["email_normalized"])
    op.create_index("ix_identities_issuer_subject_normalized", "identities", ["issuer", "subject_normalized"])


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
