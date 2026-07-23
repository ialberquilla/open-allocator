import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from open_allocator import cli
from open_allocator.cli import JsonObject, json_command
from open_allocator.core.schema import validate

runner = CliRunner()

COMMANDS = [
    "wallet-status",
    "list-vaults",
    "score-vault",
    "build-allocation",
    "simulate",
    "check-policy",
    "build-tx",
    "execute",
    "positions",
    "rebalance",
    "withdraw",
]

EXECUTION_COMMANDS = {"execute", "rebalance", "withdraw"}
EXECUTION_SURFACE_COMMANDS = {
    "wallet-status",
    "build-tx",
    "execute",
    "rebalance",
    "withdraw",
}
READ_ONLY_COMMANDS = {"list-vaults", "score-vault", "positions"}
ALLOCATION_COMMANDS = {"build-allocation", "simulate", "check-policy"}


@pytest.fixture(autouse=True)
def clear_cli_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if (
            name.startswith("ONE_TX_")
            or name.startswith("RPC_URL_")
            or name == "SIGNER_MODE"
            or name == "OPEN_ALLOCATOR_IDEMPOTENCY_STORE"
            or name == "OPEN_ALLOCATOR_CHECKPOINT_DIR"
            or name == "OPEN_ALLOCATOR_ALLOCATION_LOG"
        ):
            monkeypatch.delenv(name, raising=False)


def parse_single_stdout_object(stdout: str) -> JsonObject:
    assert stdout.endswith("\n")
    assert stdout.count("\n") == 1
    payload = json.loads(stdout)
    assert isinstance(payload, dict)
    return payload


def parse_single_stdout_value(stdout: str) -> object:
    assert stdout.endswith("\n")
    assert stdout.count("\n") == 1
    return json.loads(stdout)


def set_read_only_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONE_TX_API_URL", "http://localhost:3001/api/v1")
    monkeypatch.setenv("ONE_TX_API_KEY", "test-api-key")


def instrument(**overrides: Any) -> dict[str, Any]:
    data = {
        "instrumentId": "base-aave-usdc",
        "protocol": "aave",
        "chainId": 8453,
        "tokenSymbol": "USDC",
        "isStablecoin": True,
        "assetCategory": "USD",
        "currentApy": 0.04,
        "tvl": 10_000_000,
        "apyStability": 0.2,
        "rewardDependence": 0.1,
        "liquidity": 5_000_000,
        "oracle": "chainlink",
        "fee": 0.05,
        "curator": "curator-a",
        "marketConcentration": 0.4,
        "collateralMix": {"USDC": 1},
    }
    data.update(overrides)
    return data


@pytest.fixture
def fixture_instruments() -> list[dict[str, Any]]:
    return [
        instrument(),
        instrument(
            instrumentId="arbitrum-morpho-usdc",
            protocol="morpho",
            chainId=42161,
            currentApy=0.08,
            tvl=4_000_000,
            rewardDependence=0.4,
            curator="curator-b",
        ),
        instrument(
            instrumentId="optimism-compound-dai",
            protocol="compound",
            chainId=10,
            tokenSymbol="DAI",
            currentApy=0.03,
            tvl=25_000_000,
            rewardDependence=0.0,
            curator="curator-c",
        ),
    ]


@pytest.fixture
def compliant_instruments() -> list[dict[str, Any]]:
    return [
        instrument(
            instrumentId="base-aave-usdc",
            protocol="aave",
            chainId=8453,
            tokenSymbol="USDC",
            currentApy=0.04,
            tvl=10_000_000,
            rewardDependence=0.1,
            curator="curator-a",
        ),
        instrument(
            instrumentId="arbitrum-morpho-usdc",
            protocol="morpho",
            chainId=42161,
            tokenSymbol="USDC",
            currentApy=0.08,
            tvl=8_000_000,
            rewardDependence=0.4,
            curator="curator-b",
        ),
        instrument(
            instrumentId="optimism-compound-dai",
            protocol="compound",
            chainId=10,
            tokenSymbol="DAI",
            currentApy=0.03,
            tvl=25_000_000,
            rewardDependence=0.0,
            curator="curator-c",
        ),
        instrument(
            instrumentId="mainnet-spark-usdt",
            protocol="spark",
            chainId=1,
            tokenSymbol="USDT",
            currentApy=0.05,
            tvl=40_000_000,
            rewardDependence=0.05,
            curator="curator-d",
        ),
    ]


def simulation_payload() -> dict[str, Any]:
    return {
        "resolvedCount": 4,
        "warnings": ["descriptive only"],
        "lookbackDays": 90,
        "principalUsd": 10_000,
        "finalValueUsd": 10_100,
        "realizedReturnPct": 1.0,
        "annualizedPct": 4.0,
        "blendedApyVolPct": 0.2,
        "maxYieldDrawdownPct": 0.1,
        "benchmark": {
            "kind": "index",
            "label": "USD_INDEX",
            "finalValueUsd": 10_080,
            "annualizedPct": 3.2,
            "outperformancePct": 0.8,
        },
        "coveragePct": 100,
        "daysSimulated": 90,
        "headline": "descriptive backtest",
        "caveats": ["descriptive, not predictive"],
    }


def install_mock_onetx_client(
    monkeypatch: pytest.MonkeyPatch,
    instruments: list[dict[str, Any]],
) -> type:
    class MockOneTxClient:
        configs: list[object] = []

        def __init__(self, config: object) -> None:
            self.config = config
            self.calls = 0
            self.configs.append(config)

        def __enter__(self) -> "MockOneTxClient":
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def list_instruments(self) -> object:
            self.calls += 1
            return SimpleNamespace(data=tuple(instruments))

        def metrics_bulk(
            self,
            instrument_ids: tuple[str, ...],
            days: int,
        ) -> list[dict[str, object]]:
            self.calls += 1
            return [
                {"instrumentId": instrument_id, "metrics": []}
                for instrument_id in instrument_ids
            ]

        def instrument_analysis(self, instrument_id: str) -> dict[str, object]:
            self.calls += 1
            return {}

        def simulate_portfolio(self, body: dict[str, object]) -> dict[str, Any]:
            self.calls += 1
            self.simulate_body = body
            return simulation_payload()

    monkeypatch.setattr(cli, "OneTxClient", MockOneTxClient)
    return MockOneTxClient


def set_execution_config(
    monkeypatch: pytest.MonkeyPatch,
    *,
    idempotency_store_path: Path | None = None,
) -> SimpleNamespace:
    config = SimpleNamespace(
        onetx_api_url="http://localhost:3001/api/v1",
        onetx_api_key="test-api-key",
        signer_mode="local-eoa",
        private_key="0x" + "11" * 32,
        _rpc_overrides={8453: "rpc://base", 999999: "rpc://missing"},
        gas_checker=lambda _address, _chain_id, _rpc_url, _config: True,
        idempotency_store_path=idempotency_store_path,
    )
    monkeypatch.setattr(cli, "AllocatorConfig", lambda: config)
    return config


class ExecutionSignerSpy:
    def __init__(self, *, fail_at: int | None = None) -> None:
        self.address_calls = 0
        self.sent: list[tuple[dict[str, Any] | object, str]] = []
        self.fail_at = fail_at

    def address(self) -> str:
        self.address_calls += 1
        return "0x0000000000000000000000000000000000000001"

    def send(self, tx: object, rpc_url: str) -> dict[str, Any]:
        if self.fail_at == len(self.sent):
            raise RuntimeError("broadcast failed")
        self.sent.append((tx, rpc_url))
        index = len(self.sent)
        to_address = getattr(tx, "to", None)
        return {
            "transaction_hash": f"0x{index:064x}",
            "block_number": index,
            "gas_used": 21_000,
            "status": 1,
            "from_address": self.address(),
            "to_address": to_address,
            "contract_address": None,
            "effective_gas_price": None,
        }


class ExecutionOneTxClient:
    instances: list["ExecutionOneTxClient"] = []
    build_buy_bodies: list[dict[str, object]] = []

    def __init__(self, config: object) -> None:
        self.config = config
        self.instances.append(self)

    def __enter__(self) -> "ExecutionOneTxClient":
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def balances(self, address: str) -> dict[str, Any]:
        return {
            "address": address,
            "balances": [
                {
                    "chainId": 8453,
                    "chainName": "Base",
                    "usdcBalance": "12.340000",
                    "usdcBalanceRaw": "12340000",
                },
                {
                    "chainId": 999999,
                    "chainName": "Missing RPC Chain",
                    "usdcBalance": "5.000000",
                    "usdcBalanceRaw": "5000000",
                },
            ],
            "totalUsdcUsd": "17.340000",
        }

    def list_instruments(self) -> object:
        return SimpleNamespace(data=(execution_instrument(),))

    def metrics_bulk(
        self,
        instrument_ids: tuple[str, ...],
        days: int,
    ) -> list[dict[str, object]]:
        return [
            {"instrumentId": instrument_id, "metrics": []}
            for instrument_id in instrument_ids
        ]

    def instrument_analysis(self, instrument_id: str) -> dict[str, object]:
        return {}

    def build_buy(self, body: dict[str, object]) -> dict[str, Any]:
        self.build_buy_bodies.append(body)
        return {
            "transactions": [
                {
                    "to": "0x0000000000000000000000000000000000000002",
                    "data": "0xapprove",
                    "value": 0,
                    "chainId": 8453,
                    "type": "approve",
                },
                {
                    "to": "0x0000000000000000000000000000000000000003",
                    "data": "0xbuy",
                    "value": 0,
                    "chainId": 8453,
                    "type": "deposit",
                },
            ]
        }


class RebalanceOneTxClient:
    instances: list["RebalanceOneTxClient"] = []
    calls: list[tuple[str, dict[str, object]]] = []

    def __init__(self, config: object) -> None:
        self.config = config
        self.instances.append(self)

    def __enter__(self) -> "RebalanceOneTxClient":
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def list_instruments(self) -> object:
        return SimpleNamespace(
            data=(
                instrument(
                    instrumentId="vault-a",
                    protocol="aave",
                    chainId=8453,
                    tokenSymbol="USDC",
                    tvl=10_000_000,
                    rewardDependence=0.1,
                    curator="curator-a",
                ),
                instrument(
                    instrumentId="vault-b",
                    protocol="aave",
                    chainId=8453,
                    tokenSymbol="USDC",
                    tvl=10_000_000,
                    rewardDependence=0.1,
                    curator="curator-a",
                ),
            )
        )

    def metrics_bulk(
        self,
        instrument_ids: tuple[str, ...],
        days: int,
    ) -> list[dict[str, object]]:
        return [
            {"instrumentId": instrument_id, "metrics": []}
            for instrument_id in instrument_ids
        ]

    def instrument_analysis(self, instrument_id: str) -> dict[str, object]:
        return {}

    def build_sell(self, body: dict[str, object]) -> dict[str, Any]:
        self.calls.append(("sell", body))
        return {
            "transactions": [
                {
                    "to": "0x0000000000000000000000000000000000000002",
                    "data": "0xsell",
                    "value": 0,
                    "chainId": 8453,
                }
            ]
        }

    def build_buy(self, body: dict[str, object]) -> dict[str, Any]:
        self.calls.append(("buy", body))
        return {
            "transactions": [
                {
                    "to": "0x0000000000000000000000000000000000000003",
                    "data": "0xbuy",
                    "value": 0,
                    "chainId": 8453,
                    "type": "deposit",
                }
            ]
        }


class WithdrawOneTxClient:
    instances: list["WithdrawOneTxClient"] = []
    calls: list[dict[str, object]] = []

    def __init__(self, config: object) -> None:
        self.config = config
        self.instances.append(self)

    def __enter__(self) -> "WithdrawOneTxClient":
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def build_sell(self, body: dict[str, object]) -> dict[str, Any]:
        self.calls.append(body)
        return {
            "expectedUsdc": "79.50",
            "transactions": [
                {
                    "to": "0x0000000000000000000000000000000000000002",
                    "data": "0xsell",
                    "value": 0,
                    "chainId": 8453,
                }
            ],
        }


def install_execution_surface_mocks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    idempotency_store_path: Path | None = None,
    fail_at: int | None = None,
) -> ExecutionSignerSpy:
    set_execution_config(
        monkeypatch,
        idempotency_store_path=idempotency_store_path,
    )
    signer = ExecutionSignerSpy(fail_at=fail_at)
    ExecutionOneTxClient.instances = []
    ExecutionOneTxClient.build_buy_bodies = []
    monkeypatch.setattr(cli, "OneTxClient", ExecutionOneTxClient)
    monkeypatch.setattr(cli, "signer_from_config", lambda _config: signer)
    return signer


def install_rebalance_surface_mocks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    idempotency_store_path: Path | None = None,
) -> ExecutionSignerSpy:
    set_execution_config(
        monkeypatch,
        idempotency_store_path=idempotency_store_path,
    )
    signer = ExecutionSignerSpy()
    RebalanceOneTxClient.instances = []
    RebalanceOneTxClient.calls = []
    monkeypatch.setattr(cli, "OneTxClient", RebalanceOneTxClient)
    monkeypatch.setattr(cli, "signer_from_config", lambda _config: signer)
    return signer


def install_withdraw_surface_mocks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    idempotency_store_path: Path | None = None,
) -> ExecutionSignerSpy:
    set_execution_config(
        monkeypatch,
        idempotency_store_path=idempotency_store_path,
    )
    signer = ExecutionSignerSpy()
    WithdrawOneTxClient.instances = []
    WithdrawOneTxClient.calls = []
    monkeypatch.setattr(cli, "OneTxClient", WithdrawOneTxClient)
    monkeypatch.setattr(cli, "signer_from_config", lambda _config: signer)
    return signer


def execution_instrument() -> dict[str, Any]:
    return instrument(
        instrumentId="base-aave-usdc",
        protocol="aave",
        chainId=8453,
        tokenSymbol="USDC",
        currentApy=0.04,
        tvl=10_000_000,
        rewardDependence=0.1,
        liquidity=5_000_000,
        curator="curator-a",
    )


def execution_policy() -> dict[str, Any]:
    return {
        "version": 1,
        "wallet": {"mode": "self-custody", "signer": "local-eoa"},
        "allowed": {
            "protocols": None,
            "chains": None,
            "assets": ["USDC"],
            "curators": None,
        },
        "caps": {
            "max_weight_per_instrument": 1,
            "max_weight_per_protocol": 1,
            "max_weight_per_curator": 1,
            "max_weight_per_chain": 1,
            "min_instrument_tvl_usd": 1,
            "max_reward_dependence": 1,
        },
        "gates": {
            "new_instrument_needs_approval": True,
            "autonomous_rebalance": False,
            "max_deploy_per_cycle_usd": 1_000_000,
        },
    }


def write_execution_files(tmp_path: Path) -> tuple[Path, Path]:
    allocation_path = tmp_path / "allocation.json"
    allocation_path.write_text(
        json.dumps(
            {
                "legs": [
                    {
                        "instrument_id": "base-aave-usdc",
                        "weight": 1.0,
                        "usd": 100.0,
                    }
                ],
                "total_usd": 100.0,
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(json.dumps(execution_policy()), encoding="utf-8")
    return allocation_path, policy_path


def write_rebalance_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    current_path = tmp_path / "positions.json"
    current_path.write_text(
        json.dumps(
            {
                "address": "0x0000000000000000000000000000000000000001",
                "holdings": [
                    {
                        "instrument_id": "vault-a",
                        "protocol": "aave",
                        "chain_id": 8453,
                        "symbol": "USDC",
                        "balance": "80.000000",
                        "balance_raw": "80000000",
                        "decimals": 6,
                        "usd_value": 80.0,
                        "share_balance": "80.000000",
                        "share_balance_raw": "80000000",
                        "share_decimals": 6,
                        "yield_token_symbol": "aUSDC",
                        "yield_token_address": (
                            "0x0000000000000000000000000000000000000002"
                        ),
                    },
                    {
                        "instrument_id": "vault-b",
                        "protocol": "aave",
                        "chain_id": 8453,
                        "symbol": "USDC",
                        "balance": "20.000000",
                        "balance_raw": "20000000",
                        "decimals": 6,
                        "usd_value": 20.0,
                        "share_balance": "20.000000",
                        "share_balance_raw": "20000000",
                        "share_decimals": 6,
                        "yield_token_symbol": "aUSDC",
                        "yield_token_address": (
                            "0x0000000000000000000000000000000000000003"
                        ),
                    },
                ],
                "idle_balances": [
                    {
                        "chain_id": 8453,
                        "chain_name": "Base",
                        "usdc_balance": "0.000000",
                        "usdc_balance_raw": "0",
                        "usd_value": 0.0,
                    }
                ],
                "total_position_usd": 100.0,
                "total_idle_usdc": 0.0,
                "total_usd": 100.0,
                "total_usdc_usd": "0.000000",
            }
        ),
        encoding="utf-8",
    )
    target_path = tmp_path / "allocation.json"
    target_path.write_text(
        json.dumps(
            {
                "legs": [
                    {"instrument_id": "vault-a", "weight": 0.5, "usd": 50.0},
                    {"instrument_id": "vault-b", "weight": 0.5, "usd": 50.0},
                ],
                "total_usd": 100.0,
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(json.dumps(execution_policy()), encoding="utf-8")
    return current_path, target_path, policy_path


def test_list_vaults_returns_json_array_with_summaries(
    monkeypatch: pytest.MonkeyPatch,
    fixture_instruments: list[dict[str, Any]],
) -> None:
    set_read_only_env(monkeypatch)
    install_mock_onetx_client(monkeypatch, fixture_instruments)

    result = runner.invoke(cli.app, ["list-vaults"])

    assert result.exit_code == 0
    assert result.stderr == ""
    payload = parse_single_stdout_value(result.stdout)
    assert isinstance(payload, list)
    assert payload[0] == {
        "instrument_id": "base-aave-usdc",
        "protocol": "aave",
        "chain_id": 8453,
        "asset": "USDC",
        "apy": 0.04,
        "tvl_usd": 10_000_000.0,
        "score": pytest.approx(payload[0]["score"]),
        "risk_metrics": payload[0]["risk_metrics"],
    }
    assert set(payload[0]) == {
        "instrument_id",
        "protocol",
        "chain_id",
        "asset",
        "apy",
        "tvl_usd",
        "score",
        "risk_metrics",
    }
    assert "history_days" in payload[0]["risk_metrics"]


def test_list_vaults_filters_and_sorts(
    monkeypatch: pytest.MonkeyPatch,
    fixture_instruments: list[dict[str, Any]],
) -> None:
    set_read_only_env(monkeypatch)
    install_mock_onetx_client(monkeypatch, fixture_instruments)

    result = runner.invoke(
        cli.app,
        [
            "list-vaults",
            "--asset",
            "USDC",
            "--sort",
            "apy",
        ],
    )

    assert result.exit_code == 0
    payload = parse_single_stdout_value(result.stdout)
    assert [item["instrument_id"] for item in payload] == [
        "arbitrum-morpho-usdc",
        "base-aave-usdc",
    ]
    assert {item["asset"] for item in payload} == {"USDC"}

    chain_result = runner.invoke(
        cli.app,
        ["list-vaults", "--chain", "10", "--protocol", "compound"],
    )
    chain_payload = parse_single_stdout_value(chain_result.stdout)
    assert [item["instrument_id"] for item in chain_payload] == [
        "optimism-compound-dai"
    ]


def test_list_vaults_without_narrowing_returns_all_protocols_and_chains(
    monkeypatch: pytest.MonkeyPatch,
    fixture_instruments: list[dict[str, Any]],
) -> None:
    set_read_only_env(monkeypatch)
    install_mock_onetx_client(monkeypatch, fixture_instruments)

    result = runner.invoke(cli.app, ["list-vaults"])

    assert result.exit_code == 0
    payload = parse_single_stdout_value(result.stdout)
    assert {item["protocol"] for item in payload} == {"aave", "morpho", "compound"}
    assert {item["chain_id"] for item in payload} == {8453, 42161, 10}


def test_list_vaults_sort_score(
    monkeypatch: pytest.MonkeyPatch,
    fixture_instruments: list[dict[str, Any]],
) -> None:
    set_read_only_env(monkeypatch)
    install_mock_onetx_client(monkeypatch, fixture_instruments)

    result = runner.invoke(cli.app, ["list-vaults", "--sort", "score"])

    assert result.exit_code == 0
    payload = parse_single_stdout_value(result.stdout)
    scores = [item["score"] for item in payload]
    assert scores == sorted(scores, reverse=True)


def test_score_vault_outputs_schema_valid_factor_breakdown(
    monkeypatch: pytest.MonkeyPatch,
    fixture_instruments: list[dict[str, Any]],
) -> None:
    set_read_only_env(monkeypatch)
    install_mock_onetx_client(monkeypatch, fixture_instruments)

    result = runner.invoke(
        cli.app,
        ["score-vault", "--instrument-id", "base-aave-usdc"],
    )

    assert result.exit_code == 0
    payload = parse_single_stdout_object(result.stdout)
    assert validate(payload, "vault-score") == payload
    assert payload["instrument_id"] == "base-aave-usdc"
    assert payload["factors"]["tvl"] == {
        "raw_input": 10_000_000.0,
        "normalized_value": pytest.approx(
            payload["factors"]["tvl"]["normalized_value"]
        ),
        "weight": 1.25,
        "unknown": False,
    }
    assert set(payload["factors"]["reward_dependence"]) == {
        "raw_input",
        "normalized_value",
        "weight",
        "unknown",
    }


def test_build_allocation_outputs_schema_valid_policy_passing_allocation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    compliant_instruments: list[dict[str, Any]],
) -> None:
    set_read_only_env(monkeypatch)
    install_mock_onetx_client(monkeypatch, compliant_instruments)

    result = runner.invoke(
        cli.app,
        ["build-allocation", "--risk", "balanced", "--amount", "10000"],
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    allocation = parse_single_stdout_object(result.stdout)
    assert validate(allocation, "allocation") == allocation
    assert allocation["total_usd"] == 10_000
    assert allocation["metadata"]["policy_ok"] is True
    assert allocation["metadata"]["policy_violations"] == []

    allocation_path = tmp_path / "allocation.json"
    allocation_path.write_text(result.stdout, encoding="utf-8")
    check_result = runner.invoke(
        cli.app,
        ["check-policy", "--allocation", str(allocation_path)],
    )

    assert check_result.exit_code == 0
    policy_result = parse_single_stdout_object(check_result.stdout)
    assert policy_result == {"ok": True, "violations": []}


def test_build_allocation_accepts_strategy_and_params(
    monkeypatch: pytest.MonkeyPatch,
    compliant_instruments: list[dict[str, Any]],
) -> None:
    set_read_only_env(monkeypatch)
    install_mock_onetx_client(monkeypatch, compliant_instruments)

    result = runner.invoke(
        cli.app,
        [
            "build-allocation",
            "--amount",
            "10000",
            "--strategy",
            "equal_weight",
            "--strategy-param",
            "top_n=2",
        ],
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    allocation = parse_single_stdout_object(result.stdout)
    assert validate(allocation, "allocation") == allocation
    assert allocation["metadata"]["strategy"] == "equal_weight"


def test_build_allocation_rejects_unknown_strategy(
    monkeypatch: pytest.MonkeyPatch,
    compliant_instruments: list[dict[str, Any]],
) -> None:
    set_read_only_env(monkeypatch)
    install_mock_onetx_client(monkeypatch, compliant_instruments)

    result = runner.invoke(
        cli.app,
        ["build-allocation", "--amount", "10000", "--strategy", "bogus"],
    )

    assert result.exit_code != 0
    error = parse_single_stdout_value(result.stderr)
    assert "unsupported strategy" in json.dumps(error)


def test_build_allocation_from_strategy_spec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    compliant_instruments: list[dict[str, Any]],
) -> None:
    set_read_only_env(monkeypatch)
    install_mock_onetx_client(monkeypatch, compliant_instruments)

    spec_path = tmp_path / "spec.json"
    spec_path.write_text(
        json.dumps(
            {
                "amount_usd": 10000,
                "strategy": "equal_weight",
                "params": {"top_n": 2},
                "selection": {"max_positions": 3},
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(cli.app, ["build-allocation", "--spec", str(spec_path)])

    assert result.exit_code == 0
    assert result.stderr == ""
    allocation = parse_single_stdout_object(result.stdout)
    assert validate(allocation, "allocation") == allocation
    # equal_weight top_n=2 selects exactly 2 instruments at 0.5 each, but the
    # default 0.30 instrument cap lets 2 legs hold at most 0.60. The remainder
    # is reported as unallocatable rather than silently spread onto instruments
    # the strategy never selected.
    assert len(allocation["legs"]) == 2
    assert allocation["total_usd"] == 6_000
    assert allocation["metadata"]["strategy"] == "equal_weight"
    assert any(
        warning.startswith("caps_binding:unallocatable_weight")
        for warning in allocation["metadata"]["warnings"]
    )


def test_build_allocation_from_weights_spec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    compliant_instruments: list[dict[str, Any]],
) -> None:
    set_read_only_env(monkeypatch)
    install_mock_onetx_client(monkeypatch, compliant_instruments)

    instrument_id = compliant_instruments[0]["instrumentId"]
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(
        json.dumps({"weights": {instrument_id: 1.0}}),
        encoding="utf-8",
    )

    result = runner.invoke(
        cli.app,
        ["build-allocation", "--spec", str(spec_path), "--amount", "5000"],
    )

    assert result.exit_code == 0
    allocation = parse_single_stdout_object(result.stdout)
    assert validate(allocation, "allocation") == allocation
    assert allocation["total_usd"] == 5_000
    assert {leg["instrument_id"] for leg in allocation["legs"]} == {instrument_id}


def test_build_allocation_requires_amount_somewhere(
    monkeypatch: pytest.MonkeyPatch,
    compliant_instruments: list[dict[str, Any]],
) -> None:
    set_read_only_env(monkeypatch)
    install_mock_onetx_client(monkeypatch, compliant_instruments)

    result = runner.invoke(cli.app, ["build-allocation"])

    assert result.exit_code != 0
    error = parse_single_stdout_value(result.stderr)
    assert "amount required" in json.dumps(error)


def test_backtest_command_reports_portfolio_and_benchmark(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from open_allocator.core.types import Vault

    def _vaults(*, enrich: bool = False) -> list[Vault]:
        return [
            Vault(
                instrument_id="a",
                protocol="p",
                chain_id=1,
                asset="USDC",
                apy=5.0,
                tvl_usd=2_000_000,
                apy_series=(5.0, 5.2, 4.9, 5.1),
            ),
            Vault(
                instrument_id="b",
                protocol="q",
                chain_id=1,
                asset="USDC",
                apy=4.0,
                tvl_usd=1_000_000,
                apy_series=(4.0, 3.9, 4.1, 4.0),
            ),
        ]

    monkeypatch.setattr(cli, "_discover_vaults", _vaults)

    allocation_path = tmp_path / "allocation.json"
    allocation_path.write_text(
        json.dumps(
            {
                "legs": [
                    {"instrument_id": "a", "weight": 0.6, "usd": 600.0},
                    {"instrument_id": "b", "weight": 0.4, "usd": 400.0},
                ],
                "total_usd": 1000.0,
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(cli.app, ["backtest", "--allocation", str(allocation_path)])

    assert result.exit_code == 0
    assert result.stderr == ""
    report = parse_single_stdout_object(result.stdout)
    assert report["days"] == 4
    assert report["label"] == "descriptive-not-predictive"
    assert "yield-path only" in report["caveat"]
    assert report["portfolio"]["days"] == 4
    assert report["benchmark"] is not None


def test_screen_command_returns_kept_and_dropped(
    monkeypatch: pytest.MonkeyPatch,
    compliant_instruments: list[dict[str, Any]],
) -> None:
    set_read_only_env(monkeypatch)
    install_mock_onetx_client(monkeypatch, compliant_instruments)

    result = runner.invoke(
        cli.app,
        ["screen", "--min-history-days", "9999"],
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    payload = parse_single_stdout_object(result.stdout)
    assert payload["label"] == "advisory-not-policy"
    assert payload["criteria"]["min_history_days"] == 9999
    # An impossible history requirement drops the whole universe.
    assert payload["kept"] == []
    assert payload["dropped"]
    assert payload["dropped"][0]["rule"] == "min_history_days"


def test_check_policy_on_violating_allocation_returns_violations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    compliant_instruments: list[dict[str, Any]],
) -> None:
    set_read_only_env(monkeypatch)
    install_mock_onetx_client(monkeypatch, compliant_instruments)
    allocation_path = tmp_path / "allocation.json"
    allocation_path.write_text(
        json.dumps(
            {
                "legs": [
                    {
                        "instrument_id": "base-aave-usdc",
                        "weight": 1.0,
                        "usd": 100.0,
                    }
                ],
                "total_usd": 100.0,
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        cli.app,
        ["check-policy", "--allocation", str(allocation_path)],
    )

    assert result.exit_code == 0
    payload = parse_single_stdout_object(result.stdout)
    assert payload["ok"] is False
    assert payload["violations"]
    assert "max_weight_per_instrument" in {
        violation["rule"] for violation in payload["violations"]
    }


def test_simulate_outputs_descriptive_scorecard_from_allocation_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    compliant_instruments: list[dict[str, Any]],
) -> None:
    set_read_only_env(monkeypatch)
    install_mock_onetx_client(monkeypatch, compliant_instruments)
    allocation_path = tmp_path / "allocation.json"
    allocation_path.write_text(
        json.dumps(
            {
                "legs": [
                    {
                        "instrument_id": "base-aave-usdc",
                        "weight": 1.0,
                        "usd": 10_000.0,
                    }
                ],
                "total_usd": 10_000.0,
                "metadata": {"risk": "balanced"},
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        cli.app,
        ["simulate", "--allocation", str(allocation_path), "--benchmark", "USD_INDEX"],
    )

    assert result.exit_code == 0
    payload = parse_single_stdout_object(result.stdout)
    assert payload["label"] == "descriptive-not-predictive"
    assert payload["simulation"]["headline"] == "descriptive backtest"
    assert payload["simulation"]["benchmark"]["label"] == "USD_INDEX"


def test_read_only_commands_need_no_wallet_private_key_or_rpc(
    monkeypatch: pytest.MonkeyPatch,
    fixture_instruments: list[dict[str, Any]],
) -> None:
    set_read_only_env(monkeypatch)
    mock_client = install_mock_onetx_client(monkeypatch, fixture_instruments)

    list_result = runner.invoke(cli.app, ["list-vaults"])
    score_result = runner.invoke(
        cli.app,
        ["score-vault", "--instrument-id", "base-aave-usdc"],
    )

    assert list_result.exit_code == 0
    assert score_result.exit_code == 0
    assert "ONE_TX_PRIVATE_KEY" not in os.environ
    assert "SIGNER_MODE" not in os.environ
    assert not any(name.startswith("RPC_URL_") for name in os.environ)
    assert all(not hasattr(config, "private_key") for config in mock_client.configs)


def test_phase2_allocation_commands_need_no_signer_import_or_private_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    compliant_instruments: list[dict[str, Any]],
) -> None:
    signer_loaded_before = "eth_account" in sys.modules
    set_read_only_env(monkeypatch)
    mock_client = install_mock_onetx_client(monkeypatch, compliant_instruments)

    build_result = runner.invoke(
        cli.app,
        ["build-allocation", "--risk", "balanced", "--amount", "10000"],
    )
    allocation_path = tmp_path / "allocation.json"
    allocation_path.write_text(build_result.stdout, encoding="utf-8")
    check_result = runner.invoke(
        cli.app,
        ["check-policy", "--allocation", str(allocation_path)],
    )
    simulate_result = runner.invoke(
        cli.app,
        ["simulate", "--allocation", str(allocation_path)],
    )

    assert build_result.exit_code == 0
    assert check_result.exit_code == 0
    assert simulate_result.exit_code == 0
    assert "ONE_TX_PRIVATE_KEY" not in os.environ
    assert "SIGNER_MODE" not in os.environ
    assert not any(name.startswith("RPC_URL_") for name in os.environ)
    assert all(not hasattr(config, "private_key") for config in mock_client.configs)
    if not signer_loaded_before:
        assert "eth_account" not in sys.modules


def test_wallet_status_includes_balances_and_not_executable_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer = install_execution_surface_mocks(monkeypatch)

    def gas_status(_address: str, chain_id: int, _config: object) -> JsonObject:
        if chain_id == 8453:
            return {
                "rpc_available": True,
                "rpc_executable": True,
                "native_gas_balance_wei": 10,
                "native_gas_required_wei": 1,
                "native_gas_available": True,
                "executable": True,
                "not_executable": False,
                "not_executable_reasons": [],
            }
        return {
            "rpc_available": False,
            "rpc_executable": False,
            "native_gas_balance_wei": None,
            "native_gas_required_wei": 1,
            "native_gas_available": False,
            "executable": False,
            "not_executable": True,
            "not_executable_reasons": ["missing_rpc"],
        }

    monkeypatch.setattr(cli, "_native_gas_status", gas_status)

    result = runner.invoke(cli.app, ["wallet-status"])

    assert result.exit_code == 0
    assert result.stderr == ""
    payload = parse_single_stdout_object(result.stdout)
    assert payload["address"] == signer.address()
    assert payload["total_usdc_usd"] == "17.340000"
    assert payload["balances"][0]["chain_id"] == 8453
    assert payload["balances"][0]["usdc_balance"] == "12.340000"
    assert payload["balances"][0]["not_executable"] is False
    assert payload["balances"][1]["chain_id"] == 999999
    assert payload["balances"][1]["not_executable"] is True
    assert payload["balances"][1]["not_executable_reasons"] == ["missing_rpc"]
    assert signer.sent == []


def test_positions_command_outputs_holdings_and_idle_balances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_read_only_env(monkeypatch)
    address = "0x0000000000000000000000000000000000000001"

    class PositionsOneTxClient:
        configs: list[object] = []

        def __init__(self, config: object) -> None:
            self.config = config
            self.configs.append(config)

        def __enter__(self) -> "PositionsOneTxClient":
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def balances(self, requested_address: str) -> dict[str, Any]:
            assert requested_address == address
            return {
                "address": requested_address,
                "balances": [
                    {
                        "chainId": 8453,
                        "chainName": "Base",
                        "usdcBalance": "5.000000",
                        "usdcBalanceRaw": "5000000",
                    }
                ],
                "totalUsdcUsd": "5.000000",
            }

        def positions(self, body: dict[str, object]) -> dict[str, Any]:
            assert body == {"address": address, "chainId": 8453}
            return {
                "address": address,
                "chainId": 8453,
                "usdcBalance": "5.000000",
                "positions": [
                    {
                        "instrumentId": "base-aave-usdc",
                        "protocol": "aave",
                        "symbol": "USDC",
                        "yieldTokenSymbol": "aUSDC",
                        "balance": "10.000000",
                        "balanceRaw": "10000000",
                        "decimals": 6,
                        "shareBalance": "9.999900",
                        "shareBalanceRaw": "9999900",
                        "shareDecimals": 6,
                        "yieldTokenAddress": (
                            "0x0000000000000000000000000000000000000002"
                        ),
                        "chainId": 8453,
                    }
                ],
            }

    monkeypatch.setattr(cli, "OneTxClient", PositionsOneTxClient)

    result = runner.invoke(cli.app, ["positions", "--address", address])

    assert result.exit_code == 0
    assert result.stderr == ""
    payload = parse_single_stdout_object(result.stdout)
    assert payload["address"] == address
    assert payload["total_position_usd"] == 10
    assert payload["total_idle_usdc"] == 5
    assert payload["holdings"][0]["instrument_id"] == "base-aave-usdc"
    assert payload["holdings"][0]["share_balance"] == "9.999900"
    assert payload["holdings"][0]["share_balance_raw"] == "9999900"
    assert payload["idle_balances"][0]["usdc_balance"] == "5.000000"
    assert not any(
        hasattr(config, "private_key") for config in PositionsOneTxClient.configs
    )


def test_build_tx_emits_schema_valid_plan_and_does_not_send(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_execution_surface_mocks(monkeypatch)
    allocation_path, policy_path = write_execution_files(tmp_path)

    result = runner.invoke(
        cli.app,
        [
            "build-tx",
            "--allocation",
            str(allocation_path),
            "--policy",
            str(policy_path),
        ],
    )

    assert result.exit_code == 0
    payload = parse_single_stdout_object(result.stdout)
    assert validate(payload, "tx-plan") == payload
    assert [step["kind"] for step in payload["steps"]] == ["approve", "buy"]
    assert signer.sent == []
    assert ExecutionOneTxClient.build_buy_bodies == [
        {
            "userAddress": "0x0000000000000000000000000000000000000001",
            "instrumentId": "base-aave-usdc",
            "amountUsdc": "100",
            "sourceChainId": 8453,
        }
    ]


def test_execute_without_confirm_announces_plan_and_does_not_broadcast(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_execution_surface_mocks(monkeypatch)
    allocation_path, policy_path = write_execution_files(tmp_path)

    result = runner.invoke(
        cli.app,
        [
            "execute",
            "--allocation",
            str(allocation_path),
            "--policy",
            str(policy_path),
        ],
    )

    assert result.exit_code == 0
    payload = parse_single_stdout_object(result.stdout)
    assert payload["status"] == "planned"
    assert payload["policy_result"] == {"ok": True, "violations": []}
    assert [step["data"] for step in payload["plan"]["steps"]] == [
        "0xapprove",
        "0xbuy",
    ]
    assert payload["receipts"] == []
    assert signer.sent == []


def test_execute_with_confirm_broadcasts_and_emits_execution_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_execution_surface_mocks(monkeypatch)
    allocation_path, policy_path = write_execution_files(tmp_path)

    result = runner.invoke(
        cli.app,
        [
            "execute",
            "--allocation",
            str(allocation_path),
            "--policy",
            str(policy_path),
            "--confirm",
        ],
    )

    assert result.exit_code == 0
    payload = parse_single_stdout_object(result.stdout)
    assert payload["status"] == "success"
    assert [step["status"] for step in payload["steps"]] == ["sent", "sent"]
    assert [receipt["transaction_hash"] for receipt in payload["receipts"]] == [
        f"0x{1:064x}",
        f"0x{2:064x}",
    ]
    assert [getattr(sent[0], "data") for sent in signer.sent] == ["0xapprove", "0xbuy"]
    assert [sent[1] for sent in signer.sent] == ["rpc://base", "rpc://base"]


def test_execute_confirmed_uses_durable_idempotency_store_on_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_execution_surface_mocks(
        monkeypatch,
        idempotency_store_path=tmp_path / "idempotency.json",
        fail_at=1,
    )
    allocation_path, policy_path = write_execution_files(tmp_path)
    args = [
        "execute",
        "--allocation",
        str(allocation_path),
        "--policy",
        str(policy_path),
        "--confirm",
    ]

    failed_result = runner.invoke(cli.app, args)
    signer.fail_at = None
    retry_result = runner.invoke(cli.app, args)

    assert failed_result.exit_code == 1
    assert json.loads(failed_result.stderr) == {"error": "transaction broadcast failed"}
    assert retry_result.exit_code == 0
    payload = parse_single_stdout_object(retry_result.stdout)
    assert [step["status"] for step in payload["steps"]] == ["skipped", "sent"]
    assert [getattr(sent[0], "data") for sent in signer.sent] == ["0xapprove", "0xbuy"]


def test_build_tx_and_execute_dry_run_and_confirmed_share_plan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_execution_surface_mocks(monkeypatch)
    allocation_path, policy_path = write_execution_files(tmp_path)
    common_args = ["--allocation", str(allocation_path), "--policy", str(policy_path)]

    build_result = runner.invoke(cli.app, ["build-tx", *common_args])
    dry_run_result = runner.invoke(cli.app, ["execute", *common_args])
    confirmed_result = runner.invoke(cli.app, ["execute", *common_args, "--confirm"])

    assert build_result.exit_code == 0
    assert dry_run_result.exit_code == 0
    assert confirmed_result.exit_code == 0
    build_plan = parse_single_stdout_object(build_result.stdout)
    dry_run_plan = parse_single_stdout_object(dry_run_result.stdout)["plan"]
    confirmed_plan = parse_single_stdout_object(confirmed_result.stdout)["plan"]
    assert build_plan == dry_run_plan == confirmed_plan
    assert len(signer.sent) == 2


@pytest.mark.parametrize("flag", ["--unsafe", "--autonomous"])
def test_execute_unsafe_and_autonomous_do_not_bypass_confirm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    flag: str,
) -> None:
    signer = install_execution_surface_mocks(monkeypatch)
    allocation_path, policy_path = write_execution_files(tmp_path)

    result = runner.invoke(
        cli.app,
        [
            "execute",
            "--allocation",
            str(allocation_path),
            "--policy",
            str(policy_path),
            flag,
        ],
    )

    assert result.exit_code == 0
    payload = parse_single_stdout_object(result.stdout)
    assert payload["status"] == "planned"
    assert signer.sent == []


def test_rebalance_without_confirm_plans_deltas_and_does_not_broadcast(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_rebalance_surface_mocks(monkeypatch)
    current_path, target_path, policy_path = write_rebalance_files(tmp_path)

    result = runner.invoke(
        cli.app,
        [
            "rebalance",
            "--current",
            str(current_path),
            "--target",
            str(target_path),
            "--policy",
            str(policy_path),
        ],
    )

    assert result.exit_code == 0
    payload = parse_single_stdout_object(result.stdout)
    assert payload["status"] == "planned"
    trades = payload["rebalance_plan"]["trades"]
    assert [(trade["action"], trade["instrument_id"]) for trade in trades] == [
        ("sell", "vault-a"),
        ("buy", "vault-b"),
    ]
    assert [step["kind"] for step in payload["plan"]["steps"]] == ["sell", "buy"]
    assert [call[0] for call in RebalanceOneTxClient.calls] == ["sell", "buy"]
    assert RebalanceOneTxClient.calls[0][1]["yieldTokenAmount"] == "30"
    assert signer.sent == []


def test_rebalance_with_confirm_broadcasts_sell_before_buy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_rebalance_surface_mocks(monkeypatch)
    current_path, target_path, policy_path = write_rebalance_files(tmp_path)

    result = runner.invoke(
        cli.app,
        [
            "rebalance",
            "--current",
            str(current_path),
            "--target",
            str(target_path),
            "--policy",
            str(policy_path),
            "--confirm",
        ],
    )

    assert result.exit_code == 0
    payload = parse_single_stdout_object(result.stdout)
    assert payload["status"] == "success"
    assert [step["status"] for step in payload["steps"]] == ["sent", "sent"]
    assert [getattr(sent[0], "data") for sent in signer.sent] == ["0xsell", "0xbuy"]
    assert [call[0] for call in RebalanceOneTxClient.calls] == ["sell", "buy"]


def test_rebalance_autonomous_false_requires_confirm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_rebalance_surface_mocks(monkeypatch)
    current_path, target_path, policy_path = write_rebalance_files(tmp_path)

    result = runner.invoke(
        cli.app,
        [
            "rebalance",
            "--current",
            str(current_path),
            "--target",
            str(target_path),
            "--policy",
            str(policy_path),
            "--autonomous",
        ],
    )

    assert result.exit_code == 1
    assert "autonomous_rebalance=true" in json.loads(result.stderr)["error"]
    assert signer.sent == []
    assert RebalanceOneTxClient.calls == []


def test_withdraw_with_confirm_uses_positions_output_and_sells_shares(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_withdraw_surface_mocks(monkeypatch)
    current_path, _target_path, policy_path = write_rebalance_files(tmp_path)

    result = runner.invoke(
        cli.app,
        [
            "withdraw",
            "--position",
            "vault-a",
            "--positions",
            str(current_path),
            "--policy",
            str(policy_path),
            "--confirm",
        ],
    )

    assert result.exit_code == 0
    payload = parse_single_stdout_object(result.stdout)
    assert payload["status"] == "success"
    assert payload["withdraw_plan"]["instrument_id"] == "vault-a"
    assert payload["withdraw_plan"]["yield_token_amount"] == "80.000000"
    assert payload["sell"]["expected_usdc"] == "79.50"
    assert WithdrawOneTxClient.calls == [
        {
            "userAddress": "0x0000000000000000000000000000000000000001",
            "instrumentId": "vault-a",
            "yieldTokenAmount": "80.000000",
        }
    ]
    assert [getattr(sent[0], "data") for sent in signer.sent] == ["0xsell"]


@pytest.mark.parametrize("flag", ["--unsafe", "--autonomous"])
def test_withdraw_unsafe_and_autonomous_do_not_bypass_confirm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    flag: str,
) -> None:
    signer = install_withdraw_surface_mocks(monkeypatch)
    current_path, _target_path, policy_path = write_rebalance_files(tmp_path)

    result = runner.invoke(
        cli.app,
        [
            "withdraw",
            "--position",
            "vault-a",
            "--positions",
            str(current_path),
            "--policy",
            str(policy_path),
            flag,
        ],
    )

    assert result.exit_code == 0
    assert parse_single_stdout_object(result.stdout) == {
        "status": "plan_required",
        "command": "withdraw",
        "requires": "--confirm or explicit --unsafe/--autonomous",
    }
    assert signer.sent == []
    assert WithdrawOneTxClient.calls == []


def test_help_lists_every_planned_command() -> None:
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    for command in COMMANDS:
        assert command in result.stdout


def test_json_command_routes_errors_to_stderr() -> None:
    failing_app = typer.Typer()

    @failing_app.command("ok")
    @json_command
    def ok() -> JsonObject:
        return {"status": "ok"}

    @failing_app.command("fail")
    @json_command
    def fail() -> JsonObject:
        raise RuntimeError("boom")

    result = runner.invoke(failing_app, ["fail"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr) == {"error": "boom"}


@pytest.mark.parametrize(
    ("command", "executor_name"),
    [
        ("withdraw", "_withdraw_executor"),
    ],
)
def test_execution_commands_without_confirmation_do_not_call_executor(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    executor_name: str,
) -> None:
    calls: list[str] = []

    def spy() -> JsonObject:
        calls.append(command)
        return {"status": "spy_called"}

    monkeypatch.setattr(cli, executor_name, spy)

    result = runner.invoke(cli.app, [command])

    assert result.exit_code == 0
    assert calls == []
    assert parse_single_stdout_object(result.stdout) == {
        "status": "plan_required",
        "command": command,
        "requires": "--confirm or explicit --unsafe/--autonomous",
    }




def set_paymaster_config(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    config = SimpleNamespace(
        onetx_api_url="http://localhost:3001/api/v1",
        onetx_api_key="test-api-key",
        account="safe",
        submission="erc4337-paymaster",
        owner_signer="local",
        paymaster_provider="pimlico",
        pimlico_api_key="pim_test",
        paymaster_supported_chain_ids=None,
        paymaster_usdc_address=None,
        _rpc_overrides={8453: "rpc://base", 999999: "rpc://missing"},
        _usdc_overrides={},
        idempotency_store_path=None,
    )
    monkeypatch.setattr(cli, "AllocatorConfig", lambda: config)
    return config


def test_wallet_status_does_not_demand_native_gas_when_gas_is_paid_in_usdc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Safe holds no native token anywhere — that is the feature, not a fault.

    A native-balance verdict marked every chain not executable while the gasless
    path was working on mainnet, which tells the agent to go fund ETH.
    """
    set_paymaster_config(monkeypatch)
    ExecutionOneTxClient.instances = []
    ExecutionOneTxClient.build_buy_bodies = []
    monkeypatch.setattr(cli, "OneTxClient", ExecutionOneTxClient)
    monkeypatch.setattr(cli, "signer_from_config", lambda _config: ExecutionSignerSpy())
    monkeypatch.setattr(
        cli,
        "_native_gas_status",
        lambda *_args: pytest.fail("native gas must not be checked in paymaster mode"),
    )

    result = runner.invoke(cli.app, ["wallet-status"])

    assert result.exit_code == 0
    base = parse_single_stdout_object(result.stdout)["balances"][0]
    assert base["chain_id"] == 8453
    assert base["gas_mode"] == "usdc_paymaster"
    assert base["executable"] is True
    assert base["not_executable_reasons"] == []
    assert base["native_gas_balance_wei"] is None
    assert base["gas_token_address"] is not None


def test_wallet_status_flags_a_chain_the_paymaster_cannot_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not executable here means "no USDC gas on this chain", not "no ETH"."""
    set_paymaster_config(monkeypatch)
    ExecutionOneTxClient.instances = []
    ExecutionOneTxClient.build_buy_bodies = []
    monkeypatch.setattr(cli, "OneTxClient", ExecutionOneTxClient)
    monkeypatch.setattr(cli, "signer_from_config", lambda _config: ExecutionSignerSpy())

    result = runner.invoke(cli.app, ["wallet-status"])

    assert result.exit_code == 0
    unsupported = parse_single_stdout_object(result.stdout)["balances"][1]
    assert unsupported["chain_id"] == 999999
    assert unsupported["executable"] is False
    assert unsupported["not_executable_reasons"] == ["chain_not_gas_payable"]
