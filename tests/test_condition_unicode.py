"""Unicode text predicates agree in the domain, PostgreSQL and the shared client fixture."""
import json
from pathlib import Path

import pytest
from sqlalchemy import String, create_engine, literal, select

from apps.api.services.collection_filters import predicate
from packages.domain.conditions import EvaluationContext, evaluate


CASES = json.loads((Path(__file__).parent / 'fixtures' / 'condition_cases.json').read_text(encoding='utf-8'))
UNICODE_TEXT_CASES = [case for case in CASES['cases'] if case['id'].startswith('unicode-text-')]


@pytest.fixture(scope='module')
def unicode_postgres(postgres_worker_url):
    engine = create_engine(postgres_worker_url)
    try:
        with engine.connect() as db:
            yield db
    finally:
        engine.dispose()


@pytest.mark.postgres
@pytest.mark.parametrize('case', UNICODE_TEXT_CASES, ids=lambda case: case['id'])
def test_unicode_text_matching_agrees_with_postgres(unicode_postgres, case):
    context = EvaluationContext(timezone='Asia/Riyadh', week_start=0)
    held, tree = case['held'], case['tree']
    expected = evaluate(tree, held.get, context, row=held)
    actual = unicode_postgres.scalar(select(predicate(tree, {'name': literal(held['name'], String())}, context)))
    assert actual is expected is case['expected']
