from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from open_allocator.core.checkpoint import (
    allocation_log_totals,
    idempotency_store_from_checkpoint,
    read_allocation_log,
    read_checkpoint,
    reconcile_allocation_log,
    resume_state,
    write_allocation_log_entry,
    write_checkpoint,
)
from open_allocator.core.positions import IdleBalance, PositionHolding, Positions
from open_allocator.core.schema import SchemaValidationError
from open_allocator.core.types import (
    Allocation,
    AllocationLeg,
    Policy,
    PolicyAllowed,
    PolicyCaps,
    PolicyGates,
    PolicyWallet,
    TxStep,
    Vault,
)
from open_allocator.exec.execute import GasCheck, execute_allocation
from open_allocator.exec.signer import Receipt

ADDRESS = "0x0000000000000000000000000000000000000001"


@dataclass
class MockOneTxClient:
    responses: list[dict[str, Any]]
    bodies: list[dict[str, object]] = field(default_factory=list)

    def build_buy(self, body: dict[str, object]) -> dict[str, Any]:
        self.bodies.append(body)
        return self.responses.pop(0)


@dataclass
class MockSigner:
    sent: list[tuple[TxStep, str]] = field(default_factory=list)

    def address(self) -> str:
        return ADDRESS

    def send(self, tx: TxStep, rpc_url: str) -> Receipt:
        self.sent.append((tx, rpc_url))
        index = len(self.sent)
        return Receipt(
            transaction_hash=f"0x{index:064x}",
            block_number=index,
            gas_used=21_000,
            status=1,
            from_address=ADDRESS,
            to_address=tx.to,
        )


@dataclass(frozen=True)
class Config:
    checkpoint_dir: Path
    allocation_log_path: Path
    gas_checker: object = lambda _address, chain_id, _rpc_url, _config: GasCheck(
        chain_id=chain_id,
        ok=True,
        balance_wei=1,
        required_wei=1,
        message=f"native gas available on chain {chain_id}",
    )
    _rpc_overrides: dict[int, str] = field(default_factory=lambda: {8453: "rpc://base"})


def allocation(*instrument_ids: str) -> Allocation:
    return Allocation(
        legs=tuple(
            AllocationLeg(
                instrument_id=instrument_id,
                weight=1 / len(instrument_ids),
                usd=100,
            )
            for instrument_id in instrument_ids
        ),
        total_usd=100 * len(instrument_ids),
        metadata={},
    )


def policy() -> Policy:
    return Policy(
        wallet=PolicyWallet(mode="self-custody", signer="local-eoa"),
        allowed=PolicyAllowed(
            protocols=None,
            chains=None,
            assets=("USDC",),
            curators=None,
        ),
        caps=PolicyCaps(
            max_weight_per_instrument=1,
            max_weight_per_protocol=1,
            max_weight_per_curator=1,
            max_weight_per_chain=1,
            min_instrument_tvl_usd=1,
            max_reward_dependence=1,
        ),
        gates=PolicyGates(
            new_instrument_needs_approval=True,
            autonomous_rebalance=False,
            max_deploy_per_cycle_usd=1_000_000,
        ),
    )


def vault(instrument_id: str) -> Vault:
    return Vault(
        instrument_id=instrument_id,
        protocol="aave",
        chain_id=8453,
        asset="USDC",
        apy=0.04,
        tvl_usd=1_000_000,
        curator="curator-a",
        reward_dependence=0.1,
    )


def tx(data: str, *, type_: str = "deposit") -> dict[str, object]:
    return {
        "to": "0x0000000000000000000000000000000000000002",
        "data": data,
        "value": 0,
        "chainId": 8453,
        "type": type_,
    }


def holding(instrument_id: str, balance: str) -> PositionHolding:
    return PositionHolding(
        instrument_id=instrument_id,
        protocol="aave",
        chain_id=8453,
        symbol="USDC",
        balance=balance,
        balance_raw=str(int(float(balance) * 1_000_000)),
        decimals=6,
        usd_value=float(balance),
        share_balance=balance,
        share_balance_raw=str(int(float(balance) * 1_000_000)),
        share_decimals=6,
        yield_token_symbol="aUSDC",
        yield_token_address="0x0000000000000000000000000000000000000002",
    )


def positions_snapshot(*holdings: PositionHolding) -> Positions:
    total = sum(item.usd_value for item in holdings)
    return Positions(
        address=ADDRESS,
        holdings=tuple(holdings),
        idle_balances=(
            IdleBalance(
                chain_id=8453,
                chain_name="Base",
                usdc_balance="0",
                usdc_balance_raw="0",
                usd_value=0,
            ),
        ),
        total_position_usd=total,
        total_idle_usdc=0,
        total_usd=total,
        total_usdc_usd="0",
    )


@pytest.mark.parametrize("status", ["completed", "awaiting_human"])
def test_checkpoint_validates_successful_known_artifacts(
    tmp_path: Path,
    status: str,
) -> None:
    checkpoint = write_checkpoint(
        "build-allocation",
        status,
        allocation("vault-a"),
        checkpoint_dir=tmp_path,
        artifact_type="allocation",
    )

    loaded = read_checkpoint(checkpoint.id, checkpoint_dir=tmp_path)

    assert loaded.status == status
    assert loaded.schema_name == "allocation"
    assert loaded.artifact["legs"][0]["instrument_id"] == "vault-a"


@pytest.mark.parametrize("status", ["completed", "awaiting_human"])
def test_invalid_successful_checkpoint_artifact_fails_without_persisting(
    tmp_path: Path,
    status: str,
) -> None:
    with pytest.raises(SchemaValidationError):
        write_checkpoint(
            "build-allocation",
            status,
            {"total_usd": 100, "metadata": {}},
            checkpoint_id="bad-checkpoint",
            checkpoint_dir=tmp_path,
            artifact_type="allocation",
        )

    assert not (tmp_path / "bad-checkpoint.json").exists()


def test_resume_checkpoint_exposes_completed_idempotency_keys(
    tmp_path: Path,
) -> None:
    checkpoint = write_checkpoint(
        "execute",
        "failed",
        {
            "status": "failed",
            "plan": {"steps": [], "summary": "test plan"},
            "steps": [
                {
                    "status": "sent",
                    "idempotency_key": "leg:0:vault-a:step:0",
                },
                {
                    "status": "skipped",
                    "idempotency_key": "leg:1:vault-b:step:0",
                },
            ],
        },
        checkpoint_dir=tmp_path,
        artifact_type="execution-report",
        completed_keys=["leg:0:vault-a"],
    )

    state = resume_state(checkpoint.id, checkpoint_dir=tmp_path)
    store = idempotency_store_from_checkpoint(checkpoint.id, checkpoint_dir=tmp_path)

    assert state.completed_keys == (
        "leg:0:vault-a",
        "leg:0:vault-a:step:0",
        "leg:1:vault-b:step:0",
    )
    assert store.is_completed("leg:0:vault-a") is True
    assert store.is_completed("leg:0:vault-a:step:0") is True
    assert store.is_completed("leg:1:vault-b:step:1") is False


def test_allocation_log_appends_and_reconciles_against_positions(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "allocation-log.jsonl"

    write_allocation_log_entry(
        instrument_id="vault-a",
        chain_id=8453,
        action_type="buy",
        tx_hash="0x1",
        usd=70,
        log_path=log_path,
    )
    write_allocation_log_entry(
        instrument_id="vault-b",
        chain_id=8453,
        action_type="buy",
        tx_hash="0x2",
        usd=30,
        log_path=log_path,
    )
    write_allocation_log_entry(
        instrument_id="vault-b",
        chain_id=8453,
        action_type="sell",
        tx_hash="0x3",
        usd=10,
        log_path=log_path,
    )

    entries = read_allocation_log(log_path=log_path)
    reconciliation = reconcile_allocation_log(
        entries,
        positions_snapshot(holding("vault-a", "70"), holding("vault-b", "20")),
    )

    assert len(log_path.read_text(encoding="utf-8").splitlines()) == 3
    assert [entry.tx_hash for entry in entries] == ["0x1", "0x2", "0x3"]
    assert allocation_log_totals(entries) == {"vault-a": 70, "vault-b": 20}
    assert reconciliation.usd_difference == 0
    assert reconciliation.missing_in_positions == ()


def test_confirmed_execution_writes_checkpoint_and_allocation_log(
    tmp_path: Path,
) -> None:
    config = Config(
        checkpoint_dir=tmp_path / "checkpoints",
        allocation_log_path=tmp_path / "allocation-log.jsonl",
    )
    client = MockOneTxClient(
        [
            {
                "transactions": [
                    tx("0xapprove", type_="approve"),
                    tx("0xbuy", type_="deposit"),
                ]
            }
        ]
    )

    report = execute_allocation(
        client,
        MockSigner(),
        allocation("vault-a"),
        policy(),
        confirm=True,
        known_instruments=[vault("vault-a")],
        config=config,
        idempotency_store={},
    )

    checkpoint_files = sorted(config.checkpoint_dir.glob("*.json"))
    log_entries = read_allocation_log(log_path=config.allocation_log_path)

    assert report.status == "success"
    assert len(checkpoint_files) == 1
    checkpoint = read_checkpoint(checkpoint_files[0])
    assert checkpoint.status == "completed"
    assert checkpoint.artifact_type == "execute-report"
    assert checkpoint.artifact["plan"]["summary"].startswith("Build buy transactions")
    assert checkpoint.completed_keys == (
        "leg:0:vault-a",
        "leg:0:vault-a:step:0",
        "leg:0:vault-a:step:1",
    )
    logged_actions = [
        (entry.action_type, entry.instrument_id, entry.usd) for entry in log_entries
    ]
    assert logged_actions == [("buy", "vault-a", 100)]
