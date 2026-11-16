"""The bounded score-review read used by the coordinator's advisory-line review."""
from apps.api import models as m
from apps.api.auth import require_role
from apps.api.snapshots import domain_snapshot
from packages.contracts import schemas as s
from packages.domain.score import score_review_lines, schedule_score
from packages.domain.validation import evaluate
from .common import scoped


def read(db, actor, workspace_id):
    require_role(actor, *s.PERMISSIONS['schedule.read_team'])
    workspace = scoped(db, m.Workspace, workspace_id, actor)
    data = domain_snapshot(db, workspace)
    validation = evaluate(data, explain_findings=False)
    scored = schedule_score(data, validation, data.get('generation_options'))
    result = score_review_lines(data, validation, data.get('generation_options'), scored)
    return s.ScoreReview(
        workspace_id=workspace.id,
        workspace_revision=workspace.revision,
        fingerprint=data['fingerprint'],
        score=result['score'],
        total_lost_points=result['total_lost_points'],
        lines=result['lines'],
    ).model_dump(mode='json')
