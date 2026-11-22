"""Every API 503 error code has an explicit client outcome classification."""
import ast
from pathlib import Path

from packages.contracts.schemas import UNAVAILABLE_OUTCOME


ROOT = Path(__file__).resolve().parents[1]
API = ROOT / 'apps' / 'api'


def _argument(call, index, keyword):
    if len(call.args) > index:
        return call.args[index]
    return next((item.value for item in call.keywords if item.arg == keyword), None)


def _json_error_code(call):
    content = call.args[0] if call.args else next((item.value for item in call.keywords if item.arg == 'content'), None)
    if not isinstance(content, ast.Dict):
        return None
    error = next((value for key, value in zip(content.keys, content.values)
                  if isinstance(key, ast.Constant) and key.value == 'error'), None)
    if not isinstance(error, ast.Dict):
        return None
    code = next((value for key, value in zip(error.keys, error.values)
                 if isinstance(key, ast.Constant) and key.value == 'code'), None)
    return code if isinstance(code, ast.Constant) and isinstance(code.value, str) else None


def _503_codes():
    codes = set()
    dynamic = []
    for path in sorted(API.rglob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = function.id if isinstance(function, ast.Name) else function.attr if isinstance(function, ast.Attribute) else None
            if name not in {'JSONResponse', 'Problem', 'require', '__init__'}:
                continue
            if name == 'JSONResponse':
                status = next((item.value for item in node.keywords if item.arg == 'status_code'), None)
                code = _json_error_code(node)
                if not isinstance(status, ast.Constant) or status.value != 503 or code is None:
                    continue
                codes.add(code.value)
                continue
            status = _argument(node, 0 if name in {'Problem', '__init__'} else 3, 'status')
            if not isinstance(status, ast.Constant) or status.value != 503:
                continue
            code = _argument(node, 1, 'code')
            if isinstance(code, ast.Constant) and isinstance(code.value, str):
                codes.add(code.value)
            else:
                dynamic.append((path.relative_to(ROOT).as_posix(), node.lineno, ast.unparse(code)))
    assert not dynamic, dynamic
    return codes


def test_every_api_503_error_code_has_one_outcome_classification():
    codes = _503_codes()
    assert set(UNAVAILABLE_OUTCOME) == codes
    assert set(UNAVAILABLE_OUTCOME.values()) == {'definite', 'unknown'}
