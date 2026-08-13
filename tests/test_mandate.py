from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from open_allocator.core.mandate import (
    ALLOWLISTS,
    CEILINGS,
    FIXED,
    FLAGS,
    FLOORS,
    MandateError,
    load_mandate,
    policy_digest,
    validate_mandate,
)
from open_allocator.core.types import Policy

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_POLICY = REPO_ROOT / "policy.yaml"


def mandate_doc(**updates: object) -> dict[str, object]:
    base: dict[str, object] = {
        "version": 1,
        "text": "different buckets, good yield, enough diversification\n",
        "derived_at": "2026-08-11T11:40:00Z",
        "derived_by": "test",
        "policy_path": "policy-derived.yaml",
        "policy_hash": "sha256:" + "0" * 64,
        "strategy": "sleeves",
        "strategy_params": {
            "tiers": [
                {
                    "name": "core",
                    "min_score": 0.85,
                    "max_score": 1.01,
                    "weight": 0.5,
                    "min_positions": 5,
                },
                {
                    "name": "frontier",
                    "min_score": 0.0,
                    "max_score": 0.85,
                    "weight": 0.5,
                    "min_positions": 12,
                },
            ]
        },
        "bands": {"weight_drift_bps": 100, "sleeve_drift_bps": 500},
        "rationale": [
            {
                "knob": "strategy",
                "value": "sleeves",
                "because": "the mandate asks for declared buckets",
            }
        ],
    }
    base.update(updates)
    return base


def write_pair(
    tmp_path: Path,
    *,
    mandate_updates: dict[str, object] | None = None,
    policy_text: str | None = None,
    rehash: bool = True,
) -> Path:
    """Write a mandate + derived policy pair and return the mandate path."""
    policy_file = tmp_path / "policy-derived.yaml"
    policy_file.write_text(
        policy_text
        if policy_text is not None
        else BASELINE_POLICY.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    doc = mandate_doc(**(mandate_updates or {}))
    if rehash:
        doc["policy_hash"] = policy_digest(policy_file)

    mandate_file = tmp_path / "mandate.yaml"
    mandate_file.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return mandate_file


# --- the repo's own artifact ------------------------------------------------


def test_shipped_mandate_passes_every_check() -> None:
    """The mandate in the repo root is the worked example; it must stay valid."""
    result = validate_mandate(REPO_ROOT / "mandate.yaml", BASELINE_POLICY)

    assert result.ok
    assert [check.name for check in result.checks] == [
        "mandate_schema",
        "policy_schema",
        "policy_hash",
        "can_only_tighten",
    ]
    assert all(check.ok for check in result.checks)


def test_shipped_derived_policy_reports_the_knobs_it_tightened() -> None:
    """The two caps the derivation moved are named, so a reader can see them."""
    result = validate_mandate(REPO_ROOT / "mandate.yaml", BASELINE_POLICY)

    detail = next(
        check.detail for check in result.checks if check.name == "can_only_tighten"
    )
    assert "caps.min_effective_positions 3.0 -> 3.5" in detail
    assert "caps.max_reward_dependence 0.5 -> 0.4" in detail


def test_shipped_mandate_explains_every_knob_it_moves() -> None:
    """Rationale is the product. A knob without a reason is a silent change."""
    mandate = load_mandate(REPO_ROOT / "mandate.yaml")
    explained = {entry.knob for entry in mandate.rationale}

    assert "strategy" in explained
    assert any(knob.startswith("strategy_params.tiers") for knob in explained)
    assert any(knob.startswith("bands.") for knob in explained)
    assert all(entry.because.strip() for entry in mandate.rationale)


# --- check 1: mandate schema ------------------------------------------------


def test_missing_required_key_is_rejected(tmp_path: Path) -> None:
    doc = mandate_doc()
    del doc["rationale"]
    mandate_file = tmp_path / "mandate.yaml"
    mandate_file.write_text(yaml.safe_dump(doc), encoding="utf-8")

    with pytest.raises(MandateError, match="rationale"):
        validate_mandate(mandate_file, BASELINE_POLICY)


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    """The schema is closed: a typo must fail loudly, not be ignored."""
    mandate_file = write_pair(tmp_path, mandate_updates={"policy_hsah": "typo"})

    with pytest.raises(MandateError, match="policy_hsah"):
        validate_mandate(mandate_file, BASELINE_POLICY)


def test_empty_rationale_is_rejected(tmp_path: Path) -> None:
    mandate_file = write_pair(tmp_path, mandate_updates={"rationale": []})

    with pytest.raises(MandateError):
        validate_mandate(mandate_file, BASELINE_POLICY)


def test_malformed_hash_is_rejected(tmp_path: Path) -> None:
    mandate_file = write_pair(
        tmp_path,
        mandate_updates={"policy_hash": "9c1f"},
        rehash=False,
    )

    with pytest.raises(MandateError, match="policy_hash"):
        validate_mandate(mandate_file, BASELINE_POLICY)


def test_composite_strategy_inside_a_tier_is_rejected(tmp_path: Path) -> None:
    """Sleeve sub-strategies must be flat, matching what the library accepts."""
    tiers = [
        {
            "name": "core",
            "min_score": 0.5,
            "max_score": 1.01,
            "weight": 1.0,
            "strategy": "sleeves",
        }
    ]
    mandate_file = write_pair(
        tmp_path,
        mandate_updates={"strategy_params": {"tiers": tiers}},
    )

    with pytest.raises(MandateError):
        validate_mandate(mandate_file, BASELINE_POLICY)


def test_negative_min_positions_is_rejected(tmp_path: Path) -> None:
    tiers = [
        {
            "name": "core",
            "min_score": 0.5,
            "max_score": 1.01,
            "weight": 1.0,
            "min_positions": -1,
        }
    ]
    mandate_file = write_pair(
        tmp_path,
        mandate_updates={"strategy_params": {"tiers": tiers}},
    )

    with pytest.raises(MandateError):
        validate_mandate(mandate_file, BASELINE_POLICY)


def test_rationale_value_may_be_a_tier_ladder(tmp_path: Path) -> None:
    """The entries that matter most justify structures, not scalars."""
    rationale = [
        {
            "knob": "strategy_params.tiers",
            "value": [{"name": "core", "min_score": 0.85}],
            "because": "cut where the scores sit",
        }
    ]
    mandate_file = write_pair(tmp_path, mandate_updates={"rationale": rationale})

    result = validate_mandate(mandate_file, BASELINE_POLICY)

    assert result.ok


# --- check 2: policy schema -------------------------------------------------


def test_derived_policy_that_is_not_a_policy_fails_without_raising(
    tmp_path: Path,
) -> None:
    """A bad policy is a rejection, not a crash: the mandate itself was readable."""
    mandate_file = write_pair(tmp_path, policy_text="version: 1\ncaps: {}\n")

    result = validate_mandate(mandate_file, BASELINE_POLICY)

    assert not result.ok
    failed = [check for check in result.checks if not check.ok]
    assert [check.name for check in failed] == ["policy_schema"]


def test_hash_is_not_checked_once_the_policy_is_invalid(tmp_path: Path) -> None:
    """Later checks assume earlier ones passed; don't report on a bad file."""
    mandate_file = write_pair(tmp_path, policy_text="version: 1\ncaps: {}\n")

    result = validate_mandate(mandate_file, BASELINE_POLICY)

    assert "policy_hash" not in {check.name for check in result.checks}


def test_missing_derived_policy_raises(tmp_path: Path) -> None:
    doc = mandate_doc(policy_path="nowhere.yaml")
    mandate_file = tmp_path / "mandate.yaml"
    mandate_file.write_text(yaml.safe_dump(doc), encoding="utf-8")

    with pytest.raises(MandateError, match="does not exist"):
        validate_mandate(mandate_file, BASELINE_POLICY)


# --- check 3: hash binding --------------------------------------------------


def test_edited_policy_breaks_the_binding(tmp_path: Path) -> None:
    """The point of the hash: a policy edited after derivation is rejected."""
    mandate_file = write_pair(tmp_path)
    policy_file = tmp_path / "policy-derived.yaml"
    policy_file.write_text(
        policy_file.read_text(encoding="utf-8").replace(
            "min_effective_positions: 3.0", "min_effective_positions: 1.0"
        ),
        encoding="utf-8",
    )

    result = validate_mandate(mandate_file, BASELINE_POLICY)

    assert not result.ok
    hash_check = next(check for check in result.checks if check.name == "policy_hash")
    assert not hash_check.ok
    assert "re-derive" in hash_check.detail


def test_comment_only_edit_breaks_the_binding(tmp_path: Path) -> None:
    """Hashing bytes, not parsed values: the comments carry the measurements."""
    mandate_file = write_pair(tmp_path)
    policy_file = tmp_path / "policy-derived.yaml"
    policy_file.write_text(
        policy_file.read_text(encoding="utf-8") + "\n# a later note\n",
        encoding="utf-8",
    )

    result = validate_mandate(mandate_file, BASELINE_POLICY)

    assert not result.ok


def test_policy_path_resolves_against_the_mandate_not_the_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pair travels together; validation must not depend on where it runs."""
    nested = tmp_path / "config"
    nested.mkdir()
    mandate_file = write_pair(nested)

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    result = validate_mandate(mandate_file, BASELINE_POLICY)

    assert result.ok


# --- check 4: can-only-tighten ----------------------------------------------


def tighten(
    tmp_path: Path,
    *replacements: tuple[str, str],
    baseline: str | None = None,
) -> tuple[Path, Path]:
    """A mandate whose derived policy is the baseline with lines swapped out."""
    baseline_text = (
        baseline
        if baseline is not None
        else BASELINE_POLICY.read_text(encoding="utf-8")
    )
    baseline_file = tmp_path / "policy.yaml"
    baseline_file.write_text(baseline_text, encoding="utf-8")

    derived_text = baseline_text
    for old, new in replacements:
        assert old in derived_text, f"fixture no longer matches the policy: {old}"
        derived_text = derived_text.replace(old, new)

    return write_pair(tmp_path, policy_text=derived_text), baseline_file


def rejection(result: object) -> str:
    """The detail of the can_only_tighten failure, asserting there is one."""
    checks = result.checks  # type: ignore[attr-defined]
    check = next(check for check in checks if check.name == "can_only_tighten")
    assert not check.ok
    return check.detail


def test_an_unchanged_policy_is_accepted(tmp_path: Path) -> None:
    """Equal is not looser. A mandate may move nothing but the strategy."""
    mandate_file, baseline_file = tighten(tmp_path)

    result = validate_mandate(mandate_file, baseline_file)

    assert result.ok
    assert "no knob moved" in result.checks[-1].detail


def test_a_raised_ceiling_is_rejected(tmp_path: Path) -> None:
    mandate_file, baseline_file = tighten(
        tmp_path,
        ("max_weight_per_instrument: 0.30", "max_weight_per_instrument: 0.40"),
    )

    result = validate_mandate(mandate_file, baseline_file)

    assert not result.ok
    detail = rejection(result)
    assert "caps.max_weight_per_instrument 0.3 -> 0.4" in detail
    assert "ceiling, must not rise" in detail


def test_a_lowered_ceiling_is_accepted(tmp_path: Path) -> None:
    mandate_file, baseline_file = tighten(
        tmp_path,
        ("max_weight_per_instrument: 0.30", "max_weight_per_instrument: 0.20"),
    )

    result = validate_mandate(mandate_file, baseline_file)

    assert result.ok


def test_a_lowered_floor_is_rejected(tmp_path: Path) -> None:
    """The inversion: a blanket `derived <= baseline` would pass this."""
    mandate_file, baseline_file = tighten(
        tmp_path,
        ("min_effective_positions: 3.0", "min_effective_positions: 2.0"),
    )

    result = validate_mandate(mandate_file, baseline_file)

    assert not result.ok
    detail = rejection(result)
    assert "caps.min_effective_positions 3.0 -> 2.0" in detail
    assert "floor, must not fall" in detail


def test_a_raised_floor_is_accepted(tmp_path: Path) -> None:
    mandate_file, baseline_file = tighten(
        tmp_path,
        ("min_effective_positions: 3.0", "min_effective_positions: 4.0"),
    )

    result = validate_mandate(mandate_file, baseline_file)

    assert result.ok


def test_the_tvl_floor_is_a_floor_despite_where_the_docs_group_it(
    tmp_path: Path,
) -> None:
    """`capabilities.md` lists it under Ceilings for narrative reasons."""
    mandate_file, baseline_file = tighten(
        tmp_path,
        ("min_instrument_tvl_usd: 50_000", "min_instrument_tvl_usd: 10_000"),
    )

    result = validate_mandate(mandate_file, baseline_file)

    assert not result.ok
    assert "floor, must not fall" in rejection(result)


def test_dropping_a_floor_the_baseline_set_is_rejected(tmp_path: Path) -> None:
    """Absent is not unset -- it is unenforced, which is the widest value."""
    mandate_file, baseline_file = tighten(
        tmp_path,
        ("min_effective_positions: 3.0", "# the floor, deleted"),
    )

    result = validate_mandate(mandate_file, baseline_file)

    assert not result.ok
    assert "caps.min_effective_positions 3.0 -> null" in rejection(result)


def test_dropping_a_cap_the_baseline_set_is_rejected(tmp_path: Path) -> None:
    baseline_text = BASELINE_POLICY.read_text(encoding="utf-8").replace(
        "max_weight_per_sector: 1.00", "max_weight_per_sector: 0.60"
    )
    mandate_file, baseline_file = tighten(
        tmp_path,
        ("max_weight_per_sector: 0.60", "# the sector cap, deleted"),
        baseline=baseline_text,
    )

    result = validate_mandate(mandate_file, baseline_file)

    assert not result.ok
    assert "caps.max_weight_per_sector 0.6 -> null" in rejection(result)


def test_adding_a_cap_the_baseline_left_unset_is_accepted(tmp_path: Path) -> None:
    baseline_text = BASELINE_POLICY.read_text(encoding="utf-8").replace(
        "max_weight_per_sector: 1.00", "# no sector cap"
    )
    mandate_file, baseline_file = tighten(
        tmp_path,
        ("# no sector cap", "max_weight_per_sector: 0.60"),
        baseline=baseline_text,
    )

    result = validate_mandate(mandate_file, baseline_file)

    assert result.ok


def test_a_raised_deploy_gate_is_rejected(tmp_path: Path) -> None:
    mandate_file, baseline_file = tighten(
        tmp_path,
        ("max_deploy_per_cycle_usd: 25_000", "max_deploy_per_cycle_usd: 100_000"),
    )

    result = validate_mandate(mandate_file, baseline_file)

    assert not result.ok
    assert "gates.max_deploy_per_cycle_usd" in rejection(result)


def test_narrowing_an_open_allowlist_is_accepted(tmp_path: Path) -> None:
    mandate_file, baseline_file = tighten(
        tmp_path,
        ("protocols: null", "protocols: [morpho]"),
    )

    result = validate_mandate(mandate_file, baseline_file)

    assert result.ok


def test_an_allowlist_that_admits_a_new_name_is_rejected(tmp_path: Path) -> None:
    baseline_text = BASELINE_POLICY.read_text(encoding="utf-8").replace(
        "protocols: null", "protocols: [morpho]"
    )
    mandate_file, baseline_file = tighten(
        tmp_path,
        ("protocols: [morpho]", "protocols: [morpho, aave]"),
        baseline=baseline_text,
    )

    result = validate_mandate(mandate_file, baseline_file)

    assert not result.ok
    detail = rejection(result)
    assert "allowed.protocols admits ['aave']" in detail
    assert "must be a subset" in detail


def test_dropping_an_allowlist_is_rejected(tmp_path: Path) -> None:
    baseline_text = BASELINE_POLICY.read_text(encoding="utf-8").replace(
        "protocols: null", "protocols: [morpho]"
    )
    mandate_file, baseline_file = tighten(
        tmp_path,
        ("protocols: [morpho]", "protocols: null"),
        baseline=baseline_text,
    )

    result = validate_mandate(mandate_file, baseline_file)

    assert not result.ok
    assert "allows everything again" in rejection(result)


def test_leaving_the_stablecoin_gate_is_rejected(tmp_path: Path) -> None:
    mandate_file, baseline_file = tighten(
        tmp_path,
        ("stablecoin_only: true", "stablecoin_only: false"),
    )

    result = validate_mandate(mandate_file, baseline_file)

    assert not result.ok
    assert "allowed.stablecoin_only" in rejection(result)


def test_giving_up_autonomy_is_a_tightening(tmp_path: Path) -> None:
    """The one flag whose restricting value is False."""
    mandate_file, baseline_file = tighten(
        tmp_path,
        ("autonomous_rebalance: true", "autonomous_rebalance: false"),
    )

    result = validate_mandate(mandate_file, baseline_file)

    assert result.ok
    assert "gates.autonomous_rebalance True -> False" in result.checks[-1].detail


def test_taking_autonomy_is_rejected(tmp_path: Path) -> None:
    baseline_text = BASELINE_POLICY.read_text(encoding="utf-8").replace(
        "autonomous_rebalance: true", "autonomous_rebalance: false"
    )
    mandate_file, baseline_file = tighten(
        tmp_path,
        ("autonomous_rebalance: false", "autonomous_rebalance: true"),
        baseline=baseline_text,
    )

    result = validate_mandate(mandate_file, baseline_file)

    assert not result.ok
    assert "gates.autonomous_rebalance" in rejection(result)


def test_requiring_approval_is_a_tightening(tmp_path: Path) -> None:
    mandate_file, baseline_file = tighten(
        tmp_path,
        ("new_instrument_needs_approval: false", "new_instrument_needs_approval: true"),
    )

    result = validate_mandate(mandate_file, baseline_file)

    assert result.ok


def test_changing_the_signer_is_rejected_in_either_direction(tmp_path: Path) -> None:
    """Not a tightening; a derivation that touches the wallet changed subject."""
    mandate_file, baseline_file = tighten(
        tmp_path,
        ("signer: local-eoa", "signer: safe"),
    )

    result = validate_mandate(mandate_file, baseline_file)

    assert not result.ok
    assert "wallet.signer" in rejection(result)


def test_every_loosened_knob_is_reported_not_just_the_first(tmp_path: Path) -> None:
    mandate_file, baseline_file = tighten(
        tmp_path,
        ("max_weight_per_instrument: 0.30", "max_weight_per_instrument: 0.50"),
        ("min_effective_positions: 3.0", "min_effective_positions: 1.0"),
        ("stablecoin_only: true", "stablecoin_only: false"),
    )

    result = validate_mandate(mandate_file, baseline_file)

    detail = rejection(result)
    assert "loosens 3 knob(s)" in detail
    for knob in (
        "caps.max_weight_per_instrument",
        "caps.min_effective_positions",
        "allowed.stablecoin_only",
    ):
        assert knob in detail


def test_a_loosened_policy_is_not_compared_when_the_hash_is_broken(
    tmp_path: Path,
) -> None:
    """Ordering: each check assumes the last passed, so check 3 stops the run."""
    mandate_file, baseline_file = tighten(
        tmp_path,
        ("min_effective_positions: 3.0", "min_effective_positions: 1.0"),
    )
    (tmp_path / "policy-derived.yaml").write_text(
        (tmp_path / "policy-derived.yaml").read_text(encoding="utf-8") + "# edited\n",
        encoding="utf-8",
    )

    result = validate_mandate(mandate_file, baseline_file)

    assert not result.ok
    assert "can_only_tighten" not in {check.name for check in result.checks}


def test_every_policy_knob_has_a_direction() -> None:
    """A knob this table forgets is a knob nothing checks.

    Adding a field to `Policy` without deciding which way it tightens must
    fail here rather than silently pass through check 4 unexamined.
    """
    covered = (
        {knob for knob, _ in CEILINGS}
        | {knob for knob, _ in FLOORS}
        | set(ALLOWLISTS)
        | {knob for knob, _ in FLAGS}
        | set(FIXED)
    )

    expected: set[str] = set()
    for name, field in Policy.model_fields.items():
        annotation = field.annotation
        nested = getattr(annotation, "model_fields", None)
        if nested is None:
            expected.add(name)
            continue
        expected |= {f"{name}.{child}" for child in nested}

    assert covered == expected


def test_a_baseline_that_is_not_a_policy_raises(tmp_path: Path) -> None:
    """Nothing can be compared against it -- that is the caller's mistake."""
    mandate_file = write_pair(tmp_path)
    bad_baseline = tmp_path / "not-a-policy.yaml"
    bad_baseline.write_text("version: 1\ncaps: {}\n", encoding="utf-8")

    with pytest.raises(MandateError, match="baseline is not a valid policy"):
        validate_mandate(mandate_file, bad_baseline)


def test_a_missing_baseline_raises(tmp_path: Path) -> None:
    mandate_file = write_pair(tmp_path)

    with pytest.raises(MandateError, match="baseline policy not found"):
        validate_mandate(mandate_file, tmp_path / "nowhere.yaml")


# --- result shape -----------------------------------------------------------


def test_result_names_the_baseline_it_compared_against(tmp_path: Path) -> None:
    mandate_file = write_pair(tmp_path)

    result = validate_mandate(mandate_file, BASELINE_POLICY)

    assert result.baseline_path == str(BASELINE_POLICY)
    assert "can_only_tighten" in {check.name for check in result.checks}
