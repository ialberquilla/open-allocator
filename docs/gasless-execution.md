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

## What an operation costs

**The USDC the paymaster charges tracks the native fee closely** — measured
across mainnet operations, the implied token price sits within a few percent of
spot, so there is no hidden markup to hunt for. What the code controls is
therefore not the exchange rate but the two inputs to it:

- **The fee tier.** `pimlico_getUserOperationGasPrice` quotes `slow`,
  `standard` and `fast`; the EntryPoint charges
  `min(maxFeePerGas, baseFee + priority)` and the paymaster converts *that* into
  USDC. `PAYMASTER_FEE_TIER` selects it and **defaults to `standard`** rather
  than the `fast` that used to be hardcoded — a daily rebalancer is not racing
  anyone. ⚠️ **Do not expect much from it:** measured 2026-08-16, `fast` is
  **1.048x** `standard` on both Base and Monad, and `slow` is 0.95x. The tiers
  are 5% apart, not 2x apart. Raise it to `fast` if operations sit unincluded,
  remembering that a pending operation blocks the next one from this Safe.
- **What rides in the operation.** The paymaster approval is sent **once per
  (Safe, token, paymaster)**, not on every operation: the adapter reads the
  allowance first and skips the approval when an unlimited one is already
  standing. Beyond the approve itself, this is what lets a single-action
  operation go out as a direct call instead of a MultiSendCallOnly
  delegatecall it has no use for.

⚠️ **The first operation on a chain still carries the approval, and must.** The
"only works batched" rule above is about that operation, and it is unchanged —
the allowance is read from chain state, so an undeployed Safe (nothing to ask)
and an unreadable RPC (no answer) both fall through to sending the approval. A
redundant approval wastes a few thousand gas; a missing one reverts the
operation after paying for everything up to `postOp`.

📍 **The most an operation can be charged is bounded, not guessed.** Before a
calldata plan is sent, each operation's maximum USDC charge is reserved out of
the Safe's balance alongside what its bundles require (`exec/funding.py`). The
bound (`exec/paymaster_charge.py`) is read off Pimlico's `SingletonPaymasterV7`
`postOp`: every gas limit, plus the contract's 10% unused-execution penalty,
plus `postOpGas`, at `maxFeePerGas`, times `exchangeRate / 1e18`, plus any
constant fee. The rate, `postOpGas`, and fee flags come from the stub
`paymasterData`, which carries the same ERC-20 config the sponsorship does; a
stub that does not parse gives no bound, and the plan says so rather than
reserving a made-up amount. Checked 2026-09-17 against 26 ERC-20 charges on
Base: every one inside the bound (the largest at 52% of it), and every one
within 0.96–1.01x of `actualGasCost × exchangeRate / 1e18`.

Calldata deposits and rebalances fit their deposits around that bound
(`exec/deposit_sizing.py`): when a chain's deposits would leave less USDC than
the operation's maximum charge, they are rebuilt
smaller by the shortfall and the operation is prepared again, at most three
times. A shortfall still standing after that is reported, not guessed around.

📍 **A bridged calldata deposit pays its destination gas out of its own mint.**
The Safe may hold nothing on the destination chain, so the operation that
redeems the CCTP message and deposits is funded by the mint it redeems: the
funding check credits the attested net mint and reserves the bounded charge
after it, and the deposit is rebuilt smaller until the charge fits, at most
three preparations. An operation whose charge the adapter cannot bound is not
submitted — a flat USDC reserve would be a guess — so cross-chain calldata
deposits need an adapter that bounds it (`PAYMASTER_PROVIDER=pimlico`). Unlike
the legacy receiver, nothing mints to a third party first: `receiveMessage`
and the deposit are one Safe operation, so a reverting deposit leaves the CCTP
message unredeemed rather than idle USDC. The destination operation deploys the
Safe there when it is counterfactual.

📍 **A calldata plan carries two gas numbers, and they are different
measurements.** Each bundle's `protocol_gas` is 1Tx's simulation of the bare
calls under its ephemeral, wallet-neutral executor (`simulation.scope =
protocol_bundle`, `engine = wallet_neutral_atomic`). Each entry in a dry run's
`preparations` is this repo's bundler estimate of the real operation — Safe
deployment when counterfactual, the Safe4337Module batch, the paymaster approval
and stub, current nonce and fees — and it is the one that says the operation can
run. Neither is signed or sent; submission estimates again.

📍 **An unfunded Safe is still estimated, against assumed balances.** A Safe that
does not yet hold what its bundles spend — typically a counterfactual Safe before
its first deposit — reverts in the bundler's simulation (`AA50 postOp reverted`
when the gas USDC is missing, the module's `ExecutionFailed` when the calls'
tokens are). Preparation then estimates once more with each bundle's `requires`
(plus gas headroom in USDC) written into the Safe's balance through an
`eth_estimateUserOperationGas` state override, and the entry reports
`assumed_balances` and the original `simulation_revert`. That estimate validates
the envelope; the `funding` rows still report the shortfall, which blocks
execution, and `execute --confirm` never proceeds on an assumed-balance estimate.
The balance slot is found per token by checking candidate storage layouts
against the token's own `balanceOf` in an `eth_call`, so it needs an RPC that
accepts state overrides (the public `mainnet.base.org` does not) and fails, with
the reason in the error, for tokens whose balance is not a plain stored value.

The live gate for all of this is opt-in:
`OPEN_ALLOCATOR_LIVE_CALLDATA_PROBE=1 uv run pytest -m integration
tests/test_calldata_probe.py`. It probes every active instrument for an account
with no code and for the configured Safe (`exec/calldata_probe.py`: request
answered without `executor`, strict contract, bound to the request), then
prepares a deposit for the configured Safe and a counterfactual one on each
paymaster chain.

📍 **Modelled cost is a different number from charged cost, and the model prices
gas in the chain's own token.** `core.costs` estimates what a leg will cost
before it is sent; `exec.gas` reads the prices it needs. A chain whose gas token
has no quote is left unpriced and falls back to a static constant with
`gas_priced_live: false` — never priced at another token's rate, which on a
chain with a cheap native token is wrong by orders of magnitude.

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
