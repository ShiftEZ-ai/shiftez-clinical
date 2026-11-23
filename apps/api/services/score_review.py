"""The bounded score-review read used by the coordinator's advisory-line review."""
from apps.api import models as m
from apps.api.auth import require_role
from apps.api.snapshots import domain_snapshot
from packages.contracts import schemas as s
from packages.domain.score import score_review_lines, schedule_score
from packages.domain.validation import evaluate
from packages.domain.input_manifest import digest
from .common import scoped
from .read_projections import cached, CANONICAL_AUDIENCE
from .rota_summary import reusable_check


def read(db, actor, workspace_id):
    require_role(actor, *s.PERMISSIONS['schedule.read_team'])
    workspace = scoped(db, m.Workspace, workspace_id, actor)
    data = domain_snapshot(db, workspace, current_generation_defaults=workspace.status == 'published')
    options = data.get('generation_options')
    if reusable_check(workspace.validation, data['fingerprint'], options):
        validation = workspace.validation
        result = score_review_lines(data, validation, options, validation['score'])
    else:
        def build(_workspace_id):
            validation = evaluate(data, explain_findings=False)
            scored = schedule_score(data, validation, options)
            return score_review_lines(data, validation, options, scored)

        # The validation fingerprint omits labels that appear in the review.
        inputs = {key: value for key, value in data.items() if key not in ('input_manifest', '_person_rows')}
        stamp = digest([inputs, getattr(data, 'read_quality', None)])
        result = cached(db, [workspace.id], 'score_review', {workspace.id: stamp}, build,
                        audience=CANONICAL_AUDIENCE)[workspace.id]
    return s.ScoreReview(
        workspace_id=workspace.id,
        workspace_revision=workspace.revision,
        fingerprint=data['fingerprint'],
        score=result['score'],
        total_lost_points=result['total_lost_points'],
        lines=result['lines'],
    ).model_dump(mode='json')
