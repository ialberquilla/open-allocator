import os

import pytest
from pydantic import ValidationError

from open_allocator.exec.chains import DEFAULT_RPC_URLS
from open_allocator.exec.config import AllocatorConfig

VALID_PRIVATE_KEY = "0x" + "11" * 32
REFERRAL_WALLET = "0x0000000000000000000000000000000000000000"


@pytest.fixture(autouse=True)
def clear_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if (
            name.startswith("ONE_TX_")
            or name.startswith("RPC_URL_")
            or name.startswith("REMOTE_SIGNER_")
            or name.startswith("SAFE_")
            or name.startswith("PAYMASTER_")
            or name == "SIGNER_MODE"
            or name == "OPEN_ALLOCATOR_IDEMPOTENCY_STORE"
            or name == "OPEN_ALLOCATOR_CHECKPOINT_DIR"
            or name == "OPEN_ALLOCATOR_ALLOCATION_LOG"
        ):
            monkeypatch.delenv(name, raising=False)


def set_valid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONE_TX_API_URL", "http://localhost:3001/api/v1")
    monkeypatch.setenv("ONE_TX_API_KEY", "test-api-key")
    monkeypatch.setenv("ONE_TX_PRIVATE_KEY", VALID_PRIVATE_KEY)


def set_valid_remote_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONE_TX_API_URL", "http://localhost:3001/api/v1")
    monkeypatch.setenv("ONE_TX_API_KEY", "test-api-key")
    monkeypatch.setenv("SIGNER_MODE", "remote")
    monkeypatch.setenv("REMOTE_SIGNER_PROVIDER", "generic-http")
    monkeypatch.setenv("REMOTE_SIGNER_URL", "https://signer.example")
    monkeypatch.setenv("REMOTE_SIGNER_CREDENTIAL", "remote-credential")
    monkeypatch.setenv("REMOTE_SIGNER_KEY_ID", "key-1")


def set_valid_safe_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONE_TX_API_URL", "http://localhost:3001/api/v1")
    monkeypatch.setenv("ONE_TX_API_KEY", "test-api-key")
    monkeypatch.setenv("SIGNER_MODE", "safe")
    monkeypatch.setenv("SAFE_ADDRESS", "0x0000000000000000000000000000000000000afe")
    monkeypatch.setenv("SAFE_TRANSACTION_SERVICE_URL", "https://safe.example")
    monkeypatch.setenv("SAFE_CHAIN_ID", "8453")


def set_valid_paymaster_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONE_TX_API_URL", "http://localhost:3001/api/v1")
    monkeypatch.setenv("ONE_TX_API_KEY", "test-api-key")
    monkeypatch.setenv("SIGNER_MODE", "erc4337-paymaster")
    monkeypatch.setenv("PAYMASTER_PROVIDER", "generic-http")
    monkeypatch.setenv("PAYMASTER_BUNDLER_URL", "https://bundler.example")
    monkeypatch.setenv("PAYMASTER_BUNDLER_CREDENTIAL", "bundler-credential")
    monkeypatch.setenv("PAYMASTER_URL", "https://paymaster.example")
    monkeypatch.setenv("PAYMASTER_CREDENTIAL", "paymaster-credential")
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


@pytest.mark.parametrize("missing", ["ONE_TX_API_URL", "ONE_TX_API_KEY"])
def test_missing_required_onetx_vars_raise_clear_validation_error(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    set_valid_env(monkeypatch)
    monkeypatch.delenv(missing)

    with pytest.raises(ValidationError) as error:
        AllocatorConfig()

    assert missing in str(error.value)


def test_local_eoa_requires_private_key(monkeypatch: pytest.MonkeyPatch) -> None:
    set_valid_env(monkeypatch)
    monkeypatch.delenv("ONE_TX_PRIVATE_KEY")

    with pytest.raises(ValidationError) as error:
        AllocatorConfig()

    assert "ONE_TX_PRIVATE_KEY" in str(error.value)


@pytest.mark.parametrize("private_key", ["0x1234", "not-hex", "11" * 32])
def test_local_eoa_rejects_bad_private_key(
    monkeypatch: pytest.MonkeyPatch,
    private_key: str,
) -> None:
    set_valid_env(monkeypatch)
    monkeypatch.setenv("ONE_TX_PRIVATE_KEY", private_key)

    with pytest.raises(ValidationError) as error:
        AllocatorConfig()

    assert "32-byte 0x-prefixed hex" in str(error.value)


@pytest.mark.parametrize("signer_mode", ["remote", "safe", "erc4337-paymaster"])
def test_private_key_not_validated_for_non_local_modes(
    monkeypatch: pytest.MonkeyPatch, signer_mode: str
) -> None:
    set_valid_env(monkeypatch)
    monkeypatch.setenv("SIGNER_MODE", signer_mode)
    monkeypatch.setenv("ONE_TX_PRIVATE_KEY", "not-a-private-key")
    if signer_mode == "remote":
        monkeypatch.setenv("REMOTE_SIGNER_PROVIDER", "generic-http")
        monkeypatch.setenv("REMOTE_SIGNER_URL", "https://signer.example")
        monkeypatch.setenv("REMOTE_SIGNER_CREDENTIAL", "remote-credential")
        monkeypatch.setenv("REMOTE_SIGNER_KEY_ID", "key-1")
    if signer_mode == "safe":
        monkeypatch.setenv("SAFE_ADDRESS", "0x0000000000000000000000000000000000000afe")
        monkeypatch.setenv("SAFE_TRANSACTION_SERVICE_URL", "https://safe.example")
        monkeypatch.setenv("SAFE_CHAIN_ID", "8453")
    if signer_mode == "erc4337-paymaster":
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

    config = AllocatorConfig()

    assert config.signer_mode == signer_mode


def test_remote_signer_config_accepts_no_private_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_valid_remote_env(monkeypatch)
    monkeypatch.delenv("ONE_TX_PRIVATE_KEY", raising=False)

    config = AllocatorConfig()

    assert config.signer_mode == "remote"
    assert config.private_key is None
    assert config.remote_signer_provider == "generic-http"
    assert config.remote_signer_url == "https://signer.example"
    assert config.remote_signer_credential is not None
    assert config.remote_signer_credential.get_secret_value() == "remote-credential"
    assert config.remote_signer_key_id == "key-1"


def test_safe_signer_config_accepts_no_private_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_valid_safe_env(monkeypatch)
    monkeypatch.delenv("ONE_TX_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("SAFE_PROPOSER_ADDRESS", REFERRAL_WALLET)
    monkeypatch.setenv("SAFE_PROPOSER_CREDENTIAL", "safe-credential")

    config = AllocatorConfig()

    assert config.signer_mode == "safe"
    assert config.private_key is None
    assert config.safe_address == "0x0000000000000000000000000000000000000afe"
    assert config.safe_transaction_service_url == "https://safe.example"
    assert config.safe_chain_id == 8453
    assert config.safe_proposer_address == REFERRAL_WALLET
    assert config.safe_proposer_credential is not None
    assert config.safe_proposer_credential.get_secret_value() == "safe-credential"


def test_paymaster_config_accepts_no_private_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_valid_paymaster_env(monkeypatch)
    monkeypatch.delenv("ONE_TX_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("PAYMASTER_ACCOUNT_TYPE", "safe")
    monkeypatch.setenv("PAYMASTER_SUPPORTED_CHAIN_IDS", "8453,42161")

    config = AllocatorConfig()

    assert config.signer_mode == "erc4337-paymaster"
    assert config.private_key is None
    assert config.paymaster_provider == "generic-http"
    assert config.paymaster_bundler_url == "https://bundler.example"
    assert config.paymaster_bundler_credential is not None
    assert (
        config.paymaster_bundler_credential.get_secret_value()
        == "bundler-credential"
    )
    assert config.paymaster_url == "https://paymaster.example"
    assert config.paymaster_credential is not None
    assert config.paymaster_credential.get_secret_value() == "paymaster-credential"
    assert config.paymaster_account_type == "safe"
    assert config.paymaster_supported_chain_ids == (8453, 42161)


@pytest.mark.parametrize(
    "missing",
    [
        "REMOTE_SIGNER_PROVIDER",
        "REMOTE_SIGNER_URL",
        "REMOTE_SIGNER_CREDENTIAL",
        "REMOTE_SIGNER_KEY_ID",
    ],
)
def test_remote_signer_config_requires_remote_fields(
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    set_valid_remote_env(monkeypatch)
    monkeypatch.delenv("ONE_TX_PRIVATE_KEY", raising=False)
    monkeypatch.delenv(missing)

    with pytest.raises(ValidationError) as error:
        AllocatorConfig()

    assert missing in str(error.value)


def test_remote_signer_config_validates_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_valid_remote_env(monkeypatch)
    monkeypatch.delenv("ONE_TX_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("REMOTE_SIGNER_URL", "not-a-url")

    with pytest.raises(ValidationError) as error:
        AllocatorConfig()

    assert "REMOTE_SIGNER_URL" in str(error.value)


@pytest.mark.parametrize(
    "missing",
    ["SAFE_ADDRESS", "SAFE_TRANSACTION_SERVICE_URL", "SAFE_CHAIN_ID"],
)
def test_safe_signer_config_requires_safe_fields(
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    set_valid_safe_env(monkeypatch)
    monkeypatch.delenv("ONE_TX_PRIVATE_KEY", raising=False)
    monkeypatch.delenv(missing)

    with pytest.raises(ValidationError) as error:
        AllocatorConfig()

    assert missing in str(error.value)


def test_a_safe_paying_gas_in_usdc_needs_no_transaction_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_valid_safe_env(monkeypatch)
    set_valid_paymaster_env(monkeypatch)
    monkeypatch.delenv("ONE_TX_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("SIGNER_MODE", raising=False)
    monkeypatch.setenv("SIGNER_ACCOUNT", "safe")
    monkeypatch.setenv("SIGNER_SUBMISSION", "erc4337-paymaster")
    monkeypatch.setenv("SIGNER_OWNER", "local")
    monkeypatch.delenv("SAFE_TRANSACTION_SERVICE_URL")

    # The Safe Transaction Service carries 05-01's propose → co-sign → execute
    # flow, which a userOp does not use — it goes to the bundler. Requiring the
    # URL here would demand config for a service this composition never calls.
    config = AllocatorConfig()

    assert config.account == "safe"
    assert config.submission == "erc4337-paymaster"
    assert config.safe_transaction_service_url is None


def test_safe_signer_config_validates_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_valid_safe_env(monkeypatch)
    monkeypatch.delenv("ONE_TX_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("SAFE_TRANSACTION_SERVICE_URL", "not-a-url")

    with pytest.raises(ValidationError) as error:
        AllocatorConfig()

    assert "SAFE_TRANSACTION_SERVICE_URL" in str(error.value)


def test_safe_signer_config_validates_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_valid_safe_env(monkeypatch)
    monkeypatch.delenv("ONE_TX_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("SAFE_ADDRESS", "not-an-address")

    with pytest.raises(ValidationError) as error:
        AllocatorConfig()

    assert "SAFE_ADDRESS" in str(error.value)


@pytest.mark.parametrize(
    "missing",
    [
        "PAYMASTER_PROVIDER",
        "PAYMASTER_BUNDLER_URL",
        "PAYMASTER_URL",
        "PAYMASTER_ACCOUNT_ADDRESS",
        "PAYMASTER_ENTRY_POINT",
    ],
)
def test_paymaster_config_requires_paymaster_fields(
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    set_valid_paymaster_env(monkeypatch)
    monkeypatch.delenv("ONE_TX_PRIVATE_KEY", raising=False)
    monkeypatch.delenv(missing)

    with pytest.raises(ValidationError) as error:
        AllocatorConfig()

    assert missing in str(error.value)


def set_valid_pimlico_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The minimum a Pimlico user should have to supply.

    Deliberately no bundler URL, paymaster URL, account address, entry point or
    gas token: Pimlico derives its endpoint from the key, the Safe from the seed,
    the EntryPoint from the registry, and USDC from the per-chain registry.
    """
    monkeypatch.setenv("ONE_TX_API_URL", "http://localhost:3001/api/v1")
    monkeypatch.setenv("ONE_TX_API_KEY", "test-api-key")
    monkeypatch.setenv("SIGNER_ACCOUNT", "safe")
    monkeypatch.setenv("SIGNER_SUBMISSION", "erc4337-paymaster")
    monkeypatch.setenv("SIGNER_OWNER", "local")
    monkeypatch.setenv("SAFE_CHAIN_ID", "8453")
    monkeypatch.setenv("SAFE_OWNERS", "0x0000000000000000000000000000000000000abc")
    monkeypatch.setenv("SAFE_THRESHOLD", "1")
    monkeypatch.setenv("ONE_TX_PRIVATE_KEY", "0x" + "11" * 32)
    monkeypatch.setenv("PAYMASTER_PROVIDER", "pimlico")
    monkeypatch.setenv("PAYMASTER_ACCOUNT_TYPE", "safe")
    monkeypatch.setenv("PIMLICO_API_KEY", "pim_test_key")


def test_pimlico_needs_no_bundler_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pimlico's endpoint embeds the chain id, so one URL cannot serve a
    multi-chain deployment — the API key is the unit of config. Requiring a
    bundler URL here made PAYMASTER_PROVIDER=pimlico unusable."""
    set_valid_pimlico_env(monkeypatch)

    config = AllocatorConfig()

    assert config.paymaster_provider == "pimlico"
    assert config.paymaster_bundler_url is None


def test_pimlico_requires_its_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    set_valid_pimlico_env(monkeypatch)
    monkeypatch.delenv("PIMLICO_API_KEY")

    with pytest.raises(ValidationError) as error:
        AllocatorConfig()

    assert "PIMLICO_API_KEY" in str(error.value)


def test_gas_token_is_derived_per_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """One configured address made a multi-chain run impossible: every other
    chain got the address of whichever chain the user configured."""
    set_valid_pimlico_env(monkeypatch)

    config = AllocatorConfig()

    assert config.paymaster_usdc_address is None
    assert config.usdc_address(8453) == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    assert config.usdc_address(42161) == "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"


def test_per_chain_gas_token_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    set_valid_pimlico_env(monkeypatch)
    monkeypatch.setenv(
        "PAYMASTER_USDC_ADDRESS_8453",
        "0x0000000000000000000000000000000000000c0c",
    )

    config = AllocatorConfig()

    assert config.usdc_address(8453) == "0x0000000000000000000000000000000000000c0c"
    assert config.usdc_address(42161) == "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"


def test_per_chain_gas_token_override_is_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_valid_pimlico_env(monkeypatch)
    monkeypatch.setenv("PAYMASTER_USDC_ADDRESS_8453", "not-an-address")

    with pytest.raises(ValueError, match="PAYMASTER_USDC_ADDRESS_8453"):
        AllocatorConfig()


def test_generic_http_still_requires_its_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loosening pimlico's requirements must not loosen generic-http's."""
    set_valid_paymaster_env(monkeypatch)
    monkeypatch.delenv("PAYMASTER_BUNDLER_URL")

    with pytest.raises(ValidationError) as error:
        AllocatorConfig()

    assert "PAYMASTER_BUNDLER_URL" in str(error.value)


def test_a_bad_paymaster_account_address_is_still_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional for pimlico, but still validated when supplied."""
    set_valid_pimlico_env(monkeypatch)
    monkeypatch.setenv("PAYMASTER_ACCOUNT_ADDRESS", "not-an-address")

    with pytest.raises(ValidationError) as error:
        AllocatorConfig()

    assert "PAYMASTER_ACCOUNT_ADDRESS" in str(error.value)


@pytest.mark.parametrize("env_name", ["PAYMASTER_BUNDLER_URL", "PAYMASTER_URL"])
def test_paymaster_config_validates_urls(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
) -> None:
    set_valid_paymaster_env(monkeypatch)
    monkeypatch.delenv("ONE_TX_PRIVATE_KEY", raising=False)
    monkeypatch.setenv(env_name, "not-a-url")

    with pytest.raises(ValidationError) as error:
        AllocatorConfig()

    assert env_name in str(error.value)


@pytest.mark.parametrize(
    "env_name",
    [
        "PAYMASTER_ACCOUNT_ADDRESS",
        "PAYMASTER_ENTRY_POINT",
        "PAYMASTER_USDC_ADDRESS",
    ],
)
def test_paymaster_config_validates_addresses(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
) -> None:
    set_valid_paymaster_env(monkeypatch)
    monkeypatch.delenv("ONE_TX_PRIVATE_KEY", raising=False)
    monkeypatch.setenv(env_name, "not-an-address")

    with pytest.raises(ValidationError) as error:
        AllocatorConfig()

    assert env_name in str(error.value)


def test_referral_requires_fee_and_wallet_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_valid_env(monkeypatch)
    monkeypatch.setenv("ONE_TX_REFERRAL_FEE_BPS", "25")

    with pytest.raises(ValidationError) as error:
        AllocatorConfig()

    assert "ONE_TX_REFERRAL_FEE_BPS and ONE_TX_REFERRAL_WALLET" in str(error.value)


def test_referral_wallet_without_positive_fee_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_valid_env(monkeypatch)
    monkeypatch.setenv("ONE_TX_REFERRAL_WALLET", REFERRAL_WALLET)

    with pytest.raises(ValidationError) as error:
        AllocatorConfig()

    assert "ONE_TX_REFERRAL_FEE_BPS and ONE_TX_REFERRAL_WALLET" in str(error.value)


def test_referral_accepts_fee_and_wallet_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_valid_env(monkeypatch)
    monkeypatch.setenv("ONE_TX_REFERRAL_FEE_BPS", "25")
    monkeypatch.setenv("ONE_TX_REFERRAL_WALLET", REFERRAL_WALLET)

    config = AllocatorConfig()

    assert config.referral_fee_bps == 25
    assert config.referral_wallet == REFERRAL_WALLET


def test_referral_fee_bps_cannot_exceed_500(monkeypatch: pytest.MonkeyPatch) -> None:
    set_valid_env(monkeypatch)
    monkeypatch.setenv("ONE_TX_REFERRAL_FEE_BPS", "501")
    monkeypatch.setenv("ONE_TX_REFERRAL_WALLET", REFERRAL_WALLET)

    with pytest.raises(ValidationError) as error:
        AllocatorConfig()

    assert "ONE_TX_REFERRAL_FEE_BPS" in str(error.value)


def test_rpc_url_returns_override_default_or_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_valid_env(monkeypatch)
    default_config = AllocatorConfig()
    assert default_config.rpc_url(8453) == DEFAULT_RPC_URLS[8453]
    assert default_config.rpc_url(999999) is None

    monkeypatch.setenv("RPC_URL_8453", "https://rpc.example/base")
    override_config = AllocatorConfig()
    assert override_config.rpc_url(8453) == "https://rpc.example/base"


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "YeS"])
def test_fast_transfer_boolean_true_values(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    set_valid_env(monkeypatch)
    monkeypatch.setenv("ONE_TX_FAST_TRANSFER", value)

    config = AllocatorConfig()

    assert config.fast_transfer is True


def test_secrets_are_not_exposed_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    set_valid_env(monkeypatch)

    config_repr = repr(AllocatorConfig())

    assert "test-api-key" not in config_repr
    assert VALID_PRIVATE_KEY not in config_repr
    assert "onetx_api_key" not in config_repr
    assert "private_key" not in config_repr


def test_remote_signer_secrets_are_not_exposed_in_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_valid_remote_env(monkeypatch)
    monkeypatch.delenv("ONE_TX_PRIVATE_KEY", raising=False)

    config_repr = repr(AllocatorConfig())

    assert "test-api-key" not in config_repr
    assert "remote-credential" not in config_repr
    assert "onetx_api_key" not in config_repr
    assert "remote_signer_credential" not in config_repr


def test_safe_signer_secrets_are_not_exposed_in_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_valid_safe_env(monkeypatch)
    monkeypatch.delenv("ONE_TX_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("SAFE_PROPOSER_CREDENTIAL", "safe-credential")

    config_repr = repr(AllocatorConfig())

    assert "test-api-key" not in config_repr
    assert "safe-credential" not in config_repr
    assert "onetx_api_key" not in config_repr
    assert "safe_proposer_credential" not in config_repr


def test_paymaster_secrets_are_not_exposed_in_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_valid_paymaster_env(monkeypatch)
    monkeypatch.delenv("ONE_TX_PRIVATE_KEY", raising=False)

    config_repr = repr(AllocatorConfig())

    assert "test-api-key" not in config_repr
    assert "bundler-credential" not in config_repr
    assert "paymaster-credential" not in config_repr
    assert "onetx_api_key" not in config_repr
    assert "paymaster_bundler_credential" not in config_repr
    assert "paymaster_credential" not in config_repr
