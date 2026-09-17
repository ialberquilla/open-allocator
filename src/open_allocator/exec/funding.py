"""Whether the account actually holds what a calldata plan spends.

1Tx simulates a bundle with balance state overrides (``assumedBalances``), so
a successful simulation proves the calls compose, not that the Safe owns the
tokens. This module closes that gap with a ledger in raw token units, per
(chain, account, token), walked in the exact order the plan will execute:

- before a bundle runs, its ``requires`` must be present, and are then spent;
- after it runs, its output is credited conservatively — ``min_out`` when
  quoted, otherwise ``expected_out`` less the configured slippage — so a
  withdrawal can fund what follows it in the same operation;
- ``leftovers`` are never credited: they are what a bundle *may* leave behind;
- after an ERC-4337 operation's calls, the paymaster's bounded maximum USDC
  charge must be present, because it is pulled in ``postOp``.

Each requirement is reported as the smallest starting balance the whole plan
needs, next to the balance actually read, so an announcement can say exactly
how short a plan is. A balance that cannot be read is a shortfall, never zero
and never enough.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import Field
from web3 import HTTPProvider, Web3

from open_allocator.core.types import FrozenModel, TxBundle
from open_allocator.exec import chains, erc20

_BPS = 10_000
_NATIVE_TOKENS = frozenset(
    {
        "0x0000000000000000000000000000000000000000",
        "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    }
)

# (chain id, rpc url, token, account) -> raw balance. Raises when unreadable.
BalanceReader = Callable[[int, str, str, str], int]
BalanceKey = tuple[int, str, str]


class FundingRequirement(FrozenModel):
    """One token the plan spends on one chain, against what the account holds."""

    chain_id: int = Field(ge=1)
    account: str
    token: str
    # The least balance the account must hold before the plan starts, after
    # conservative credits from earlier bundles in the same plan.
    required_raw: str = Field(pattern=r"^\d+$")
    # None when the balance could not be read; that is a shortfall.
    available_raw: str | None = Field(default=None, pattern=r"^\d+$")
    shortfall_raw: str = Field(pattern=r"^\d+$")
    bundle_ids: tuple[str, ...]
    # Whether ``required_raw`` includes a paymaster's maximum gas-token charge.
    includes_gas_charge: bool = False
    ok: bool


class FundingCheck(FrozenModel):
    requirements: tuple[FundingRequirement, ...] = ()
    messages: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return all(item.ok for item in self.requirements)

    @property
    def blockers(self) -> tuple[str, ...]:
        return tuple(
            _shortfall_message(item) for item in self.requirements if not item.ok
        )


@dataclass(frozen=True)
class LedgerOperation:
    """One wallet operation as the ledger sees it: bundles, then the gas charge."""

    chain_id: int
    bundles: tuple[TxBundle, ...]
    # The paymaster's gas token and bounded charge; None when the operation is
    # not paid in a token, or the charge could not be bounded.
    gas_token: str | None = None
    gas_charge_raw: int | None = None


@dataclass(frozen=True)
class _Movement:
    kind: Literal["need", "debit", "credit"]
    key: BalanceKey
    account: str
    token: str
    amount: int
    bundle_id: str | None
    gas_charge: bool = False


def conservative_output(bundle: TxBundle, slippage_bps: int) -> int:
    """What a bundle can be counted on to produce, in raw ``token_out`` units."""
    if bundle.min_out is not None:
        return int(bundle.min_out)
    if bundle.expected_out is None:
        return 0
    haircut = min(max(slippage_bps, 0), _BPS)
    return int(bundle.expected_out) * (_BPS - haircut) // _BPS


def check(
    operations: Sequence[LedgerOperation],
    balances: Mapping[BalanceKey, int | None],
    *,
    slippage_bps: int,
) -> tuple[FundingRequirement, ...]:
    """Walk the plan against starting balances; report every token it spends."""
    running: dict[BalanceKey, int] = {}
    required: dict[BalanceKey, int] = {}
    tokens: dict[BalanceKey, tuple[str, str]] = {}
    bundle_ids: dict[BalanceKey, list[str]] = {}
    gas_charged: set[BalanceKey] = set()

    for movement in _movements(operations, slippage_bps):
        key = movement.key
        # Net spend so far: debits less credits, relative to the start.
        spent = running.get(key, 0)
        if movement.kind == "need":
            tokens.setdefault(key, (movement.account, movement.token))
            required[key] = max(required.get(key, 0), spent + movement.amount)
            names = bundle_ids.setdefault(key, [])
            if movement.bundle_id is not None and movement.bundle_id not in names:
                names.append(movement.bundle_id)
            if movement.gas_charge:
                gas_charged.add(key)
        elif movement.kind == "debit":
            running[key] = spent + movement.amount
        else:
            running[key] = spent - movement.amount

    requirements: list[FundingRequirement] = []
    for key, amount in required.items():
        chain_id = key[0]
        account, token = tokens[key]
        available = balances.get(key)
        shortfall = amount if available is None else max(0, amount - available)
        requirements.append(
            FundingRequirement(
                chain_id=chain_id,
                account=account,
                token=token,
                required_raw=str(amount),
                available_raw=None if available is None else str(available),
                shortfall_raw=str(shortfall),
                bundle_ids=tuple(bundle_ids.get(key, ())),
                includes_gas_charge=key in gas_charged,
                ok=available is not None and shortfall == 0,
            )
        )
    return tuple(requirements)


def project(
    operations: Sequence[LedgerOperation],
    balances: Mapping[BalanceKey, int],
    *,
    slippage_bps: int,
) -> dict[BalanceKey, int]:
    """Balances after the operations, crediting only their conservative output."""
    projected = dict(balances)
    for movement in _movements(operations, slippage_bps):
        if movement.kind == "debit":
            projected[movement.key] = projected.get(movement.key, 0) - movement.amount
        elif movement.kind == "credit":
            projected[movement.key] = projected.get(movement.key, 0) + movement.amount
    return projected


def balance_keys(operations: Iterable[LedgerOperation]) -> tuple[BalanceKey, ...]:
    """Every balance a funding check of these operations needs, once each."""
    keys: dict[BalanceKey, None] = {}
    for movement in _movements(tuple(operations), 0):
        if movement.kind == "need":
            keys[movement.key] = None
    return tuple(keys)


def read_balances(
    keys: Iterable[BalanceKey],
    config: object | None,
) -> tuple[dict[BalanceKey, int | None], tuple[str, ...]]:
    """Read each balance, recording why any could not be read."""
    reader = _balance_reader(config)
    balances: dict[BalanceKey, int | None] = {}
    messages: list[str] = []
    for key in keys:
        chain_id, account, token = key
        rpc_url = chains.rpc_url(chain_id, config)
        if rpc_url is None:
            balances[key] = None
            messages.append(
                f"no RPC for {_chain(chain_id)}, so the balance of {token} cannot "
                f"be read; set RPC_URL_{chain_id}"
            )
            continue
        try:
            balances[key] = int(reader(chain_id, rpc_url, token, account))
        except Exception as error:  # noqa: BLE001 - unreadable is a shortfall
            balances[key] = None
            messages.append(
                f"could not read the balance of {token} on {_chain(chain_id)}: "
                f"{_error_text(error)}"
            )
    return balances, tuple(messages)


def check_operations(
    operations: Sequence[LedgerOperation],
    config: object | None,
    *,
    unsettled: Sequence[LedgerOperation] = (),
) -> FundingCheck:
    """Read balances and check the operations against them.

    ``unsettled`` are operations already submitted but not yet included: a
    fresh read does not reflect them yet, but they execute first, so their
    spends and conservative credits are applied to what is read. An included
    operation needs no such adjustment — the read already shows it — and
    anything else that moved the balance meanwhile is in the read either way.
    """
    rate = slippage_bps(config)
    balances, messages = read_balances(balance_keys(operations), config)
    if unsettled:
        after = project(
            unsettled,
            {key: value for key, value in balances.items() if value is not None},
            slippage_bps=rate,
        )
        balances = {
            key: None if value is None else max(after[key], 0)
            for key, value in balances.items()
        }
    requirements = check(operations, balances, slippage_bps=rate)
    return FundingCheck(requirements=requirements, messages=messages)


def slippage_bps(config: object | None) -> int:
    value = _config_value(config, "slippage_bps")
    return 0 if value is None else int(value)  # type: ignore[call-overload]


def key_for(chain_id: int, account: str, token: str) -> BalanceKey:
    return (chain_id, account.casefold(), token.casefold())


def onchain_balance(chain_id: int, rpc_url: str, token: str, account: str) -> int:
    """The default reader: ``balanceOf``, or the native balance for a sentinel."""
    _ = chain_id
    w3 = Web3(HTTPProvider(rpc_url))
    if token.casefold() in _NATIVE_TOKENS:
        try:
            return int(w3.eth.get_balance(Web3.to_checksum_address(account)))
        except Exception as error:
            raise erc20.BalanceReadError(
                f"native balance read failed ({type(error).__name__})"
            ) from None
    return erc20.balance_of(w3, token, owner=account)


def _movements(
    operations: Sequence[LedgerOperation],
    slippage_bps: int,
) -> Iterable[_Movement]:
    for operation in operations:
        account = ""
        for bundle in operation.bundles:
            account = bundle.account
            needs: dict[BalanceKey, tuple[str, int]] = {}
            for item in bundle.requires:
                key = key_for(bundle.chain_id, bundle.account, item.token)
                token, amount = needs.get(key, (item.token, 0))
                needs[key] = (token, amount + int(item.amount))
            for key, (token, amount) in needs.items():
                yield _Movement("need", key, account, token, amount, bundle.bundle_id)
                yield _Movement("debit", key, account, token, amount, bundle.bundle_id)
            credit = conservative_output(bundle, slippage_bps)
            if credit > 0:
                key = key_for(bundle.chain_id, bundle.account, bundle.token_out.address)
                yield _Movement(
                    "credit",
                    key,
                    account,
                    bundle.token_out.address,
                    credit,
                    bundle.bundle_id,
                )
        if (
            operation.gas_token is not None
            and operation.gas_charge_raw is not None
            and account
        ):
            key = key_for(operation.chain_id, account, operation.gas_token)
            amount = operation.gas_charge_raw
            yield _Movement(
                "need",
                key,
                account,
                operation.gas_token,
                amount,
                None,
                gas_charge=True,
            )
            yield _Movement("debit", key, account, operation.gas_token, amount, None)


def _balance_reader(config: object | None) -> BalanceReader:
    reader = _config_value(config, "token_balance_reader")
    return onchain_balance if reader is None else reader  # type: ignore[return-value]


def _shortfall_message(item: FundingRequirement) -> str:
    where = f"{_chain(item.chain_id)}"
    held = "an unreadable balance" if item.available_raw is None else item.available_raw
    what = (
        "including the paymaster's maximum gas charge"
        if item.includes_gas_charge
        else ""
    )
    bundles = ", ".join(item.bundle_ids) or "the gas charge"
    return (
        f"{item.account} needs {item.required_raw} raw units of {item.token} on "
        f"{where}{' ' + what if what else ''} for {bundles} but holds {held}; "
        f"short by {item.shortfall_raw}"
    )


def _chain(chain_id: int) -> str:
    return f"{chains.chain_name(chain_id)} (chain {chain_id})"


def _error_text(error: Exception) -> str:
    # A balance-read error from this module already names what failed without
    # quoting a URL; anything else is reduced to its type, because provider
    # errors quote RPC URLs and those carry API keys.
    if isinstance(error, erc20.BalanceReadError):
        return str(error)
    return type(error).__name__


def _config_value(config: object | None, attr: str) -> object | None:
    if config is None:
        return None
    if isinstance(config, Mapping):
        return config.get(attr)
    return getattr(config, attr, None)


__all__ = [
    "BalanceKey",
    "BalanceReader",
    "FundingCheck",
    "FundingRequirement",
    "LedgerOperation",
    "balance_keys",
    "check",
    "check_operations",
    "conservative_output",
    "key_for",
    "onchain_balance",
    "project",
    "read_balances",
    "slippage_bps",
]
