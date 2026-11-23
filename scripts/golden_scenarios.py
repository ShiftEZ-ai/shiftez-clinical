"""Reviewed golden JSON -> public commands; no direct database writes or rule evaluator.

UUID5 command keys are stable; API-generated entity IDs are retained in the receipt.
Replaying identical inputs uses the API's idempotency records, including after a
partly completed seed. Changed commands require a reviewed scenario revision.
"""
import hashlib
import json
from pathlib import Path
import time
import uuid

from packages.contracts.schemas import GenerationOptions

PACK = Path(__file__).resolve().parents[1] / 'tests/scenarios/golden/v1'


def load_pack():
    manifest = json.loads((PACK / 'manifest.json').read_text(encoding='utf-8'))
    if manifest['version'] != 1 or len(set(manifest['scenarios'])) != len(manifest['scenarios']):
        raise ValueError('Invalid golden manifest')
    scenarios = []
    for name in manifest['scenarios']:
        path = (PACK / name).resolve()
        if not path.is_relative_to(PACK.resolve()):
            raise ValueError('Golden entry must stay inside its pack')
        spec = json.loads(path.read_text(encoding='utf-8'))
        if not spec['pressure'] or not spec['expect']['binding_rules']:
            raise ValueError('Golden entry needs pressure and binding rules')
        robustness = spec['expect']['seed_robustness']
        if (len(robustness['seeds']) != 3 or len(set(robustness['seeds'])) != 3
                or robustness['workers'] != 1 or robustness['seeds'] != manifest['seeds']):
            raise ValueError('Golden micro scenarios require three distinct seeds and one worker')
        GenerationOptions.model_validate(spec['inputs']['generation_options'])
        keys = [row['key'] for row in spec['commands']]
        if len(keys) != len(set(keys)):
            raise ValueError('Duplicate golden command key')
        scenarios.append(spec)
    if len({spec['id'] for spec in scenarios}) != len(scenarios):
        raise ValueError('Duplicate golden scenario ID')
    return scenarios


def command_key(spec, key):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"fmuc:golden:v1:{spec['id']}:{spec['revision']}:{key}"))


def compile_scenario(command, spec, *, read, committed, work=None):
    """POST commands and GET checks using authenticated API transports.

    Tests may service their disposable worker synchronously with ``work``; DEV
    uses its existing worker. ``committed`` reads a successful immutable command
    receipt by key, or returns None when absent. Neither path bypasses policy review receipts.
    """
    records, ids, workspace = {}, {}, None

    def resolve(value):
        if isinstance(value, str) and value.startswith('$'):
            parts = value[1:].split('.')
            record = records[parts[0]]
            return record[parts[1]] if len(parts) == 2 else record['id']
        if isinstance(value, list):
            return [resolve(item) for item in value]
        if isinstance(value, dict):
            return {key: resolve(item) for key, item in value.items()}
        return value

    for row in spec['commands']:
        payload, target, revision = resolve(row.get('payload', {})), resolve(row.get('target')), resolve(row.get('revision'))
        if row['kind'] == 'policy.activate':
            # A committed activation already owns its exact checked context set.
            # Replay that immutable result before today's effect list can grow.
            previous = committed(row['role'], command_key(spec, row['key']))
            if previous is not None:
                payload = {**payload, 'check_job_ids': [evidence['check_job_id'] for evidence in
                    sorted(previous['context_check_evidence'], key=lambda evidence: evidence['workspace_id'])]}
        if row['kind'] == 'policy.activate' and previous is None:
            deadline = time.monotonic() + 120
            failure = 'Golden policy check failed or timed out; inspect its job before resuming'
            failed, offset = set(), 0
            while True:
                page = read('approver', f"/api/jobs?limit=100&offset={offset}")
                failed.update(job['id'] for job in page['jobs'] if job['status'] in {'failed', 'cancelled'})
                if time.monotonic() >= deadline:
                    raise ValueError(failure)
                if not page['has_more']:
                    break
                offset += page['limit']
            # A department-wide rule requires a receipt for every affected rota.
            # Keep ready contexts too: partial resume must submit the exact union.
            review = read('approver', f'/api/policies/{target}/review')
            contexts = sorted((effect['workspace_id'], effect.get('revision'))
                              for effect in review.get('effect', []))
            if not contexts and workspace is not None:
                contexts = [(workspace['id'], workspace.get('revision'))]
            if not contexts:
                raise ValueError(failure)
            check_ids = []
            for workspace_id, workspace_revision in contexts:
                check_key = row['key'] + ':check' + (f':{workspace_id}' if len(contexts) > 1 else '')
                while True:
                    if time.monotonic() >= deadline:
                        raise ValueError(failure)
                    check = command('approver', 'job.create', {
                        'kind': 'feasibility', 'workspace_id': workspace_id, 'check_seconds': 10,
                        'rule_changes': [{'policy_id': target, 'expected_revision': revision}]},
                        revision=workspace_revision or workspace['revision'], key=command_key(spec, check_key))['job']
                    check = read('approver', '/api/jobs/' + check['id'])['job']
                    if time.monotonic() >= deadline:
                        raise ValueError(failure)
                    if check['status'] not in {'failed', 'cancelled'} or check['id'] not in failed:
                        break
                    check_key = row['key'] + ':check:after:' + check['id']
                if work and check['status'] in {'queued', 'running'}:
                    work()
                    check = read('approver', '/api/jobs/' + check['id'])['job']
                while True:
                    if time.monotonic() >= deadline:
                        raise ValueError(failure)
                    if check['status'] == 'succeeded':
                        break
                    if check['status'] not in {'queued', 'running'}:
                        raise ValueError(failure)
                    time.sleep(0.25)
                    check = read('approver', '/api/jobs/' + check['id'])['job']
                check_ids.append(check['id'])
            if time.monotonic() >= deadline:
                raise ValueError(failure)
            payload = {**payload, 'check_job_ids': check_ids}
        reply = command(row['role'], row['kind'], payload,
                        target=target, revision=revision,
                        key=command_key(spec, row['key']))
        if 'workspace' in reply:
            workspace = reply['workspace']
            records['workspace'] = workspace
        record = (reply.get('record') or
                  ({'id': reply['shift_id']} if 'shift_id' in reply else None) or
                  reply.get('workspace'))
        if record:
            records[row['key']] = record
            ids[row['key']] = record['id']
        if names := row.get('bulk_records'):
            items = reply['items']
            if len(items) != len(names):
                raise ValueError('Incomplete golden bulk result')
            for index, (name, item) in enumerate(zip(names, items)):
                if item['index'] != index or item['result'] != 'changed':
                    raise ValueError('Golden bulk result did not create every record')
                saved = item['results'][0]['record']
                records[name] = saved
                ids[name] = saved['id']
        if row.get('target') and 'record' in reply:
            records[row['target'][1:]] = reply['record']
    digest = hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'workspace': workspace, 'ids': ids, 'records': records, 'input_sha256': digest,
            'scenario_id': spec['id'], 'revision': spec['revision'],
            'generation_options': GenerationOptions.model_validate(
                spec['inputs']['generation_options']).model_dump(mode='json')}
