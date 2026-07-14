from pathlib import Path
from typing import Any

import pytest
import yaml

from open_allocator.core.policy_loader import load_policy
from open_allocator.core.schema import SchemaValidationError, validate

REPO_ROOT = Path(__file__).resolve().parents[1]


def policy_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "version": 1,
        "wallet": {
            "mode": "self-custody",
            "signer": "local-eoa",
        },
        "allowed": {
            "protocols": None,
            "chains": None,
            "assets": ["USDC", "USDT", "DAI"],
            "curators": None,
        },
        "caps": {
            "max_weight_per_instrument": 0.30,
            "max_weight_per_protocol": 0.50,
            "max_weight_per_curator": 0.40,
            "max_weight_per_chain": 0.70,
            "min_instrument_tvl_usd": 5_000_000,
            "max_reward_dependence": 0.50,
        },
        "gates": {
            "new_instrument_needs_approval": True,
            "autonomous_rebalance": False,
            "max_deploy_per_cycle_usd": 25_000,
        },
    }
    data.update(overrides)
    return data


def write_policy(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_root_policy_example_loads_and_validates() -> None:
    policy = load_policy(REPO_ROOT / "policy.yaml")

    assert policy.wallet.mode == "self-custody"
    assert policy.wallet.signer == "local-eoa"
    assert policy.allowed.stablecoin_only is True
    assert policy.allowed.asset_categories is None
    assert policy.allowed.assets is None
    validate(policy.model_dump(mode="json"), "policy")


def test_null_allowlists_parse_as_all(tmp_path: Path) -> None:
    policy = load_policy(write_policy(tmp_path, policy_data()))

    assert policy.allowed.protocols is None
    assert policy.allowed.chains is None
    assert policy.allowed.curators is None


def test_omitted_allowlists_parse_as_all(tmp_path: Path) -> None:
    data = policy_data(allowed={"assets": ["USDC"]})

    policy = load_policy(write_policy(tmp_path, data))

    assert policy.allowed.protocols is None
    assert policy.allowed.chains is None
    assert policy.allowed.assets == ("USDC",)
    assert policy.allowed.curators is None


def test_explicit_allowlists_parse_as_is(tmp_path: Path) -> None:
    data = policy_data(
        allowed={
            "protocols": ["morpho", "aave"],
            "chains": [8453, 42161],
            "assets": ["USDC", "DAI"],
            "curators": ["curator-a", "curator-b"],
        }
    )

    policy = load_policy(write_policy(tmp_path, data))

    assert policy.allowed.protocols == ("morpho", "aave")
    assert policy.allowed.chains == (8453, 42161)
    assert policy.allowed.assets == ("USDC", "DAI")
    assert policy.allowed.curators == ("curator-a", "curator-b")


@pytest.mark.parametrize(
    ("mutate", "expected_path", "expected_message"),
    [
        (
            lambda data: data["wallet"].__setitem__("address", "0x0"),
            "$.wallet.address",
            "unexpected property",
        ),
        (
            lambda data: data["wallet"].__setitem__("mode", 1),
            "$.wallet.mode",
            "is not of type 'string'",
        ),
        (
            lambda data: data["caps"].__setitem__("max_weight_per_chain", 1.01),
            "$.caps.max_weight_per_chain",
            "maximum of 1",
        ),
    ],
)
def test_invalid_policy_yaml_produces_clear_schema_errors(
    tmp_path: Path,
    mutate: Any,
    expected_path: str,
    expected_message: str,
) -> None:
    data = policy_data()
    mutate(data)

    with pytest.raises(SchemaValidationError) as error:
        load_policy(write_policy(tmp_path, data))

    assert expected_path in error.value.paths
    assert expected_path in str(error.value)
    assert expected_message in str(error.value)


def test_policy_reserialized_validates(tmp_path: Path) -> None:
    policy = load_policy(write_policy(tmp_path, policy_data()))

    validate(policy.model_dump(mode="json"), "policy")
