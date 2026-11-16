"""Layout guard for the other roles (tests/test_browser_overflow.py has the checks and the planners)."""
import pytest

from .test_browser import browser, live_server  # noqa: F401
from .test_browser_overflow import sweep
from .test_home import big_department

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('role', ['credentialer', 'programme', 'auditor', 'staff', 'operator'])
def test_no_page_overflows_overlaps_or_clips(browser, live_server, h, role):
    sweep(browser, live_server, h, role)


def test_department_scale(browser, live_server, h):
    """160 people, a year of publications (~540 sessions a month), requests and audit history, plus the long names."""
    big_department(h)
    sweep(browser, live_server, h, 'coordinator')
