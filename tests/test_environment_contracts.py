"""Do not hide misspelled delegated journeys behind unavailable environment fixtures.

This imports named helpers only; it does NOT execute or qualify PG/browser tests.
"""
import ast
import importlib
from pathlib import Path
import pytest


@pytest.mark.parametrize('filename',['test_product_context_postgres.py','test_postgres_roles.py','test_browser_audit_environment.py','test_browser_staffing.py','test_browser_quotas.py','test_browser_operations.py'])
def test_authored_environment_journeys_reference_existing_local_functions(filename):
    tree=ast.parse((Path(__file__).parent/filename).read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node,ast.ImportFrom):
            name='tests.'+node.module if node.level and node.module else node.module or 'tests'
            module=importlib.import_module(name)
            for alias in node.names:
                if not hasattr(module,alias.name):
                    importlib.import_module(name+'.'+alias.name)
                assert hasattr(module,alias.name),(filename,name,alias.name)
