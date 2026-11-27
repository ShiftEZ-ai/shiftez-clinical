"""Exercise the shared browser diagnostics without launching Chromium."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from .test_browser import Traced
from .test_browser_pending_requests import Context, Node


@pytest.mark.parametrize(("mode", "failed", "trace_saved"), [
    ("off", False, False),
    ("off", True, False),
    ("on", False, True),
    ("on", True, True),
    (None, False, False),
    (None, True, True),
])
def test_browser_diagnostic_artifacts(monkeypatch, tmp_path, mode, failed, trace_saved):
    traces = tmp_path / "traces"
    if mode is None:
        monkeypatch.delenv("PW_TRACE_MODE", raising=False)
        monkeypatch.delenv("PW_SCREENSHOT_DIR", raising=False)
        screenshots = tmp_path / "traces-screenshots"
    else:
        monkeypatch.setenv("PW_TRACE_MODE", mode)
        screenshots = tmp_path / "failure-screenshots"
        monkeypatch.setenv("PW_SCREENSHOT_DIR", str(screenshots))
    monkeypatch.delenv("PW_API_LOG_DIR", raising=False)
    context = Context()
    context.tracing = Mock()
    page = Mock()
    context.pages.append(page)
    node = Node()
    node.rep_call = SimpleNamespace(failed=failed)
    browser = Traced(SimpleNamespace(new_context=lambda **kwargs: context), traces, "test", node)

    browser.new_context().close()

    assert context.tracing.start.call_count == (mode != "off")
    assert context.tracing.stop.call_count == (mode != "off")
    if trace_saved:
        trace = Path(context.tracing.stop.call_args.kwargs["path"])
        assert trace.parent == traces and trace.suffix == ".zip"
    elif mode != "off":
        context.tracing.stop.assert_called_once_with()
    assert page.screenshot.call_count == failed
    if failed:
        screenshot = Path(page.screenshot.call_args.kwargs["path"])
        assert screenshot.parent == screenshots and screenshot.suffix == ".png"
    assert traces.is_dir() == trace_saved
    assert screenshots.is_dir() == failed
