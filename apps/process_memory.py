"""Release completed request/job graphs and native solver arenas, with opt-in RSS evidence."""
import gc
import json
import logging
import os
from pathlib import Path
import sys
from threading import Lock

log = logging.getLogger('rota.memory')
_reclaiming = Lock()


def resident_bytes():
    if not sys.platform.startswith('linux'):
        return 0
    try:
        return next(int(line.split()[1]) * 1024 for line in Path('/proc/self/status').read_text().splitlines()
                    if line.startswith('VmRSS:'))
    except (OSError, StopIteration, ValueError):
        return 0


def memory_probe(stage, claim=None):
    path = os.getenv('ROTA_MEMORY_PROBE_FILE')
    if not path:
        return
    try:
        with Path(path).open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'stage': stage, 'pid': os.getpid(), 'rss_bytes': resident_bytes(),
                                     'job_id': claim.get('id') if claim else None}) + '\n')
    except OSError:
        log.debug('memory_probe_unavailable', exc_info=True)


def release_memory():
    # Request-local closures and CP-SAT callbacks form cycles. On glibc, collection
    # frees their objects but malloc_trim is also needed to return native arenas.
    if not _reclaiming.acquire(blocking=False):
        return
    try:
        gc.collect()
        if sys.platform.startswith('linux'):
            try:
                import ctypes
                ctypes.CDLL(None).malloc_trim(0)
            except (AttributeError, OSError):
                pass
    finally:
        _reclaiming.release()
