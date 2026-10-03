"""Shared authorization gate for every test beneath ``tests/live``."""

from __future__ import annotations

import pytest

from tests.live.qualification import conftest as qualification_gate


def pytest_addoption(parser: pytest.Parser) -> None:
    qualification_gate.add_live_authorization_options(parser)


@pytest.fixture(scope="session")
def live_authorization(pytestconfig: pytest.Config):
    """Consume one receipt-bound authorization for the whole live category."""

    yield from qualification_gate.live_authorization_session(pytestconfig)
