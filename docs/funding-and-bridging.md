# Funding & Cross-Chain Bridging Model

How USDC, gas, and cross-chain routing actually work when the allocator executes
through 1Tx. Read this before announcing an execution or reasoning about how much
a wallet needs and where.

## The one-sentence model

A leg's **destination chain is encoded in its `instrumentId`**; the allocator
**sources USDC from whichever chain the wallet is actually funded on** and 1Tx
(`SwapDepositRouter` + CCTP) **bridges to the destination automatically** when
they differ. The wallet only needs native gas on the chain it *signs* on — the
source chain — not on every destination.

## Instrument IDs encode the destination chain

From `1tx-contracts` `InstrumentIdLib`:

```
instrumentId = [ chainId : top 32 bits ][ hash(executionAddress, marketId) : 224 bits ]
```

The first 4 bytes (8 hex digits after `0x`) are the destination chain id:

| instrumentId prefix | chainId | Chain |
| --- | --- | --- |
| `0x00002105…` | 8453 | Base |
| `0x0000a4b1…` | 42161 | Arbitrum One |
| `0x00000082…` | 130 | Unichain |

Decode it directly: `chain_id = int(instrument_id[2:10], 16)`. The `Vault.chain_id`
returned by discovery matches this; never re-derive the universe from it, but it is
a reliable cross-check for which chain a leg lands on.

## Source chain selection (deterministic, balance-aware)

When building each buy, the executor sets 1Tx's `sourceChainId` to the chain the
wallet is funded on, in this precedence (`exec/execute.py:_source_chain_id` →
`_select_source_chain`):

1. **Explicit override** — `source_chain_id` in config (`ONE_TX_SOURCE_CHAIN_ID`)
   or in the allocation's `metadata`. Always wins.
2. **Vault's own chain, if it holds enough USDC** — no bridge, cheapest.
3. **Best-funded chain that can cover the leg** — bridge via CCTP to the vault's
   chain.
4. **Fallback when no single chain can cover the leg** — the vault's chain if it
   holds any USDC, else the best-funded chain; 1Tx then surfaces the shortfall.
5. **No balance info available** — omit `sourceChainId` and let 1Tx auto-select.

> Do **not** pin `sourceChainId` to the vault's own chain unconditionally. If the
> wallet holds no USDC there, 1Tx returns `400 No chain has sufficient USDC
> balance` instead of bridging. This was a real bug; the balance-aware default
> fixes it. Rebalance buys use the same path (`exec/rebalance.py`).

### With the calldata API (`ONE_TX_TRANSACTION_API=calldata`)

1Tx's calldata API builds same-chain bundles and a separate CCTP source burn; it
does not settle a destination. The allocator does that itself (`exec/bridge.py`).

- **Deposits** are built on the vault's chain from the Safe's USDC there when
  that chain holds enough. Otherwise the leg is **bridged**: the source is the
  pinned `source_chain_id` (allocation metadata) when set, else the best-funded
  CCTP chain in 1Tx's `GET /cctp/config` whose USDC covers the leg, read on
  chain, legs taken in order. A leg nothing covers stays on its chain and is
  reported as a funding shortfall. Each chain's deposits and burns go out as one
  Safe operation. A deposit or burn is sized down only to leave the paymaster's
  maximum gas charge in the Safe, and the dry run names every one it sized down.
- **A bridged leg takes several runs of `execute --confirm`.** The burn is sent
  with its source chain's operation. Each later run checks, once, whether Circle
  has attested it (`in_progress` until then), and when it has, requests fresh
  deposit calldata and sends `MessageTransmitterV2.receiveMessage` followed by
  the deposit as **one destination operation**. The deposit is the smaller of
  the leg and the Safe's destination USDC plus the attested mint, less the
  paymaster's maximum charge; Circle's fee comes out of the mint. The leg is
  complete only once that operation is included.
- **Needs** `SIGNER_SUBMISSION=erc4337-paymaster` with a provider that estimates
  operations and bounds their charge (`PAYMASTER_PROVIDER=pimlico`): the
  destination operation pays its gas out of the mint. Any other signer refuses a
  pinned cross-chain leg and never routes one.
- **Rebalances** fund each chain's buys from that chain alone: the Safe's USDC
  there plus the conservative proceeds (`minOut`, else `expectedOut` less
  `slippage_bps`) of the sells on the same chain. Each chain's sells and buys go
  out as one atomic Safe operation, sells first, so no staging or settle wait
  is needed. A buy is sized down only to absorb the gap between a sell's dollar
  value and its conservative proceeds, plus the paymaster's maximum gas charge;
  the dry run names every buy it sized down.
- A rebalance whose buys on one chain need proceeds from sells on another is
  still **refused as cross-chain**. One that needs more money than the chain
  holds and sells is left at full size and reported as a funding shortfall,
  which blocks execution.

#### Bridged legs, step by step

Each leg's progress is a record in the run's idempotency store under
`bridge:leg:<index>:<instrument>`, validated by
[bridge-state.schema.json](../src/open_allocator/schemas/bridge-state.schema.json)
and copied into every `execute` report's `bridges`:

| State | Meaning | A rerun |
| --- | --- | --- |
| `bridge_planned` | Burn built, not yet sent | Plans the leg afresh |
| `source_submitted` | Burn operation sent | Looks the operation up; never resends it |
| `awaiting_attestation` | Burn found in its transaction's `MessageSent` log | Asks Circle once |
| `destination_ready` | Attestation checked against the burn | Builds and sends receive + deposit |
| `destination_submitted` | Destination operation sent | Reconciles it; never resends it |
| `completed` | Destination included, or its nonce already used | Nothing |
| `failed` | Burn or attestation did not match; or the burn reverted | Nothing, unless the burn reverted, which replans |

What is checked, and what happens when something goes wrong:

- **Before the burn is sent**, its calldata is decoded: the approval, amount,
  token, `mintRecipient` and `destinationCaller` (both the Safe), destination
  domain, `maxFee`, and finality must match 1Tx's response, and the messenger
  and domains must match 1Tx's CCTP configuration.
- **The burn is identified** from the source receipt's `MessageSent` logs,
  emitted by the configured MessageTransmitter and sent by the Safe, in call
  order. Other accounts' burns in the same bundler transaction are ignored.
- **An attestation is used only** when Circle's message is the source message
  with only nonce, executed finality, executed fee, and expiration filled in,
  and every field matches the burn, the executed fee is at most `maxFee`, and
  the executed finality meets the requested one. A mismatch fails the leg; it
  is never redeemed automatically.
- **A destination deposit that reverts** reverts `receiveMessage` with it, so
  the CCTP nonce stays unused; the leg stays `destination_ready`, the report is
  `failed` with the reason, and a rerun rebuilds with fresh calldata.
- **An expired attestation** is re-attested through Circle's
  `POST /v2/reattest/{nonce}`; nothing is burned again.
- **A nonce already used** on the destination completes the leg without a
  second redemption, and the report says how much USDC the Safe holds there, so
  `positions` can confirm whether it was deposited.

Circle is `CIRCLE_IRIS_API_URL` (production by default); each check is bounded
by `CIRCLE_HTTP_TIMEOUT_SECONDS` and `CIRCLE_HTTP_MAX_RETRIES`, not by the
attestation wait.

#### Moving USDC without depositing: `bridge`

`bridge --from <chain id> --to <chain id> --amount <usdc> [--confirm]` moves the
Safe's own USDC between two CCTP chains and deposits nothing. It exists for what
`execute` cannot route: a loop is built same-chain only, so its equity must
already be USDC in the Safe on the loop's chain.

It is the bridged leg above less its deposit. The burn comes from 1Tx's
`/bridge/calldata`, is checked the same way, and is sized down (never up) to what
the source chain holds less the paymaster's maximum charge. At
`destination_ready` the destination operation carries `receiveMessage` alone, and
the paymaster's charge comes out of the mint. The record is the same
`BridgeState` with `deposit: false`, kept in a scope derived from the Safe, both
chains, the amount and `--ref`:

- Rerun the **same** `bridge --confirm` until its `bridges` state is
  `completed`; it resumes and never burns twice. A dry run reports a transfer
  under way instead of planning another.
- Once completed, the same arguments do nothing; pass `--ref <label>` to move
  the same amount again.
- `--confirm` refuses to burn without an idempotency store, because a burn with
  no record could not be redeemed.
- Fast versus standard transfer follows `ONE_TX_FAST_TRANSFER`, as for `execute`.

## What a wallet actually needs

For a normal (`local-eoa`) self-custody wallet:

- **USDC on one chain is enough.** You do not need USDC pre-positioned on every
  chain your allocation touches — CCTP bridges from the source chain.
- **Native gas is per-chain and only on chains you sign on.** With an EOA every
  transaction is signed and broadcast on its own chain, so the wallet needs gas
  on:
  - the **source chain(s)** for deposits (where the buy/approve txs execute), and
  - the **position's own chain** for exits — `sell`/`withdraw` are share-
    denominated on the chain the position lives on, so those sign there.
- **Size the deploy to the funded chain's balance.** Legs draw down the same
  source-chain USDC in sequence. `build-tx` validates each leg against the
  current balance, so an `--amount` larger than the funded chain's USDC builds a
  plan but fails partway through execution as that balance drains.

`wallet-status` reports USDC and native-gas readiness per chain; treat a chain as
executable only when both are present.

## Announce this before executing

A complete execution announcement (see [AGENT_GUIDE.md](../AGENT_GUIDE.md)) must
name the **source chain(s)** the USDC comes from, the **destination chain(s)** the
instruments live on, whether any leg **bridges**, and the **native-gas assets**
required on each chain that will be signed. On the legacy API a bridged leg is handed
to 1Tx once its source-chain transaction lands; 1Tx settles the destination mint.
On the calldata API the allocator settles it: announce that the leg bridges, from
and to which chain, that its deposit is built only after Circle attests and is
sized to the mint less Circle's fee and the destination paymaster charge, and that
`execute --confirm` must be rerun until the leg's `bridges` state is `completed`.
Either way, checkpoint and resume idempotently, never blind-retry.
