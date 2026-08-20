import os

import pytest

from open_allocator.exec import chains
from open_allocator.exec.chains import (
    DEFAULT_RPC_URLS,
    USDC_ADDRESSES,
    MissingRPCError,
    chain_name,
    require_rpc_url,
    rpc_url,
    usdc_address,
)


@pytest.fixture(autouse=True)
def clear_rpc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(("RPC_URL_", "PAYMASTER_USDC_ADDRESS_")):
            monkeypatch.delenv(name, raising=False)


def test_rpc_url_prefers_env_override_over_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RPC_URL_8453", "https://rpc.example/base")

    assert DEFAULT_RPC_URLS[8453] != "https://rpc.example/base"
    assert rpc_url(8453) == "https://rpc.example/base"


def test_rpc_url_prefers_config_override_over_default() -> None:
    assert rpc_url(8453, {"RPC_URL_8453": "https://rpc.example/config-base"}) == (
        "https://rpc.example/config-base"
    )


def test_unknown_chain_without_override_has_no_rpc() -> None:
    assert rpc_url(999999, {}) is None


def test_require_rpc_url_raises_clear_missing_rpc_error() -> None:
    with pytest.raises(MissingRPCError, match="^no RPC for chain 999999$"):
        require_rpc_url(999999, {})


def test_chain_name_is_display_only_with_unknown_fallback() -> None:
    assert chain_name(8453) == "Base"
    assert chain_name(999999) == "chain 999999"


def test_usdc_is_a_different_address_on_every_chain() -> None:
    """The whole reason this is a registry: one address cannot serve a run."""
    assert len(set(USDC_ADDRESSES.values())) == len(USDC_ADDRESSES)
    assert usdc_address(8453) != usdc_address(42161)
    assert usdc_address(130) != usdc_address(8453)


def test_usdc_address_prefers_env_override_over_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "PAYMASTER_USDC_ADDRESS_8453",
        "0x0000000000000000000000000000000000000c0c",
    )

    assert usdc_address(8453) == "0x0000000000000000000000000000000000000c0c"
    assert usdc_address(42161) == USDC_ADDRESSES[42161]


def test_usdc_address_prefers_config_override_over_registry() -> None:
    override = {"PAYMASTER_USDC_ADDRESS_8453": "0x" + "0c" * 20}

    assert usdc_address(8453, override) == "0x" + "0c" * 20


def test_rpc_mapping_does_not_answer_the_gas_token_lookup() -> None:
    """Both lookups accepted a bare chain-id key would cross-answer."""
    assert (
        usdc_address(8453, {8453: "https://rpc.example/base"}) == (USDC_ADDRESSES[8453])
    )


def test_unknown_chain_has_no_gas_token() -> None:
    assert usdc_address(999999, {}) is None


def test_every_registry_row_names_its_gas_token() -> None:
    """The default is ETH, so the rows that are not ETH are the ones that matter."""
    for chain_id, info in chains.DEFAULT_CHAINS.items():
        assert info.native_symbol, f"chain {chain_id} has no native symbol"


def test_the_known_non_eth_chains_are_marked_as_such() -> None:
    assert chains.native_symbol(143) == "MON"
    assert chains.pays_gas_in_eth(143) is False
    for chain_id in (56, 100, 137, 146, 250, 5000, 42220, 43114, 80094):
        assert chains.pays_gas_in_eth(chain_id) is False


def test_the_eth_chains_still_answer_true() -> None:
    for chain_id in (1, 10, 130, 8453, 42161, 59144, 534352):
        assert chains.pays_gas_in_eth(chain_id) is True


def test_an_unknown_chain_has_no_gas_token_and_is_not_eth() -> None:
    """Unknown answers False here, unlike supports_deterministic_safe: a wrong
    guess about the gas token converts a price at the wrong rate."""
    assert chains.native_symbol(999_999) is None
    assert chains.pays_gas_in_eth(999_999) is False
    assert chains.supports_deterministic_safe(999_999) is True
