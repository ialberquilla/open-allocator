"""Execution, split into a plan and the application of exactly that plan.

`plan_*` builds and prices a plan and broadcasts nothing. `apply_*` executes a plan
`plan_*` returned, as it stands: it never discovers, sizes or plans again, and it
refuses a plan the wallet has moved past. Approval sits between the two, which is
what lets a human approve the plan that then runs.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from open_allocator.core.policy_loader import load_policy
from open_allocator.core.state import ScopedIdempotencyStore, backend_from_config
from open_allocator.core.types import Allocation, Policy
from open_allocator.exec.allocation_plan import AllocationPlan
from open_allocator.exec.client import OneTxClient
from open_allocator.exec.config import AllocatorConfig
from open_allocator.exec.execute import (
    ExecutionReport,
    PolicyCheckFailed,
    apply_allocation_plan,
    check_allocation_policy,
    plan_allocation,
)
from open_allocator.service._common import JsonObject, model_payload, signer_from_config
from open_allocator.service.allocation import DEFAULT_POLICY_PATH, parse_allocation
from open_allocator.service.errors import ServiceError
from open_allocator.service.plan_store import PlanStore, plan_hash
from open_allocator.service.universe import OnWarning, discover_vaults_from_client

EXECUTE = "execute"


def idempotency_store(config: object, scope: str) -> ScopedIdempotencyStore | None:
    """The store that decides whether a step is re-sent, bound to its scope.

    Where it *lives* is `core.state`'s problem: on a laptop that is a JSON file,
    and in a container whose filesystem does not survive a retry it is whatever
    backend the caller injected. This is the seam that stops a retried run from
    broadcasting trades that already landed.
    """
    backend = backend_from_config(config, needs="idempotency_store_path")
    if backend is None:
        return None
    return ScopedIdempotencyStore(backend, scope)


def allocation_scope(allocation: Allocation) -> str:
    payload = allocation.model_dump(mode="json")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def plan_allocation_execution(
    allocation: Allocation | Mapping[str, Any],
    *,
    policy: Policy | Path = DEFAULT_POLICY_PATH,
    on_warning: OnWarning | None = None,
) -> AllocationPlan:
    """The deposit plan `execute` would submit for `allocation`, not sent."""
    allocation_model = parse_allocation(allocation)
    policy_model = policy if isinstance(policy, Policy) else load_policy(policy)
    config = AllocatorConfig()
    signer = signer_from_config(config)
    with OneTxClient(config) as client:
        known_instruments = discover_vaults_from_client(
            client, enrich=True, loops=True, on_warning=on_warning
        )
        return plan_allocation(
            client,
            signer,  # type: ignore[arg-type]
            allocation_model,
            policy_model,
            known_instruments=known_instruments,
            config=config,
            # Read only: legs already sent or bridging are not planned again.
            idempotency_store=idempotency_store(
                config, allocation_scope(allocation_model)
            ),
        )


def dry_run_report(planned: AllocationPlan) -> JsonObject:
    """The `execute` dry-run report: the plan, its preparation and blockers."""
    preparation = planned.preparation
    return model_payload(
        ExecutionReport(
            status="planned",
            policy_result=planned.policy_result,
            plan=planned.plan,
            preparations=preparation.preparations,
            funding=preparation.funding,
            loops=planned.loops,
            messages=(
                "dry-run only; no transactions broadcast",
                *planned.messages,
                *preparation.messages,
                *preparation.blockers,
            ),
        )
    )


def plan_execute(
    allocation: Allocation | Mapping[str, Any],
    *,
    policy: Policy | Path = DEFAULT_POLICY_PATH,
    on_warning: OnWarning | None = None,
) -> JsonObject:
    """`{"kind", "plan", "plan_hash", "report"}` for `allocation`; sends nothing.

    `plan` is what `apply_execute` takes, and `plan_hash` binds an approval to it.
    `report` is the dry run `execute` prints without `--confirm`.
    """
    planned = plan_allocation_execution(
        allocation, policy=policy, on_warning=on_warning
    )
    document = planned.model_dump(mode="json")
    return {
        "kind": EXECUTE,
        "plan": document,
        "plan_hash": plan_hash(EXECUTE, document),
        "report": dry_run_report(planned),
    }


def apply_execute(
    plan: AllocationPlan | Mapping[str, Any],
    *,
    expected_hash: str | None = None,
) -> JsonObject:
    """Execute a plan from `plan_execute` exactly; the `ExecutionReport` payload.

    With `expected_hash`, a plan that does not hash to it is refused before
    anything is read or sent.
    """
    document = (
        plan.model_dump(mode="json") if isinstance(plan, AllocationPlan) else dict(plan)
    )
    if expected_hash is not None and plan_hash(EXECUTE, document) != expected_hash:
        raise ServiceError(
            "plan_mismatch", "the plan does not match the approved plan hash"
        )
    planned = (
        plan
        if isinstance(plan, AllocationPlan)
        else AllocationPlan.model_validate(document)
    )
    config = AllocatorConfig()
    signer = signer_from_config(config)
    with OneTxClient(config) as client:
        report = apply_allocation_plan(
            client,
            signer,  # type: ignore[arg-type]
            planned,
            config=config,
            idempotency_store=idempotency_store(
                config, allocation_scope(planned.allocation)
            ),
        )
    return model_payload(report)


def recheck_execute_policy(
    plan: AllocationPlan | Mapping[str, Any],
    *,
    policy: Policy | Path = DEFAULT_POLICY_PATH,
    on_warning: OnWarning | None = None,
) -> JsonObject:
    """Check a stored plan's allocation against `policy` on today's shelf.

    The plan carries the policy result it was built under, against the policy the
    caller chose then. Approval checks it again against the operator's policy
    before anything is sent. Raises `ServiceError` `policy_violation`.
    """
    planned = (
        plan
        if isinstance(plan, AllocationPlan)
        else AllocationPlan.model_validate(plan)
    )
    policy_model = policy if isinstance(policy, Policy) else load_policy(policy)
    config = AllocatorConfig()
    signer = signer_from_config(config)
    with OneTxClient(config) as client:
        known_instruments = discover_vaults_from_client(
            client, enrich=True, loops=True, on_warning=on_warning
        )
        try:
            result = check_allocation_policy(
                client,
                signer,  # type: ignore[arg-type]
                planned.allocation,
                policy_model,
                known_instruments=known_instruments,
                config=config,
                idempotency_store=idempotency_store(
                    config, allocation_scope(planned.allocation)
                ),
            )
        except PolicyCheckFailed as failed:
            raise ServiceError("policy_violation", str(failed)) from failed
    return model_payload(result)


# What applies each kind of stored plan, and what re-checks its policy first.
_APPLY: dict[str, Callable[..., JsonObject]] = {EXECUTE: apply_execute}
_RECHECK: dict[str, Callable[..., JsonObject]] = {EXECUTE: recheck_execute_policy}


def propose(store: PlanStore, proposal: Mapping[str, Any]) -> JsonObject:
    """Store a `plan_*` proposal for approval; what an execution tool returns.

    The model sees the plan and its hash, never a way to apply it.
    """
    stored = store.put(proposal["kind"], proposal["plan"])
    if stored.plan_hash != proposal["plan_hash"]:
        raise ServiceError("plan_mismatch", "the stored plan hashes differently")
    return {
        "plan_required": True,
        "kind": stored.kind,
        "plan_hash": stored.plan_hash,
        "expires_at": stored.expires_at.isoformat(),
        "plan": proposal["report"],
    }


def apply_approved(
    store: PlanStore,
    approved_hash: str,
    *,
    policy: Policy | Path | None = None,
) -> JsonObject:
    """Apply the stored plan a human approved by hash, at most once.

    The plan is marked used before anything else, so a repeated approval cannot
    send it twice; a refused or failed run needs a new plan. With `policy`, the
    plan's allocation is checked against it on today's shelf before it is
    applied, and refused on a violation.
    """
    stored = store.take(approved_hash)
    apply = _APPLY.get(stored.kind)
    if apply is None:
        raise ServiceError("invalid_input", f"no plan kind {stored.kind}")
    if plan_hash(stored.kind, stored.plan) != approved_hash:
        raise ServiceError(
            "plan_mismatch", "the stored plan does not match the approved hash"
        )
    if policy is not None:
        _RECHECK[stored.kind](stored.plan, policy=policy)
    return apply(stored.plan, expected_hash=approved_hash)
