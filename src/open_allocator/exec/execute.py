from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, overload, runtime_checkable

from pydantic import Field
from web3 import HTTPProvider, Web3

from open_allocator.core import checkpoint as checkpoint_core
from open_allocator.core import policy as policy_core
from open_allocator.core.types import (
    Allocation,
    FrozenModel,
    Policy,
    TxPlan,
    TxStep,
    Vault,
)
from open_allocator.exec import chains
from open_allocator.exec.erc4337_paymaster import (
    PaymasterError,
    paymaster_cost_notes,
    submits_via_paymaster,
    validate_paymaster_preflight,
)
from open_allocator.exec.signer import Receipt, Signer


class GasCheck(FrozenModel):
    chain_id: int
    ok: bool
    balance_wei: int | None = Field(default=None, ge=0)
    required_wei: int = Field(default=1, ge=0)
    message: str


class ExecutionStepReport(FrozenModel):
    leg_index: int
    step_index: int
    instrument_id: str
    status: Literal["planned", "sent", "skipped"]
    step: TxStep | None = None
    receipt: Receipt | None = None
    idempotency_key: str | None = None


class ExecutionReport(FrozenModel):
    status: Literal["planned", "success", "in_progress", "failed"]
    policy_result: policy_core.PolicyResult
    plan: TxPlan
    steps: tuple[ExecutionStepReport, ...] = Field(default_factory=tuple)
    receipts: tuple[Receipt, ...] = Field(default_factory=tuple)
    gas_checks: tuple[GasCheck, ...] = Field(default_factory=tuple)
    in_progress: bool = False
    messages: tuple[str, ...] = Field(default_factory=tuple)


class ExecutionError(RuntimeError):
    pass


class PolicyCheckFailed(ExecutionError):
    def __init__(self, result: policy_core.PolicyResult) -> None:
        self.result = result
        violations = ", ".join(
            f"{violation.rule}:{violation.entity}"
            for violation in result.violations
        )
        super().__init__(f"policy check failed: {violations}")


class TransactionPlanError(ExecutionError):
    pass


class GasPreflightError(ExecutionError):
    def __init__(self, checks: Sequence[GasCheck]) -> None:
        self.checks = tuple(checks)
        failures = "; ".join(check.message for check in self.checks if not check.ok)
        super().__init__(f"gas preflight failed: {failures}")


class ExecutionBroadcastError(ExecutionError):
    def __init__(
        self,
        message: str,
        *,
        leg_index: int,
        step_index: int,
        partial_report: ExecutionReport,
    ) -> None:
        self.leg_index = leg_index
        self.step_index = step_index
        self.partial_report = partial_report
        super().__init__(message)


@runtime_checkable
class IdempotencyStore(Protocol):
    def is_completed(self, key: str) -> bool: ...

    def mark_completed(self, key: str, value: object | None = None) -> None: ...


GasChecker = Callable[[str, int, str, object | None], GasCheck | bool]

_PENDING_STATUSES = {"pending", "confirming_source", "confirmingsource"}


@overload
def execute_allocation(
    client: object,
    signer: Signer,
    allocation: Allocation | Mapping[str, object],
    policy: Policy | Mapping[str, object],
    confirm: Literal[False] = False,
    known_instruments: Iterable[Vault | Mapping[str, object]] | None = None,
    config: object | None = None,
    idempotency_store: object | None = None,
) -> TxPlan: ...


@overload
def execute_allocation(
    client: object,
    signer: Signer,
    allocation: Allocation | Mapping[str, object],
    policy: Policy | Mapping[str, object],
    confirm: Literal[True],
    known_instruments: Iterable[Vault | Mapping[str, object]] | None = None,
    config: object | None = None,
    idempotency_store: object | None = None,
) -> ExecutionReport: ...


def execute_allocation(
    client: object,
    signer: Signer,
    allocation: Allocation | Mapping[str, object],
    policy: Policy | Mapping[str, object],
    confirm: bool = False,
    known_instruments: Iterable[Vault | Mapping[str, object]] | None = None,
    config: object | None = None,
    idempotency_store: object | None = None,
) -> ExecutionReport | TxPlan:
    allocation_model = _allocation(allocation)
    policy_model = _policy(policy)
    known = tuple(known_instruments or ())
    policy_result = policy_core.check(allocation_model, policy_model, known)
    if not policy_result.ok:
        raise PolicyCheckFailed(policy_result)

    address = signer.address()
    vaults_by_id = _vaults_by_id(known)
    balances_by_chain = _idle_usdc_by_chain(client, address)
    plan_steps: list[TxStep] = []
    step_refs: list[_StepRef] = []
    build_payloads: list[object] = []

    for leg_index, leg in enumerate(allocation_model.legs):
        leg_key = _leg_key(leg_index, leg.instrument_id)
        if _store_completed(idempotency_store, leg_key):
            continue

        response = _build_buy(
            client,
            _buy_body(
                address,
                allocation_model,
                leg_index,
                vaults_by_id,
                config,
                balances_by_chain,
            ),
        )
        build_payloads.append(response)
        raw_steps = _raw_transactions(response)

        for step_index, raw_step in enumerate(raw_steps):
            step = _tx_step(raw_step, step_index, len(raw_steps))
            step_key = _step_key(leg_index, leg.instrument_id, step_index)
            plan_steps.append(step)
            step_refs.append(
                _StepRef(
                    leg_index=leg_index,
                    step_index=step_index,
                    instrument_id=leg.instrument_id,
                    step=step,
                    idempotency_key=step_key,
                    usd=leg.usd,
                    action_type="buy",
                )
            )

    plan = TxPlan(
        steps=tuple(plan_steps),
        summary=_plan_summary(allocation_model, plan_steps),
    )
    in_progress = any(_is_in_progress_payload(payload) for payload in build_payloads)
    messages = _messages(build_payloads)
    if not confirm:
        return plan

    rpc_urls, gas_checks = _preflight(
        address,
        step_refs,
        config,
        idempotency_store,
    )

    execution_steps: list[ExecutionStepReport] = []
    receipts: list[Receipt] = []
    for group in submission_groups(step_refs, idempotency_store, signer):
        if group.completed:
            for ref in group.refs:
                execution_steps.append(
                    ExecutionStepReport(
                        leg_index=ref.leg_index,
                        step_index=ref.step_index,
                        instrument_id=ref.instrument_id,
                        status="skipped",
                        step=ref.step,
                        idempotency_key=ref.idempotency_key,
                    )
                )
                _mark_leg_if_complete(ref, step_refs, idempotency_store)
            continue

        ref = group.refs[0]
        try:
            receipt = submit_steps(signer, group.refs, rpc_urls[ref.step.chain_id])
        except PaymasterError:
            raise
        except Exception as error:
            partial_report = ExecutionReport(
                status="failed",
                policy_result=policy_result,
                plan=plan,
                steps=tuple(execution_steps),
                receipts=tuple(receipts),
                gas_checks=gas_checks,
                in_progress=False,
                messages=messages,
            )
            _write_checkpoint(
                config,
                "execute",
                partial_report,
                completed_keys=_completed_keys(step_refs, execution_steps),
            )
            raise ExecutionBroadcastError(
                "transaction broadcast failed",
                leg_index=ref.leg_index,
                step_index=ref.step_index,
                partial_report=partial_report,
            ) from error

        # One receipt per operation, shared by every step that rode in it: the
        # batch is atomic, so they all landed in the same transaction or none did.
        receipts.append(receipt)
        for member in group.refs:
            _store_mark_completed(idempotency_store, member.idempotency_key, receipt)
            _append_allocation_log(config, member, receipt)
            _mark_leg_if_complete(member, step_refs, idempotency_store)
            execution_steps.append(
                ExecutionStepReport(
                    leg_index=member.leg_index,
                    step_index=member.step_index,
                    instrument_id=member.instrument_id,
                    status="sent",
                    step=member.step,
                    receipt=receipt,
                    idempotency_key=member.idempotency_key,
                )
            )

    unconfirmed = pending_receipt_messages(receipts)
    in_progress = in_progress or bool(unconfirmed)
    report = ExecutionReport(
        status="in_progress" if in_progress else "success",
        policy_result=policy_result,
        plan=plan,
        steps=tuple(execution_steps),
        receipts=tuple(receipts),
        gas_checks=gas_checks,
        in_progress=in_progress,
        messages=(*messages, *unconfirmed),
    )
    _write_checkpoint(
        config,
        "execute",
        report,
        completed_keys=_completed_keys(step_refs, execution_steps),
    )
    return report


@dataclass(frozen=True)
class SubmissionGroup:
    """Steps that go out together, and whether they are already done."""

    refs: tuple[Any, ...]
    completed: bool


def supports_batching(signer: object) -> bool:
    """Whether this signer can put several steps in one transaction.

    A smart account can; an EOA cannot, and must keep sending one at a time.
    """
    return callable(getattr(signer, "send_batch", None))


def submission_groups(
    step_refs: Sequence[Any],
    idempotency_store: object,
    signer: object,
) -> list[SubmissionGroup]:
    """Consecutive steps on one chain, batched when the signer allows it.

    Only consecutive runs are merged, so the order the plan was built in — an
    approval before the call it clears — survives.
    """
    batching = supports_batching(signer)
    groups: list[SubmissionGroup] = []
    for ref in step_refs:
        done = _store_completed(idempotency_store, ref.idempotency_key)
        last = groups[-1] if groups else None
        joinable = (
            last is not None
            and last.completed == done
            and (done or batching)
            and last.refs[-1].step.chain_id == ref.step.chain_id
        )
        if joinable and last is not None:
            groups[-1] = SubmissionGroup((*last.refs, ref), done)
        else:
            groups.append(SubmissionGroup((ref,), done))
    return groups


def pending_receipt_messages(
    receipts: Sequence[Receipt | Mapping[str, object]],
) -> tuple[str, ...]:
    """One message per receipt that was submitted but has no on-chain result.

    A Safe transaction awaiting co-signers and a user operation the bundler has
    not included yet are both real submissions that have settled nothing, so a
    report carrying either must not read as a completed spend.

    The idempotency key is still marked completed: the submission happened, and
    re-running must not propose or send it a second time. What changes is what
    the report claims — `in_progress`, never `success`.
    """
    messages: list[str] = []
    for receipt in receipts:
        # Signers hand back a Receipt; the mapping form is what a raw adapter
        # response looks like before it is validated.
        if not _attr(receipt, "pending"):
            continue
        tx_hash = _attr(receipt, "transaction_hash", "transactionHash")
        execution_status = _attr(receipt, "execution_status", "executionStatus")
        if execution_status == "safe_proposed":
            messages.append(
                f"Safe transaction {tx_hash} is proposed and awaiting "
                "threshold signatures/execution"
            )
        elif execution_status == "user_operation_submitted":
            messages.append(
                f"user operation {tx_hash} was submitted but is not confirmed "
                "on chain"
            )
        else:
            messages.append(
                f"transaction {tx_hash} was submitted but is not confirmed "
                "on chain"
            )
    return tuple(messages)


def submit_steps(signer: object, refs: Sequence[Any], rpc_url: str) -> Receipt:
    steps = [ref.step for ref in refs]
    if len(steps) == 1:
        return signer.send(steps[0], rpc_url)  # type: ignore[attr-defined]
    return signer.send_batch(steps, rpc_url)  # type: ignore[attr-defined]


class _StepRef(FrozenModel):
    leg_index: int
    step_index: int
    instrument_id: str
    step: TxStep
    idempotency_key: str
    usd: float | None = None
    shares: str | None = None
    action_type: str


def _allocation(allocation: Allocation | Mapping[str, object]) -> Allocation:
    if isinstance(allocation, Allocation):
        return allocation
    return Allocation.model_validate(allocation)


def _policy(policy: Policy | Mapping[str, object]) -> Policy:
    if isinstance(policy, Policy):
        return policy
    return Policy.model_validate(policy)


def _vaults_by_id(
    known_instruments: Iterable[Vault | Mapping[str, object]],
) -> dict[str, Vault]:
    vaults: dict[str, Vault] = {}
    for instrument in known_instruments:
        vault = (
            instrument
            if isinstance(instrument, Vault)
            else Vault.model_validate(instrument)
        )
        vaults[vault.instrument_id] = vault
    return vaults


def _build_buy(client: object, body: Mapping[str, object]) -> object:
    build_buy = getattr(client, "build_buy", None)
    if not callable(build_buy):
        raise TransactionPlanError("client does not implement build_buy")
    return build_buy(body)


def _buy_body(
    address: str,
    allocation: Allocation,
    leg_index: int,
    vaults_by_id: Mapping[str, Vault],
    config: object | None,
    balances_by_chain: Mapping[int, float] | None = None,
) -> dict[str, object]:
    leg = allocation.legs[leg_index]
    body: dict[str, object] = {
        "userAddress": address,
        "instrumentId": leg.instrument_id,
        "amountUsdc": _amount_usdc(leg.usd),
    }
    source_chain_id = _source_chain_id(
        allocation,
        leg.instrument_id,
        vaults_by_id,
        config,
        leg.usd,
        balances_by_chain,
    )
    if source_chain_id is not None:
        body["sourceChainId"] = source_chain_id

    _copy_config_value(body, "slippageBps", config, "slippage_bps")
    _copy_config_value(body, "fastTransfer", config, "fast_transfer")
    _copy_config_value(body, "referralFeeBps", config, "referral_fee_bps")
    _copy_config_value(body, "referralWallet", config, "referral_wallet")
    return body


def _amount_usdc(value: float) -> str:
    return format(value, ".6f").rstrip("0").rstrip(".")


def _source_chain_id(
    allocation: Allocation,
    instrument_id: str,
    vaults_by_id: Mapping[str, Vault],
    config: object | None,
    leg_usd: float | None,
    balances_by_chain: Mapping[int, float] | None,
) -> int | None:
    configured = _config_value(config, "source_chain_id")
    if configured is not None:
        return int(configured)

    for key in ("source_chain_id", "sourceChainId"):
        value = allocation.metadata.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value

    # Balance-aware default: source USDC from the chain the wallet is actually
    # funded on. 1Tx (SwapDepositRouter + CCTP) bridges from that source chain
    # to the vault's destination chain when they differ. Pinning the source to
    # the vault's own chain breaks execution whenever the wallet holds no USDC
    # there — 1Tx returns "No chain has sufficient USDC balance" instead of
    # bridging. If balances are unavailable, omit sourceChainId and let 1Tx
    # auto-select. An explicit source_chain_id (config/metadata) overrides all.
    vault = vaults_by_id.get(instrument_id)
    vault_chain = vault.chain_id if vault is not None else None
    return _select_source_chain(vault_chain, leg_usd, balances_by_chain)


def _select_source_chain(
    vault_chain: int | None,
    amount: float | None,
    balances_by_chain: Mapping[int, float] | None,
) -> int | None:
    if not balances_by_chain:
        return None
    funded = {chain: usdc for chain, usdc in balances_by_chain.items() if usdc > 0}
    if not funded:
        return None

    required = amount if amount is not None else 0.0
    sufficient = {chain: usdc for chain, usdc in funded.items() if usdc >= required}
    # Prefer the vault's own chain when it can cover the leg (no bridge needed).
    if vault_chain in sufficient:
        return vault_chain
    if sufficient:
        return max(sufficient, key=lambda chain: sufficient[chain])
    # No single chain can cover the leg. Stay bridge-free when possible, then
    # fall back to the best-funded chain; 1Tx surfaces the shortfall.
    if vault_chain in funded:
        return vault_chain
    return max(funded, key=lambda chain: funded[chain])


def _idle_usdc_by_chain(client: object, address: str) -> dict[int, float]:
    getter = getattr(client, "balances", None)
    if not callable(getter):
        return {}
    try:
        response = getter(address)
    except Exception:  # noqa: BLE001 - routing degrades to 1Tx auto-select
        return {}

    raw_balances = _attr(response, "balances")
    if raw_balances is None or isinstance(raw_balances, str | bytes | bytearray):
        return {}
    if not isinstance(raw_balances, Iterable):
        return {}

    result: dict[int, float] = {}
    for item in raw_balances:
        chain = _attr(item, "chain_id", "chainId")
        usdc = _attr(item, "usdc_balance", "usdcBalance")
        if not isinstance(chain, int) or isinstance(chain, bool):
            continue
        try:
            result[chain] = float(usdc)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return result


def _attr(obj: object, *names: str) -> object | None:
    if isinstance(obj, Mapping):
        for name in names:
            if name in obj:
                return obj[name]
        return None
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


def _copy_config_value(
    body: dict[str, object],
    body_key: str,
    config: object | None,
    attr: str,
) -> None:
    value = _config_value(config, attr)
    if value is not None:
        body[body_key] = value


def _config_value(config: object | None, attr: str) -> object | None:
    if config is None:
        return None
    if isinstance(config, Mapping):
        return config.get(attr)
    return getattr(config, attr, None)


def _raw_transactions(response: object) -> tuple[Mapping[str, object], ...]:
    if isinstance(response, Mapping):
        transactions = response.get("transactions")
    else:
        transactions = response

    if not isinstance(transactions, Sequence) or isinstance(
        transactions,
        str | bytes | bytearray,
    ):
        raise TransactionPlanError("buy response does not contain transactions")

    raw_steps: list[Mapping[str, object]] = []
    for item in transactions:
        if not isinstance(item, Mapping):
            raise TransactionPlanError("transaction step is not an object")
        raw_steps.append(item)
    return tuple(raw_steps)


def _tx_step(
    raw_step: Mapping[str, object],
    step_index: int,
    step_count: int,
) -> TxStep:
    try:
        return TxStep(
            to=str(raw_step["to"]),
            data=str(raw_step["data"]),
            value=_int_value(raw_step.get("value", 0)),
            chain_id=_int_value(raw_step["chainId"]),
            kind=_tx_kind(raw_step, step_index, step_count),
        )
    except KeyError as error:
        field = error.args[0]
        raise TransactionPlanError(f"transaction missing field: {field}") from error
    except (TypeError, ValueError) as error:
        raise TransactionPlanError("transaction has invalid field values") from error


def _tx_kind(
    raw_step: Mapping[str, object],
    step_index: int,
    step_count: int,
) -> Literal["approve", "buy", "sell"]:
    raw_kind = raw_step.get("kind", raw_step.get("type"))
    if isinstance(raw_kind, str):
        normalized = raw_kind.casefold()
        if normalized in {"approve", "approval"}:
            return "approve"
        if normalized == "sell":
            return "sell"
        if normalized in {"buy", "deposit", "router", "swap"}:
            return "buy"
    if step_count > 1 and step_index < step_count - 1:
        return "approve"
    return "buy"


def _int_value(value: object) -> int:
    if isinstance(value, str):
        return int(value, 0)
    return int(value)


def _plan_summary(allocation: Allocation, steps: Sequence[TxStep]) -> str:
    return (
        f"Build buy transactions for {len(allocation.legs)} allocation legs "
        f"across {len(steps)} transaction steps"
    )


def _paymaster_gas_message(note: Mapping[str, object]) -> str:
    chain_id = note["chain_id"]
    provider = note.get("provider")
    message = f"gas paid in USDC via {provider} on chain {chain_id}"
    # The provider's fee is inside the quoted rate, so naming the rate is the
    # only honest way to quote the cost — a flat percentage here would be a
    # number we made up.
    if note.get("exchange_rate") is not None:
        message += " (live quote, provider fee included in rate)"
    else:
        message += " (rate quoted at submission, provider fee included)"
    return message


def _preflight(
    address: str,
    step_refs: Sequence[_StepRef],
    config: object | None,
    idempotency_store: object | None,
) -> tuple[dict[int, str], tuple[GasCheck, ...]]:
    chain_ids = sorted(
        {
            ref.step.chain_id
            for ref in step_refs
            if not _store_completed(idempotency_store, ref.idempotency_key)
        }
    )
    rpc_urls: dict[int, str] = {}
    checks: list[GasCheck] = []

    if submits_via_paymaster(config):
        rpc_urls = validate_paymaster_preflight(config, chain_ids)
        for note in paymaster_cost_notes(config, chain_ids):
            checks.append(
                GasCheck(
                    chain_id=int(note["chain_id"]),
                    ok=True,
                    required_wei=0,
                    message=_paymaster_gas_message(note),
                )
            )
        return rpc_urls, tuple(checks)

    for chain_id in chain_ids:
        try:
            rpc_url = chains.require_rpc_url(chain_id, config)
        except chains.MissingRPCError:
            checks.append(
                GasCheck(
                    chain_id=chain_id,
                    ok=False,
                    message=f"missing RPC for chain {chain_id}",
                )
            )
            continue

        rpc_urls[chain_id] = rpc_url
        try:
            checks.append(_run_gas_checker(address, chain_id, rpc_url, config))
        except Exception as error:
            checks.append(
                GasCheck(
                    chain_id=chain_id,
                    ok=False,
                    message=f"native gas check failed on chain {chain_id}: {error}",
                )
            )

    failed = tuple(check for check in checks if not check.ok)
    if failed:
        raise GasPreflightError(checks)
    return rpc_urls, tuple(checks)


def _run_gas_checker(
    address: str,
    chain_id: int,
    rpc_url: str,
    config: object | None,
) -> GasCheck:
    checker = _config_value(config, "gas_checker")
    if checker is None:
        return _default_gas_check(address, chain_id, rpc_url, config)

    check_method = getattr(checker, "check", None)
    result = (
        check_method(address, chain_id, rpc_url, config)
        if callable(check_method)
        else checker(address, chain_id, rpc_url, config)
    )
    if isinstance(result, GasCheck):
        return result
    if isinstance(result, bool):
        return GasCheck(
            chain_id=chain_id,
            ok=result,
            message=(
                f"native gas available on chain {chain_id}"
                if result
                else f"insufficient native gas on chain {chain_id}"
            ),
        )
    raise TypeError("gas_checker must return GasCheck or bool")


def _default_gas_check(
    address: str,
    chain_id: int,
    rpc_url: str,
    config: object | None,
) -> GasCheck:
    required_wei = int(_config_value(config, "min_native_gas_wei") or 1)
    balance_wei = int(Web3(HTTPProvider(rpc_url)).eth.get_balance(address))
    ok = balance_wei >= required_wei
    return GasCheck(
        chain_id=chain_id,
        ok=ok,
        balance_wei=balance_wei,
        required_wei=required_wei,
        message=(
            f"native gas available on chain {chain_id}"
            if ok
            else f"insufficient native gas on chain {chain_id}"
        ),
    )


def _store_completed(store: object | None, key: str) -> bool:
    if store is None:
        return False

    is_completed = getattr(store, "is_completed", None)
    if callable(is_completed):
        return bool(is_completed(key))

    completed = getattr(store, "completed", None)
    if isinstance(completed, set | frozenset | list | tuple):
        return key in completed


    if isinstance(store, Mapping):
        return bool(store.get(key))


    contains = getattr(store, "__contains__", None)
    if callable(contains):
        return bool(key in store)  # type: ignore[operator]
    return False


def _store_mark_completed(
    store: object | None,
    key: str,
    value: object | None = None,
) -> None:
    if store is None:
        return

    mark_completed = getattr(store, "mark_completed", None)
    if callable(mark_completed):
        try:
            mark_completed(key, value)
        except TypeError:
            mark_completed(key)
        return

    complete = getattr(store, "complete", None)
    if callable(complete):
        try:
            complete(key, value)
        except TypeError:
            complete(key)
        return

    if isinstance(store, MutableMapping):
        store[key] = value if value is not None else True
        return

    completed = getattr(store, "completed", None)
    add = getattr(completed, "add", None)
    if callable(add):
        add(key)


def _mark_leg_if_complete(
    ref: _StepRef,
    step_refs: Sequence[_StepRef],
    store: object | None,
) -> None:
    leg_refs = [item for item in step_refs if item.leg_index == ref.leg_index]
    if leg_refs and all(
        _store_completed(store, item.idempotency_key) for item in leg_refs
    ):
        _store_mark_completed(store, _leg_key(ref.leg_index, ref.instrument_id), True)


def _write_checkpoint(
    config: object | None,
    stage: str,
    report: object,
    *,
    completed_keys: Iterable[str] = (),
) -> None:
    checkpoint_dir = _path_config_value(config, "checkpoint_dir")
    if checkpoint_dir is None:
        return
    status = getattr(report, "status", None)
    checkpoint_status: checkpoint_core.CheckpointStatus
    if status == "success":
        checkpoint_status = "completed"
    elif status == "failed":
        checkpoint_status = "failed"
    else:
        checkpoint_status = "in_progress"
    checkpoint_core.write_checkpoint(
        stage,
        checkpoint_status,
        report,
        checkpoint_dir=checkpoint_dir,
        artifact_type=f"{stage}-report",
        completed_keys=completed_keys,
    )


def _append_allocation_log(
    config: object | None,
    ref: _StepRef,
    receipt: Receipt,
) -> None:
    if ref.step.kind == "approve":
        return
    log_path = _path_config_value(config, "allocation_log_path")
    if log_path is None:
        return
    checkpoint_core.write_allocation_log_entry(
        instrument_id=ref.instrument_id,
        chain_id=ref.step.chain_id,
        action_type=ref.action_type,
        tx_hash=receipt.transaction_hash,
        usd=ref.usd,
        shares=ref.shares,
        log_path=log_path,
    )


def _path_config_value(config: object | None, attr: str) -> Path | None:
    value = _config_value(config, attr)
    if value is None:
        return None
    return Path(value)


def _completed_keys(
    step_refs: Sequence[_StepRef],
    execution_steps: Sequence[ExecutionStepReport],
) -> tuple[str, ...]:
    completed_step_keys = {
        step.idempotency_key
        for step in execution_steps
        if step.idempotency_key is not None and step.status in {"sent", "skipped"}
    }
    keys = set(completed_step_keys)
    for ref in step_refs:
        leg_refs = [item for item in step_refs if item.leg_index == ref.leg_index]
        if leg_refs and all(
            item.idempotency_key in completed_step_keys for item in leg_refs
        ):
            keys.add(ref.idempotency_key.rsplit(":step:", 1)[0])
    return tuple(sorted(keys))


def _leg_key(leg_index: int, instrument_id: str) -> str:
    return f"leg:{leg_index}:{instrument_id}"


def _step_key(leg_index: int, instrument_id: str, step_index: int) -> str:
    return f"leg:{leg_index}:{instrument_id}:step:{step_index}"


def _is_in_progress_payload(payload: object) -> bool:
    for value in _walk_values(payload):
        if isinstance(value, str) and value.casefold() in _PENDING_STATUSES:
            return True
    return False


def _messages(payloads: Sequence[object]) -> tuple[str, ...]:
    messages: list[str] = []
    for payload in payloads:
        if _is_in_progress_payload(payload):
            messages.append("cross-chain buy is in progress")
    return tuple(messages)


def _walk_values(value: object) -> Iterable[object]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield key
            yield from _walk_values(item)
        return
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        for item in value:
            yield from _walk_values(item)
        return
    yield value


__all__ = [
    "ExecutionBroadcastError",
    "ExecutionError",
    "ExecutionReport",
    "ExecutionStepReport",
    "GasCheck",
    "GasPreflightError",
    "IdempotencyStore",
    "PolicyCheckFailed",
    "TransactionPlanError",
    "execute_allocation",
    "pending_receipt_messages",
]
