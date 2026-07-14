from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from pydantic import BaseModel

from open_allocator.core import eligibility
from open_allocator.core.types import Policy, Unknown, Vault

_MISSING = object()


def discover(client: object, policy: object | None = None) -> list[Vault]:
    response = client.list_instruments()
    instruments = list(_instrument_items(response))

    while _has_more(response):
        pagination = _required_pagination(response)
        limit = int(_required(pagination, "limit"))
        offset = int(_required(pagination, "offset"))
        response = client.list_instruments(limit=limit, offset=offset + limit)
        instruments.extend(_instrument_items(response))

    vaults = [_to_vault(instrument) for instrument in instruments]

    if policy is None:
        return vaults

    return [vault for vault in vaults if _allowed(vault, policy)]


def seen_protocols(vaults: Iterable[Vault]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(vault.protocol for vault in vaults))


def seen_chains(vaults: Iterable[Vault]) -> tuple[int, ...]:
    return tuple(dict.fromkeys(vault.chain_id for vault in vaults))


def _instrument_items(response: object) -> Sequence[object]:
    data = _value(response, "data")
    if data is not _MISSING:
        return _sequence(data)
    return _sequence(response)


def _has_more(response: object) -> bool:
    pagination = _value(response, "pagination")
    if pagination is _MISSING or pagination is None:
        return False
    has_more = _value(pagination, "has_more", "hasMore")
    if has_more is _MISSING or has_more is None:
        return False
    return bool(has_more)


def _required_pagination(response: object) -> object:
    pagination = _value(response, "pagination")
    if pagination is _MISSING or pagination is None:
        raise ValueError("paginated list_instruments() response is missing pagination")
    return pagination


def _sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return value
    raise TypeError("list_instruments() must return a sequence or an object with data")


def _to_vault(instrument: object) -> Vault:
    return Vault(
        instrument_id=str(_required(instrument, "instrument_id", "instrumentId")),
        protocol=str(_required(instrument, "protocol")),
        chain_id=int(_required(instrument, "chain_id", "chainId")),
        asset=str(_required(instrument, "asset", "token_symbol", "tokenSymbol")),
        asset_category=_optional_text(instrument, "asset_category", "assetCategory"),
        is_stablecoin=_optional_bool(instrument, "is_stablecoin", "isStablecoin"),
        apy=float(_required(instrument, "apy", "current_apy", "currentApy")),
        tvl_usd=float(_required(instrument, "tvl_usd", "tvlUsd", "tvl")),
        curator=_optional_risk(instrument, "curator"),
        reward_dependence=_optional_risk(
            instrument,
            "reward_dependence",
            "rewardDependence",
        ),
        oracle=_optional_risk(instrument, "oracle"),
        fee=_optional_risk(instrument, "fee"),
        apy_stability=_optional_risk(instrument, "apy_stability", "apyStability"),
        market_concentration=_optional_risk(
            instrument,
            "market_concentration",
            "marketConcentration",
        ),
        liquidity=_optional_risk(instrument, "liquidity"),
        collateral_mix=_optional_risk(instrument, "collateral_mix", "collateralMix"),
    )


def _allowed(vault: Vault, policy: object) -> bool:
    # Coarse discovery-time narrowing (allowlists minus curator + TVL floor);
    # the finer per-vault caps run later at candidate selection. Shares the one
    # rule engine in core.eligibility so axes can never drift between sites.
    policy_model = (
        policy if isinstance(policy, Policy) else Policy.model_validate(policy)
    )
    return eligibility.discovery_eligible(vault, policy_model)


def _optional_text(value: object, *names: str) -> str | None:
    found = _value(value, *names)
    if found is _MISSING or found is None:
        return None
    return str(found)


def _optional_bool(value: object, *names: str) -> bool | None:
    found = _value(value, *names)
    if found is _MISSING or found is None:
        return None
    return bool(found)


def _required(value: object, *names: str) -> object:
    for source in _sources(value):
        for name in names:
            found = _source_value(source, name)
            if found is not _MISSING and found is not None:
                return found

    joined_names = ", ".join(names)
    raise ValueError(f"instrument is missing required field: {joined_names}")


def _optional_risk(value: object, *names: str) -> object:
    found = _value(value, *names)
    if found is _MISSING:
        return Unknown
    return found


def _value(value: object, *names: str) -> object:
    for source in _sources(value):
        for name in names:
            found = _source_value(source, name)
            if found is not _MISSING:
                return found
    return _MISSING


def _source_value(source: object, name: str) -> object:
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
