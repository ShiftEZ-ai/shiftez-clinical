"""Browser setup skips redundant package updates without bypassing failed prerequisites."""
import subprocess
import sys
from unittest.mock import Mock

import pytest

from scripts import install_browser


@pytest.mark.parametrize("probe_status", [0, 1, 2])
def test_hosted_runner_checks_packages_and_always_installs_pinned_browser(monkeypatch, probe_status):
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    run = Mock(return_value=subprocess.CompletedProcess([], probe_status))
    monkeypatch.setattr(install_browser.subprocess, "run", run)
    configure = Mock()
    monkeypatch.setattr(install_browser, "configure_hosted_apt", configure)
    install_browser.install()
    calls = run.call_args_list
    assert calls[0].args[0] == [sys.executable, "-m", "playwright", "install-deps", "--dry-run", "chromium"]
    assert calls[0].kwargs == {"check": False}
    assert [call.args[0][3:] for call in calls[1:]] == (
        [["install-deps", "chromium"], ["install", "chromium"]] if probe_status else [["install", "chromium"]]
    )
    assert all(call.kwargs == {"check": True} for call in calls[1:])
    assert configure.call_count == (1 if probe_status else 0)


def test_failed_system_install_blocks_browser_setup(monkeypatch):
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    failure = subprocess.CalledProcessError(100, ["playwright", "install-deps", "chromium"])
    run = Mock(side_effect=[subprocess.CompletedProcess([], 1), failure])
    monkeypatch.setattr(install_browser.subprocess, "run", run)
    monkeypatch.setattr(install_browser, "configure_hosted_apt", Mock())
    with pytest.raises(subprocess.CalledProcessError):
        install_browser.install()
    assert run.call_count == 2


@pytest.mark.parametrize("environment", ["self-hosted", ""])
def test_non_hosted_runner_still_checks_pinned_browser_install_without_apt_changes(monkeypatch, environment):
    monkeypatch.setenv("RUNNER_ENVIRONMENT", environment)
    failure = subprocess.CalledProcessError(1, ["playwright", "install", "chromium"])
    run = Mock(side_effect=failure)
    monkeypatch.setattr(install_browser.subprocess, "run", run)
    configure = Mock()
    monkeypatch.setattr(install_browser, "configure_hosted_apt", configure)
    with pytest.raises(subprocess.CalledProcessError):
        install_browser.install()
    run.assert_called_once_with([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    configure.assert_not_called()


@pytest.mark.parametrize("mirrors", ["known", "missing", "unexpected"])
def test_hosted_apt_prefers_only_existing_known_fallback_and_bounds_downloads(monkeypatch, tmp_path, mirrors):
    path = tmp_path / "apt-mirrors.txt"
    original = ("# Keep runner-managed sources\n"
                "http://azure.archive.ubuntu.com/ubuntu/\tpriority:1\n"
                "https://archive.ubuntu.com/ubuntu/\tpriority:2\n"
                "https://security.ubuntu.com/ubuntu/\tpriority:3\n"
                "https://example.invalid/other/\tpriority:4\n")
    if mirrors != "missing":
        path.write_text(original if mirrors == "known" else original.replace("priority:2", "priority:8"), encoding="utf-8")
    monkeypatch.setattr(install_browser, "MIRRORS", path)
    run = Mock()
    monkeypatch.setattr(install_browser.subprocess, "run", run)
    install_browser.configure_hosted_apt()
    calls = run.call_args_list
    assert len(calls) == (2 if mirrors == "known" else 1)
    if mirrors == "known":
        assert calls[0].args[0] == ["sudo", "tee", str(path)]
        assert calls[0].kwargs == {"input": original.replace("priority:1", "priority:2", 1).replace(
            "https://archive.ubuntu.com/ubuntu/\tpriority:2", "https://archive.ubuntu.com/ubuntu/\tpriority:1"),
            "text": True, "stdout": subprocess.DEVNULL, "check": True}
    assert calls[-1].args[0] == ["sudo", "tee", install_browser.APT_SETTINGS]
    assert calls[-1].kwargs == {"input": 'Acquire::http::Timeout "30";\nAcquire::https::Timeout "30";\nAcquire::Retries "1";\n',
                               "text": True, "stdout": subprocess.DEVNULL, "check": True}


@pytest.mark.parametrize("failure_at", ["mirror", "settings"])
def test_failed_hosted_apt_setup_blocks_dependency_and_browser_install(monkeypatch, tmp_path, failure_at):
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    path = tmp_path / "apt-mirrors.txt"
    path.write_text("http://azure.archive.ubuntu.com/ubuntu/\tpriority:1\n"
                    "https://archive.ubuntu.com/ubuntu/\tpriority:2\n", encoding="utf-8")
    monkeypatch.setattr(install_browser, "MIRRORS", path)
    failure = subprocess.CalledProcessError(1, ["sudo", "tee"])
    outcomes = [subprocess.CompletedProcess([], 1)]
    if failure_at == "settings":
        outcomes.append(subprocess.CompletedProcess([], 0))
    run = Mock(side_effect=[*outcomes, failure])
    monkeypatch.setattr(install_browser.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        install_browser.install()
    assert run.call_count == (2 if failure_at == "mirror" else 3)
    assert all("install" not in call.args[0] for call in run.call_args_list)
