from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from math import isfinite, log10

from open_allocator.core.types import FactorScore, Unknown, Vault, VaultScore

DEFAULT_WEIGHTS: Mapping[str, float] = {
    "tvl": 1.25,
    "apy_stability": 1.0,
    "reward_dependence": 1.25,
    "liquidity": 1.25,
    "oracle": 1.0,
    "fee": 0.75,
    "curator": 0.75,
    "market_concentration": 1.0,
    "collateral_mix": 0.75,
}

_TVL_FLOOR_USD = 100_000.0
_TVL_CAP_USD = 50_000_000.0
_LIQUIDITY_FLOOR_USD = 100_000.0
_LIQUIDITY_CAP_USD = 10_000_000.0
_APY_STABILITY_CV_CAP = 1.0

type Normalizer = Callable[[Vault], tuple[object, float | None]]


def score_vault(
    vault: Vault,
    weights: Mapping[str, float] | None = None,
) -> VaultScore:
    factor_weights = DEFAULT_WEIGHTS if weights is None else weights
    factors: dict[str, FactorScore] = {}

    for name, weight in factor_weights.items():
        normalizer = _NORMALIZERS.get(name)
        if normalizer is None:
            raise ValueError(f"unknown scoring factor: {name}")

        raw_input, normalized_value = normalizer(vault)
        unknown = normalized_value is None
        factors[name] = FactorScore(
            raw_input=raw_input,
            normalized_value=None if unknown else _clamp(normalized_value),
            weight=float(weight),
            unknown=unknown,
        )

    return VaultScore(
        instrument_id=vault.instrument_id,
        score=_composite(factors),
        factors=factors,
    )


def _composite(factors: Mapping[str, FactorScore]) -> float:
    known_factors = [factor for factor in factors.values() if not factor.unknown]
    total_weight = sum(factor.weight for factor in known_factors)
    if total_weight == 0:
        return 0.0
    return (
        sum(
            factor.normalized_value * factor.weight
            for factor in known_factors
            if factor.normalized_value is not None
        )
        / total_weight
    )


def _tvl(vault: Vault) -> tuple[object, float | None]:
    return vault.tvl_usd, _normalize_usd_depth(
        vault.tvl_usd,
        floor=_TVL_FLOOR_USD,
        cap=_TVL_CAP_USD,
    )


def _apy_stability(vault: Vault) -> tuple[object, float | None]:
    return vault.apy_stability, _normalize_inverse_bounded_number(
        vault.apy_stability,
        cap=_APY_STABILITY_CV_CAP,
    )


def _reward_dependence(vault: Vault) -> tuple[object, float | None]:
    return vault.reward_dependence, _normalize_inverse_ratio(vault.reward_dependence)


def _liquidity(vault: Vault) -> tuple[object, float | None]:
    raw = vault.liquidity
    value = _number(raw)
    if value is None or value < 0:
        return raw, None
    if value <= 1:
        return raw, 1 - _clamp(value)
    return raw, _normalize_usd_depth(
        value,
        floor=_LIQUIDITY_FLOOR_USD,
        cap=_LIQUIDITY_CAP_USD,
    )


def _oracle(vault: Vault) -> tuple[object, float | None]:
    return vault.oracle, _normalize_oracle(vault.oracle)


def _fee(vault: Vault) -> tuple[object, float | None]:
    return vault.fee, _normalize_inverse_ratio(vault.fee)


def _curator(vault: Vault) -> tuple[object, float | None]:
    raw = vault.curator
    if _unknown(raw):
        return raw, None
    return raw, 1.0


def _market_concentration(vault: Vault) -> tuple[object, float | None]:
    return vault.market_concentration, _normalize_inverse_ratio(
        vault.market_concentration
    )


def _collateral_mix(vault: Vault) -> tuple[object, float | None]:
    return vault.collateral_mix, _normalize_collateral_mix(vault.collateral_mix)


_NORMALIZERS: Mapping[str, Normalizer] = {
    "tvl": _tvl,
    "apy_stability": _apy_stability,
    "reward_dependence": _reward_dependence,
    "liquidity": _liquidity,
    "oracle": _oracle,
    "fee": _fee,
    "curator": _curator,
    "market_concentration": _market_concentration,
    "collateral_mix": _collateral_mix,
}


def _normalize_usd_depth(raw: object, *, floor: float, cap: float) -> float | None:
    value = _number(raw)
    if value is None or value < 0:
        return None
    if value <= floor:
        return 0.0
    if value >= cap:
        return 1.0
    return log10(value / floor) / log10(cap / floor)


def _normalize_inverse_bounded_number(raw: object, *, cap: float) -> float | None:
    value = _number(raw)
    if value is None or value < 0:
        return None
    return 1 - _clamp(value / cap)


def _normalize_inverse_ratio(raw: object) -> float | None:
    ratio = _ratio(raw)
    if ratio is None:
        return None
    return 1 - ratio


def _ratio(raw: object) -> float | None:
    value = _number(raw)
    if value is None or value < 0:
        return None
    if value > 1 and value <= 100:
        value = value / 100
    return _clamp(value)


def _normalize_oracle(raw: object) -> float | None:
    if _unknown(raw):
        return None

    value = str(raw).strip().lower()
    for keyword, score in _ORACLE_KEYWORD_SCORES.items():
        if keyword in value:
            return score
    return None


_ORACLE_KEYWORD_SCORES: Mapping[str, float] = {
    "chainlink": 1.0,
    "chronicle": 0.9,
    "pyth": 0.9,
    "redstone": 0.9,
    "twap": 0.6,
    "uniswap": 0.5,
    "custom": 0.4,
    "internal": 0.3,
    "none": 0.0,
}


def _normalize_collateral_mix(raw: object) -> float | None:
    if _unknown(raw):
        return None
    concentration = _collateral_concentration(raw)
    if concentration is None:
        return None
    return 1 - concentration


def _collateral_concentration(raw: object) -> float | None:
    ratio = _ratio(raw)
    if ratio is not None:
        return ratio

    if isinstance(raw, Mapping):
        values = [value for value in (_number(item) for item in raw.values()) if value]
        total = sum(values)
        if total <= 0:
            return None
        return _clamp(max(values) / total)

    if isinstance(raw, Sequence) and not isinstance(raw, str | bytes | bytearray):
        if not raw:
            return None
        unique_count = len({str(item) for item in raw})
        return 1 - _clamp((unique_count - 1) / 4)

    return None


def _number(raw: object) -> float | None:
    if _unknown(raw):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not isfinite(value):
        return None
    return value


def _unknown(raw: object) -> bool:
    if raw is None or raw == Unknown:
        return True
    if isinstance(raw, str) and raw.strip().lower() == "unknown":
        return True
    return False


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))
