"""Keep input-table DML on the session event path; exercise the guard against bypasses."""
import ast
from pathlib import Path
import re

import pytest

from apps.api import models as m, snapshots


ROOT = Path(__file__).parents[1]
MODEL_TABLES = {mapper.class_.__name__: mapper.local_table.name for mapper in m.Base.registry.mappers}
INPUT_TABLES = {cls.__tablename__ for _, cls, *_ in snapshots.snapshot_input_specs()}
NON_INPUT_CORE_TABLES = {
    m.Session.__tablename__: 'Authentication liveness writes do not change scheduling inputs.',
    m.RateLimit.__tablename__: 'Request admission counters do not change scheduling inputs.',
    m.FeedbackReport.__tablename__: 'Expired feedback screenshot retention does not change scheduling inputs.',
    m.LoginAttempt.__tablename__: 'Expired authentication throttling records are not scheduling inputs.',
    m.AuthLoginEvent.__tablename__: 'Authentication history retention is independent of scheduling inputs.',
    m.WorkspaceReadProjection.__tablename__: 'Derived read-cache upserts never write canonical inputs.',
    m.ReadProjectionWarmCursor.__tablename__: 'Derived warm-cursor and supersession done-marker progress never changes scheduling inputs.',
    m.JobCandidateEvidence.__tablename__: 'Completed job evidence is a derived output independent of scheduling inputs.',
}


RAW_LEDGER_ADAPTER = ('apps/api/services/fairness.py', '_write_ledger_rows')


def input_write_bypasses(source, path=None):
    tree = ast.parse(source)
    failures, writers = [], []
    scopes = [tree, *[node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]]
    for scope in scopes:
        # A function's assignments must not inherit unrelated functions' local aliases.
        nodes = []
        def walk(node):
            if node is not scope and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return
            nodes.append(node)
            for child in ast.iter_child_nodes(node):
                walk(child)
        walk(scope)
        stamped_adapter = (path, getattr(scope, 'name', None)) == RAW_LEDGER_ADAPTER and any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == 'bump_snapshot_input_revisions' for node in nodes)
        def allowed_adapter(targets):
            return stamped_adapter and targets == {m.FairnessLedger.__tablename__}
        aliases = {}
        for node in [*tree.body, *nodes]:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        aliases[target.id] = node.value
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in MODEL_TABLES:
                        aliases[alias.asname or alias.name] = ast.Constant(MODEL_TABLES[alias.name])
        core_names = {'connection', 'conn'}
        for node in nodes:
            if isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    call = item.context_expr
                    if isinstance(item.optional_vars, ast.Name) and isinstance(call, ast.Call):
                        if isinstance(call.func, ast.Attribute) and call.func.attr in {'begin', 'connect', 'connection'}:
                            core_names.add(item.optional_vars.id)
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                if isinstance(node.value.func, ast.Attribute) and node.value.func.attr in {'connect', 'connection'}:
                    core_names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        while True:
            propagated = {name for name, value in aliases.items() if isinstance(value, ast.Name) and value.id in core_names}
            if propagated <= core_names:
                break
            core_names.update(propagated)
        def expanded(node, seen=frozenset()):
            for part in ast.walk(node):
                yield part
                if isinstance(part, ast.Name) and part.id in aliases and part.id not in seen:
                    yield from expanded(aliases[part.id], seen | {part.id})
        def tables(node, seen=frozenset()):
            if isinstance(node, ast.Name):
                if node.id in MODEL_TABLES:
                    return {MODEL_TABLES[node.id]}
                if node.id in aliases and node.id not in seen:
                    return tables(aliases[node.id], seen | {node.id})
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return {node.value} if node.value in MODEL_TABLES.values() else set()
            if isinstance(node, ast.Attribute):
                return {MODEL_TABLES[node.attr]} if node.attr in MODEL_TABLES else tables(node.value, seen)
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    return tables(node.func.value, seen)
                return tables(node.args[0], seen) if node.args else set()
            return set()
        for node in nodes:
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            method = node.func.attr
            if method not in {'execute', 'exec_driver_sql', 'copy', 'bulk_insert_mappings', 'bulk_update_mappings', 'bulk_save_objects'}:
                continue
            if not node.args:
                continue
            statement = node.args[0]
            rendered = ast.unparse(statement)
            named_tables = tables(statement)
            parts = list(expanded(statement))
            sql = ' '.join(part.value for part in parts if isinstance(part, ast.Constant) and isinstance(part.value, str))
            raw_targets = {table for table in INPUT_TABLES if re.search(
                rf'\b(?:update\s+|insert\s+into\s+|delete\s+from\s+|copy\s+)(?:[\w"`\[\]]+\.)?["`\[]?{re.escape(table)}\b', sql, re.I)}
            raw_sql = method in {'exec_driver_sql', 'copy'} or any(isinstance(part, ast.Call) and
                getattr(part.func, 'id', getattr(part.func, 'attr', '')) == 'text' for part in parts)
            raw_dml = raw_sql and re.search(r'\b(?:update\s+|insert\s+into\s+|delete\s+from\s+|copy\s+)', sql, re.I)
            if raw_dml:
                targets = {table for table in MODEL_TABLES.values() if re.search(
                    rf'\b(?:update\s+|insert\s+into\s+|delete\s+from\s+|copy\s+)(?:[\w"`\[\]]+\.)?["`\[]?{re.escape(table)}\b', sql, re.I)}
                if (not targets or not targets <= NON_INPUT_CORE_TABLES.keys()) and not allowed_adapter(targets):
                    failures.append((node.lineno, rendered))
            receiver = node.func.value
            session = isinstance(receiver, ast.Name) and receiver.id in {'db', 'session'} and receiver.id not in core_names
            dml = any(isinstance(part, ast.Call) and (
                isinstance(part.func, ast.Name) and part.func.id in {'update', 'insert', 'delete', 'postgres_insert', 'sqlite_insert'}
                or isinstance(part.func, ast.Attribute) and part.func.attr in {'update', 'insert', 'delete'}) for part in parts)
            if raw_targets and not allowed_adapter(raw_targets) or method.startswith('bulk_') or dml and not session and (
                    not named_tables or not named_tables <= NON_INPUT_CORE_TABLES.keys()) and not allowed_adapter(named_tables):
                failures.append((node.lineno, rendered))
            if dml and named_tables & INPUT_TABLES:
                writers.append((node.lineno, sorted(named_tables & INPUT_TABLES), session))
    return sorted(set(failures)), writers


def test_application_input_dml_cannot_bypass_the_session_events():
    assert not INPUT_TABLES & NON_INPUT_CORE_TABLES.keys()
    failures = []
    for folder in ('apps', 'packages'):
        for path in (ROOT / folder).rglob('*.py'):
            bypasses, _ = input_write_bypasses(path.read_text(encoding='utf-8'), path.relative_to(ROOT).as_posix())
            failures.extend((str(path.relative_to(ROOT)), *bypass) for bypass in bypasses)
    assert not failures, failures


@pytest.mark.parametrize('source', [
    'connection.execute(update(m.Availability).values(retracted=True))',
    'connection.execute(insert(m.Availability.__table__), rows)',
    'connection.execute(delete(m.Availability))',
    'statement = update(m.Availability).values(retracted=True)\nconnection.execute(statement)',
    'A = m.Availability\nstatement = delete(A.__table__)\nconnection.execute(statement)',
    'with engine.begin() as db:\n    db.execute(delete(m.Availability))',
    'db.connection().execute(update(m.Availability).values(retracted=True))',
    'db.execute(text("UPDATE availability SET retracted = true"))',
    'connection.exec_driver_sql("DELETE FROM availability")',
    'statement = text("DELETE FROM public.availability")\nconnection.execute(statement)',
    'db = connection\ndb.execute(delete(m.Availability))',
    'db.execute(text(f"UPDATE {m.Availability.__tablename__} SET retracted = true"))',
    'db.execute(text("DELETE FROM " + table))',
    'connection.execute(insert(m.FairnessLedger), rows)',
    'cursor.copy("COPY fairness_ledger FROM STDIN")',
    'db.bulk_insert_mappings(m.Availability, rows)',
    'db.bulk_update_mappings(m.Availability, rows)',
    'db.bulk_save_objects([m.Availability()])',
])
def test_guard_rejects_core_and_legacy_bulk_bypasses(source):
    assert input_write_bypasses(source)[0]


@pytest.mark.parametrize('source', [
    'db.execute(update(m.Availability).values(retracted=True))',
    'session.execute(insert(m.Availability), rows)',
    'db.execute(delete(m.Availability))',
    'connection.execute(insert(m.WorkspaceReadProjection.__table__), rows)',
    'connection.execute(update(m.ReadProjectionWarmCursor).values(after_id=None))',
])
def test_guard_accepts_event_paths_and_named_non_input_exceptions(source):
    assert not input_write_bypasses(source)[0]


# Request-scoped authority memos clear only on session events (apps/api/db.py), so these tables need the same guard.
REQUEST_MEMO_TABLES = (m.Department, m.Organisation, m.RoleBinding, m.SelfApprovalPermission)


@pytest.mark.parametrize('model', REQUEST_MEMO_TABLES, ids=lambda model: model.__tablename__)
def test_guard_rejects_connection_dml_to_request_memo_tables(model):
    assert model.__tablename__ not in NON_INPUT_CORE_TABLES
    name, table = model.__name__, model.__tablename__
    for source in (f'connection.execute(update(m.{name}).values(revoked=True))',
                   f'db.connection().execute(delete(m.{name}.__table__))',
                   f'db.execute(text("UPDATE {table} SET revoked = true"))',
                   f'connection.exec_driver_sql("DELETE FROM {table}")'):
        assert input_write_bypasses(source)[0], source


def test_raw_ledger_exception_is_one_stamped_adapter_only():
    source = '''def _write_ledger_rows(db, rows):
    cursor.copy("COPY fairness_ledger FROM STDIN")
    bump_snapshot_input_revisions(db, organisation_ids)
'''
    assert not input_write_bypasses(source, RAW_LEDGER_ADAPTER[0])[0]
    assert input_write_bypasses(source)[0]
    assert input_write_bypasses(source.replace('bump_snapshot_input_revisions', 'unrelated'), RAW_LEDGER_ADAPTER[0])[0]
    assert input_write_bypasses(source.replace('fairness_ledger', 'availability'), RAW_LEDGER_ADAPTER[0])[0]
