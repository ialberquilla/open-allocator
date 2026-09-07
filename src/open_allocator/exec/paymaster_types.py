"""Shared contracts for ERC-4337 paymaster signers and provider adapters."""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import Field

from open_allocator.core.types import FrozenModel
from open_allocator.exec.signer import SignerError


class PaymasterError(SignerError):
    pass


class PaymasterConfigurationError(PaymasterError):
    pass


class PaymasterRejected(PaymasterError):
    pass


class PaymasterUnsupportedChain(PaymasterError):
    def __init__(self, chain_id: int) -> None:
        self.chain_id = chain_id
        super().__init__(
            f"ERC-4337 USDC paymaster is not configured for chain {chain_id}"
        )


class UserOperationCall(FrozenModel):
    to: str
    data: str
    value: int = Field(ge=0)


class PaymasterUserOperationRequest(FrozenModel):
    sender: str
    chain_id: int
    entry_point: str
    # A sequence because a smart account can batch: the calls of one plan ride in
    # a single operation, so the gas the paymaster pulls in postOp can be paid
    # out of USDC the same operation just produced.
    calls: tuple[UserOperationCall, ...] = Field(min_length=1)
    gas_token: Literal["USDC"] = "USDC"
    gas_token_address: str
    account_type: Literal["smart-account", "safe"] = "smart-account"

    @property
    def call_data(self) -> UserOperationCall:
        """The first call — the whole operation when it is not a batch."""
        return self.calls[0]


class PaymasterUserOperationSubmission(FrozenModel):
    user_op_hash: str
    transaction_hash: str | None = None
    status: Literal["submitted", "included"] = "submitted"
    block_number: int = Field(default=0, ge=0)
    gas_used: int = Field(default=0, ge=0)
    message: str | None = None


@runtime_checkable
class PaymasterUserOperationAdapter(Protocol):
    def address(self) -> str: ...

    def submit_user_operation(
        self,
        request: PaymasterUserOperationRequest,
    ) -> PaymasterUserOperationSubmission: ...
