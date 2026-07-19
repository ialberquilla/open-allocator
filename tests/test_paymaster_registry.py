from __future__ import annotations

import pytest

from open_allocator.exec import paymaster_registry as registry
from open_allocator.exec.paymaster_registry import (
    ChainNotGasPayable,
    is_gas_payable,
    paymaster_chain,
    require_paymaster_chain,
)

BASE = 8453
ARBITRUM = 42161
MONAD = 143
ETHEREUM = 1
SONIC = 146
UNLISTED = 999_999


def test_known_chain_resolves_to_the_default_provider() -> None:
    row = require_paymaster_chain(BASE)

    assert row.provider == "pimlico"
    # v0.7, not v0.8: Safe4337Module pins its EntryPoint immutably and the
    # latest release only speaks v0.7, so a Safe cannot submit a v0.8 userOp.
    assert row.entry_point_version == "v0.7"


def test_pimlico_is_the_default_provider() -> None:
    assert registry.DEFAULT_PROVIDER == "pimlico"


def test_unknown_chain_is_not_gas_payable_and_does_not_crash() -> None:
    assert paymaster_chain(UNLISTED) is None
    assert is_gas_payable(UNLISTED) is False


def test_requiring_an_unknown_chain_raises_a_typed_error() -> None:
    with pytest.raises(ChainNotGasPayable) as error:
        require_paymaster_chain(UNLISTED)

    # Not gas-payable is a capability fact, not a universe gate: the chain must
    # stay discoverable and scorable.
    assert "remains" in str(error.value)
    assert "scorable" in str(error.value)


def test_monad_is_gas_payable_via_pimlico() -> None:
    assert is_gas_payable(MONAD)
    assert require_paymaster_chain(MONAD).provider == "pimlico"


def test_monad_is_not_gas_payable_via_circle() -> None:
    assert paymaster_chain(MONAD, provider="circle") is None


def test_circle_serves_its_seven_chains() -> None:
    for chain_id in (ETHEREUM, BASE, ARBITRUM):
        assert is_gas_payable(chain_id, provider="circle")


def test_circle_is_narrower_than_pimlico() -> None:
    pimlico_chains = {c for c in registry.PAYMASTER_CHAINS if is_gas_payable(c)}
    circle_chains = {
        c for c in registry.PAYMASTER_CHAINS if is_gas_payable(c, provider="circle")
    }

    assert circle_chains < pimlico_chains


def test_sonic_is_pimlico_only() -> None:
    assert is_gas_payable(SONIC)
    assert not is_gas_payable(SONIC, provider="circle")


# --- the registry states capability, never cost ---------------------------


def test_registry_makes_no_cost_claims() -> None:
    # Pimlico bakes its fee into the exchangeRate from pimlico_getTokenQuotes,
    # so a static per-chain surcharge cannot be honest. The registry answers
    # "can this chain pay gas in USDC" and nothing about price.
    assert not hasattr(registry, "surcharge_bps")
    assert not hasattr(require_paymaster_chain(BASE), "surcharge_bps")


# --- provider swap changes endpoints only ---------------------------------


def test_provider_swap_keeps_the_chain_identity() -> None:
    pimlico = require_paymaster_chain(BASE, provider="pimlico")
    circle = require_paymaster_chain(BASE, provider="circle")

    assert pimlico.chain_id == circle.chain_id == BASE
    assert pimlico.provider != circle.provider


def test_entry_point_is_a_protocol_singleton() -> None:
    # EntryPoint is the same address on every chain; only the version varies.
    assert require_paymaster_chain(BASE).entry_point == registry.ENTRY_POINT_V07
    assert registry.ENTRY_POINTS["v0.8"] == registry.ENTRY_POINT_V08


def test_every_chain_defaults_to_the_entry_point_a_safe_can_use() -> None:
    # Safe4337Module v0.3.0 is pinned to EntryPoint v0.7 in an immutable
    # constructor arg (verified on-chain, Base + Monad). A v0.8 default would
    # make the Safe path — the point of this phase — silently unusable.
    for chain_id in registry.PAYMASTER_CHAINS:
        assert require_paymaster_chain(chain_id).entry_point_version == "v0.7"


def test_paymaster_address_tracks_the_entry_point_version() -> None:
    # Pimlico's ERC-20 paymaster is a different contract per EntryPoint version,
    # so the address cannot be a single constant.
    assert require_paymaster_chain(BASE).paymaster_address == (
        registry.ERC20_PAYMASTERS["v0.7"]
    )
    assert len(set(registry.ERC20_PAYMASTERS.values())) == 3
