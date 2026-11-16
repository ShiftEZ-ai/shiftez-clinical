"""Keep configured duty short codes beside the versioned duty definition."""
from alembic import op
import sqlalchemy as sa


revision = '0139_duty_short_code'
down_revision = '0138_contact_action_off'
branch_labels = depends_on = None
SAFETY_CLASS = 'fill_only'


# The update is deliberately bounded by the primary key subquery. It is
# rerunnable: a populated short_code is never overwritten. The first row for
# a derived code keeps the current three-initial display; later rows receive a
# stable numeric suffix, matching the existing dense-view code family without
# changing any unique duty's code.
_SQLITE_FILL = """
UPDATE assignment_types
SET short_code = CASE
    WHEN UPPER(SUBSTR(TRIM(name), 1, 1) ||
         CASE WHEN INSTR(TRIM(name), ' ') > 0
              THEN SUBSTR(TRIM(name), INSTR(TRIM(name), ' ') + 1, 1) ELSE '' END ||
         CASE WHEN INSTR(SUBSTR(TRIM(name), INSTR(TRIM(name), ' ') + 1), ' ') > 0
              THEN SUBSTR(TRIM(name), INSTR(TRIM(name), ' ') +
                   INSTR(SUBSTR(TRIM(name), INSTR(TRIM(name), ' ') + 1), ' ') + 1, 1)
              ELSE '' END) = '' THEN ''
    WHEN (SELECT COUNT(*) FROM assignment_types AS prior
          WHERE prior.short_code IS NULL
            AND prior.department_id = assignment_types.department_id
            AND UPPER(SUBSTR(TRIM(prior.name), 1, 1) ||
                CASE WHEN INSTR(TRIM(prior.name), ' ') > 0
                     THEN SUBSTR(TRIM(prior.name), INSTR(TRIM(prior.name), ' ') + 1, 1) ELSE '' END ||
                CASE WHEN INSTR(SUBSTR(TRIM(prior.name), INSTR(TRIM(prior.name), ' ') + 1), ' ') > 0
                     THEN SUBSTR(TRIM(prior.name), INSTR(TRIM(prior.name), ' ') +
                          INSTR(SUBSTR(TRIM(prior.name), INSTR(TRIM(prior.name), ' ') + 1), ' ') + 1, 1)
                     ELSE '' END) =
                UPPER(SUBSTR(TRIM(assignment_types.name), 1, 1) ||
                CASE WHEN INSTR(TRIM(assignment_types.name), ' ') > 0
                     THEN SUBSTR(TRIM(assignment_types.name), INSTR(TRIM(assignment_types.name), ' ') + 1, 1) ELSE '' END ||
                CASE WHEN INSTR(SUBSTR(TRIM(assignment_types.name), INSTR(TRIM(assignment_types.name), ' ') + 1), ' ') > 0
                     THEN SUBSTR(TRIM(assignment_types.name), INSTR(TRIM(assignment_types.name), ' ') +
                          INSTR(SUBSTR(TRIM(assignment_types.name), INSTR(TRIM(assignment_types.name), ' ') + 1), ' ') + 1, 1)
                     ELSE '' END)
            AND (prior.name, prior.id) < (assignment_types.name, assignment_types.id)) = 0
    THEN UPPER(SUBSTR(TRIM(name), 1, 1) ||
         CASE WHEN INSTR(TRIM(name), ' ') > 0
              THEN SUBSTR(TRIM(name), INSTR(TRIM(name), ' ') + 1, 1) ELSE '' END ||
         CASE WHEN INSTR(SUBSTR(TRIM(name), INSTR(TRIM(name), ' ') + 1), ' ') > 0
              THEN SUBSTR(TRIM(name), INSTR(TRIM(name), ' ') +
                   INSTR(SUBSTR(TRIM(name), INSTR(TRIM(name), ' ') + 1), ' ') + 1, 1)
              ELSE '' END)
    ELSE SUBSTR(UPPER(SUBSTR(TRIM(name), 1, 1) ||
         CASE WHEN INSTR(TRIM(name), ' ') > 0
              THEN SUBSTR(TRIM(name), INSTR(TRIM(name), ' ') + 1, 1) ELSE '' END ||
         CASE WHEN INSTR(SUBSTR(TRIM(name), INSTR(TRIM(name), ' ') + 1), ' ') > 0
              THEN SUBSTR(TRIM(name), INSTR(TRIM(name), ' ') +
                   INSTR(SUBSTR(TRIM(name), INSTR(TRIM(name), ' ') + 1), ' ') + 1, 1)
              ELSE '' END), 1, 2) || CAST((SELECT COUNT(*) FROM assignment_types AS prior
          WHERE prior.short_code IS NULL
            AND prior.department_id = assignment_types.department_id
            AND UPPER(SUBSTR(TRIM(prior.name), 1, 1) ||
                CASE WHEN INSTR(TRIM(prior.name), ' ') > 0
                     THEN SUBSTR(TRIM(prior.name), INSTR(TRIM(prior.name), ' ') + 1, 1) ELSE '' END ||
                CASE WHEN INSTR(SUBSTR(TRIM(prior.name), INSTR(TRIM(prior.name), ' ') + 1), ' ') > 0
                     THEN SUBSTR(TRIM(prior.name), INSTR(TRIM(prior.name), ' ') +
                          INSTR(SUBSTR(TRIM(prior.name), INSTR(TRIM(prior.name), ' ') + 1), ' ') + 1, 1)
                     ELSE '' END) =
                UPPER(SUBSTR(TRIM(assignment_types.name), 1, 1) ||
                CASE WHEN INSTR(TRIM(assignment_types.name), ' ') > 0
                     THEN SUBSTR(TRIM(assignment_types.name), INSTR(TRIM(assignment_types.name), ' ') + 1, 1) ELSE '' END ||
                CASE WHEN INSTR(SUBSTR(TRIM(assignment_types.name), INSTR(TRIM(assignment_types.name), ' ') + 1), ' ') > 0
                     THEN SUBSTR(TRIM(assignment_types.name), INSTR(TRIM(assignment_types.name), ' ') +
                          INSTR(SUBSTR(TRIM(assignment_types.name), INSTR(TRIM(assignment_types.name), ' ') + 1), ' ') + 1, 1)
                     ELSE '' END)
            AND (prior.name, prior.id) < (assignment_types.name, assignment_types.id)) + 1 AS TEXT)
END
WHERE short_code IS NULL
  AND id IN (SELECT id FROM assignment_types WHERE short_code IS NULL LIMIT 1000)
"""


_POSTGRES_FILL = """
UPDATE assignment_types
SET short_code = CASE
    WHEN UPPER(SUBSTR(TRIM(name), 1, 1) ||
         CASE WHEN SPLIT_PART(TRIM(name), ' ', 2) <> ''
              THEN SUBSTR(SPLIT_PART(TRIM(name), ' ', 2), 1, 1) ELSE '' END ||
         CASE WHEN SPLIT_PART(TRIM(name), ' ', 3) <> ''
              THEN SUBSTR(SPLIT_PART(TRIM(name), ' ', 3), 1, 1) ELSE '' END) = '' THEN ''
    WHEN (SELECT COUNT(*) FROM assignment_types AS prior
          WHERE prior.short_code IS NULL
            AND prior.department_id = assignment_types.department_id
            AND UPPER(SUBSTR(TRIM(prior.name), 1, 1) ||
                CASE WHEN SPLIT_PART(TRIM(prior.name), ' ', 2) <> ''
                     THEN SUBSTR(SPLIT_PART(TRIM(prior.name), ' ', 2), 1, 1) ELSE '' END ||
                CASE WHEN SPLIT_PART(TRIM(prior.name), ' ', 3) <> ''
                     THEN SUBSTR(SPLIT_PART(TRIM(prior.name), ' ', 3), 1, 1) ELSE '' END) =
                UPPER(SUBSTR(TRIM(assignment_types.name), 1, 1) ||
                CASE WHEN SPLIT_PART(TRIM(assignment_types.name), ' ', 2) <> ''
                     THEN SUBSTR(SPLIT_PART(TRIM(assignment_types.name), ' ', 2), 1, 1) ELSE '' END ||
                CASE WHEN SPLIT_PART(TRIM(assignment_types.name), ' ', 3) <> ''
                     THEN SUBSTR(SPLIT_PART(TRIM(assignment_types.name), ' ', 3), 1, 1) ELSE '' END)
            AND (prior.name, prior.id) < (assignment_types.name, assignment_types.id)) = 0
    THEN UPPER(SUBSTR(TRIM(name), 1, 1) ||
         CASE WHEN SPLIT_PART(TRIM(name), ' ', 2) <> ''
              THEN SUBSTR(SPLIT_PART(TRIM(name), ' ', 2), 1, 1) ELSE '' END ||
         CASE WHEN SPLIT_PART(TRIM(name), ' ', 3) <> ''
              THEN SUBSTR(SPLIT_PART(TRIM(name), ' ', 3), 1, 1) ELSE '' END)
    ELSE SUBSTR(UPPER(SUBSTR(TRIM(name), 1, 1) ||
         CASE WHEN SPLIT_PART(TRIM(name), ' ', 2) <> ''
              THEN SUBSTR(SPLIT_PART(TRIM(name), ' ', 2), 1, 1) ELSE '' END ||
         CASE WHEN SPLIT_PART(TRIM(name), ' ', 3) <> ''
              THEN SUBSTR(SPLIT_PART(TRIM(name), ' ', 3), 1, 1) ELSE '' END), 1, 2) || CAST((SELECT COUNT(*) FROM assignment_types AS prior
          WHERE prior.short_code IS NULL
            AND prior.department_id = assignment_types.department_id
            AND UPPER(SUBSTR(TRIM(prior.name), 1, 1) ||
                CASE WHEN SPLIT_PART(TRIM(prior.name), ' ', 2) <> ''
                     THEN SUBSTR(SPLIT_PART(TRIM(prior.name), ' ', 2), 1, 1) ELSE '' END ||
                CASE WHEN SPLIT_PART(TRIM(prior.name), ' ', 3) <> ''
                     THEN SUBSTR(SPLIT_PART(TRIM(prior.name), ' ', 3), 1, 1) ELSE '' END) =
                UPPER(SUBSTR(TRIM(assignment_types.name), 1, 1) ||
                CASE WHEN SPLIT_PART(TRIM(assignment_types.name), ' ', 2) <> ''
                     THEN SUBSTR(SPLIT_PART(TRIM(assignment_types.name), ' ', 2), 1, 1) ELSE '' END ||
                CASE WHEN SPLIT_PART(TRIM(assignment_types.name), ' ', 3) <> ''
                     THEN SUBSTR(SPLIT_PART(TRIM(assignment_types.name), ' ', 3), 1, 1) ELSE '' END)
            AND (prior.name, prior.id) < (assignment_types.name, assignment_types.id)) + 1 AS TEXT)
END
WHERE short_code IS NULL
  AND id IN (SELECT id FROM assignment_types WHERE short_code IS NULL LIMIT 1000)
"""


def upgrade():
    op.add_column('assignment_types', sa.Column('short_code', sa.String(6), nullable=True))
    if op.get_bind().dialect.name == 'sqlite':
        op.execute(_SQLITE_FILL)
    else:
        op.execute(_POSTGRES_FILL)


def downgrade():
    raise RuntimeError('Forward-only: retain configured duty codes in snapshots and history')
