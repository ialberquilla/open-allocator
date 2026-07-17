import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import Field, PrivateAttr, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from open_allocator.exec import chains
from open_allocator.exec.composition import (
    LEGACY_SIGNER_MODES,
    Account,
    OwnerSigner,
    SignerComposition,
    Submission,
    UnknownSignerMode,
    axis_env_name,
    legacy_signer_mode,
)
from open_allocator.exec.secrets import SecretBackendSettingsSource, backend_from_env

DEFAULT_RPC_URLS = chains.DEFAULT_RPC_URLS
RPC_ENV_PREFIX = chains.RPC_ENV_PREFIX

DEFAULT_ENV_FILE = ".env"
ENV_FILE_ENV_VAR = "OPEN_ALLOCATOR_ENV_FILE"


def env_file_path() -> str:
    """The .env to read. Point it outside the working tree to keep the
    plaintext file out of the repo an agent has access to."""
    return os.environ.get(ENV_FILE_ENV_VAR, "").strip() or DEFAULT_ENV_FILE


def _with_secret_backend(
    settings_cls: type[BaseSettings],
    init_settings: PydanticBaseSettingsSource,
    env_settings: PydanticBaseSettingsSource,
    dotenv_settings: PydanticBaseSettingsSource,
    file_secret_settings: PydanticBaseSettingsSource,
) -> tuple[PydanticBaseSettingsSource, ...]:
    return (
        init_settings,
        env_settings,
        SecretBackendSettingsSource(settings_cls, backend_from_env()),
        DotEnvSettingsSource(
            settings_cls,
            env_file=env_file_path(),
            env_file_encoding="utf-8",
            case_sensitive=True,
        ),
        file_secret_settings,
    )


class AllocatorConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        case_sensitive=True,
        extra="ignore",
        populate_by_name=True,
        env_file=".env",
        env_file_encoding="utf-8",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return _with_secret_backend(
            settings_cls,
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )

    onetx_api_url: str = Field(..., validation_alias="ONE_TX_API_URL")
    onetx_api_key: SecretStr = Field(..., validation_alias="ONE_TX_API_KEY", repr=False)
    slippage_bps: int = Field(50, validation_alias="ONE_TX_SLIPPAGE_BPS")
    fast_transfer: bool = Field(False, validation_alias="ONE_TX_FAST_TRANSFER")
    referral_fee_bps: int = Field(
        0,
        ge=0,
        le=500,
        validation_alias="ONE_TX_REFERRAL_FEE_BPS",
    )
    referral_wallet: str | None = Field(None, validation_alias="ONE_TX_REFERRAL_WALLET")
    # How to sign and submit is three independent axes. SIGNER_MODE is the
    # deprecated one-enum form; it is expanded onto the axes before validation
    # and re-derived after, so existing .env files and readers keep working.
    account: Account = Field("eoa", validation_alias="SIGNER_ACCOUNT")
    submission: Submission = Field("rpc", validation_alias="SIGNER_SUBMISSION")
    owner_signer: OwnerSigner = Field("local", validation_alias="SIGNER_OWNER")
    signer_mode: str | None = Field(None, validation_alias="SIGNER_MODE")
    private_key: SecretStr | None = Field(
        None,
        validation_alias="ONE_TX_PRIVATE_KEY",
        repr=False,
    )
    remote_signer_provider: Literal["generic-http"] | None = Field(
        None,
        validation_alias="REMOTE_SIGNER_PROVIDER",
    )
    remote_signer_url: str | None = Field(
        None,
        validation_alias="REMOTE_SIGNER_URL",
    )
    remote_signer_credential: SecretStr | None = Field(
        None,
        validation_alias="REMOTE_SIGNER_CREDENTIAL",
        repr=False,
    )
    remote_signer_key_id: str | None = Field(
        None,
        validation_alias="REMOTE_SIGNER_KEY_ID",
    )
    remote_signer_address: str | None = Field(
        None,
        validation_alias="REMOTE_SIGNER_ADDRESS",
    )
    # SAFE_ADDRESS is optional: with owners + threshold + salt the address is
    # derived counterfactually and is the same on every supported chain, so
    # nobody has to create a Safe in the UI first. An explicit address still
    # wins, for Safes that already exist.
    safe_address: str | None = Field(
        None,
        validation_alias="SAFE_ADDRESS",
    )
    safe_owners: tuple[str, ...] | None = Field(
        None,
        validation_alias="SAFE_OWNERS",
    )
    safe_threshold: int | None = Field(
        None,
        ge=1,
        validation_alias="SAFE_THRESHOLD",
    )
    safe_salt_nonce: int = Field(
        0,
        ge=0,
        validation_alias="SAFE_SALT_NONCE",
    )
    safe_transaction_service_url: str | None = Field(
        None,
        validation_alias="SAFE_TRANSACTION_SERVICE_URL",
    )
    safe_chain_id: int | None = Field(
        None,
        ge=1,
        validation_alias="SAFE_CHAIN_ID",
    )
    safe_proposer_address: str | None = Field(
        None,
        validation_alias="SAFE_PROPOSER_ADDRESS",
    )
    safe_proposer_credential: SecretStr | None = Field(
        None,
        validation_alias="SAFE_PROPOSER_CREDENTIAL",
        repr=False,
    )
    paymaster_provider: Literal["pimlico", "circle", "generic-http"] | None = Field(
        None,
        validation_alias="PAYMASTER_PROVIDER",
    )
    # Pimlico's endpoint embeds the chain id, so one bundler URL cannot serve a
    # multi-chain deployment. The API key is the portable unit: the per-chain
    # URL is derived from it (see paymaster_registry.pimlico_rpc_url). Being a
    # SecretStr, it is keyring-able for free and never lands in a repr.
    pimlico_api_key: SecretStr | None = Field(
        None,
        validation_alias="PIMLICO_API_KEY",
        repr=False,
    )
    paymaster_bundler_url: str | None = Field(
        None,
        validation_alias="PAYMASTER_BUNDLER_URL",
    )
    paymaster_bundler_credential: SecretStr | None = Field(
        None,
        validation_alias="PAYMASTER_BUNDLER_CREDENTIAL",
        repr=False,
    )
    paymaster_url: str | None = Field(
        None,
        validation_alias="PAYMASTER_URL",
    )
    paymaster_credential: SecretStr | None = Field(
        None,
        validation_alias="PAYMASTER_CREDENTIAL",
        repr=False,
    )
    paymaster_account_address: str | None = Field(
        None,
        validation_alias="PAYMASTER_ACCOUNT_ADDRESS",
    )
    paymaster_account_type: Literal["smart-account", "safe"] = Field(
        "smart-account",
        validation_alias="PAYMASTER_ACCOUNT_TYPE",
    )
    paymaster_entry_point: str | None = Field(
        None,
        validation_alias="PAYMASTER_ENTRY_POINT",
    )
    paymaster_usdc_address: str | None = Field(
        None,
        validation_alias="PAYMASTER_USDC_ADDRESS",
    )
    paymaster_supported_chain_ids: tuple[int, ...] | None = Field(
        None,
        validation_alias="PAYMASTER_SUPPORTED_CHAIN_IDS",
    )
    idempotency_store_path: Path | None = Field(
        Path(".open_allocator/execution-idempotency.json"),
        validation_alias="OPEN_ALLOCATOR_IDEMPOTENCY_STORE",
    )
    checkpoint_dir: Path | None = Field(
        Path(".open_allocator/checkpoints"),
        validation_alias="OPEN_ALLOCATOR_CHECKPOINT_DIR",
    )
    allocation_log_path: Path | None = Field(
        Path(".open_allocator/allocation-log.jsonl"),
        validation_alias="OPEN_ALLOCATOR_ALLOCATION_LOG",
    )

    _rpc_overrides: dict[int, str] = PrivateAttr(default_factory=dict)

    def model_post_init(self, __context: object) -> None:
        self._rpc_overrides = chains.rpc_overrides_from_env(os.environ)

    @model_validator(mode="before")
    @classmethod
    def _expand_legacy_signer_mode(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data

        mode = _first_present(data, "SIGNER_MODE", "signer_mode")
        if mode is None or (isinstance(mode, str) and mode.strip() == ""):
            return data

        try:
            composition = LEGACY_SIGNER_MODES[mode]
        except (KeyError, TypeError) as error:
            raise UnknownSignerMode(mode) from error

        for axis, implied in composition._asdict().items():
            env_name = axis_env_name(axis)
            explicit = _first_present(data, env_name, axis)
            if explicit is None:
                data[env_name] = implied
            elif explicit != implied:
                raise ValueError(
                    f"SIGNER_MODE={mode} implies {env_name}={implied}, "
                    f"but {env_name}={explicit} was set; "
                    f"set the axes directly and drop SIGNER_MODE"
                )

        return data

    @field_validator("referral_wallet", mode="before")
    @classmethod
    def _blank_referral_wallet_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @field_validator(
        "remote_signer_provider",
        "remote_signer_url",
        "remote_signer_credential",
        "remote_signer_key_id",
        "remote_signer_address",
        "safe_address",
        "safe_transaction_service_url",
        "safe_proposer_address",
        "safe_proposer_credential",
        "paymaster_provider",
        "paymaster_bundler_url",
        "paymaster_bundler_credential",
        "paymaster_url",
        "paymaster_credential",
        "paymaster_account_address",
        "paymaster_entry_point",
        "paymaster_usdc_address",
        mode="before",
    )
    @classmethod
    def _blank_signer_value_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @field_validator("safe_owners", mode="before")
    @classmethod
    def _parse_safe_owners(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped == "":
                return None
            # Order is preserved deliberately: it feeds the Safe setup calldata
            # and therefore the derived address. Sorting would move the Safe.
            return tuple(item.strip() for item in stripped.split(",") if item.strip())
        return value

    @field_validator("paymaster_supported_chain_ids", mode="before")
    @classmethod
    def _parse_paymaster_supported_chain_ids(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped == "":
                return None
            return tuple(
                int(item.strip()) for item in stripped.split(",") if item.strip()
            )
        return value

    @model_validator(mode="after")
    def _validate_cross_field_rules(self) -> "AllocatorConfig":
        has_referral_fee = self.referral_fee_bps > 0
        has_referral_wallet = self.referral_wallet is not None
        if has_referral_fee != has_referral_wallet:
            raise ValueError(
                "ONE_TX_REFERRAL_FEE_BPS and ONE_TX_REFERRAL_WALLET "
                "must be set together"
            )

        # Each axis carries its own requirements, and they compose. A raw local
        # key is only needed when no other axis supplies the signature: a Safe
        # signs via its proposer credential, a remote owner via the enclave, and
        # a 4337 submission via the paymaster adapter.
        needs_raw_key = (
            self.account == "eoa"
            and self.submission == "rpc"
            and self.owner_signer == "local"
        )
        if needs_raw_key:
            if self.private_key is None:
                raise ValueError(
                    "ONE_TX_PRIVATE_KEY is required for a local-key EOA "
                    "(SIGNER_ACCOUNT=eoa, SIGNER_SUBMISSION=rpc, SIGNER_OWNER=local)"
                )
            _validate_private_key(self.private_key.get_secret_value())

        if self.owner_signer == "remote":
            _require_axis_value(
                self.remote_signer_provider,
                "REMOTE_SIGNER_PROVIDER",
                "SIGNER_OWNER=remote",
            )
            _require_axis_value(
                self.remote_signer_url,
                "REMOTE_SIGNER_URL",
                "SIGNER_OWNER=remote",
            )
            _require_axis_value(
                self.remote_signer_credential,
                "REMOTE_SIGNER_CREDENTIAL",
                "SIGNER_OWNER=remote",
            )
            _require_axis_value(
                self.remote_signer_key_id,
                "REMOTE_SIGNER_KEY_ID",
                "SIGNER_OWNER=remote",
            )
            _validate_http_url(self.remote_signer_url, "REMOTE_SIGNER_URL")
            if self.remote_signer_address is not None:
                _validate_address(self.remote_signer_address, "REMOTE_SIGNER_ADDRESS")

        if self.account == "safe":
            # Either name an existing Safe or give the seed to derive one.
            if self.safe_address is None and self.safe_owners is None:
                raise ValueError(
                    "SIGNER_ACCOUNT=safe needs either SAFE_ADDRESS (an existing "
                    "Safe) or SAFE_OWNERS + SAFE_THRESHOLD (to derive one "
                    "counterfactually, same address on every supported chain)"
                )
            if self.safe_owners is not None:
                if self.safe_threshold is None:
                    raise ValueError("SAFE_THRESHOLD is required with SAFE_OWNERS")
                if self.safe_threshold > len(self.safe_owners):
                    raise ValueError(
                        f"SAFE_THRESHOLD ({self.safe_threshold}) exceeds the "
                        f"number of SAFE_OWNERS ({len(self.safe_owners)})"
                    )
                for owner in self.safe_owners:
                    _validate_address(owner, "SAFE_OWNERS")
            if self.submission == "rpc":
                _require_axis_value(
                    self.safe_transaction_service_url,
                    "SAFE_TRANSACTION_SERVICE_URL",
                    "SIGNER_ACCOUNT=safe with SIGNER_SUBMISSION=rpc",
                )
                _validate_http_url(
                    self.safe_transaction_service_url,
                    "SAFE_TRANSACTION_SERVICE_URL",
                )
            _require_axis_value(
                self.safe_chain_id,
                "SAFE_CHAIN_ID",
                "SIGNER_ACCOUNT=safe",
            )
            if self.safe_address is not None:
                _validate_address(self.safe_address, "SAFE_ADDRESS")
            if self.safe_proposer_address is not None:
                _validate_address(self.safe_proposer_address, "SAFE_PROPOSER_ADDRESS")

        if self.submission == "erc4337-paymaster":
            required = "SIGNER_SUBMISSION=erc4337-paymaster"
            _require_axis_value(self.paymaster_provider, "PAYMASTER_PROVIDER", required)

            # The gas token is the one thing every provider needs from the user:
            # it names what to pay in, and nothing can derive that for them.
            _require_axis_value(
                self.paymaster_usdc_address,
                "PAYMASTER_USDC_ADDRESS",
                required,
            )
            _validate_address(self.paymaster_usdc_address, "PAYMASTER_USDC_ADDRESS")

            if self.paymaster_provider == "pimlico":
                _require_axis_value(
                    self.pimlico_api_key,
                    "PIMLICO_API_KEY",
                    "PAYMASTER_PROVIDER=pimlico",
                )
            elif self.paymaster_provider == "generic-http":
                generic = "PAYMASTER_PROVIDER=generic-http"
                _require_axis_value(
                    self.paymaster_bundler_url,
                    "PAYMASTER_BUNDLER_URL",
                    generic,
                )
                _require_axis_value(self.paymaster_url, "PAYMASTER_URL", generic)
                _require_axis_value(
                    self.paymaster_account_address,
                    "PAYMASTER_ACCOUNT_ADDRESS",
                    generic,
                )
                _require_axis_value(
                    self.paymaster_entry_point,
                    "PAYMASTER_ENTRY_POINT",
                    generic,
                )
                _validate_http_url(self.paymaster_bundler_url, "PAYMASTER_BUNDLER_URL")
                _validate_http_url(self.paymaster_url, "PAYMASTER_URL")

            if self.paymaster_account_address is not None:
                _validate_address(
                    self.paymaster_account_address,
                    "PAYMASTER_ACCOUNT_ADDRESS",
                )
            if self.paymaster_entry_point is not None:
                _validate_address(self.paymaster_entry_point, "PAYMASTER_ENTRY_POINT")

        self.signer_mode = legacy_signer_mode(self.composition)

        return self

    @property
    def composition(self) -> SignerComposition:
        return SignerComposition(self.account, self.submission, self.owner_signer)

    def rpc_url(self, chain_id: int) -> str | None:
        return chains.rpc_url(chain_id, self)


class ReadOnlyOneTxConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        case_sensitive=True,
        extra="ignore",
        populate_by_name=True,
        env_file=".env",
        env_file_encoding="utf-8",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return _with_secret_backend(
            settings_cls,
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )

    onetx_api_url: str = Field(..., validation_alias="ONE_TX_API_URL")
    onetx_api_key: SecretStr = Field(..., validation_alias="ONE_TX_API_KEY", repr=False)


def _validate_private_key(value: str) -> None:
    if re.fullmatch(r"0x[0-9a-fA-F]{64}", value) is None:
        raise ValueError("ONE_TX_PRIVATE_KEY must be a 32-byte 0x-prefixed hex string")


def _require_axis_value(value: object, env_name: str, required_by: str) -> None:
    if value is None:
        raise ValueError(f"{env_name} is required when {required_by}")


def _first_present(data: dict[object, object], *keys: str) -> object:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return None


def _validate_http_url(value: str | None, env_name: str) -> None:
    if value is None:
        return
    if re.fullmatch(r"https?://\S+", value) is None:
        raise ValueError(f"{env_name} must be an http(s) URL")


def _validate_address(value: str, env_name: str) -> None:
    if re.fullmatch(r"0x[0-9a-fA-F]{40}", value) is None:
        raise ValueError(f"{env_name} must be a 20-byte 0x-prefixed hex address")


def _rpc_overrides_from_env(env: Mapping[str, str]) -> dict[int, str]:
    return chains.rpc_overrides_from_env(env)
