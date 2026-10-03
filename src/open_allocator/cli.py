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
from open_allocator.core import drift as drift_core
from open_allocator.core import mandate as mandate_core
from open_allocator.core import policy as policy_core
from open_allocator.core import positions as positions_core
from open_allocator.core import (
    universe,
)
from open_allocator.core.policy_loader import load_policy
from open_allocator.core.schema import validate
from open_allocator.core.state import (
    ScopedIdempotencyStore,
)
from open_allocator.core.types import (
    Allocation,
    Vault,
)
from open_allocator.exec.client import OneTxClient
from open_allocator.exec.config import AllocatorConfig, ReadOnlyOneTxConfig
from open_allocator.exec.execute import TransactionPlanError
from open_allocator.service import allocation as allocation_service
from open_allocator.service import execution as execution_service
from open_allocator.service import positions as positions_service
from open_allocator.service import universe as universe_service
from open_allocator.service import wallet as wallet_service
from open_allocator.service._common import model_payload as _model_payload
from open_allocator.service._common import signer_from_config

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


DEFAULT_POLICY_PATH = allocation_service.DEFAULT_POLICY_PATH


def _write_json(payload: JsonValue, *, err: bool = False) -> None:
    typer.echo(json.dumps(payload, separators=(",", ":")), err=err)


def json_command(
    func: Callable[P, R] | None = None,
) -> Callable[[Callable[P, R]], Callable[P, None]] | Callable[P, None]:
    def decorator(inner: Callable[P, R]) -> Callable[P, None]:
        @wraps(inner)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
            try:
                result = inner(*args, **kwargs)
                _write_json(result)
            except Exception as error:
                _write_json({"error": str(error)}, err=True)
                raise typer.Exit(1) from error

        return wrapper

    if func is None:
        return decorator
    return decorator(func)


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


def _held_off_shelf(
    positions_snapshot: positions_core.Positions,
    shelf: Sequence[Vault],
) -> list[Vault]:
    """Held instruments the shelf no longer lists — a matured PT, most often.

    Only opens a client when something held is actually missing.
    """
    listed = {vault.instrument_id for vault in shelf}
    # A levered holding's id is its loop id, which names no instrument.
    missing = [
        holding.instrument_id
        for holding in positions_snapshot.holdings
        if holding.levered is None and holding.instrument_id not in listed
    ]
    if not missing:
        return []
    with OneTxClient(ReadOnlyOneTxConfig()) as client:
        off_shelf, skipped = universe.held_off_shelf(client, missing, shelf)
    if skipped:
        _write_json(
            {
                "warning": "held_instruments_unreadable",
                "instruments": [s.model_dump() for s in skipped],
            },
            err=True,
        )
    return off_shelf


def _warn(warning: JsonObject) -> None:
    # stderr, not stdout: every command's stdout is one JSON object and callers
    # parse it.
    _write_json(warning, err=True)


def _discover_vaults(*, enrich: bool = False) -> list[Vault]:
    return universe_service.discover_vaults(enrich=enrich, on_warning=_warn)


def _discover_vaults_from_client(
    client: object,
    *,
    enrich: bool = False,
    loops: bool = False,
) -> list[Vault]:
    return universe_service.discover_vaults_from_client(
        client, enrich=enrich, loops=loops, on_warning=_warn
    )


def _read_allocation(path: Path) -> Allocation:
    return allocation_service.parse_allocation(_read_json(path))


def _read_json(path: Path) -> object:
    with path.open(encoding="utf-8") as file:
        return json.load(file)


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


def execute_rebalance(*args: object, **kwargs: object) -> object:
    from open_allocator.exec.rebalance import execute_rebalance as executor

    return executor(*args, **kwargs)


def execute_withdraw(*args: object, **kwargs: object) -> object:
    from open_allocator.exec.withdraw import withdraw as executor

    return executor(*args, **kwargs)


def execute_loop_close(*args: object, **kwargs: object) -> object:
    from open_allocator.exec.loop_close import close as executor

    return executor(*args, **kwargs)


def execute_loop_open(*args: object, **kwargs: object) -> object:
    from open_allocator.exec.loop_open import open_loop as executor

    return executor(*args, **kwargs)


def _idempotency_store(config: object, scope: str) -> ScopedIdempotencyStore | None:
    return execution_service.idempotency_store(config, scope)


def _execution_idempotency_store(
    config: object,
    allocation: Allocation,
) -> ScopedIdempotencyStore | None:
    return _idempotency_store(config, execution_service.allocation_scope(allocation))


def _rebalance_idempotency_store(
    config: object,
    positions: positions_core.Positions,
    target: Allocation,
    *,
    min_trade_usd: float,
) -> ScopedIdempotencyStore | None:
    return _idempotency_store(
        config,
        _rebalance_scope(positions, target, min_trade_usd=min_trade_usd),
    )


def _withdraw_idempotency_store(
    config: object,
    position: positions_core.PositionHolding,
    *,
    amount: float | None,
) -> ScopedIdempotencyStore | None:
    return _idempotency_store(config, _withdraw_scope(position, amount=amount))


def _rebalance_scope(
    positions: positions_core.Positions,
    target: Allocation,
    *,
    min_trade_usd: float,
) -> str:
    return execution_service.rebalance_scope(
        positions, target, min_trade_usd=min_trade_usd
    )


def _loop_close_idempotency_store(
    config: object,
    loop_id: str,
    account: str,
) -> ScopedIdempotencyStore | None:
    return _idempotency_store(config, _loop_close_scope(loop_id, account))


def _loop_close_scope(loop_id: str, account: str) -> str:
    payload = {"loop_id": loop_id, "account": account}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _loop_open_idempotency_store(
    config: object,
    loop_id: str,
    account: str,
) -> ScopedIdempotencyStore | None:
    return _idempotency_store(config, _loop_open_scope(loop_id, account))


def _loop_open_scope(loop_id: str, account: str) -> str:
    payload = {"loop_id": loop_id, "account": account, "action": "open"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _withdraw_scope(
    position: positions_core.PositionHolding,
    *,
    amount: float | None,
) -> str:
    return execution_service.withdraw_scope(position, amount=amount)


def _execute_allocation_from_cli(
    allocation_path: Path,
    policy_path: Path,
    *,
    confirm: bool,
) -> JsonObject:
    proposal = execution_service.plan_execute(
        _read_allocation(allocation_path), policy=policy_path, on_warning=_warn
    )
    if not confirm:
        return proposal["report"]
    # Confirmed in the same run: the plan just built is the plan that runs.
    return execution_service.apply_execute(
        proposal["plan"], expected_hash=proposal["plan_hash"]
    )


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


def _loop_close_from_cli(
    loop_id: str,
    policy_path: Path,
    *,
    confirm: bool,
) -> JsonObject:
    policy = load_policy(policy_path)
    config = AllocatorConfig()
    signer = signer_from_config(config)

    with OneTxClient(config) as client:
        known_instruments = _discover_vaults_from_client(client, enrich=True)
        report = execute_loop_close(
            client,
            signer,
            loop_id,
            policy=policy,
            known_instruments=known_instruments,
            confirm=confirm,
            config=config,
            idempotency_store=(
                _loop_close_idempotency_store(config, loop_id, str(signer.address()))
                if confirm
                else None
            ),
        )

    return _model_payload(report)


def _loop_open_from_cli(
    loop_id: str,
    equity_usd: float,
    leverage: float,
    policy_path: Path,
    *,
    confirm: bool,
) -> JsonObject:
    policy = load_policy(policy_path)
    config = AllocatorConfig()
    signer = signer_from_config(config)

    with OneTxClient(config) as client:
        known_instruments = _discover_vaults_from_client(client, enrich=True)
        report = execute_loop_open(
            client,
            signer,
            loop_id,
            equity_usd=equity_usd,
            leverage=leverage,
            policy=policy,
            known_instruments=known_instruments,
            confirm=confirm,
            config=config,
            idempotency_store=(
                _loop_open_idempotency_store(config, loop_id, str(signer.address()))
                if confirm
                else None
            ),
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
    if position is None:
        raise ValueError("--position is required")

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
            confirm=confirm,
            config=config,
            # A dry run reads no completion state, as for rebalance.
            idempotency_store=(
                _withdraw_idempotency_store(config, holding, amount=amount)
                if confirm
                else None
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
    from open_allocator.exec import loops as loops_exec

    current, warnings = loops_exec.read_book(client, str(address_method()))
    for warning in warnings:
        _write_json({"warning": "levered_positions", "message": warning}, err=True)
    return _select_position(current, position)


def _positions(address: str | None) -> JsonObject:
    return positions_service.positions(
        address,
        on_warning=lambda message: _write_json(
            {"warning": "levered_positions", "message": message}, err=True
        ),
    )


def _select_position(
    source: positions_core.Positions | positions_core.PositionHolding,
    position_id: str,
) -> positions_core.PositionHolding:
    return execution_service.select_position(source, position_id)


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
            raise ValueError(f"--strategy-param must be 'key=value', got: {item!r}")
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


@app.command("wallet-status")
@json_command
def wallet_status() -> JsonObject:
    return wallet_service.wallet_status()


@app.command("safe-address")
@json_command
def safe_address(
    chain: Annotated[
        list[int] | None,
        typer.Option(
            "--chain",
            help="Chain to report; repeatable. Defaults to the derivation chain.",
        ),
    ] = None,
) -> JsonObject:
    return wallet_service.safe_address(tuple(chain) if chain else None)


@app.command("list-vaults")
@json_command
def list_vaults(
    chain: Annotated[int | None, typer.Option("--chain")] = None,
    asset: Annotated[str | None, typer.Option("--asset")] = None,
    protocol: Annotated[str | None, typer.Option("--protocol")] = None,
    sort: Annotated[VaultSort | None, typer.Option("--sort")] = None,
) -> list[JsonObject]:
    return universe_service.list_vaults(
        chain=chain,
        asset=asset,
        protocol=protocol,
        sort=sort.value if sort else None,
        on_warning=_warn,
    )


@app.command("score-vault")
@json_command
def score_vault(
    instrument_id: Annotated[str, typer.Option("--instrument-id")],
) -> JsonObject:
    return universe_service.score_vault(instrument_id, on_warning=_warn)


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
    caps_headroom_bps: Annotated[
        float,
        typer.Option(
            "--caps-headroom-bps",
            min=0,
            help=(
                "Build under the policy's concentration caps by this many bps, "
                "RELATIVE (300 = caps x 0.97). check-policy still scores against "
                "the untightened policy. Room for the friction between building "
                "a book and holding one."
            ),
        ),
    ] = 0.0,
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
    return allocation_service.build_allocation(
        amount,
        risk=risk.value,
        policy=policy_path,
        spec=_read_json(spec) if spec is not None else None,
        strategy=strategy,
        strategy_params=_parse_strategy_params(strategy_param),
        criteria=universe_service.screen_criteria(
            min_sharpe=min_sharpe,
            max_drawdown=max_drawdown,
            max_reward_dependence=max_reward_dependence,
            min_history_days=min_history_days,
            curators=screen_curator,
            min_tvl_usd=min_tvl_usd,
        ),
        max_positions=max_positions,
        min_position_usd=min_position_usd,
        score_power=score_power,
        apy_weight=apy_weight,
        caps_headroom_bps=caps_headroom_bps,
        exclude=exclude,
        pins=_parse_pins(pin),
        source_chain_id=source_chain_id,
        on_warning=_warn,
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
    criteria = universe_service.screen_criteria(
        min_sharpe=min_sharpe,
        max_drawdown=max_drawdown,
        max_reward_dependence=max_reward_dependence,
        min_history_days=min_history_days,
        curators=screen_curator,
        min_tvl_usd=min_tvl_usd,
    )
    return universe_service.screen(criteria, on_warning=_warn)


@app.command("simulate")
@json_command
def simulate(
    allocation_path: Annotated[
        Path,
        typer.Option("--allocation", exists=True, dir_okay=False, readable=True),
    ],
    benchmark: Annotated[str | None, typer.Option("--benchmark")] = None,
) -> JsonObject:
    return allocation_service.simulate(
        _read_allocation(allocation_path),
        benchmark=benchmark,
        on_warning=_warn,
    )


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
    against_path: Annotated[
        Path | None,
        typer.Option(
            "--against",
            exists=True,
            dir_okay=False,
            readable=True,
            help=(
                "Positions JSON to add the allocation to. Scores the resulting "
                "book instead of the buy in isolation."
            ),
        ),
    ] = None,
) -> JsonObject:
    allocation = _read_allocation(allocation_path)
    policy = load_policy(policy_path)
    known_instruments = _discover_vaults(enrich=True)
    if against_path is None:
        return policy_core.check(
            allocation,
            policy,
            known_instruments,
        ).model_dump(mode="json")
    held_usd = positions_core.held_usd_by_instrument(_read_positions(against_path))
    return policy_core.check_incremental(
        allocation,
        policy,
        known_instruments,
        held_usd,
    ).model_dump(mode="json")


@app.command("validate-mandate")
@json_command
def validate_mandate(
    mandate_path: Annotated[
        Path,
        typer.Option("--mandate", exists=True, dir_okay=False, readable=True),
    ],
    baseline_path: Annotated[
        Path,
        typer.Option("--baseline", dir_okay=False, readable=True),
    ] = DEFAULT_POLICY_PATH,
) -> JsonObject:
    """Gate an LLM-authored mandate before its policy is used.

    Reads files only -- no discovery, no network, no model. The derived policy
    is located relative to the mandate, not to the working directory.
    """
    return mandate_core.validate_mandate(mandate_path, baseline_path).model_dump(
        mode="json"
    )


@app.command("drift")
@json_command
def drift(
    mandate_path: Annotated[
        Path,
        typer.Option("--mandate", exists=True, dir_okay=False, readable=True),
    ],
    positions_path: Annotated[
        Path | None,
        typer.Option("--positions", exists=True, dir_okay=False, readable=True),
    ] = None,
    allocation_path: Annotated[
        Path | None,
        typer.Option("--allocation", exists=True, dir_okay=False, readable=True),
    ] = None,
    previous_shelf_path: Annotated[
        Path | None,
        typer.Option("--previous-shelf", exists=True, dir_okay=False, readable=True),
    ] = None,
) -> JsonObject:
    """The daily gate. Run it first; if `drifted` is false, stop.

    Reports rather than decides, and never answers `false` because it could not
    tell -- a check it cannot run appears in `reasons` as `unevaluated`.

    `--allocation` is the target the book was built to, and is the only source
    of a per-instrument `target_bps`: a mandate carries tier weights, not
    per-instrument ones. `--previous-shelf` takes yesterday's `list-vaults`
    output or the `shelf` block this command returns.
    """
    mandate = mandate_core.load_mandate(mandate_path)
    policy = load_policy(
        mandate_core.resolve_policy_path(mandate_path, mandate.policy_path),
    )
    positions_snapshot = (
        _read_positions(positions_path)
        if positions_path is not None
        else positions_core.Positions.model_validate(_positions(None))
    )
    shelf = _discover_vaults(enrich=True)
    off_shelf = _held_off_shelf(positions_snapshot, shelf)
    return drift_core.evaluate(
        mandate,
        positions_snapshot,
        policy,
        target=(
            _read_allocation(allocation_path) if allocation_path is not None else None
        ),
        known_instruments=shelf,
        held_off_shelf=off_shelf,
        previous_shelf=(
            _read_json(previous_shelf_path) if previous_shelf_path is not None else None
        ),
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
    planned = execution_service.plan_allocation_execution(
        _read_allocation(allocation_path), policy=policy_path, on_warning=_warn
    )
    # The plan is the output, so blockers are an error rather than a note.
    if planned.preparation.blockers:
        raise TransactionPlanError("; ".join(planned.preparation.blockers))
    payload = planned.plan.model_dump(mode="json")
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
    return _positions(address)


@app.command("rewards")
@json_command
def rewards(
    wallet: Annotated[str, typer.Option("--wallet")],
    chain: Annotated[int | None, typer.Option("--chain")] = None,
) -> JsonObject:
    return positions_service.rewards(wallet, chain)


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
@json_command
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
    return _withdraw_executor(
        position,
        positions_path,
        policy_path,
        amount=amount,
        confirm=confirm,
    )


@app.command("loop-close")
@json_command
def loop_close(
    loop: Annotated[str, typer.Option("--loop")],
    confirm: ConfirmOption = False,
    policy_path: Annotated[
        Path,
        typer.Option("--policy", dir_okay=False, readable=True),
    ] = DEFAULT_POLICY_PATH,
) -> JsonObject:
    """Unwind one levered loop, without authoring a target allocation."""
    return _loop_close_from_cli(loop, policy_path, confirm=confirm)


@app.command("loop-open")
@json_command
def loop_open(
    loop: Annotated[str, typer.Option("--loop")],
    amount: Annotated[float, typer.Option("--amount", min=0)],
    leverage: Annotated[float, typer.Option("--leverage", min=1)],
    confirm: ConfirmOption = False,
    policy_path: Annotated[
        Path,
        typer.Option("--policy", dir_okay=False, readable=True),
    ] = DEFAULT_POLICY_PATH,
) -> JsonObject:
    """Open one levered loop from idle USDC, checked against the held book."""
    return _loop_open_from_cli(loop, amount, leverage, policy_path, confirm=confirm)


@app.command("bridge")
@json_command
def bridge(
    from_chain: Annotated[int, typer.Option("--from", min=1)],
    to_chain: Annotated[int, typer.Option("--to", min=1)],
    amount: Annotated[float, typer.Option("--amount", min=0)],
    ref: Annotated[str | None, typer.Option("--ref")] = None,
    confirm: ConfirmOption = False,
    unsafe: UnsafeOption = False,
    autonomous: AutonomousOption = False,
) -> JsonObject:
    _ = (unsafe, autonomous)
    return _bridge_from_cli(from_chain, to_chain, amount, ref=ref, confirm=confirm)


def _bridge_from_cli(
    from_chain_id: int,
    to_chain_id: int,
    amount: float,
    *,
    ref: str | None,
    confirm: bool,
) -> JsonObject:
    from open_allocator.exec.transfer import execute_transfer

    config = AllocatorConfig()
    signer = signer_from_config(config)
    address = str(signer.address())

    with OneTxClient(config) as client:
        report = execute_transfer(
            client,
            signer,
            from_chain_id=from_chain_id,
            to_chain_id=to_chain_id,
            amount_usdc=amount,
            known_instruments=_discover_vaults_from_client(client),
            confirm=confirm,
            config=config,
            # Read on a dry run too, so a transfer under way is reported, not
            # planned again.
            idempotency_store=_idempotency_store(
                config,
                _bridge_scope(address, from_chain_id, to_chain_id, amount, ref),
            ),
        )

    return _model_payload(report)


def _bridge_scope(
    address: str,
    from_chain_id: int,
    to_chain_id: int,
    amount: float,
    ref: str | None,
) -> str:
    """The same arguments resume the same transfer; --ref starts another."""
    payload = {
        "bridge": {
            "account": address.casefold(),
            "from": from_chain_id,
            "to": to_chain_id,
            "amount": amount,
            "ref": ref,
        }
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def main() -> None:
    app()
