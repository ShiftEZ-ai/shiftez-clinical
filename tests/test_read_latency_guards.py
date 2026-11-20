"""Pure guards for the bounded first-read paths (CI runs the API fixtures)."""
from collections import namedtuple
from types import SimpleNamespace
from uuid import uuid4

from apps.api import models as m, snapshots
from packages.domain import input_manifest
from packages.domain.rota_summary import cause_summary


Row = namedtuple('Row', 'id revision catalogue_revision input_revision availability_revision')


class _CurrencyDB:
    def __init__(self, department, organisation):
        self.department = department
        self.organisation = organisation
        self.gets = []
        self.revision_reads = 0

    def get(self, cls, ident):
        self.gets.append((cls, ident))
        if cls is m.Department:
            return self.department
        if cls is m.Organisation:
            return self.organisation
        raise AssertionError(cls)

    def execute(self, _query):
        # One joined column read per organisation: current department and organisation revisions.
        self.revision_reads += 1
        row = Row(self.department.id, 1, 1, self.organisation.input_revision, self.organisation.availability_revision)
        return SimpleNamespace(all=lambda: [row])


def test_cheap_fingerprints_never_infers_currency_from_a_stale_hint():
    department_id, organisation_id, workspace_id = (str(uuid4()) for _ in range(3))
    department = SimpleNamespace(id=department_id, organisation_id=organisation_id)
    organisation = SimpleNamespace(id=organisation_id, input_revision=4, availability_revision=9)
    workspace = SimpleNamespace(id=workspace_id, department_id=department_id, content_revision=12)
    token = 'a' * 64
    toolchain = input_manifest.digest(input_manifest.toolchain(snapshots.quota_policy_limit()))
    matching = {'input_manifest': {'token': token, 'dirty_hint': {
        'input_revision': 4, 'availability_revision': 9, 'content_revision': 12,
        'department_revisions': [[department_id, 1, 1]], 'baseline': [None, None]},
        'components': {'toolchain': toolchain}}}
    db = _CurrencyDB(department, organisation)
    assert snapshots.cheap_fingerprints(db, [workspace], checks={workspace_id: matching}) == {workspace_id: token}
    matching['input_manifest']['dirty_hint']['content_revision'] = 11
    assert snapshots.cheap_fingerprints(db, [workspace], checks={workspace_id: matching}) == {}


def test_cheap_fingerprints_reuses_shared_currency_rows():
    department_id, organisation_id = (str(uuid4()) for _ in range(2))
    department = SimpleNamespace(id=department_id, organisation_id=organisation_id)
    organisation = SimpleNamespace(id=organisation_id, input_revision=4, availability_revision=9)
    workspaces = [SimpleNamespace(id=str(uuid4()), department_id=department_id, content_revision=12)
                  for _ in range(3)]
    toolchain = input_manifest.digest(input_manifest.toolchain(snapshots.quota_policy_limit()))
    checks = {workspace.id: {'input_manifest': {'token': 'a' * 64, 'dirty_hint': {
        'input_revision': 4, 'availability_revision': 9, 'content_revision': 12,
        'department_revisions': [[department_id, 1, 1]], 'baseline': [None, None]},
        'components': {'toolchain': toolchain}}} for workspace in workspaces}
    db = _CurrencyDB(department, organisation)
    assert set(snapshots.cheap_fingerprints(db, workspaces, checks=checks)) == {w.id for w in workspaces}
    assert db.gets == [(m.Department, department_id)] and db.revision_reads == 1


def test_cause_summary_keeps_the_small_projection_free_of_session_rows():
    shift_id = str(uuid4())
    finding = {'code': 'COVERAGE_GAP', 'severity': 'error', 'shift_id': shift_id,
               'start': '2026-11-01T08:00:00+00:00', 'end': '2026-11-01T09:00:00+00:00',
               'cause': {'code': 'capacity_short', 'required': 1, 'eligible_people': 0},
               'fix_hints': []}
    result = cause_summary({'fingerprint': 'b' * 64, 'findings': [finding],
                            'coverage': [], 'error_count': 1, 'warning_count': 0}, [])
    assert set(result) == {'observations', 'repair_tasks', 'cause_groups'}
    assert result['repair_tasks'] and result['cause_groups']
    assert all('sessions' not in row for row in result['observations'])
