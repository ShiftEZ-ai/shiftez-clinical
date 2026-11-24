"""Conservative proofs for SQL executed by migrations; never execute migration SQL."""
import re

MAX_BATCH = 1000
CURRENT_SCHEMA = '__migration_current_schema__'
EXECUTION_PROOFS = ('append_functions', 'append_targets', 'guard_functions', 'guard_targets', 'current_schema')
_IDENT = re.compile(r'[A-Za-z_][A-Za-z_0-9$]*\Z|"(?:[^"]|"")+"\Z')
_NUMBER = re.compile(r'\d+(?:\.\d+)?\Z')
_TYPES = {'VARCHAR', 'CHAR', 'TEXT', 'INTEGER', 'INT', 'BIGINT', 'SMALLINT', 'BOOLEAN', 'BOOL',
          'TIMESTAMP', 'TIME', 'DATE', 'JSON', 'JSONB', 'NUMERIC', 'DECIMAL', 'FLOAT', 'DOUBLE',
          'REAL', 'UUID', 'BYTEA'}
# These are read-only SQL expressions.  Migration DML may use them when a
# NULL-guarded fill computes a value from the row it is filling; mutating or
# user-defined calls remain fail-closed below.
_SYNTAX_CALLS = {
    'CAST', 'SELECT', 'IN', 'EXISTS', 'VALUES', 'AND', 'OR', 'NOT',
    'COUNT', 'TRIM', 'UPPER', 'SUBSTR', 'INSTR', 'SPLIT_PART', 'COALESCE', 'WHEN',
}


def clear_execution_proofs(schema):
    for key in EXECUTION_PROOFS:
        schema.pop(key, None)


def _forget_function(tokens, schema):
    tail = tokens[2:] if tokens[:2] in (['CREATE', 'FUNCTION'], ['DROP', 'FUNCTION']) else (
        tokens[4:] if tokens[:4] == ['CREATE', 'OR', 'REPLACE', 'FUNCTION'] else [])
    if tail[:2] == ['IF', 'EXISTS']:
        tail = tail[2:]
    if tail:
        name, _ = _name(tail)
        for key in ('append_functions', 'guard_functions'):
            schema.get(key, {}).pop(name, None)


def clear_execution_proofs(schema):
    for key in EXECUTION_PROOFS:
        schema.pop(key, None)


def _forget_function(tokens, schema):
    tail = tokens[2:] if tokens[:2] in (['CREATE', 'FUNCTION'], ['DROP', 'FUNCTION']) else (
        tokens[4:] if tokens[:4] == ['CREATE', 'OR', 'REPLACE', 'FUNCTION'] else [])
    if tail[:2] == ['IF', 'EXISTS']:
        tail = tail[2:]
    if tail:
        name, _ = _name(tail)
        for key in ('append_functions', 'guard_functions'):
            schema.get(key, {}).pop(name, None)


def _tokens(sql):
    tokens, i = [], 0
    while i < len(sql):
        if sql[i].isspace():
            i += 1
        elif sql.startswith('--', i):
            end = sql.find('\n', i)
            i = len(sql) if end < 0 else end + 1
        elif sql.startswith('/*', i):
            depth, i = 1, i + 2
            while depth and i < len(sql):
                if sql.startswith('/*', i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith('*/', i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if depth:
                raise ValueError('unterminated SQL comment')
        elif sql[i] in "'\"" or (sql[i:i + 1].upper() == 'E' and sql[i + 1:i + 2] == "'"):
            start = i
            escaped = sql[i:i + 1].upper() == 'E'
            i += int(escaped)
            quote, i = sql[i], i + 1
            while i < len(sql):
                if escaped and sql[i] == '\\':
                    i += 2
                elif sql[i] == quote:
                    i += 1
                    if sql[i:i + 1] != quote:
                        break
                    i += 1
                else:
                    i += 1
            else:
                raise ValueError('unterminated SQL quote')
            tokens.append(sql[start:i])
        elif sql[i] == '$' and (match := re.match(r'\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$', sql[i:])):
            tag, start = match.group(), i
            end = sql.find(tag, i + len(tag))
            if end < 0:
                raise ValueError('unterminated SQL routine body')
            i = end + len(tag)
            tokens.append(sql[start:i])
        elif match := re.match(r'[A-Za-z_][A-Za-z_0-9$]*|\d+(?:\.\d+)?|:[A-Za-z_][A-Za-z_0-9]*|::|<=|>=|<>|!=|\|\||[-+*/%=<>,.();?\[\]]', sql[i:]):
            tokens.append(match.group().upper() if _IDENT.fullmatch(match.group()) else match.group())
            i += len(match.group())
        else:
            raise ValueError('unrecognized SQL token')
    return tokens


def _parts(tokens, separator):
    parts, start, depth = [], 0, 0
    for i, token in enumerate(tokens):
        depth += token == '('
        depth -= token == ')'
        if depth < 0:
            raise ValueError('unbalanced SQL parentheses')
        if depth == 0 and token == separator:
            parts.append(tokens[start:i])
            start = i + 1
    if depth:
        raise ValueError('unbalanced SQL parentheses')
    return parts + [tokens[start:]]


def _top_level_index(tokens, token):
    """Find a keyword outside nested expressions/subqueries."""
    depth = 0
    for index, item in enumerate(tokens):
        if item == '(':
            depth += 1
        elif item == ')':
            depth -= 1
        elif depth == 0 and item == token:
            return index
    return -1


def _statements(tokens):
    statements, current, trigger_body, cases = [], [], False, 0
    for token in tokens:
        current.append(token)
        if current[:2] == ['CREATE', 'TRIGGER'] and token == 'BEGIN':
            trigger_body = True
        elif trigger_body and token == 'CASE':
            cases += 1
        elif trigger_body and token == 'END':
            if cases:
                cases -= 1
            else:
                trigger_body = False
        if token == ';' and not trigger_body:
            statements.append(current[:-1])
            current = []
    if trigger_body:
        raise ValueError('unterminated SQL trigger')
    if current:
        statements.append(current)
    return [statement for statement in statements if statement]


def _identifier(token):
    return token[1:-1].replace('""', '"') if token.startswith('"') else token.lower()


def _name(tokens):
    if not tokens or not _IDENT.fullmatch(tokens[0]):
        return None, tokens
    if '.' in _identifier(tokens[0]):
        return None, tokens
    names, tokens = [tokens[0]], tokens[1:]
    while len(tokens) >= 2 and tokens[0] == '.' and _IDENT.fullmatch(tokens[1]):
        if '.' in _identifier(tokens[1]):
            return None, tokens
        names.append(tokens[1])
        tokens = tokens[2:]
    return '.'.join(_identifier(name) for name in names), tokens


def _literal(token):
    return bool(_NUMBER.fullmatch(token) or token in {'NULL', 'TRUE', 'FALSE'} or token.startswith("'"))


def _check_in_list(tokens):
    if len(tokens) < 5 or tokens[1:3] != ['IN', '('] or tokens[-1] != ')' or not _IDENT.fullmatch(tokens[0]):
        return None
    values = tokens[3:-1:2]
    if tokens[4:-1:2] != [','] * (len(values) - 1):
        return None
    if all(value.startswith("'") and '\\' not in value for value in values):
        kind = 'text'
    elif all(re.fullmatch(r'[0-9]{1,9}', value) for value in values):
        kind, values = 'integer', [int(value) for value in values]
    elif all(value in {'TRUE', 'FALSE'} for value in values):
        kind = 'boolean'
    else:
        return None
    return _identifier(tokens[0]), kind, frozenset(values)


def check_definition(sql):
    """The source proof for a plain CHECK membership expression."""
    try:
        return _check_in_list(_tokens(sql)) if isinstance(sql, str) else None
    except ValueError:
        return None


def forget_checks(schema, table):
    if table in schema.get('checks', {}):
        del schema['checks'][table]


def _named_check(tokens):
    if (len(tokens) > 5 and tokens[0] == 'CONSTRAINT' and _IDENT.fullmatch(tokens[1])
            and tokens[2:4] == ['CHECK', '('] and tokens[-1] == ')'):
        definition = _check_in_list(tokens[4:-1])
        if definition:
            return _identifier(tokens[1]), definition
    return None


def _alter_actions(tokens):
    return _name(tokens[2:]) if tokens[:2] == ['ALTER', 'TABLE'] else (None, [])


def _replace_check(schema, first, second=()):
    table, actions = _alter_actions(first)
    if not table or CURRENT_SCHEMA in table or actions[:2] != ['DROP', 'CONSTRAINT']:
        return False
    if second:
        second_table, add = _alter_actions(second)
        if second_table != table:
            return False
        parts = [actions, add]
    else:
        parts = _parts(actions, ',')
    if len(parts) != 2:
        return False
    drop, add = parts
    if len(drop) != 3 or not _IDENT.fullmatch(drop[2]) or add[:1] != ['ADD']:
        return False
    replacement = _named_check(add[1:])
    if not replacement or replacement[0] != _identifier(drop[2]):
        return False
    name, new = replacement
    old = schema.get('checks', {}).get(table, {}).get(name)
    if not old or old[:2] != new[:2] or not old[2] < new[2]:
        return False
    schema['checks'][table][name] = new
    return True


def check_replacement(schema, sql, following=None):
    """Consume one ALTER or an adjacent DROP/ADD pair, with no intervening effects."""
    try:
        statements = _statements(_tokens(sql))
        if following is not None:
            other = _statements(_tokens(following))
            if len(statements) != 1 or len(other) != 1:
                return False
            statements += other
        return len(statements) in {1, 2} and _replace_check(schema, *statements)
    except ValueError:
        return False


def schema_template(sql, holes):
    """Every substitution must occupy a complete schema identifier, including inside bodies."""
    def count(tokens):
        found = 0
        for i, token in enumerate(tokens):
            if token.startswith('$'):
                tag = re.match(r'\$[^$]*\$', token).group()
                found += count(_tokens(token[len(tag):-len(tag)]))
            elif CURRENT_SCHEMA in token:
                if (token != '"' + CURRENT_SCHEMA + '"' or tokens[i + 1:i + 2] != ['.']
                        or i and tokens[i - 1] == '.' or i + 2 >= len(tokens)
                        or not _IDENT.fullmatch(tokens[i + 2])):
                    raise ValueError('schema substitution is not an identifier qualifier')
                found += 1
        return found
    try:
        return count(_tokens(sql)) == holes
    except (ValueError, IndexError):
        return False


def _read_expression(tokens):
    if any(token in {'DELETE', 'INSERT', 'UPDATE', 'TRUNCATE', 'DROP', 'ALTER', 'INTO', 'CALL',
                     'DO', 'EXECUTE', 'OPERATOR', 'COLLATE'} for token in tokens):
        return False
    for i, token in enumerate(tokens[:-1]):
        if token == '::' and (tokens[i + 1] not in _TYPES or tokens[i + 2:i + 3] == ['.']):
            return False
        if _IDENT.fullmatch(token) and tokens[i + 1] == '(':
            if token not in _SYNTAX_CALLS:
                return False
            if i >= 1 and tokens[i - 1] == '.':
                return False
            if token == 'CAST':
                depth = 0
                for j in range(i + 1, len(tokens)):
                    depth += tokens[j] == '('
                    depth -= tokens[j] == ')'
                    if tokens[j] == 'AS' and depth == 1:
                        if j + 1 >= len(tokens) or tokens[j + 1] not in _TYPES or tokens[j + 2:j + 3] == ['.']:
                            return False
                    if depth == 0:
                        break
    return True


def _routine(tokens):
    prefix = ['CREATE', 'OR', 'REPLACE', 'FUNCTION'] if tokens[:3] == ['CREATE', 'OR', 'REPLACE'] else ['CREATE', 'FUNCTION']
    if tokens[:len(prefix)] != prefix:
        return False
    _, tail = _name(tokens[len(prefix):])
    if not tail or tail[0] != '(' or 'AS' not in tail or 'LANGUAGE' not in tail:
        return False
    body_index, language_index = tail.index('AS') + 1, tail.index('LANGUAGE') + 1
    if body_index >= len(tail) or language_index >= len(tail):
        return False
    if tail[language_index].strip("'").upper() not in {'SQL', 'PLPGSQL'}:
        return False
    body = tail[body_index]
    if not body.startswith(("'", '$')) or any(t.startswith(("'", '$')) for t in tail[body_index + 1:] if t != tail[language_index]):
        return False
    # The body is stored, not run. Reject unrecognized executable header expressions.
    header = tail[:body_index - 1] + tail[body_index + 1:]
    if any(t in {'SELECT', 'INSERT', 'DELETE', 'UPDATE', 'CALL', 'DO', 'EXECUTE', 'SUPPORT'} for t in header):
        return False
    return body_index == len(tail) - 1 or tail[body_index + 1:body_index + 2] == ['LANGUAGE']


def _additive_routine(tokens):
    # Replacements can disable an inherited evidence guard without writing a row.
    if tokens[:2] != ['CREATE', 'FUNCTION'] or not _routine(tokens):
        return False
    _, tail = _name(tokens[2:])
    if 'DEFAULT' in tail or 'TRANSFORM' in tail:
        return False
    body = tail[tail.index('AS') + 1]
    if body.startswith("'"):
        body = body[1:-1].replace("''", "'")
    else:
        tag = re.match(r'\$[^$]*\$', body).group()
        body = body[len(tag):-len(tag)]
    body = _tokens(body)
    language = tail[tail.index('LANGUAGE') + 1].strip("'").upper()
    if language == 'SQL':
        statements = _statements(body)
        return bool(statements) and all(s[:1] == ['SELECT'] and _read_expression(s) for s in statements)
    if body[-1:] == [';']:
        body = body[:-1]
    if body[:1] != ['BEGIN'] or body[-2:] != [';', 'END']:
        return False
    statement = body[1:-2]
    return (statement[:1] == ['RETURN'] and len(statement) > 1 and ';' not in statement
            and statement[1] not in {'NEXT', 'QUERY'} and _read_expression(statement[1:])
            or len(statement) == 3 and statement[:2] == ['RAISE', 'EXCEPTION'] and statement[2].startswith("'"))


def _grant(tokens):
    if tokens[:1] in (['REVOKE'], ['GRANT']) and 'ON' in tokens:
        on = tokens.index('ON')
        separator = 'FROM' if tokens[0] == 'REVOKE' else 'TO'
        privileges = tokens[1:on]
        target, rest = _name(tokens[on + 1:])
        if (privileges and all(token in {'ALL', 'PRIVILEGES', 'SELECT', 'INSERT', 'UPDATE', 'DELETE',
                                         'REFERENCES', 'TRIGGER', 'TRUNCATE', ','} for token in privileges)
                and target and len(rest) == 2 and rest[0] == separator and _IDENT.fullmatch(rest[1])):
            return True
    if tokens[:1] == ['REVOKE']:
        prefix, separator = ['REVOKE', 'ALL', 'ON', 'FUNCTION'], 'FROM'
    else:
        prefix, separator = ['GRANT', 'EXECUTE', 'ON', 'FUNCTION'], 'TO'
    if tokens[:len(prefix)] != prefix:
        return False
    name, tail = _name(tokens[len(prefix):])
    if not name or not tail or tail[0] != '(' or ')' not in tail:
        return False
    end = tail.index(')')
    return (all(t in _TYPES or t == ',' for t in tail[1:end])
            and tail[end + 1:end + 2] == [separator]
            and len(tail[end + 2:]) == 1 and bool(_IDENT.fullmatch(tail[-1])))


def _role_grant(tokens):
    if len(tokens) != 2 or tokens[0] != 'DO' or not tokens[1].startswith('$'):
        return False
    tag = re.match(r'\$[^$]*\$', tokens[1]).group()
    body = _tokens(tokens[1][len(tag):-len(tag)])
    if body[-1:] == [';']:
        body = body[:-1]
    if body[4:6] == ['SELECT', '1']:
        body = body[:5] + body[6:]
    prefix = ['BEGIN', 'IF', 'EXISTS', '(', 'SELECT', 'FROM', 'PG_ROLES', 'WHERE', 'ROLNAME', '=']
    if body[:len(prefix)] != prefix or not body[len(prefix):len(prefix) + 1] or not body[len(prefix)].startswith("'"):
        return False
    rest = body[len(prefix) + 1:]
    if rest[:2] != [')', 'THEN'] or rest[-5:] != [';', 'END', 'IF', ';', 'END']:
        return False
    grant = rest[2:-5]
    return _grant(grant) and _identifier(grant[-1]) == body[len(prefix)][1:-1]


def _column_definition(tokens, checks):
    if len(tokens) < 2 or not _IDENT.fullmatch(tokens[0]) or tokens[1] not in _TYPES:
        return False
    tail = tokens[2:]
    if tail[:1] == ['(']:
        if ')' not in tail:
            return False
        end = tail.index(')')
        if not all(t.isdigit() or t == ',' for t in tail[1:end]):
            return False
        tail = tail[end + 1:]
    if tail[:2] in (['WITH', 'TIME'], ['WITHOUT', 'TIME']) and tail[2:3] == ['ZONE']:
        tail = tail[3:]
    not_null, default, check_name = False, None, None
    while tail:
        if tail[:1] == ['CONSTRAINT'] and len(tail) > 2 and _IDENT.fullmatch(tail[1]):
            check_name = _identifier(tail[1]) if tail[2:3] == ['CHECK'] else None
            tail = tail[2:]
        elif tail[:2] == ['NOT', 'NULL']:
            not_null, tail = True, tail[2:]
        elif tail[:2] in (['PRIMARY', 'KEY'],):
            not_null, tail = True, tail[2:]
        elif tail[:1] in (['NULL'], ['UNIQUE']):
            tail = tail[1:]
        elif tail[:1] == ['DEFAULT'] and len(tail) >= 2 and (_literal(tail[1]) or tail[1] == 'CURRENT_TIMESTAMP'):
            default, tail = tail[1], tail[2:]
        elif tail[:1] == ['REFERENCES']:
            table, reference = _name(tail[1:])
            if not table or reference[:1] != ['('] or ')' not in reference:
                return False
            end = reference.index(')')
            if not all(_IDENT.fullmatch(t) or t == ',' for t in reference[1:end]):
                return False
            tail = reference[end + 1:]
        elif tail[:2] == ['CHECK', '(']:
            depth = 0
            for end in range(1, len(tail)):
                depth += tail[end] == '('
                depth -= tail[end] == ')'
                if depth == 0:
                    break
            expression = tail[2:end]
            if depth or not expression or 'SELECT' in expression or not _read_expression(expression):
                return False
            if check_name is not None:
                checks[check_name] = _check_in_list(expression)
                check_name = None
            tail = tail[end + 1:]
        else:
            return False
    return not_null, default


def _index(tokens):
    if tokens[:2] not in (['CREATE', 'INDEX'], ['CREATE', 'UNIQUE']):
        return False
    tail = tokens[2:] if tokens[1] == 'INDEX' else tokens[3:] if tokens[2:3] == ['INDEX'] else []
    if tail[:3] == ['IF', 'NOT', 'EXISTS']:
        tail = tail[3:]
    name, tail = _name(tail)
    if not name or tail[:1] != ['ON']:
        return False
    table, tail = _name(tail[1:])
    if not table or tail[:1] != ['(']:
        return False
    depth = 0
    for end, token in enumerate(tail):
        depth += token == '('
        depth -= token == ')'
        if depth == 0:
            break
    if depth:
        return False
    suffix = tail[end + 1:]
    if suffix and (suffix[:1] != ['WHERE'] or len(suffix) == 1 or not _read_expression(suffix[1:])):
        return False
    for column in _parts(tail[1:end], ','):
        if column[-2:] in (['NULLS', 'FIRST'], ['NULLS', 'LAST']):
            column = column[:-2]
        if column[-1:] in (['ASC'], ['DESC']):
            column = column[:-1]
        if len(column) == 1 and _IDENT.fullmatch(column[0]):
            continue
        expression = column[0:1] == ['(']
        if not expression or column[-1:] != [')'] or 'SELECT' in column or not _read_expression(column):
            return False
        start = 0 if column[0] == '(' else 1
        if column[start:start + 1] != ['(']:
            return False
        depth = 0
        for last in range(start, len(column)):
            depth += column[last] == '('
            depth -= column[last] == ')'
            if depth == 0:
                break
        if last != len(column) - 1:
            return False
    return True


def _schema_statement(tokens, schema):
    checks = {}
    if tokens[:2] == ['CREATE', 'TABLE']:
        tail = tokens[2:]
        conditional = tail[:3] == ['IF', 'NOT', 'EXISTS']
        if conditional:
            tail = tail[3:]
        name, tail = _name(tail)
        if not name or tail[:1] != ['('] or tail[-1:] != [')']:
            return False
        for part in _parts(tail[1:-1], ','):
            named = _named_check(part)
            if named:
                checks[named[0]] = named[1]
            elif _column_definition(part, checks) is False:
                return False
        forget_checks(schema, name)
        if checks and not conditional:
            schema.setdefault('checks', {})[name] = checks
        return True
    if _index(tokens):
        return True
    if tokens[:2] == ['ALTER', 'TABLE']:
        table, tail = _name(tokens[2:])
        if not table or tail[:1] != ['ADD']:
            return False
        tail = tail[2:] if tail[1:2] == ['COLUMN'] else tail[1:]
        column = _column_definition(tail, checks)
        if column is False or column[0] and column[1] in (None, 'NULL'):
            return False
        if checks:
            schema.setdefault('checks', {}).setdefault(table, {}).update(checks)
        return True
    return False


def _conjuncts(tokens):
    if any(token in {'CASE', 'BETWEEN', 'OR', 'NOT'} for token in tokens):
        raise ValueError('unsupported SQL boolean expression')
    while tokens[:1] == ['('] and tokens[-1:] == [')']:
        depth = 0
        for i, token in enumerate(tokens):
            depth += token == '('
            depth -= token == ')'
            if depth == 0:
                break
        if i != len(tokens) - 1:
            break
        tokens = tokens[1:-1]
    parts = _parts(tokens, 'AND')
    if len(parts) > 1:
        return [part for group in parts for part in _conjuncts(group)]
    # Only the recognized bounded-key subquery may contain parentheses after conjunction groups are removed.
    if 'AND' in tokens or '(' in tokens and (
            tokens[1:4] != ['IN', '(', 'SELECT'] or tokens[-1:] != [')'] or tokens.count('(') != 1):
        raise ValueError('unsupported nested SQL boolean expression')
    return [tokens]


def _fill_update(tokens, schema):
    table, tail = _name(tokens[1:])
    info = schema.get('tables', {}).get(table, {})
    where_index = _top_level_index(tail, 'WHERE')
    if not info or tail[:1] != ['SET'] or where_index < 0 or 'OR' in tail:
        return False
    assigned = _parts(tail[1:where_index], ',')
    targets = set()
    for assignment in assigned:
        if len(assignment) < 3 or assignment[1] != '=' or _identifier(assignment[0]) not in info['columns']:
            return False
        targets.add(_identifier(assignment[0]))
    predicates = _conjuncts(tail[where_index + 1:])
    guarded = {_identifier(p[0]) for p in predicates if len(p) == 3 and p[1:] == ['IS', 'NULL']}
    if not targets <= guarded or not _read_expression(tail[1:where_index] + tail[where_index + 1:]):
        return False
    keys = info.get('primary_key', [])
    # A legacy table's original schema may be intentionally unavailable after
    # an unproved ancestor.  A current migration can still prove a bounded
    # fill for a column it has just added: the id-limited subquery bounds the
    # rows and the NULL guard makes the statement rerunnable.  Existing-column
    # writes continue to require the recorded primary key proof.
    inferred_added_key = not keys and targets <= set(info.get('added_columns', []))
    if len(keys) != 1 and not inferred_added_key:
        return False
    if inferred_added_key:
        keys = ['id']
    for p in predicates:
        if len(p) == 3 and _identifier(p[0]) == keys[0] and p[1] == '=' and (_literal(p[2]) or p[2].startswith(':')):
            return True
        if p[:2] == [keys[0].upper(), 'IN'] and p[2:4] == ['(', 'SELECT'] and p[-1:] == [')']:
            sub = p[4:-1]
            if sub[:3] != [keys[0].upper(), 'FROM', table.upper()] or 'LIMIT' not in sub or 'OFFSET' in sub:
                continue
            limit = sub[sub.index('LIMIT') + 1:]
            if len(limit) == 1 and limit[0].isdigit() and 1 <= int(limit[0]) <= MAX_BATCH:
                return True
    return False


def _fill_insert(tokens, schema):
    if tokens[:2] != ['INSERT', 'INTO']:
        return False
    table, tail = _name(tokens[2:])
    info = schema.get('tables', {}).get(table, {})
    if not info.get('new') or not info.get('primary_key') or tail[:1] != ['('] or ')' not in tail:
        return False
    end = tail.index(')')
    columns = tail[1:end:2]
    if tail[2:end:2] != [','] * max(0, len(columns) - 1) or not all(_IDENT.fullmatch(c) for c in columns):
        return False
    columns = [_identifier(c) for c in columns]
    if not set(info['primary_key']) <= set(columns) or not set(columns) <= set(info['columns']):
        return False
    tail = tail[end + 1:]
    if tail[:1] != ['VALUES'] or tail[-4:] != ['ON', 'CONFLICT', 'DO', 'NOTHING']:
        return False
    rows = _parts(tail[1:-4], ',')
    if not 1 <= len(rows) <= MAX_BATCH:
        return False
    for row in rows:
        if row[:1] != ['('] or row[-1:] != [')']:
            return False
        values = _parts(row[1:-1], ',')
        if len(values) != len(columns) or any(len(v) != 1 or not _literal(v[0]) for v in values):
            return False
        if any(values[columns.index(key)][0] == 'NULL' for key in info['primary_key']):
            return False
    return True


def _trigger(tokens):
    return tokens[:2] == ['CREATE', 'TRIGGER'] and ('BEGIN' in tokens and tokens[-1:] == ['END']
            or 'EXECUTE' in tokens and tokens[-1:] == [')'] and 'FUNCTION' in tokens)


def _new_table_insert(tokens, schema):
    """Copy read-only facts into a fresh table using a declared unique conflict key."""
    if tokens[:2] != ['INSERT', 'INTO']:
        return False
    table, tail = _name(tokens[2:])
    info = schema.get('tables', {}).get(table, {})
    if not info.get('new') or tail[:1] != ['('] or ')' not in tail:
        return False
    end = tail.index(')')
    columns = tail[1:end:2]
    if tail[2:end:2] != [','] * max(0, len(columns) - 1) or not columns:
        return False
    if not all(_IDENT.fullmatch(c) for c in columns):
        return False
    columns = [_identifier(c) for c in columns]
    tail = tail[end + 1:]
    if not set(columns) <= set(info['columns']) or tail.count('CONFLICT') != 1:
        return False
    conflict = tail.index('CONFLICT')
    suffix = tail[conflict - 1:]
    if suffix[:3] != ['ON', 'CONFLICT', '('] or suffix[-3:] != [')', 'DO', 'NOTHING']:
        return False
    keys = suffix[3:-3:2]
    if suffix[4:-3:2] != [','] * max(0, len(keys) - 1) or not all(_IDENT.fullmatch(k) for k in keys):
        return False
    keys = [_identifier(k) for k in keys]
    if keys not in info.get('unique_keys', []) or not set(keys) <= set(columns):
        return False
    source = tail[:conflict - 1]
    return bool(source and source[0] in {'SELECT', 'VALUES'} and _read_expression(source))


def _append_routine(tokens, schema):
    if tokens[:2] != ['CREATE', 'FUNCTION'] or not _routine(tokens):
        return False
    name, tail = _name(tokens[2:])
    if tail[:6] != ['(', ')', 'RETURNS', 'TRIGGER', 'LANGUAGE', 'PLPGSQL'] or tail[6:7] != ['AS']:
        return False
    body = tail[7]
    if not body.startswith('$'):
        return False
    tag = re.match(r'\$[^$]*\$', body).group()
    statements = _tokens(body[len(tag):-len(tag)])
    if statements[:1] != ['BEGIN'] or statements[-4:] != ['NEW', ';', 'END', ';']:
        return False
    parts = _statements(statements[1:-2])
    if len(parts) != 2 or parts[1] != ['RETURN', 'NEW'] or not _new_table_insert(parts[0], schema):
        return False
    schema.setdefault('append_functions', {})[name] = _name(parts[0][2:])[0]
    return True


def _guard_table(name, schema):
    prefix = schema.get('current_schema', '') + '.'
    if schema.get('current_schema') and name and name.startswith(prefix):
        name = name[len(prefix):]
    return name if schema.get('tables', {}).get(name, {}).get('new') else None


def _guard_predicate(tokens, schema):
    # Match dialects by the read predicate, with only the proven current-schema qualification removed.
    result, i = [], 0
    while i < len(tokens):
        if (schema.get('current_schema') and _identifier(tokens[i]) == schema['current_schema']
                and tokens[i + 1:i + 2] == ['.']):
            i += 2
        else:
            result.append(tokens[i])
            i += 1
    return tuple(result)


def _guard_routine(tokens, schema):
    if tokens[:2] != ['CREATE', 'FUNCTION']:
        return False
    name, tail = _name(tokens[2:])
    header = ['(', ')', 'RETURNS', 'TRIGGER', 'LANGUAGE', 'PLPGSQL', 'SECURITY', 'INVOKER',
              'SET', 'SEARCH_PATH', '=', 'PG_CATALOG', ',', 'PG_TEMP', 'AS']
    if not name or tail[:len(header)] != header or len(tail) != len(header) + 1 or not tail[-1].startswith('$'):
        return False
    tag = re.match(r'\$[^$]*\$', tail[-1]).group()
    body = _tokens(tail[-1][len(tag):-len(tag)])
    if body[-1:] == [';']:
        body = body[:-1]
    prefix = ['BEGIN', 'IF', 'TG_OP', '<>', "'INSERT'", 'THEN']
    if body[:len(prefix)] != prefix:
        return False
    body = body[len(prefix):]
    if (body[:2] != ['RAISE', 'EXCEPTION'] or len(body) < 7 or not body[2].startswith("'")
            or body[3:7] != [';', 'END', 'IF', ';']):
        return False
    body = body[7:]
    if body[:4] != ['IF', 'NOT', 'EXISTS', '('] or 'THEN' not in body:
        return False
    then = body.index('THEN')
    condition, refusal = body[1:then], body[then + 1:]
    if (condition[:4] != ['NOT', 'EXISTS', '(', 'SELECT'] or condition[-1:] != [')']
            or ';' in condition or not _read_expression(condition)
            or len(refusal) != 11 or refusal[:2] != ['RAISE', 'EXCEPTION']
            or not refusal[2].startswith("'")
            or refusal[3:] != [';', 'END', 'IF', ';', 'RETURN', 'NEW', ';', 'END']):
        return False
    _parts(condition, ',')
    schema.setdefault('guard_functions', {})[name] = _guard_predicate(condition, schema)
    return True


def _new_table_guard(tokens, schema):
    if tokens[:2] != ['CREATE', 'TRIGGER']:
        return False
    name, tail = _name(tokens[2:])
    if not name or tail[:1] != ['BEFORE'] or 'ON' not in tail:
        return False
    on = tail.index('ON')
    events, tail = tail[1:on], tail[on + 1:]
    table, tail = _name(tail)
    table = _guard_table(table, schema)
    if not table:
        return False
    if events == ['INSERT', 'OR', 'UPDATE', 'OR', 'DELETE']:
        if tail[:5] != ['FOR', 'EACH', 'ROW', 'EXECUTE', 'FUNCTION']:
            return False
        function, rest = _name(tail[5:])
        predicate = schema.get('guard_functions', {}).get(function)
        if predicate is None or rest != ['(', ')']:
            return False
        schema.setdefault('guard_targets', set()).update({(table, 'UPDATE', ()), (table, 'DELETE', ()),
                                                        (table, 'INSERT', predicate)})
        return True
    if events not in (['INSERT'], ['UPDATE'], ['DELETE']) or 'BEGIN' not in tail:
        return False
    begin = tail.index('BEGIN')
    when = tail[:begin]
    if when and (when[:4] != ['WHEN', 'NOT', 'EXISTS', '('] or when[-1:] != [')']
                 or ';' in when or not _read_expression(when[1:])):
        return False
    body = tail[begin + 1:]
    if (len(body) != 9 or body[:5] != ['SELECT', 'RAISE', '(', 'ABORT', ',']
            or not body[5].startswith("'") or body[6:] != [')', ';', 'END']):
        return False
    schema.setdefault('guard_targets', set()).add((table, events[0], _guard_predicate(when[1:], schema)))
    return True


def _additive_trigger(tokens, schema=None):
    schema = schema or {}
    if _trigger(tokens) and 'BEGIN' not in tokens:
        # Only an AFTER INSERT observer proved in this same migration may run.
        _, tail = _name(tokens[2:])
        if tail[:3] != ['AFTER', 'INSERT', 'ON']:
            return False
        _, tail = _name(tail[3:])
        if tail[:5] != ['FOR', 'EACH', 'ROW', 'EXECUTE', 'FUNCTION']:
            return False
        function, rest = _name(tail[5:])
        target = schema.get('append_functions', {}).get(function)
        if rest != ['(', ')'] or not schema.get('tables', {}).get(target, {}).get('new', False):
            return False
        schema.setdefault('append_targets', set()).add(target)
        return True
    if not _trigger(tokens) or 'BEGIN' not in tokens:
        return False  # An external PostgreSQL trigger function has no body proof here.
    begin = tokens.index('BEGIN')
    header = tokens[:begin]
    if 'WHEN' in header and not _read_expression(header[header.index('WHEN') + 1:]):
        return False
    statements = _statements(tokens[begin + 1:-1])
    for statement in statements:
        if (len(statement) == 7 and statement[:5] == ['SELECT', 'RAISE', '(', 'ABORT', ',']
                and statement[5].startswith("'") and statement[-1] == ')'):
            continue
        if _new_table_insert(statement, schema):
            schema.setdefault('append_targets', set()).add(_name(statement[2:])[0])
            continue
        if statement[:1] != ['SELECT'] or not _read_expression(statement):
            return False
    return bool(statements)


def _drop_routine(tokens):
    if tokens[:2] not in (['DROP', 'TRIGGER'], ['DROP', 'FUNCTION']):
        return False
    tail = tokens[2:]
    if tail[:2] == ['IF', 'EXISTS']:
        tail = tail[2:]
    name, tail = _name(tail)
    if not name:
        return False
    if tokens[1] == 'FUNCTION' and tail[:1] == ['('] and ')' in tail:
        end = tail.index(')')
        if not all(t in _TYPES or t == ',' for t in tail[1:end]):
            return False
        tail = tail[end + 1:]
    elif tokens[1] == 'TRIGGER' and tail[:1] == ['ON']:
        table, tail = _name(tail[1:])
        if not table:
            return False
    return tail in ([], ['RESTRICT'])


def _pragma(tokens):
    name, tail = _name(tokens[1:])
    if not name or len(name.split('.')) > 2:
        return False
    setting = name.split('.')[-1]
    if setting in {'schema_version', 'foreign_keys', 'writable_schema'}:
        if not tail:
            return True
        value = tail[1:] if tail[:1] == ['='] else tail[1:-1] if tail[:1] == ['('] and tail[-1:] == [')'] else []
        return len(value) == 1 and (value[0].isdigit() or value[0] in {'ON', 'OFF', 'TRUE', 'FALSE'})
    if setting not in {'table_info', 'table_xinfo', 'table_list', 'index_list', 'index_info', 'index_xinfo',
                       'foreign_key_list', 'foreign_key_check', 'integrity_check', 'quick_check'}:
        return False
    return not tail or (len(tail) == 3 and tail[0] == '(' and tail[-1] == ')'
                        and (_IDENT.fullmatch(tail[1]) or _literal(tail[1])))


def _pure_dml(tokens):
    if tokens[:1] == ['UPDATE']:
        table, tail = _name(tokens[1:])
        if tail[:1] == ['AS'] and len(tail) >= 2 and _IDENT.fullmatch(tail[1]):
            tail = tail[2:]
        if tail[:1] != ['SET']:
            return False
    elif tokens[:2] in (['INSERT', 'INTO'], ['DELETE', 'FROM']):
        table, tail = _name(tokens[2:])
    else:
        return False
    if (not table or not tail and tokens[:2] != ['DELETE', 'FROM'] or any(part.startswith('pg_') for part in table.split('.'))
            or table.split('.')[0] == 'information_schema'
            or table.split('.')[-1] in {'sqlite_schema', 'sqlite_master', 'sqlite_temp_schema', 'sqlite_temp_master'}):
        return False
    return _read_expression(tail)


def preserves_schema(sql):
    """Whether known SQL leaves inherited table/column/primary-key facts valid."""
    try:
        for tokens in _statements(_tokens(sql)):
            _parts(tokens, ',')
            if (_routine(tokens) or _trigger(tokens) or _index(tokens) or _drop_routine(tokens) or _grant(tokens)
                    or _role_grant(tokens) or _pure_dml(tokens)
                    or tokens[:1] == ['SELECT'] and _read_expression(tokens)
                    or tokens[:1] == ['PRAGMA'] and _pragma(tokens)):
                continue
            if tokens in (['BEGIN'], ['BEGIN', 'TRANSACTION'], ['COMMIT'], ['COMMIT', 'TRANSACTION'],
                          ['END'], ['END', 'TRANSACTION']):
                continue
            if tokens[:1] == ['BEGIN'] and tokens[1:2] in (['DEFERRED'], ['IMMEDIATE'], ['EXCLUSIVE']) and tokens[2:] in ([], ['TRANSACTION']):
                continue
            if ((tokens[:1] == ['SAVEPOINT'] or tokens[:1] == ['RELEASE']) and len(tokens) == 2
                    or tokens[:2] == ['RELEASE', 'SAVEPOINT'] and len(tokens) == 3) and _IDENT.fullmatch(tokens[-1]):
                continue
            return False
        return True
    except (ValueError, IndexError):
        return False


def analyze_sql(sql, *, schema=None):
    """Return a proven class and actionable uncertainty; unknown SQL always needs approval."""
    try:
        statements = _statements(_tokens(sql))
        result, consumed = 'additive', False
        proof = schema if schema is not None else {}
        for index, tokens in enumerate(statements):
            if consumed:
                consumed = False
                continue
            _parts(tokens, ',')  # Reject malformed or injected parenthesis boundaries.
            _forget_function(tokens, proof)
            if (_additive_routine(tokens) or _grant(tokens) or _schema_statement(tokens, proof)
                    or (tokens[:1] == ['SELECT'] and _read_expression(tokens))):
                continue
            if _replace_check(proof, tokens):
                continue
            if index + 1 < len(statements) and _replace_check(proof, tokens, statements[index + 1]):
                consumed = True
                continue
            if (_guard_routine(tokens, proof) or _new_table_guard(tokens, proof)
                    or _append_routine(tokens, proof) or _additive_trigger(tokens, schema)):
                continue
            if ((tokens[:1] == ['UPDATE'] and _fill_update(tokens, schema or {}))
                    or _fill_insert(tokens, schema or {}) or _new_table_insert(tokens, schema or {})):
                result = 'fill_only'
                continue
            clear_execution_proofs(proof)
            proof.pop('checks', None)
            return {'class': 'destructive', 'findings': ['SQL is destructive or not statically proven safe: ' + ' '.join(tokens[:6])]}
        return {'class': result, 'findings': []}
    except (ValueError, IndexError):
        if schema is not None:
            clear_execution_proofs(schema)
            schema.pop('checks', None)
        return {'class': 'destructive', 'findings': ['SQL cannot be statically parsed safely']}
