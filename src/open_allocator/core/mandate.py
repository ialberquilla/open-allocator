"""Validation for mandate artifacts.

A mandate is an LLM-authored file. That is the whole reason this module is
deterministic Python and contains no model call: the agent proposes knobs and a
reason for each, and everything downstream of that proposal is checked here
before any of it reaches an allocation.

The checks are ordered so that each one may assume the previous passed, and the
first failure stops the run. Structure before contents, contents before
identity: a file that is not a mandate cannot have its policy read, and a policy
that is not a policy cannot meaningfully be hashed against a claim about it.

Check 4 -- can-only-tighten, comparing the derived policy against the baseline
it was derived from -- is the reason this file exists and is not implemented
yet. Until it lands, a passing result here means the mandate is well-formed and
honestly bound to a policy file; it does not yet mean the policy is safe.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from open_allocator.core.policy_loader import load_policy
from open_allocator.core.schema import SchemaValidationError, validate
from open_allocator.core.types import FrozenModel, Policy

HASH_PREFIX = "sha256:"


class MandateError(ValueError):
    """A mandate could not be loaded or does not describe what it claims to."""


class RationaleEntry(FrozenModel):
    knob: str
    # Deliberately untyped. `core.types.JsonValue` bottoms out at scalars one
    # level down, and a rationale entry routinely justifies a whole tier ladder
    # -- a list of objects -- so typing this as JsonValue rejects the most
    # important entries in the file. The schema constrains the shape; this
    # model only has to carry it.
    value: Any
    because: str


class MandateBands(FrozenModel):
    weight_drift_bps: int = Field(ge=0, le=10_000)
    sleeve_drift_bps: int = Field(ge=0, le=10_000)


class Mandate(BaseModel):
    """The parsed artifact.

    Not frozen-strict like the rest of `types`: `strategy_params` is an open
    bag because strategies own their own parameter shapes, and the schema is
    the authority on what may appear there.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(ge=1)
    text: str
    derived_at: str
    derived_by: str
    policy_path: str
    policy_hash: str
    strategy: str
    # Same reason as RationaleEntry.value: tiers are a list of objects, which
    # JsonValue cannot express. Strategies own their parameter shapes and the
    # schema is the authority on what may appear here.
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    bands: MandateBands
    rationale: tuple[RationaleEntry, ...]


class MandateCheck(FrozenModel):
    name: str
    ok: bool
    detail: str


class MandateResult(FrozenModel):
    ok: bool
    mandate_path: str
    policy_path: str
    baseline_path: str
    checks: tuple[MandateCheck, ...]


def load_mandate(path: str | Path) -> Mandate:
    """Read a mandate file and validate it against `schemas/mandate.schema.json`."""
    mandate_path = Path(path)
    with mandate_path.open(encoding="utf-8") as file:
        raw: Any = yaml.safe_load(file)

    validate(raw, "mandate")
    return Mandate.model_validate(raw)


def policy_digest(path: str | Path) -> str:
    """The `sha256:<hex>` of a policy file's bytes.

    Bytes, not parsed contents: the comments in a derived policy carry the
    measurements that justify its numbers, so a policy stripped of them is not
    the file the rationale was written about.
    """
    return HASH_PREFIX + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_mandate(
    mandate_path: str | Path,
    baseline_path: str | Path,
) -> MandateResult:
    """Run the mandate gate, stopping at the first failed check.

    Raises `MandateError` rather than returning `ok=False` when the failure
    means no further check can run -- an unparseable file, a missing policy.
    A returned `ok=False` is a mandate that could be read and was rejected.
    """
    mandate_file = Path(mandate_path)
    baseline_file = Path(baseline_path)
    checks: list[MandateCheck] = []

    mandate = _checked_mandate(mandate_file, checks)
    # Relative to the mandate, not to the process's working directory: the
    # pair travels together and must validate the same way from anywhere.
    derived_file = _resolve_policy_path(mandate_file, mandate.policy_path)

    derived = _checked_policy(derived_file, checks)
    if derived is None:
        return _result(mandate_file, derived_file, baseline_file, checks)

    _check_hash(derived_file, mandate.policy_hash, checks)

    return _result(mandate_file, derived_file, baseline_file, checks)


def _checked_mandate(path: Path, checks: list[MandateCheck]) -> Mandate:
    try:
        mandate = load_mandate(path)
    except FileNotFoundError as error:
        raise MandateError(f"mandate file not found: {path}") from error
    except yaml.YAMLError as error:
        raise MandateError(f"mandate is not valid YAML: {path}: {error}") from error
    except SchemaValidationError as error:
        raise MandateError(str(error)) from error

    checks.append(
        MandateCheck(
            name="mandate_schema",
            ok=True,
            detail=(
                f"{path.name} validates against mandate.schema.json "
                f"({len(mandate.rationale)} rationale entries)"
            ),
        )
    )
    return mandate


def _checked_policy(path: Path, checks: list[MandateCheck]) -> Policy | None:
    try:
        policy = load_policy(path)
    except FileNotFoundError as error:
        raise MandateError(
            f"mandate references a policy that does not exist: {path}"
        ) from error
    except SchemaValidationError as error:
        checks.append(
            MandateCheck(
                name="policy_schema",
                ok=False,
                detail=(
                    f"{path.name} does not validate against "
                    f"policy.schema.json: {error}"
                ),
            )
        )
        return None

    checks.append(
        MandateCheck(
            name="policy_schema",
            ok=True,
            detail=f"{path.name} validates against policy.schema.json",
        )
    )
    return policy


def _check_hash(path: Path, claimed: str, checks: list[MandateCheck]) -> None:
    actual = policy_digest(path)
    if actual == claimed:
        checks.append(
            MandateCheck(
                name="policy_hash",
                ok=True,
                detail=f"{path.name} matches the hash the mandate claims for it",
            )
        )
        return

    checks.append(
        MandateCheck(
            name="policy_hash",
            ok=False,
            detail=(
                f"{path.name} has changed since the mandate was derived: "
                f"mandate claims {claimed}, file is {actual}. The rationale no "
                f"longer describes this policy -- re-derive rather than re-hash."
            ),
        )
    )


def _resolve_policy_path(mandate_file: Path, policy_path: str) -> Path:
    candidate = Path(policy_path)
    if candidate.is_absolute():
        return candidate
    return mandate_file.parent / candidate


def _result(
    mandate_file: Path,
    derived_file: Path,
    baseline_file: Path,
    checks: list[MandateCheck],
) -> MandateResult:
    return MandateResult(
        ok=all(check.ok for check in checks),
        mandate_path=str(mandate_file),
        policy_path=str(derived_file),
        baseline_path=str(baseline_file),
        checks=tuple(checks),
    )
