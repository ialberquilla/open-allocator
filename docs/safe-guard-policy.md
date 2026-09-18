# Safe Guard Policy Notes

`SafeSigner` is a signer swap: 1Tx still builds the original `{to,data,value,chainId}` steps, and Safe mode wraps those exact steps as one Safe multisig transaction proposal per chain run. A single step is proposed as a plain call. Several consecutive steps on one chain are proposed together as one `DELEGATECALL` to Safe 1.4.1 `MultiSendCallOnly`, so owners sign and execute them atomically; proposed separately, each would read the same on-chain Safe nonce and at most one could execute. `MultiSendCallOnly` can only emit plain `CALL`s, so a batch cannot delegatecall out of the Safe.

A proposal waits for co-signers with no execution deadline, so the Safe Transaction Service path refuses calldata bundles that carry an expiring quote (`ONE_TX_TRANSACTION_API=calldata`), and refuses to propose to a Safe that is not yet deployed on the chain. Both are reported by the dry run and rejected before anything is proposed.

## Python Guard Helper

`open_allocator.exec.safe_signer.SafeGuardPolicy` is a testable mirror of the intended on-chain boundary. It rejects a `TxStep` when:

- `policy.allowed.chains` is set and `tx.chain_id` is outside that allowlist.
- `allowed_targets` is set and `tx.to` is outside that module/adapter target allowlist.

For a batch it validates every inner step, not the `MultiSendCallOnly` wrapper, which is the same contract for every batch and says nothing about where funds go.

This helper is not a substitute for on-chain enforcement. It is intentionally small so tests can verify the policy shape that a Safe guard/module must enforce.

## On-Chain Guard/Module Design

A production Safe deployment should install a Safe guard or module that enforces the same block-only policy before execution:

- Chain boundary: deploy the guard/module only on the configured Safe chain, and reject transactions whose expected domain chain differs from the Safe chain.
- Target boundary: allow only reviewed 1Tx router/adapter targets or project-approved execution modules. Do not allow arbitrary `to` targets. A batched proposal targets `MultiSendCallOnly` with `DELEGATECALL`, so the guard must decode the packed MultiSend payload and apply this boundary to each inner call rather than allow-listing the wrapper.
- Calldata boundary: for each approved target, decode the selector and critical arguments needed to prove the action remains within the allocator policy. Unknown selectors should reject by default.
- Value boundary: reject unexpected native-token value unless the policy explicitly allows that action.
- Upgrade boundary: Safe owners should require the same multisig threshold to update guard/module policy, and policy updates should be checkpointed in repo config.

The Python signer remains proposal-only. It does not bypass Safe threshold collection, and it only executes through an adapter that can prove the Safe transaction has reached threshold.
