"""Plain CCTP transfers, run by rerunning `bridge --confirm`.

The fake world of ``test_bridge`` stands in for both chains, Circle, and the
bundler. A transfer burns on Base and redeems on Arbitrum with
``receiveMessage`` alone: no deposit calldata is ever requested.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from cctp_messages import ARBITRUM, ARBITRUM_USDC, BASE, BASE_USDC
from test_bridge import CHARGE, FEE, Circle, Client, Config, Signer, World, vaults

from open_allocator.core.schema import validate
from open_allocator.exec import bridge_state
from open_allocator.exec.bridge import BridgeUnavailableError
from open_allocator.exec.execute import ExecutionReport, TransactionPlanError
from open_allocator.exec.transfer import TRANSFER_LEG, execute_transfer

AMOUNT = 100_000_000


@dataclass
class Harness:
    world: World = field(default_factory=World)
    client: Client = field(default_factory=Client)
    store: dict[str, object] = field(default_factory=dict)
    signer: Signer = field(init=False)
    circle: Circle = field(init=False)
    config: Config = field(init=False)

    def __post_init__(self) -> None:
        self.world.credit(BASE, BASE_USDC, 500_000_000)
        self.signer = Signer(self.world)
        self.circle = Circle(self.world)
        self.config = Config(self.world, self.circle, source_chain_id=None)

    def run(self, *, confirm: bool = True, usdc: float = 100) -> ExecutionReport:
        report = execute_transfer(
            self.client,
            self.signer,
            from_chain_id=BASE,
            to_chain_id=ARBITRUM,
            amount_usdc=usdc,
            known_instruments=vaults(),
            confirm=confirm,
            config=self.config,
            idempotency_store=self.store,
        )
        validate(report.plan.model_dump(mode="json"), "tx-plan")
        for item in report.bridges:
            validate(item.model_dump(mode="json"), "bridge-state")
        return report

    def state(self) -> bridge_state.BridgeState:
        found = bridge_state.load(self.store, TRANSFER_LEG)
        assert found is not None
        return found

    def kinds(self) -> list[list[str]]:
        return [[step.kind for step in batch] for batch in self.signer.batches]


def test_dry_run_plans_the_burn_and_sends_nothing() -> None:
    harness = Harness()

    report = harness.run(confirm=False)

    assert report.status == "planned"
    assert [bundle.action for bundle in report.plan.bundles] == ["bridge"]
    assert report.plan.bundles[0].amount == str(AMOUNT)
    assert harness.signer.batches == []
    assert harness.store == {}
    assert any("`bridge --confirm`" in message for message in report.messages)


def test_transfer_burns_then_redeems_into_the_safe_without_a_deposit() -> None:
    harness = Harness()

    first = harness.run()

    assert first.status == "in_progress"
    assert harness.kinds() == [["approve", "bridge_burn"]]
    assert harness.state().state == "awaiting_attestation"
    assert harness.state().deposit is False
    assert harness.world.balance(BASE, BASE_USDC) == 500_000_000 - AMOUNT

    waiting = harness.run()

    assert waiting.status == "in_progress"
    assert len(harness.signer.batches) == 1

    harness.circle.ready = True
    settled = harness.run()

    assert settled.status == "success"
    assert harness.kinds()[1:] == [["cctp_receive"]]
    assert harness.client.deposits == []
    assert harness.state().state == "completed"
    assert harness.world.balance(ARBITRUM, ARBITRUM_USDC) == AMOUNT - FEE
    assert [item.state for item in settled.bridges] == ["completed"]


def test_rerun_after_settlement_sends_nothing_again() -> None:
    harness = Harness()
    harness.circle.ready = True
    assert harness.run().status == "success"
    sent = len(harness.signer.batches)

    again = harness.run()
    dry = harness.run(confirm=False)

    assert again.status == "success"
    assert dry.status == "success"
    assert len(harness.signer.batches) == sent
    assert len(harness.client.bridges) == 1
    assert any("--ref" in message for message in again.messages)


def test_dry_run_reports_a_transfer_under_way_instead_of_planning_it() -> None:
    harness = Harness()
    harness.run()

    report = harness.run(confirm=False)

    assert report.status == "planned"
    assert report.plan.bundles == ()
    assert [item.state for item in report.bridges] == ["awaiting_attestation"]
    assert len(harness.client.bridges) == 1


def test_a_mint_already_redeemed_completes_without_resending() -> None:
    harness = Harness()
    harness.run()
    harness.circle.ready = True
    # Someone else relayed the message first.
    harness.world.used_nonces.add("0x" + (1000).to_bytes(32, "big").hex())

    report = harness.run()

    assert report.status == "success"
    assert harness.kinds() == [["approve", "bridge_burn"]]
    assert any("already used" in message for message in report.messages)
    assert not any("positions" in message for message in report.messages)


def test_an_expired_attestation_is_reattested_not_reburned() -> None:
    harness = Harness()
    harness.run()
    harness.circle.ready = True
    harness.circle.expiration = 50

    # Expired at block 50; the destination is at block 100.
    report = harness.run()

    [nonce] = harness.circle.reattested
    assert report.status == "success"
    assert harness.state().nonce != nonce
    assert harness.kinds() == [["approve", "bridge_burn"], ["cctp_receive"]]
    assert len(harness.client.bridges) == 1


def test_a_reverted_redemption_is_rebuilt_on_the_next_run() -> None:
    harness = Harness()
    harness.run()
    harness.circle.ready = True
    harness.signer.revert_next = True

    failed = harness.run()

    assert failed.status == "failed"
    assert harness.state().state == "destination_ready"
    assert harness.run().status == "success"
    assert harness.world.balance(ARBITRUM, ARBITRUM_USDC) == AMOUNT - FEE


def test_the_burn_is_sized_down_to_leave_the_paymaster_charge() -> None:
    harness = Harness()
    harness.world.balances.clear()
    harness.world.credit(BASE, BASE_USDC, AMOUNT)

    report = harness.run(confirm=False)

    assert report.plan.bundles[0].amount == str(AMOUNT - CHARGE)
    assert any("sized to" in message for message in report.messages)


def test_confirm_without_a_store_is_refused_before_anything_is_sent() -> None:
    harness = Harness()

    with pytest.raises(TransactionPlanError, match="idempotency store"):
        execute_transfer(
            harness.client,
            harness.signer,
            from_chain_id=BASE,
            to_chain_id=ARBITRUM,
            amount_usdc=100,
            known_instruments=vaults(),
            confirm=True,
            config=harness.config,
            idempotency_store=None,
        )
    assert harness.signer.batches == []


def test_a_signer_that_cannot_carry_a_bridge_is_refused() -> None:
    harness = Harness()
    harness.signer.cross_chain = False

    with pytest.raises(BridgeUnavailableError):
        harness.run(confirm=False)


def test_same_chain_is_refused() -> None:
    harness = Harness()

    with pytest.raises(ValueError, match="must differ"):
        execute_transfer(
            harness.client,
            harness.signer,
            from_chain_id=BASE,
            to_chain_id=BASE,
            amount_usdc=100,
            known_instruments=vaults(),
            confirm=False,
            config=harness.config,
            idempotency_store=harness.store,
        )
