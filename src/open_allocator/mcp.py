"""MCP adapter: the service layer exposed as tools for a model to call.

Thin by design, like the CLI. A tool parses nothing and decides nothing; it calls
one `open_allocator.service` function and returns its dict. Tool names are the CLI
command names, so the AGENT_GUIDE workflows read the same over either surface.

Only read-only commands are exposed. An execution tool must return a plan-required
response and never accept a confirmation: approval is a human action outside the
model's reach (see docs/ui.md).

Run over stdio with `open-allocator-mcp` (needs the `mcp` extra). stdout carries the
protocol, which is why nothing in the service layer may print.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from open_allocator import __version__
from open_allocator.service import ServiceError
from open_allocator.service import positions as positions_service
from open_allocator.service import wallet as wallet_service

JsonObject = dict[str, Any]

INSTRUCTIONS = (
    "Open Allocator: a policy-bounded DeFi yield allocator on 1Tx. "
    "Every tool returns the same JSON object as the CLI command of the same name. "
    "APY figures are descriptive, not predictive. "
    "Fields reported as unknown or null are unknown; do not fill them in."
)

# Reads 1Tx and chain RPCs; changes nothing anywhere.
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


def build_mcp() -> MCPServer:
    server = MCPServer(
        name="open-allocator",
        version=__version__,
        instructions=INSTRUCTIONS,
    )

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

    return server


def main() -> None:
    build_mcp().run("stdio")
