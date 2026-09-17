from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from open_allocator.exec.calldata_probe import PROBE_ACCOUNT, probe_instrument
from open_allocator.exec.client import (
    InstrumentCalldataQuery,
    InstrumentCalldataResponse,
    OneTxDecodeError,
    OneTxHTTPError,
)

FIXTURES = Path(__file__).parent / "fixtures"
INSTRUMENT_ID = "0x" + "ab" * 32
BASE = 8453


def payload(**overrides: Any) -> dict[str, Any]:
    body = json.loads(
        (FIXTURES / "calldata-instrument-deposit-swap.json").read_text(encoding="utf-8")
    )
    body.update(instrumentId=INSTRUMENT_ID, account=PROBE_ACCOUNT, expiresAt=None)
    body.update(overrides)
    return body


@dataclass
class Client:
    """Answers like OneTxClient.instrument_calldata: parse strictly or raise."""

    body: dict[str, Any] | None = None
    error: Exception | None = None
    queries: list[InstrumentCalldataQuery] = field(default_factory=list)

    def instrument_calldata(
        self, instrument_id: str, query: InstrumentCalldataQuery
    ) -> InstrumentCalldataResponse:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        try:
            return InstrumentCalldataResponse.model_validate(self.body)
        except ValueError as error:
            raise OneTxDecodeError(f"outside the execution contract: {error}") from None


def undeployed(_chain_id: int, _account: str) -> bytes:
    return b""


def probe(client: Client, **overrides: Any) -> Any:
    arguments: dict[str, Any] = {
        "instrument_id": INSTRUMENT_ID,
        "chain_id": BASE,
        "action": "deposit",
        "amount": "100250000",
        "code_reader": undeployed,
    }
    arguments.update(overrides)
    return probe_instrument(client, **arguments)


def test_a_compatible_deployment_passes_every_check_for_an_undeployed_account() -> None:
    client = Client(body=payload())

    result = probe(client)

    assert result.ok
    assert [check.name for check in result.checks] == [
        "account_state",
        "request_without_executor",
        "execution_contract",
        "bound_to_request",
    ]
    assert result.protocol_gas == "412345"
    [query] = client.queries
    assert "executor" not in query.model_dump(by_alias=True)
    assert query.account == PROBE_ACCOUNT


def test_a_response_still_carrying_executor_fails_the_contract_check() -> None:
    result = probe(Client(body=payload(executor=PROBE_ACCOUNT)))

    assert not result.ok
    assert result.checks[-1].name == "execution_contract"
    assert "executor" in result.checks[-1].detail


def test_a_simulation_from_another_engine_fails_the_contract_check() -> None:
    body = payload()
    body["simulation"]["engine"] = "safe_adapter"

    result = probe(Client(body=body))

    assert result.checks[-1].name == "execution_contract"
    assert not result.checks[-1].ok


def test_a_deployment_that_refuses_the_undeployed_account_says_so() -> None:
    refused = OneTxHTTPError(
        "GET", "/instruments/x/calldata", 400, "executor must be provided"
    )

    result = probe(Client(error=refused))

    assert not result.ok
    assert result.checks[-1].name == "request_without_executor"
    assert "executor must be provided" in result.checks[-1].detail


def test_a_response_for_another_account_fails_the_binding_check() -> None:
    result = probe(Client(body=payload(account="0x" + "22" * 20)))

    assert result.checks[-1].name == "bound_to_request"
    assert not result.checks[-1].ok


def test_an_account_with_code_is_not_taken_as_undeployed() -> None:
    client = Client(body=payload())

    result = probe(client, code_reader=lambda _chain, _account: b"\x60\x80")

    assert not result.ok
    assert [check.name for check in result.checks] == ["account_state"]
    assert client.queries == []


def test_an_unreadable_account_state_is_a_failure_not_an_assumption() -> None:
    def unreachable(_chain_id: int, _account: str) -> bytes:
        raise httpx.ConnectError("https://rpc.invalid/key-in-url")

    result = probe(Client(body=payload()), code_reader=unreachable)

    assert not result.ok
    assert "ConnectError" in result.checks[0].detail
    assert "key-in-url" not in result.checks[0].detail


def test_the_same_probe_verifies_a_deployed_account() -> None:
    safe = "0x" + "5a" * 20

    result = probe(
        Client(body=payload(account=safe)),
        account=safe,
        expect_deployed=True,
        code_reader=lambda _chain, _account: b"\x60\x80",
    )

    assert result.ok


# --- the live gate ----------------------------------------------------------


@pytest.mark.integration
def test_live_calldata_compatibility_across_the_active_catalog() -> None:
    """Rollout gate: every active instrument, both account states, wallet gas.

    Per instrument: a deposit and an exact withdrawal probed for an account with
    no code, and a deposit probed for the configured Safe where it is deployed.
    Per paymaster chain: one deposit prepared — estimated, never signed or sent —
    for the configured Safe and for a counterfactual Safe of the same owners,
    with protocol gas and wallet gas reported as separate measurements.
    """
    if os.environ.get("OPEN_ALLOCATOR_LIVE_CALLDATA_PROBE") != "1":
        pytest.skip("set OPEN_ALLOCATOR_LIVE_CALLDATA_PROBE=1 to opt in")
    missing = [
        name
        for name in ("ONE_TX_API_URL", "ONE_TX_API_KEY")
        if not os.environ.get(name)
    ]
    if missing:
        pytest.skip(f"live calldata probe requires: {', '.join(missing)}")

    from web3 import HTTPProvider, Web3

    from open_allocator.core import universe
    from open_allocator.exec import calldata, chains, paymaster_registry
    from open_allocator.exec.bundle_execution import (
        PlannedBundle,
        assemble_plan,
        prepare_plan,
    )
    from open_allocator.exec.client import OneTxClient
    from open_allocator.exec.config import AllocatorConfig
    from open_allocator.exec.signer import signer_from_config

    config = AllocatorConfig(transaction_api="calldata")

    def code(chain_id: int, account: str) -> bytes | None:
        url = chains.rpc_url(chain_id, config)
        if url is None:
            return None
        return bytes(Web3(HTTPProvider(url)).eth.get_code(account))

    failures: list[str] = []
    # Instruments whose undeployed-account deposit probe passed.
    quotable: set[str] = set()
    with OneTxClient(config) as client:
        vaults, _skipped = universe.discover_instruments(client)
        active = [vault for vault in vaults if vault.token_decimals is not None]
        assert active, "discovery returned no instrument with token metadata"

        signer = signer_from_config(config)
        safe = str(signer.address())  # type: ignore[attr-defined]
        for vault in active:
            unit = str(10 ** int(vault.token_decimals))  # type: ignore[arg-type]
            probes = [
                probe_instrument(
                    client,
                    instrument_id=vault.instrument_id,
                    chain_id=vault.chain_id,
                    action=action,
                    amount=unit,
                    code_reader=code,
                    config=config,
                )
                for action in ("deposit", "withdraw")
            ]
            if code(vault.chain_id, safe):
                probes.append(
                    probe_instrument(
                        client,
                        instrument_id=vault.instrument_id,
                        chain_id=vault.chain_id,
                        action="deposit",
                        amount=unit,
                        code_reader=code,
                        account=safe,
                        expect_deployed=True,
                        config=config,
                    )
                )
            if probes[0].ok:
                quotable.add(vault.instrument_id)
            failures.extend(
                f"{item.instrument_id} {item.action} "
                f"{'deployed' if item.expect_deployed else 'undeployed'}: "
                f"{item.checks[-1].name}: {item.checks[-1].detail}"
                for item in probes
                if not item.ok
            )

        if not callable(getattr(signer, "prepare_batch", None)):
            assert not failures, "\n".join(failures)
            return
        counterfactual_config = AllocatorConfig(
            transaction_api="calldata",
            safe_salt_nonce=int.from_bytes(os.urandom(4), "big") + 1,
        )
        counterfactual = signer_from_config(counterfactual_config)
        for chain_id in sorted({vault.chain_id for vault in active}):
            if not paymaster_registry.is_gas_payable(chain_id, provider="pimlico"):
                continue
            usdc = (chains.usdc_address(chain_id, config) or "").casefold()
            vault = next(
                (
                    item
                    for item in active
                    if item.chain_id == chain_id
                    and item.instrument_id in quotable
                    and (item.token_address or "").casefold() == usdc
                ),
                None,
            )
            if vault is None:
                failures.append(f"chain {chain_id}: no quotable USDC instrument")
                continue
            token = calldata.deposit_token(chain_id, active, config)
            for account_signer, account_config in (
                (signer, config),
                (counterfactual, counterfactual_config),
            ):
                account = str(account_signer.address())  # type: ignore[attr-defined]
                deployed = bool(code(chain_id, account))
                if account_signer is counterfactual and deployed:
                    failures.append(f"counterfactual Safe {account} has code")
                    continue
                steps, bundle = calldata.request_bundle(
                    client,
                    instrument_id=vault.instrument_id,
                    action="deposit",
                    account=account,
                    chain_id=chain_id,
                    amount=calldata.deposit_amount_raw("0.1", token),
                    leg_index=0,
                    first_step_index=0,
                    config=account_config,
                    token=token,
                )
                plan = assemble_plan(
                    [PlannedBundle(bundle=bundle, steps=steps)], "compatibility probe"
                )
                try:
                    preparation = prepare_plan(account_signer, plan, account_config)
                except Exception as error:  # noqa: BLE001 - collected for the report
                    failures.append(
                        f"chain {chain_id} wallet estimate for {account}: {error}"
                    )
                    continue
                [prepared] = preparation.preparations
                if prepared.includes_deployment is deployed:
                    failures.append(
                        f"chain {chain_id} {account}: includes_deployment is "
                        f"{prepared.includes_deployment} for a Safe that is "
                        f"{'deployed' if deployed else 'undeployed'}"
                    )
                wallet = prepared.model_dump()
                assert "protocol_gas" not in wallet
                assert int(bundle.protocol_gas) > 0
                assert prepared.call_gas_limit and prepared.verification_gas_limit

    assert not failures, "\n".join(failures)
