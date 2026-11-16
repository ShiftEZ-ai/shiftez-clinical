from sqlalchemy import select

from apps.api import models as m
from apps.api.errors import require


def require_generation_sessions_in_scope(session_ids, *, db=None, workspace=None, sessions=None):
    if not session_ids:
        return
    if sessions is None:
        sessions = db.execute(select(m.Shift.id, m.Shift.cancelled).where(m.Shift.workspace_id == workspace.id))
    selected = {session_id for session_id, cancelled in sessions if not cancelled}
    require(set(session_ids).issubset(selected), 'INVALID_SCOPE', 'Select sessions from this draft')
