"""What a wallet holds: the position book and claimable rewards."""

from __future__ import annotations

import time
from collections.abc import Callable

from open_allocator.core.schema import validate
from open_allocator.exec.client import OneTxClient
from open_allocator.exec.config import AllocatorConfig, ReadOnlyOneTxConfig
from open_allocator.service._common import JsonObject, signer_address


def positions(
    address: str | None = None,
    *,
    on_warning: Callable[[str], None] | None = None,
) -> JsonObject:
    """The book for `address`, or for the configured signer when omitted.

    Levered positions that cannot be read are skipped, not fatal; each skip is
    reported through `on_warning` so the adapter decides where it goes.
    """
    if address is None:
        config = AllocatorConfig()
        address = signer_address(config)
    else:
        config = ReadOnlyOneTxConfig()

    from open_allocator.exec import loops as loops_exec

    with OneTxClient(config) as client:
        book, warnings = loops_exec.read_book(client, address, config)
    if on_warning is not None:
        for warning in warnings:
            on_warning(warning)
    return book.model_dump(mode="json")


def rewards(wallet: str, chain_id: int | None = None) -> JsonObject:
    with OneTxClient(ReadOnlyOneTxConfig()) as client:
        response = client.rewards(wallet, chain_id)

    payload: JsonObject = {
        "wallet": response.wallet,
        "rewards": [],
        "errors": list(response.errors),
        "expires_at": response.expires_at,
        "expired": response.expires_at <= int(time.time()),
    }
    rewards = payload["rewards"]
    assert isinstance(rewards, list)
    for reward in response.rewards:
        item = reward.model_dump(mode="json")
        item["claimable_amount_normalized"] = reward.claimable_amount_normalized
        item["pending_amount_normalized"] = reward.pending_amount_normalized
        rewards.append(item)

    validate(payload, "rewards")
    return payload
