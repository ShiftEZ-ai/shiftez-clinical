"""Measure the actual history statement against the migrated catalogue prefix index."""
import copy
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import event, insert, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateIndex
from sqlalchemy.sql.elements import BindParameter
from sqlalchemy.sql.visitors import iterate

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.rule_catalogue import _history_keys
from .test_requests_search import both  # noqa: F401
from .test_rule_catalogue_api import approve, line, propose


@pytest.mark.parametrize('dialect', [sqlite.dialect(), postgresql.dialect()], ids=['sqlite', 'postgresql'])
def test_compiled_history_keys_match_index_ddl_and_keep_user_values_bound(dialect):
    index = next(index for index in m.Policy.__table__.indexes if index.name == 'ix_policy_catalogue_history')
    ddl = str(CreateIndex(index).compile(dialect=dialect))
    keys = _history_keys()
    for key, indexed in zip(keys, index.expressions[1:3]):
        rendered = str(key.compile(dialect=dialect, compile_kwargs={'include_table': False, 'render_postcompile': True}))
        assert rendered == str(indexed.compile(dialect=dialect, compile_kwargs={'include_table': False, 'literal_binds': True}))
        assert rendered in ddl
        original = [part for part in iterate(indexed) if isinstance(part, BindParameter)]
        rewritten = [part for part in iterate(key) if isinstance(part, BindParameter)]
        assert [(part.value, type(part.type)) for part in rewritten] == [(part.value, type(part.type)) for part in original]
        assert all(part.literal_execute for part in rewritten)
    department, entry, version = 'department-user-value', "entry'?-user-value", 909
    statement = select(m.Policy.id).where(m.Policy.department_id == department, keys[0] == entry, keys[1] == version)
    compiled = statement.compile(dialect=dialect, compile_kwargs={'render_postcompile': True})
    assert all(value in compiled.params.values() for value in (department, entry, version))
    assert all(str(value) not in str(compiled) for value in (department, entry, version))


def test_entry_history_exact_statement_plan_and_bounded_projection(both, tmp_path):
    h = both
    original = approve(h, propose(h, 'duties_per_week', [line(numbers={'max': {'value': 5}})])['record'])
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with transaction(h.app.state.engine, write=True) as db:
        base = db.get(m.Policy, original['id'])
        definition = copy.deepcopy(base.definition)
        # Explicit v1 and legacy missing-version rows must share the canonical v1 history.
        definition['rule'].pop('catalogue_version', None)
        other = m.Department(organisation_id=h.seed['organisation_id'], name='Fictional plan department')
        db.add(other)
        db.flush()
        records, relevant = [], []
        for n in range(2112):
            body = copy.deepcopy(definition)
            department = base.department_id
            if n >= 64:
                if n % 3 == 0:
                    body['rule']['id'] = 'hours_per_week'
                elif n % 3 == 1:
                    department = other.id
                else:
                    body['rule'].update(id='workload_limit', catalogue_version=2)
            elif n % 2:
                body['rule']['catalogue_version'] = 1
            row = dict(id=m.uid(), revision=2, department_id=department, name='Fictional retained version',
                kind='catalogue', definition=body, active=False, starts_on=date(2026, 1, 1), status='retired',
                owner='Fictional owner', source='Retained provenance', created_by=base.created_by,
                approved_by=base.approved_by, created_at=at + timedelta(days=n // 8))
            records.append(row)
            if n < 64:
                relevant.append(row)
        db.execute(insert(m.Policy), records)
    captured = []

    def capture(conn, cursor, statement, parameters, context, many):
        if 'FROM policies' in statement and 'ORDER BY' in statement and 'LIMIT' in statement:
            captured.append((statement, parameters, str(context.compiled.statement)))

    event.listen(h.app.state.engine, 'before_cursor_execute', capture)
    try:
        first = h.get('coordinator', '/api/rules/catalogue/duties_per_week/history?limit=7')
        second = h.get('coordinator', '/api/rules/catalogue/duties_per_week/history?limit=7&page=2')
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', capture)
    assert len(captured) == 2
    assert first['has_more'] and second['has_more']
    assert len(first['policies']) == len(second['policies']) == 7
    assert not {row['id'] for row in first['policies']} & {row['id'] for row in second['policies']}
    assert {row['id'] for row in first['policies'] + second['policies']} <= {original['id'], *(row['id'] for row in relevant)}
    # created_at remains primary and the immutable insertion ordinal breaks ties, newest first.
    with transaction(h.app.state.engine) as db:
        insertion = dict(db.execute(m.RecordInsertion.__table__.select().with_only_columns(
            m.RecordInsertion.record_id, m.RecordInsertion.seq).where(m.RecordInsertion.table_name == 'policies')).tuples().all())
    expected = [original['id']] + [row['id'] for row in sorted(relevant,
        key=lambda row: (row['created_at'], insertion[row['id']]), reverse=True)]
    assert [row['id'] for row in first['policies'] + second['policies']] == expected[:14]
    dialect = h.app.state.engine.dialect.name
    with h.app.state.engine.begin() as conn:
        conn.exec_driver_sql('ANALYZE policies')
        sql, parameters, ast = captured[0]
        prefix = 'EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ' if dialect == 'postgresql' else 'EXPLAIN QUERY PLAN '
        plan = [list(row) for row in conn.exec_driver_sql(prefix + sql, parameters)]
    evidence = {'dialect': dialect, 'fixture': {'relevant': 65, 'unrelated': 2048},
                'sql': sql, 'parameters': parameters, 'sqlalchemy_ast': ast, 'plan': plan,
                'page_size': len(first['policies']), 'fetch_limit': 8}
    (tmp_path / (dialect + '-history-plan.json')).write_text(json.dumps(evidence, indent=2, default=str))
    assert 'ix_policy_catalogue_history' in json.dumps(plan), plan
    if dialect == 'sqlite':
        search = next(row[-1] for row in plan if 'ix_policy_catalogue_history' in row[-1])
        assert 'department_id=? AND <expr>=? AND <expr>=?' in search
    else:
        def nodes(node):
            yield node
            for child in node.get('Plans', []):
                yield from nodes(child)
        scan = next(node for node in nodes(plan[0][0][0]['Plan']) if node.get('Index Name') == 'ix_policy_catalogue_history')
        assert all(key in scan['Index Cond'] for key in ('department_id', "'id'", 'COALESCE', "'catalogue_version'"))
