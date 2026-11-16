"""Retry only unavailable PyPI audits; a complete clean audit is required to pass."""
import json
import os
import re
import subprocess
import sys
import time


TOTAL_TIMEOUT = 180
RETRY_DELAY = 2


def partial_output(value, stream):
    if not value:
        return
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(key):
            text = text.replace(os.environ[key], ('[red' + 'acte' + 'd]'))
    text = re.sub(('http' + 's?:/' + '/\\S+' + '|gh[' + 'pous' + 'r]_[' + 'A-Za' + '-z0-' + '9_]+' + '|git' + 'hub_' + 'pat_' + '[A-Z' + 'a-z0' + '-9_]' + '+'), ('[red' + 'acte' + 'd]'), text)
    # Quote control characters and workflow commands while retaining package context.
    print("pip-audit interrupted: " + json.dumps(text), file=stream, flush=True)


def audit(command):
    deadline = time.monotonic() + TOTAL_TIMEOUT
    for attempt in range(2):
        remaining = deadline - time.monotonic()
        # Reserve a second attempt and its backoff; 30s is the CLI's socket timeout.
        limit = (remaining - RETRY_DELAY) / 2 if attempt == 0 else remaining
        if limit <= 0:
            break
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=limit)
        except subprocess.TimeoutExpired as error:
            partial_output(error.stdout, sys.stdout)
            partial_output(error.stderr, sys.stderr)
            timed_out = True
        else:
            sys.stdout.write(result.stdout)
            sys.stderr.write(result.stderr)
            if time.monotonic() >= deadline:
                break
            if result.returncode == 0:
                return 0
            timed_out = bool(re.search(
                r"(?m)^requests\.exceptions\.(?:ReadTimeout|ConnectTimeout|Timeout):", result.stderr))
            if not timed_out:
                return result.returncode
        if attempt == 0 and timed_out and time.monotonic() + RETRY_DELAY < deadline:
            print("::warning::PyPI audit timeout (30s socket limit, shared 180s deadline); retrying once",
                  file=sys.stderr, flush=True)
            time.sleep(RETRY_DELAY)
        else:
            break
    print("::error::PyPI audit timeout: 30s socket limit, one retry, 180s total cap; audit unavailable",
          file=sys.stderr, flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(audit(sys.argv[1:]))
