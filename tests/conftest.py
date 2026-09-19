"""Shared teardown.

The Playwright instance is process-wide (see `core.driver._playwright`), so it
is released once at the end of the session rather than per driver.
"""

import pytest

from core.driver import shutdown_playwright


@pytest.fixture(scope="session", autouse=True)
def _release_playwright():
    yield
    shutdown_playwright()
