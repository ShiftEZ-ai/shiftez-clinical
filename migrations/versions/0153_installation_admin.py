"""Widen role bindings for explicit installation administration and seed its first holder."""
import re
from zoneinfo import ZoneInfo

from alembic import op
import sqlalchemy as sa

from apps.api import models as m
from apps.api.auth import _live, has_installation_admin
from packages.contracts.schemas import INSTALLATION_ADMIN_ROLE, ROLE_NAMES

revision = "0153_installation_admin"
down_revision = "0152_feasibility_results"
branch_labels = depends_on = None
SAFETY_CLASS = "destructive"
DESTRUCTIVE_REVIEWED = (
    "Only widen the role_bindings role CHECK to include the contract roles and grant the first administrator "
    "with a role.grant audit row. The migration never changes or deletes an existing row. "
    "Verify a backup before deployment and restore the compatible database and image together if needed.")


def upgrade():
    connection = op.get_bind()
    table = sa.Table("role_bindings", sa.MetaData(), autoload_with=connection)
    check, = (constraint for constraint in table.constraints if isinstance(constraint, sa.CheckConstraint)
              and re.match(r'^\(*"?role\b', str(constraint.sqltext).strip()))
    if not all(f"'{role}'" in str(check.sqltext) for role in ROLE_NAMES):
        # The legacy SQLite check is unnamed. Name it on the reflected copy so batch mode can replace it.
        name = check.name or "ck_role_bindings_role"
        check.name = name
        with op.batch_alter_table(table.name, copy_from=table) as batch:
            batch.drop_constraint(name, type_="check")
            batch.create_check_constraint(name, "role IN (" + ",".join(f"'{role}'" for role in ROLE_NAMES) + ")")

    if has_installation_admin(connection):
        return
    binding = m.RoleBinding.__table__
    department, organisation = m.Department.__table__, m.Organisation.__table__
    identity, account = m.Identity.__table__, m.Account.__table__
    query = sa.select(binding.c.identity_id, binding.c.department_id, binding.c.starts_on, binding.c.ends_on,
        binding.c.revoked, organisation.c.timezone).select_from(
        binding.join(department).join(organisation).join(identity, identity.c.id == binding.c.identity_id)
        .outerjoin(account, account.c.identity_id == identity.c.id)).where(
        binding.c.role == "admin", binding.c.revoked.is_(False), identity.c.disabled.is_(False),
        sa.or_(account.c.id.is_(None), account.c.accepted_at.is_not(None))).order_by(
        department.c.created_at, department.c.id, binding.c.created_at, binding.c.id)
    moment = m.now()
    with connection.execute(query.execution_options(yield_per=100)) as rows:
        first = next(((admin, day) for admin in rows
                      if _live(admin, day := moment.astimezone(ZoneInfo(admin.timezone)).date())), None)
    if first is None:
        return
    admin, day = first
    grant_id = connection.execute(binding.insert().values(
        identity_id=admin.identity_id, department_id=admin.department_id,
        role=INSTALLATION_ADMIN_ROLE, starts_on=day).returning(binding.c.id)).scalar_one()
    connection.execute(m.AuditEvent.__table__.insert().values(
        department_id=admin.department_id, actor_id=admin.identity_id, action="role.grant",
        target_id=grant_id, reason="Installation admin granted to the first administrator",
        detail={"role": INSTALLATION_ADMIN_ROLE}, correlation_id=revision))


def downgrade():
    raise RuntimeError("Forward-only: retain installation administrator grants and their audit history")
