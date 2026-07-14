from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, Field

from open_allocator.core.types import Allocation, FrozenModel

_MISSING = object()
_USDC_QUANTUM = Decimal("0.000001")
_EPSILON = Decimal("0.0000005")


class PositionHolding(FrozenModel):
    instrument_id: str
    protocol: str
    chain_id: int
    symbol: str
    balance: str
    balance_raw: str | None = None
    decimals: int | None = Field(default=None, ge=0)
    usd_value: float = Field(ge=0)
    share_balance: str
    share_balance_raw: str
    share_decimals: int = Field(ge=0)
    yield_token_symbol: str | None = None
    yield_token_address: str | None = None
    description: str | None = None
    current_apy: float | None = None


class IdleBalance(FrozenModel):
    chain_id: int
    chain_name: str | None = None
    usdc_balance: str
    usdc_balance_raw: str | None = None
    usd_value: float = Field(ge=0)


class Positions(FrozenModel):
    address: str
    holdings: tuple[PositionHolding, ...]
    idle_balances: tuple[IdleBalance, ...]
    total_position_usd: float = Field(ge=0)
    total_idle_usdc: float = Field(ge=0)
    total_usd: float = Field(ge=0)
    total_usdc_usd: str | None = None


class PositionDelta(FrozenModel):
    instrument_id: str
    action: Literal["buy", "sell"]
    current_usd: float = Field(ge=0)
    target_usd: float = Field(ge=0)
    delta_usd: float
    buy_usd: float = Field(ge=0)
    sell_usd: float = Field(ge=0)
    current_weight: float = Field(ge=0)
    target_weight: float = Field(ge=0)


class Diff(FrozenModel):
    deltas: tuple[PositionDelta, ...]
    total_usd: float = Field(ge=0)
    total_position_usd: float = Field(ge=0)
    idle_usdc: float = Field(ge=0)
    target_allocation_total_usd: float = Field(ge=0)
    total_buy_usd: float = Field(ge=0)
    total_sell_usd: float = Field(ge=0)
    deploy_usdc: float = Field(ge=0)


def read_positions(client: object, address: str) -> Positions:
    address_text = str(address)
    balances_response = _client_call(client, "balances", address_text)
    idle_balances = _idle_balances(balances_response)

    holdings: list[PositionHolding] = []
    for chain_id in dict.fromkeys(balance.chain_id for balance in idle_balances):
        response = _client_call(
            client,
            "positions",
            {"address": address_text, "chainId": chain_id},
        )
        holdings.extend(_holdings(response, default_chain_id=chain_id))

    return _positions(
        address=address_text,
        holdings=holdings,
        idle_balances=idle_balances,
        total_usdc_usd=_optional_text(
            balances_response,
            "total_usdc_usd",
            "totalUsdcUsd",
        ),
    )


def reconcile(
    positions: Positions | Mapping[str, object],
    target_allocation: Allocation | Mapping[str, object],
) -> Diff:
    current = (
        positions
        if isinstance(positions, Positions)
        else Positions.model_validate(positions)
    )
    target = (
        target_allocation
        if isinstance(target_allocation, Allocation)
        else Allocation.model_validate(target_allocation)
    )

    current_usd = _current_usd_by_instrument(current)
    target_weights = _target_weights(target)
    total_usd = _money_decimal(current.total_usd, "positions.total_usd")
    target_usd = {
        instrument_id: _quantize(total_usd * weight)
        for instrument_id, weight in target_weights.items()
    }

    deltas: list[PositionDelta] = []
    for instrument_id in sorted(set(current_usd) | set(target_usd)):
        current_amount = current_usd.get(instrument_id, Decimal("0"))
        target_amount = target_usd.get(instrument_id, Decimal("0"))
        delta = _quantize(target_amount - current_amount)
        if abs(delta) <= _EPSILON:
            continue

        buy = max(delta, Decimal("0"))
        sell = max(-delta, Decimal("0"))
        deltas.append(
            PositionDelta(
                instrument_id=instrument_id,
                action="buy" if delta > 0 else "sell",
                current_usd=_amount(current_amount),
                target_usd=_amount(target_amount),
                delta_usd=_signed_amount(delta),
                buy_usd=_amount(buy),
                sell_usd=_amount(sell),
                current_weight=_weight(current_amount, total_usd),
                target_weight=float(target_weights.get(instrument_id, Decimal("0"))),
            )
        )

    total_buy = sum(
        (_money_decimal(delta.buy_usd, "buy_usd") for delta in deltas),
        Decimal("0"),
    )
    total_sell = sum(
        (_money_decimal(delta.sell_usd, "sell_usd") for delta in deltas),
        Decimal("0"),
    )
    deploy_usdc = max(_quantize(total_buy - total_sell), Decimal("0"))

    return Diff(
        deltas=tuple(deltas),
        total_usd=_amount(total_usd),
        total_position_usd=current.total_position_usd,
        idle_usdc=current.total_idle_usdc,
        target_allocation_total_usd=target.total_usd,
        total_buy_usd=_amount(total_buy),
        total_sell_usd=_amount(total_sell),
        deploy_usdc=_amount(deploy_usdc),
    )


def _client_call(client: object, name: str, *args: object) -> object:
    method = getattr(client, name, None)
    if not callable(method):
        raise TypeError(f"client does not implement {name}")
    return method(*args)


def _positions(
    *,
    address: str,
    holdings: Sequence[PositionHolding],
    idle_balances: Sequence[IdleBalance],
    total_usdc_usd: str | None,
) -> Positions:
    sorted_holdings = tuple(
        sorted(holdings, key=lambda item: (item.chain_id, item.instrument_id))
    )
    sorted_idle = tuple(sorted(idle_balances, key=lambda item: item.chain_id))
    total_position_usd = sum(
        _money_decimal(holding.usd_value, "holding.usd_value")
        for holding in sorted_holdings
    )
    total_idle_usdc = sum(
        _money_decimal(balance.usd_value, "idle.usd_value")
        for balance in sorted_idle
    )

    return Positions(
        address=address,
        holdings=sorted_holdings,
        idle_balances=sorted_idle,
        total_position_usd=_amount(total_position_usd),
        total_idle_usdc=_amount(total_idle_usdc),
        total_usd=_amount(total_position_usd + total_idle_usdc),
        total_usdc_usd=total_usdc_usd,
    )


def _idle_balances(response: object) -> tuple[IdleBalance, ...]:
    balances = _sequence(_required(response, "balances"), "balances")
    parsed: list[IdleBalance] = []
    for balance in balances:
        usdc_balance = str(_required(balance, "usdc_balance", "usdcBalance"))
        parsed.append(
            IdleBalance(
                chain_id=int(_required(balance, "chain_id", "chainId")),
                chain_name=_optional_text(balance, "chain_name", "chainName"),
                usdc_balance=usdc_balance,
                usdc_balance_raw=_optional_text(
                    balance,
                    "usdc_balance_raw",
                    "usdcBalanceRaw",
                ),
                usd_value=_amount(_money_decimal(usdc_balance, "usdc_balance")),
            )
        )
    return tuple(parsed)


def _holdings(
    response: object,
    *,
    default_chain_id: int,
) -> tuple[PositionHolding, ...]:
    positions = _sequence(_required(response, "positions"), "positions")
    response_chain_id = _value(response, "chain_id", "chainId")
    parsed: list[PositionHolding] = []
    for position in positions:
        balance = str(_required(position, "balance"))
        chain_id = _value(position, "chain_id", "chainId")
        if chain_id is _MISSING or chain_id is None:
            chain_id = response_chain_id
        if chain_id is _MISSING or chain_id is None:
            chain_id = default_chain_id

        parsed.append(
            PositionHolding(
                instrument_id=str(
                    _required(position, "instrument_id", "instrumentId")
                ),
                protocol=str(_required(position, "protocol")),
                chain_id=int(chain_id),
                symbol=str(_required(position, "symbol")),
                balance=balance,
                balance_raw=_optional_text(position, "balance_raw", "balanceRaw"),
                decimals=_optional_int(position, "decimals"),
                usd_value=_amount(_money_decimal(balance, "position.balance")),
                share_balance=str(
                    _required(position, "share_balance", "shareBalance")
                ),
                share_balance_raw=str(
                    _required(position, "share_balance_raw", "shareBalanceRaw")
                ),
                share_decimals=int(
                    _required(position, "share_decimals", "shareDecimals")
                ),
                yield_token_symbol=_optional_text(
                    position,
                    "yield_token_symbol",
                    "yieldTokenSymbol",
                ),
                yield_token_address=_optional_text(
                    position,
                    "yield_token_address",
                    "yieldTokenAddress",
                ),
                description=_optional_text(position, "description"),
                current_apy=_optional_float(position, "current_apy", "currentApy"),
            )
        )
    return tuple(parsed)


def _current_usd_by_instrument(positions: Positions) -> dict[str, Decimal]:
    current: dict[str, Decimal] = defaultdict(Decimal)
    for holding in positions.holdings:
        current[holding.instrument_id] += _money_decimal(
            holding.usd_value,
            "holding.usd_value",
        )
    return dict(current)


def _target_weights(allocation: Allocation) -> dict[str, Decimal]:
    weights: dict[str, Decimal] = defaultdict(Decimal)
    for leg in allocation.legs:
        weights[leg.instrument_id] += _money_decimal(leg.weight, "leg.weight")

    total_weight = sum(weights.values(), Decimal("0"))
    if total_weight <= 0:
        return {}
    return {
        instrument_id: weight / total_weight
        for instrument_id, weight in weights.items()
        if weight > 0
    }


def _required(value: object, *names: str) -> object:
    found = _value(value, *names)
    if found is _MISSING or found is None:
        joined = ", ".join(names)
        raise ValueError(f"positions response is missing required field: {joined}")
    return found


def _optional_text(value: object, *names: str) -> str | None:
    found = _value(value, *names)
    if found is _MISSING or found is None:
        return None
    return str(found)


def _optional_int(value: object, *names: str) -> int | None:
    found = _value(value, *names)
    if found is _MISSING or found is None:
        return None
    return int(found)


def _optional_float(value: object, *names: str) -> float | None:
    found = _value(value, *names)
    if found is _MISSING or found is None:
        return None
    return float(found)


def _value(value: object, *names: str) -> object:
    for source in _sources(value):
        for name in names:
            if isinstance(source, Mapping):
                if name in source:
                    return source[name]
            elif hasattr(source, name):
                return getattr(source, name)
    return _MISSING


def _sources(value: object) -> tuple[object, ...]:
    if isinstance(value, BaseModel):
        return (
            value,
            value.model_dump(by_alias=False),
            value.model_dump(by_alias=True),
        )
    if isinstance(value, Mapping):
        return (value,)
    return (value,)


def _sequence(value: object, name: str) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return value
    raise TypeError(f"{name} must be an array")


def _money_decimal(value: object, name: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{name} must be a finite non-negative number") from error
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return amount


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_USDC_QUANTUM)


def _amount(value: Decimal) -> float:
    return float(_quantize(value))


def _signed_amount(value: Decimal) -> float:
    return float(_quantize(value))


def _weight(value: Decimal, total: Decimal) -> float:
    if total <= 0:
        return 0.0
    return float(value / total)


__all__ = [
    "Diff",
    "IdleBalance",
    "PositionDelta",
    "PositionHolding",
    "Positions",
    "read_positions",
    "reconcile",
]
