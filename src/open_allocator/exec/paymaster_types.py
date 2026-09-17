"""Shared contracts for ERC-4337 paymaster signers and provider adapters."""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import Field

from open_allocator.core.types import FrozenModel
from open_allocator.exec.signer import SignerError


class PaymasterError(SignerError):
    pass


class PaymasterConfigurationError(PaymasterError):
    pass


class PaymasterRejected(PaymasterError):
    pass


class PaymasterPreparationUnavailable(PaymasterError):
    """The adapter can submit an operation but cannot prepare one unsent."""


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


class UserOperationGas(FrozenModel):
    """The wallet gas of one complete operation, as the bundler estimated it."""

    call_gas_limit: int = Field(ge=0)
    verification_gas_limit: int = Field(ge=0)
    pre_verification_gas: int = Field(ge=0)
    paymaster_verification_gas_limit: int | None = Field(default=None, ge=0)
    paymaster_post_op_gas_limit: int | None = Field(default=None, ge=0)
    max_fee_per_gas: int = Field(ge=0)
    max_priority_fee_per_gas: int = Field(ge=0)


class PaymasterTokenQuote(FrozenModel):
    """The paymaster and gas-token rate the operation was estimated against."""

    paymaster: str
    token: str
    exchange_rate: int | None = Field(default=None, ge=0)
    post_op_gas: int | None = Field(default=None, ge=0)
    # Whether the paymaster's token approval rides in front of the calls.
    approval_included: bool


class PreparedUserOperation(FrozenModel):
    """A complete operation built and estimated, but neither signed nor sent.

    Everything in it — nonce, fees, paymaster data, the estimate — goes stale,
    so it is for reporting and validation only. Submission prepares afresh
    immediately before signing rather than sending one of these.
    """

    sender: str
    chain_id: int = Field(ge=1)
    entry_point: str
    # The unsigned operation, carrying a stub signature and stub paymaster data.
    user_operation: dict[str, Any]
    deployed: bool
    factory: str | None = None
    factory_data: str | None = None
    gas: UserOperationGas
    paymaster: PaymasterTokenQuote
    # The most gas token the operation can be charged. None when the adapter
    # cannot bound it defensibly: such an operation may still spend a funded
    # balance, but must not be the one a bridged mint has to pay for.
    max_gas_token_charge_raw: str | None = Field(default=None, pattern=r"^\d+$")

    @property
    def includes_deployment(self) -> bool:
        return not self.deployed


@runtime_checkable
class PaymasterUserOperationAdapter(Protocol):
    def address(self) -> str: ...

    def submit_user_operation(
        self,
        request: PaymasterUserOperationRequest,
    ) -> PaymasterUserOperationSubmission: ...


@runtime_checkable
class PreparingPaymasterUserOperationAdapter(PaymasterUserOperationAdapter, Protocol):
    """An adapter that can build and estimate an operation without sending it."""

    def prepare_user_operation(
        self,
        request: PaymasterUserOperationRequest,
    ) -> PreparedUserOperation: ...
