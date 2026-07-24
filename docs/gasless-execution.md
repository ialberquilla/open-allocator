# Gasless Execution (Safe + ERC-4337 + USDC paymaster)

How the allocator signs and pays for transactions when `SIGNER_ACCOUNT=safe` and
`SIGNER_SUBMISSION=erc4337-paymaster`. Read this before changing anything under
`exec/` that touches user operations — most of what follows was learned by
running it against mainnet, not from documentation.

The EOA path (`SIGNER_ACCOUNT=eoa`, `SIGNER_SUBMISSION=rpc`) is unaffected by
everything here: an EOA holds its own native gas and cannot batch.

## The model

The Safe is **counterfactual** (address from `SAFE_OWNERS` + `SAFE_THRESHOLD` +
`SAFE_SALT_NONCE`, the same on every chain the Safe Singleton Factory reaches;
`open-allocator safe-address` prints it) and **deploys itself inside its first user
operation on each chain** via EntryPoint v0.7 `factory`/`factoryData` — no separate
deployment step. Gas is paid in **USDC**, pulled from the Safe by the paymaster in
`postOp`, which runs *after* execution, so an operation that produces USDC can pay
for itself. A plan's steps for one chain ride in **one user operation**, batched
atomically through MultiSendCallOnly.

The narrative — fund one chain, who settles a cross-chain leg, why it must be
batched — is in the README's [Gas in USDC](../README.md#gas-in-usdc-no-native-tokens)
section. What follows is the engineering behind it: what was observed on chain, the
traps, and the limits.

**This only works batched.** Sent one step at a time, the first operation is an
approval that produces no USDC, and `postOp` reverts `AA50 / TransferFromFailed()`
against an account with a zero balance. Splitting a plan into one operation per
step re-breaks it.

## Verified against mainnet

Facts below were observed on chain, not derived from a spec:

- The paymaster checks **neither balance nor allowance during validation**. A
  first operation whose paymaster approval is inside its own batch succeeds, and
  a zero-balance operation reaches `postOp` before failing. Both are load-bearing:
  the approval-in-batch trick and self-funding exits each depend on it.
- A Safe deploy + paymaster approval + token approval + deposit fits in one
  operation, as does deploy + approve + redeem.
- Cross-chain buys land the position at the Safe's address on the destination
  chain **without deploying the Safe there**. The 1Tx CCTP receiver mints to
  itself and calls the router's `buyFor` on behalf of the recipient; ERC-4626
  shares mint to an address with no code. The Safe is only deployed there later,
  by the first operation actually sent *from* it.
- If the destination-side deposit reverts, the receiver transfers plain USDC to
  the recipient instead. A failed cross-chain buy strands value as idle USDC at
  the same address; it is not lost.
- Redeeming the CCTP message on the destination chain is **permissionless**, so a
  transfer that no relayer has completed can be finished by anyone holding the
  message and Circle's attestation.

## Traps

- **Wait for inclusion before the next operation.** Two operations from one
  sender are sequential whether or not the code treats them that way: the second
  reads the nonce and deployment status from chain state that the first has not
  yet changed, re-sends `factory`, and the EntryPoint rejects it with
  `AA10 sender already constructed`. `PimlicoUserOperationAdapter` polls
  `eth_getUserOperationReceipt` for this reason.
- **`pm_getPaymasterStubData` requires the gas-limit fields to exist**, but they
  come from an estimate that needs the stub first. Seed `callGasLimit`,
  `verificationGasLimit` and `preVerificationGas` with `0x0` before the stub call;
  the estimate overwrites them.
- **USDC is a different contract per chain.** The gas token is a per-chain
  registry lookup (`chains.USDC_ADDRESSES`), never one configured address —
  see [funding-and-bridging.md](funding-and-bridging.md).
- **Pimlico configures no URLs.** Its endpoint embeds the chain id and is derived
  from `PIMLICO_API_KEY`; requiring a bundler URL, paymaster URL, account address
  or EntryPoint from it makes the provider unreachable. Only `generic-http` needs
  those.
- **Sign last.** The SafeOp hash commits to every field except the signature, so a
  re-estimate or a fresh paymaster quote after signing silently invalidates it.
- **Estimate with a stub signature, not `"0x"`.** The bundler prices the
  signature's length; an empty one underprices verification and the real
  operation then fails `AA23`.
- **The owner list is never sorted; signatures always are.** Owner order feeds
  `setup()` and therefore the CREATE2 salt, so reordering moves the Safe — away
  from any funds already sent to it. Signatures must be sorted by signer address
  ascending or `Safe.checkSignatures` reverts `GS026`.
- **EntryPoint is v0.7, not v0.8.** `Safe4337Module` pins it in an immutable
  constructor argument.
- **Never log a Pimlico URL** — the API key rides in its query string.

## Known limits

- **Dust cannot self-fund.** If an exit's proceeds are worth less than its gas,
  `postOp` reverts and the whole batch reverts with it. Atomic, so nothing is
  half-done, but the position stays put.
- **One operation belongs to one chain.** A plan spanning chains is still one
  operation per chain; only contiguous same-chain runs merge.
- **An N-of-M Safe cannot use this path.** A user operation is signed in one
  shot, with no propose→co-sign round trip, so it needs threshold-many keys
  present. Use `safe` + `rpc` submission to collect signatures instead.
- **The owner key must be on disk.** `SIGNER_OWNER=remote` is not wired for
  `erc4337-paymaster`.
