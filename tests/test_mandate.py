from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from open_allocator.core.mandate import (
    MandateError,
    load_mandate,
    policy_digest,
    validate_mandate,
)

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


def test_shipped_mandate_passes_every_implemented_check() -> None:
    """The mandate in the repo root is the worked example; it must stay valid."""
    result = validate_mandate(REPO_ROOT / "mandate.yaml", BASELINE_POLICY)

    assert result.ok
    assert [check.name for check in result.checks] == [
        "mandate_schema",
        "policy_schema",
        "policy_hash",
    ]
    assert all(check.ok for check in result.checks)


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


# --- result shape -----------------------------------------------------------


def test_result_names_the_baseline_it_has_not_yet_compared_against(
    tmp_path: Path,
) -> None:
    """Check 4 is missing; the result must still say which baseline it means."""
    mandate_file = write_pair(tmp_path)

    result = validate_mandate(mandate_file, BASELINE_POLICY)

    assert result.baseline_path == str(BASELINE_POLICY)
    assert "can_only_tighten" not in {check.name for check in result.checks}
