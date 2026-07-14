import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import Field, PrivateAttr, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from open_allocator.exec import chains

DEFAULT_RPC_URLS = chains.DEFAULT_RPC_URLS
RPC_ENV_PREFIX = chains.RPC_ENV_PREFIX


class AllocatorConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        case_sensitive=True,
        extra="ignore",
        populate_by_name=True,
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
    signer_mode: Literal[
        "local-eoa",
        "remote",
        "safe",
        "erc4337-paymaster",
    ] = Field(
        "local-eoa",
        validation_alias="SIGNER_MODE",
    )
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
    safe_address: str | None = Field(
        None,
        validation_alias="SAFE_ADDRESS",
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
    paymaster_provider: Literal["generic-http"] | None = Field(
        None,
        validation_alias="PAYMASTER_PROVIDER",
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

        if self.signer_mode == "local-eoa":
            if self.private_key is None:
                raise ValueError(
                    "ONE_TX_PRIVATE_KEY is required when SIGNER_MODE=local-eoa"
                )
            _validate_private_key(self.private_key.get_secret_value())
        elif self.signer_mode == "remote":
            _require_remote_signer_value(
                self.remote_signer_provider,
                "REMOTE_SIGNER_PROVIDER",
            )
            _require_remote_signer_value(self.remote_signer_url, "REMOTE_SIGNER_URL")
            _require_remote_signer_value(
                self.remote_signer_credential,
                "REMOTE_SIGNER_CREDENTIAL",
            )
            _require_remote_signer_value(
                self.remote_signer_key_id,
                "REMOTE_SIGNER_KEY_ID",
            )
            _validate_http_url(self.remote_signer_url, "REMOTE_SIGNER_URL")
            if self.remote_signer_address is not None:
                _validate_address(self.remote_signer_address, "REMOTE_SIGNER_ADDRESS")
        elif self.signer_mode == "safe":
            _require_safe_signer_value(self.safe_address, "SAFE_ADDRESS")
            _require_safe_signer_value(
                self.safe_transaction_service_url,
                "SAFE_TRANSACTION_SERVICE_URL",
            )
            _require_safe_signer_value(self.safe_chain_id, "SAFE_CHAIN_ID")
            _validate_address(self.safe_address, "SAFE_ADDRESS")
            _validate_http_url(
                self.safe_transaction_service_url,
                "SAFE_TRANSACTION_SERVICE_URL",
            )
            if self.safe_proposer_address is not None:
                _validate_address(self.safe_proposer_address, "SAFE_PROPOSER_ADDRESS")
        elif self.signer_mode == "erc4337-paymaster":
            _require_paymaster_value(self.paymaster_provider, "PAYMASTER_PROVIDER")
            _require_paymaster_value(
                self.paymaster_bundler_url,
                "PAYMASTER_BUNDLER_URL",
            )
            _require_paymaster_value(self.paymaster_url, "PAYMASTER_URL")
            _require_paymaster_value(
                self.paymaster_account_address,
                "PAYMASTER_ACCOUNT_ADDRESS",
            )
            _require_paymaster_value(
                self.paymaster_entry_point,
                "PAYMASTER_ENTRY_POINT",
            )
            _require_paymaster_value(
                self.paymaster_usdc_address,
                "PAYMASTER_USDC_ADDRESS",
            )
            _validate_http_url(self.paymaster_bundler_url, "PAYMASTER_BUNDLER_URL")
            _validate_http_url(self.paymaster_url, "PAYMASTER_URL")
            _validate_address(
                self.paymaster_account_address,
                "PAYMASTER_ACCOUNT_ADDRESS",
            )
            _validate_address(self.paymaster_entry_point, "PAYMASTER_ENTRY_POINT")
            _validate_address(self.paymaster_usdc_address, "PAYMASTER_USDC_ADDRESS")

        return self

    def rpc_url(self, chain_id: int) -> str | None:
        return chains.rpc_url(chain_id, self)


class ReadOnlyOneTxConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        case_sensitive=True,
        extra="ignore",
        populate_by_name=True,
    )

    onetx_api_url: str = Field(..., validation_alias="ONE_TX_API_URL")
    onetx_api_key: SecretStr = Field(..., validation_alias="ONE_TX_API_KEY", repr=False)


def _validate_private_key(value: str) -> None:
    if re.fullmatch(r"0x[0-9a-fA-F]{64}", value) is None:
        raise ValueError("ONE_TX_PRIVATE_KEY must be a 32-byte 0x-prefixed hex string")


def _require_remote_signer_value(value: object, env_name: str) -> None:
    if value is None:
        raise ValueError(f"{env_name} is required when SIGNER_MODE=remote")


def _require_safe_signer_value(value: object, env_name: str) -> None:
    if value is None:
        raise ValueError(f"{env_name} is required when SIGNER_MODE=safe")


def _require_paymaster_value(value: object, env_name: str) -> None:
    if value is None:
        raise ValueError(f"{env_name} is required when SIGNER_MODE=erc4337-paymaster")


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
