"""
Pytest configuration for yandex-mail-mcp tests.

Test categories (markers):
- (unmarked) — pure unit tests and safe read-only integration tests.
  Always run. No writes, no sends.
- destructive — tests that create/modify/delete mailbox state (folders,
  flags, moves, trash). Only run with --run-destructive.
- send — tests that actually send emails via SMTP. Only run with
  --run-destructive. These send messages to the configured EMAIL
  address (self-addressed) with unique markers for cleanup.

Run commands:
    pytest                                # default: safe tests only
    pytest -m destructive --run-destructive   # destructive only
    pytest -m send --run-destructive          # send only
    pytest --run-destructive                  # everything
"""

import os
import uuid

import pytest
from dotenv import load_dotenv

# Load .env from the project directory so tests work regardless of cwd
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))


def pytest_addoption(parser):
    parser.addoption(
        "--run-destructive",
        action="store_true",
        default=False,
        help=(
            "Run destructive and send tests (create/modify/delete mailbox "
            "state, send real emails). Off by default for safety."
        ),
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "destructive: test modifies mailbox state (create/move/delete/flag)",
    )
    config.addinivalue_line(
        "markers",
        "send: test actually sends mail via SMTP",
    )


def pytest_collection_modifyitems(config, items):
    """Skip destructive+send tests unless --run-destructive was given."""
    if config.getoption("--run-destructive"):
        return
    skip_marker = pytest.mark.skip(
        reason="skipped by default; pass --run-destructive to run"
    )
    for item in items:
        if "destructive" in item.keywords or "send" in item.keywords:
            item.add_marker(skip_marker)


# ---- Session fixtures -------------------------------------------------------


@pytest.fixture(scope="session")
def yandex_email() -> str:
    """Yandex email address from .env."""
    email = os.getenv("YANDEX_EMAIL")
    if not email:
        pytest.skip("YANDEX_EMAIL not set in .env")
    assert email is not None  # for type checker
    return email


@pytest.fixture(scope="session")
def yandex_password() -> str:
    """Yandex app password from .env."""
    pwd = os.getenv("YANDEX_APP_PASSWORD")
    if not pwd:
        pytest.skip("YANDEX_APP_PASSWORD not set in .env")
    assert pwd is not None  # for type checker
    return pwd


@pytest.fixture(scope="session")
def run_id() -> str:
    """
    Short unique identifier for this test run.

    Used to tag test artifacts (folders, email subjects) so concurrent or
    repeated runs don't collide.
    """
    return uuid.uuid4().hex[:8]


@pytest.fixture
def test_folder_name(run_id) -> str:
    """Generate a unique test folder name for a single test."""
    return f"MCP-Test-{run_id}-{uuid.uuid4().hex[:6]}"


@pytest.fixture
def sandbox_folder(test_folder_name):
    """
    Create a dedicated test folder, yield its name, and delete it afterwards.

    Teardown runs even on test failure.
    """
    from yandex_mail_mcp import create_folder, delete_folder

    create_folder(test_folder_name)
    try:
        yield test_folder_name
    finally:
        try:
            delete_folder(test_folder_name)
        except Exception:
            # Best effort cleanup — don't mask the original test failure
            pass
