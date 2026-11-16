"""Dated programme membership and explicit rotation availability."""
from alembic import op
import sqlalchemy as sa

revision = "0011_alpha_programmes"
down_revision = "0010_validation_contract"
branch_labels = depends_on = None


def upgrade():
    checks = sa.inspect(op.get_bind()).get_check_constraints("availability")
    kind_check = next(c for c in checks if "kind" in c["sqltext"])
    with op.batch_alter_table("availability", naming_convention={"ck": "ck_%(table_name)s_kind"}) as batch:
        batch.drop_constraint(kind_check["name"] or "ck_availability_kind", type_="check")
        batch.create_check_constraint("availability_kind_alpha", "kind IN ('approved_leave','actual_absence','external_commitment','protected_time','off_duty')")
    op.create_table("programme_memberships",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("person_id", sa.String(36), sa.ForeignKey("people.id"), nullable=False),
        sa.Column("programme_id", sa.String(36), sa.ForeignKey("programmes.id"), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=False),
        sa.CheckConstraint("ends_on > starts_on"))
    op.create_index("ix_programme_memberships_person_id", "programme_memberships", ["person_id"])
    op.create_index("ix_programme_memberships_department_id", "programme_memberships", ["department_id"])
    op.add_column("rotations", sa.Column("department_available", sa.Boolean(), nullable=False, server_default=sa.true()))
    # Older binaries must not publish without programme/rotation validation.
    if op.get_bind().dialect.name == "sqlite":
        op.execute("""CREATE TRIGGER publications_alpha_contract BEFORE INSERT ON publications
            WHEN json_extract(NEW.validation, '$.contracts.block_quota') IS 'block-quota-v1'
             AND json_extract(NEW.validation, '$.contracts.programmes') IS NOT 'programmes-v1'
            BEGIN SELECT RAISE(ABORT, 'programme-aware publication validation required'); END""")
    else:
        op.execute("""CREATE FUNCTION rota_publication_alpha_contract() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF (NEW.validation->'contracts'->>'block_quota') = 'block-quota-v1'
                 AND (NEW.validation->'contracts'->>'programmes') IS DISTINCT FROM 'programmes-v1' THEN
                RAISE EXCEPTION 'programme-aware publication validation required' USING ERRCODE='23514';
              END IF;
              RETURN NEW;
            END $$""")
        op.execute("""CREATE TRIGGER publications_alpha_contract BEFORE INSERT ON publications
            FOR EACH ROW EXECUTE FUNCTION rota_publication_alpha_contract()""")


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
