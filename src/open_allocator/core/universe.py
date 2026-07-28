from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from pydantic import BaseModel

from open_allocator.core import eligibility
from open_allocator.core.types import FrozenModel, Policy, Unknown, Vault

_MISSING = object()


class SkippedInstrument(FrozenModel):
    """An instrument the API served that could not be read as a :class:`Vault`."""

    instrument_id: str
    reason: str


def discover(client: object, policy: object | None = None) -> list[Vault]:
    vaults, _ = discover_instruments(client, policy)
    return vaults


def discover_instruments(
    client: object,
    policy: object | None = None,
) -> tuple[list[Vault], tuple[SkippedInstrument, ...]]:
    """Discover the universe, reporting instruments that could not be read.

    An instrument missing a required field is **skipped, not fatal**. Upstream
    serves rows whose metrics are absent — a freshly re-provisioned instrument
    is live with a null APY until its first sync lands — and one such row used
    to raise, taking the entire shelf down with it. A row we cannot price is
    one instrument we cannot offer, not an outage.

    Skips are returned rather than swallowed: an instrument that quietly
    vanishes from the universe is indistinguishable from one that was never
    there, and callers need to be able to say which happened.
    """
    response = client.list_instruments()
    instruments = list(_instrument_items(response))

    while _has_more(response):
        pagination = _required_pagination(response)
        limit = int(_required(pagination, "limit"))
        offset = int(_required(pagination, "offset"))
        response = client.list_instruments(limit=limit, offset=offset + limit)
        instruments.extend(_instrument_items(response))

    vaults: list[Vault] = []
    skipped: list[SkippedInstrument] = []
    for position, instrument in enumerate(instruments):
        try:
            vaults.append(_to_vault(instrument))
        except (ValueError, TypeError) as error:
            skipped.append(
                SkippedInstrument(
                    instrument_id=_identify(instrument, position),
                    reason=str(error),
                )
            )

    if policy is not None:
        vaults = [vault for vault in vaults if _allowed(vault, policy)]

    return vaults, tuple(skipped)


def _identify(instrument: object, position: int) -> str:
    """Best-effort id for an instrument we already failed to parse."""
    found = _value(instrument, "instrument_id", "instrumentId")
    if found is _MISSING or found is None:
        return f"<unidentified:index={position}>"
    return str(found)


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
        sector=_optional_text(instrument, "sector"),
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
