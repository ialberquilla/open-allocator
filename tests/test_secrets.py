from __future__ import annotations

import os
from pathlib import Path

import pytest

from open_allocator.exec.config import AllocatorConfig, ReadOnlyOneTxConfig
from open_allocator.exec.secrets import (
    KeyringBackend,
    SecretBackendUnavailable,
    backend_from_env,
    secret_field_names,
)

VALID_PRIVATE_KEY = "0x" + "11" * 32


@pytest.fixture(autouse=True)
def clear_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("ONE_TX_") or name == "SIGNER_MODE":
            monkeypatch.delenv(name, raising=False)


class StubBackend:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.requested: list[str] = []

    def get(self, name: str) -> str | None:
        self.requested.append(name)
        return self.values.get(name)


def write_env_file(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "dotenv"
    path.write_text(body, encoding="utf-8")
    return path


def use_env_file(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("OPEN_ALLOCATOR_ENV_FILE", str(path))


def use_backend(monkeypatch: pytest.MonkeyPatch, backend: object) -> None:
    monkeypatch.setattr(
        "open_allocator.exec.config.backend_from_env",
        lambda: backend,
    )


# --- plain .env, no dotenvx, no Node -------------------------------------


def test_config_loads_from_plain_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = write_env_file(
        tmp_path,
        "ONE_TX_API_URL=http://localhost:3001/api/v1\n"
        "ONE_TX_API_KEY=file-api-key\n"
        f"ONE_TX_PRIVATE_KEY={VALID_PRIVATE_KEY}\n"
        "ONE_TX_SLIPPAGE_BPS=75\n",
    )
    use_env_file(monkeypatch, env_file)

    config = AllocatorConfig()

    assert config.onetx_api_key.get_secret_value() == "file-api-key"
    assert config.slippage_bps == 75


def test_read_only_config_loads_from_plain_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = write_env_file(
        tmp_path,
        "ONE_TX_API_URL=http://localhost:3001/api/v1\nONE_TX_API_KEY=file-api-key\n",
    )
    use_env_file(monkeypatch, env_file)

    assert ReadOnlyOneTxConfig().onetx_api_key.get_secret_value() == "file-api-key"


def test_env_file_may_live_outside_the_working_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    outside = write_env_file(
        tmp_path,
        "ONE_TX_API_URL=http://localhost:3001/api/v1\nONE_TX_API_KEY=outside-key\n",
    )
    use_env_file(monkeypatch, outside)
    monkeypatch.chdir(tmp_path / "..")

    assert ReadOnlyOneTxConfig().onetx_api_key.get_secret_value() == "outside-key"


# --- secret backends ------------------------------------------------------


def test_backend_supplies_secrets_to_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = write_env_file(
        tmp_path,
        "ONE_TX_API_URL=http://localhost:3001/api/v1\n",
    )
    use_env_file(monkeypatch, env_file)
    use_backend(
        monkeypatch,
        StubBackend(
            {
                "ONE_TX_API_KEY": "keychain-api-key",
                "ONE_TX_PRIVATE_KEY": VALID_PRIVATE_KEY,
            }
        ),
    )

    config = AllocatorConfig()

    assert config.onetx_api_key.get_secret_value() == "keychain-api-key"
    assert config.private_key.get_secret_value() == VALID_PRIVATE_KEY


def test_backend_is_only_asked_for_secret_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = write_env_file(
        tmp_path,
        "ONE_TX_API_URL=http://localhost:3001/api/v1\n"
        f"ONE_TX_PRIVATE_KEY={VALID_PRIVATE_KEY}\n",
    )
    use_env_file(monkeypatch, env_file)
    backend = StubBackend({"ONE_TX_API_KEY": "keychain-api-key"})
    use_backend(monkeypatch, backend)

    AllocatorConfig()

    assert "ONE_TX_API_KEY" in backend.requested
    # Non-secret config is not a backend concern.
    assert "ONE_TX_API_URL" not in backend.requested
    assert "ONE_TX_SLIPPAGE_BPS" not in backend.requested


def test_explicit_env_var_beats_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    use_env_file(monkeypatch, write_env_file(tmp_path, ""))
    monkeypatch.setenv("ONE_TX_API_URL", "http://localhost:3001/api/v1")
    monkeypatch.setenv("ONE_TX_API_KEY", "env-wins")
    monkeypatch.setenv("ONE_TX_PRIVATE_KEY", VALID_PRIVATE_KEY)
    use_backend(monkeypatch, StubBackend({"ONE_TX_API_KEY": "keychain-loses"}))

    assert AllocatorConfig().onetx_api_key.get_secret_value() == "env-wins"


def test_backend_beats_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = write_env_file(
        tmp_path,
        "ONE_TX_API_URL=http://localhost:3001/api/v1\n"
        "ONE_TX_API_KEY=file-loses\n"
        f"ONE_TX_PRIVATE_KEY={VALID_PRIVATE_KEY}\n",
    )
    use_env_file(monkeypatch, env_file)
    use_backend(monkeypatch, StubBackend({"ONE_TX_API_KEY": "keychain-wins"}))

    assert AllocatorConfig().onetx_api_key.get_secret_value() == "keychain-wins"


def test_absent_backend_falls_back_to_env_file_without_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = write_env_file(
        tmp_path,
        "ONE_TX_API_URL=http://localhost:3001/api/v1\n"
        "ONE_TX_API_KEY=file-api-key\n"
        f"ONE_TX_PRIVATE_KEY={VALID_PRIVATE_KEY}\n",
    )
    use_env_file(monkeypatch, env_file)
    use_backend(monkeypatch, None)

    assert AllocatorConfig().onetx_api_key.get_secret_value() == "file-api-key"


def test_unavailable_keyring_falls_back_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Boom:
        @staticmethod
        def get_password(service: str, name: str) -> str:
            raise RuntimeError("no libsecret on this box")

    monkeypatch.setitem(__import__("sys").modules, "keyring", Boom)

    # A headless machine with no keychain is a fallback, not a failure.
    assert KeyringBackend().get("ONE_TX_API_KEY") is None


# --- containers: env vars only, no .env, no keychain -----------------------


def test_config_loads_from_env_alone_with_no_env_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A container/server passes secrets as env vars and ships no .env. The
    # missing file must be a non-event, not an error.
    monkeypatch.setenv("OPEN_ALLOCATOR_ENV_FILE", "/nonexistent/does/not/exist/.env")
    monkeypatch.setenv("ONE_TX_API_URL", "http://api:3001/v1")
    monkeypatch.setenv("ONE_TX_API_KEY", "from-container-env")
    monkeypatch.setenv("ONE_TX_PRIVATE_KEY", VALID_PRIVATE_KEY)
    use_backend(monkeypatch, None)

    assert AllocatorConfig().onetx_api_key.get_secret_value() == "from-container-env"


def test_backend_is_not_consulted_for_values_already_in_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    use_env_file(monkeypatch, write_env_file(tmp_path, ""))
    monkeypatch.setenv("ONE_TX_API_URL", "http://api:3001/v1")
    monkeypatch.setenv("ONE_TX_API_KEY", "from-container-env")
    monkeypatch.setenv("ONE_TX_PRIVATE_KEY", VALID_PRIVATE_KEY)
    backend = StubBackend({"ONE_TX_API_KEY": "never-read"})
    use_backend(monkeypatch, backend)

    AllocatorConfig()

    # env outranks the backend, so asking it could only waste a lookup — which
    # in a container means a failing keychain call per secret per construction.
    assert "ONE_TX_API_KEY" not in backend.requested
    assert "ONE_TX_PRIVATE_KEY" not in backend.requested


def test_unavailable_keyring_is_only_probed_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []

    class Boom:
        @staticmethod
        def get_password(service: str, name: str) -> str:
            attempts.append(name)
            raise RuntimeError("no keychain in this container")

    monkeypatch.setitem(__import__("sys").modules, "keyring", Boom)
    backend = KeyringBackend()

    for name in ("ONE_TX_API_KEY", "PAYMASTER_CREDENTIAL", "SAFE_PROPOSER_CREDENTIAL"):
        assert backend.get(name) is None

    # Absence is a property of the box, not of the key being looked up.
    assert attempts == ["ONE_TX_API_KEY"]


# --- backend selection ----------------------------------------------------


def test_keyring_is_the_default_backend() -> None:
    assert isinstance(backend_from_env({}), KeyringBackend)


def test_backend_can_be_disabled() -> None:
    assert backend_from_env({"OPEN_ALLOCATOR_SECRET_BACKEND": "none"}) is None


def test_gcp_backend_requires_a_project() -> None:
    with pytest.raises(SecretBackendUnavailable) as error:
        backend_from_env({"OPEN_ALLOCATOR_SECRET_BACKEND": "gcp"})

    assert "OPEN_ALLOCATOR_GCP_SECRET_PROJECT" in str(error.value)


def test_unknown_backend_is_rejected() -> None:
    with pytest.raises(SecretBackendUnavailable) as error:
        backend_from_env({"OPEN_ALLOCATOR_SECRET_BACKEND": "sops"})

    assert "sops" in str(error.value)


def test_secret_field_names_covers_every_credential() -> None:
    names = secret_field_names(AllocatorConfig)

    # Declaring a field SecretStr is all it takes to make it keyring-able and
    # kept out of reprs. This asserts the exact set so a new credential is a
    # deliberate choice rather than a silent omission.
    assert set(names) == {
        "ONE_TX_API_KEY",
        "ONE_TX_PRIVATE_KEY",
        "REMOTE_SIGNER_CREDENTIAL",
        "SAFE_PROPOSER_CREDENTIAL",
        "PAYMASTER_BUNDLER_CREDENTIAL",
        "PAYMASTER_CREDENTIAL",
        "PIMLICO_API_KEY",
    }
