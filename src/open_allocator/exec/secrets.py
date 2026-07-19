from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from pydantic import SecretStr
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

SECRET_SERVICE = "open-allocator"
BACKEND_ENV_VAR = "OPEN_ALLOCATOR_SECRET_BACKEND"
GCP_PROJECT_ENV_VAR = "OPEN_ALLOCATOR_GCP_SECRET_PROJECT"

DEFAULT_BACKEND = "keyring"
BACKEND_NAMES = ("keyring", "gcp", "none")


@runtime_checkable
class SecretBackend(Protocol):
    def get(self, name: str) -> str | None: ...


class KeyringBackend:
    """OS keychain (libsecret / macOS Keychain)."""

    def __init__(self, service: str = SECRET_SERVICE) -> None:
        self._service = service
        self._unavailable = False

    def __repr__(self) -> str:
        return f"KeyringBackend(service={self._service!r})"

    def get(self, name: str) -> str | None:
        # A container or CI box has no keychain, and every lookup there pays the
        # cost of failing. Fail once, then stay out of the way.
        if self._unavailable:
            return None

        try:
            import keyring
        except ImportError:
            self._unavailable = True
            return None

        # A headless box with no libsecret raises rather than returning None.
        # An unavailable keychain is not an error: it falls back to .env.
        try:
            value = keyring.get_password(self._service, name)
        except Exception:
            self._unavailable = True
            return None

        return value or None


class GcpSecretManagerBackend:
    """GCP Secret Manager, for anything that also runs server-side."""

    def __init__(self, project: str) -> None:
        self._project = project

    def __repr__(self) -> str:
        return f"GcpSecretManagerBackend(project={self._project!r})"

    def get(self, name: str) -> str | None:
        try:
            from google.cloud import secretmanager
        except ImportError as error:
            raise SecretBackendUnavailable(
                f"{BACKEND_ENV_VAR}=gcp requires google-cloud-secret-manager; "
                "install it or choose another backend"
            ) from error

        client = secretmanager.SecretManagerServiceClient()
        path = f"projects/{self._project}/secrets/{name}/versions/latest"
        try:
            response = client.access_secret_version(request={"name": path})
        except Exception:
            return None

        return response.payload.data.decode("utf-8") or None


class SecretBackendUnavailable(RuntimeError):
    pass


def backend_from_env(env: Mapping[str, str] | None = None) -> SecretBackend | None:
    environ = os.environ if env is None else env
    name = environ.get(BACKEND_ENV_VAR, DEFAULT_BACKEND).strip().lower()

    if name in ("", "none"):
        return None
    if name == "keyring":
        return KeyringBackend()
    if name == "gcp":
        project = environ.get(GCP_PROJECT_ENV_VAR, "").strip()
        if not project:
            raise SecretBackendUnavailable(
                f"{BACKEND_ENV_VAR}=gcp requires {GCP_PROJECT_ENV_VAR}"
            )
        return GcpSecretManagerBackend(project)

    raise SecretBackendUnavailable(
        f"{BACKEND_ENV_VAR} must be one of {', '.join(BACKEND_NAMES)}; got {name!r}"
    )


def secret_field_names(settings_cls: type[BaseSettings]) -> tuple[str, ...]:
    """Env names of the SecretStr fields — the only ones a backend is asked for."""
    names: list[str] = []
    for field_name, field in settings_cls.model_fields.items():
        if not _is_secret_field(field):
            continue
        names.append(_env_name(field_name, field))
    return tuple(names)


class SecretBackendSettingsSource(PydanticBaseSettingsSource):
    """Feeds SecretStr fields from a secret backend.

    Ordered after the env source and before the dotenv source, so an explicit
    env var still wins and a plain .env remains the floor.
    """

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        backend: SecretBackend | None,
    ) -> None:
        super().__init__(settings_cls)
        self._backend = backend

    def get_field_value(
        self,
        field: FieldInfo,
        field_name: str,
    ) -> tuple[Any, str, bool]:
        if self._backend is None or not _is_secret_field(field):
            return None, field_name, False

        env_name = _env_name(field_name, field)

        # An explicit env var outranks the backend, so a lookup here could only
        # be discarded. Skipping it keeps the container path (secrets injected
        # as env vars, no keychain present) from paying for a backend at all.
        if env_name in os.environ:
            return None, env_name, False

        return self._backend.get(env_name), env_name, False

    def __call__(self) -> dict[str, Any]:
        if self._backend is None:
            return {}

        values: dict[str, Any] = {}
        for field_name, field in self.settings_cls.model_fields.items():
            value, key, is_complex = self.get_field_value(field, field_name)
            if value is None:
                continue
            # Key by the env alias, matching the env and dotenv sources — a
            # field-name key would not collide with theirs, so precedence
            # between the sources would silently not apply.
            values[key] = self.prepare_field_value(
                field_name,
                field,
                value,
                is_complex,
            )
        return values


def _is_secret_field(field: FieldInfo) -> bool:
    annotation = field.annotation
    if annotation is SecretStr:
        return True
    return SecretStr in getattr(annotation, "__args__", ())


def _env_name(field_name: str, field: FieldInfo) -> str:
    alias = field.validation_alias
    if isinstance(alias, str):
        return alias
    return field_name.upper()
