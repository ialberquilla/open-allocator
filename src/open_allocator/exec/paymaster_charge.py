"""The most gas token Pimlico's ERC-20 paymaster can charge one operation.

Derived from the deployed contract, not from the quote's documentation:
``SingletonPaymasterV7`` (pimlicolabs/singleton-paymaster ``1e2305d``) charges in
``postOp``::

    penalty = (executionGasLimit - executionGasUsed) * 10 / 100 * feePerGas
    cost    = (actualGasCost + penalty + postOpGas * feePerGas) * exchangeRate / 1e18
              + constantFee

with ``executionGasLimit = callGasLimit + paymasterPostOpGasLimit``. Each input
is bounded by the operation as built: the EntryPoint refuses an operation whose
``actualGasCost`` exceeds its prefund (every gas limit times ``maxFeePerGas``),
the fee per gas never exceeds ``maxFeePerGas``, and the penalty never exceeds
10% of the execution limit. So::

    max = ((VGL + CGL + PVG + PMVGL + PMPOGL) + (CGL + PMPOGL) // 10 + postOpGas)
          * maxFeePerGas * exchangeRate // 1e18 + constantFee

Checked 2026-09-17 against 26 ERC-20-mode charges the paymaster emitted on Base
mainnet (``UserOperationSponsored.tokenAmountPaid``, with limits and paymaster
config decoded from each ``handleOps`` input): every charge sat inside this
bound, the largest at 52% of it, and every charge was within 0.96-1.01x of
``actualGasCost * exchangeRate / 1e18`` — so the 1e18 scaling is the real one.

What it cannot bound: a sponsorship signed at a higher rate than the one the
operation was prepared against. Submission prepares and sponsors afresh
seconds later; the bound's headroom over real charges is what absorbs that
drift, not a guarantee.
"""

from __future__ import annotations

from pydantic import Field

from open_allocator.core.types import FrozenModel
from open_allocator.exec.paymaster_types import UserOperationGas

ERC20_MODE = 1
_PENALTY_PERCENT = 10
_RATE_SCALE = 10**18

# paymasterData layout for ERC-20 mode, after the 52 bytes of paymaster address
# and gas limits that precede it in paymasterAndData.
_MODE_BYTES = 1
_FIXED_CONFIG_BYTES = 1 + 6 + 6 + 20 + 16 + 32 + 16 + 20
_PREFUND_PRESENT = 0x04
_CONSTANT_FEE_PRESENT = 0x01


class Erc20PaymasterConfig(FrozenModel):
    """The charge-relevant fields of an ERC-20-mode ``paymasterData``."""

    token: str
    post_op_gas: int = Field(ge=0)
    exchange_rate: int = Field(ge=0)
    constant_fee: int = Field(default=0, ge=0)


def parse_erc20_paymaster_data(paymaster_data: object) -> Erc20PaymasterConfig | None:
    """Decode ``paymasterData``, or None when it is not ERC-20 mode as laid out.

    The stub data Pimlico returns for estimation carries the same config the
    sponsorship will — token, postOpGas, rate, fee flags — so the bound can be
    taken before anything is signed. Anything that does not parse is None,
    never a guessed config.
    """
    if not isinstance(paymaster_data, str) or not paymaster_data.startswith("0x"):
        return None
    try:
        data = bytes.fromhex(paymaster_data[2:])
    except ValueError:
        return None
    if len(data) < _MODE_BYTES + _FIXED_CONFIG_BYTES:
        return None
    if data[0] >> 1 != ERC20_MODE:
        return None
    config = data[_MODE_BYTES:]
    flags = config[0]
    token = "0x" + config[13:33].hex()
    post_op_gas = int.from_bytes(config[33:49], "big")
    exchange_rate = int.from_bytes(config[49:81], "big")
    pointer = _FIXED_CONFIG_BYTES
    if flags & _PREFUND_PRESENT:
        pointer += 16
    constant_fee = 0
    if flags & _CONSTANT_FEE_PRESENT:
        if len(config) < pointer + 16:
            return None
        constant_fee = int.from_bytes(config[pointer : pointer + 16], "big")
    return Erc20PaymasterConfig(
        token=token,
        post_op_gas=post_op_gas,
        exchange_rate=exchange_rate,
        constant_fee=constant_fee,
    )


def max_token_charge(
    gas: UserOperationGas,
    *,
    post_op_gas: int,
    exchange_rate: int,
    constant_fee: int = 0,
) -> int | None:
    """The bound above, or None when the estimate lacks a limit it needs."""
    if (
        gas.paymaster_verification_gas_limit is None
        or gas.paymaster_post_op_gas_limit is None
    ):
        return None
    limits = (
        gas.verification_gas_limit
        + gas.call_gas_limit
        + gas.pre_verification_gas
        + gas.paymaster_verification_gas_limit
        + gas.paymaster_post_op_gas_limit
    )
    execution_limit = gas.call_gas_limit + gas.paymaster_post_op_gas_limit
    penalty = execution_limit * _PENALTY_PERCENT // 100
    gas_units = limits + penalty + post_op_gas
    return gas_units * gas.max_fee_per_gas * exchange_rate // _RATE_SCALE + constant_fee


__all__ = [
    "Erc20PaymasterConfig",
    "max_token_charge",
    "parse_erc20_paymaster_data",
]
