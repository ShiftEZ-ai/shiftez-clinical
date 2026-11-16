"""Join the job-supersession and workspace-baseline migration branches."""

revision = "0143_merge_heads"
down_revision = ("0142_workspace_baselines", "0142_person_window_indexes")
branch_labels = depends_on = None
SAFETY_CLASS = "additive"


def upgrade():
    pass


def downgrade():
    raise RuntimeError("Forward-only: retain both integrated migration branches")
