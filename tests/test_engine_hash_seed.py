"""The same snapshot must compile and solve identically in fresh Python processes."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.postgres
@pytest.mark.parametrize('metric', ['duties', 'pair_exposure', 'weekends'])
def test_balance_goal_is_stable_across_python_hash_seeds(pg, tmp_path, metric):
    h = pg
    typ = h.configure('assignment_types', {'name': 'Deterministic duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2 if metric == 'pair_exposure' else 1,
                                        'maximum': 2 if metric == 'pair_exposure' else 1}],
                       'allow_shared_services': True, 'shared_duty_reason': 'Synthetic shared coverage'}})['record']['id']
    rule = {'kind': 'balance_goal', 'metric': metric, 'period': 'rota', 'aim': 'equal_share', 'share_by': 'equal'}
    if metric == 'pair_exposure':
        rule.update(aim='min_per_pair', amount=1, partners={'match': 'all', 'conditions': [
            {'field': 'person_ids', 'op': 'any', 'values': h.seed['people']}]})
    h.policy(rule, severity='advisory')
    h.policy({'kind': 'maximum_participants', 'maximum': 2, 'aggregation': 'service'},
             {'service_ids': h.seed['services'][:2]})
    h.policy({'kind': 'allocation_preference', 'priority': 0, 'balance_by': 'duties',
              'balance_window': 'rolling', 'balance_window_days': 7}, severity='advisory')
    w = h.workspace(end='2026-10-18' if metric == 'weekends' else '2026-10-11')
    for day in (9, 10, 16, 17) if metric == 'weekends' else (4, 5, 6, 7):
        w = h.shift(w, typ=typ, people=[], start=f'2026-10-{day:02}T08:00:00+03:00',
                    end=f'2026-10-{day:02}T12:00:00+03:00')['workspace']
    data = h.snapshot(w)
    for shift in data['shifts']:
        shift['service_ids'] = h.seed['services'][:2]
    data['generation_options'] = {'shortlist_width': 0}
    source = tmp_path / 'snapshot.json'
    source.write_text(json.dumps(data), encoding='utf-8')
    script = '''import hashlib,json,sys
from packages.domain.cp_sat import generate
data=json.load(open(sys.argv[1],encoding="utf-8"))
models=[]
generate(data,1,workers=1,deterministic=True,feasibility_only=True,_on_model=lambda m:models.append(hashlib.sha256(str(m.proto).encode()).hexdigest()))
r=generate(data,1,workers=1,deterministic=True)
print(json.dumps({"status":r["status"],"model":models[0],"assignments":sorted((a["shift_id"],a["participant"]["person_id"]) for a in r["additions"])}))
'''
    results = []
    for seed in ('1', '2', '3', '4'):
        env = {**os.environ, 'PYTHONHASHSEED': seed, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
        result = json.loads(subprocess.check_output([sys.executable, '-c', script, str(source)],
                                                   env=env, text=True, timeout=60))
        assert result['status'] == 'candidate_feasible', result
        results.append(result)
    assert len({r['model'] for r in results}) == 1, results
    assert len({json.dumps(r['assignments']) for r in results}) == 1, results
