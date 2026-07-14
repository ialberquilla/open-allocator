import os

import pytest

from open_allocator.exec.chains import (
    DEFAULT_RPC_URLS,
    MissingRPCError,
    chain_name,
    require_rpc_url,
    rpc_url,
)


@pytest.fixture(autouse=True)
def clear_rpc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("RPC_URL_"):
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
