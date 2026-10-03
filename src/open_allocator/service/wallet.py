"""Wallet readiness: balances, gas, and the Safe smart account address."""

from __future__ import annotations

from collections.abc import Sequence

from open_allocator.exec import chains, safe_deployment
from open_allocator.exec.client import OneTxClient
from open_allocator.exec.config import AllocatorConfig
from open_allocator.service._common import JsonObject, model_payload, signer_address
from open_allocator.service.errors import ServiceError


def wallet_status() -> JsonObject:
    config = AllocatorConfig()
    address = signer_address(config)

    with OneTxClient(config) as client:
        balances_response = client.balances(address)

    from open_allocator.exec.erc4337_paymaster import submits_via_paymaster

    balances_payload = _normalize_balances_response(balances_response)
    # Which readiness question to ask depends on how the transaction reaches the
    # chain. A smart account paying gas in USDC holds no native token anywhere by
    # design, so asking for a native balance answers a question this wallet never
    # has to satisfy.
    gas_status = (
        _paymaster_gas_status if submits_via_paymaster(config) else _native_gas_status
    )
    balances = []
    for balance in balances_payload["balances"]:
        chain_id = int(balance["chain_id"])
        balances.append(
            {
                **balance,
                **gas_status(address, chain_id, config),
            }
        )

    return {
        "address": address,
        "balances": balances,
        "total_usdc_usd": balances_payload.get("total_usdc_usd"),
    }


def _safe_seed_from_config(config: AllocatorConfig) -> safe_deployment.SafeSeed:
    if config.safe_owners is None or config.safe_threshold is None:
        raise ServiceError(
            "invalid_config",
            "safe-address needs SAFE_OWNERS + SAFE_THRESHOLD to derive the "
            "counterfactual address",
        )
    return safe_deployment.SafeSeed(
        owners=config.safe_owners,
        threshold=config.safe_threshold,
        salt_nonce=config.safe_salt_nonce,
    )


def safe_address(chain_ids: tuple[int, ...] | None = None) -> JsonObject:
    from web3 import HTTPProvider, Web3

    config = AllocatorConfig()
    if config.account != "safe":
        raise ServiceError(
            "invalid_config", "safe-address requires SIGNER_ACCOUNT=safe"
        )

    # No chain is configured in the common case, so fall back to the one the
    # address would be derived from. The address is the same everywhere; the
    # chain only decides who answers the eth_call.
    targets = chain_ids or (safe_deployment.derivation_chain_id(config),)

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


def _normalize_balances_response(response: object) -> JsonObject:
    payload = model_payload(response)
    raw_balances = payload.get("balances", [])
    if not isinstance(raw_balances, Sequence) or isinstance(
        raw_balances,
        str | bytes | bytearray,
    ):
        raise TypeError("balances response did not contain a balances array")

    balances: list[JsonObject] = []
    for raw_balance in raw_balances:
        balance_payload = model_payload(raw_balance)
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
            "gas_mode": "native",
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
            "gas_mode": "native",
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
        "gas_mode": "native",
        "rpc_available": True,
        "rpc_executable": True,
        "native_gas_balance_wei": balance_wei,
        "native_gas_required_wei": required_wei,
        "native_gas_available": gas_available,
        "executable": gas_available,
        "not_executable": not gas_available,
        "not_executable_reasons": [] if gas_available else ["insufficient_native_gas"],
    }


def _paymaster_gas_status(_address: str, chain_id: int, config: object) -> JsonObject:
    """Readiness on a chain whose gas is paid in USDC by the smart account.

    There is no native balance to have: the paymaster fronts the native gas and
    pulls USDC, and an exit funds itself from what it redeems, so a chain where
    the account holds nothing at all is still executable. What decides it is
    whether the paymaster can price this chain, whether its gas token is known,
    and whether an RPC exists to read the account's nonce and deployment status.

    USDC balance is deliberately not part of the verdict — it is already on the
    row, and requiring it would mark exactly the self-funding exits unusable.
    """
    from open_allocator.exec.erc4337_paymaster import (
        PaymasterError,
        PaymasterUnsupportedChain,
        usdc_address_for_chain,
        validate_paymaster_preflight,
    )

    reasons: list[str] = []
    message: str | None = None

    rpc_url = chains.rpc_url(chain_id, config)
    if rpc_url is None:
        reasons.append("missing_rpc")

    gas_token: str | None = None
    try:
        validate_paymaster_preflight(config, (chain_id,))
        gas_token = usdc_address_for_chain(config, chain_id)
    except PaymasterUnsupportedChain as error:
        reasons.append("chain_not_gas_payable")
        message = str(error)
    except PaymasterError as error:
        reasons.append("paymaster_not_configured")
        message = str(error)

    status: JsonObject = {
        "gas_mode": "usdc_paymaster",
        "gas_token": "USDC",
        "gas_token_address": gas_token,
        "rpc_available": rpc_url is not None,
        "rpc_executable": rpc_url is not None,
        # Not zero — inapplicable. This account never holds a native token.
        "native_gas_balance_wei": None,
        "native_gas_required_wei": 0,
        "native_gas_available": None,
        "executable": not reasons,
        "not_executable": bool(reasons),
        "not_executable_reasons": reasons,
    }
    if message is not None:
        status["message"] = message
    return status
