"""Checks that a 1Tx deployment serves the calldata API contract.

For an account with no code and no ``executor``, a request must be answered,
parse strictly as a ``protocol_bundle``/``wallet_neutral_atomic`` simulation,
match the request, and outlive the minimum TTL. Each check is reported
separately.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Literal

from eth_utils import keccak, to_checksum_address
from pydantic import Field

from open_allocator.core.types import FrozenModel
from open_allocator.exec import calldata
from open_allocator.exec.client import (
    CalldataAction,
    InstrumentCalldataQuery,
    OneTxDecodeError,
    OneTxHTTPError,
)

# An address nobody holds a key for; its code is still read, not assumed.
PROBE_ACCOUNT = to_checksum_address(
    keccak(text="open-allocator:calldata-compatibility-probe")[12:]
)

# (chain id, account) -> the account's code; None when it cannot be read.
CodeReader = Callable[[int, str], bytes | None]

ProbeCheckName = Literal[
    "account_state",
    "request_without_executor",
    "execution_contract",
    "bound_to_request",
]


class ProbeCheck(FrozenModel):
    name: ProbeCheckName
    ok: bool
    detail: str


class CalldataProbe(FrozenModel):
    """One instrument's answer to the probe, check by check."""

    instrument_id: str
    chain_id: int = Field(ge=1)
    action: CalldataAction
    account: str
    amount: str
    expect_deployed: bool
    checks: tuple[ProbeCheck, ...]
    # 1Tx's wallet-neutral gas for the bare calls; never the wallet's gas.
    protocol_gas: str | None = None
    quote_block: int | None = None

    @property
    def ok(self) -> bool:
        return len(self.checks) == 4 and all(check.ok for check in self.checks)


def probe_instrument(
    client: object,
    *,
    instrument_id: str,
    chain_id: int,
    action: CalldataAction,
    amount: str,
    code_reader: CodeReader,
    account: str = PROBE_ACCOUNT,
    expect_deployed: bool = False,
    config: object | None = None,
) -> CalldataProbe:
    """Probe one instrument; stops at the first check that fails.

    Runs as a deployed-account probe too (``expect_deployed``), so both account
    states can be verified against the same instrument.
    """
    checks: list[ProbeCheck] = []

    def result(**extra: object) -> CalldataProbe:
        return CalldataProbe(
            instrument_id=instrument_id,
            chain_id=chain_id,
            action=action,
            account=account,
            amount=amount,
            expect_deployed=expect_deployed,
            checks=tuple(checks),
            **extra,  # type: ignore[arg-type]
        )

    checks.append(_account_state(code_reader, chain_id, account, expect_deployed))
    if not checks[-1].ok:
        return result()

    fetch = getattr(client, "instrument_calldata", None)
    if not callable(fetch):
        raise TypeError("client does not implement instrument_calldata")
    query = InstrumentCalldataQuery(
        action=action,
        account=account,
        amount=amount,
        slippage_bps=_slippage_bps(config),
    )
    # The query model forbids unknown fields, so executor cannot be sent.
    answered = ProbeCheck(
        name="request_without_executor",
        ok=True,
        detail="answered with query parameters "
        f"{sorted(query.model_dump(by_alias=True, exclude_none=True))}",
    )
    try:
        response = fetch(instrument_id, query)
    except OneTxHTTPError as error:
        checks.append(answered.model_copy(update={"ok": False, "detail": str(error)}))
        return result()
    except OneTxDecodeError as error:
        checks.append(answered)
        checks.append(
            ProbeCheck(name="execution_contract", ok=False, detail=str(error))
        )
        return result()
    checks.append(answered)
    checks.append(
        ProbeCheck(
            name="execution_contract",
            ok=True,
            detail=(
                "strict parse: no executor or unknown field; simulation "
                f"{response.simulation.scope}/{response.simulation.engine}"
            ),
        )
    )
    try:
        calldata.validate_instrument_calldata(
            response,
            instrument_id=instrument_id,
            account=account,
            action=action,
            chain_id=chain_id,
            amount=amount,
            min_ttl_seconds=calldata.min_ttl_seconds(config),
        )
    except calldata.CalldataValidationError as error:
        checks.append(ProbeCheck(name="bound_to_request", ok=False, detail=str(error)))
    else:
        checks.append(
            ProbeCheck(
                name="bound_to_request",
                ok=True,
                detail=f"{len(response.calls)} calls "
                f"({', '.join(call.type for call in response.calls)})",
            )
        )
    return result(
        protocol_gas=response.simulation.gas_used,
        quote_block=response.quote_block,
    )


def _slippage_bps(config: object | None) -> int | None:
    value = (
        config.get("slippage_bps")
        if isinstance(config, Mapping)
        else getattr(config, "slippage_bps", None)
    )
    return None if value is None else int(value)


def _account_state(
    code_reader: CodeReader,
    chain_id: int,
    account: str,
    expect_deployed: bool,
) -> ProbeCheck:
    expected = "deployed" if expect_deployed else "undeployed"
    try:
        code = code_reader(chain_id, account)
    except Exception as error:  # noqa: BLE001 - reported, never guessed
        # The type only: provider errors quote RPC URLs, which carry keys.
        code, reason = None, type(error).__name__
    else:
        reason = "no RPC"
    if code is None:
        return ProbeCheck(
            name="account_state",
            ok=False,
            detail=f"cannot confirm {account} is {expected} on chain {chain_id} "
            f"({reason})",
        )
    deployed = len(code) > 0
    return ProbeCheck(
        name="account_state",
        ok=deployed == expect_deployed,
        detail=f"{account} is {'deployed' if deployed else 'undeployed'} on "
        f"chain {chain_id}",
    )


__all__ = [
    "PROBE_ACCOUNT",
    "CalldataProbe",
    "CodeReader",
    "ProbeCheck",
    "probe_instrument",
]
