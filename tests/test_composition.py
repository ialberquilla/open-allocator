from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from open_allocator.exec.composition import (
    LEGACY_SIGNER_MODES,
    SignerComposition,
    UnknownSignerMode,
    composition_from_config,
)
from open_allocator.exec.config import AllocatorConfig
from open_allocator.exec.erc4337_paymaster import (
    Erc4337PaymasterSigner,
    submits_via_paymaster,
)
from open_allocator.exec.signer import (
    LocalEoaSigner,
    RemoteSigner,
    SafeSigner,
    UnsupportedComposition,
    signer_from_config,
)

VALID_PRIVATE_KEY = "0x" + "11" * 32
SAFE_ADDRESS = "0x0000000000000000000000000000000000000afe"


@pytest.fixture(autouse=True)
def clear_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if (
            name.startswith("ONE_TX_")
            or name.startswith("REMOTE_SIGNER_")
            or name.startswith("SAFE_")
            or name.startswith("PAYMASTER_")
            or name.startswith("SIGNER_")
        ):
            monkeypatch.delenv(name, raising=False)


def set_base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONE_TX_API_URL", "http://localhost:3001/api/v1")
    monkeypatch.setenv("ONE_TX_API_KEY", "test-api-key")


def set_local_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONE_TX_PRIVATE_KEY", VALID_PRIVATE_KEY)


def set_remote_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REMOTE_SIGNER_PROVIDER", "generic-http")
    monkeypatch.setenv("REMOTE_SIGNER_URL", "https://signer.example")
    monkeypatch.setenv("REMOTE_SIGNER_CREDENTIAL", "remote-credential")
    monkeypatch.setenv("REMOTE_SIGNER_KEY_ID", "key-1")


def set_safe_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAFE_ADDRESS", SAFE_ADDRESS)
    monkeypatch.setenv("SAFE_TRANSACTION_SERVICE_URL", "https://safe.example")
    monkeypatch.setenv("SAFE_CHAIN_ID", "8453")


def set_paymaster_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYMASTER_PROVIDER", "generic-http")
    monkeypatch.setenv("PAYMASTER_BUNDLER_URL", "https://bundler.example")
    monkeypatch.setenv("PAYMASTER_URL", "https://paymaster.example")
    monkeypatch.setenv(
        "PAYMASTER_ACCOUNT_ADDRESS",
        "0x0000000000000000000000000000000000000aaa",
    )
    monkeypatch.setenv(
        "PAYMASTER_ENTRY_POINT",
        "0x0000000000000000000000000000000000004337",
    )
    monkeypatch.setenv(
        "PAYMASTER_USDC_ADDRESS",
        "0x0000000000000000000000000000000000000c0c",
    )


# --- legacy SIGNER_MODE keeps working ------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected"),
    sorted((mode, tuple(comp)) for mode, comp in LEGACY_SIGNER_MODES.items()),
)
def test_legacy_signer_mode_expands_to_expected_axes(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected: tuple[str, str, str],
) -> None:
    set_base_env(monkeypatch)
    set_local_key(monkeypatch)
    set_remote_env(monkeypatch)
    set_safe_env(monkeypatch)
    set_paymaster_env(monkeypatch)
    monkeypatch.setenv("SIGNER_MODE", mode)

    assert tuple(AllocatorConfig().composition) == expected


@pytest.mark.parametrize(
    ("mode", "expected_signer"),
    [
        ("local-eoa", LocalEoaSigner),
        ("remote", RemoteSigner),
        ("safe", SafeSigner),
        ("erc4337-paymaster", Erc4337PaymasterSigner),
    ],
)
def test_legacy_signer_mode_builds_the_same_signer_as_before(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected_signer: type,
) -> None:
    set_base_env(monkeypatch)
    set_local_key(monkeypatch)
    set_remote_env(monkeypatch)
    set_safe_env(monkeypatch)
    set_paymaster_env(monkeypatch)
    monkeypatch.setenv("SIGNER_MODE", mode)

    assert isinstance(signer_from_config(AllocatorConfig()), expected_signer)


def test_default_composition_is_the_local_eoa_quickstart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_base_env(monkeypatch)
    set_local_key(monkeypatch)

    config = AllocatorConfig()

    assert config.composition == SignerComposition("eoa", "rpc", "local")
    assert config.signer_mode == "local-eoa"


def test_signer_mode_is_rederived_from_axes(monkeypatch: pytest.MonkeyPatch) -> None:
    set_base_env(monkeypatch)
    set_safe_env(monkeypatch)
    monkeypatch.setenv("SIGNER_ACCOUNT", "safe")

    # Configured purely by axis, but legacy readers still see a mode.
    assert AllocatorConfig().signer_mode == "safe"


def test_composition_without_a_legacy_name_has_no_signer_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_base_env(monkeypatch)
    set_safe_env(monkeypatch)
    set_paymaster_env(monkeypatch)
    monkeypatch.setenv("SIGNER_ACCOUNT", "safe")
    monkeypatch.setenv("SIGNER_SUBMISSION", "erc4337-paymaster")

    assert AllocatorConfig().signer_mode is None


def test_unknown_signer_mode_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    set_base_env(monkeypatch)
    set_local_key(monkeypatch)
    monkeypatch.setenv("SIGNER_MODE", "custodial-vendor")

    with pytest.raises(ValidationError) as error:
        AllocatorConfig()

    assert "custodial-vendor" in str(error.value)


def test_signer_mode_contradicting_an_axis_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_base_env(monkeypatch)
    set_local_key(monkeypatch)
    monkeypatch.setenv("SIGNER_MODE", "safe")
    monkeypatch.setenv("SIGNER_ACCOUNT", "eoa")

    with pytest.raises(ValidationError) as error:
        AllocatorConfig()

    assert "SIGNER_MODE=safe implies SIGNER_ACCOUNT=safe" in str(error.value)


# --- the axes v3 actually needs ------------------------------------------


def test_safe_with_paymaster_is_expressible(monkeypatch: pytest.MonkeyPatch) -> None:
    set_base_env(monkeypatch)
    set_safe_env(monkeypatch)
    set_paymaster_env(monkeypatch)
    monkeypatch.setenv("SIGNER_ACCOUNT", "safe")
    monkeypatch.setenv("SIGNER_SUBMISSION", "erc4337-paymaster")

    config = AllocatorConfig()

    # The composition the old flat enum could not name at all.
    assert config.composition == SignerComposition("safe", "erc4337-paymaster", "local")
    assert submits_via_paymaster(config)


def test_safe_with_remote_owner_is_expressible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_base_env(monkeypatch)
    set_safe_env(monkeypatch)
    set_remote_env(monkeypatch)
    monkeypatch.setenv("SIGNER_ACCOUNT", "safe")
    monkeypatch.setenv("SIGNER_OWNER", "remote")

    config = AllocatorConfig()

    assert config.composition == SignerComposition("safe", "rpc", "remote")


def test_safe_with_paymaster_builds_a_safe_user_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_base_env(monkeypatch)
    set_safe_env(monkeypatch)
    set_paymaster_env(monkeypatch)
    monkeypatch.setenv("SIGNER_ACCOUNT", "safe")
    monkeypatch.setenv("SIGNER_SUBMISSION", "erc4337-paymaster")

    # The composition this phase exists to deliver: a Safe paying its own gas in
    # USDC. Every Safe we derive is 4337-enabled at setup, so the module is
    # already present to receive validateUserOp.
    signer = signer_from_config(AllocatorConfig())

    assert isinstance(signer, Erc4337PaymasterSigner)
    # The userOp must be signed as a Safe, not as a bare smart account — they
    # sign differently, and getting it wrong fails only at the bundler.
    assert signer._account_type == "safe"


def test_safe_with_remote_owner_awaits_its_wiring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_base_env(monkeypatch)
    set_safe_env(monkeypatch)
    set_remote_env(monkeypatch)
    monkeypatch.setenv("SIGNER_ACCOUNT", "safe")
    monkeypatch.setenv("SIGNER_OWNER", "remote")

    with pytest.raises(UnsupportedComposition) as error:
        signer_from_config(AllocatorConfig())

    assert "06-04" in str(error.value)


# --- per-axis requirements compose ---------------------------------------


def test_safe_axis_requires_an_address_or_a_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_base_env(monkeypatch)
    monkeypatch.setenv("SIGNER_ACCOUNT", "safe")

    with pytest.raises(ValidationError) as error:
        AllocatorConfig()

    # Either name an existing Safe or give the seed to derive one (06-02).
    assert "SAFE_ADDRESS" in str(error.value)
    assert "SAFE_OWNERS" in str(error.value)


def test_remote_owner_axis_requires_remote_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_base_env(monkeypatch)
    monkeypatch.setenv("SIGNER_OWNER", "remote")

    with pytest.raises(ValidationError) as error:
        AllocatorConfig()

    assert "REMOTE_SIGNER_PROVIDER is required when SIGNER_OWNER=remote" in str(
        error.value
    )


def test_safe_with_remote_owner_requires_both_axes_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_base_env(monkeypatch)
    set_safe_env(monkeypatch)
    monkeypatch.setenv("SIGNER_ACCOUNT", "safe")
    monkeypatch.setenv("SIGNER_OWNER", "remote")

    # Requirements from both axes apply; the safe half is satisfied.
    with pytest.raises(ValidationError) as error:
        AllocatorConfig()

    message = str(error.value)
    assert "SIGNER_OWNER=remote" in message
    assert "SAFE_ADDRESS" not in message


def test_raw_key_not_required_when_another_axis_signs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_base_env(monkeypatch)
    set_safe_env(monkeypatch)
    monkeypatch.setenv("SIGNER_ACCOUNT", "safe")

    # A Safe signs via its proposer credential, so no raw key on disk.
    assert AllocatorConfig().private_key is None


# --- duck-typed configs still resolve -------------------------------------


def test_legacy_only_config_object_still_resolves() -> None:
    class LegacyConfig:
        signer_mode = "safe"

    assert composition_from_config(LegacyConfig()) == SignerComposition(
        "safe",
        "rpc",
        "local",
    )


def test_config_object_with_no_signer_hints_defaults() -> None:
    class Bare:
        pass

    assert composition_from_config(Bare()) == SignerComposition("eoa", "rpc", "local")


def test_duck_typed_unknown_mode_is_rejected() -> None:
    class LegacyConfig:
        signer_mode = "nonsense"

    with pytest.raises(UnknownSignerMode):
        composition_from_config(LegacyConfig())
