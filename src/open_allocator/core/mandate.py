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
it was derived from -- is the reason this file exists. It is what makes a
passing result mean the policy is *safe* and not merely well-formed: a model
may narrow the constitution it was handed, never widen it.

"Tighter" is not one direction, and getting that wrong would pass exactly the
mandate this check exists to catch. The knobs sort into five kinds, and each
kind is compared by its own rule:

  ceilings   tighter = LOWER    every `caps.max_weight_per_*`,
                                `caps.max_reward_dependence`,
                                `gates.max_deploy_per_cycle_usd`
  floors     tighter = HIGHER   `caps.min_effective_positions`,
                                `caps.min_instrument_tvl_usd`
  allowlists tighter = SUBSET   every `allowed.*` list
  flags      tighter = the safe value, named per flag: restricting to
                                stablecoins and requiring approval are tighter;
                                autonomous rebalancing is looser
  fixed      not an axis at all -- `version` and `wallet` must match, because
                                changing the signer is not a tightening, it is
                                changing the subject

`null` is never neutral. Every nullable knob here is permissive when absent --
an unset sector cap is 1.00, an unset `min_effective_positions` is unenforced,
an unset allowlist is the whole universe -- so each is compared *as* that
permissive extreme rather than skipped. A derivation that drops a cap the
baseline set is therefore rejected as loosening, which is the intent: silence
is the widest possible value, not the absence of one.

Note that `docs/capabilities.md` groups `min_instrument_tvl_usd` with the
ceilings for narrative reasons. That grouping is prose, not this table.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from open_allocator.core.policy_loader import load_policy
from open_allocator.core.schema import SchemaValidationError, validate
from open_allocator.core.types import FrozenModel, Policy

HASH_PREFIX = "sha256:"

# The comparison table for check 4, one entry per policy knob. It is written
# out rather than derived from the model because a knob this table forgets is a
# knob nothing checks -- `test_every_policy_knob_has_a_direction` fails when a
# new field is added to `Policy` without a decision about which way it tightens.
#
# The second element of each numeric entry is the value a `None` stands in for:
# always the permissive extreme, so an omitted cap compares as no cap at all.
CEILINGS: tuple[tuple[str, float], ...] = (
    ("caps.max_weight_per_instrument", 1.0),
    ("caps.max_weight_per_protocol", 1.0),
    ("caps.max_weight_per_curator", 1.0),
    ("caps.max_weight_per_chain", 1.0),
    ("caps.max_weight_per_sector", 1.0),
    ("caps.max_reward_dependence", 1.0),
    ("gates.max_deploy_per_cycle_usd", math.inf),
)

FLOORS: tuple[tuple[str, float], ...] = (
    ("caps.min_effective_positions", 0.0),
    ("caps.min_instrument_tvl_usd", 0.0),
)

ALLOWLISTS: tuple[str, ...] = (
    "allowed.protocols",
    "allowed.chains",
    "allowed.asset_categories",
    "allowed.assets",
    "allowed.curators",
)

# (knob, the value that restricts). Absent reads as the other one.
FLAGS: tuple[tuple[str, bool], ...] = (
    ("allowed.stablecoin_only", True),
    ("gates.new_instrument_needs_approval", True),
    ("gates.autonomous_rebalance", False),
)

FIXED: tuple[str, ...] = (
    "version",
    "wallet.mode",
    "wallet.signer",
)


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
    if not checks[-1].ok:
        return _result(mandate_file, derived_file, baseline_file, checks)

    baseline = _loaded_baseline(baseline_file)
    _check_can_only_tighten(derived, baseline, derived_file, baseline_file, checks)

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
                    f"{path.name} does not validate against policy.schema.json: {error}"
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


def _loaded_baseline(path: Path) -> Policy:
    """The policy the derivation started from.

    Both failures raise rather than returning `ok=False`: a baseline that
    cannot be read is the caller pointing at the wrong file, not a mandate
    being rejected, and there is nothing to compare against either way.
    """
    try:
        return load_policy(path)
    except FileNotFoundError as error:
        raise MandateError(f"baseline policy not found: {path}") from error
    except SchemaValidationError as error:
        raise MandateError(
            f"baseline is not a valid policy, so nothing can be compared "
            f"against it: {path}: {error}"
        ) from error


def _check_can_only_tighten(
    derived: Policy,
    baseline: Policy,
    derived_file: Path,
    baseline_file: Path,
    checks: list[MandateCheck],
) -> None:
    """Reject a derived policy that widens anything the baseline narrowed.

    Reports *every* loosened knob, not the first: a derivation that widened
    three caps has three things wrong with it, and fixing them one run at a
    time hides how far off the derivation was.
    """
    loosened: list[str] = []
    tightened: list[str] = []

    for knob, when_absent in CEILINGS:
        _compare_bound(derived, baseline, knob, when_absent, loosened, tightened)
    for knob, when_absent in FLOORS:
        _compare_bound(
            derived, baseline, knob, when_absent, loosened, tightened, floor=True
        )
    for knob in ALLOWLISTS:
        _compare_allowlist(derived, baseline, knob, loosened, tightened)
    for knob, restricting in FLAGS:
        _compare_flag(derived, baseline, knob, restricting, loosened, tightened)
    for knob in FIXED:
        _compare_fixed(derived, baseline, knob, loosened)

    if loosened:
        checks.append(
            MandateCheck(
                name="can_only_tighten",
                ok=False,
                detail=(
                    f"{derived_file.name} loosens {len(loosened)} knob(s) against "
                    f"{baseline_file.name}: {'; '.join(loosened)}. A derivation may "
                    f"only narrow the baseline -- widen it in {baseline_file.name} "
                    f"deliberately, not in a file a model wrote."
                ),
            )
        )
        return

    summary = ", ".join(tightened) if tightened else "no knob moved"
    checks.append(
        MandateCheck(
            name="can_only_tighten",
            ok=True,
            detail=(
                f"{derived_file.name} loosens nothing against "
                f"{baseline_file.name} ({summary})"
            ),
        )
    )


def _compare_bound(
    derived: Policy,
    baseline: Policy,
    knob: str,
    when_absent: float,
    loosened: list[str],
    tightened: list[str],
    *,
    floor: bool = False,
) -> None:
    derived_value = _absent_is_permissive(_read(derived, knob), when_absent)
    baseline_value = _absent_is_permissive(_read(baseline, knob), when_absent)
    if math.isclose(derived_value, baseline_value, rel_tol=1e-9, abs_tol=1e-12):
        return

    if floor:
        stricter = derived_value > baseline_value
    else:
        stricter = derived_value < baseline_value
    movement = f"{knob} {_show(baseline, knob)} -> {_show(derived, knob)}"
    if stricter:
        tightened.append(movement)
        return
    loosened.append(
        f"{movement} ({'floor, must not fall' if floor else 'ceiling, must not rise'})"
    )


def _compare_allowlist(
    derived: Policy,
    baseline: Policy,
    knob: str,
    loosened: list[str],
    tightened: list[str],
) -> None:
    derived_value = _read(derived, knob)
    baseline_value = _read(baseline, knob)
    if derived_value == baseline_value:
        return

    # None is the whole universe, so it is a superset of everything: dropping a
    # list the baseline set widens the shelf back open.
    if derived_value is None:
        loosened.append(
            f"{knob} {_show(baseline, knob)} -> null "
            f"(allowlist, dropping it allows everything again)"
        )
        return
    if baseline_value is None:
        tightened.append(f"{knob} null -> {_show(derived, knob)}")
        return

    added = sorted(set(derived_value) - set(baseline_value), key=str)
    if added:
        loosened.append(
            f"{knob} admits {added}, which the baseline does not "
            f"(allowlist, must be a subset)"
        )
        return
    tightened.append(f"{knob} {_show(baseline, knob)} -> {_show(derived, knob)}")


def _compare_flag(
    derived: Policy,
    baseline: Policy,
    knob: str,
    restricting: bool,
    loosened: list[str],
    tightened: list[str],
) -> None:
    derived_value = _read(derived, knob) is restricting
    baseline_value = _read(baseline, knob) is restricting
    if derived_value == baseline_value:
        return

    movement = f"{knob} {_show(baseline, knob)} -> {_show(derived, knob)}"
    if derived_value:
        tightened.append(movement)
        return
    loosened.append(f"{movement} (flag, {restricting!r} is the restricting value)")


def _compare_fixed(
    derived: Policy,
    baseline: Policy,
    knob: str,
    loosened: list[str],
) -> None:
    if _read(derived, knob) == _read(baseline, knob):
        return
    loosened.append(
        f"{knob} {_show(baseline, knob)} -> {_show(derived, knob)} "
        f"(not a tightening in either direction -- a derivation may not change it)"
    )


def _read(policy: Policy, knob: str) -> Any:
    value: Any = policy
    for part in knob.split("."):
        value = getattr(value, part)
    return value


def _show(policy: Policy, knob: str) -> str:
    value = _read(policy, knob)
    return "null" if value is None else str(value)


def _absent_is_permissive(value: float | None, when_absent: float) -> float:
    return when_absent if value is None else float(value)


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
