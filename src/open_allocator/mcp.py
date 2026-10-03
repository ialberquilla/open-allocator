"""MCP adapter: the service layer exposed as tools for a model to call.

Thin by design, like the CLI. A tool parses nothing and decides nothing; it calls
one `open_allocator.service` function and returns its dict. Tool names are the CLI
command names, so the AGENT_GUIDE workflows read the same over either surface.

Read-only and analysis commands are exposed as they are. An execution tool builds
its plan, stores it in the `PlanStore` and returns a plan-required response with the
plan's hash. It never accepts a confirmation and nothing here can apply a plan:
approval is a human action outside the model's reach (see docs/ui.md).

Run over stdio with `open-allocator-mcp` (needs the `mcp` extra). stdout carries the
protocol, which is why nothing in the service layer may print.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from open_allocator import __version__
from open_allocator.core import allocator as allocation_core
from open_allocator.core import strategies as strategies_core
from open_allocator.service import ServiceError
from open_allocator.service import allocation as allocation_service
from open_allocator.service import execution as execution_service
from open_allocator.service import positions as positions_service
from open_allocator.service import universe as universe_service
from open_allocator.service import wallet as wallet_service
from open_allocator.service.plan_store import InMemoryPlanStore, PlanStore

JsonObject = dict[str, Any]

INSTRUCTIONS = (
    "Open Allocator: a policy-bounded DeFi yield allocator on 1Tx. "
    "Every tool returns the same JSON object as the CLI command of the same name, "
    "plus a `warnings` list where the CLI would report warnings on stderr. "
    "`list-vaults` wraps its rows as `vaults` and `build-allocation` its result "
    "as `allocation`; pass that `allocation` object to `simulate` unchanged. "
    "APY figures are descriptive, not predictive. "
    "Fields reported as unknown or null are unknown; do not fill them in."
)

# Advisory screen arguments, shared by `screen` and `build-allocation`.
MinSharpe = Annotated[
    float | None, Field(description="Drop below this Sharpe (Unknown fails).")
]
MaxDrawdown = Annotated[
    float | None,
    Field(ge=0, description="Max tolerated NAV dip magnitude (0.1 == 10%)."),
]
MaxRewardDependence = Annotated[
    float | None,
    Field(ge=0, description="Drop above this reward dependence (Unknown fails)."),
]
MinHistoryDays = Annotated[
    int | None, Field(ge=0, description="Require this many days of history.")
]
Curators = Annotated[list[str] | None, Field(description="Curator allowlist.")]
MinTvlUsd = Annotated[float | None, Field(ge=0, description="Minimum TVL in USD.")]

# Reads 1Tx and chain RPCs; changes nothing anywhere. Execution tools are read-only
# too: they only store a plan, which a human may later approve elsewhere.
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


def _call(function: Callable[..., JsonObject], *args: Any, **kwargs: Any) -> JsonObject:
    """Run a service call, surfacing failures as the CLI's error object.

    The SDK hides the message of any exception that is not a `ToolError`, which
    would leave the model with "Error executing tool" where the CLI user reads the
    actual reason.
    """
    try:
        return function(*args, **kwargs)
    except ServiceError as error:
        raise ToolError(
            json.dumps({"error": error.detail, "code": error.code})
        ) from error
    except Exception as error:
        raise ToolError(json.dumps({"error": str(error)})) from error


def build_mcp(
    plan_store: PlanStore | None = None,
    *,
    approval_url: Callable[[str], str] | None = None,
) -> MCPServer:
    """The MCP app. `plan_store` holds the plans execution tools propose; the
    default keeps them in this process. `approval_url` maps a plan hash to the
    page where a human approves it, when the host serves one."""
    plans = plan_store if plan_store is not None else InMemoryPlanStore()
    server = MCPServer(
        name="open-allocator",
        version=__version__,
        instructions=INSTRUCTIONS,
    )

    def _proposed(proposal: JsonObject) -> JsonObject:
        response = execution_service.propose(plans, proposal)
        if approval_url is not None:
            response["approval_url"] = approval_url(response["plan_hash"])
        return response

    @server.tool(name="wallet-status", annotations=READ_ONLY)
    def wallet_status() -> JsonObject:
        """The configured signer's address, USDC per chain, and whether each chain
        is executable (gas readiness, RPC availability) with the reasons if not."""
        return _call(wallet_service.wallet_status)

    @server.tool(name="safe-address", annotations=READ_ONLY)
    def safe_address(
        chain: Annotated[
            list[int] | None,
            Field(description="Chain ids to report. Defaults to the derivation chain."),
        ] = None,
    ) -> JsonObject:
        """The counterfactual Safe address (the same on every chain), its owners and
        threshold, and whether it is deployed on each requested chain. Requires
        SIGNER_ACCOUNT=safe."""
        return _call(wallet_service.safe_address, tuple(chain) if chain else None)

    @server.tool(name="positions", annotations=READ_ONLY)
    def positions(
        address: Annotated[
            str | None,
            Field(description="Wallet to read. Defaults to the configured signer."),
        ] = None,
    ) -> JsonObject:
        """Current holdings with USD values, idle USDC per chain, and totals. Loops
        are reported at their equity. `warnings` lists anything that could not be
        read and is therefore missing or reported gross."""
        warnings: list[str] = []
        book = _call(positions_service.positions, address, on_warning=warnings.append)
        return {**book, "warnings": warnings}

    @server.tool(name="rewards", annotations=READ_ONLY)
    def rewards(
        wallet: Annotated[str, Field(description="Wallet address to read.")],
        chain: Annotated[
            int | None, Field(description="Restrict to one chain id.")
        ] = None,
    ) -> JsonObject:
        """Claimable and pending protocol rewards for a wallet. Claim calldata
        expires at `expires_at`; `expired` says whether it already has."""
        return _call(positions_service.rewards, wallet, chain)

    @server.tool(name="list-vaults", annotations=READ_ONLY)
    def list_vaults(
        chain: Annotated[int | None, Field(description="Chain id filter.")] = None,
        asset: Annotated[str | None, Field(description="Asset symbol filter.")] = None,
        protocol: Annotated[str | None, Field(description="Protocol filter.")] = None,
        sort: Annotated[
            Literal["apy", "tvl", "score"] | None,
            Field(description="Sort descending by this field."),
        ] = None,
    ) -> JsonObject:
        """The live universe with score, advertised and priced APY, TVL and
        yield-path risk metrics per instrument. Risk metrics never cover
        principal, depeg or contract loss."""
        warnings: list[JsonObject] = []
        vaults = _call(
            universe_service.list_vaults,
            chain=chain,
            asset=asset,
            protocol=protocol,
            sort=sort,
            on_warning=warnings.append,
        )
        return {"vaults": vaults, "warnings": warnings}

    @server.tool(name="score-vault", annotations=READ_ONLY)
    def score_vault(
        instrument_id: Annotated[str, Field(description="Instrument id to score.")],
    ) -> JsonObject:
        """Score breakdown and risk metrics for one instrument."""
        warnings: list[JsonObject] = []
        score = _call(
            universe_service.score_vault, instrument_id, on_warning=warnings.append
        )
        return {**score, "warnings": warnings}

    @server.tool(name="screen", annotations=READ_ONLY)
    def screen(
        min_sharpe: MinSharpe = None,
        max_drawdown: MaxDrawdown = None,
        max_reward_dependence: MaxRewardDependence = None,
        min_history_days: MinHistoryDays = None,
        curators: Curators = None,
        min_tvl_usd: MinTvlUsd = None,
    ) -> JsonObject:
        """Advisory metric screen over the live universe: what is kept, and what
        is dropped by which rule. Narrows only; policy still applies downstream
        and no screen can loosen it."""
        warnings: list[JsonObject] = []
        criteria = universe_service.screen_criteria(
            min_sharpe=min_sharpe,
            max_drawdown=max_drawdown,
            max_reward_dependence=max_reward_dependence,
            min_history_days=min_history_days,
            curators=curators,
            min_tvl_usd=min_tvl_usd,
        )
        result = _call(universe_service.screen, criteria, on_warning=warnings.append)
        return {**result, "warnings": warnings}

    @server.tool(name="build-allocation", annotations=READ_ONLY)
    def build_allocation(
        amount: Annotated[
            float | None,
            Field(ge=0, description="USD to allocate. Required unless in `spec`."),
        ] = None,
        risk: Annotated[
            Literal["conservative", "balanced", "aggressive"],
            Field(description="Risk preset."),
        ] = "balanced",
        policy_path: Annotated[
            str,
            Field(description="Policy YAML, relative to the server's directory."),
        ] = str(allocation_service.DEFAULT_POLICY_PATH),
        spec: Annotated[
            dict[str, Any] | None,
            Field(
                description="Allocation-spec object (weights, or strategy + params "
                "+ selection). What it sets wins over the matching arguments, "
                "except `amount`."
            ),
        ] = None,
        strategy: Annotated[
            str,
            Field(
                description="Allocation strategy, one of: "
                + ", ".join(strategies_core.available())
            ),
        ] = allocation_core.DEFAULT_STRATEGY,
        strategy_params: Annotated[
            dict[str, Any] | None, Field(description="Strategy parameters.")
        ] = None,
        min_sharpe: MinSharpe = None,
        max_drawdown: MaxDrawdown = None,
        max_reward_dependence: MaxRewardDependence = None,
        min_history_days: MinHistoryDays = None,
        curators: Curators = None,
        min_tvl_usd: MinTvlUsd = None,
        max_positions: Annotated[
            int | None, Field(ge=1, description="Keep only the top-N positions.")
        ] = None,
        min_position_usd: Annotated[
            float | None, Field(ge=0, description="Drop legs below this USD size.")
        ] = None,
        score_power: Annotated[
            float | None, Field(ge=0, description="Override preset score exponent.")
        ] = None,
        apy_weight: Annotated[
            float | None, Field(ge=0, description="Override the preset APY tilt.")
        ] = None,
        caps_headroom_bps: Annotated[
            float,
            Field(
                ge=0,
                description="Build under the policy's concentration caps by this "
                "many bps, relative (300 = caps x 0.97). check-policy still scores "
                "against the untightened policy.",
            ),
        ] = 0.0,
        exclude: Annotated[
            list[str] | None, Field(description="Instrument ids to veto.")
        ] = None,
        pins: Annotated[
            dict[str, float] | None,
            Field(description="Pinned weights by instrument id."),
        ] = None,
        source_chain_id: Annotated[
            int | None,
            Field(
                description="Chain the wallet's USDC is funded on, for the cost "
                "estimate. Defaults to the chain holding most of the deploy."
            ),
        ] = None,
    ) -> JsonObject:
        """A policy-checked allocation over the live universe, with a cost
        estimate. Builds a proposal only; nothing is executed. Check
        `metadata.policy_ok` and `metadata.warnings` before presenting it."""
        warnings: list[JsonObject] = []
        criteria = universe_service.screen_criteria(
            min_sharpe=min_sharpe,
            max_drawdown=max_drawdown,
            max_reward_dependence=max_reward_dependence,
            min_history_days=min_history_days,
            curators=curators,
            min_tvl_usd=min_tvl_usd,
        )
        allocation = _call(
            allocation_service.build_allocation,
            amount,
            risk=risk,
            policy=Path(policy_path),
            spec=spec,
            strategy=strategy,
            strategy_params=strategy_params,
            criteria=criteria,
            max_positions=max_positions,
            min_position_usd=min_position_usd,
            score_power=score_power,
            apy_weight=apy_weight,
            caps_headroom_bps=caps_headroom_bps,
            exclude=exclude,
            pins=pins,
            source_chain_id=source_chain_id,
            on_warning=warnings.append,
        )
        return {"allocation": allocation, "warnings": warnings}

    @server.tool(name="simulate", annotations=READ_ONLY)
    def simulate(
        allocation: Annotated[
            dict[str, Any],
            Field(description="The `allocation` object from build-allocation."),
        ],
        benchmark: Annotated[
            str | None, Field(description="Benchmark to compare against.")
        ] = None,
    ) -> JsonObject:
        """Descriptive scorecard of an allocation: yield, stability and how many
        independent sleeves the capital sits in. Not a forecast."""
        warnings: list[JsonObject] = []
        scorecard = _call(
            allocation_service.simulate,
            allocation,
            benchmark=benchmark,
            on_warning=warnings.append,
        )
        return {**scorecard, "warnings": warnings}

    @server.tool(name="backtest", annotations=READ_ONLY)
    def backtest(
        allocation: Annotated[
            dict[str, Any],
            Field(description="The `allocation` object from build-allocation."),
        ],
    ) -> JsonObject:
        """Daily-compounded NAV backtest of an allocation against a TVL-weighted
        universe benchmark, over the history 1Tx serves. Yield path only: no
        principal, depeg or contract loss. Descriptive, not predictive."""
        warnings: list[JsonObject] = []
        report = _call(
            allocation_service.backtest, allocation, on_warning=warnings.append
        )
        return {**report, "warnings": warnings}

    @server.tool(name="check-policy", annotations=READ_ONLY)
    def check_policy(
        allocation: Annotated[
            dict[str, Any],
            Field(description="The `allocation` object from build-allocation."),
        ],
        policy_path: Annotated[
            str,
            Field(description="Policy YAML, relative to the server's directory."),
        ] = str(allocation_service.DEFAULT_POLICY_PATH),
        against: Annotated[
            dict[str, Any] | None,
            Field(
                description="A positions book, as the `positions` tool returns it. "
                "Scores the book the allocation would leave instead of the buy "
                "in isolation."
            ),
        ] = None,
    ) -> JsonObject:
        """Score an allocation against the policy on today's shelf: `ok` and
        each violation with its rule, entity, limit and actual value. Stop on
        any violation."""
        warnings: list[JsonObject] = []
        if against is not None:
            # The `positions` tool's own warnings are not part of the book.
            against = {k: v for k, v in against.items() if k != "warnings"}
        result = _call(
            allocation_service.check_policy,
            allocation,
            policy=Path(policy_path),
            against=against,
            on_warning=warnings.append,
        )
        return {**result, "warnings": warnings}

    @server.tool(name="execute", annotations=READ_ONLY)
    def execute(
        allocation: Annotated[
            dict[str, Any],
            Field(description="The `allocation` object from build-allocation."),
        ],
        policy_path: Annotated[
            str,
            Field(description="Policy YAML, relative to the server's directory."),
        ] = str(allocation_service.DEFAULT_POLICY_PATH),
    ) -> JsonObject:
        """Plan the deposits for an allocation and submit the plan for human
        approval. Broadcasts nothing. Returns `plan_required: true`, the
        `plan_hash` a human approves, `expires_at`, and `plan`: the dry-run
        report (steps, funding, wallet preparation, blockers), and `approval_url`
        when the server has an approval page. Show the plan and its blockers and
        give the user the link; only the user can approve it, outside this
        conversation."""
        warnings: list[JsonObject] = []
        proposal = _call(
            execution_service.plan_execute,
            allocation,
            policy=Path(policy_path),
            on_warning=warnings.append,
        )
        return {**_proposed(proposal), "warnings": warnings}

    @server.tool(name="rebalance", annotations=READ_ONLY)
    def rebalance(
        target: Annotated[
            dict[str, Any],
            Field(description="The target `allocation` object from build-allocation."),
        ],
        min_trade_usd: Annotated[
            float,
            Field(ge=0, description="Trades smaller than this USD are skipped."),
        ] = 1.0,
        policy_path: Annotated[
            str,
            Field(description="Policy YAML, relative to the server's directory."),
        ] = str(allocation_service.DEFAULT_POLICY_PATH),
    ) -> JsonObject:
        """Plan the trades that move the signer's current book to a target
        allocation (each chain's withdrawals before its deposits) and submit the
        plan for human approval. Broadcasts nothing. Levered loops are not
        traded. Returns `plan_required: true`, the `plan_hash` a human approves,
        `expires_at`, and `plan`: the dry-run report (trades, skipped deltas,
        steps, funding, blockers), and `approval_url` when the server has an
        approval page. Show the trades and blockers and give the user the link;
        only the user can approve it, outside this conversation."""
        warnings: list[JsonObject] = []
        proposal = _call(
            execution_service.plan_rebalance,
            target,
            min_trade_usd=min_trade_usd,
            policy=Path(policy_path),
            on_warning=warnings.append,
        )
        return {**_proposed(proposal), "warnings": warnings}

    @server.tool(name="withdraw", annotations=READ_ONLY)
    def withdraw(
        position: Annotated[
            str,
            Field(description="Instrument id of a position in the signer's book."),
        ],
        amount: Annotated[
            float | None,
            Field(
                gt=0,
                description="USD to withdraw. Omit, or pass at least the position's "
                "value, for a full exit.",
            ),
        ] = None,
    ) -> JsonObject:
        """Plan a withdrawal from one position into USDC and submit the plan for
        human approval. Broadcasts nothing. Levered loops are refused: they are
        unwound by a loop close. Returns `plan_required: true`, the `plan_hash` a
        human approves, `expires_at`, and `plan`: the dry-run report (shares sold,
        expected USDC, steps, funding, blockers), and `approval_url` when the server
        has an approval page. Show the plan and its blockers and give the user the
        link; only the user can approve it, outside this conversation."""
        warnings: list[str] = []
        proposal = _call(
            execution_service.plan_withdraw,
            position,
            amount=amount,
            on_warning=warnings.append,
        )
        return {**_proposed(proposal), "warnings": warnings}

    @server.tool(name="loop-open", annotations=READ_ONLY)
    def loop_open(
        loop: Annotated[
            str,
            Field(description="Loop id from list-vaults' levered loop rows."),
        ],
        amount: Annotated[
            float,
            Field(
                gt=0, description="Equity in USD, from idle USDC on the loop's chain."
            ),
        ],
        leverage: Annotated[
            float,
            Field(ge=1, description="Target leverage of the opened loop."),
        ],
        policy_path: Annotated[
            str,
            Field(description="Policy YAML, relative to the server's directory."),
        ] = str(allocation_service.DEFAULT_POLICY_PATH),
    ) -> JsonObject:
        """Plan opening one levered loop from idle USDC, scored against the
        signer's current book, and submit the plan for human approval. Broadcasts
        nothing. Returns `plan_required: true`, the `plan_hash` a human approves,
        `expires_at`, and `plan`: the dry-run report (policy result, the loop
        announcement with its modelled and simulated health factor, account
        config change and other positions in the pool, steps), and `approval_url`
        when the server has an approval page. Show the announcement and give the
        user the link; only the user can approve it, outside this conversation."""
        warnings: list[JsonObject] = []
        proposal = _call(
            execution_service.plan_loop_open,
            loop,
            equity_usd=amount,
            leverage=leverage,
            policy=Path(policy_path),
            on_warning=warnings.append,
        )
        return {**_proposed(proposal), "warnings": warnings}

    @server.tool(name="loop-close", annotations=READ_ONLY)
    def loop_close(
        loop: Annotated[
            str,
            Field(description="Loop id of a levered loop the signer holds."),
        ],
        policy_path: Annotated[
            str,
            Field(description="Policy YAML, relative to the server's directory."),
        ] = str(allocation_service.DEFAULT_POLICY_PATH),
    ) -> JsonObject:
        """Plan unwinding one levered loop and submit the plan for human
        approval. Broadcasts nothing. Returns `plan_required: true`, the
        `plan_hash` a human approves, `expires_at`, and `plan`: the dry-run report
        (the loop announcement: what is repaid and returned, account config
        change, other positions in the pool, steps), and `approval_url` when the
        server has an approval page. Show the announcement and give the user the
        link; only the user can approve it, outside this conversation."""
        warnings: list[JsonObject] = []
        proposal = _call(
            execution_service.plan_loop_close,
            loop,
            policy=Path(policy_path),
            on_warning=warnings.append,
        )
        return {**_proposed(proposal), "warnings": warnings}

    @server.tool(name="bridge", annotations=READ_ONLY)
    def bridge(
        from_chain: Annotated[int, Field(ge=1, description="Source chain id.")],
        to_chain: Annotated[int, Field(ge=1, description="Destination chain id.")],
        amount: Annotated[float, Field(gt=0, description="USDC to move.")],
        ref: Annotated[
            str | None,
            Field(
                description="Names a new transfer with the same arguments as one "
                "already settled."
            ),
        ] = None,
    ) -> JsonObject:
        """Plan moving the Safe's USDC from one chain to another over CCTP, with
        no deposit, and submit the plan for human approval. Broadcasts nothing.
        The same arguments name the same transfer: while it waits on Circle's
        attestation, calling this again plans advancing it (the mint into the
        Safe), which also needs approval. Returns `plan_required: true`, the
        `plan_hash` a human approves, `expires_at`, and `plan`: the dry-run report
        (the burn, funding, blockers, or the transfer under way), and
        `approval_url` when the server has an approval page. Show the plan and
        give the user the link; only the user can approve it, outside this
        conversation."""
        warnings: list[JsonObject] = []
        proposal = _call(
            execution_service.plan_bridge,
            from_chain,
            to_chain,
            amount,
            ref=ref,
            on_warning=warnings.append,
        )
        return {**_proposed(proposal), "warnings": warnings}

    return server


def main() -> None:
    build_mcp().run("stdio")
