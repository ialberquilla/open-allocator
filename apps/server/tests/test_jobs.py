"""Job runs, rewards and executions, as the dashboard reports them."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from oa_server.dashboard import Dashboard, rewards_view
from oa_server.db.models import AllocationLogRow

READ_AT = datetime(2026, 10, 5, tzinfo=UTC)
MONAD_USDC = "0x754704Bc059F8C67012fEd69BC8A327a5aafb603"


def reward(symbol: str, address: str, claimable: str, **swap: Any) -> dict[str, Any]:
    return {
        "provider": "MERKL",
        "chain_id": 143,
        "reward_token": {
            "address": address,
            "chain_id": 143,
            "decimals": 18,
            "symbol": symbol,
        },
        "claimable_amount": "1",
        "pending_amount": "0",
        "claimable_amount_normalized": claimable,
        "pending_amount_normalized": "0",
        "claim": {},
        "swap": {"status": "no-route", "token_out": MONAD_USDC} | swap,
        "instrument_ids": ["0xabc"],
    }


def payload() -> dict[str, Any]:
    return {
        "wallet": "0x5a",
        "rewards": [
            reward("nWMON", "0x" + "01" * 20, "0.46"),
            reward(
                "WMON",
                "0x" + "02" * 20,
                "3.37",
                status="ready",
                expected_amount_out="108950",
            ),
            reward("USDC", MONAD_USDC, "0.00115", status="not-needed"),
        ],
        "errors": [],
    }


def test_rewards_are_valued_only_where_1tx_quotes_usdc() -> None:
    view = rewards_view(payload(), read_at=READ_AT)

    assert [(r.symbol, r.usd) for r in view.rewards] == [
        ("WMON", pytest.approx(0.10895)),
        ("USDC", pytest.approx(0.00115)),
        ("nWMON", None),
    ]
    assert view.claimable_usd == pytest.approx(0.1101)
    assert view.unpriced == 1
    assert view.rewards[0].chain == "Monad"


def test_each_read_leaves_a_job_run(engine: Engine) -> None:
    reads = {"n": 0}

    def read_rewards() -> dict[str, Any]:
        reads["n"] += 1
        if reads["n"] == 2:
            raise RuntimeError("1Tx is down")
        return payload()

    board = Dashboard(
        engine,
        read_rewards=read_rewards,
        read_shelf=lambda: ([], []),
        clock=lambda: READ_AT,
    )
    assert board.run_job("rewards")
    with pytest.raises(RuntimeError):
        board.run_job("rewards")
    board.rewards()  # the last good read, still cached
    board.run_job("shelf")

    jobs = board.jobs()
    assert [run.job for run in jobs.runs] == ["shelf", "rewards", "rewards"]
    assert jobs.latest["rewards"].status == "failed"
    assert jobs.latest["rewards"].detail == {"error": "1Tx is down"}
    assert jobs.runs[2].status == "ok"
    assert jobs.runs[2].detail is not None and jobs.runs[2].detail["unpriced"] == 1
    assert jobs.latest["shelf"].detail == {"vaults": 0, "warnings": []}
    assert jobs.running == []
    assert reads["n"] == 2
    with pytest.raises(ValueError):
        board.run_job("drift")


def test_executions_are_the_allocation_log_newest_first(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        for n, action in enumerate(("buy", "withdraw")):
            session.add(
                AllocationLogRow(
                    entry={
                        "instrument_id": "0xabc",
                        "chain_id": 8453,
                        "action_type": action,
                        "tx_hash": f"0x{n}",
                        "timestamp": "2026-10-05T00:00:00Z",
                        "usd": 12.5,
                        "shares": None,
                        "share_price": None,
                        "basis": "unresolved",
                    }
                )
            )
    executions = Dashboard(engine).executions()
    assert [(e.action_type, e.chain, e.usd) for e in executions] == [
        ("withdraw", "Base", 12.5),
        ("buy", "Base", 12.5),
    ]
