"""Allowlisted migration source proofs; never import or execute a revision."""
import ast
from copy import deepcopy
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

from migrations.sql_safety import (CURRENT_SCHEMA, analyze_sql, clear_execution_proofs, schema_template,
                                   sqlite_nullable_update_guard)


CLASSES = ('additive', 'fill_only', 'destructive')
# Source-bound proof of the published closure, including both merge revisions. The trigger installed
# in 0002 calls the function replaced in 0050, which compares the whole row except current and effective_until.
# The same sealed SQLite history leaves the calendar lineage trigger name unused.
# ponytail: only this audited historical guard is seeded. New guard shapes need their own source proof.
PUBLICATION_GUARD_CHECKPOINT = '0152_feasibility_results'
PUBLICATION_GUARD_HISTORY = 'ad3ec2b570a972a0193024b5d89eef7506619353d039e03f9f89cae2ca494e2b'
PUBLICATION_COLUMNS = frozenset({
    'workspace_id', 'sequence', 'starts_on', 'ends_on', 'current', 'supersedes_id', 'published_by',
    'snapshot', 'validation', 'fingerprint', 'id', 'revision', 'created_at', 'department_id', 'effective_until',
})
MISSING_RETAINED_GUARD = 'dialect-only update guard lacks a proved retained PostgreSQL guard'
TYPES = {'String', 'Text', 'Integer', 'BigInteger', 'SmallInteger', 'Boolean', 'Float', 'Numeric',
         'JSON', 'Date', 'DateTime', 'Time', 'LargeBinary', 'Enum'}
CONSTRUCTORS = TYPES | {'table', 'Table', 'column', 'Column', 'MetaData', 'ForeignKey',
    'ForeignKeyConstraint', 'PrimaryKeyConstraint', 'UniqueConstraint', 'CheckConstraint',
    'select', 'update', 'insert', 'delete', 'bindparam', 'literal', 'and_', 'or_', 'not_', 'true', 'false', 'null'}
CONSTRUCTOR_OPTIONS = {
    'Column': {'primary_key', 'nullable', 'server_default', 'default', 'unique', 'index', 'autoincrement'},
    'column': {'type_'}, 'table': {'schema'}, 'Table': {'schema'}, 'MetaData': set(),
    'ForeignKey': {'name', 'ondelete', 'onupdate', 'deferrable', 'initially'},
    'ForeignKeyConstraint': {'name', 'ondelete', 'onupdate', 'deferrable', 'initially'},
    'PrimaryKeyConstraint': {'name'}, 'UniqueConstraint': {'name'}, 'CheckConstraint': {'name'},
    'bindparam': {'value'}, 'literal': {'type_'},
}
POSITIONAL_LIMITS = {'column': 2, 'MetaData': 0, 'ForeignKey': 1, 'ForeignKeyConstraint': 2,
    'CheckConstraint': 1, 'bindparam': 2, 'literal': 2, 'update': 1, 'insert': 1, 'delete': 1,
    'not_': 1, 'true': 0, 'false': 0, 'null': 0}
OP_OPTIONS = {'create_table': {'schema', 'if_not_exists'}, 'add_column': {'schema', 'if_not_exists'},
    'create_index': {'schema', 'unique', 'if_not_exists', 'sqlite_where', 'postgresql_where'},
    'create_foreign_key': {'source_schema', 'referent_schema', 'ondelete', 'onupdate', 'deferrable', 'initially'},
    'create_primary_key': {'schema'}, 'create_unique_constraint': {'schema', 'deferrable', 'initially'},
    'create_check_constraint': {'schema'}}
ADDITIVE_OPS = {'create_table', 'create_index', 'create_foreign_key', 'create_unique_constraint',
                'create_check_constraint', 'create_primary_key'}


@lru_cache(maxsize=256)
def parse(source):
    return ast.parse(source)


def assigned(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            try:
                return ast.literal_eval(node.value)
            except (ValueError, TypeError):
                return None
    return None


class Analyzer(ast.NodeVisitor):
    def __init__(self, source, schema):
        self.tree, self.schema = parse(source), schema
        self.parents = {child: node for node in ast.walk(self.tree) for child in ast.iter_child_nodes(node)}
        self.env, self.aliases = {}, {}
        self.function = None
        self.dialect, self.dollar_free = None, set()
        self.rank, self.findings, self.needs_schema = 0, [], False
        self.unproved = False
        self.retained_guard_use = None
        self.globals = {}
        definitions = [node.name for node in self.tree.body if isinstance(node, ast.FunctionDef)]
        for node in self.tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.aliases[alias.asname or alias.name.split('.')[0]] = alias.name if alias.asname else alias.name.split('.')[0]
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    self.aliases[alias.asname or alias.name] = (node.module or '') + '.' + alias.name
            elif isinstance(node, ast.FunctionDef):
                if definitions.count(node.name) != 1 or node.name in self.aliases:
                    self.issue(node, 'shadowed or repeated function definition')
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                    if isinstance(target, ast.Name):
                        if target.id in self.globals:
                            self.issue(node, 'reassigned module binding')
                        self.globals[target.id] = node.value

    def name(self, node):
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, '') if node.id not in self.env else ''
        if isinstance(node, ast.Attribute):
            parent = self.name(node.value)
            return parent + '.' + node.attr if parent else ''
        return ''

    def invalidate(self):
        for fact in self.schema['tables'].values():
            fact.update(columns=[], primary_key=[], added_columns=[], new=False)
        clear_execution_proofs(self.schema)
        self.schema.pop('retained_publication_guard', None)

    def issue(self, node, reason):
        self.rank, self.unproved = 2, True
        self.invalidate()
        item = f'line {node.lineno}: {reason}'
        if item not in self.findings:
            self.findings.append(item)

    def resolve(self, node):
        seen = set()
        while isinstance(node, ast.Name) and node.id in self.env and node.id not in seen:
            seen.add(node.id)
            node = self.env[node.id]
        return node

    def value(self, node):
        node = self.resolve(node)
        try:
            return ast.literal_eval(node)
        except (ValueError, TypeError):
            return node

    def literal(self, node):
        return not isinstance(self.value(node), ast.AST)

    def constructor(self, node):
        name = self.name(node.func) if isinstance(node, ast.Call) else self.name(node)
        if name.startswith('sqlalchemy.') and name.count('.') == 1:
            return name.split('.')[-1]
        if name in {'sqlalchemy.dialects.sqlite.insert', 'sqlalchemy.dialects.postgresql.insert'}:
            return 'insert'
        return ''

    def expression(self, node, seen=None):
        # Every accepted expression has an explicit model; method names alone prove nothing.
        seen = set() if seen is None else seen
        if isinstance(node, ast.Name) and node.id in self.env:
            if node.id in seen:
                return False
            return self.expression(self.env[node.id], seen | {node.id})
        if self.literal(node):
            return True
        if isinstance(node, (ast.List, ast.Tuple)):
            return all(self.expression(value, seen) for value in node.elts)
        if self.schema_lookup(node) or self.quoted_schema(node) or self.index_expression(node):
            return True
        if isinstance(node, ast.Name) or isinstance(node, ast.Attribute) and self.name(node):
            return self.name(node) in {'sqlalchemy.' + kind for kind in TYPES}
        if isinstance(node, ast.Attribute) and self.column(node)[0]:
            return self.expression(node.value.value, seen)
        if isinstance(node, ast.Subscript) and self.column(node)[0]:
            return self.literal(node.slice) and self.expression(node.value.value, seen)
        if isinstance(node, ast.Compare):
            return all(isinstance(op, (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot)) for op in node.ops) \
                and all(self.expression(value, seen) for value in [node.left, *node.comparators])
        if not isinstance(node, ast.Call) or any(key.arg is None for key in node.keywords):
            return False
        kind = self.constructor(node)
        if kind in CONSTRUCTORS:
            if kind in POSITIONAL_LIMITS and len(node.args) > POSITIONAL_LIMITS[kind]:
                return False
            options = {'timezone', 'length', 'precision', 'scale', 'name', 'native_enum', 'create_constraint', 'none_as_null'} if kind in TYPES else CONSTRUCTOR_OPTIONS.get(kind, set())
            if any(key.arg not in options for key in node.keywords):
                return False
            if kind == 'CheckConstraint' and (not node.args or not self.sql_expression(node.args[0])):
                return False
            values = [*node.args, *(key.value for key in node.keywords)]
            return all(self.literal(value) if kind in TYPES else self.expression(value, seen) for value in values)
        if isinstance(node.func, ast.Attribute):
            receiver = self.resolve(node.func.value)
            method = node.func.attr
            if self.table(receiver) and method in {'update', 'insert', 'delete'}:
                return not node.args and not node.keywords and self.expression(receiver, seen)
            if self.column(receiver)[0] and method in {'is_', 'is_not', 'in_', 'not_in', 'asc', 'desc'}:
                return self.expression(receiver, seen) and all(self.literal(arg) for arg in node.args) and not node.keywords
            calls = self.chain(receiver)
            if calls and self.constructor(calls[-1]) in {'select', 'update', 'insert', 'delete'} \
                    or calls and isinstance(calls[-1].func, ast.Attribute) and self.table(calls[-1].func.value):
                if method in {'where', 'values', 'order_by', 'select_from', 'limit', 'offset', 'on_conflict_do_nothing'}:
                    return self.expression(receiver, seen) and all(self.expression(value, seen)
                        for value in [*node.args, *(key.value for key in node.keywords)])
        return False

    def json_path(self, node):
        node = self.resolve(node)
        if not isinstance(node, ast.Subscript) or type(self.value(node.slice)) not in {str, int}:
            return False
        receiver = self.resolve(node.value)
        if isinstance(receiver, ast.Subscript):
            return self.json_path(receiver)
        table, column = self.column(receiver)
        if not table or not self.expression(receiver.value.value):
            return False
        definition = self.resolve(receiver.value.value)
        return any(isinstance(item, ast.Call) and self.constructor(item) in {'column', 'Column'}
            and len(item.args) >= 2 and self.value(item.args[0]) == column
            and (self.constructor(self.resolve(item.args[1])) == 'JSON'
                 or self.name(self.resolve(item.args[1])) == 'sqlalchemy.JSON') for item in definition.args[1:])

    def index_expression(self, node):
        node = self.resolve(node)
        if self.column(node)[0]:
            return self.expression(node.value.value)
        if self.json_path(node):
            return True
        if isinstance(node, ast.Compare):
            return len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq) \
                and self.index_expression(node.left) and self.literal(node.comparators[0])
        if not isinstance(node, ast.Call) or node.keywords:
            return False
        if self.name(node.func) == 'sqlalchemy.func.coalesce':
            return len(node.args) == 2 and self.index_expression(node.args[0]) and self.literal(node.args[1])
        if isinstance(node.func, ast.Attribute) and not node.args:
            if node.func.attr in {'as_string', 'as_integer'}:
                return self.json_path(node.func.value)
            if node.func.attr in {'asc', 'desc'}:
                return self.index_expression(node.func.value)
        return False

    def schema_lookup(self, node):
        node = self.resolve(node)
        if self.dialect != 'postgresql' or not isinstance(node, ast.Call) or node.args or node.keywords \
                or not isinstance(node.func, ast.Attribute) or node.func.attr != 'scalar_one':
            return False
        execute = self.resolve(node.func.value)
        if not isinstance(execute, ast.Call) or len(execute.args) != 1 or execute.keywords \
                or not isinstance(execute.func, ast.Attribute) or execute.func.attr != 'execute' \
                or not self.connection(execute.func.value):
            return False
        text = self.resolve(execute.args[0])
        return isinstance(text, ast.Call) and self.name(text.func) == 'sqlalchemy.text' \
            and len(text.args) == 1 and not text.keywords and self.value(text.args[0]) == 'SELECT current_schema()'

    def quoted_schema(self, node):
        node = self.resolve(node)
        if not isinstance(node, ast.Call) or len(node.args) != 1 or node.keywords \
                or not isinstance(node.func, ast.Attribute) or node.func.attr != 'quote_identifier':
            return False
        preparer = node.func.value
        return isinstance(preparer, ast.Attribute) and preparer.attr == 'identifier_preparer' \
            and isinstance(preparer.value, ast.Attribute) and preparer.value.attr == 'dialect' \
            and self.connection(preparer.value.value) and self.schema_lookup(node.args[0])

    def template(self, node):
        if not isinstance(node, ast.JoinedStr):
            return None
        parts, holes = [], 0
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                if CURRENT_SCHEMA in part.value:
                    return None
                parts.append(part.value)
            elif isinstance(part, ast.FormattedValue) and part.conversion == -1 and part.format_spec is None \
                    and self.quoted_schema(part.value):
                quote = self.resolve(part.value)
                if ast.dump(self.resolve(quote.args[0])) not in self.dollar_free:
                    return None  # Identifier quoting alone cannot escape an enclosing dollar body.
                parts.append('"' + CURRENT_SCHEMA + '"')
                holes += 1
            else:
                return None
        sql = ''.join(parts)
        return sql if holes and schema_template(sql, holes) else None

    def sql_expression(self, node):
        value = self.value(node)
        return isinstance(value, str) and analyze_sql('SELECT ' + value)['class'] == 'additive'

    def connection(self, node):
        node = self.resolve(node)
        return isinstance(node, ast.Call) and self.name(node.func) == 'alembic.op.get_bind' \
            and not node.args and not node.keywords

    def visit_Import(self, node):
        if any(alias.name != 'sqlalchemy' for alias in node.names):
            self.issue(node, 'unproved import')

    def visit_ImportFrom(self, node):
        if node.level or any(alias.asname and alias.asname != alias.name for alias in node.names) or not ((node.module == 'alembic' and all(alias.name == 'op' for alias in node.names))
                or node.module == 'sqlalchemy' and all(alias.name in CONSTRUCTORS | {'text'} for alias in node.names)
                or node.module in {'sqlalchemy.dialects.sqlite', 'sqlalchemy.dialects.postgresql'}
                and all(alias.name == 'insert' for alias in node.names)):
            self.issue(node, 'unproved import')

    def visit_Assign(self, node):
        self.visit(node.value)
        for target in node.targets:
            if not isinstance(target, ast.Name) or target.id in self.aliases or target.id in self.env:
                self.issue(node, 'unproved assignment target')
            elif isinstance(self.parents.get(node), ast.Module):
                self.env[target.id] = node.value
            else:
                self.env[target.id] = self.resolve(node.value)

    def visit_AnnAssign(self, node):
        self.issue(node, 'unproved annotated assignment')

    def visit_FunctionDef(self, node):
        if node.type_params or node.decorator_list or node.returns or node.args.defaults or node.args.kw_defaults \
                or node.args.args or node.args.posonlyargs or node.args.kwonlyargs or node.args.vararg or node.args.kwarg:
            self.issue(node, 'unproved function definition')
        if not isinstance(self.parents.get(node), ast.Module):
            self.issue(node, 'unproved nested function definition')
            return
        before, previous, schema = self.env, self.function, deepcopy(self.schema)
        clear_execution_proofs(self.schema)
        if node.name != 'upgrade':
            self.schema.pop('record_fill', None)
            self.schema.pop('record_duplicate_precheck', None)
            for table in self.schema['tables'].values():
                table['new'] = False
        self.env, self.function = self.globals.copy(), node.name
        for statement in node.body:
            self.visit(statement)
        self.env, self.function = before, previous
        if node.name != 'upgrade':
            self.schema.clear()
            self.schema.update(schema)
        clear_execution_proofs(self.schema)

    def visit_Expr(self, node):
        self.visit(node.value)

    def visit_Constant(self, node):
        pass

    def visit_Pass(self, node):
        pass

    def visit_Return(self, node):
        self.issue(node, 'unproved control flow')

    def nullable_update_guard(self, node, dialect):
        upgrade = self.parents.get(node)
        # Both dialects execute the same column addition. Only SQLite executes the refusal.
        if (self.function != 'upgrade' or self.dialect is not None or not isinstance(upgrade, ast.FunctionDef)
                or len(upgrade.body) != 2 or upgrade.body[1] is not node or not isinstance(upgrade.body[0], ast.Expr)):
            return None
        metadata = {'revision', 'down_revision', 'branch_labels', 'depends_on', 'SAFETY_CLASS'}
        if self.aliases.keys() & (metadata | {'upgrade', 'downgrade'}):
            return None
        if not all(statement is upgrade or isinstance(statement, (ast.Import, ast.ImportFrom))
                   or (isinstance(statement, ast.Assign) and self.literal(statement.value)
                       and all(isinstance(target, ast.Name) and target.id in metadata for target in statement.targets))
                   or isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)
                   or (isinstance(statement, ast.FunctionDef) and statement.name == 'downgrade'
                       and len(statement.body) == 1 and isinstance(statement.body[0], ast.Raise))
                   for statement in self.tree.body):
            return None
        sqlite, postgres = (node.body, node.orelse) if dialect == 'sqlite' else (node.orelse, node.body)
        if len(sqlite) != 1 or not isinstance(sqlite[0], ast.Expr) or any(not isinstance(s, ast.Pass) for s in postgres):
            return None
        call = sqlite[0].value
        if not isinstance(call, ast.Call) or self.name(call.func) != 'alembic.op.execute' or len(call.args) != 1 or call.keywords:
            return None
        sql = self.value(call.args[0])
        if isinstance(sql, str):
            guard = sqlite_nullable_update_guard(sql)
            if guard in self.schema.get('nullable_added_columns', ()):
                return guard
        return None

    def visit_If(self, node):
        test = node.test
        if (self.dialect == 'postgresql' and isinstance(test, ast.Compare) and len(test.ops) == 1
                and isinstance(test.ops[0], ast.In) and self.value(test.left) == '$'
                and self.schema_lookup(test.comparators[0]) and not node.orelse and len(node.body) == 1
                and isinstance(node.body[0], ast.Raise) and self.refusal(node.body[0])):
            self.dollar_free.add(ast.dump(self.resolve(test.comparators[0])))
            return
        if not (isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)
                and isinstance(test.left, ast.Attribute) and test.left.attr == 'name'
                and isinstance(test.left.value, ast.Attribute) and test.left.value.attr == 'dialect'
                and self.connection(test.left.value.value)
                and self.value(test.comparators[0]) in {'sqlite', 'postgresql'}):
            self.issue(node, 'unproved conditional execution')
            return
        dialect_name = self.value(test.comparators[0])
        if guard := self.nullable_update_guard(node, dialect_name):
            self.needs_schema = True
            if guard[0] != 'publications' or 'retained_publication_guard' not in self.schema:
                self.issue(node, MISSING_RETAINED_GUARD)
            else:
                self.retained_guard_use = node
            return
        self.schema['append_targets'], self.schema['guard_targets'] = set(), set()
        environment, dialect, dollar_free = self.env.copy(), self.dialect, self.dollar_free.copy()
        before = deepcopy(self.schema)
        baseline_rank, baseline_unproved = self.rank, self.unproved
        self.dialect = dialect_name
        self.rank, self.unproved = 0, False
        for statement in node.body:
            self.visit(statement)
        first = deepcopy(self.schema)
        first_rank, first_unproved = self.rank, self.unproved
        self.env, self.dollar_free = environment.copy(), dollar_free.copy()
        self.schema.clear()
        self.schema.update(deepcopy(before))
        self.dialect = 'postgresql' if self.dialect == 'sqlite' else 'sqlite'
        self.rank, self.unproved = 0, False
        for statement in node.orelse:
            self.visit(statement)
        second_rank, second_unproved = self.rank, self.unproved
        self.rank = max(baseline_rank, first_rank, second_rank)
        self.unproved = baseline_unproved or first_unproved or second_unproved
        self.env, self.dialect, self.dollar_free = environment, dialect, dollar_free
        if self.schema['tables'] != first['tables']:
            self.issue(node, 'dialect branches have different schema proofs')
        if self.schema.get('retained_publication_guard') != first.get('retained_publication_guard'):
            self.schema.pop('retained_publication_guard', None)
        appends = self.schema.get('append_targets', set())
        guards, first_guards = self.schema.get('guard_targets', set()), first.get('guard_targets', set())
        if appends and appends == first.get('append_targets'):
            # Existing append proofs permit extra unconditional read/raise guards.
            guards, first_guards = ({item for item in group if item[2]} for group in (guards, first_guards))
        pure_fill_branches = (not baseline_unproved and not first_unproved and not second_unproved
                              and first_rank == second_rank == CLASSES.index('fill_only'))
        if ((not (appends or guards) and not pure_fill_branches)
                or appends != first.get('append_targets') or guards != first_guards):
            self.issue(node, 'dialect branches lack matching new-table observer or guard proofs')
        clear_execution_proofs(self.schema)

    def refusal(self, node):
        return isinstance(node.exc, ast.Call) and isinstance(node.exc.func, ast.Name) \
            and node.exc.func.id == 'RuntimeError' and node.exc.func.id not in self.env \
            and node.exc.func.id not in self.aliases and len(node.exc.args) == 1 \
            and isinstance(self.value(node.exc.args[0]), str) and not node.exc.keywords and node.cause is None

    def visit_Raise(self, node):
        # Forward-only downgrade refusal is the one modeled non-SQL executable statement.
        if not (self.function == 'downgrade' and self.refusal(node)):
            self.issue(node, 'unproved raise')

    def generic_visit(self, node):
        if not self.expression(node):
            self.issue(node, 'unproved executable ' + type(node).__name__)

    def table_name(self, node, index=0):
        name = self.value(node.args[index]) if len(node.args) > index else None
        schema = next((self.value(key.value) for key in node.keywords if key.arg == 'schema'), None)
        if not isinstance(name, str) or '.' in name or schema is not None and (not isinstance(schema, str) or '.' in schema):
            return None
        return schema + '.' + name if schema else name

    def table(self, node):
        node = self.resolve(node)
        if isinstance(node, ast.Call) and self.name(node.func).startswith('sqlalchemy.') \
                and self.name(node.func).split('.')[-1] in {'table', 'Table'} and node.args:
            return self.table_name(node)
        return None

    def column(self, node):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute) and node.value.attr == 'c':
            return self.table(node.value.value), node.attr
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == 'c':
            return self.table(node.value.value), self.value(node.slice)
        return None, None

    def chain(self, node):
        node, calls = self.resolve(node), []
        while isinstance(node, ast.Call):
            calls.append(node)
            if isinstance(node.func, ast.Attribute):
                child = self.resolve(node.func.value)
                if isinstance(child, ast.Call) and self.table(child) is None:
                    node = child
                    continue
            break
        return calls

    def guards(self, node, table):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
            return set().union(*(self.guards(value, table) for value in node.values))
        if isinstance(node, ast.Call):
            if self.name(node.func).endswith('.and_'):
                return set().union(*(self.guards(value, table) for value in node.args))
            if isinstance(node.func, ast.Attribute) and node.func.attr == 'is_' and len(node.args) == 1 \
                    and self.value(node.args[0]) is None:
                owner, column = self.column(node.func.value)
                return {column} if owner == table else set()
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.Eq, ast.Is)) \
                and self.value(node.comparators[0]) is None:
            owner, column = self.column(node.left)
            return {column} if owner == table else set()
        return set()

    def bounded(self, predicates, table):
        keys = set(self.schema['tables'].get(table, {}).get('primary_key', []))
        equal = set()
        def visit(node):
            if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
                for value in node.values:
                    visit(value)
            elif isinstance(node, ast.Call) and self.name(node.func).endswith('.and_'):
                for value in node.args:
                    visit(value)
            elif isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq):
                for left, right in ((node.left, node.comparators[0]), (node.comparators[0], node.left)):
                    owner, column = self.column(left)
                    if owner == table and column in keys and self.independent(right):
                        equal.add(column)
        for predicate in predicates:
            visit(predicate)
        return bool(keys) and keys <= equal


    def schema_operation(self, node, method):
        if not node.args:
            return
        table = self.table_name(node)
        if not isinstance(table, str):
            return
        if self.function != 'upgrade':
            return
        if method == 'create_table' and table.rsplit('.', 1)[-1].lower() == 'publications':
            self.schema.pop('retained_publication_guard', None)
        parent = self.parents.get(node)
        direct = isinstance(parent, (ast.Expr, ast.Assign, ast.AnnAssign)) and parent.value is node
        if not direct:
            if method == 'create_table':
                self.schema['tables'][table] = {'columns': [], 'primary_key': [], 'new': False}
            return
        if method == 'create_table':
            self.schema['tables'][table] = {'columns': [], 'primary_key': [], 'new': False}
            if any(key.arg == 'if_not_exists' and self.value(key.value) is not False for key in node.keywords):
                return
        fact = self.schema['tables'].setdefault(table, {'columns': [], 'primary_key': [], 'new': False})
        fact.setdefault('added_columns', [])
        if method == 'create_table':
            fact['new'] = True
            fact['unique_keys'] = []
            columns = node.args[1:]
        elif method == 'add_column':
            columns = node.args[1:2]
        else:
            return
        for column in columns:
            if not isinstance(column, ast.Call) or not column.args:
                continue
            kind = self.name(column.func).split('.')[-1]
            name = self.value(column.args[0])
            if kind == 'Column' and isinstance(name, str):
                if method == 'add_column':
                    known = self.schema.get('retained_publication_guard') if table == 'publications' else None
                    column_type = self.resolve(column.args[1]) if len(column.args) == 2 else None
                    if (self.dialect is None and isinstance(self.parents.get(parent), ast.FunctionDef)
                            and not node.keywords
                            and name.isascii() and name.isidentifier() and name.islower() and len(name) <= 63
                            and isinstance(column_type, ast.Call) and self.constructor(column_type) == 'Boolean'
                            and not column_type.args and not column_type.keywords
                            and len(column.keywords) == 1 and column.keywords[0].arg == 'nullable'
                            and self.value(column.keywords[0].value) is True
                            and name not in fact['columns'] and (known is None or name not in known)):
                        self.schema.setdefault('nullable_added_columns', set()).add((table, name))
                    if known is not None:
                        if name.lower() in known and not any(key.arg == 'if_not_exists' and self.value(key.value) is True
                                                     for key in node.keywords):
                            self.schema.pop('retained_publication_guard')
                        else:
                            known.add(name.lower())
                    elif table.rsplit('.', 1)[-1].lower() == 'publications':
                        self.schema.pop('retained_publication_guard', None)
                if name not in fact['columns']:
                    fact['columns'].append(name)
                if method == 'add_column' and name not in fact['added_columns']:
                    fact['added_columns'].append(name)
                if method == 'create_table' and any(key.arg == 'primary_key' and self.value(key.value) is True for key in column.keywords):
                    fact['primary_key'].append(name)
            elif method == 'create_table' and kind == 'PrimaryKeyConstraint':
                names = [self.value(arg) for arg in column.args]
                if all(isinstance(item, str) for item in names):
                    fact['primary_key'] = names
            elif method == 'create_table' and kind == 'UniqueConstraint':
                names = [self.value(arg) for arg in column.args]
                if all(isinstance(item, str) for item in names):
                    fact['unique_keys'].append(names)


    def independent(self, node):
        return isinstance(self.value(node), (str, int, float, bool)) or isinstance(self.resolve(node), ast.Call) \
            and self.constructor(self.resolve(node)) == 'bindparam' and self.expression(node)

    def statement(self, node):
        node = self.resolve(node)
        if isinstance(node, ast.Call) and self.name(node.func) == 'sqlalchemy.text' \
                and len(node.args) == 1 and not node.keywords:
            node = self.resolve(node.args[0])
        value = self.value(node)
        template = self.template(node)
        if template is not None:
            self.needs_schema = True
            self.schema['current_schema'] = CURRENT_SCHEMA
            try:
                result = analyze_sql(template, schema=self.schema)
            finally:
                self.schema.pop('current_schema', None)
            return result['class'], '; '.join(result['findings'])
        if isinstance(value, str):
            self.needs_schema = True
            result = analyze_sql(value, schema=self.schema)
            return result['class'], '; '.join(result['findings'])
        if not self.expression(node):
            return 'destructive', 'unproved SQL expression or execution construct'
        calls = self.chain(node)
        if not calls:
            return 'destructive', 'unknown SQL execution'
        base = calls[-1]
        kind = self.constructor(base) or getattr(base.func, 'attr', '')
        if kind == 'select':
            return 'additive', ''
        table = self.table(base.args[0]) if self.constructor(base) and base.args else None
        if not table and isinstance(base.func, ast.Attribute):
            table = self.table(base.func.value)
        self.needs_schema = True
        setters = [call for call in calls if getattr(call.func, 'attr', '') == 'values']
        known = self.schema['tables'].get(table, {})
        if kind == 'update':
            targets = {key.arg for call in setters for key in call.keywords if key.arg is not None}
            predicates = [arg for call in calls if getattr(call.func, 'attr', '') == 'where' for arg in call.args]
            guarded = set().union(*(self.guards(predicate, table) for predicate in predicates))
            if setters and not any(call.args for call in setters) and targets \
                    and targets <= set(known.get('columns', [])) and targets <= guarded and self.bounded(predicates, table):
                if record_fill := self.schema.get('record_fill'):
                    record_fill(table, targets)
                return 'fill_only', ''
        if kind == 'insert':
            values = {key.arg: self.value(key.value) for call in setters for key in call.keywords}
            keys = known.get('primary_key', [])
            if known.get('new') and keys and len(setters) == 1 and not setters[0].args \
                    and any(getattr(call.func, 'attr', '') == 'on_conflict_do_nothing' for call in calls) \
                    and all(isinstance(values.get(key), (str, int)) for key in keys):
                if record_fill := self.schema.get('record_fill'):
                    record_fill(table, None)
                return 'fill_only', ''
        return 'destructive', 'write is not proven bounded, NULL-guarded or idempotent'

    def visit_Call(self, node):
        name, method = self.name(node.func), getattr(node.func, 'attr', '')
        if name in {'alembic.op.get_bind', 'alembic.op.get_context'} and not node.args and not node.keywords:
            return
        if name == 'alembic.op.execute' or isinstance(node.func, ast.Attribute) \
                and method in {'execute', 'exec_driver_sql', 'scalar', 'scalars'} and self.connection(node.func.value):
            kind, reason = self.statement(node.args[0]) if len(node.args) == 1 and not node.keywords else ('destructive', 'unproved SQL execution arguments')
            self.rank = max(self.rank, CLASSES.index(kind))
            if kind == 'destructive':
                self.issue(node, reason)
            return
        if name == 'alembic.op.' + method and method in ADDITIVE_OPS | {'add_column'}:
            if not node.args or not all(self.expression(value) for value in [*node.args, *(key.value for key in node.keywords)]) \
                    or any(key.arg not in OP_OPTIONS[method] for key in node.keywords) or not isinstance(self.value(node.args[0]), str):
                self.issue(node, 'unproved schema operation arguments')
                return
            if method == 'create_index':
                columns = self.resolve(node.args[2]) if len(node.args) == 3 else None
                if not isinstance(columns, (ast.List, ast.Tuple)) or not columns.elts \
                        or not all(isinstance(self.value(value), str) or self.index_expression(value) for value in columns.elts) \
                        or any(not self.index_expression(key.value) for key in node.keywords
                               if key.arg in {'sqlite_where', 'postgresql_where'}):
                    self.issue(node, 'unproved index expression or predicate')
                    return
            if method == 'create_check_constraint' and (len(node.args) < 3 or not self.sql_expression(node.args[2])):
                self.issue(node, 'unproved check constraint expression')
                return
            if method == 'add_column':
                column = node.args[1] if len(node.args) == 2 else None
                keys = {key.arg: self.value(key.value) for key in column.keywords} if isinstance(column, ast.Call) else {}
                nullable = keys.get('nullable', keys.get('primary_key', False) is False)
                if not isinstance(column, ast.Call) or self.constructor(column) != 'Column' \
                        or nullable is not True and keys.get('server_default') is None:
                    self.issue(node, 'ADD COLUMN is not proven nullable or server-defaulted')
                    return
            if method in {'create_table', 'add_column'}:
                self.schema_operation(node, method)
            return
        if not self.expression(node):
            self.issue(node, 'unrecognized call ' + (name or ast.unparse(node.func)))

    def run(self):
        for node in self.tree.body:
            self.visit(node)
        if self.retained_guard_use is not None and not self.unproved:
            if 'retained_publication_guard' not in self.schema:
                self.issue(self.retained_guard_use, MISSING_RETAINED_GUARD)
            elif self.rank:
                self.issue(self.retained_guard_use, 'dialect-only update guard requires a shared additive schema')
            else:
                # The SQLite trigger name is no longer available in descendants.
                self.schema.pop('retained_publication_guard')
        if self.unproved:
            self.invalidate()
        return {'class': CLASSES[self.rank], 'findings': self.findings, 'needs_schema': self.needs_schema}


@lru_cache(maxsize=256)
def schema_at(ancestors, retained_at=0):
    if not ancestors:
        return {'tables': {}}
    schema = deepcopy(schema_at(ancestors[:-1], retained_at))
    clear_execution_proofs(schema)
    for table in schema['tables'].values():
        table['new'] = False
    Analyzer(ancestors[-1], schema).run()
    clear_execution_proofs(schema)
    if len(ancestors) == retained_at:
        schema['retained_publication_guard'] = set(PUBLICATION_COLUMNS)
    return schema


def _revision_schema(ancestors, retained_at):
    schema = deepcopy(schema_at(ancestors, retained_at))
    for table in schema['tables'].values():
        table['new'] = False
    return schema


@lru_cache(maxsize=512)
def analyze_sources(source, ancestors, retained_at=0):
    # Source-keyed prefix reuse avoids reanalyzing every ancestor for every revision.
    return Analyzer(source, _revision_schema(ancestors, retained_at)).run()


def _published_guard_history(sources):
    pending, reachable = [PUBLICATION_GUARD_CHECKPOINT], set()
    while pending:
        rev = pending.pop()
        if rev in reachable:
            continue
        if rev not in sources:
            return False
        reachable.add(rev)
        parents = assigned(parse(sources[rev]), 'down_revision')
        if isinstance(parents, str):
            pending.append(parents)
        elif isinstance(parents, tuple) and all(isinstance(parent, str) for parent in parents):
            pending.extend(parents)
        elif parents is not None:
            return False
    digest = sha256()
    for rev in sorted(reachable):
        digest.update(sha256(sources[rev].encode('utf-8')).digest())
    return digest.hexdigest() == PUBLICATION_GUARD_HISTORY


def _linear_guard_descendant(source):
    return all(isinstance(node.value, ast.Constant) and node.value.value is None
               for node in parse(source).body if isinstance(node, ast.Assign)
               and any(isinstance(target, ast.Name) and target.id == 'depends_on' for target in node.targets))


def _revision_sources(path):
    path = Path(path)
    source = path.read_text(encoding='utf-8')
    if not analyze_sources(source, (), 0)['needs_schema']:
        return source, (), 0
    previous = assigned(parse(source), 'down_revision')
    ancestors, seen = [], set()
    sources, ambiguous = {}, False
    if previous:
        for item in path.parent.glob('*.py'):
            if item.name == '__init__.py':
                continue
            text = item.read_text(encoding='utf-8')
            rev = assigned(parse(text), 'revision')
            ambiguous |= isinstance(rev, str) and rev in sources
            sources[rev] = text
        while isinstance(previous, str) and previous not in seen and previous in sources:
            seen.add(previous)
            ancestors.append(previous)
            previous = assigned(parse(sources[previous]), 'down_revision')
    ancestors.reverse()
    history = tuple(sources[rev] for rev in ancestors)
    if (ambiguous or PUBLICATION_GUARD_CHECKPOINT not in seen
            or not any(finding.endswith(MISSING_RETAINED_GUARD)
                       for finding in analyze_sources(source, history, 0)['findings'])):
        return source, history, 0
    retained_at = ancestors.index(PUBLICATION_GUARD_CHECKPOINT) + 1
    if (not _linear_guard_descendant(source) or not all(map(_linear_guard_descendant, history[retained_at:]))
            or not _published_guard_history(sources)):
        return source, history, 0
    return source, history, retained_at


def analyze(path):
    return analyze_sources(*_revision_sources(path))


def preflight_analyzer(path, *, record_fill, record_duplicate_precheck):
    source, ancestors, retained_at = _revision_sources(path)
    schema = _revision_schema(ancestors, retained_at)
    schema.update(record_fill=record_fill, record_duplicate_precheck=record_duplicate_precheck)
    return Analyzer(source, schema)
