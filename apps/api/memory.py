"""Reclaim after the complete ASGI response, including dependency/background cleanup."""
from starlette.concurrency import run_in_threadpool
from apps.process_memory import memory_probe, release_memory, resident_bytes


class MemoryReclamationMiddleware:
    def __init__(self, app, threshold_mb):
        self.app, self.threshold_bytes = app, threshold_mb * 1024 * 1024
        # Lowest RSS since the last reclaim. A process whose steady working set already exceeds the threshold
        # must not run a full collection (one uninterruptible GIL hold) after every response; uvicorn
        # kills a worker that cannot answer its health ping for 5 s.
        self.floor = 0

    async def __call__(self, scope, receive, send):
        try:
            await self.app(scope, receive, send)
        finally:
            if scope['type'] == 'http' and scope['path'].startswith('/api/'):
                memory_probe('after_response')
                rss = resident_bytes()
                self.floor = min(self.floor, rss)
                if self.threshold_bytes and rss >= self.floor + self.threshold_bytes:
                    await run_in_threadpool(release_memory)
                    self.floor = resident_bytes()
                    memory_probe('after_reclaim')
