"""PostgreSQL department-scale CPU budgets with generous contention ceilings."""
from datetime import date
import gc
from time import perf_counter, process_time

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from apps.api import models as m
from apps.api.db import transaction


pytestmark = pytest.mark.postgres


def budget(seconds):
    import coverage
    return seconds * (3 if coverage.Coverage.current() else 1)


def department(h, relationships=3000, *, operational=False):
    """Use real PostgreSQL rows; preparation is outside the measured operations."""
    first, last = date(2026, 1, 1), date(2027, 1, 1)
    with transaction(h.app.state.engine, write=True) as db:
        people = [m.Person(organisation_id=h.seed["organisation_id"], display_name=f"Synthetic bulk person {n:03d}",
                           short_code=f"P{n:03d}") for n in range(150)]
        services = [m.Service(department_id=h.seed["department_id"], name=f"Synthetic bulk clinic {n:03d}", starts_on=first)
                    for n in range((relationships + 149) // 150)]
        db.add_all([*people, *services])
        db.flush()
        db.add_all(m.Membership(department_id=h.seed["department_id"], person_id=person.id,
                               category_id=h.seed["category"], starts_on=first) for person in people)
        rows = [m.Eligibility(department_id=h.seed["department_id"], person_id=people[n % 150].id,
                              service_id=services[n // 150].id, starts_on=first, ends_on=last)
                for n in range(relationships)]
        db.add_all(rows)
        db.add_all(m.Policy(department_id=h.seed["department_id"], name=f"Synthetic scale rule {n}",
                            owner="Synthetic owner", source="Synthetic performance fixture", starts_on=first,
                            kind="max_duties", status="active" if operational and n < 20 else "retired", active=operational and n < 20,
                            definition={"rule": {"kind": "max_duties", "maximum": 20, "window": "week"},
                                        "scope": {}, "severity": "mandatory", "exception_allowed": False},
                            created_by=h.seed["identities"]["coordinator"]) for n in range(1000))
        db.flush()
        items = [{"target_id": row.id, "expected_revision": row.revision, "payload": {"ends_on": "2027-02-01"}} for row in rows]
    if operational:
        for number in range(2):
            workspace = h.workspace(f"Synthetic bulk operational rota {number}")
            workspace = h.shift(workspace)["workspace"]
            h.shift(workspace, start="2026-10-05T08:00:00+03:00", end="2026-10-05T12:00:00+03:00")
    return items


def measured(h, label, operation, seconds, timings, *, department_table=None):
    flushes, queries, target_preloads, department_reads = [], [], [], []

    def flushed(session, context):
        if session.info.get("bulk"):
            flushes.append(1)

    def queried(connection, cursor, statement, parameters, context, many):
        queries.append(statement.split(None, 1)[0].upper())
        if "from eligibilities" in statement.casefold() and "eligibilities.id in" in statement.casefold():
            target_preloads.append(1)
        if department_table and f"from {department_table}" in statement.casefold() and f"{department_table}.department_id =" in statement.casefold():
            department_reads.append(1)

    event.listen(Session, "after_flush", flushed)
    event.listen(h.app.state.engine, "before_cursor_execute", queried)
    gc.collect()  # Exclude earlier fixtures' garbage; collection stays enabled during the operation.
    started, cpu_started = perf_counter(), process_time()
    try:
        result = operation()
        elapsed = perf_counter() - started
        cpu_elapsed = process_time() - cpu_started
    finally:
        event.remove(Session, "after_flush", flushed)
        event.remove(h.app.state.engine, "before_cursor_execute", queried)
    print("bulk performance", {label: elapsed, "cpu_seconds": cpu_elapsed, "budget": budget(seconds),
                               "selects": queries.count("SELECT"), "flushes": len(flushes),
                               "department_reads": len(department_reads)})
    timings[label] = elapsed
    timings[label + "_cpu_seconds"] = cpu_elapsed
    timings[label + "_flushes"] = len(flushes)
    timings[label + "_target_preloads"] = len(target_preloads)
    timings[label + "_selects"] = queries.count("SELECT")
    timings[label + "_department_reads"] = len(department_reads)
    return result


@pytest.fixture(scope='module', params=[500, 2000])
def relationship_measurement(scale_harness, request):
    pg, count = scale_harness('pg'), request.param
    items = department(pg)[:count]
    seconds = 2 * count / 500
    timings = {}
    pg.client("credentialer")
    payload = {"item_kind": "eligibility.update_dates", "items": items, "reason": "Synthetic scale extension"}

    def preview():
        response = pg.client("credentialer").post("/api/commands/preview", json={"kind": "bulk.apply", "payload": payload})
        assert response.status_code == 200, response.text
        return response.json()

    labels = [f'{action}_{count}' for action in ('preview', 'apply', 'undo')]
    check = measured(pg, labels[0], preview, seconds, timings)
    result = measured(pg, labels[1], lambda: pg.command("credentialer", "bulk.apply", payload), seconds, timings)
    undo = measured(pg, labels[2], lambda: pg.command("credentialer", **result["undo"]), seconds, timings)
    with transaction(pg.app.state.engine) as db:
        ids = [item["target_id"] for item in items]
        restored = db.scalar(select(func.count()).select_from(m.Eligibility).where(
            m.Eligibility.id.in_(ids), m.Eligibility.ends_on == date(2027, 1, 1)))
    return count, labels, seconds, timings, check, result, undo, restored


def test_preview_apply_and_undo_at_department_scale(relationship_measurement, request):
    count, labels, _, metrics, check, result, undo, restored = relationship_measurement
    request.node.user_properties.extend(metrics.items())
    assert check["counts"]["ready"] == count and check["counts"]["problem"] == 0
    assert result["count"] == count and undo["count"] == count and restored == count
    assert all(metrics[label + "_selects"] <= 80 for label in labels), metrics
    assert all(metrics[label + "_flushes"] == 1 for label in labels), metrics
    assert all(metrics[label + "_target_preloads"] == 1 for label in labels), metrics


@pytest.mark.cpu_budget
def test_preview_apply_and_undo_cpu_budget(relationship_measurement):
    _, labels, seconds, timings, *_ = relationship_measurement
    assert all(timings[label + "_cpu_seconds"] <= budget(seconds) for label in labels), timings


@pytest.mark.wall_benchmark
def test_preview_apply_and_undo_wall_benchmark(relationship_measurement):
    _, labels, seconds, timings, *_ = relationship_measurement
    assert all(timings[label] <= 4 * budget(seconds) for label in labels), timings


@pytest.fixture(scope='module', params=[(kind, mode) for kind in ('eligibility', 'grant', 'membership')
                                       for mode in ('single', 'bulk')], ids=lambda value: '-'.join(value))
def relationship_creation_measurement(scale_harness, request):
    pg, (kind, mode) = scale_harness('pg'), request.param
    department(pg)
    cls, ref_cls, ref_key, role = {
        'eligibility': (m.Eligibility, m.Service, 'service_id', 'credentialer'),
        'grant': (m.CapabilityGrant, m.Capability, 'capability_id', 'credentialer'),
        'membership': (m.Membership, m.Category, 'category_id', 'coordinator'),
    }[kind]
    first, last = date(2026, 1, 1), date(2027, 1, 1)
    with transaction(pg.app.state.engine, write=True) as db:
        people = list(db.scalars(select(m.Person.id).where(m.Person.display_name.startswith('Synthetic bulk person'))))
        refs = [ref_cls(department_id=pg.seed['department_id'], name=f'Synthetic create {kind} reference {n}',
                        starts_on=first) for n in range(4 if kind == 'eligibility' else 23 if kind == 'membership' else 24)]
        db.add_all(refs)
        db.flush()
        if kind != 'eligibility':
            # department() already gave every person the seed rank; complete twenty references per person.
            old_refs = refs[:-4]
            db.add_all(cls(department_id=pg.seed['department_id'], person_id=person, **{ref_key: ref.id},
                starts_on=first, ends_on=last, **({'granted_by': pg.seed['identities'][role],
                    'evidence': 'Synthetic existing evidence'} if kind == 'grant' else {}))
                for person in people for ref in old_refs)
        ref_ids = [ref.id for ref in refs[-4:]]
    with transaction(pg.app.state.engine) as db:
        before = db.scalar(select(func.count()).select_from(cls).where(cls.person_id.in_(people)))
    dates = {'starts_on': first.isoformat(), 'ends_on': last.isoformat()}
    evidence = {'evidence': 'Synthetic verified evidence'} if kind == 'grant' else {}
    if mode == 'single':
        items = [{'payload': {'person_id': person, ref_key: ref, **dates, **evidence}}
                 for person in people for ref in ref_ids]
    elif kind == 'membership':
        items = [{'payload': {'person_ids': people, ref_key: ref, **dates}} for ref in ref_ids]
    else:
        items = [{'payload': {'person_ids': people, ref_key.replace('_id', '_ids'): ref_ids, **dates, **evidence}}]
    payload = {'item_kind': kind + ('.create' if mode == 'single' else '.bulk_create'),
               'items': items, 'reason': 'Synthetic create-path scale budget'}
    pg.client(role)
    seconds, label, timings = 2 * 600 / 500, f'{kind}_{mode}_create_600', {}
    result = measured(pg, label, lambda: pg.command(role, 'bulk.apply', payload), seconds, timings,
                      department_table=cls.__tablename__)
    with transaction(pg.app.state.engine) as db:
        after = db.scalar(select(func.count()).select_from(cls).where(cls.person_id.in_(people)))
    return label, seconds, timings, before, after, result, len(items)


def test_600_relationship_creates_have_bounded_reads_and_flushes(relationship_creation_measurement, request):
    label, _, metrics, before, after, result, item_count = relationship_creation_measurement
    request.node.user_properties.extend(metrics.items())
    assert before == 3000 and after == before + 600
    assert result['counts']['changed'] == item_count
    assert metrics[label + '_department_reads'] == 1, metrics
    # Allow one materialisation per new row plus queued history; fewer flushes are welcome.
    assert 1 <= metrics[label + '_flushes'] <= 601, metrics


@pytest.mark.cpu_budget
def test_600_relationship_creates_cpu_budget(relationship_creation_measurement):
    label, seconds, timings, *_ = relationship_creation_measurement
    assert timings[label + '_cpu_seconds'] <= budget(seconds), timings


@pytest.mark.wall_benchmark
def test_600_relationship_creates_wall_benchmark(relationship_creation_measurement):
    label, seconds, timings, *_ = relationship_creation_measurement
    assert timings[label] <= 4 * budget(seconds), timings


@pytest.fixture(scope='module', params=[False, True])
def resolver_measurement(scale_harness, request):
    pg, conditions = scale_harness('pg'), request.param
    department(pg, relationships=5000)
    pg.client("credentialer")

    def resolve():
        where = {"q": "Synthetic bulk person"}
        if conditions:
            where.update(as_of='2026-10-04', conditions={'match': 'all', 'conditions': [
                {'field': 'category_ids', 'op': 'any', 'values': [pg.seed['category']]},
                {'match': 'any', 'conditions': [{'field': 'service_ids', 'op': 'any',
                                               'values': [items_service]}]}]})
        response = pg.client("credentialer").post("/api/bulk/resolve", json={"collection": "eligibilities",
                                                                            "where": where})
        assert response.status_code == 200, response.text
        return response.json()

    with transaction(pg.app.state.engine) as db:
        items_service = db.scalar(select(m.Service.id).where(m.Service.name == 'Synthetic bulk clinic 000'))
    label = "resolve_conditions_5000" if conditions else "resolve_5000"
    timings = {}
    result = measured(pg, label, resolve, .3, timings)
    return conditions, label, timings, result


def test_resolver_5000_candidates(resolver_measurement, request):
    conditions, label, metrics, result = resolver_measurement
    request.node.user_properties.extend(metrics.items())
    assert result["count"] == (150 if conditions else 5000) and len(result["sample"]) == 100
    assert metrics[label + "_selects"] <= 80, metrics


@pytest.mark.cpu_budget
def test_resolver_5000_candidates_budget(resolver_measurement):
    _, label, timings, _ = resolver_measurement
    assert timings[label + "_cpu_seconds"] <= budget(.3), timings


@pytest.mark.wall_benchmark
def test_resolver_5000_candidates_wall_benchmark(resolver_measurement):
    _, label, timings, _ = resolver_measurement
    assert timings[label] <= 4 * budget(.3), timings


@pytest.fixture(scope='module', params=['catalogue', 'tag'])
def people_measurement(scale_harness, request):
    pg, action = scale_harness('pg'), request.param
    department(pg, operational=True)
    with transaction(pg.app.state.engine) as db:
        people = list(db.scalars(select(m.Person).where(m.Person.display_name.startswith('Synthetic bulk person'))))
        records = [{'target_id': person.id, 'expected_revision': person.revision} for person in people]
    if action == 'tag':
        tag = pg.configure('tags', {'name': 'Synthetic scale team', 'labels': ['people']})['record']
        changes = [{'action': 'tag', 'how': 'add', 'value': [tag['id']]}]
    else:
        changes = [{'field': 'active', 'value': False}]
    payload = {'item_kind': 'catalogue.update', 'common': {'catalogue': 'people'}, 'changes': changes,
               'items': records, 'reason': 'Synthetic complete collection scale budget'}
    pg.client('coordinator')
    def preview():
        response = pg.client('coordinator').post('/api/commands/preview', json={'kind': 'bulk.apply', 'payload': payload})
        assert response.status_code == 200, response.text
        return response.json()
    timings = {}
    checked = measured(pg, f'{action}_preview_150', preview, 2, timings)
    changed = measured(pg, f'{action}_apply_150', lambda: pg.command('coordinator', 'bulk.apply', payload),
                       2, timings)
    undone = measured(pg, f'{action}_undo_150', lambda: pg.command('coordinator', **changed['undo']),
                      2, timings)
    with transaction(pg.app.state.engine) as db:
        restored = list(db.scalars(select(m.Person).where(m.Person.id.in_([person['target_id'] for person in records]))))
        restored_state = [(person.active, person.tags) for person in restored]
    return action, records, timings, checked, changed, undone, restored_state


def test_150_people_changes_with_current_rotas_and_rule_history(people_measurement, request):
    action, records, metrics, checked, changed, undone, restored = people_measurement
    request.node.user_properties.extend(metrics.items())
    assert len(records) == 150
    assert checked['counts']['ready'] == 150 and checked['counts']['problem'] == 0
    assert changed['count'] == 150 and undone['count'] == 150
    assert metrics[f'{action}_preview_150_selects'] <= 400, metrics
    assert all(metrics[f'{action}_{phase}_150_selects'] <= 800 for phase in ('apply', 'undo')), metrics
    assert [metrics[f'{action}_{phase}_150_flushes'] for phase in ('preview', 'apply', 'undo')] == [1, 2, 1], metrics
    assert all(active and not tags for active, tags in restored)


@pytest.mark.cpu_budget
def test_150_people_changes_cpu_budget(people_measurement):
    action, _, timings, *_ = people_measurement
    assert all(timings[label + '_cpu_seconds'] <= budget(2)
               for label in (f'{action}_preview_150', f'{action}_apply_150', f'{action}_undo_150')), timings


@pytest.mark.wall_benchmark
def test_150_people_changes_wall_benchmark(people_measurement):
    action, _, timings, *_ = people_measurement
    assert all(timings[label] <= 4 * budget(2)
               for label in (f'{action}_preview_150', f'{action}_apply_150', f'{action}_undo_150')), timings


@pytest.fixture(scope='module', params=['catalogue', 'membership'])
def creation_measurement(scale_harness, request):
    pg, kind = scale_harness('pg'), request.param
    pg.client('admin')
    if kind == 'catalogue':
        common = {'catalogue': 'services'}
        items = [{'payload': {'value': {'name': f'Synthetic disposable clinic {n}', 'starts_on': '2020-01-01'}}}
                 for n in range(500)]
    else:
        with transaction(pg.app.state.engine, write=True) as db:
            categories = [m.Category(department_id=pg.seed['department_id'], name=f'Synthetic disposable rank {n}',
                                     starts_on=date(2020, 1, 1)) for n in range(500)]
            db.add_all(categories)
            db.flush()
            items = [{'payload': {'person_id': pg.seed['people'][0], 'category_id': category.id,
                                  'starts_on': '2026-10-01'}} for category in categories]
        common = {}
    created = pg.command('admin', 'bulk.apply', {'item_kind': kind + '.create', 'common': common,
                         'items': items, 'reason': 'Synthetic batch reference budget'})
    timings = {}
    label = f'{kind}_create_undo_500'
    result = measured(pg, label, lambda: pg.command('admin', **created['undo']), 2, timings)
    with transaction(pg.app.state.engine) as db:
        cls = m.Service if kind == 'catalogue' else m.Membership
        remaining = db.scalar(select(func.count()).select_from(cls).where(
            cls.id.in_([row['record']['id'] for row in created['items']])))
    return label, timings, result, remaining


def test_500_creation_undo_reference_checks(creation_measurement, request):
    label, metrics, result, remaining = creation_measurement
    request.node.user_properties.extend(metrics.items())
    assert result['count'] == 500 and remaining == 0
    assert metrics[label + '_selects'] <= 80, metrics
    assert metrics[label + '_flushes'] == 1, metrics


@pytest.mark.cpu_budget
def test_500_creation_undo_reference_budget(creation_measurement):
    label, timings, _, _ = creation_measurement
    assert timings[label + '_cpu_seconds'] <= budget(2), timings


@pytest.mark.wall_benchmark
def test_500_creation_undo_reference_wall_benchmark(creation_measurement):
    label, timings, _, _ = creation_measurement
    assert timings[label] <= 4 * budget(2), timings
