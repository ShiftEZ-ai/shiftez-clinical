"""Streaming responses own and close their iterators, including interrupted downloads."""
from collections.abc import AsyncIterable
from threading import Lock

from anyio import CancelScope
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse


class _OwnedIterator:
    def __init__(self, content):
        self.iterator, self.lock = iter(content), Lock()

    def __iter__(self):
        return self

    def __next__(self):
        with self.lock:
            return next(self.iterator)

    def close(self):
        # A cancelled response may still have a worker finishing next().
        with self.lock:
            if close := getattr(self.iterator, "close", None):
                close()


class ClosingStreamingResponse(StreamingResponse):
    def __init__(self, content, *args, **kwargs):
        self._iterator = aiter(content) if isinstance(content, AsyncIterable) else _OwnedIterator(content)
        super().__init__(self._iterator, *args, **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with CancelScope(shield=True):
                try:
                    if close := getattr(self._iterator, "aclose", None):
                        await close()
                    elif close := getattr(self._iterator, "close", None):
                        await run_in_threadpool(close)
                finally:
                    if self.body_iterator is not self._iterator:
                        await self.body_iterator.aclose()
