"""Expand staff wishes with additive working-pattern preferences, preserving existing wishes and outcomes."""
from alembic import op
import sqlalchemy as sa

revision = "0101_pattern_wishes"
down_revision = "0107_standing_wishes"
branch_labels = depends_on = None


def upgrade():
    connection = op.get_bind()
    table = sa.Table("staff_wishes", sa.MetaData(), autoload_with=connection)
    check = next(c for c in table.constraints if isinstance(c, sa.CheckConstraint) and "kind" in str(c.sqltext))
    check.name = check.name or "ck_staff_wishes_kind_legacy"
    # SQLite rebuilds for a widened check; retain every other constraint, index, row and existing trigger.
    triggers = list(connection.execute(sa.text("SELECT sql FROM sqlite_master WHERE type='trigger' "
                                               "AND tbl_name='staff_wishes' AND sql IS NOT NULL")).scalars()) \
        if connection.dialect.name == "sqlite" else []
    def expand():
        with op.batch_alter_table("staff_wishes", copy_from=table) as batch:
            batch.add_column(sa.Column("limit", sa.Integer(), nullable=True))
            batch.add_column(sa.Column("weekend_start", sa.Integer(), nullable=True))
            batch.add_column(sa.Column("weekend_days", sa.Integer(), nullable=True))
            batch.drop_constraint(check.name, type_="check")
            batch.create_check_constraint("ck_staff_wishes_kind", "kind IN ("
                                          "'prefer_off','prefer_on','prefer_shift_type','avoid_shift_type',"
                                          "'max_days_in_row','min_days_off_together','max_weekends_in_row','whole_weekends')")
        for trigger in triggers:
            op.execute(sa.text(trigger))

    if connection.dialect.name != "sqlite":
        expand()
        return
    # Referencing overrides prevent SQLite's batch copy with FK enforcement; switch outside a transaction.
    foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
    with op.get_context().autocommit_block():
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            connection.exec_driver_sql("SAVEPOINT pattern_wish_expand")
            try:
                expand()
                if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
                    raise RuntimeError("Pattern preference migration failed its foreign-key check")
            except BaseException:
                connection.exec_driver_sql("ROLLBACK TO SAVEPOINT pattern_wish_expand")
                raise
            finally:
                # A rolled-back savepoint must also close before SQLite accepts the restored FK setting.
                connection.exec_driver_sql("RELEASE SAVEPOINT pattern_wish_expand")
        finally:
            connection.exec_driver_sql(f"PRAGMA foreign_keys={foreign_keys}")


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
