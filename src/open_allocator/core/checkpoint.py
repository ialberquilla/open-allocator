from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeAlias

from pydantic import Field, model_validator

from open_allocator.core.schema import validate
from open_allocator.core.types import FrozenModel

JsonValue: TypeAlias = Any
CheckpointStatus: TypeAlias = Literal[
    "completed",
    "failed",
    "awaiting_human",
    "in_progress",
]

DEFAULT_CHECKPOINT_DIR = Path(".open_allocator/checkpoints")
DEFAULT_ALLOCATION_LOG_PATH = Path(".open_allocator/allocation-log.jsonl")
_VALIDATED_STATUSES = {"completed", "awaiting_human"}
_SCHEMA_BY_ARTIFACT_TYPE = {
    "allocation": "allocation",
    "tx-plan": "tx-plan",
    "policy": "policy",
    "vault-score": "vault-score",
}


class Checkpoint(FrozenModel):
    id: str
    stage: str
    status: CheckpointStatus
    artifact: JsonValue
    artifact_type: str | None = None
    schema_name: str | None = None
    created_at: str
    completed_keys: tuple[str, ...] = Field(default_factory=tuple)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class ResumeState(FrozenModel):
    checkpoint: Checkpoint
    completed_keys: tuple[str, ...]

    def idempotency_store(self) -> "CheckpointIdempotencyStore":
        return CheckpointIdempotencyStore(self.completed_keys)


class CheckpointIdempotencyStore:
    def __init__(self, completed_keys: Iterable[str] = ()) -> None:
        self.completed: dict[str, object] = {key: True for key in completed_keys}

    def is_completed(self, key: str) -> bool:
        return key in self.completed

    def mark_completed(self, key: str, value: object | None = None) -> None:
        self.completed[key] = _json_compatible(value) if value is not None else True


class AllocationLogEntry(FrozenModel):
    instrument_id: str
    chain_id: int
    action_type: str
    tx_hash: str
    timestamp: str
    usd: float | None = Field(default=None, ge=0)
    shares: str | None = None

    @model_validator(mode="after")
    def _has_amount(self) -> "AllocationLogEntry":
        if self.usd is None and self.shares is None:
            raise ValueError("allocation log entry requires usd or shares")
        return self


class AllocationLogReconciliation(FrozenModel):
    logged_usd_by_instrument: dict[str, float]
    position_usd_by_instrument: dict[str, float]
    total_logged_position_usd: float
    total_positions_usd: float
    usd_difference: float
    missing_in_positions: tuple[str, ...] = Field(default_factory=tuple)


def write_checkpoint(
    stage: str,
    status: CheckpointStatus,
    artifact: object,
    *,
    checkpoint_id: str | None = None,
    checkpoint_dir: str | Path = DEFAULT_CHECKPOINT_DIR,
    artifact_type: str | None = None,
    schema_name: str | None = None,
    metadata: Mapping[str, object] | None = None,
    completed_keys: Iterable[str] | None = None,
) -> Checkpoint:
    payload = _json_compatible(artifact)
    inferred_schema = schema_name or _infer_schema_name(payload, artifact_type)
    if status in _VALIDATED_STATUSES:
        _validate_known_artifact(payload, inferred_schema)

    completed = tuple(
        sorted(set(completed_keys or ()) | set(completed_keys_from_artifact(payload)))
    )
    checkpoint = Checkpoint(
        id=checkpoint_id or _checkpoint_id(stage, status, payload),
        stage=stage,
        status=status,
        artifact=payload,
        artifact_type=artifact_type,
        schema_name=inferred_schema,
        created_at=_timestamp(),
        completed_keys=completed,
        metadata=_metadata(metadata),
    )
    _write_checkpoint_file(Path(checkpoint_dir), checkpoint)
    return checkpoint


def read_checkpoint(
    checkpoint_id: str | Path,
    *,
    checkpoint_dir: str | Path = DEFAULT_CHECKPOINT_DIR,
) -> Checkpoint:
    path = _checkpoint_path(checkpoint_id, Path(checkpoint_dir))
    with path.open(encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, Mapping):
        raise TypeError("checkpoint file must contain a JSON object")
    return Checkpoint.model_validate(payload)


def resume_state(
    checkpoint_id: str | Path | Checkpoint | Mapping[str, object],
    *,
    checkpoint_dir: str | Path = DEFAULT_CHECKPOINT_DIR,
) -> ResumeState:
    checkpoint = _checkpoint(checkpoint_id, checkpoint_dir=checkpoint_dir)
    completed = tuple(sorted(completed_keys_from_checkpoint(checkpoint)))
    return ResumeState(checkpoint=checkpoint, completed_keys=completed)


def idempotency_store_from_checkpoint(
    checkpoint_id: str | Path | Checkpoint | Mapping[str, object],
    *,
    checkpoint_dir: str | Path = DEFAULT_CHECKPOINT_DIR,
) -> CheckpointIdempotencyStore:
    state = resume_state(checkpoint_id, checkpoint_dir=checkpoint_dir)
    return state.idempotency_store()


def completed_keys_from_checkpoint(
    checkpoint: Checkpoint | Mapping[str, object],
) -> frozenset[str]:
    checkpoint_model = (
        checkpoint
        if isinstance(checkpoint, Checkpoint)
        else Checkpoint.model_validate(checkpoint)
    )
    return frozenset(checkpoint_model.completed_keys) | completed_keys_from_artifact(
        checkpoint_model.artifact,
    )


def completed_keys_from_artifact(artifact: object) -> frozenset[str]:
    payload = _json_compatible(artifact)
    keys: set[str] = set()
    if isinstance(payload, Mapping):
        explicit = payload.get("completed_keys")
        if isinstance(explicit, Sequence) and not isinstance(explicit, str | bytes):
            keys.update(str(item) for item in explicit)
        _collect_completed_step_keys(payload.get("steps"), keys)
    return frozenset(keys)


def append_allocation_log_entry(
    entry: AllocationLogEntry | Mapping[str, object],
    *,
    log_path: str | Path = DEFAULT_ALLOCATION_LOG_PATH,
) -> AllocationLogEntry:
    model = (
        entry
        if isinstance(entry, AllocationLogEntry)
        else AllocationLogEntry.model_validate(entry)
    )
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(model.model_dump(mode="json"), separators=(",", ":")))
        file.write("\n")
    return model


def write_allocation_log_entry(
    *,
    instrument_id: str,
    chain_id: int,
    action_type: str,
    tx_hash: str,
    usd: float | None = None,
    shares: str | None = None,
    timestamp: str | None = None,
    log_path: str | Path = DEFAULT_ALLOCATION_LOG_PATH,
) -> AllocationLogEntry:
    return append_allocation_log_entry(
        AllocationLogEntry(
            instrument_id=instrument_id,
            chain_id=chain_id,
            action_type=action_type,
            tx_hash=tx_hash,
            timestamp=timestamp or _timestamp(),
            usd=usd,
            shares=shares,
        ),
        log_path=log_path,
    )


def read_allocation_log(
    *,
    log_path: str | Path = DEFAULT_ALLOCATION_LOG_PATH,
) -> tuple[AllocationLogEntry, ...]:
    path = Path(log_path)
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
                raise TypeError(f"allocation log line {line_number} is not an object")
            entries.append(AllocationLogEntry.model_validate(payload))
    return tuple(entries)


def allocation_log_totals(
    entries: Iterable[AllocationLogEntry | Mapping[str, object]],
) -> dict[str, float]:
    totals: dict[str, float] = {}
    for entry in entries:
        model = (
            entry
            if isinstance(entry, AllocationLogEntry)
            else AllocationLogEntry.model_validate(entry)
        )
        if model.usd is None:
            continue
        totals[model.instrument_id] = (
            totals.get(model.instrument_id, 0.0) + _signed_usd(model)
        )
    return {key: round(value, 6) for key, value in sorted(totals.items())}


def reconcile_allocation_log(
    entries: Iterable[AllocationLogEntry | Mapping[str, object]],
    positions: object,
) -> AllocationLogReconciliation:
    from open_allocator.core.positions import Positions

    positions_model = (
        positions
        if isinstance(positions, Positions)
        else Positions.model_validate(positions)
    )
    logged = allocation_log_totals(entries)
    position_totals: dict[str, float] = {}
    for holding in positions_model.holdings:
        position_totals[holding.instrument_id] = round(
            position_totals.get(holding.instrument_id, 0.0) + holding.usd_value,
            6,
        )
    total_logged = round(sum(logged.values()), 6)
    total_positions = round(sum(position_totals.values()), 6)
    return AllocationLogReconciliation(
        logged_usd_by_instrument=logged,
        position_usd_by_instrument=dict(sorted(position_totals.items())),
        total_logged_position_usd=total_logged,
        total_positions_usd=total_positions,
        usd_difference=round(total_positions - total_logged, 6),
        missing_in_positions=tuple(sorted(set(logged) - set(position_totals))),
    )


def _checkpoint(
    checkpoint_id: str | Path | Checkpoint | Mapping[str, object],
    *,
    checkpoint_dir: str | Path,
) -> Checkpoint:
    if isinstance(checkpoint_id, Checkpoint):
        return checkpoint_id
    if isinstance(checkpoint_id, Mapping):
        return Checkpoint.model_validate(checkpoint_id)
    return read_checkpoint(checkpoint_id, checkpoint_dir=checkpoint_dir)


def _validate_known_artifact(payload: JsonValue, schema_name: str | None) -> None:
    if schema_name is not None:
        validate(payload, schema_name)
        return
    if isinstance(payload, Mapping) and isinstance(payload.get("plan"), Mapping):
        validate(payload["plan"], "tx-plan")


def _infer_schema_name(payload: JsonValue, artifact_type: str | None) -> str | None:
    if artifact_type is not None:
        schema_name = _SCHEMA_BY_ARTIFACT_TYPE.get(artifact_type)
        if schema_name is not None:
            return schema_name

    if not isinstance(payload, Mapping):
        return None
    keys = set(payload)
    if {"legs", "total_usd"}.issubset(keys):
        return "allocation"
    if {"steps", "summary"}.issubset(keys):
        return "tx-plan"
    if {"wallet", "allowed", "caps", "gates"}.issubset(keys):
        return "policy"
    if {"instrument_id", "score", "factors"}.issubset(keys):
        return "vault-score"
    return None


def _collect_completed_step_keys(value: object, keys: set[str]) -> None:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return
    for step in value:
        if not isinstance(step, Mapping):
            continue
        status = step.get("status")
        key = step.get("idempotency_key")
        if status in {"sent", "skipped", "completed"} and key is not None:
            keys.add(str(key))


def _metadata(metadata: Mapping[str, object] | None) -> dict[str, JsonValue]:
    if metadata is None:
        return {}
    payload = _json_compatible(dict(metadata))
    if not isinstance(payload, dict):
        raise TypeError("checkpoint metadata must be a JSON object")
    return payload


def _json_compatible(value: object) -> JsonValue:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        value = model_dump(mode="json")
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise TypeError(f"value is not JSON-compatible: {error}") from error


def _checkpoint_id(stage: str, status: str, artifact: JsonValue) -> str:
    encoded = json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode(
        "utf-8",
    )
    digest = hashlib.sha256(encoded).hexdigest()[:16]
    safe_stage = "".join(
        char if char.isalnum() or char in {"-", "_"} else "-" for char in stage
    ).strip("-")
    return f"{_timestamp(compact=True)}-{safe_stage or 'checkpoint'}-{status}-{digest}"


def _write_checkpoint_file(checkpoint_dir: Path, checkpoint: Checkpoint) -> None:
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    path = checkpoint_dir / f"{checkpoint.id}.json"
    if path.exists():
        raise FileExistsError(f"checkpoint already exists: {checkpoint.id}")
    temp_path = checkpoint_dir / f".{checkpoint.id}.tmp"
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(checkpoint.model_dump(mode="json"), file, sort_keys=True, indent=2)
        file.write("\n")
    temp_path.replace(path)


def _checkpoint_path(checkpoint_id: str | Path, checkpoint_dir: Path) -> Path:
    path = Path(checkpoint_id)
    if path.exists() or path.suffix == ".json" or path.is_absolute():
        return path
    return checkpoint_dir / f"{checkpoint_id}.json"


def _timestamp(*, compact: bool = False) -> str:
    now = datetime.now(UTC)
    if compact:
        return now.strftime("%Y%m%dT%H%M%S%fZ")
    return now.isoformat().replace("+00:00", "Z")


def _signed_usd(entry: AllocationLogEntry) -> float:
    amount = entry.usd or 0.0
    action = entry.action_type.casefold()
    if action in {"sell", "withdraw", "exit"}:
        return -amount
    return amount


__all__ = [
    "AllocationLogEntry",
    "AllocationLogReconciliation",
    "Checkpoint",
    "CheckpointIdempotencyStore",
    "CheckpointStatus",
    "DEFAULT_ALLOCATION_LOG_PATH",
    "DEFAULT_CHECKPOINT_DIR",
    "ResumeState",
    "allocation_log_totals",
    "append_allocation_log_entry",
    "completed_keys_from_artifact",
    "completed_keys_from_checkpoint",
    "idempotency_store_from_checkpoint",
    "read_allocation_log",
    "read_checkpoint",
    "reconcile_allocation_log",
    "resume_state",
    "write_allocation_log_entry",
    "write_checkpoint",
]
