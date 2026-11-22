"""The bounded score-review read used by the coordinator's advisory-line review."""
from apps.api import models as m
from apps.api.auth import require_role
from apps.api.snapshots import domain_snapshot
from packages.contracts import schemas as s
from packages.domain.score import score_review_lines, schedule_score
from packages.domain.validation import evaluate
from .common import scoped
from .rota_summary import reusable_check


def read(db, actor, workspace_id):
    require_role(actor, *s.PERMISSIONS['schedule.read_team'])
    workspace = scoped(db, m.Workspace, workspace_id, actor)
    data = domain_snapshot(db, workspace, current_generation_defaults=workspace.status == 'published')
    options = data.get('generation_options')
    if reusable_check(workspace.validation, data['fingerprint'], options):
        validation = workspace.validation
        scored = validation['score']
    else:
        validation = evaluate(data, explain_findings=False)
        scored = schedule_score(data, validation, options)
    result = score_review_lines(data, validation, options, scored)
    return s.ScoreReview(
        workspace_id=workspace.id,
        workspace_revision=workspace.revision,
        fingerprint=data['fingerprint'],
        score=result['score'],
        total_lost_points=result['total_lost_points'],
        lines=result['lines'],
    ).model_dump(mode='json')
