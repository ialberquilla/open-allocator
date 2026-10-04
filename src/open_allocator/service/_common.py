from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from open_allocator.core.state import StateBackend, with_state_backend
from open_allocator.exec.config import AllocatorConfig

JsonObject = dict[str, Any]

# Where this process keeps execution state, when not the `.open_allocator/` files.
_state_backend: StateBackend | None = None


def use_state_backend(backend: StateBackend | None) -> None:
    """Keep every later service call's execution state in `backend`.

    The server sets its database here once, at start; the CLI never does, so it
    keeps the files. `None` goes back to the files.
    """
    global _state_backend
    _state_backend = backend


def allocator_config() -> object:
    """The configuration a service call runs with, carrying this process's state
    backend when one is set."""
    config = AllocatorConfig()
    if _state_backend is None:
        return config
    return with_state_backend(config, _state_backend)


def signer_from_config(config: object) -> object:
    from open_allocator.exec.signer import signer_from_config as factory

    return factory(config)


def signer_address(config: object) -> str:
    signer = signer_from_config(config)
    address_method = getattr(signer, "address", None)
    if not callable(address_method):
        raise TypeError("signer does not implement address()")
    return str(address_method())


def model_payload(value: object) -> JsonObject:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        payload = model_dump(mode="json")
    elif isinstance(value, Mapping):
        payload = dict(value)
    elif hasattr(value, "__dict__"):
        payload = vars(value)
    else:
        payload = value
    if not isinstance(payload, dict):
        raise TypeError("expected JSON object payload")
    return payload
