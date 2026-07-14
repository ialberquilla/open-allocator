# Safe Guard Policy Notes

`SafeSigner` is a signer swap: 1Tx still builds the original `{to,data,value,chainId}` step, and Safe mode wraps that exact step as a Safe multisig transaction proposal.

## Python Guard Helper

`open_allocator.exec.safe_signer.SafeGuardPolicy` is a testable mirror of the intended on-chain boundary. It rejects a `TxStep` when:

- `policy.allowed.chains` is set and `tx.chain_id` is outside that allowlist.
- `allowed_targets` is set and `tx.to` is outside that module/adapter target allowlist.

This helper is not a substitute for on-chain enforcement. It is intentionally small so tests can verify the policy shape that a Safe guard/module must enforce.

## On-Chain Guard/Module Design

A production Safe deployment should install a Safe guard or module that enforces the same block-only policy before execution:

- Chain boundary: deploy the guard/module only on the configured Safe chain, and reject transactions whose expected domain chain differs from the Safe chain.
- Target boundary: allow only reviewed 1Tx router/adapter targets or project-approved execution modules. Do not allow arbitrary `to` targets.
- Calldata boundary: for each approved target, decode the selector and critical arguments needed to prove the action remains within the allocator policy. Unknown selectors should reject by default.
- Value boundary: reject unexpected native-token value unless the policy explicitly allows that action.
- Upgrade boundary: Safe owners should require the same multisig threshold to update guard/module policy, and policy updates should be checkpointed in repo config.

The Python signer remains proposal-only. It does not bypass Safe threshold collection, and it only executes through an adapter that can prove the Safe transaction has reached threshold.
