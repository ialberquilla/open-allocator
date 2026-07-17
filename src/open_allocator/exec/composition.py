from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, NamedTuple

Account = Literal["eoa", "safe"]
Submission = Literal["rpc", "erc4337-paymaster"]
OwnerSigner = Literal["local", "remote"]

ACCOUNTS: tuple[Account, ...] = ("eoa", "safe")
SUBMISSIONS: tuple[Submission, ...] = ("rpc", "erc4337-paymaster")
OWNER_SIGNERS: tuple[OwnerSigner, ...] = ("local", "remote")


class SignerComposition(NamedTuple):
    account: Account
    submission: Submission
    owner_signer: OwnerSigner


LEGACY_SIGNER_MODES: Mapping[str, SignerComposition] = {
    "local-eoa": SignerComposition("eoa", "rpc", "local"),
    "remote": SignerComposition("eoa", "rpc", "remote"),
    "safe": SignerComposition("safe", "rpc", "local"),
    "erc4337-paymaster": SignerComposition("eoa", "erc4337-paymaster", "local"),
}

DEFAULT_COMPOSITION = LEGACY_SIGNER_MODES["local-eoa"]

_AXIS_ENV_NAMES: Mapping[str, str] = {
    "account": "SIGNER_ACCOUNT",
    "submission": "SIGNER_SUBMISSION",
    "owner_signer": "SIGNER_OWNER",
}


class UnknownSignerMode(ValueError):
    def __init__(self, mode: object) -> None:
        known = ", ".join(sorted(LEGACY_SIGNER_MODES))
        super().__init__(f"unknown SIGNER_MODE {mode!r}; expected one of {known}")


def axis_env_name(axis: str) -> str:
    return _AXIS_ENV_NAMES[axis]


def legacy_signer_mode(composition: SignerComposition) -> str | None:
    """The SIGNER_MODE that names this composition, if one does."""
    for mode, known in LEGACY_SIGNER_MODES.items():
        if known == composition:
            return mode
    return None


def composition_from_config(config: object) -> SignerComposition:
    """Resolve the composition from either the axes or a legacy SIGNER_MODE.

    Accepts any config-shaped object, so duck-typed callers that only carry a
    signer_mode keep working.
    """
    account = getattr(config, "account", None)
    submission = getattr(config, "submission", None)
    owner_signer = getattr(config, "owner_signer", None)
    if account is not None and submission is not None and owner_signer is not None:
        return SignerComposition(account, submission, owner_signer)

    mode = getattr(config, "signer_mode", None)
    if mode is None:
        return DEFAULT_COMPOSITION
    try:
        return LEGACY_SIGNER_MODES[mode]
    except KeyError as error:
        raise UnknownSignerMode(mode) from error
