"""Install pinned Chromium; avoid refreshing package mirrors when its OS packages are present."""
import os
from pathlib import Path
import subprocess
import sys


MIRRORS = Path("/etc/apt/apt-mirrors.txt")
APT_SETTINGS = "/etc/apt/apt.conf.d/99-fmuc-browser"


def configure_hosted_apt():
    # The disposable Ubuntu runner already supplies these signed archive mirrors.
    # Prefer its official fallback while Azure's mirror stalls; never replace unknown sources.
    # https://github.com/actions/runner-images/blob/ubuntu24/20261004.327/images/ubuntu/scripts/build/configure-apt-sources.sh
    azure = "http://azure.archive.ubuntu.com/ubuntu/\tpriority:1"
    ubuntu = "https://archive.ubuntu.com/ubuntu/\tpriority:2"
    if MIRRORS.exists():
        lines = MIRRORS.read_text(encoding="utf-8").splitlines(keepends=True)
        entries = [line.rstrip("\n") for line in lines]
        if entries.count(azure) == entries.count(ubuntu) == 1:
            preferred = "".join(line.replace("priority:1", "priority:2") if entry == azure else
                                line.replace("priority:2", "priority:1") if entry == ubuntu else line
                                for line, entry in zip(lines, entries))
            subprocess.run(["sudo", "tee", str(MIRRORS)], input=preferred, text=True,
                           stdout=subprocess.DEVNULL, check=True)
        else:
            print("Keeping unrecognized runner mirror preferences", flush=True)
    # APT reads this after sudo, unlike an unpreserved parent APT_CONFIG environment variable.
    # Limit both connection and stalled-data waits without changing package validation.
    settings = 'Acquire::http::Timeout "30";\nAcquire::https::Timeout "30";\nAcquire::Retries "1";\n'
    subprocess.run(["sudo", "tee", APT_SETTINGS], input=settings, text=True,
                   stdout=subprocess.DEVNULL, check=True)


def install():
    command = [sys.executable, "-m", "playwright"]
    if os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted":
        # Playwright's dry run simulates apt locally and returns nonzero for missing packages.
        # Browser-cache markers do not establish that a fresh runner has its OS dependencies.
        print("Checking Chromium system packages", flush=True)
        probe = subprocess.run([*command, "install-deps", "--dry-run", "chromium"], check=False)
        if probe.returncode:
            configure_hosted_apt()
            print("Installing Chromium system packages", flush=True)
            subprocess.run([*command, "install-deps", "chromium"], check=True)
    print("Installing pinned Chromium (reusing its download cache when present)", flush=True)
    subprocess.run([*command, "install", "chromium"], check=True)


if __name__ == "__main__":
    install()
