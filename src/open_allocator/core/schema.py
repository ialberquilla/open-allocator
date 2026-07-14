from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TypeVar

from jsonschema import Draft202012Validator

SCHEMA_DIR = Path(__file__).resolve().parents[3] / "schemas"
T = TypeVar("T")


@dataclass(frozen=True)
class SchemaViolation:
    path: str
    message: str


class SchemaNotFoundError(ValueError):
    pass


class SchemaValidationError(ValueError):
    def __init__(self, schema_name: str, violations: Sequence[SchemaViolation]) -> None:
        self.schema_name = schema_name
        self.violations = tuple(violations)
        paths = ", ".join(violation.path for violation in self.violations)
        details = "; ".join(
            f"{violation.path}: {violation.message}"
            for violation in self.violations
        )
        super().__init__(
            f"{schema_name} schema validation failed at {paths}: {details}"
        )

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(violation.path for violation in self.violations)


def validate(obj: T, schema_name: str) -> T:
    validator = _validator(schema_name)
    instance = _json_compatible(obj, schema_name)
    violations = tuple(_violations(validator.iter_errors(instance)))

    if violations:
        raise SchemaValidationError(schema_name, violations)

    return obj


@cache
def _validator(schema_name: str) -> Draft202012Validator:
    schema_path = _schema_path(schema_name)
    schema = json.loads(schema_path.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _json_compatible(obj: object, schema_name: str) -> object:
    try:
        return json.loads(json.dumps(obj, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise SchemaValidationError(
            schema_name,
            (SchemaViolation("$", f"object is not JSON-serializable: {error}"),),
        ) from error


def _schema_path(schema_name: str) -> Path:
    if "/" in schema_name or "\\" in schema_name:
        raise _unknown_schema(schema_name)

    canonical_name = schema_name.removesuffix(".schema.json")
    schema_paths = _schema_paths()
    path = schema_paths.get(canonical_name)
    if path is None:
        raise _unknown_schema(schema_name)
    return path


@cache
def _schema_paths() -> dict[str, Path]:
    if not SCHEMA_DIR.exists():
        return {}

    return {
        path.name.removesuffix(".schema.json"): path
        for path in SCHEMA_DIR.glob("*.schema.json")
    }


def _unknown_schema(schema_name: str) -> SchemaNotFoundError:
    available = ", ".join(sorted(_schema_paths())) or "none"
    return SchemaNotFoundError(
        f"unknown schema {schema_name!r}; available schemas: {available}"
    )


def _violations(errors: object) -> list[SchemaViolation]:
    violations: list[SchemaViolation] = []
    sorted_errors = sorted(
        errors,
        key=lambda item: (list(item.absolute_path), item.message),
    )
    for error in sorted_errors:
        if error.validator == "required":
            violations.extend(_required_violations(error))
        elif error.validator == "additionalProperties":
            violations.extend(_additional_property_violations(error))
        else:
            violations.append(
                SchemaViolation(_format_path(error.absolute_path), error.message)
            )

    return sorted(violations, key=lambda violation: (violation.path, violation.message))


def _required_violations(error: object) -> list[SchemaViolation]:
    if not isinstance(error.instance, Mapping):
        return [SchemaViolation(_format_path(error.absolute_path), error.message)]

    return [
        SchemaViolation(
            _format_path((*error.absolute_path, field)),
            "required property missing",
        )
        for field in error.validator_value
        if field not in error.instance
    ]


def _additional_property_violations(error: object) -> list[SchemaViolation]:
    if not isinstance(error.instance, Mapping):
        return [SchemaViolation(_format_path(error.absolute_path), error.message)]

    properties = set(error.schema.get("properties", {}))
    extras = sorted(set(error.instance) - properties)
    return [
        SchemaViolation(
            _format_path((*error.absolute_path, field)),
            "unexpected property",
        )
        for field in extras
    ]


def _format_path(parts: object) -> str:
    path = "$"
    for part in parts:
        if isinstance(part, int):
            path = f"{path}[{part}]"
        else:
            path = f"{path}.{part}"
    return path
