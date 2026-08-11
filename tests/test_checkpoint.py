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


# --- cost basis -------------------------------------------------------------
#
# The log is the only record of what a share cost: 1Tx has no wallet-history
# endpoint, so an amount not written at execution time is gone, not deferred.


def test_a_quoted_price_is_kept_and_completes_the_dollar_amount(
    tmp_path: Path,
) -> None:
    """A withdraw knows its price -- the plan quotes it -- so nothing is lost."""
    entry = write_allocation_log_entry(
        instrument_id="vault-a",
        chain_id=8453,
        action_type="withdraw",
        tx_hash="0x1",
        shares="1000",
        share_price="1.05",
        log_path=tmp_path / "log.jsonl",
    )

    assert entry.basis == "quoted"
    assert entry.share_price == "1.05"
    assert entry.usd == pytest.approx(1050.0)


def test_a_price_is_derived_when_both_amounts_are_known(tmp_path: Path) -> None:
    entry = write_allocation_log_entry(
        instrument_id="vault-a",
        chain_id=8453,
        action_type="sell",
        tx_hash="0x1",
        usd=1050.0,
        shares="1000",
        log_path=tmp_path / "log.jsonl",
    )

    assert entry.basis == "derived"
    assert entry.share_price == "1.05"


def test_a_quoted_price_wins_over_the_derivable_one(tmp_path: Path) -> None:
    """The venue's own number beats arithmetic over two rounded amounts."""
    entry = write_allocation_log_entry(
        instrument_id="vault-a",
        chain_id=8453,
        action_type="withdraw",
        tx_hash="0x1",
        usd=999.0,
        shares="1000",
        share_price="1.05",
        log_path=tmp_path / "log.jsonl",
    )

    assert entry.basis == "quoted"
    assert entry.share_price == "1.05"
    assert entry.usd == pytest.approx(999.0)


def test_a_buy_records_dollars_and_admits_it_has_no_price(tmp_path: Path) -> None:
    """The honest case, and the reason `basis` exists.

    1Tx's build endpoint neither takes nor returns a share amount and the
    receipt carries no logs, so a buy's price is genuinely unknown at write
    time. It must read as unknown rather than as an estimate.
    """
    entry = write_allocation_log_entry(
        instrument_id="vault-a",
        chain_id=8453,
        action_type="buy",
        tx_hash="0x1",
        usd=1000.0,
        log_path=tmp_path / "log.jsonl",
    )

    assert entry.basis == "unresolved"
    assert entry.share_price is None
    assert entry.usd == 1000.0


def test_a_zero_share_count_does_not_lose_the_entry(tmp_path: Path) -> None:
    """The write happens after the money moved; a bad amount must not raise."""
    entry = write_allocation_log_entry(
        instrument_id="vault-a",
        chain_id=8453,
        action_type="sell",
        tx_hash="0x1",
        usd=500.0,
        shares="0",
        log_path=tmp_path / "log.jsonl",
    )

    assert entry.basis == "unresolved"
    assert entry.share_price is None
    assert entry.usd == 500.0


def test_a_tiny_price_is_not_rounded_away(tmp_path: Path) -> None:
    """Bounded by significant digits: a fixed decimal place reads dust as free."""
    entry = write_allocation_log_entry(
        instrument_id="vault-a",
        chain_id=8453,
        action_type="sell",
        tx_hash="0x1",
        usd=1.0,
        shares="1000000000000000000000000000",
        log_path=tmp_path / "log.jsonl",
    )

    assert entry.share_price is not None
    assert float(entry.share_price) > 0
    assert "e" not in entry.share_price.casefold()


def test_a_large_share_count_survives_the_round_trip(tmp_path: Path) -> None:
    """Shares are decimal strings because this number breaks a float."""
    shares = "123456789012345678901234567890"
    log_path = tmp_path / "log.jsonl"
    write_allocation_log_entry(
        instrument_id="vault-a",
        chain_id=8453,
        action_type="withdraw",
        tx_hash="0x1",
        shares=shares,
        share_price="1.0",
        log_path=log_path,
    )

    assert read_allocation_log(log_path=log_path)[0].shares == shares


def test_a_withdrawal_now_subtracts_from_the_logged_total(tmp_path: Path) -> None:
    """Before the price was recorded, an exit carried no dollars at all and
    reconciliation skipped it -- so the log drifted after every withdrawal."""
    log_path = tmp_path / "log.jsonl"
    write_allocation_log_entry(
        instrument_id="vault-a",
        chain_id=8453,
        action_type="buy",
        tx_hash="0x1",
        usd=1000.0,
        log_path=log_path,
    )
    write_allocation_log_entry(
        instrument_id="vault-a",
        chain_id=8453,
        action_type="withdraw",
        tx_hash="0x2",
        shares="400",
        share_price="1.0",
        log_path=log_path,
    )

    totals = allocation_log_totals(read_allocation_log(log_path=log_path))

    assert totals == {"vault-a": 600}


def test_old_entries_still_load(tmp_path: Path) -> None:
    """Existing logs predate these fields and must not become unreadable."""
    log_path = tmp_path / "log.jsonl"
    log_path.write_text(
        '{"instrument_id":"vault-a","chain_id":8453,"action_type":"buy",'
        '"tx_hash":"0x1","timestamp":"2026-08-01T00:00:00Z","usd":100.0,'
        '"shares":null}\n',
        encoding="utf-8",
    )

    entry = read_allocation_log(log_path=log_path)[0]

    assert entry.usd == 100.0
    assert entry.basis == "unresolved"


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
