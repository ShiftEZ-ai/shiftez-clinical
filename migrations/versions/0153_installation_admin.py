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
        organisation.c.timezone, (account.c.id.is_not(None) & account.c.accepted_at.is_(None)).label("pending")).select_from(
        binding.join(department, department.c.id == binding.c.department_id)
        .join(organisation, organisation.c.id == department.c.organisation_id)
        .join(identity, identity.c.id == binding.c.identity_id)
        .outerjoin(account, account.c.identity_id == identity.c.id)).where(
        binding.c.role.in_(("admin", "installation_admin")), binding.c.revoked.is_(False),
        binding.c.ends_on.is_(None), identity.c.disabled.is_(False)).order_by(
        (binding.c.role == "installation_admin").desc(), department.c.created_at, department.c.id,
        binding.c.created_at, binding.c.id)
    moment = datetime.now(timezone.utc)
    holder = None
    pending = False
    with connection.execute(query.execution_options(yield_per=100)) as rows:
        for candidate in rows:
            day = moment.astimezone(ZoneInfo(candidate.timezone)).date()
            if candidate.starts_on > day:
                continue
            if candidate.pending:
                pending = True
                continue
            holder = candidate
            break
    if holder is None and connection.scalar(sa.select(organisation.c.id).limit(1)) is not None:
        if pending:
            raise RuntimeError("Complete the pending administrator setup before upgrading. If the setup link was lost or "
                               "expired, use reissue-first-admin on the installed version if bootstrap evidence is "
                               "available. Otherwise ask the installation owner for verified recovery before retrying.")
        raise RuntimeError("No active permanent administrator can receive installation administration. Restore an enabled "
                           "administrator with an open-ended grant on the installed version, then retry.")

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
    if holder is None or holder.role == "installation_admin":
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
