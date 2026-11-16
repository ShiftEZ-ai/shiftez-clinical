"""Real database and validator budgets for the worker's department-sized audience job."""
import gc
import json
import time
from datetime import date, timedelta

import pytest
from sqlalchemy import event, insert, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import staffing, swaps
from apps.api.snapshots import domain_snapshot, snapshot_hash
from packages.domain import validation as validator
from packages.domain.validation import evaluate
from .governance_support import identity, transition
from .test_performance_budget import python_budget
from .test_staff_swaps import fixed_clock, ordered_clock  # noqa: F401


@pytest.fixture(scope="module", params=['h', pytest.param('pg', marks=pytest.mark.postgres)], ids=['sqlite', 'postgresql'])
def department_swap_audiences(scale_harness, request):
    backend = request.param
    h = scale_harness(backend)
    with pytest.MonkeyPatch.context() as monkeypatch, ordered_clock(monkeypatch):
        identity(h, 'staff_two', ['staff'], h.seed['people'][1])
        identity(h, 'staff_three', ['staff'], h.seed['people'][2])
        workspace = h.shift(h.workspace(start='2026-10-04', end='2026-11-01'))['workspace']
        with transaction(h.app.state.engine, write=True) as db:
            extra = [m.Person(organisation_id=h.seed['organisation_id'], display_name=f'Synthetic audience staff {i:03}')
                     for i in range(147)]
            db.add_all(extra)
            db.flush()
            people = list(h.seed['people']) + [person.id for person in extra]
            accounts = [m.Identity(issuer='local-development', subject=person.id, person_id=person.id,
                                   display_name=person.display_name) for person in extra]
            db.add_all(accounts)
            db.flush()
            db.add_all(m.RoleBinding(identity_id=account.id, department_id=h.seed['department_id'],
                                     role='staff', starts_on=date(2020, 1, 1)) for account in accounts)
            for model in (m.Membership, m.Eligibility, m.CapabilityGrant):
                originals = list(db.execute(select(model.__table__).where(model.person_id == people[0])).mappings())
                db.execute(insert(model), [{**dict(row), 'id': m.uid(), 'person_id': person.id}
                                           for person in extra for row in originals])
            db.get(m.Location, h.seed['locations'][0]).capacity = 1000
            template = db.scalar(select(m.Shift).where(m.Shift.workspace_id == workspace['id']))
            first = db.scalar(select(m.Assignment.id).where(m.Assignment.shift_id == template.id))
            shifts, assignments, services = [], [], []
            for person_id in people:
                # The proposer's other nine duties do not overlap any offered return duty.
                days = range(15, 24) if person_id == people[0] else range(5, 15)
                for day in days:
                    start = template.start.replace(day=day)
                    end = template.end.replace(day=day)
                    shift_id = m.uid()
                    shifts.append({'id': shift_id, 'workspace_id': workspace['id'],
                        'assignment_type_id': template.assignment_type_id, 'type_revision': template.type_revision,
                        'type_snapshot': template.type_snapshot, 'location_id': template.location_id,
                        'start': start, 'end': end})
                    assignments.append({'shift_id': shift_id, 'person_id': person_id, 'start': start, 'end': end})
                    services.append({'shift_id': shift_id, 'service_id': h.seed['services'][0]})
            db.execute(insert(m.Shift), shifts)
            db.execute(insert(m.Assignment), assignments)
            db.execute(insert(m.ShiftService), services)
            db.flush()
            stored = db.get(m.Workspace, workspace['id'])
            snapshot = domain_snapshot(db, stored, include_fairness=False)
            validation = evaluate(snapshot)
            dimensions = len(snapshot['people']), len(snapshot['shifts'])
            stored.status = 'published'
            publication = m.Publication(workspace_id=stored.id, department_id=stored.department_id, sequence=1,
                starts_on=stored.starts_on, ends_on=stored.ends_on, current=True, published_by=stored.created_by,
                snapshot=snapshot, validation=validation, fingerprint=snapshot_hash(snapshot))
            db.add(publication)
            db.flush()
            db.add_all(m.PublicationPerson(publication_id=publication.id, person_id=person_id) for person_id in people)
            publication_id = publication.id

        # Every blocked exchange recipient has ten valid future return duties to exhaust; one remains eligible.
        h.policy({'kind': 'restriction', 'weekdays': [6], 'start_time': '08:00', 'end_time': '12:00'},
                 {'person_ids': people[1:-1]})
        preparations, policy_groups = [], []
        prepare, group = staffing.prepare_assignment_trials, validator.policy_group_tiers

        def prepare_context(data):
            preparations.append(True)
            return prepare(data)

        def prepare_groups(policies):
            policy_groups.append(True)
            return group(policies)

        monkeypatch.setattr(staffing, 'prepare_assignment_trials', prepare_context)
        monkeypatch.setattr(validator, 'policy_group_tiers', prepare_groups)
        timings, results = {}, {}
        for kind in ('exchange', 'give'):
            row = h.command('staff', 'swap.offer', {'publication_id': publication_id, 'assignment_id': first,
                'kind': kind, 'expires_at': (m.now() + timedelta(days=1)).isoformat(),
                'reason': 'Synthetic audience performance fixture'})['swap']
            reads = []
            preparations.clear()
            policy_groups.clear()

            def count_read(_connection, _cursor, statement, _parameters, _context, _many):
                if statement.lstrip().upper().startswith('SELECT'):
                    reads.append(True)

            gc.collect()
            event.listen(h.app.state.engine, 'before_cursor_execute', count_read)
            began, cpu = time.perf_counter(), time.process_time()
            try:
                with transaction(h.app.state.engine) as db:
                    result = swaps.compute_audience(db, {'swap_id': row['id']})
                timings[kind] = {'wall_seconds': time.perf_counter() - began,
                                 'cpu_seconds': time.process_time() - cpu, 'selects': len(reads),
                                 'preparations': len(preparations), 'policy_groups': len(policy_groups)}
            finally:
                event.remove(h.app.state.engine, 'before_cursor_execute', count_read)
            results[kind] = result
            transition(h, row, 'withdraw')
        return backend, people, results, timings, validation, dimensions


def test_audience_for_150_staff_with_ten_duties_meets_department_budget(department_swap_audiences, request):
    backend, people, results, timings, validation, dimensions = department_swap_audiences
    assert validation['error_count'] == 0, validation['findings']
    assert dimensions == (150, 1500)
    for kind, result in results.items():
        assert result == {'person_ids': [people[-1]], 'no_eligible_recipients_reason': None}
        # Bounded reads catch per-recipient database work independently of runner speed.
        assert timings[kind]['selects'] < 100, timings
        assert timings[kind]['preparations'] == timings[kind]['policy_groups'] == 1, timings
    request.node.user_properties.append(('swap_audience_performance', json.dumps(timings)))
    print('swap audience department performance', {'backend': backend, 'staff': 150, 'duties': 1500, **timings})


@pytest.mark.cpu_budget
def test_audience_for_150_staff_with_ten_duties_meets_department_budget_cpu_budget(department_swap_audiences):
    _, _, _, timings, _, _ = department_swap_audiences
    for kind, ceiling in (('exchange', 5), ('give', 5)):
        assert timings[kind]['cpu_seconds'] < python_budget(ceiling), timings
