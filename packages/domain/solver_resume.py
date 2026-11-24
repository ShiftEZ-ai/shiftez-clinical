"""Version-bound, JSON-safe prepared CP-SAT models and solution hints."""
import hashlib
import json
import base64
import gzip
import io

from ortools import __version__ as solver_version
from ortools.sat.python import cp_model

from .solver_rules import COMPILER_VERSION


PREPARATION_FORMAT = 12
# Private keys are transient unless they carry rule semantics or approved source provenance.
SEMANTIC_PREPARATION_KEYS = frozenset({
    '_allocation_strengths', '_catalogue_v2_materialized', '_catalogue_sources',
    '_count_scope', '_draft_type_safety', '_quota_full_window', '_protected_interval',
    '_live_zero', '_live_zero_line_ids', '_live_mandatory_recheck',
    '_legacy_shadow_rows', '_legacy_own_lines',
    '_quota_line_indices', '_quota_template_people', '_zero_max_projection',
    '_overtime_fact', '_overtime_request', '_overtime_source', '_overtime_dates',
    '_source_default_only', '_source_identity', '_source_index', '_source_policy_id',
    '_solver_provenance',
    '_v2_source', '_v2_source_lines', '_v2_source_default', '_v2_row_indices',
    '_v2_row_templates', '_v2_changed_scope', '_v2_changed_dates', '_v2_setting_edits',
    '_v2_validation_definition', '_v2_base_variants', '_v2_base_exclusions',
})


def compress_state(state):
    """Encode the immutable preparation as JSON inside the existing gzip envelope."""
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode='wb', compresslevel=1, mtime=0) as compressed:
        compressed.write(json.dumps(state, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        # Match the sync flush the old text wrapper wrote before gzip closed.
        compressed.flush()
    return {'encoding': 'gzip', 'data': base64.b64encode(buffer.getbuffer()).decode('ascii'),
            'key': state.get('key')}


def decompress_state(state, *, check=None):
    """Read compressed preparation and legacy raw JSON checkpoints."""
    if check is not None:
        check()
    if state is None:
        return state
    if 'encoding' not in state:
        restored = state if 'checkpoint' not in state else {key: value for key, value in state.items() if key != 'checkpoint'}
    else:
        if state['encoding'] != 'gzip':
            raise ValueError('Unsupported prepared-state encoding')
        with gzip.GzipFile(fileobj=io.BytesIO(base64.b64decode(state['data'], validate=True))) as compressed:
            restored = json.load(compressed)
        if not isinstance(restored, dict) or restored.get('key') != state.get('key'):
            raise ValueError('Prepared-state envelope does not match its contents')
    if check is not None:
        check()
    return {**restored, **state['checkpoint']} if state.get('checkpoint') else restored


def resume_key(snapshot, options, feasibility_only, diagnostic_proposal):
    from packages.contracts.schemas import GENERATION_RUNTIME_ALIASES, GENERATION_RUNTIME_OPTIONS
    semantic = options.model_dump(mode='json')
    runtime = GENERATION_RUNTIME_OPTIONS | GENERATION_RUNTIME_ALIASES.keys() | {'search_seconds'}
    for name in runtime:
        semantic.pop(name, None)
    inputs = {name: value for name, value in snapshot.items()
              if not name.startswith('_') and name not in {
                  'generation_options', 'generation_defaults', 'generation_options_overridden', 'diagnostic_proposal',
                  'continuation', 'generation_manifest'}}
    defaults = {name: value for name, value in snapshot.get('generation_defaults', {}).items() if name not in runtime}
    return hashlib.sha256(json.dumps({
        'format': PREPARATION_FORMAT, 'inputs': inputs, 'defaults': defaults,
        'fingerprint': snapshot['fingerprint'], 'compiler': COMPILER_VERSION,
        'solver': solver_version, 'options': semantic,
        'feasibility_only': feasibility_only,
    }, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def pack(value):
    """Encode only data and linear expressions; never persist executable Python objects."""
    if type(value) in (str, int, float, bool, type(None)):
        return value
    if isinstance(value, cp_model.IntVar):
        return {'variable': value.index}
    if isinstance(value, cp_model.LinearExpr):
        flat = cp_model.FlatIntExpr(value)
        return {'expression': [[variable.index for variable in flat.vars], list(flat.coeffs), flat.offset]}
    if isinstance(value, dict):
        items = [(key, item) for key, item in value.items()
                 if not isinstance(key, str) or not key.startswith('_') or key in SEMANTIC_PREPARATION_KEYS]
        if all(isinstance(key, str) for key, _ in items):
            # JSON objects avoid per-entry lists and let the decoder share repeated property names.
            return {'mapping': {key: pack(item) for key, item in items}}
        return {'mapping': [[pack(key), pack(item)] for key, item in items]}
    if isinstance(value, (tuple, set)):
        return {'tuple' if isinstance(value, tuple) else 'set': [pack(item) for item in value]}
    if isinstance(value, list):
        return [pack(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f'Unsupported prepared-model data: {type(value).__name__}')


def unpack(value, model, *, check=None):
    # One wrapper per referenced variable, scoped to this restore (never shared between models).
    return _unpack(value, model, {}, check, {})


def _variable(index, model, variables, check):
    if type(index) is not int:
        raise ValueError('Invalid prepared-model variable index')
    if index not in variables:
        if check is not None:
            check()
        variables[index] = model.get_int_var_from_proto_index(index)
    return variables[index]


def _unpack(value, model, variables, check, strings):
    if type(value) is str:
        return strings.setdefault(value, value)
    if isinstance(value, list):
        return [_unpack(item, model, variables, check, strings) for item in value]
    if not isinstance(value, dict):
        return value
    if 'variable' in value:
        return _variable(value['variable'], model, variables, check)
    if 'expression' in value:
        indices, coefficients, offset = value['expression']
        return cp_model.LinearExpr.weighted_sum(
            [_variable(index, model, variables, check) for index in indices], coefficients) + offset
    if 'mapping' in value:
        mapping = value['mapping']
        if isinstance(mapping, dict):
            if not all(isinstance(key, str) for key in mapping):
                raise ValueError('Invalid prepared-model data')
            return {key: _unpack(item, model, variables, check, strings) for key, item in mapping.items()}
        return {_unpack(key, model, variables, check, strings): _unpack(item, model, variables, check, strings) for key, item in mapping}
    if 'tuple' in value:
        return tuple(_unpack(item, model, variables, check, strings) for item in value['tuple'])
    if 'set' in value:
        return set(_unpack(item, model, variables, check, strings) for item in value['set'])
    raise ValueError('Invalid prepared-model data')


def prepared_state(key, prepared):
    model = prepared['model']
    return {'key': key, 'model': str(model.proto),
            'prepared': pack({name: value for name, value in prepared.items() if name != 'model'}),
            'hint': [], 'has_solution': False}


def restore(state, key, *, check=None):
    if check is not None:
        check()
    if not state or state.get('key') != key or not state.get('model'):
        return None
    model = cp_model.CpModel()
    if not model.proto.parse_text_format(state['model']):
        raise ValueError('Invalid prepared CP-SAT model')
    if check is not None:
        check()
    if model.validate():
        raise ValueError('Invalid prepared CP-SAT model')
    if check is not None:
        check()
    model.rebuild_constant_map()
    if check is not None:
        check()
    restored = unpack(state['prepared'], model, check=check)
    if check is not None:
        check()
    return {**restored, 'model': model}
