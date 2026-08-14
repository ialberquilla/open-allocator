"""Where the library's state lives, as a choice the caller makes.

Everything the allocator remembers between one command and the next -- which
checkpoint a run reached, what it has already executed, what it paid per share
-- is on disk today, under `.open_allocator/`. That is the right default for a
CLI and the wrong one for a scheduled container: Cloud Run Jobs hand each retry
a **fresh filesystem**, so the record of what already landed disappears exactly
when it is needed. A retried task reads an empty idempotency store, concludes
nothing has been sent, and sends it again. The state is not incidental to
correctness here; it *is* the correctness argument, and it cannot live somewhere
the platform erases between attempts.

So persistence becomes a port. `StateBackend` names the six operations that
touch durable state, `LocalFsBackend` implements them against the same files as
before, and a caller that needs state to outlive the filesystem supplies its own
implementation.

Per the showcase plan's §0.2, only the Protocol and the filesystem
implementation ship here. The Postgres implementation lives in the consumer and
is **injected** -- this library's whole claim is that it has no service
dependencies, and importing `psycopg` to serve one caller would trade that away
for a convenience the port already provides.

**What the port deliberately does not own.** Checkpoint ids are content-derived,
artifacts are schema-validated before they are stored, and a log entry completes
its own third amount. None of that is a backend's business: a backend receives a
model that is already valid and puts it somewhere. The alternative -- each
backend deriving ids and validating artifacts for itself -- produces two
implementations that agree until the day they do not, and the disagreement
surfaces on a retry, which is the one moment nobody is watching. Domain logic
stays in `core.checkpoint`; this module moves bytes.

**Checkpoints are addressed by id, not by path.** `core.checkpoint` still
accepts a path for a checkpoint that names a file directly, because that is
useful from a shell -- but resolving a path is a filesystem idea, so it stays
inside the filesystem backend rather than being pushed onto every implementation
as a concept it would have to fake.

**Idempotency is scoped, and the scope is content-derived.** A completed key
means "already done *for this exact allocation*", never "already done": the
scope is a hash of the allocation, positions or withdrawal the run is executing,
so a genuinely new run with a coincidentally equal step key is not skipped. The
backend stores keys under a scope it is given and does not compute one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TypeAlias, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - import cycle: see the module docstring
    from open_allocator.core.checkpoint import AllocationLogEntry, Checkpoint

JsonValue: TypeAlias = Any

DEFAULT_CHECKPOINT_DIR = Path(".open_allocator/checkpoints")
DEFAULT_ALLOCATION_LOG_PATH = Path(".open_allocator/allocation-log.jsonl")
DEFAULT_IDEMPOTENCY_STORE_PATH = Path(".open_allocator/execution-idempotency.json")

_IDEMPOTENCY_STORE_VERSION = 1


class StateError(RuntimeError):
    """A backend could not do what it was asked.

    The two subclasses below also inherit the builtin they replace, so callers
    and tests that catch `FileNotFoundError` keep working against the filesystem
    backend. That is not only compatibility: on the filesystem these *are*
    filesystem errors, and pretending otherwise would be a lie about what
    happened. What the library-owned name adds is something a Postgres backend
    can raise honestly -- a missing row is not a missing file, and it should not
    have to claim to be one to be caught by the same `except`.
    """


class CheckpointNotFound(StateError, FileNotFoundError):
    """No checkpoint is stored under this id."""


class CheckpointExists(StateError, FileExistsError):
    """A checkpoint is already stored under this id.

    Ids are derived from the artifact's bytes, so a collision means two writes
    disagree about a run they both believe is the same one. Refusing is the
    conservative reading: an overwrite would silently discard whichever record
    happened to be written first.
    """


@runtime_checkable
class StateBackend(Protocol):
    """The six operations that touch state which must survive a process.

    An implementation stores and returns already-valid models. It may assume
    every model it is handed has passed the checks in `core.checkpoint`, and it
    must not add checks of its own -- a backend that rejected what the
    filesystem accepts would make correctness depend on where state happened to
    be configured to live.
    """

    def write_checkpoint(self, checkpoint: Checkpoint) -> None:
        """Store a checkpoint, raising `CheckpointExists` on a duplicate id."""
        ...

    def read_checkpoint(self, checkpoint_id: str) -> Checkpoint:
        """Load a checkpoint, raising `CheckpointNotFound` when there is none."""
        ...

    def append_allocation_log_entry(self, entry: AllocationLogEntry) -> None:
        """Append one executed action. Append-only: there is no backfill for a
        record that is lost, so entries are never rewritten in place."""
        ...

    def read_allocation_log(self) -> tuple[AllocationLogEntry, ...]:
        """Every logged action, oldest first. Empty when nothing was logged."""
        ...

    def is_completed(self, scope: str, key: str) -> bool:
        """Whether this step already executed within this scope.

        The answer gates a broadcast, so a backend that cannot tell must raise
        rather than return False. "I don't know" and "not yet" are the same
        value here and opposite instructions.
        """
        ...

    def mark_completed(self, scope: str, key: str, value: JsonValue = None) -> None:
        """Record a step as executed, with whatever the caller knows about it.

        Called *after* the transaction is broadcast, so it must be durable
        before the next step is attempted -- buffering this is the same bug as
        losing the filesystem.
        """
        ...


class ScopedIdempotencyStore:
    """A backend and a scope, presented as the store the executors already take.

    `execute_allocation` and friends consume anything with `is_completed` and
    `mark_completed`; binding the scope here keeps the scope out of their
    signatures and out of every call site, so a run cannot accidentally ask
    about one allocation while executing another.
    """

    def __init__(self, backend: StateBackend, scope: str) -> None:
        self._backend = backend
        self._scope = scope

    @property
    def scope(self) -> str:
        return self._scope

    def is_completed(self, key: str) -> bool:
        return self._backend.is_completed(self._scope, key)

    def mark_completed(self, key: str, value: JsonValue = None) -> None:
        self._backend.mark_completed(self._scope, key, value)


class LocalFsBackend:
    """State under `.open_allocator/`, byte-for-byte as before this port existed.

    The file layout is unchanged on purpose: an existing checkpoint directory,
    allocation log and idempotency store stay readable, and the seam is
    verifiable by diffing what lands on disk rather than by trusting that a
    rewrite preserved a format.
    """

    def __init__(
        self,
        *,
        checkpoint_dir: str | Path | None = DEFAULT_CHECKPOINT_DIR,
        log_path: str | Path | None = DEFAULT_ALLOCATION_LOG_PATH,
        idempotency_store_path: str | Path | None = DEFAULT_IDEMPOTENCY_STORE_PATH,
    ) -> None:
        self.checkpoint_dir = None if checkpoint_dir is None else Path(checkpoint_dir)
        self.log_path = None if log_path is None else Path(log_path)
        self.idempotency_store_path = (
            None if idempotency_store_path is None else Path(idempotency_store_path)
        )

    @classmethod
    def from_config(cls, config: object) -> "LocalFsBackend":
        """Build from anything carrying the three path settings.

        Duck-typed rather than typed against `AllocatorConfig`, matching how the
        exec layer already reads its config, and so `core` keeps not importing
        `exec`.
        """
        return cls(
            checkpoint_dir=_config_path(config, "checkpoint_dir"),
            log_path=_config_path(config, "allocation_log_path"),
            idempotency_store_path=_config_path(config, "idempotency_store_path"),
        )

    def write_checkpoint(self, checkpoint: Checkpoint) -> None:
        directory = self._require(self.checkpoint_dir, "checkpoint_dir")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{checkpoint.id}.json"
        if path.exists():
            raise CheckpointExists(f"checkpoint already exists: {checkpoint.id}")
        # Written to a temporary name and renamed: a checkpoint read while it is
        # half-written is worse than one that is missing, because the missing
        # one is obvious.
        temp_path = directory / f".{checkpoint.id}.tmp"
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(
                checkpoint.model_dump(mode="json"),
                file,
                sort_keys=True,
                indent=2,
            )
            file.write("\n")
        temp_path.replace(path)

    def read_checkpoint(self, checkpoint_id: str) -> Checkpoint:
        from open_allocator.core.checkpoint import Checkpoint

        path = self.checkpoint_path(checkpoint_id)
        try:
            with path.open(encoding="utf-8") as file:
                payload = json.load(file)
        except FileNotFoundError as error:
            raise CheckpointNotFound(f"no checkpoint at {path}") from error
        if not isinstance(payload, Mapping):
            raise TypeError("checkpoint file must contain a JSON object")
        return Checkpoint.model_validate(payload)

    def checkpoint_path(self, checkpoint_id: str | Path) -> Path:
        """Resolve an id -- or a path, which only this backend understands."""
        path = Path(checkpoint_id)
        if path.exists() or path.suffix == ".json" or path.is_absolute():
            return path
        directory = self._require(self.checkpoint_dir, "checkpoint_dir")
        return directory / f"{checkpoint_id}.json"

    def append_allocation_log_entry(self, entry: AllocationLogEntry) -> None:
        path = self._require(self.log_path, "log_path")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry.model_dump(mode="json"), separators=(",", ":")))
            file.write("\n")

    def read_allocation_log(self) -> tuple[AllocationLogEntry, ...]:
        from open_allocator.core.checkpoint import AllocationLogEntry

        path = self._require(self.log_path, "log_path")
        if not path.exists():
            return ()

        entries: list[AllocationLogEntry] = []
        with path.open(encoding="utf-8") as file:
            for line_number, line in enumerate(file, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                payload = json.loads(stripped)
                if not isinstance(payload, Mapping):
                    raise TypeError(
                        f"allocation log line {line_number} is not an object"
                    )
                entries.append(AllocationLogEntry.model_validate(payload))
        return tuple(entries)

    def is_completed(self, scope: str, key: str) -> bool:
        return key in self._scope_data(self._read_idempotency(), scope)

    def mark_completed(self, scope: str, key: str, value: JsonValue = None) -> None:
        path = self._require(self.idempotency_store_path, "idempotency_store_path")
        payload = self._read_idempotency()
        entry: dict[str, JsonValue] = {"completed": True}
        if value is not None:
            entry["value"] = json_safe(value)
        self._scope_data(payload, scope)[key] = entry
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.tmp")
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, sort_keys=True, separators=(",", ":"))
        temp_path.replace(path)

    def _read_idempotency(self) -> dict[str, JsonValue]:
        path = self._require(self.idempotency_store_path, "idempotency_store_path")
        if not path.exists():
            return {"version": _IDEMPOTENCY_STORE_VERSION, "scopes": {}}

        with path.open(encoding="utf-8") as file:
            payload = json.load(file)
        if not isinstance(payload, dict):
            raise TypeError("idempotency store must contain a JSON object")
        payload.setdefault("version", _IDEMPOTENCY_STORE_VERSION)
        payload.setdefault("scopes", {})
        return payload

    @staticmethod
    def _scope_data(payload: dict[str, JsonValue], scope: str) -> dict[str, JsonValue]:
        scopes = payload.setdefault("scopes", {})
        if not isinstance(scopes, dict):
            raise TypeError("idempotency store scopes must be a JSON object")
        scope_data = scopes.setdefault(scope, {})
        if not isinstance(scope_data, dict):
            raise TypeError("idempotency store scope must be a JSON object")
        return scope_data

    @staticmethod
    def _require(path: Path | None, name: str) -> Path:
        if path is None:
            raise StateError(f"{name} is not configured on this backend")
        return path


class _ConfigWithBackend:
    """A config with a backend attached, by wrapping rather than by mutating it.

    `AllocatorConfig` is a pydantic settings model and refuses an attribute it
    never declared -- correctly, because a backend is an injected object and has
    no sensible environment value, so it is not a setting and should not sit in
    the settings surface. Wrapping leaves that model exactly what it was and
    still puts the backend where every reader already looks.
    """

    def __init__(self, config: object, backend: StateBackend) -> None:
        self._config = config
        self.state_backend = backend

    def __getattr__(self, name: str) -> object:
        # Reached only for names not on the wrapper itself. A Mapping config is
        # unwrapped by key, because the wrapper is not a Mapping and the callers
        # that special-case one would stop recognising it.
        config = self._config
        if isinstance(config, Mapping):
            try:
                return config[name]
            except KeyError as error:
                raise AttributeError(name) from error
        return getattr(config, name)

    def __repr__(self) -> str:
        return f"with_state_backend({self._config!r}, {self.state_backend!r})"


def with_state_backend(config: object, backend: StateBackend) -> object:
    """Attach a backend to an existing config, leaving the original untouched.

    This is the line the showcase job writes: build the usual `AllocatorConfig`,
    hand it a `PostgresBackend`, and every command run through it records where
    a Cloud Run retry can still see it.
    """
    return _ConfigWithBackend(config, backend)


def backend_from_config(config: object | None, *, needs: str) -> StateBackend | None:
    """The backend a config describes, or None when that record is switched off.

    This is the single injection point. A caller that wants state somewhere the
    filesystem cannot reach attaches a backend with `with_state_backend` and
    changes nothing else; the CLI and the exec layer both come through here.

    `needs` names the path this particular record requires, because each record
    keeps its own off switch: unsetting `checkpoint_dir` has always meant "write
    no checkpoints" without also silencing the allocation log or the idempotency
    store. An injected backend answers for all of them and skips the check.
    """
    if config is None:
        return None
    injected = _config_value(config, "state_backend")
    if injected is not None:
        return injected  # type: ignore[return-value]
    if _config_path(config, needs) is None:
        return None
    return LocalFsBackend.from_config(config)


def _config_value(config: object, attr: str) -> object | None:
    if isinstance(config, Mapping):
        return config.get(attr)
    return getattr(config, attr, None)


def _config_path(config: object, attr: str) -> Path | None:
    value = _config_value(config, attr)
    return None if value is None else Path(value)


def json_safe(value: object) -> JsonValue:
    """A JSON-compatible echo of a value, coercing rather than refusing.

    Deliberately the lenient counterpart to `checkpoint._json_compatible`, which
    raises. That one guards an artifact on its way *into* a checkpoint, where a
    value that will not serialise is a bug worth surfacing before anything is
    sent. This one runs *after* a transaction has been broadcast, where the same
    value must not be the reason the record of a sent trade fails to land, so
    unrepresentable leaves degrade to their `str`.
    """
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [json_safe(item) for item in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


__all__ = [
    "DEFAULT_ALLOCATION_LOG_PATH",
    "DEFAULT_CHECKPOINT_DIR",
    "DEFAULT_IDEMPOTENCY_STORE_PATH",
    "CheckpointExists",
    "CheckpointNotFound",
    "LocalFsBackend",
    "ScopedIdempotencyStore",
    "StateBackend",
    "StateError",
    "backend_from_config",
    "json_safe",
    "with_state_backend",
]
