"""The persisted state of one bridged deposit leg.

A leg whose deposit chain is not where its USDC is burns on the source chain,
waits for Circle to attest the burn, and then redeems and deposits in one
destination operation. That spans several invocations of ``execute --confirm``,
so the leg's progress is a record kept in the run's idempotency scope, under
``bridge:<leg key>``. Checkpoints carry copies of it as audit snapshots; the
record in the store is the one a rerun resumes from.

States advance in order and are never re-entered backwards except where a
destination operation reverted, which leaves the CCTP nonce unused:

    bridge_planned -> source_submitted -> awaiting_attestation
        -> destination_ready -> destination_submitted -> completed

``failed`` is terminal: a burn that cannot be identified or whose attestation
does not match it is never redeemed automatically.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field

from open_allocator.core.types import FrozenModel

BridgeStatus = Literal[
    "bridge_planned",
    "source_submitted",
    "awaiting_attestation",
    "destination_ready",
    "destination_submitted",
    "completed",
    "failed",
]

_RAW = r"^\d+$"


class BridgeState(FrozenModel):
    version: Literal[1] = 1
    # The allocation leg this bridge funds, as ``leg:<index>:<instrument>``.
    leg_id: str = Field(min_length=1)
    leg_index: int = Field(ge=0)
    instrument_id: str = Field(min_length=1)
    state: BridgeStatus
    # The Safe: burner, mint recipient, destination caller, and depositor.
    account: str
    source_chain_id: int = Field(ge=1)
    destination_chain_id: int = Field(ge=1)
    source_domain: int = Field(ge=0)
    destination_domain: int = Field(ge=0)
    source_token: str
    source_token_messenger: str
    burn_amount_raw: str = Field(pattern=r"^[1-9]\d*$")
    max_fee_raw: str = Field(pattern=_RAW)
    min_finality_threshold: Literal[1000, 2000]
    fast: bool
    # The leg's full size in destination USDC; the deposit never exceeds it.
    wanted_deposit_raw: str = Field(pattern=_RAW)
    source_bundle_id: str
    source_bundle_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Which of the Safe's burns in its source operation this is, in call order.
    burn_index: int = Field(default=0, ge=0)
    burns_in_operation: int = Field(default=1, ge=1)
    # The user operation (or transaction) hash the source burn went out under.
    source_operation_hash: str | None = None
    source_transaction_hash: str | None = None
    source_message_transmitter: str | None = None
    source_log_index: int | None = Field(default=None, ge=0)
    # The burn's message as emitted, before Circle fills nonce and fee.
    source_message: str | None = None
    # Among the Safe's identical burns in the operation, which one this is.
    message_ordinal: int = Field(default=0, ge=0)
    attestation_status: str | None = None
    delay_reason: str | None = None
    message_hash: str | None = None
    nonce: str | None = None
    message: str | None = None
    attestation: str | None = None
    fee_executed_raw: str | None = Field(default=None, pattern=_RAW)
    net_mint_raw: str | None = Field(default=None, pattern=_RAW)
    expiration_block: int | None = Field(default=None, ge=0)
    destination_bundle_digest: str | None = None
    deposit_amount_raw: str | None = Field(default=None, pattern=_RAW)
    destination_operation_hash: str | None = None
    destination_transaction_hash: str | None = None
    last_error: str | None = None
    created_at: str
    updated_at: str

    @property
    def active(self) -> bool:
        """Whether a burn may have happened, so the leg must not be replanned.

        A planned record never reached the chain, and a failed one without a
        source transaction reverted there; either leaves the leg free to plan
        afresh. Everything else resumes.
        """
        if self.state == "bridge_planned":
            return False
        return not (self.state == "failed" and self.source_transaction_hash is None)

    def advanced(self, state: BridgeStatus, **changes: object) -> BridgeState:
        return self.model_copy(
            update={"state": state, "updated_at": now_text(), **changes}
        )

    def noted(self, **changes: object) -> BridgeState:
        return self.model_copy(update={"updated_at": now_text(), **changes})


def state_key(leg_id: str) -> str:
    return f"bridge:{leg_id}"


def load(store: object | None, leg_id: str) -> BridgeState | None:
    value = _stored_value(store, state_key(leg_id))
    if value is None or value is True:
        return None
    if not isinstance(value, Mapping):
        raise TypeError(f"bridge state for {leg_id} is not an object")
    return BridgeState.model_validate(value)


def save(store: object | None, state: BridgeState) -> None:
    """Persist the record; required before anything that depends on it is sent."""
    if store is None:
        return
    payload = state.model_dump(mode="json")
    mark = getattr(store, "mark_completed", None)
    if callable(mark):
        mark(state_key(state.leg_id), payload)
        return
    if isinstance(store, dict):
        store[state_key(state.leg_id)] = payload
        return
    raise TypeError("the idempotency store cannot hold bridge state")


def now_text() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _stored_value(store: object | None, key: str) -> object | None:
    if store is None:
        return None
    completed_value = getattr(store, "completed_value", None)
    if callable(completed_value):
        return completed_value(key)
    if isinstance(store, Mapping):
        return store.get(key)
    return None


__all__ = [
    "BridgeState",
    "BridgeStatus",
    "load",
    "now_text",
    "save",
    "state_key",
]
