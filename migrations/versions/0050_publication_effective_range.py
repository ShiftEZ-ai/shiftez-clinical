"""Publication effective ranges ("replace from a date") and a database guard against overlapping rotas in force.

Expand-only. Two nullable columns:
- publications.effective_until: first day a publication is no longer in force (a later rota replaced it from that day).
  Snapshots stay immutable; this date is the only field besides `current` that may change, and only ever earlier.
- workspaces.replaces_publication_id: the publication a draft replaces from its own first day.

Guards (both dialects): a publication's effective end lies strictly inside its dates, and no two current publications of
one department are in force on the same day. Writers already serialise on the application write lock (apps/api/db.py),
so the check sees every committed publication. See docs/design/schedule-unification.md.

Revision ID: 0050_publication_effective_range
Revises: 0049_workspace_return_note
"""
from alembic import op
import sqlalchemy as sa

revision = "0050_publication_effective_range"
down_revision = "0049_workspace_return_note"
branch_labels = None
depends_on = None

# Another current publication of the department whose in-force days meet NEW's.
OVERLAP = """EXISTS (SELECT 1 FROM publications p WHERE p.department_id = NEW.department_id AND p.id <> NEW.id AND p.current
             AND p.starts_on < COALESCE(NEW.effective_until, NEW.ends_on)
             AND NEW.starts_on < COALESCE(p.effective_until, p.ends_on))"""
OUTSIDE = "NEW.effective_until IS NOT NULL AND NOT (NEW.starts_on < NEW.effective_until AND NEW.effective_until < NEW.ends_on)"
GROWS = "OLD.effective_until IS NOT NULL AND (NEW.effective_until IS NULL OR NEW.effective_until > OLD.effective_until)"


def upgrade():
    dialect = op.get_bind().dialect.name
    op.add_column("publications", sa.Column("effective_until", sa.Date(), nullable=True))
    if dialect == "sqlite":
        op.execute("ALTER TABLE workspaces ADD COLUMN replaces_publication_id VARCHAR(36) REFERENCES publications(id)")
        # 0002's update guard lists the immutable columns, so effective_until is already writable; these limit how.
        op.execute(f"""CREATE TRIGGER publications_in_force_insert BEFORE INSERT ON publications
                       WHEN ({OUTSIDE}) OR (NEW.current AND {OVERLAP})
                       BEGIN SELECT RAISE(ABORT, 'overlapping current publications'); END""")
        op.execute(f"""CREATE TRIGGER publications_in_force_update BEFORE UPDATE ON publications
                       WHEN ({OUTSIDE}) OR ({GROWS}) OR (NEW.current AND {OVERLAP})
                       BEGIN SELECT RAISE(ABORT, 'overlapping current publications'); END""")
    elif dialect == "postgresql":
        op.add_column("workspaces", sa.Column("replaces_publication_id", sa.String(36), sa.ForeignKey("publications.id"), nullable=True))
        # Same immutability as 0002, now also letting effective_until move earlier (never later, never cleared).
        op.execute(f"""CREATE OR REPLACE FUNCTION rota_publication_guard() RETURNS trigger LANGUAGE plpgsql AS $$
                      BEGIN
                        IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'immutable publication'; END IF;
                        IF (to_jsonb(NEW) - 'current' - 'effective_until') IS DISTINCT FROM (to_jsonb(OLD) - 'current' - 'effective_until') THEN
                          RAISE EXCEPTION 'immutable publication';
                        END IF;
                        IF {GROWS} THEN RAISE EXCEPTION 'immutable publication'; END IF;
                        RETURN NEW;
                      END $$""")
        op.execute(f"""CREATE FUNCTION rota_publication_in_force() RETURNS trigger LANGUAGE plpgsql AS $$
                      BEGIN
                        IF {OUTSIDE} THEN
                          RAISE EXCEPTION 'publication effective range outside its dates' USING ERRCODE = '23514';
                        END IF;
                        IF NEW.current AND {OVERLAP} THEN
                          RAISE EXCEPTION 'overlapping current publications' USING ERRCODE = '23P01';
                        END IF;
                        RETURN NEW;
                      END $$""")
        op.execute("CREATE TRIGGER publications_in_force_guard BEFORE INSERT OR UPDATE ON publications "
                   "FOR EACH ROW EXECUTE FUNCTION rota_publication_in_force()")
    else:
        raise RuntimeError("Unsupported database dialect")


def downgrade():
    raise RuntimeError("Forward-only: effective ranges are part of the publication record; restore a compatible database/image pair")
