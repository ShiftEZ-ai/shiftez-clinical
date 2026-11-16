"""One bounded child per media preparation or real HTTP attempt; private IPC only."""
import base64
from contextlib import suppress
import json
import logging
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time


MEMORY_BYTES = 512 * 1024 * 1024
MEDIA_SECONDS = 30
INPUT_BYTES = 48 * 1024 * 1024
OUTPUT_BYTES = 40 * 1024 * 1024
ROOT = Path(__file__).resolve().parents[3]
log = logging.getLogger(__name__)
_warned_same_uid = False


class IsolationError(ValueError):
    """Only a fixed category crosses the subprocess boundary."""


def protect_parent():
    """Deny same-user child access to Linux worker memory and /proc secrets."""
    if sys.platform == 'linux':
        import ctypes
        prctl = ctypes.CDLL(None, use_errno=True).prctl
        prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        prctl.restype = ctypes.c_int
        if prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
            raise OSError('Cannot protect score-check parent')


def _privilege_options():
    global _warned_same_uid
    from apps.api.settings import environment_from_env
    production = environment_from_env() == 'production'
    protect_parent()
    if sys.platform != 'linux':
        if production:
            raise OSError('Score-check isolation unavailable: production requires Linux child credentials')
        return {}
    uid, gid = os.getenv('ROTA_SCORE_CHECK_UID'), os.getenv('ROTA_SCORE_CHECK_GID')
    if uid is None and gid is None:
        if production:
            raise OSError('Score-check isolation unavailable: configure separate ROTA_SCORE_CHECK_UID/GID')
        if os.geteuid() == 0:
            raise OSError('Root score-check workers require an unprivileged child UID/GID')
        if not _warned_same_uid:
            log.warning('score_check_child_same_uid: configure ROTA_SCORE_CHECK_UID/GID for separate credentials; '
                        'parent /proc access is denied by PR_SET_DUMPABLE=0')
            _warned_same_uid = True
        return {}
    try:
        uid, gid = int(uid), int(gid)
    except (TypeError, ValueError):
        raise OSError('Score-check child UID/GID must both be positive integers') from None
    if uid <= 0 or gid <= 0 or uid == os.geteuid() or gid == os.getegid():
        raise OSError('Score-check child requires separate unprivileged credentials')
    return {'user': uid, 'group': gid, 'extra_groups': []}


def _check_parent():
    if sys.platform == 'linux':
        try:
            with open(f'/proc/{os.getppid()}/environ', 'rb'):
                pass
        except PermissionError:
            return
        raise OSError('Score-check child can access parent secrets')


def _memory_limit(memory_bytes):
    if os.name == "posix":
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        return None
    if os.name != "nt":
        raise OSError("Resource isolation is unavailable")
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
            ("flags", wintypes.DWORD), ("minimum_working_set", ctypes.c_size_t),
            ("maximum_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
            ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]

    class Counters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

    class Limits(ctypes.Structure):
        _fields_ = [("basic", Basic), ("io", Counters), ("process_memory", ctypes.c_size_t),
                   ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.SetErrorMode.argtypes = [wintypes.UINT]
    kernel.SetErrorMode.restype = wintypes.UINT
    kernel.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise OSError("Resource isolation is unavailable")
    limits = Limits()
    # Process/job commit limits, one process, and terminate it if the job handle closes.
    limits.basic.flags = 0x100 | 0x200 | 0x8 | 0x2000
    limits.basic.active_processes = 1
    limits.process_memory = limits.job_memory = memory_bytes
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not (
            kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess())):
        kernel.CloseHandle(job)
        raise OSError("Resource isolation is unavailable")
    return job  # Retain the handle until this short-lived child exits.


def _hidden_options():
    options = {}
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        options = {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startup}
    return options


def run(operation, payload, *, timeout, memory_bytes=MEMORY_BYTES):
    deadline = time.monotonic() + timeout
    if operation not in {"media", "request"}:
        raise IsolationError("unavailable")
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(encoded) > INPUT_BYTES:
        raise IsolationError("limit")
    environment = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP")
                   if key in os.environ}
    process = None
    exchange = None
    finished, result, failures = threading.Event(), [], []
    try:
        if time.monotonic() >= deadline:
            raise IsolationError("timeout")
        process = subprocess.Popen([sys.executable, "-I", "-X", "utf8", str(Path(__file__).resolve()),
            operation, str(memory_bytes), str(deadline)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=ROOT, env=environment, **_hidden_options(), **_privilege_options())

        def transfer():
            try:
                try:
                    process.stdin.write(encoded)
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
                prefix = process.stdout.read(4)
                if len(prefix) != 4:
                    raise IsolationError("resource")
                length = struct.unpack("!I", prefix)[0]
                if length > OUTPUT_BYTES:
                    raise IsolationError("limit")
                data = process.stdout.read(length)
                if len(data) != length or process.stdout.read(1):
                    raise IsolationError("resource")
                result.append(data)
            except Exception:
                failures.append(True)
            finally:
                finished.set()

        exchange = threading.Thread(target=transfer, name=f"score-check-io-{process.pid}", daemon=True)
        exchange.start()
        if not finished.wait(max(0, deadline - time.monotonic())):
            raise IsolationError("timeout")
        if failures:
            raise IsolationError("resource")
        process.wait(timeout=max(0, deadline - time.monotonic()))
        if process.returncode:
            raise IsolationError("resource")
        value = json.loads(result[0])
        if time.monotonic() >= deadline:
            raise IsolationError("timeout")
        if not isinstance(value, dict):
            raise IsolationError("resource")
        return value
    except IsolationError:
        raise
    except subprocess.TimeoutExpired:
        raise IsolationError("timeout") from None
    except Exception:
        raise IsolationError("unavailable") from None
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            if exchange is not None and exchange.ident is not None:
                exchange.join()
            with suppress(OSError):
                process.stdin.close()
            with suppress(OSError):
                process.stdout.close()


def _dispatch(operation, payload):
    sys.path.insert(0, str(ROOT))
    from apps.api.services import score_check_provider as provider
    if operation == "request":
        data = provider._post(**payload)
        return {"data": base64.b64encode(data).decode("ascii")}
    for name, value in payload["limits"].items():
        setattr(provider, name, value)
    source = provider._prepare_file(payload["file_ref"], payload["filename"], base64.b64decode(payload["content"], validate=True))
    return {"file_ref": source.file_ref, "sha256": source.sha256, "text": source.text,
        "cell_refs": sorted(source.cell_refs), "images": [{"ref": image.ref, "media_type": image.media_type,
            "data": base64.b64encode(image.data).decode("ascii")} for image in source.images]}


def _main():
    # Native/parser output cannot corrupt the bounded, length-prefixed private protocol.
    wire = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    with open(os.devnull, "wb") as sink:
        os.dup2(sink.fileno(), sys.stdout.fileno())
    try:
        job = _memory_limit(int(sys.argv[2]))
        _check_parent()
        # This same deadline remains enforced if the owning worker disappears.
        watchdog = threading.Timer(max(0, float(sys.argv[3]) - time.monotonic()), os._exit, args=(1,))
        watchdog.daemon = True
        watchdog.start()
        raw = sys.stdin.buffer.read(INPUT_BYTES + 1)
        if len(raw) > INPUT_BYTES:
            raise MemoryError
        payload = json.loads(raw)
        value = _dispatch(sys.argv[1], payload)
    except MemoryError:
        value = {"error": "SCORE_INPUT_LIMIT"}
    except BaseException as error:
        code = getattr(error, "code", "")
        value = {"error": code if code in {"SCORE_INPUT_INVALID", "SCORE_INPUT_LIMIT", "SCORE_INPUT_UNAVAILABLE",
            "SCORE_PROVIDER_ENDPOINT", "SCORE_PROVIDER_HTTP", "SCORE_PROVIDER_UNAVAILABLE"} else "SCORE_INPUT_UNAVAILABLE"}
    try:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        if len(data) > OUTPUT_BYTES:
            data = b'{"error":"SCORE_INPUT_LIMIT"}'
        wire.write(struct.pack("!I", len(data)) + data)
        wire.close()
    except BaseException:
        os._exit(1)


if __name__ == "__main__":
    _main()
