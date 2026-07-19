from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_ambient_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's real .env and OS keychain out of unit tests.

    AllocatorConfig reads a .env and consults a secret backend, so without this
    a unit test would resolve against whatever the machine happens to have.
    Tests that want either must point at them explicitly.
    """
    monkeypatch.setenv("OPEN_ALLOCATOR_SECRET_BACKEND", "none")
    monkeypatch.setenv("OPEN_ALLOCATOR_ENV_FILE", "/nonexistent/open-allocator/.env")
