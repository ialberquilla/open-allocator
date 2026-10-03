from __future__ import annotations

from collections.abc import Mapping
from typing import Any

JsonObject = dict[str, Any]


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
