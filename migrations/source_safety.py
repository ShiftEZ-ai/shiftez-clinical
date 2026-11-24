"""Allowlisted migration source proofs; never import or execute a revision."""
import ast
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

from migrations.sql_safety import (
    CURRENT_SCHEMA, analyze_sql, check_definition, check_replacement, clear_execution_proofs,
    forget_checks, schema_template,
)


CLASSES = ('additive', 'fill_only', 'destructive')
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
        self.schema.pop('checks', None)
        clear_execution_proofs(self.schema)

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
        if node.decorator_list or node.returns or node.args.defaults or node.args.kw_defaults \
                or node.args.args or node.args.posonlyargs or node.args.kwonlyargs or node.args.vararg or node.args.kwarg:
            self.issue(node, 'unproved function definition')
        if not isinstance(self.parents.get(node), ast.Module):
            self.issue(node, 'unproved nested function definition')
            return
        before, previous, schema = self.env, self.function, deepcopy(self.schema)
        clear_execution_proofs(self.schema)
        if node.name != 'upgrade':
            for table in self.schema['tables'].values():
                table['new'] = False
        self.env, self.function = self.globals.copy(), node.name
        self.visit_block(node.body)
        self.env, self.function = before, previous
        if node.name != 'upgrade':
            self.schema.clear()
            self.schema.update(schema)
        clear_execution_proofs(self.schema)

    def visit_Expr(self, node):
        self.visit(node.value)

    def visit_block(self, statements):
        index, checks_only = 0, bool(statements)
        while index < len(statements):
            node = statements[index]
            if self.function == 'upgrade':
                sql = self.check_sql(node)
                if sql and 'DROP' in sql.upper() and 'CONSTRAINT' in sql.upper():
                    self.needs_schema = True
                    if check_replacement(self.schema, sql):
                        index += 1
                        continue
                    following = self.check_sql(statements[index + 1]) if index + 1 < len(statements) else None
                    if following and check_replacement(self.schema, sql, following):
                        index += 2
                        continue
            self.visit(node)
            checks_only = False
            index += 1
        return checks_only

    def visit_Constant(self, node):
        pass

    def visit_Pass(self, node):
        pass

    def visit_Return(self, node):
        self.issue(node, 'unproved control flow')

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
        self.schema['append_targets'], self.schema['guard_targets'] = set(), set()
        environment, dialect, dollar_free = self.env.copy(), self.dialect, self.dollar_free.copy()
        before = deepcopy(self.schema)
        baseline_rank, baseline_unproved = self.rank, self.unproved
        selected_dialect = self.value(test.comparators[0])
        self.dialect = selected_dialect
        self.rank, self.unproved = 0, False
        first_checks_only = self.visit_block(node.body)
        first = deepcopy(self.schema)
        first_rank, first_unproved = self.rank, self.unproved
        self.env, self.dollar_free = environment.copy(), dollar_free.copy()
        self.schema.clear()
        self.schema.update(deepcopy(before))
        self.dialect = 'postgresql' if self.dialect == 'sqlite' else 'sqlite'
        self.rank, self.unproved = 0, False
        self.visit_block(node.orelse)
        second_rank, second_unproved = self.rank, self.unproved
        self.rank = max(baseline_rank, first_rank, second_rank)
        self.unproved = baseline_unproved or first_unproved or second_unproved
        self.env, self.dialect, self.dollar_free = environment, dialect, dollar_free
        if self.schema['tables'] != first['tables']:
            self.issue(node, 'dialect branches have different schema proofs')
        for table, checks in self.schema.get('checks', {}).items():
            other = first.get('checks', {}).get(table, {})
            self.schema['checks'][table] = {name: fact for name, fact in checks.items() if other.get(name) == fact}
        appends = self.schema.get('append_targets', set())
        guards, first_guards = self.schema.get('guard_targets', set()), first.get('guard_targets', set())
        if appends and appends == first.get('append_targets'):
            # Existing append proofs permit extra unconditional read/raise guards.
            guards, first_guards = ({item for item in group if item[2]} for group in (guards, first_guards))
        pure_fill_branches = (not baseline_unproved and not first_unproved and not second_unproved
                              and first_rank == second_rank == CLASSES.index('fill_only'))
        postgres_check_only = first_checks_only and selected_dialect == 'postgresql' and not node.orelse
        if ((not (appends or guards) and not (pure_fill_branches or postgres_check_only))
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
        parent = self.parents.get(node)
        direct = isinstance(parent, (ast.Expr, ast.Assign, ast.AnnAssign)) and parent.value is node
        if not direct:
            if method == 'create_table':
                self.schema['tables'][table] = {'columns': [], 'primary_key': [], 'new': False}
            return
        if method == 'create_table':
            forget_checks(self.schema, table)
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
            elif method == 'create_table' and kind == 'CheckConstraint':
                name = next((self.value(key.value) for key in column.keywords if key.arg == 'name'), None)
                self.record_check(table, name, column.args[0])

    def record_check(self, table, name, expression):
        if table and isinstance(name, str) and name:
            self.schema.setdefault('checks', {}).setdefault(table, {})[name] = check_definition(self.value(expression))

    def check_sql(self, statement):
        if not isinstance(statement, ast.Expr) or not isinstance(node := statement.value, ast.Call):
            return None
        name = self.name(node.func)
        if name == 'alembic.op.execute' or (isinstance(node.func, ast.Attribute)
                and node.func.attr in {'execute', 'exec_driver_sql'} and self.connection(node.func.value)):
            if len(node.args) == 1 and not node.keywords:
                value = self.value(self.unwrap_sql(node.args[0]))
                return value if isinstance(value, str) else None
            return None
        if name not in {'alembic.op.drop_constraint', 'alembic.op.create_check_constraint'}:
            return None
        drop = name == 'alembic.op.drop_constraint'
        options = {key.arg: self.value(key.value) for key in node.keywords}
        if (len(options) != len(node.keywords) or len(node.args) != (2 if drop else 3)
                or options.keys() - ({'schema', 'type_'} if drop else {'schema'})
                or drop and options.get('type_') not in (None, 'check')):
            return None
        table, constraint = self.table_name(node, 1), self.value(node.args[0])
        if not table or not isinstance(constraint, str) or not constraint or '.' in constraint:
            return None
        owner = '.'.join('"' + part.replace('"', '""') + '"' for part in table.split('.'))
        label = '"' + constraint.replace('"', '""') + '"'
        if drop:
            return f'ALTER TABLE {owner} DROP CONSTRAINT {label}'
        expression = self.value(node.args[2])
        return f'ALTER TABLE {owner} ADD CONSTRAINT {label} CHECK ({expression})' if isinstance(expression, str) else None

    def independent(self, node):
        return isinstance(self.value(node), (str, int, float, bool)) or isinstance(self.resolve(node), ast.Call) \
            and self.constructor(self.resolve(node)) == 'bindparam' and self.expression(node)

    def unwrap_sql(self, node):
        node = self.resolve(node)
        if isinstance(node, ast.Call) and self.name(node.func) == 'sqlalchemy.text' \
                and len(node.args) == 1 and not node.keywords:
            return self.resolve(node.args[0])
        return node

    def statement(self, node):
        node = self.unwrap_sql(node)
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
                return 'fill_only', ''
        if kind == 'insert':
            values = {key.arg: self.value(key.value) for call in setters for key in call.keywords}
            keys = known.get('primary_key', [])
            if known.get('new') and keys and len(setters) == 1 and not setters[0].args \
                    and any(getattr(call.func, 'attr', '') == 'on_conflict_do_nothing' for call in calls) \
                    and all(isinstance(values.get(key), (str, int)) for key in keys):
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
            if (method == 'create_check_constraint' and self.function == 'upgrade' and len(node.args) == 3
                    and isinstance(self.parents[node], ast.Expr)):
                self.record_check(self.table_name(node, 1), self.value(node.args[0]), node.args[2])
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
        if self.unproved:
            self.invalidate()
        return {'class': CLASSES[self.rank], 'findings': self.findings, 'needs_schema': self.needs_schema}


@lru_cache(maxsize=256)
def schema_at(ancestors):
    if not ancestors:
        return {'tables': {}}
    schema = deepcopy(schema_at(ancestors[:-1]))
    clear_execution_proofs(schema)
    for table in schema['tables'].values():
        table['new'] = False
    Analyzer(ancestors[-1], schema).run()
    clear_execution_proofs(schema)
    return schema


@lru_cache(maxsize=512)
def analyze_sources(source, ancestors, branched=False):
    # Source-keyed prefix reuse avoids reanalyzing every ancestor for every revision.
    schema = deepcopy(schema_at(ancestors))
    if branched:
        # A sibling branch may have replaced a CHECK this chain cannot see, so no recorded definition is proof.
        schema.pop('checks', None)
    for table in schema['tables'].values():
        table['new'] = False
    return Analyzer(source, schema).run()


def analyze(path):
    path = Path(path)
    source = path.read_text(encoding='utf-8')
    direct = analyze_sources(source, ())
    if not direct['needs_schema']:
        return direct
    previous = assigned(parse(source), 'down_revision')
    ancestors, seen, children = [], set(), {}
    if previous:
        files = {}
        for item in path.parent.glob('*.py'):
            if item.name != '__init__.py':
                tree = parse(item.read_text(encoding='utf-8'))
                files[assigned(tree, 'revision')] = item
                parents = assigned(tree, 'down_revision')
                for parent in parents if isinstance(parents, (tuple, list)) else [parents]:
                    children[parent] = children.get(parent, 0) + 1
        while isinstance(previous, str) and previous not in seen and previous in files:
            seen.add(previous)
            text = files[previous].read_text(encoding='utf-8')
            ancestors.append(text)
            previous = assigned(parse(text), 'down_revision')
    branched = any(children.get(revision, 0) > 1 for revision in seen)
    return analyze_sources(source, tuple(reversed(ancestors)), branched)
