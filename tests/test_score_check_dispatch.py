"""Multipart uploads leave synchronous database and import work off the ASGI loop."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import uuid

import httpx
import pytest

pytestmark = pytest.mark.usefixtures('score_check_enabled')

from apps.api import main
from .test_score_check_frontend_api import upload


@pytest.mark.parametrize('adding', [False, True], ids=['upload', 'add-files'])
@pytest.mark.parametrize('boundary', ['auth', 'rate_limit', 'encode', 'execute'])
def test_multipart_sync_boundaries_allow_other_requests_while_blocked(h, monkeypatch, adding, boundary):
    value = upload(h) if adding else None
    signed_in = h.client('coordinator')
    headers = {'origin': h.settings.public_origin, 'x-csrf-token': signed_in.headers['x-csrf-token']}
    cookies = dict(signed_in.cookies)
    owner, name = (main.base64, 'b64encode') if boundary == 'encode' else (
        main, {'auth': 'authenticate', 'rate_limit': 'enforce', 'execute': 'execute'}[boundary])
    original = getattr(owner, name)

    async def scenario():
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        entered, release, seen = asyncio.Event(), threading.Event(), []

        def blocked(*args, **kwargs):
            seen.append(threading.get_ident())
            loop.call_soon_threadsafe(entered.set)
            assert seen[-1] != loop_thread, 'Synchronous upload work ran on the ASGI loop'
            assert release.wait(5), 'The test did not release the synchronous boundary'
            return original(*args, **kwargs)

        monkeypatch.setattr(owner, name, blocked)
        path = '/api/score-imports/' + value['id'] + '/sources' if adding else '/api/score-imports'
        pending = None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url=h.settings.public_origin,
                                     headers=headers, cookies=cookies) as client:
            try:
                pending = asyncio.create_task(client.post(path, data={'revision': str(value['revision'])} if adding else {},
                    files={'files': ('thread-check.csv', b'Person,Date,Code\nStaff 01(test),2026-10-04,AM\n', 'text/csv')},
                    headers={'idempotency-key': str(uuid.uuid4())}))
                await asyncio.wait_for(entered.wait(), 5)
                assert seen and all(thread != loop_thread for thread in seen)
                assert not pending.done()
                response = await asyncio.wait_for(client.get('/health/live'), 5)
                assert response.status_code == 200
                assert not pending.done()
                release.set()
                # Completion includes real PostgreSQL import work; responsiveness was checked before release.
                response = await asyncio.wait_for(pending, 60)
                assert response.status_code == 200, response.text
            finally:
                release.set()
                if pending is not None:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(asyncio.run, scenario()).result()
