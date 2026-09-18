"""Builders for CCTP V2 burns, messages, and logs as the chain encodes them."""

from __future__ import annotations

from typing import Any

from eth_abi import encode as abi_encode
from eth_utils import keccak

from open_allocator.exec.cctp import MESSAGE_SENT_TOPIC

SAFE = "0x1111111111111111111111111111111111111111"
OTHER = "0x2222222222222222222222222222222222222222"
BASE = 8453
ARBITRUM = 42161
BASE_DOMAIN = 6
ARBITRUM_DOMAIN = 3
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ARBITRUM_USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
TOKEN_MESSENGER = "0x28b5a0e9C621a5BadaA536219b3a228C8168cf5d"
MESSAGE_TRANSMITTER = "0x81D40F21F12A8F0E3252Bccb954D722d4c464B64"


def word(address: str) -> bytes:
    return bytes(12) + bytes.fromhex(address[2:])


def burn_message(
    *,
    amount: int,
    max_fee: int = 12_000,
    sender: str = SAFE,
    recipient: str = SAFE,
    caller: str = SAFE,
    token: str = BASE_USDC,
    source_domain: int = BASE_DOMAIN,
    destination_domain: int = ARBITRUM_DOMAIN,
    source_messenger: str = TOKEN_MESSENGER,
    destination_messenger: str = TOKEN_MESSENGER,
    threshold: int = 1000,
    nonce: int = 0,
    executed: int = 0,
    fee_executed: int = 0,
    expiration: int = 0,
    version: int = 1,
) -> bytes:
    """A MessageV2 carrying a BurnMessageV2; zeroed Circle fields by default."""
    body = (
        (1).to_bytes(4, "big")
        + word(token)
        + word(recipient)
        + amount.to_bytes(32, "big")
        + word(sender)
        + max_fee.to_bytes(32, "big")
        + fee_executed.to_bytes(32, "big")
        + expiration.to_bytes(32, "big")
    )
    return (
        version.to_bytes(4, "big")
        + source_domain.to_bytes(4, "big")
        + destination_domain.to_bytes(4, "big")
        + nonce.to_bytes(32, "big")
        + word(source_messenger)
        + word(destination_messenger)
        + word(caller)
        + threshold.to_bytes(4, "big")
        + executed.to_bytes(4, "big")
        + body
    )


def attested(
    source: bytes,
    *,
    nonce: int,
    fee_executed: int = 0,
    executed: int = 1000,
    expiration: int = 0,
) -> bytes:
    raw = bytearray(source)
    raw[12:44] = nonce.to_bytes(32, "big")
    raw[144:148] = executed.to_bytes(4, "big")
    raw[148 + 164 : 148 + 196] = fee_executed.to_bytes(32, "big")
    raw[148 + 196 : 148 + 228] = expiration.to_bytes(32, "big")
    return bytes(raw)


def burn_calls(
    amount: int,
    *,
    max_fee: int = 12_000,
    chain_id: int = BASE,
    token: str = BASE_USDC,
    recipient: str = SAFE,
    caller: str = SAFE,
    destination_domain: int = ARBITRUM_DOMAIN,
    threshold: int = 1000,
    approval: int | None = None,
) -> list[dict[str, Any]]:
    approve = keccak(text="approve(address,uint256)")[:4] + abi_encode(
        ["address", "uint256"],
        [TOKEN_MESSENGER, amount if approval is None else approval],
    )
    burn = keccak(
        text="depositForBurn(uint256,uint32,bytes32,address,bytes32,uint256,uint32)"
    )[:4] + abi_encode(
        ["uint256", "uint32", "bytes32", "address", "bytes32", "uint256", "uint32"],
        [
            amount,
            destination_domain,
            word(recipient),
            token,
            word(caller),
            max_fee,
            threshold,
        ],
    )
    return [
        {
            "to": token,
            "data": "0x" + approve.hex(),
            "value": "0",
            "chainId": chain_id,
            "type": "approve",
        },
        {
            "to": TOKEN_MESSENGER,
            "data": "0x" + burn.hex(),
            "value": "0",
            "chainId": chain_id,
            "type": "bridge_burn",
        },
    ]


def bridge_response(
    amount: int,
    *,
    account: str = SAFE,
    from_chain_id: int = BASE,
    to_chain_id: int = ARBITRUM,
    fast: bool = True,
    max_fee: int = 12_000,
    calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    token = BASE_USDC if from_chain_id == BASE else ARBITRUM_USDC
    return {
        "fromChainId": from_chain_id,
        "toChainId": to_chain_id,
        "sourceDomain": BASE_DOMAIN if from_chain_id == BASE else ARBITRUM_DOMAIN,
        "destinationDomain": ARBITRUM_DOMAIN
        if to_chain_id == ARBITRUM
        else BASE_DOMAIN,
        "account": account,
        "amount": str(amount),
        "token": token,
        "fast": fast,
        "maxFee": str(max_fee),
        "minFinalityThreshold": 1000 if fast else 2000,
        "quoteBlock": 35_123_470,
        "requires": [{"token": token, "amount": str(amount)}],
        "calls": calls
        if calls is not None
        else burn_calls(
            amount,
            max_fee=max_fee,
            chain_id=from_chain_id,
            token=token,
            destination_domain=ARBITRUM_DOMAIN
            if to_chain_id == ARBITRUM
            else BASE_DOMAIN,
            threshold=1000 if fast else 2000,
            recipient=account,
            caller=account,
        ),
        "simulation": {
            "ok": True,
            "scope": "protocol_bundle",
            "engine": "wallet_neutral_atomic",
            "gasUsed": "154321",
            "quoteBlock": 35_123_470,
            "tokenOutDelta": "0",
            "assumedBalances": True,
        },
    }


def message_sent_log(
    message: bytes,
    *,
    log_index: int,
    emitter: str = MESSAGE_TRANSMITTER,
) -> dict[str, Any]:
    return {
        "address": emitter,
        "topics": [MESSAGE_SENT_TOPIC],
        "data": "0x" + abi_encode(["bytes"], [message]).hex(),
        "logIndex": log_index,
    }


def cctp_config_payload() -> dict[str, Any]:
    return {
        "supportedChains": [
            {
                "chainId": chain_id,
                "name": name,
                "cctpDomain": domain,
                "tokenMessenger": TOKEN_MESSENGER,
                "messageTransmitter": MESSAGE_TRANSMITTER,
                "cctpReceiver": "0x0000000000000000000000000000000000000abc",
            }
            for chain_id, name, domain in (
                (BASE, "Base", BASE_DOMAIN),
                (ARBITRUM, "Arbitrum", ARBITRUM_DOMAIN),
            )
        ]
    }
