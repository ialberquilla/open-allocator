"""Cross-chain calldata deposits, run by rerunning `execute --confirm`.

A small fake world stands in for both chains, Circle, and the bundler: a
submitted operation's burns emit ``MessageSent`` logs and debit the Safe, its
redemptions spend CCTP nonces and credit the mint. Every scenario drives
``execute_allocation`` itself, invocation by invocation, as the CLI would.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from cctp_messages import (
    ARBITRUM,
    ARBITRUM_USDC,
    BASE,
    BASE_USDC,
    MESSAGE_TRANSMITTER,
    OTHER,
    SAFE,
    attested,
    bridge_response,
    burn_calls,
    burn_message,
    cctp_config_payload,
    message_sent_log,
)
from eth_abi import decode as abi_decode

from open_allocator.core.schema import validate
from open_allocator.core.state import json_safe
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
from open_allocator.exec import bridge_state
from open_allocator.exec.bridge import BridgeUnavailableError
from open_allocator.exec.calldata import CalldataValidationError
from open_allocator.exec.circle import CircleMessage, CircleMessages
from open_allocator.exec.client import (
    BridgeCalldataQuery,
    BridgeCalldataResponse,
    CctpConfigResponse,
    InstrumentCalldataQuery,
    InstrumentCalldataResponse,
)
from open_allocator.exec.execute import (
    ExecutionReport,
    GasCheck,
    execute_allocation,
    plan_calldata_allocation,
)
from open_allocator.exec.paymaster_types import (
    PaymasterTokenQuote,
    PreparedUserOperation,
    UserOperationGas,
    UserOperationReverted,
)
from open_allocator.exec.signer import Receipt

FIXTURES = Path(__file__).parent / "fixtures"
LEG = "leg:0:arb-vault"
CHARGE = 40_000
FEE = 10_000


# --- the world ----------------------------------------------------------------


@dataclass
class World:
    balances: dict[tuple[int, str], int] = field(default_factory=dict)
    logs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # op hash -> (chain, tx hash), for included operations.
    included: dict[str, tuple[int, str]] = field(default_factory=dict)
    pending: dict[str, tuple[int, str]] = field(default_factory=dict)
    reverted: set[str] = field(default_factory=set)
    used_nonces: set[str] = field(default_factory=set)
    # tx hash -> the Safe's source messages in it.
    burns: dict[str, list[bytes]] = field(default_factory=dict)
    block: int = 100
    noise: bool = False

    def balance(self, chain_id: int, token: str) -> int:
        return self.balances.get((chain_id, token.casefold()), 0)

    def credit(self, chain_id: int, token: str, amount: int) -> None:
        key = (chain_id, token.casefold())
        self.balances[key] = self.balances.get(key, 0) + amount

    def apply(self, steps: tuple[TxStep, ...], tx_hash: str) -> None:
        logs: list[dict[str, Any]] = []
        if self.noise:
            logs.append(
                message_sent_log(
                    burn_message(amount=100_000_000, sender=OTHER), log_index=0
                )
            )
        for step in steps:
            if step.kind == "bridge_burn":
                amount, domain, _r, token, _c, max_fee, threshold = abi_decode(
                    [
                        "uint256",
                        "uint32",
                        "bytes32",
                        "address",
                        "bytes32",
                        "uint256",
                        "uint32",
                    ],
                    bytes.fromhex(step.data[10:]),
                )
                self.credit(step.chain_id, token, -amount)
                message = burn_message(
                    amount=amount,
                    max_fee=max_fee,
                    destination_domain=domain,
                    threshold=threshold,
                )
                self.burns.setdefault(tx_hash, []).append(message)
                logs.append(message_sent_log(message, log_index=len(logs) + 1))
            elif step.kind == "cctp_receive":
                message, _attestation = abi_decode(
                    ["bytes", "bytes"], bytes.fromhex(step.data[10:])
                )
                nonce = "0x" + message[12:44].hex()
                assert nonce not in self.used_nonces, "a nonce was redeemed twice"
                self.used_nonces.add(nonce)
                amount = int.from_bytes(message[148 + 68 : 148 + 100], "big")
                fee = int.from_bytes(message[148 + 164 : 148 + 196], "big")
                self.credit(step.chain_id, ARBITRUM_USDC, amount - fee)
        self.logs[tx_hash] = logs


@dataclass
class Signer:
    world: World
    batches: list[tuple[TxStep, ...]] = field(default_factory=list)
    stay_pending: bool = False
    revert_next: bool = False
    cross_chain: bool = True
    prepared: list[tuple[TxStep, ...]] = field(default_factory=list)

    def address(self) -> str:
        return SAFE

    def supports_cross_chain(self) -> bool:
        return self.cross_chain

    def send(self, tx: TxStep, rpc_url: str) -> Receipt:
        raise AssertionError("a Safe sends whole operations")

    def send_batch(self, steps: tuple[TxStep, ...], rpc_url: str) -> Receipt:
        self.batches.append(tuple(steps))
        index = len(self.batches)
        op_hash = f"0x{index:064x}"
        tx_hash = f"0x{index + 0xF000:064x}"
        chain_id = steps[0].chain_id
        if self.revert_next:
            self.revert_next = False
            self.world.reverted.add(op_hash)
            raise UserOperationReverted(f"user operation {op_hash} reverted on chain")
        self.world.apply(tuple(steps), tx_hash)
        if self.stay_pending:
            self.world.pending[op_hash] = (chain_id, tx_hash)
            return Receipt(
                transaction_hash=op_hash,
                block_number=0,
                gas_used=0,
                status=0,
                from_address=SAFE,
                pending=True,
                execution_status="user_operation_submitted",
                safe_tx_hash=op_hash,
            )
        self.world.included[op_hash] = (chain_id, tx_hash)
        return self._included(op_hash, tx_hash)

    def operation_receipt(self, chain_id: int, operation_hash: str) -> Receipt | None:
        if operation_hash in self.world.reverted:
            raise UserOperationReverted(f"user operation {operation_hash} reverted")
        if operation_hash in self.world.pending:
            return None
        found = self.world.included.get(operation_hash)
        if found is None:
            return None
        return self._included(operation_hash, found[1])

    def include_pending(self) -> None:
        self.world.included.update(self.world.pending)
        self.world.pending.clear()
        self.stay_pending = False

    def prepare_batch(
        self,
        steps: tuple[TxStep, ...],
        rpc_url: str,
        *,
        assumed_balances: dict[str, int] | None = None,
    ) -> PreparedUserOperation:
        self.prepared.append(tuple(steps))
        token = BASE_USDC if steps[0].chain_id == BASE else ARBITRUM_USDC
        return PreparedUserOperation(
            sender=SAFE,
            chain_id=steps[0].chain_id,
            entry_point="0x0000000071727De22E5E9d8BAf0edAc6f37da032",
            user_operation={"sender": SAFE},
            deployed=True,
            gas=UserOperationGas(
                call_gas_limit=900_000,
                verification_gas_limit=450_000,
                pre_verification_gas=60_000,
                max_fee_per_gas=1_000_000,
                max_priority_fee_per_gas=1_000,
            ),
            paymaster=PaymasterTokenQuote(
                paymaster="0x777777777777AeC03fd955926DbF81597e66834C",
                token=token,
                exchange_rate=10**18,
                approval_included=False,
            ),
            max_gas_token_charge_raw=str(CHARGE),
        )

    def _included(self, op_hash: str, tx_hash: str) -> Receipt:
        return Receipt(
            transaction_hash=tx_hash,
            block_number=1,
            gas_used=21_000,
            status=1,
            from_address=SAFE,
            execution_status="user_operation_submitted",
            safe_tx_hash=op_hash,
        )


@dataclass
class Circle:
    world: World
    ready: bool = False
    fee_executed: int = FEE
    expiration: int = 0
    tamper: dict[str, object] = field(default_factory=dict)
    requests: list[tuple[int, str]] = field(default_factory=list)
    reattested: list[str] = field(default_factory=list)
    # Keep serving the expired attestation after a re-attestation request.
    slow_reattestation: bool = False

    def messages(self, source_domain: int, transaction_hash: str) -> CircleMessages:
        self.requests.append((source_domain, transaction_hash))
        burns = self.world.burns.get(transaction_hash, [])
        if not self.ready:
            return CircleMessages(
                messages=tuple(
                    CircleMessage(
                        message="0x",
                        attestation="PENDING",
                        status="pending_confirmations",
                        delay_reason="insufficient_confirmations",
                    )
                    for _ in burns
                )
            )
        items = []
        for index, source in enumerate(burns):
            if self.tamper:
                source = burn_message(
                    **{
                        "amount": int.from_bytes(source[148 + 68 : 148 + 100], "big"),
                        **self.tamper,
                    }  # type: ignore[arg-type]
                )
            reattested = 0 if self.slow_reattestation else len(self.reattested)
            nonce = 1000 + index + 10 * reattested
            items.append(
                CircleMessage(
                    message="0x"
                    + attested(
                        source,
                        nonce=nonce,
                        fee_executed=self.fee_executed,
                        expiration=self.expiration,
                    ).hex(),
                    attestation="0x" + "cd" * 65,
                    event_nonce="0x" + nonce.to_bytes(32, "big").hex(),
                    cctp_version=2,
                    status="complete",
                )
            )
        return CircleMessages(messages=tuple(items))

    def reattest(self, nonce: str) -> None:
        self.reattested.append(nonce)
        if not self.slow_reattestation:
            self.expiration = 0


@dataclass
class Reader:
    world: World

    def transaction_logs(
        self, chain_id: int, transaction_hash: str
    ) -> tuple[dict[str, Any], ...] | None:
        logs = self.world.logs.get(transaction_hash)
        return None if logs is None else tuple(logs)

    def nonce_used(self, chain_id: int, message_transmitter: str, nonce: str) -> bool:
        assert message_transmitter == MESSAGE_TRANSMITTER
        return nonce in self.world.used_nonces

    def block_number(self, chain_id: int) -> int:
        return self.world.block


@dataclass
class Client:
    deposits: list[tuple[str, str]] = field(default_factory=list)
    bridges: list[BridgeCalldataQuery] = field(default_factory=list)
    burn_calls: list[dict[str, Any]] | None = None

    def instrument_calldata(
        self, instrument_id: str, query: InstrumentCalldataQuery
    ) -> InstrumentCalldataResponse:
        self.deposits.append((instrument_id, query.amount))
        payload = json.loads(
            (FIXTURES / "calldata-instrument-deposit-swap.json").read_text()
        )
        chain_id, usdc = (
            (ARBITRUM, ARBITRUM_USDC)
            if instrument_id.startswith("arb")
            else (BASE, BASE_USDC)
        )
        payload.update(
            instrumentId=instrument_id,
            account=query.account,
            amountIn=query.amount,
            expiresAt=None,
            chainId=chain_id,
            requires=[{"token": usdc, "amount": query.amount}],
            leftovers=[],
        )
        payload["tokenIn"]["address"] = usdc
        for call in payload["calls"]:
            call["chainId"] = chain_id
        return InstrumentCalldataResponse.model_validate(payload)

    def bridge_calldata(self, query: BridgeCalldataQuery) -> BridgeCalldataResponse:
        self.bridges.append(query)
        return BridgeCalldataResponse.model_validate(
            bridge_response(
                int(query.amount),
                from_chain_id=query.from_chain_id,
                to_chain_id=query.to_chain_id,
                fast=query.fast,
                calls=self.burn_calls,
            )
        )

    def cctp_config(self) -> CctpConfigResponse:
        return CctpConfigResponse.model_validate(cctp_config_payload())


@dataclass
class Config:
    world: World
    circle: Circle
    source_chain_id: int | None = BASE
    transaction_api: str = "calldata"
    slippage_bps: int = 30
    fast_transfer: bool = True
    referral_fee_bps: int = 0
    referral_wallet: str | None = None
    min_calldata_ttl_seconds: int = 20
    checkpoint_dir: Path | None = None
    allocation_log_path: Path | None = None
    _rpc_overrides: dict[int, str] = field(
        default_factory=lambda: {BASE: "rpc://base", ARBITRUM: "rpc://arbitrum"}
    )
    gas_checker: object = lambda _address, chain_id, _rpc, _config: GasCheck(
        chain_id=chain_id, ok=True, message="ok"
    )
    cctp_reader: object = None

    def __post_init__(self) -> None:
        self.cctp_reader = Reader(self.world)

    @property
    def circle_client(self) -> Circle:
        return self.circle

    @property
    def token_balance_reader(self) -> object:
        return lambda chain_id, _rpc, token, _account: self.world.balance(
            chain_id, token
        )


def vaults() -> list[Vault]:
    def make(instrument_id: str, chain_id: int, usdc: str) -> Vault:
        return Vault(
            instrument_id=instrument_id,
            protocol="morpho",
            chain_id=chain_id,
            asset="USDC",
            apy=0.04,
            tvl_usd=1_000_000,
            reward_dependence=0.1,
            token_address=usdc,
            token_decimals=6,
        )

    return [
        make("arb-vault", ARBITRUM, ARBITRUM_USDC),
        make("arb-vault-2", ARBITRUM, ARBITRUM_USDC),
        make("base-vault", BASE, BASE_USDC),
    ]


def policy() -> Policy:
    return Policy(
        wallet=PolicyWallet(mode="self-custody", signer="safe"),
        allowed=PolicyAllowed(assets=("USDC",)),
        caps=PolicyCaps(
            max_weight_per_instrument=1,
            max_weight_per_protocol=1,
            max_weight_per_curator=1,
            max_weight_per_chain=1,
            min_instrument_tvl_usd=1,
            max_reward_dependence=1,
        ),
        gates=PolicyGates(
            new_instrument_needs_approval=False,
            autonomous_rebalance=False,
            max_deploy_per_cycle_usd=1_000_000,
        ),
    )


def one_leg(instrument_id: str = "arb-vault", usd: float = 100) -> Allocation:
    return Allocation(
        legs=(AllocationLeg(instrument_id=instrument_id, weight=1, usd=usd),),
        total_usd=usd,
    )


@dataclass
class Harness:
    world: World = field(default_factory=World)
    client: Client = field(default_factory=Client)
    store: dict[str, object] = field(default_factory=dict)
    allocation: Allocation = field(default_factory=one_leg)
    signer: Signer = field(init=False)
    circle: Circle = field(init=False)
    config: Config = field(init=False)

    def __post_init__(self) -> None:
        self.world.credit(BASE, BASE_USDC, 500_000_000)
        self.signer = Signer(self.world)
        self.circle = Circle(self.world)
        self.config = Config(self.world, self.circle)

    def run(self) -> ExecutionReport:
        report = execute_allocation(
            self.client,
            self.signer,  # type: ignore[arg-type]
            self.allocation,
            policy(),
            confirm=True,
            known_instruments=vaults(),
            config=self.config,
            idempotency_store=self.store,
        )
        payload = report.plan.model_dump(mode="json")
        validate(payload, "tx-plan")
        for item in report.bridges:
            validate(item.model_dump(mode="json"), "bridge-state")
        return report

    def state(self) -> bridge_state.BridgeState:
        found = bridge_state.load(self.store, LEG)
        assert found is not None
        return found

    def kinds(self) -> list[list[str]]:
        return [[step.kind for step in batch] for batch in self.signer.batches]


RECEIVE_AND_DEPOSIT = [
    "cctp_receive",
    "approve",
    "swap",
    "approve",
    "approve",
    "deposit",
]


# --- the happy path -------------------------------------------------------------


def test_a_bridged_leg_advances_exactly_once_from_burn_to_settlement() -> None:
    h = Harness()

    first = h.run()

    assert first.status == "in_progress"
    assert h.kinds() == [["approve", "bridge_burn"]]
    assert h.state().state == "awaiting_attestation"
    assert h.state().attestation_status == "pending_confirmations"
    assert h.state().delay_reason == "insufficient_confirmations"
    # The deposit is quoted only after Circle attests.
    assert h.client.deposits == []
    assert "leg:0:arb-vault" not in h.store

    waiting = h.run()

    assert waiting.status == "in_progress"
    assert len(h.signer.batches) == 1
    assert h.client.bridges and len(h.client.bridges) == 1

    h.circle.ready = True
    settled = h.run()

    assert settled.status == "success"
    assert h.kinds() == [["approve", "bridge_burn"], RECEIVE_AND_DEPOSIT]
    net_mint = 100_000_000 - FEE
    # Quoted at the attested mint, then again leaving the paymaster's charge.
    assert h.client.deposits == [
        ("arb-vault", str(net_mint)),
        ("arb-vault", str(net_mint - CHARGE)),
    ]
    state = h.state()
    assert state.state == "completed"
    assert state.net_mint_raw == str(net_mint)
    assert state.deposit_amount_raw == str(net_mint - CHARGE)
    assert state.destination_transaction_hash is not None
    assert h.store["leg:0:arb-vault"] is True
    assert [item.state for item in settled.bridges] == ["completed"]

    again = h.run()

    assert again.status == "success"
    assert len(h.signer.batches) == 2
    assert len(h.world.used_nonces) == 1


def test_the_destination_redeems_and_deposits_in_one_operation() -> None:
    h = Harness()
    h.circle.ready = True

    report = h.run()

    assert report.status == "success"
    [_burn, destination] = h.signer.batches
    assert destination[0].kind == "cctp_receive"
    assert destination[0].to == MESSAGE_TRANSMITTER
    assert {step.chain_id for step in destination} == {ARBITRUM}
    assert destination in h.signer.prepared


def test_a_burn_is_recorded_before_its_completion_is_marked() -> None:
    h = Harness()
    seen: list[str] = []

    class Recording(dict[str, object]):
        def __setitem__(self, key: str, value: object) -> None:
            seen.append(
                f"{key}={value.get('state') if isinstance(value, dict) else value}"
            )
            super().__setitem__(key, value)

    h.store = Recording()

    h.run()

    submitted = seen.index("bridge:leg:0:arb-vault=source_submitted")
    burn_key = seen.index("bridge:leg:0:arb-vault:burn=True")
    assert seen.index("bridge:leg:0:arb-vault=bridge_planned") < submitted < burn_key


def test_identical_burns_in_one_operation_each_redeem_their_own_message() -> None:
    h = Harness(
        allocation=Allocation(
            legs=(
                AllocationLeg(instrument_id="arb-vault", weight=0.5, usd=100),
                AllocationLeg(instrument_id="arb-vault-2", weight=0.5, usd=100),
            ),
            total_usd=200,
        )
    )
    h.circle.ready = True

    report = h.run()

    assert report.status == "success"
    [source, *destinations] = h.signer.batches
    assert [step.kind for step in source] == ["approve", "bridge_burn"] * 2
    assert len(destinations) == 2
    first = bridge_state.load(h.store, "leg:0:arb-vault")
    second = bridge_state.load(h.store, "leg:1:arb-vault-2")
    assert first is not None and second is not None
    assert (first.burn_index, second.burn_index) == (0, 1)
    assert (first.burns_in_operation, second.burns_in_operation) == (2, 2)
    assert first.nonce != second.nonce
    assert len(h.world.used_nonces) == 2


def test_a_destination_whose_gas_charge_cannot_be_bounded_is_not_submitted() -> None:
    h = Harness()
    h.run()
    h.circle.ready = True
    unbounded = h.signer.prepare_batch

    def prepare(steps: tuple[TxStep, ...], rpc_url: str, **kwargs: Any) -> Any:
        prepared = unbounded(steps, rpc_url, **kwargs)
        if steps[0].chain_id != ARBITRUM:
            return prepared
        return prepared.model_copy(update={"max_gas_token_charge_raw": None})

    h.signer.prepare_batch = prepare  # type: ignore[method-assign]

    report = h.run()

    assert report.status == "failed"
    assert h.kinds() == [["approve", "bridge_burn"]]
    assert h.state().state == "destination_ready"
    assert "could not be bounded" in (h.state().last_error or "")


# --- restart and resume -------------------------------------------------------------


def test_a_pending_source_operation_is_observed_never_resent() -> None:
    h = Harness()
    h.signer.stay_pending = True
    h.circle.ready = True

    first = h.run()

    assert first.status == "in_progress"
    assert h.state().state == "source_submitted"
    assert h.state().source_transaction_hash is None

    second = h.run()

    assert second.status == "in_progress"
    assert len(h.signer.batches) == 1

    h.signer.include_pending()
    third = h.run()

    assert third.status == "success"
    assert h.kinds() == [["approve", "bridge_burn"], RECEIVE_AND_DEPOSIT]
    assert len(h.client.bridges) == 1


def test_a_process_restart_resumes_from_the_stored_record_alone() -> None:
    h = Harness()
    h.run()
    stored = json.loads(json.dumps(json_safe(h.store)))

    restarted = Harness(world=h.world, client=Client(), store=stored)
    restarted.signer.batches = list(h.signer.batches)
    restarted.circle.ready = True
    report = restarted.run()

    assert report.status == "success"
    assert restarted.client.bridges == []
    assert [batch[0].kind for batch in restarted.signer.batches] == [
        "approve",
        "cctp_receive",
    ]


def test_a_pending_destination_operation_is_reconciled_not_resent() -> None:
    h = Harness()
    h.circle.ready = True
    h.run()
    # Undo the settlement's inclusion: the destination op is still pending.
    op_hash = h.state().destination_operation_hash
    assert op_hash is not None
    record = h.state().model_copy(
        update={"state": "destination_submitted", "destination_transaction_hash": None}
    )
    bridge_state.save(h.store, record)
    del h.store["leg:0:arb-vault"]
    h.world.pending[op_hash] = h.world.included.pop(op_hash)
    h.world.used_nonces.clear()

    pending = h.run()

    assert pending.status == "in_progress"
    assert len(h.signer.batches) == 2

    h.signer.include_pending()
    h.world.used_nonces.add(record.nonce or "")
    settled = h.run()

    assert settled.status == "success"
    assert len(h.signer.batches) == 2
    assert h.store["leg:0:arb-vault"] is True


def test_an_already_used_nonce_reconciles_instead_of_redeeming_twice() -> None:
    h = Harness()
    h.run()
    h.circle.ready = True
    # Observe the attestation without settling, then redeem out of band.
    h.signer.cross_chain = False
    h.run()
    assert h.state().state == "destination_ready"
    h.world.used_nonces.add(h.state().nonce or "")
    h.signer.cross_chain = True

    report = h.run()

    assert report.status == "success"
    assert h.kinds() == [["approve", "bridge_burn"]]
    assert h.state().state == "completed"
    assert any("already used" in message for message in report.messages)
    assert h.store["leg:0:arb-vault"] is True


def test_a_reverted_destination_deposit_leaves_the_nonce_and_is_rebuilt() -> None:
    h = Harness()
    h.run()
    h.circle.ready = True
    h.signer.revert_next = True

    failed = h.run()

    assert failed.status == "failed"
    assert h.state().state == "destination_ready"
    assert h.state().last_error is not None
    assert h.world.used_nonces == set()
    assert "leg:0:arb-vault" not in h.store

    quoted = len(h.client.deposits)
    rebuilt = h.run()

    assert rebuilt.status == "success"
    # Fresh deposit calldata, not the reverted operation's.
    assert len(h.client.deposits) > quoted
    assert len(h.world.used_nonces) == 1
    assert len([b for b in h.signer.batches if b[0].kind == "bridge_burn"]) == 0
    assert len([b for b in h.signer.batches if b[-1].kind == "bridge_burn"]) == 1


def test_an_expired_attestation_is_reattested_without_burning_again() -> None:
    h = Harness()
    h.run()
    h.circle.ready = True
    # Expired at block 50; the destination is at block 100.
    h.circle.expiration = 50

    report = h.run()

    [nonce] = h.circle.reattested
    assert report.status == "success"
    assert h.state().nonce != nonce
    assert h.kinds() == [["approve", "bridge_burn"], RECEIVE_AND_DEPOSIT]
    assert len(h.client.bridges) == 1


def test_a_still_expired_attestation_waits_instead_of_spinning() -> None:
    h = Harness()
    h.run()
    h.circle.ready = True
    h.circle.expiration = 50
    h.circle.slow_reattestation = True

    report = h.run()

    assert report.status == "in_progress"
    assert len(h.circle.reattested) == 1
    assert h.state().state == "awaiting_attestation"
    assert h.kinds() == [["approve", "bridge_burn"]]

    h.circle.slow_reattestation = False
    h.circle.expiration = 0
    assert h.run().status == "success"
    assert len(h.circle.reattested) == 1


# --- refusals -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tamper",
    [
        {"caller": OTHER},
        {"recipient": OTHER},
        {"destination_domain": 2},
        {"token": OTHER},
    ],
)
def test_an_attestation_that_does_not_match_the_burn_is_never_redeemed(
    tamper: dict[str, object],
) -> None:
    h = Harness()
    h.circle.ready = True
    h.circle.tamper = tamper

    report = h.run()

    # A message that is not this burn's is never selected, let alone redeemed.
    assert report.status == "in_progress"
    assert h.kinds() == [["approve", "bridge_burn"]]
    assert h.world.used_nonces == set()
    assert h.state().state == "awaiting_attestation"


def test_an_executed_fee_above_the_maximum_fails_the_leg() -> None:
    h = Harness()
    h.circle.ready = True
    h.circle.fee_executed = 12_001

    report = h.run()

    assert report.status == "failed"
    assert h.state().state == "failed"
    assert "feeExecuted" in (h.state().last_error or "")
    assert h.kinds() == [["approve", "bridge_burn"]]

    again = h.run()

    assert again.status == "failed"
    assert h.kinds() == [["approve", "bridge_burn"]]


def test_other_accounts_burns_in_the_bundler_transaction_are_ignored() -> None:
    h = Harness()
    h.world.noise = True
    h.circle.ready = True

    report = h.run()

    assert report.status == "success"
    assert h.state().source_log_index == 2


def test_a_reverted_source_burn_lets_the_leg_be_planned_again() -> None:
    h = Harness()
    h.signer.stay_pending = True
    h.run()
    op_hash = h.state().source_operation_hash
    assert op_hash is not None
    h.world.pending.pop(op_hash)
    h.world.reverted.add(op_hash)
    h.signer.stay_pending = False

    failed = h.run()

    assert failed.status == "failed"
    assert h.state().state == "failed"
    assert not h.state().active

    h.circle.ready = True
    replanned = h.run()

    assert replanned.status == "success"
    assert len(h.client.bridges) == 2


def test_burn_calldata_to_another_recipient_is_refused_before_sending() -> None:
    h = Harness()
    h.client.burn_calls = burn_calls(100_000_000, recipient=OTHER)

    with pytest.raises(CalldataValidationError, match="mintRecipient"):
        h.run()

    assert h.signer.batches == []
    assert bridge_state.load(h.store, LEG) is None


def test_a_signer_that_cannot_carry_a_bridge_is_refused_for_a_pinned_source() -> None:
    h = Harness()
    h.signer.cross_chain = False

    with pytest.raises(BridgeUnavailableError):
        h.run()

    assert h.signer.batches == []


# --- routing -------------------------------------------------------------------------


def test_an_unfunded_deposit_chain_is_funded_over_cctp_from_a_funded_one() -> None:
    h = Harness()
    h.config.source_chain_id = None
    h.circle.ready = True

    fitted = plan_calldata_allocation(
        h.client,
        h.signer,  # type: ignore[arg-type]
        h.allocation,
        policy(),
        known_instruments=vaults(),
        config=h.config,
        idempotency_store=h.store,
    )

    [bundle] = fitted.plan.bundles
    assert bundle.action == "bridge"
    assert bundle.chain_id == BASE
    assert bundle.bridge is not None and bundle.bridge.to_chain_id == ARBITRUM
    assert any("funded from Base" in message for message in fitted.messages)
    assert h.signer.batches == []

    assert h.run().status == "success"


def test_a_funded_deposit_chain_is_not_bridged() -> None:
    h = Harness()
    h.config.source_chain_id = None
    h.world.credit(ARBITRUM, ARBITRUM_USDC, 500_000_000)

    report = h.run()

    assert report.status == "success"
    assert h.client.bridges == []
    assert h.kinds() == [["approve", "swap", "approve", "approve", "deposit"]]


def test_a_dry_run_reports_an_in_flight_bridge_instead_of_planning_it_again() -> None:
    h = Harness()
    h.run()

    fitted = plan_calldata_allocation(
        h.client,
        h.signer,  # type: ignore[arg-type]
        h.allocation,
        policy(),
        known_instruments=vaults(),
        config=h.config,
        idempotency_store=h.store,
    )

    assert fitted.plan.bundles == ()
    assert any("awaiting_attestation" in message for message in fitted.messages)
    assert len(h.client.bridges) == 1
