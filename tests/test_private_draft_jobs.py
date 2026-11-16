"""Jobs of a coordinator's private draft stay invisible to every other reader (list, detail, Home)."""
import pytest
from .governance_support import identity


def _private_with_job(h):
    shared = h.shift(h.workspace('Shared draft'))['workspace']
    private = h.command('coordinator', 'workspace.create', {'name': 'SECRET private draft',
        'starts_on': shared['starts_on'], 'ends_on': shared['ends_on'],
        'visibility': 'private', 'purpose': 'sandbox'})['workspace']
    private = h.shift(private)['workspace']
    made = h.command('coordinator', 'job.create', {'workspace_id': private['id'], 'kind': 'validation'},
                     revision=h.detail(private)['workspace']['revision'])
    return private, made['job']


@pytest.mark.parametrize('reader', ['other_coordinator', 'approver', 'auditor', 'admin', 'staff'])
def test_private_draft_jobs_are_invisible_to_other_readers(h, reader):
    private, job = _private_with_job(h)
    if reader == 'other_coordinator':
        identity(h, reader, ['coordinator'])
    client = h.client(reader)
    seen = {}
    for name, url in {
        'list': '/api/jobs',
        'list_ws': '/api/jobs?workspace_id=' + private['id'],
        'detail': '/api/jobs/' + job['id'],
        'detail_full': '/api/jobs/' + job['id'] + '?full=true',
        'status': '/api/jobs/' + job['id'] + '/status',
        'evidence': '/api/jobs/' + job['id'] + '/evidence',
        'input_changes': '/api/jobs/' + job['id'] + '/input-changes',
        'home': '/api/home',
        'workspace': '/api/workspaces/' + private['id'],
    }.items():
        r = client.get(url)
        seen[name] = (r.status_code, job['id'] in r.text, private['id'] in r.text, 'SECRET private draft' in r.text)
    leaks = {k: v for k, v in seen.items() if v[1] or v[2] or v[3]}
    assert not leaks, leaks


def test_owner_still_sees_the_job(h):
    private, job = _private_with_job(h)
    r = h.client('coordinator').get('/api/jobs?workspace_id=' + private['id'])
    assert r.status_code == 200 and job['id'] in r.text
