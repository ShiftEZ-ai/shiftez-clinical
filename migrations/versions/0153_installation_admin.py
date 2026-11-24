"""Widen role bindings for explicit installation administration and seed its first holder."""
from datetime import datetime, timezone
import re
from uuid import uuid4
from zoneinfo import ZoneInfo

from alembic import op
import sqlalchemy as sa

revision = "0153_installation_admin"
down_revision = "0152_feasibility_results"
branch_labels = depends_on = None
SAFETY_CLASS = "destructive"
DESTRUCTIVE_REVIEWED = (
    "Only widen the role_bindings role CHECK to include the contract roles and grant the first administrator "
    "with a role.grant audit row. The migration never changes or deletes an existing row. "
    "Verify a backup before deployment and restore the compatible database and image together if needed.")

ROLES = ("staff", "coordinator", "approver", "credentialer", "admin", "auditor", "programme_lead",
         "operator", "installation_admin")


def upgrade():
    connection = op.get_bind()
    table = sa.Table("role_bindings", sa.MetaData(), autoload_with=connection)
    check, = (constraint for constraint in table.constraints if isinstance(constraint, sa.CheckConstraint)
              and re.match(r'^\(*"?role\b', str(constraint.sqltext).strip()))
    if not all(f"'{role}'" in str(check.sqltext) for role in ROLES):
        # The legacy SQLite check is unnamed. Name it on the reflected copy so batch mode can replace it.
        name = check.name or "ck_role_bindings_role"
        check.name = name
        with op.batch_alter_table(table.name, copy_from=table) as batch:
            batch.drop_constraint(name, type_="check")
            batch.create_check_constraint(name, "role IN (" + ",".join(f"'{role}'" for role in ROLES) + ")")

    binding = sa.table("role_bindings", sa.column("id"), sa.column("revision"),
        sa.column("created_at", sa.DateTime(timezone=True)), sa.column("identity_id"), sa.column("department_id"),
        sa.column("role"), sa.column("starts_on", sa.Date), sa.column("ends_on", sa.Date), sa.column("revoked", sa.Boolean))
    department = sa.table("departments", sa.column("id"), sa.column("organisation_id"), sa.column("created_at"))
    organisation = sa.table("organisations", sa.column("id"), sa.column("timezone"))
    identity = sa.table("identities", sa.column("id"), sa.column("disabled", sa.Boolean))
    account = sa.table("accounts", sa.column("id"), sa.column("identity_id"), sa.column("accepted_at"))
    audit = sa.table("audit_events", sa.column("id"), sa.column("revision"),
        sa.column("created_at", sa.DateTime(timezone=True)), sa.column("department_id"), sa.column("actor_id"),
        sa.column("action"), sa.column("target_id"), sa.column("reason"), sa.column("detail", sa.JSON),
        sa.column("correlation_id"))
    query = sa.select(binding.c.role, binding.c.identity_id, binding.c.department_id, binding.c.starts_on,
        binding.c.ends_on, organisation.c.timezone).select_from(
        binding.join(department, department.c.id == binding.c.department_id)
        .join(organisation, organisation.c.id == department.c.organisation_id)
        .join(identity, identity.c.id == binding.c.identity_id)
        .outerjoin(account, account.c.identity_id == identity.c.id)).where(
        binding.c.role.in_(("admin", "installation_admin")), binding.c.revoked.is_(False), identity.c.disabled.is_(False),
        sa.or_(account.c.id.is_(None), account.c.accepted_at.is_not(None))).order_by(
        (binding.c.role == "installation_admin").desc(), department.c.created_at, department.c.id,
        binding.c.created_at, binding.c.id)
    moment = datetime.now(timezone.utc)
    with connection.execute(query.execution_options(yield_per=100)) as rows:
        for holder in rows:
            day = moment.astimezone(ZoneInfo(holder.timezone)).date()
            if holder.starts_on <= day and (holder.ends_on is None or day < holder.ends_on):
                break
        else:
            return
    if holder.role == "installation_admin":
        return

    grant_id = str(uuid4())
    connection.execute(binding.insert().values(id=grant_id, revision=1, created_at=moment,
        identity_id=holder.identity_id, department_id=holder.department_id, role="installation_admin",
        starts_on=day, revoked=False))
    connection.execute(audit.insert().values(id=str(uuid4()), revision=1, created_at=moment,
        department_id=holder.department_id, actor_id=holder.identity_id, action="role.grant",
        target_id=grant_id, reason="Installation admin granted to the first administrator",
        detail={"role": "installation_admin", "source": "migration"}, correlation_id=revision))


def downgrade():
    raise RuntimeError("Forward-only: retain installation administrator grants and their audit history")
