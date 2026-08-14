"""The state port, and the retry it exists to survive.

The bug being fixed is not a crash. A Cloud Run Job hands each retry a fresh
filesystem, so the idempotency store -- the only record of which transactions
already went out -- is gone at exactly the moment it is consulted. The retry
reads an empty store, concludes nothing has been sent, and sends it again. It
succeeds, twice, and the money is what tells you.

So the first two tests below are the whole point: the same wipe, once against
the filesystem and once against an injected backend, asserting that one re-sends
and the other does not. Everything after them guards the seam that makes that
substitution possible without changing what a laptop writes to disk.
"""

from __future__ import annotations

import inspect
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from open_allocator import cli
from open_allocator.core.checkpoint import (
    AllocationLogEntry,
    Checkpoint,
    append_allocation_log_entry,
    read_allocation_log,
    read_checkpoint,
    write_checkpoint,
)
from open_allocator.core.state import (
    CheckpointExists,
    CheckpointNotFound,
    LocalFsBackend,
    ScopedIdempotencyStore,
    StateBackend,
    StateError,
    backend_from_config,
    json_safe,
    with_state_backend,
)
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
from open_allocator.exec.config import AllocatorConfig
from open_allocator.exec.execute import GasCheck, execute_allocation
from open_allocator.exec.signer import Receipt

ADDRESS = "0x0000000000000000000000000000000000000001"


class InMemoryBackend:
    """A backend that outlives the filesystem, standing in for the job's Postgres.

    Deliberately not a `LocalFsBackend` pointed somewhere else: the claim under
    test is that state survives when the *filesystem* does not, and a second
    directory on the same disk would not be evidence of that.
    """

    def __init__(self) -> None:
        self.checkpoints: dict[str, Checkpoint] = {}
        self.log: list[AllocationLogEntry] = []
        self.completed: dict[tuple[str, str], Any] = {}

    def write_checkpoint(self, checkpoint: Checkpoint) -> None:
        if checkpoint.id in self.checkpoints:
            raise CheckpointExists(checkpoint.id)
        self.checkpoints[checkpoint.id] = checkpoint

    def read_checkpoint(self, checkpoint_id: str) -> Checkpoint:
        try:
            return self.checkpoints[checkpoint_id]
        except KeyError as error:
            raise CheckpointNotFound(checkpoint_id) from error

    def append_allocation_log_entry(self, entry: AllocationLogEntry) -> None:
        self.log.append(entry)

    def read_allocation_log(self) -> tuple[AllocationLogEntry, ...]:
        return tuple(self.log)

    def is_completed(self, scope: str, key: str) -> bool:
        return (scope, key) in self.completed

    def mark_completed(self, scope: str, key: str, value: Any = None) -> None:
        self.completed[(scope, key)] = value


@dataclass
class MockOneTxClient:
    responses: list[dict[str, Any]]

    def build_buy(self, body: dict[str, object]) -> dict[str, Any]:
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


@dataclass
class Config:
    """Only the attributes the state seam reads. Everything is duck-typed, which
    is how the job will supply a Postgres backend without importing this class."""

    checkpoint_dir: Path | None = None
    allocation_log_path: Path | None = None
    idempotency_store_path: Path | None = None
    state_backend: object | None = None
    gas_checker: object = lambda _address, chain_id, _rpc_url, _config: GasCheck(
        chain_id=chain_id,
        ok=True,
        balance_wei=1,
        required_wei=1,
        message=f"native gas available on chain {chain_id}",
    )
    _rpc_overrides: dict[int, str] = field(
        default_factory=lambda: {8453: "rpc://base"},
    )


def fs_config(state_dir: Path, **overrides: object) -> Config:
    return Config(
        checkpoint_dir=state_dir / "checkpoints",
        allocation_log_path=state_dir / "allocation-log.jsonl",
        idempotency_store_path=state_dir / "execution-idempotency.json",
        **overrides,  # type: ignore[arg-type]
    )


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


def buy_response() -> dict[str, Any]:
    return {
        "transactions": [
            {
                "to": "0x0000000000000000000000000000000000000002",
                "data": "0xbuy",
                "value": 0,
                "chainId": 8453,
                "type": "deposit",
            },
        ],
    }


def run_execution(config: Config, signer: MockSigner) -> object:
    """One `execute --confirm`, through the CLI's own store construction.

    Building the store with `cli._execution_idempotency_store` rather than by
    hand is the point: the scope is content-derived there, and a test that
    invented its own scope would prove nothing about the path a retry takes.
    """
    target = allocation("vault-a")
    store = cli._execution_idempotency_store(config, target)
    return execute_allocation(
        MockOneTxClient([buy_response()]),
        signer,
        target,
        policy(),
        True,
        (vault("vault-a"),),
        config,
        store,
    )


# --- the money bug --------------------------------------------------------


def test_a_fresh_filesystem_re_sends_the_trade_that_already_landed(
    tmp_path: Path,
) -> None:
    """The failure this whole port exists for, reproduced.

    Nothing errors. The second run simply cannot see the first, so it does the
    work again -- which on a live wallet is a second deposit of the same money.
    """
    state_dir = tmp_path / "state"
    signer = MockSigner()

    run_execution(fs_config(state_dir), signer)
    shutil.rmtree(state_dir)  # what Cloud Run hands the retry
    run_execution(fs_config(state_dir), signer)

    assert len(signer.sent) == 2


def test_an_injected_backend_survives_the_filesystem_being_wiped(
    tmp_path: Path,
) -> None:
    """The same wipe, with state somewhere the platform does not own."""
    state_dir = tmp_path / "state"
    backend = InMemoryBackend()
    signer = MockSigner()

    run_execution(fs_config(state_dir, state_backend=backend), signer)
    # `ignore_errors` because there is nothing to remove: the first run wrote
    # no files at all, which is the same claim as the assertion below.
    shutil.rmtree(state_dir, ignore_errors=True)
    report = run_execution(fs_config(state_dir, state_backend=backend), signer)

    assert len(signer.sent) == 1
    assert report.status == "success"
    assert not state_dir.exists(), "an injected backend must not touch the disk"
    assert len(backend.log) == 1
    assert len(backend.checkpoints) == 2


# --- the port ------------------------------------------------------------


def test_local_fs_backend_satisfies_the_state_backend_protocol() -> None:
    assert isinstance(LocalFsBackend(), StateBackend)
    assert isinstance(InMemoryBackend(), StateBackend)


def test_every_protocol_method_is_implemented_with_the_same_signature() -> None:
    """A method added to the port must fail here, not in a container.

    `runtime_checkable` only checks that names exist, so an implementation whose
    parameters have drifted still passes `isinstance`. Comparing signatures is
    what turns "the Protocol grew a method" into a red test instead of a
    `TypeError` on the one code path nobody runs locally.
    """
    protocol_methods = {
        name
        for name, value in vars(StateBackend).items()
        if not name.startswith("_") and inspect.isfunction(value)
    }

    assert protocol_methods == {
        "write_checkpoint",
        "read_checkpoint",
        "append_allocation_log_entry",
        "read_allocation_log",
        "is_completed",
        "mark_completed",
    }
    for name in protocol_methods:
        expected = inspect.signature(getattr(StateBackend, name))
        actual = inspect.signature(getattr(LocalFsBackend, name))
        assert actual == expected, f"LocalFsBackend.{name} has drifted from the port"


def test_an_injected_backend_wins_over_configured_paths(tmp_path: Path) -> None:
    backend = InMemoryBackend()
    config = fs_config(tmp_path / "state", state_backend=backend)

    assert backend_from_config(config, needs="checkpoint_dir") is backend
    assert backend_from_config(config, needs="idempotency_store_path") is backend


def test_a_settings_model_can_be_given_a_backend_without_being_mutated(
    tmp_path: Path,
) -> None:
    """The gap this closes: `AllocatorConfig` rejects an attribute it did not
    declare, so the seam is only reachable by wrapping.

    Constructed here the way the job will: a real settings model, plus a
    backend, with nothing about the model's environment surface changed.
    """
    settings = AllocatorConfig(
        ONE_TX_API_URL="http://localhost:3001/api/v1",
        ONE_TX_API_KEY="test-api-key",
        ONE_TX_PRIVATE_KEY="0x" + "11" * 32,
        OPEN_ALLOCATOR_CHECKPOINT_DIR=tmp_path / "checkpoints",
    )
    backend = InMemoryBackend()

    with pytest.raises(ValueError, match="state_backend"):
        settings.state_backend = backend  # type: ignore[attr-defined]

    config = with_state_backend(settings, backend)

    assert backend_from_config(config, needs="checkpoint_dir") is backend
    assert config.checkpoint_dir == tmp_path / "checkpoints"
    assert getattr(settings, "state_backend", None) is None


def test_each_record_keeps_its_own_off_switch(tmp_path: Path) -> None:
    """Unsetting one path has always silenced one record, not all of them."""
    config = Config(allocation_log_path=tmp_path / "log.jsonl")

    assert backend_from_config(config, needs="checkpoint_dir") is None
    assert backend_from_config(config, needs="allocation_log_path") is not None


def test_no_config_at_all_means_no_backend() -> None:
    assert backend_from_config(None, needs="checkpoint_dir") is None


def test_a_backend_built_without_a_path_refuses_rather_than_guessing(
    tmp_path: Path,
) -> None:
    """The failure mode worth avoiding is a silent write to `.open_allocator/`
    in whatever directory the process happened to start in."""
    backend = LocalFsBackend(checkpoint_dir=None, log_path=tmp_path / "log.jsonl")

    with pytest.raises(StateError):
        backend.read_checkpoint("anything")


# --- the on-disk contract -------------------------------------------------


def test_the_checkpoint_file_layout_is_unchanged(tmp_path: Path) -> None:
    checkpoint = write_checkpoint(
        "build-allocation",
        "completed",
        allocation("vault-a"),
        checkpoint_dir=tmp_path,
        artifact_type="allocation",
    )

    path = tmp_path / f"{checkpoint.id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["id"] == checkpoint.id
    assert payload["schema_name"] == "allocation"
    assert read_checkpoint(checkpoint.id, checkpoint_dir=tmp_path) == checkpoint


def test_a_checkpoint_write_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    """Written to a temp name and renamed: a half-written checkpoint is worse
    than a missing one, because the missing one is obvious."""
    write_checkpoint("execute", "in_progress", {"a": 1}, checkpoint_dir=tmp_path)

    assert [path.name for path in tmp_path.iterdir() if path.name.startswith(".")] == []


def test_the_allocation_log_stays_one_json_object_per_line(tmp_path: Path) -> None:
    log_path = tmp_path / "allocation-log.jsonl"
    for index in range(2):
        append_allocation_log_entry(
            {
                "instrument_id": f"vault-{index}",
                "chain_id": 8453,
                "action_type": "buy",
                "tx_hash": f"0x{index:064x}",
                "timestamp": "2026-08-14T00:00:00Z",
                "usd": 100.0,
            },
            log_path=log_path,
        )

    lines = log_path.read_text(encoding="utf-8").splitlines()

    assert len(lines) == 2
    assert [json.loads(line)["instrument_id"] for line in lines] == [
        "vault-0",
        "vault-1",
    ]
    assert read_allocation_log(log_path=log_path)[0].basis == "unresolved"


def test_the_idempotency_store_keeps_its_versioned_scope_layout(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution-idempotency.json"
    backend = LocalFsBackend(idempotency_store_path=path)

    backend.mark_completed("scope-a", "leg:0:vault-a:step:0", {"hash": "0xabc"})

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["scopes"]["scope-a"]["leg:0:vault-a:step:0"] == {
        "completed": True,
        "value": {"hash": "0xabc"},
    }


def test_completed_keys_do_not_leak_between_scopes(tmp_path: Path) -> None:
    """The scope is a hash of what is being executed, so two runs sharing a step
    key are still two different runs."""
    backend = LocalFsBackend(idempotency_store_path=tmp_path / "store.json")
    first = ScopedIdempotencyStore(backend, "scope-a")
    second = ScopedIdempotencyStore(backend, "scope-b")

    first.mark_completed("leg:0:vault-a:step:0")

    assert first.is_completed("leg:0:vault-a:step:0")
    assert not second.is_completed("leg:0:vault-a:step:0")


def test_a_read_of_an_untouched_store_is_empty_rather_than_an_error(
    tmp_path: Path,
) -> None:
    backend = LocalFsBackend(
        log_path=tmp_path / "missing.jsonl",
        idempotency_store_path=tmp_path / "missing.json",
    )

    assert backend.read_allocation_log() == ()
    assert not backend.is_completed("scope-a", "key")


# --- errors ---------------------------------------------------------------


def test_a_duplicate_checkpoint_id_is_refused(tmp_path: Path) -> None:
    backend = LocalFsBackend(checkpoint_dir=tmp_path)
    checkpoint = write_checkpoint(
        "execute",
        "in_progress",
        {"a": 1},
        checkpoint_dir=tmp_path,
    )

    with pytest.raises(CheckpointExists):
        backend.write_checkpoint(checkpoint)


def test_a_missing_checkpoint_raises_the_ports_own_name(tmp_path: Path) -> None:
    backend = LocalFsBackend(checkpoint_dir=tmp_path)

    with pytest.raises(CheckpointNotFound):
        backend.read_checkpoint("nothing-was-written-here")


@pytest.mark.parametrize(
    ("error", "builtin"),
    [(CheckpointNotFound, FileNotFoundError), (CheckpointExists, FileExistsError)],
)
def test_the_ports_errors_are_still_catchable_as_the_builtins_they_replace(
    error: type[Exception],
    builtin: type[Exception],
) -> None:
    """A Postgres backend raises a name that is honest about a missing row; code
    written against the filesystem keeps catching what it always caught."""
    assert issubclass(error, builtin)
    assert issubclass(error, StateError)


# --- recording values -----------------------------------------------------


def test_json_safe_records_what_it_can_rather_than_raising() -> None:
    """This runs after the transaction is broadcast. A value that will not
    serialise must not be the reason the record of a sent trade is lost."""

    class Unserialisable:
        def __repr__(self) -> str:
            return "<unserialisable>"

    assert json_safe({"a": [1, Unserialisable()]}) == {"a": [1, "<unserialisable>"]}

    receipt = Receipt(
        transaction_hash=f"0x{1:064x}",
        block_number=1,
        gas_used=21_000,
        status=1,
        from_address=ADDRESS,
        to_address=ADDRESS,
    )
    assert json_safe(receipt)["transaction_hash"] == receipt.transaction_hash
