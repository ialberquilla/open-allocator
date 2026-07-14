import json
from collections.abc import Iterator

import pytest

from open_allocator.core.schema import (
    SCHEMA_DIR,
    SchemaNotFoundError,
    SchemaValidationError,
    validate,
)
from open_allocator.core.types import (
    Allocation,
    AllocationLeg,
    FactorScore,
    Policy,
    PolicyAllowed,
    PolicyCaps,
    PolicyGates,
    PolicyWallet,
    TxPlan,
    TxStep,
    Unknown,
    VaultScore,
)


def sample_vault_score() -> VaultScore:
    return VaultScore(
        instrument_id="morpho-base-usdc-1",
        score=0.6,
        factors={
            "tvl": FactorScore(
                raw_input=7_500_000,
                normalized_value=0.8,
                weight=2,
                unknown=False,
            ),
            "reward_dependence": FactorScore(
                raw_input=0.15,
                normalized_value=0.2,
                weight=1,
                unknown=False,
            ),
            "oracle": FactorScore(
                raw_input=Unknown,
                normalized_value=None,
                weight=1,
                unknown=True,
            ),
        },
    )


def sample_allocation() -> Allocation:
    return Allocation(
        legs=(
            AllocationLeg(
                instrument_id="morpho-base-usdc-1",
                weight=1,
                usd=1_000,
            ),
        ),
        total_usd=1_000,
        metadata={"risk": "balanced"},
    )


def sample_tx_plan() -> TxPlan:
    return TxPlan(
        steps=(
            TxStep(
                to="0x0000000000000000000000000000000000000001",
                data="0x1234",
                value=0,
                chain_id=8453,
                kind="approve",
            ),
            TxStep(
                to="0x0000000000000000000000000000000000000002",
                data="0xabcd",
                value=0,
                chain_id=8453,
                kind="buy",
            ),
        ),
        summary="Approve then buy morpho-base-usdc-1",
    )


def sample_policy() -> Policy:
    return Policy(
        wallet=PolicyWallet(mode="self-custody", signer="local-eoa"),
        allowed=PolicyAllowed(
            protocols=None,
            chains=None,
            assets=("USDC", "USDT", "DAI"),
            curators=None,
        ),
        caps=PolicyCaps(
            max_weight_per_instrument=0.30,
            max_weight_per_protocol=0.50,
            max_weight_per_curator=0.40,
            max_weight_per_chain=0.70,
            min_instrument_tvl_usd=5_000_000,
            max_reward_dependence=0.50,
        ),
        gates=PolicyGates(
            new_instrument_needs_approval=True,
            autonomous_rebalance=False,
            max_deploy_per_cycle_usd=25_000,
        ),
    )


SAMPLES = {
    "policy": sample_policy,
    "vault-score": sample_vault_score,
    "allocation": sample_allocation,
    "tx-plan": sample_tx_plan,
}

MODEL_BY_SCHEMA_TITLE = {
    "Allocation": Allocation,
    "AllocationLeg": AllocationLeg,
    "FactorScore": FactorScore,
    "Policy": Policy,
    "PolicyAllowed": PolicyAllowed,
    "PolicyCaps": PolicyCaps,
    "PolicyGates": PolicyGates,
    "PolicyWallet": PolicyWallet,
    "TxPlan": TxPlan,
    "TxStep": TxStep,
    "VaultScore": VaultScore,
}


@pytest.mark.parametrize("schema_name", sorted(SAMPLES))
def test_valid_model_dumps_pass_schema(schema_name: str) -> None:
    obj = SAMPLES[schema_name]().model_dump()

    assert validate(obj, schema_name) == obj


@pytest.mark.parametrize(
    ("schema_name", "mutate", "expected_path"),
    [
        (
            "policy",
            lambda data: data["wallet"].pop("signer"),
            "$.wallet.signer",
        ),
        (
            "vault-score",
            lambda data: data.__setitem__("score", 2),
            "$.score",
        ),
        (
            "allocation",
            lambda data: data["legs"][0].__setitem__("weight", 2),
            "$.legs[0].weight",
        ),
        (
            "tx-plan",
            lambda data: data["steps"][0].__setitem__("kind", "hold"),
            "$.steps[0].kind",
        ),
    ],
)
def test_malformed_artifacts_fail_with_offending_path(
    schema_name: str,
    mutate: object,
    expected_path: str,
) -> None:
    data = json.loads(json.dumps(SAMPLES[schema_name]().model_dump()))
    mutate(data)

    with pytest.raises(SchemaValidationError) as error:
        validate(data, schema_name)

    assert expected_path in error.value.paths
    assert expected_path in str(error.value)


def test_validation_error_lists_every_violation_path() -> None:
    data = json.loads(json.dumps(sample_policy().model_dump()))
    data["unexpected"] = True
    data["wallet"].pop("signer")
    data["caps"]["max_reward_dependence"] = 2
    data["gates"]["extra"] = "typo"

    with pytest.raises(SchemaValidationError) as error:
        validate(data, "policy")

    assert {
        "$.unexpected",
        "$.wallet.signer",
        "$.caps.max_reward_dependence",
        "$.gates.extra",
    } <= set(error.value.paths)


def test_schema_required_fields_are_model_fields() -> None:
    for schema_path in SCHEMA_DIR.glob("*.schema.json"):
        schema = json.loads(schema_path.read_text())
        for object_schema in object_schemas(schema):
            title = object_schema.get("title")
            model = MODEL_BY_SCHEMA_TITLE.get(title)
            if model is None:
                continue

            assert set(object_schema.get("required", ())) <= set(model.model_fields)


@pytest.mark.parametrize("schema_name", sorted(SAMPLES))
def test_model_serialized_samples_validate(schema_name: str) -> None:
    validate(SAMPLES[schema_name]().model_dump(), schema_name)


def test_unknown_schema_name_errors_clearly() -> None:
    with pytest.raises(SchemaNotFoundError) as error:
        validate({}, "not-real")

    message = str(error.value)
    assert "unknown schema 'not-real'" in message
    assert "allocation" in message
    assert "policy" in message
    assert "tx-plan" in message
    assert "vault-score" in message


def object_schemas(schema: dict[str, object]) -> Iterator[dict[str, object]]:
    if schema.get("type") == "object":
        yield schema

    defs = schema.get("$defs", {})
    if isinstance(defs, dict):
        for subschema in defs.values():
            if isinstance(subschema, dict):
                yield from object_schemas(subschema)

    properties = schema.get("properties", {})
    if isinstance(properties, dict):
        for subschema in properties.values():
            if isinstance(subschema, dict):
                yield from object_schemas(subschema)

    items = schema.get("items")
    if isinstance(items, dict):
        yield from object_schemas(items)

    for keyword in ("anyOf", "oneOf", "allOf"):
        alternatives = schema.get(keyword, ())
        if isinstance(alternatives, list):
            for subschema in alternatives:
                if isinstance(subschema, dict):
                    yield from object_schemas(subschema)
