from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from functools import wraps
from pathlib import Path
from typing import Annotated, Any, ParamSpec, TypeVar

import typer

from open_allocator.core import allocator as allocation_core
from open_allocator.core import backtest as backtest_core
from open_allocator.core import costs as costs_core
from open_allocator.core import eligibility, universe
from open_allocator.core import policy as policy_core
from open_allocator.core import positions as positions_core
from open_allocator.core import riskmetrics as riskmetrics_core
from open_allocator.core import screen as screen_core
from open_allocator.core import simulate as simulate_core
from open_allocator.core import strategies as strategies_core
from open_allocator.core.metrics import enrich as enrich_vaults
from open_allocator.core.policy_loader import load_policy
from open_allocator.core.schema import validate
from open_allocator.core.scoring import score_vault as score_vault_model
from open_allocator.core.types import (
    Allocation,
    Policy,
    TxPlan,
    Vault,
    VaultScore,
)
from open_allocator.exec import chains, safe_deployment
from open_allocator.exec.client import OneTxClient
from open_allocator.exec.config import AllocatorConfig, ReadOnlyOneTxConfig

JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None
JsonObject = dict[str, Any]
P = ParamSpec("P")
R = TypeVar("R", bound=JsonValue)

app = typer.Typer(no_args_is_help=True)


class VaultSort(StrEnum):
    APY = "apy"
    TVL = "tvl"
    SCORE = "score"


class RiskPreset(StrEnum):
    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"


DEFAULT_POLICY_PATH = Path("policy.yaml")


def _write_json(payload: JsonValue, *, err: bool = False) -> None:
    typer.echo(json.dumps(payload, separators=(",", ":")), err=err)


def _not_implemented() -> JsonObject:
    return {"status": "not_implemented"}


def _execution_plan(command_name: str) -> JsonObject:
    return {
        "status": "plan_required",
        "command": command_name,
        "requires": "--confirm or explicit --unsafe/--autonomous",
    }


def json_command(
    func: Callable[P, R] | None = None,
    *,
    execution_command: bool = False,
    command_name: str | None = None,
) -> Callable[[Callable[P, R]], Callable[P, None]] | Callable[P, None]:
    def decorator(inner: Callable[P, R]) -> Callable[P, None]:
        @wraps(inner)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
            try:
                if execution_command and not (
                    kwargs.get("confirm")
                    or kwargs.get("unsafe")
                    or kwargs.get("autonomous")
                ):
                    name = command_name or inner.__name__.replace("_", "-")
                    result = _execution_plan(name)
                else:
                    result = inner(*args, **kwargs)
                _write_json(result)
            except Exception as error:
                _write_json({"error": str(error)}, err=True)
                raise typer.Exit(1) from error

        return wrapper

    if func is None:
        return decorator
    return decorator(func)


def _execute_executor() -> JsonObject:
    return _not_implemented()


def _rebalance_executor() -> JsonObject:
    return _not_implemented()


def _withdraw_executor(
    position: str | None,
    positions_path: Path | None,
    policy_path: Path,
    *,
    amount: float | None,
    confirm: bool,
) -> JsonObject:
    return _withdraw_from_cli(
        position,
        positions_path,
        policy_path,
        amount=amount,
        confirm=confirm,
    )


def _discover_vaults(*, enrich: bool = False) -> list[Vault]:
    with OneTxClient(ReadOnlyOneTxConfig()) as client:
        return _discover_vaults_from_client(client, enrich=enrich)


def _discover_vaults_from_client(
    client: object,
    *,
    enrich: bool = False,
) -> list[Vault]:
    vaults = universe.discover(client)
    if enrich:
        return enrich_vaults(client, vaults, days=30)
    return vaults


def _filter_vaults(
    vaults: list[Vault],
    *,
    chain: int | None,
    asset: str | None,
    protocol: str | None,
) -> list[Vault]:
    return [
        vault
        for vault in vaults
        if (chain is None or vault.chain_id == chain)
        and (asset is None or vault.asset.casefold() == asset.casefold())
        and (protocol is None or vault.protocol.casefold() == protocol.casefold())
    ]


def _score_by_instrument(vaults: list[Vault]) -> dict[str, VaultScore]:
    return {vault.instrument_id: score_vault_model(vault) for vault in vaults}


def _read_allocation(path: Path) -> Allocation:
    with path.open(encoding="utf-8") as file:
        payload = json.load(file)

    validate(payload, "allocation")
    return Allocation.model_validate(payload)


def _load_allocation_spec(path: Path) -> JsonObject:
    with path.open(encoding="utf-8") as file:
        payload = json.load(file)

    validate(payload, "allocation-spec")
    return payload


def _read_positions(path: Path) -> positions_core.Positions:
    with path.open(encoding="utf-8") as file:
        payload = json.load(file)

    return positions_core.Positions.model_validate(payload)


def _read_position_source(
    path: Path,
) -> positions_core.Positions | positions_core.PositionHolding:
    with path.open(encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, Mapping):
        raise TypeError("position file must contain a JSON object")
    if "holdings" in payload:
        return positions_core.Positions.model_validate(payload)
    return positions_core.PositionHolding.model_validate(payload)


def signer_from_config(config: object) -> object:
    from open_allocator.exec.signer import signer_from_config as factory

    return factory(config)


def execute_allocation(*args: object, **kwargs: object) -> object:
    from open_allocator.exec.execute import execute_allocation as executor

    return executor(*args, **kwargs)


def execute_rebalance(*args: object, **kwargs: object) -> object:
    from open_allocator.exec.rebalance import execute_rebalance as executor

    return executor(*args, **kwargs)


def execute_withdraw(*args: object, **kwargs: object) -> object:
    from open_allocator.exec.withdraw import withdraw as executor

    return executor(*args, **kwargs)


def _execution_report(
    *,
    status: str,
    policy_result: object,
    plan: TxPlan,
    messages: tuple[str, ...] = (),
) -> object:
    from open_allocator.exec.execute import ExecutionReport

    return ExecutionReport(
        status=status,
        policy_result=policy_result,
        plan=plan,
        messages=messages,
    )


def _model_payload(value: object) -> JsonObject:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        payload = model_dump(mode="json")
    elif isinstance(value, Mapping):
        payload = dict(value)
    elif hasattr(value, "__dict__"):
        payload = vars(value)
    else:
        payload = value
    if not isinstance(payload, dict):
        raise TypeError("expected JSON object payload")
    return payload


class _JsonIdempotencyStore:
    def __init__(self, path: Path, scope: str) -> None:
        self._path = path
        self._scope = scope

    def is_completed(self, key: str) -> bool:
        return key in self._scope_data(self._read())

    def mark_completed(self, key: str, value: object | None = None) -> None:
        payload = self._read()
        scope_data = self._scope_data(payload)
        entry: JsonObject = {"completed": True}
        if value is not None:
            entry["value"] = _json_safe(value)
        scope_data[key] = entry
        self._write(payload)

    def _read(self) -> JsonObject:
        if not self._path.exists():
            return {"version": 1, "scopes": {}}

        with self._path.open(encoding="utf-8") as file:
            payload = json.load(file)
        if not isinstance(payload, dict):
            raise TypeError("idempotency store must contain a JSON object")
        payload.setdefault("version", 1)
        payload.setdefault("scopes", {})
        return payload

    def _write(self, payload: JsonObject) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._path.with_name(f".{self._path.name}.tmp")
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, sort_keys=True, separators=(",", ":"))
        temp_path.replace(self._path)

    def _scope_data(self, payload: JsonObject) -> JsonObject:
        scopes = payload.setdefault("scopes", {})
        if not isinstance(scopes, dict):
            raise TypeError("idempotency store scopes must be a JSON object")
        scope = scopes.setdefault(self._scope, {})
        if not isinstance(scope, dict):
            raise TypeError("idempotency store scope must be a JSON object")
        return scope


def _execution_idempotency_store(
    config: object,
    allocation: Allocation,
) -> _JsonIdempotencyStore | None:
    path = getattr(config, "idempotency_store_path", None)
    if path is None:
        return None
    scope = _allocation_scope(allocation)
    return _JsonIdempotencyStore(Path(path), scope)


def _rebalance_idempotency_store(
    config: object,
    positions: positions_core.Positions,
    target: Allocation,
    *,
    min_trade_usd: float,
) -> _JsonIdempotencyStore | None:
    path = getattr(config, "idempotency_store_path", None)
    if path is None:
        return None
    scope = _rebalance_scope(positions, target, min_trade_usd=min_trade_usd)
    return _JsonIdempotencyStore(Path(path), scope)


def _withdraw_idempotency_store(
    config: object,
    position: positions_core.PositionHolding,
    *,
    amount: float | None,
) -> _JsonIdempotencyStore | None:
    path = getattr(config, "idempotency_store_path", None)
    if path is None:
        return None
    scope = _withdraw_scope(position, amount=amount)
    return _JsonIdempotencyStore(Path(path), scope)


def _allocation_scope(allocation: Allocation) -> str:
    payload = allocation.model_dump(mode="json")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _rebalance_scope(
    positions: positions_core.Positions,
    target: Allocation,
    *,
    min_trade_usd: float,
) -> str:
    payload = {
        "positions": positions.model_dump(mode="json"),
        "target": target.model_dump(mode="json"),
        "min_trade_usd": min_trade_usd,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _withdraw_scope(
    position: positions_core.PositionHolding,
    *,
    amount: float | None,
) -> str:
    payload = {
        "position": position.model_dump(mode="json"),
        "amount": amount,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _json_safe(value: object) -> JsonValue:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_json_safe(item) for item in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def _build_execution_plan(
    allocation_path: Path,
    policy_path: Path,
) -> tuple[Allocation, Policy, TxPlan, list[Vault]]:
    allocation = _read_allocation(allocation_path)
    policy = load_policy(policy_path)
    config = AllocatorConfig()
    signer = signer_from_config(config)

    with OneTxClient(config) as client:
        known_instruments = _discover_vaults_from_client(client, enrich=True)
        plan = execute_allocation(
            client,
            signer,
            allocation,
            policy,
            confirm=False,
            known_instruments=known_instruments,
            config=config,
        )

    if not isinstance(plan, TxPlan):
        raise TypeError("execute_allocation(confirm=False) did not return a TxPlan")
    return allocation, policy, plan, known_instruments


def _execute_allocation_from_cli(
    allocation_path: Path,
    policy_path: Path,
    *,
    confirm: bool,
) -> JsonObject:
    if not confirm:
        allocation, policy, plan, known_instruments = _build_execution_plan(
            allocation_path,
            policy_path,
        )
        policy_result = policy_core.check(allocation, policy, known_instruments)
        report = _execution_report(
            status="planned",
            policy_result=policy_result,
            plan=plan,
            messages=("dry-run only; no transactions broadcast",),
        )
        return _model_payload(report)

    allocation = _read_allocation(allocation_path)
    policy = load_policy(policy_path)
    config = AllocatorConfig()
    signer = signer_from_config(config)

    with OneTxClient(config) as client:
        known_instruments = _discover_vaults_from_client(client, enrich=True)
        report = execute_allocation(
            client,
            signer,
            allocation,
            policy,
            confirm=True,
            known_instruments=known_instruments,
            config=config,
            idempotency_store=_execution_idempotency_store(config, allocation),
        )

    return _model_payload(report)


def _rebalance_from_cli(
    current_path: Path,
    target_path: Path,
    policy_path: Path,
    *,
    confirm: bool,
    autonomous: bool,
    min_trade_usd: float,
) -> JsonObject:
    current = _read_positions(current_path)
    target = _read_allocation(target_path)
    policy = load_policy(policy_path)
    config = AllocatorConfig()
    signer = signer_from_config(config)

    with OneTxClient(config) as client:
        known_instruments = _discover_vaults_from_client(client, enrich=True)
        report = execute_rebalance(
            client,
            signer,
            current,
            target,
            policy,
            confirm=confirm,
            autonomous=autonomous,
            known_instruments=known_instruments,
            config=config,
            idempotency_store=(
                _rebalance_idempotency_store(
                    config,
                    current,
                    target,
                    min_trade_usd=min_trade_usd,
                )
                if confirm or autonomous
                else None
            ),
            min_trade_usd=min_trade_usd,
        )

    return _model_payload(report)


def _withdraw_from_cli(
    position: str | None,
    positions_path: Path | None,
    policy_path: Path,
    *,
    amount: float | None,
    confirm: bool,
) -> JsonObject:
    if not confirm:
        return _execution_plan("withdraw")
    if position is None:
        raise ValueError("--position is required with --confirm")

    policy = load_policy(policy_path)
    config = AllocatorConfig()
    signer = signer_from_config(config)

    with OneTxClient(config) as client:
        holding = _withdraw_position_from_cli(
            client,
            signer,
            position,
            positions_path,
        )
        report = execute_withdraw(
            client,
            signer,
            holding,
            policy,
            amount=amount,
            confirm=True,
            config=config,
            idempotency_store=_withdraw_idempotency_store(
                config,
                holding,
                amount=amount,
            ),
        )

    return _model_payload(report)


def _withdraw_position_from_cli(
    client: object,
    signer: object,
    position: str,
    positions_path: Path | None,
) -> positions_core.PositionHolding:
    if positions_path is not None:
        return _select_position(_read_position_source(positions_path), position)

    candidate_path = Path(position)
    if candidate_path.exists() and candidate_path.is_file():
        source = _read_position_source(candidate_path)
        if isinstance(source, positions_core.PositionHolding):
            return source
        if len(source.holdings) == 1:
            return source.holdings[0]
        raise ValueError(
            "position file has multiple holdings; pass --positions and --position <id>"
        )

    address_method = getattr(signer, "address", None)
    if not callable(address_method):
        raise TypeError("signer does not implement address()")
    current = positions_core.read_positions(client, str(address_method()))
    return _select_position(current, position)


def _select_position(
    source: positions_core.Positions | positions_core.PositionHolding,
    position_id: str,
) -> positions_core.PositionHolding:
    if isinstance(source, positions_core.PositionHolding):
        if source.instrument_id != position_id:
            raise ValueError(f"position not found: {position_id}")
        return source

    matches = [
        holding for holding in source.holdings if holding.instrument_id == position_id
    ]
    if not matches:
        raise ValueError(f"position not found: {position_id}")
    if len(matches) > 1:
        raise ValueError(f"position id is ambiguous: {position_id}")
    return matches[0]


def _wallet_status_payload() -> JsonObject:
    config = AllocatorConfig()
    signer = signer_from_config(config)
    address_method = getattr(signer, "address", None)
    if not callable(address_method):
        raise TypeError("signer does not implement address()")
    address = str(address_method())

    with OneTxClient(config) as client:
        balances_response = client.balances(address)

    balances_payload = _normalize_balances_response(balances_response)
    balances = []
    for balance in balances_payload["balances"]:
        chain_id = int(balance["chain_id"])
        balances.append(
            {
                **balance,
                **_native_gas_status(address, chain_id, config),
            }
        )

    return {
        "address": address,
        "balances": balances,
        "total_usdc_usd": balances_payload.get("total_usdc_usd"),
    }


def _safe_seed_from_config(config: AllocatorConfig) -> safe_deployment.SafeSeed:
    if config.safe_owners is None or config.safe_threshold is None:
        raise ValueError(
            "safe-address needs SAFE_OWNERS + SAFE_THRESHOLD to derive the "
            "counterfactual address"
        )
    return safe_deployment.SafeSeed(
        owners=config.safe_owners,
        threshold=config.safe_threshold,
        salt_nonce=config.safe_salt_nonce,
    )


def _safe_address_payload(chain_ids: tuple[int, ...] | None) -> JsonObject:
    from web3 import HTTPProvider, Web3

    config = AllocatorConfig()
    if config.account != "safe":
        raise ValueError("safe-address requires SIGNER_ACCOUNT=safe")

    targets = chain_ids or (
        (config.safe_chain_id,) if config.safe_chain_id is not None else ()
    )
    if not targets:
        raise ValueError("no chain to report; pass --chain or set SAFE_CHAIN_ID")

    seed = _safe_seed_from_config(config)
    predicted: str | None = config.safe_address
    per_chain: list[JsonObject] = []

    for chain_id in targets:
        entry: JsonObject = {"chain_id": chain_id, "chain": chains.chain_name(chain_id)}
        try:
            rpc_url = chains.require_rpc_url(chain_id, config)
            w3 = Web3(HTTPProvider(rpc_url))
            status = safe_deployment.deployment_status(w3, seed, chain_id=chain_id)
            entry["address"] = status.address
            entry["deployed"] = status.deployed
            predicted = predicted or status.address
        except Exception as error:
            entry["error"] = str(error)
            entry["deployed"] = None
        per_chain.append(entry)

    return {
        "address": predicted,
        "owners": list(seed.owners),
        "threshold": seed.threshold,
        "salt_nonce": seed.salt_nonce,
        "safe_version": safe_deployment.SAFE_VERSION,
        "chains": per_chain,
    }


def _positions_payload(address: str | None) -> JsonObject:
    if address is None:
        config = AllocatorConfig()
        signer = signer_from_config(config)
        address_method = getattr(signer, "address", None)
        if not callable(address_method):
            raise TypeError("signer does not implement address()")
        address = str(address_method())
    else:
        config = ReadOnlyOneTxConfig()

    with OneTxClient(config) as client:
        return positions_core.read_positions(client, address).model_dump(mode="json")


def _normalize_balances_response(response: object) -> JsonObject:
    payload = _model_payload(response)
    raw_balances = payload.get("balances", [])
    if not isinstance(raw_balances, Sequence) or isinstance(
        raw_balances,
        str | bytes | bytearray,
    ):
        raise TypeError("balances response did not contain a balances array")

    balances: list[JsonObject] = []
    for raw_balance in raw_balances:
        balance_payload = _model_payload(raw_balance)
        chain_id = int(_mapping_value(balance_payload, "chain_id", "chainId"))
        balances.append(
            {
                "chain_id": chain_id,
                "chain_name": str(
                    _mapping_value(
                        balance_payload,
                        "chain_name",
                        "chainName",
                        default=chains.chain_name(chain_id),
                    )
                ),
                "usdc_balance": str(
                    _mapping_value(balance_payload, "usdc_balance", "usdcBalance")
                ),
                "usdc_balance_raw": str(
                    _mapping_value(
                        balance_payload,
                        "usdc_balance_raw",
                        "usdcBalanceRaw",
                    )
                ),
            }
        )

    return {
        "balances": balances,
        "total_usdc_usd": _mapping_value(
            payload,
            "total_usdc_usd",
            "totalUsdcUsd",
            default=None,
        ),
    }


def _mapping_value(
    mapping: JsonObject,
    *keys: str,
    default: object = Ellipsis,
) -> object:
    for key in keys:
        if key in mapping:
            return mapping[key]
    if default is not Ellipsis:
        return default
    raise KeyError(keys[0])


def _native_gas_status(address: str, chain_id: int, config: object) -> JsonObject:
    required_wei = int(getattr(config, "min_native_gas_wei", 1))
    rpc_url = chains.rpc_url(chain_id, config)
    if rpc_url is None:
        return {
            "rpc_available": False,
            "rpc_executable": False,
            "native_gas_balance_wei": None,
            "native_gas_required_wei": required_wei,
            "native_gas_available": False,
            "executable": False,
            "not_executable": True,
            "not_executable_reasons": ["missing_rpc"],
        }

    try:
        from web3 import HTTPProvider, Web3

        balance_wei = int(Web3(HTTPProvider(rpc_url)).eth.get_balance(address))
    except Exception as error:
        return {
            "rpc_available": True,
            "rpc_executable": False,
            "native_gas_balance_wei": None,
            "native_gas_required_wei": required_wei,
            "native_gas_available": False,
            "executable": False,
            "not_executable": True,
            "not_executable_reasons": ["rpc_error"],
            "message": str(error),
        }

    gas_available = balance_wei >= required_wei
    return {
        "rpc_available": True,
        "rpc_executable": True,
        "native_gas_balance_wei": balance_wei,
        "native_gas_required_wei": required_wei,
        "native_gas_available": gas_available,
        "executable": gas_available,
        "not_executable": not gas_available,
        "not_executable_reasons": [] if gas_available else ["insufficient_native_gas"],
    }


def _policy_candidate_vaults(
    vaults: Sequence[Vault],
    policy: Policy,
) -> tuple[list[Vault], list[str]]:
    candidates: list[Vault] = []
    exclusions: list[str] = []

    for vault in vaults:
        rule = _policy_exclusion_rule(vault, policy)
        if rule is None:
            candidates.append(vault)
        else:
            exclusions.append(f"policy_excluded:{vault.instrument_id}:{rule}")

    return candidates, exclusions


def _policy_exclusion_rule(vault: Vault, policy: Policy) -> str | None:
    # Single source of truth for per-vault policy eligibility.
    return eligibility.candidate_exclusion(vault, policy)


def _policy_violation_summary(result: policy_core.PolicyResult) -> list[str]:
    return [
        f"{violation.rule}:{violation.entity}:limit={violation.limit}:actual={violation.actual}"
        for violation in result.violations
    ]


def _allocation_payload_with_policy_result(
    allocation: Allocation,
    result: policy_core.PolicyResult,
    *,
    discovered: Sequence[Vault],
    candidates: Sequence[Vault],
    exclusions: Sequence[str],
    cost_estimate: costs_core.CostEstimate | None = None,
) -> JsonObject:
    metadata = dict(allocation.metadata)
    warnings = [str(item) for item in metadata.get("warnings", [])]
    warnings.extend(exclusions)
    if cost_estimate is not None:
        metadata["cost_estimate"] = cost_estimate.as_metadata()
        cost_warning = cost_estimate.warning()
        if cost_warning is not None:
            warnings.append(cost_warning)
    metadata.update(
        {
            "warnings": sorted(set(warnings)),
            "policy_ok": result.ok,
            "policy_violations": _policy_violation_summary(result),
            "discovered_instruments": [vault.instrument_id for vault in discovered],
            "candidate_instruments": [vault.instrument_id for vault in candidates],
        }
    )
    payload = allocation.model_copy(update={"metadata": metadata}).model_dump(
        mode="json"
    )
    validate(payload, "allocation")
    return payload


def _vault_summary(vault: Vault, score: VaultScore) -> JsonObject:
    return {
        "instrument_id": vault.instrument_id,
        "protocol": vault.protocol,
        "chain_id": vault.chain_id,
        "asset": vault.asset,
        "apy": vault.apy,
        "tvl_usd": vault.tvl_usd,
        "score": score.score,
        "risk_metrics": _risk_metrics(vault),
    }


def _vault_score_payload(score: VaultScore, vault: Vault) -> JsonObject:
    payload = score.model_dump(mode="json")
    payload["risk_metrics"] = _risk_metrics(vault)
    validate(payload, "vault-score")
    return payload


def _risk_metrics(vault: Vault) -> JsonObject:
    # Yield-path risk only; never principal/depeg/contract loss. Unknown stays
    # Unknown when history is insufficient.
    return {
        name: _json_safe(value)
        for name, value in riskmetrics_core.summary(vault).items()
    }


def _parse_pins(pins: list[str] | None) -> dict[str, float] | None:
    if not pins:
        return None
    parsed: dict[str, float] = {}
    for item in pins:
        instrument_id, sep, weight = item.partition("=")
        if not sep or not instrument_id.strip():
            raise ValueError(f"--pin must be 'instrument_id=weight', got: {item!r}")
        try:
            parsed[instrument_id.strip()] = float(weight)
        except ValueError as error:
            raise ValueError(f"--pin weight must be a number, got: {item!r}") from error
    return parsed


def _parse_strategy_params(params: list[str] | None) -> dict[str, Any] | None:
    if not params:
        return None
    parsed: dict[str, Any] = {}
    for item in params:
        key, sep, raw = item.partition("=")
        if not sep or not key.strip():
            raise ValueError(
                f"--strategy-param must be 'key=value', got: {item!r}"
            )
        parsed[key.strip()] = _coerce_scalar(raw.strip())
    return parsed


def _coerce_scalar(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


ConfirmOption = Annotated[bool, typer.Option("--confirm")]
UnsafeOption = Annotated[bool, typer.Option("--unsafe")]
AutonomousOption = Annotated[bool, typer.Option("--autonomous")]

# Advisory risk-screening options (shared by build-allocation and screen).
MinSharpeOption = Annotated[
    float | None,
    typer.Option("--min-sharpe", help="Screen: drop below this Sharpe (Unknown fails)"),
]
MaxDrawdownOption = Annotated[
    float | None,
    typer.Option(
        "--max-drawdown",
        min=0,
        help="Screen: max tolerated NAV dip magnitude (0.1 == 10%).",
    ),
]
MaxRewardDependenceOption = Annotated[
    float | None,
    typer.Option(
        "--max-reward-dependence",
        min=0,
        help="Screen: drop above this reward dependence (Unknown fails).",
    ),
]
MinHistoryDaysOption = Annotated[
    int | None,
    typer.Option("--min-history-days", min=0, help="Screen: require N days history."),
]
ScreenCuratorOption = Annotated[
    list[str] | None,
    typer.Option("--screen-curator", help="Screen: curator allowlist (repeatable)."),
]
MinScreenTvlOption = Annotated[
    float | None,
    typer.Option("--min-tvl-usd", min=0, help="Screen: minimum TVL in USD."),
]


def _screen_criteria(
    *,
    min_sharpe: float | None,
    max_drawdown: float | None,
    max_reward_dependence: float | None,
    min_history_days: int | None,
    curators: list[str] | None,
    min_tvl_usd: float | None,
) -> screen_core.ScreenCriteria:
    return screen_core.ScreenCriteria(
        min_sharpe=min_sharpe,
        max_drawdown=max_drawdown,
        max_reward_dependence=max_reward_dependence,
        min_history_days=min_history_days,
        curators=tuple(curators) if curators else None,
        min_tvl_usd=min_tvl_usd,
    )


@app.command("wallet-status")
@json_command
def wallet_status() -> JsonObject:
    return _wallet_status_payload()


@app.command("safe-address")
@json_command
def safe_address(
    chain: Annotated[
        list[int] | None,
        typer.Option(
            "--chain",
            help="Chain to report; repeatable. Defaults to SAFE_CHAIN_ID.",
        ),
    ] = None,
) -> JsonObject:
    return _safe_address_payload(tuple(chain) if chain else None)


@app.command("list-vaults")
@json_command
def list_vaults(
    chain: Annotated[int | None, typer.Option("--chain")] = None,
    asset: Annotated[str | None, typer.Option("--asset")] = None,
    protocol: Annotated[str | None, typer.Option("--protocol")] = None,
    sort: Annotated[VaultSort | None, typer.Option("--sort")] = None,
) -> list[JsonObject]:
    vaults = _filter_vaults(
        _discover_vaults(enrich=True),
        chain=chain,
        asset=asset,
        protocol=protocol,
    )
    scores = _score_by_instrument(vaults)

    if sort == VaultSort.APY:
        vaults.sort(key=lambda vault: vault.apy, reverse=True)
    elif sort == VaultSort.TVL:
        vaults.sort(key=lambda vault: vault.tvl_usd, reverse=True)
    elif sort == VaultSort.SCORE:
        vaults.sort(key=lambda vault: scores[vault.instrument_id].score, reverse=True)

    return [
        _vault_summary(vault, scores[vault.instrument_id])
        for vault in vaults
    ]


@app.command("score-vault")
@json_command
def score_vault(
    instrument_id: Annotated[str, typer.Option("--instrument-id")],
) -> JsonObject:
    for vault in _discover_vaults(enrich=True):
        if vault.instrument_id == instrument_id:
            return _vault_score_payload(score_vault_model(vault), vault)

    raise ValueError(f"instrument not found: {instrument_id}")


@app.command("build-allocation")
@json_command
def build_allocation(
    amount: Annotated[float | None, typer.Option("--amount", min=0)] = None,
    risk: Annotated[RiskPreset, typer.Option("--risk")] = RiskPreset.BALANCED,
    policy_path: Annotated[
        Path,
        typer.Option("--policy", dir_okay=False, readable=True),
    ] = DEFAULT_POLICY_PATH,
    spec: Annotated[
        Path | None,
        typer.Option(
            "--spec",
            exists=True,
            dir_okay=False,
            readable=True,
            help="Allocation-spec JSON (weights or strategy+params+selection).",
        ),
    ] = None,
    strategy: Annotated[
        str,
        typer.Option("--strategy", help="Allocation strategy (see --strategy list)."),
    ] = allocation_core.DEFAULT_STRATEGY,
    strategy_param: Annotated[
        list[str] | None,
        typer.Option(
            "--strategy-param",
            help="Strategy param 'key=value' (repeatable); value is a JSON scalar.",
        ),
    ] = None,
    min_sharpe: MinSharpeOption = None,
    max_drawdown: MaxDrawdownOption = None,
    max_reward_dependence: MaxRewardDependenceOption = None,
    min_history_days: MinHistoryDaysOption = None,
    screen_curator: ScreenCuratorOption = None,
    min_tvl_usd: MinScreenTvlOption = None,
    max_positions: Annotated[
        int | None,
        typer.Option("--max-positions", min=1, help="Keep only the top-N positions."),
    ] = None,
    min_position_usd: Annotated[
        float | None,
        typer.Option(
            "--min-position-usd",
            min=0,
            help="Drop legs below this USD size (dust).",
        ),
    ] = None,
    score_power: Annotated[
        float | None,
        typer.Option("--score-power", min=0, help="Override preset score exponent."),
    ] = None,
    apy_weight: Annotated[
        float | None,
        typer.Option("--apy-weight", min=0, help="Override the preset APY tilt."),
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option("--exclude", help="Instrument id to veto (repeatable)."),
    ] = None,
    pin: Annotated[
        list[str] | None,
        typer.Option(
            "--pin",
            help="Pin a weight as 'instrument_id=weight' (repeatable).",
        ),
    ] = None,
    source_chain_id: Annotated[
        int | None,
        typer.Option(
            "--source-chain-id",
            help="Chain the wallet's USDC is funded on, for the cost estimate. "
            "Defaults to the chain holding the largest share of the deploy.",
        ),
    ] = None,
) -> JsonObject:
    if strategy in {"list", "help"}:
        # Discovery convenience: enumerate strategies without needing --amount.
        return {"strategies": list(strategies_core.available())}
    risk_value = risk.value
    strategy_params = _parse_strategy_params(strategy_param)
    overrides = _parse_pins(pin)
    if spec is not None:
        spec_data = _load_allocation_spec(spec)
        selection = spec_data.get("selection", {})
        amount = amount if amount is not None else spec_data.get("amount_usd")
        risk_value = spec_data.get("risk", risk_value)
        strategy = spec_data.get("strategy", strategy)
        strategy_params = spec_data.get("params", strategy_params)
        weights = spec_data.get("weights")
        if weights:
            overrides = {str(key): float(value) for key, value in weights.items()}
        exclude = selection.get("exclude", exclude)
        max_positions = selection.get("max_positions", max_positions)
        min_position_usd = selection.get("min_position_usd", min_position_usd)
        criteria = _screen_criteria(
            min_sharpe=selection.get("min_sharpe"),
            max_drawdown=selection.get("max_drawdown"),
            max_reward_dependence=selection.get("max_reward_dependence"),
            min_history_days=selection.get("min_history_days"),
            curators=selection.get("curators"),
            min_tvl_usd=selection.get("min_tvl_usd"),
        )
    else:
        criteria = _screen_criteria(
            min_sharpe=min_sharpe,
            max_drawdown=max_drawdown,
            max_reward_dependence=max_reward_dependence,
            min_history_days=min_history_days,
            curators=screen_curator,
            min_tvl_usd=min_tvl_usd,
        )

    if amount is None:
        raise ValueError(
            "amount required: pass --amount or set amount_usd in the spec"
        )

    policy = load_policy(policy_path)
    discovered = _discover_vaults(enrich=True)
    candidates, exclusions = _policy_candidate_vaults(discovered, policy)
    if criteria.active:
        screened = screen_core.screen(candidates, criteria)
        candidates = list(screened.kept)
        exclusions = [*exclusions, *screened.warnings()]
    scores = _score_by_instrument(discovered)
    allocation = allocation_core.build_allocation(
        [(vault, scores[vault.instrument_id]) for vault in candidates],
        amount,
        risk=risk_value,
        caps=policy.caps,
        strategy=strategy,
        strategy_params=strategy_params,
        max_positions=max_positions,
        min_position_usd=min_position_usd,
        overrides=overrides,
        exclude=exclude,
        score_power=score_power,
        apy_weight=apy_weight,
    )
    result = policy_core.check(allocation, policy, discovered)
    cost_estimate = costs_core.estimate_from_allocation_legs(
        [leg.model_dump() for leg in allocation.legs],
        chain_by_instrument={v.instrument_id: v.chain_id for v in discovered},
        apy_by_instrument={v.instrument_id: v.apy for v in discovered},
        source_chain_id=source_chain_id,
    )
    return _allocation_payload_with_policy_result(
        allocation,
        result,
        discovered=discovered,
        candidates=candidates,
        exclusions=exclusions,
        cost_estimate=cost_estimate,
    )


@app.command("screen")
@json_command
def screen(
    min_sharpe: MinSharpeOption = None,
    max_drawdown: MaxDrawdownOption = None,
    max_reward_dependence: MaxRewardDependenceOption = None,
    min_history_days: MinHistoryDaysOption = None,
    screen_curator: ScreenCuratorOption = None,
    min_tvl_usd: MinScreenTvlOption = None,
) -> JsonObject:
    """Advisory metric screen over the live universe.

    Narrows only; policy (``check-policy``) still applies downstream and cannot
    be loosened by any screen.
    """
    criteria = _screen_criteria(
        min_sharpe=min_sharpe,
        max_drawdown=max_drawdown,
        max_reward_dependence=max_reward_dependence,
        min_history_days=min_history_days,
        curators=screen_curator,
        min_tvl_usd=min_tvl_usd,
    )
    discovered = _discover_vaults(enrich=True)
    scores = _score_by_instrument(discovered)
    result = screen_core.screen(discovered, criteria)
    return {
        "label": "advisory-not-policy",
        "criteria": {
            "min_sharpe": criteria.min_sharpe,
            "max_drawdown": criteria.max_drawdown,
            "max_reward_dependence": criteria.max_reward_dependence,
            "min_history_days": criteria.min_history_days,
            "curators": list(criteria.curators) if criteria.curators else None,
            "min_tvl_usd": criteria.min_tvl_usd,
        },
        "kept": [
            _vault_summary(vault, scores[vault.instrument_id]) for vault in result.kept
        ],
        "dropped": [
            {
                "instrument_id": drop.instrument_id,
                "rule": drop.rule,
                "detail": drop.detail,
            }
            for drop in result.dropped
        ],
    }


@app.command("simulate")
@json_command
def simulate(
    allocation_path: Annotated[
        Path,
        typer.Option("--allocation", exists=True, dir_okay=False, readable=True),
    ],
    benchmark: Annotated[str | None, typer.Option("--benchmark")] = None,
) -> JsonObject:
    allocation = _read_allocation(allocation_path)
    with OneTxClient(ReadOnlyOneTxConfig()) as client:
        return simulate_core.simulate(
            client,
            allocation,
            benchmark=benchmark,
        ).model_dump(mode="json")


@app.command("backtest")
@json_command
def backtest(
    allocation_path: Annotated[
        Path,
        typer.Option("--allocation", exists=True, dir_okay=False, readable=True),
    ],
) -> JsonObject:
    """Read-only daily-compounded NAV backtest of an allocation vs. a
    TVL-weighted universe benchmark. Yield-path only; descriptive not
    predictive."""
    allocation = _read_allocation(allocation_path)
    discovered = _discover_vaults(enrich=True)
    apy_series_by_id = {vault.instrument_id: vault.apy_series for vault in discovered}
    tvl_by_id = {vault.instrument_id: vault.tvl_usd for vault in discovered}
    weights = {leg.instrument_id: leg.weight for leg in allocation.legs}
    report = backtest_core.run(weights, apy_series_by_id, tvl_by_id)
    return report.model_dump(mode="json")


@app.command("check-policy")
@json_command
def check_policy(
    allocation_path: Annotated[
        Path,
        typer.Option("--allocation", exists=True, dir_okay=False, readable=True),
    ],
    policy_path: Annotated[
        Path,
        typer.Option("--policy", dir_okay=False, readable=True),
    ] = DEFAULT_POLICY_PATH,
) -> JsonObject:
    allocation = _read_allocation(allocation_path)
    policy = load_policy(policy_path)
    known_instruments = _discover_vaults(enrich=True)
    return policy_core.check(
        allocation,
        policy,
        known_instruments,
    ).model_dump(mode="json")


@app.command("build-tx")
@json_command
def build_tx(
    allocation_path: Annotated[
        Path,
        typer.Option("--allocation", exists=True, dir_okay=False, readable=True),
    ],
    policy_path: Annotated[
        Path,
        typer.Option("--policy", dir_okay=False, readable=True),
    ] = DEFAULT_POLICY_PATH,
) -> JsonObject:
    _allocation, _policy, plan, _known_instruments = _build_execution_plan(
        allocation_path,
        policy_path,
    )
    payload = plan.model_dump(mode="json")
    validate(payload, "tx-plan")
    return payload


@app.command("execute")
@json_command
def execute(
    allocation_path: Annotated[
        Path,
        typer.Option("--allocation", exists=True, dir_okay=False, readable=True),
    ],
    confirm: ConfirmOption = False,
    unsafe: UnsafeOption = False,
    autonomous: AutonomousOption = False,
    policy_path: Annotated[
        Path,
        typer.Option("--policy", dir_okay=False, readable=True),
    ] = DEFAULT_POLICY_PATH,
) -> JsonObject:
    _ = (unsafe, autonomous)
    return _execute_allocation_from_cli(
        allocation_path,
        policy_path,
        confirm=confirm,
    )


@app.command("positions")
@json_command
def positions(
    address: Annotated[str | None, typer.Option("--address")] = None,
) -> JsonObject:
    return _positions_payload(address)


@app.command("rebalance")
@json_command
def rebalance(
    current_path: Annotated[
        Path,
        typer.Option(
            "--current",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    target_path: Annotated[
        Path,
        typer.Option(
            "--target",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    confirm: ConfirmOption = False,
    unsafe: UnsafeOption = False,
    autonomous: AutonomousOption = False,
    policy_path: Annotated[
        Path,
        typer.Option("--policy", dir_okay=False, readable=True),
    ] = DEFAULT_POLICY_PATH,
    min_trade_usd: Annotated[
        float,
        typer.Option("--min-trade-usd", min=0),
    ] = 1.0,
) -> JsonObject:
    _ = unsafe
    return _rebalance_from_cli(
        current_path,
        target_path,
        policy_path,
        confirm=confirm,
        autonomous=autonomous,
        min_trade_usd=min_trade_usd,
    )


@app.command("withdraw")
@json_command(execution_command=True, command_name="withdraw")
def withdraw(
    position: Annotated[str | None, typer.Option("--position")] = None,
    amount: Annotated[float | None, typer.Option("--amount", min=0)] = None,
    confirm: ConfirmOption = False,
    unsafe: UnsafeOption = False,
    autonomous: AutonomousOption = False,
    positions_path: Annotated[
        Path | None,
        typer.Option(
            "--positions",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    policy_path: Annotated[
        Path,
        typer.Option("--policy", dir_okay=False, readable=True),
    ] = DEFAULT_POLICY_PATH,
) -> JsonObject:
    _ = (unsafe, autonomous)
    if not confirm:
        return _execution_plan("withdraw")
    return _withdraw_executor(
        position,
        positions_path,
        policy_path,
        amount=amount,
        confirm=confirm,
    )


def main() -> None:
    app()
