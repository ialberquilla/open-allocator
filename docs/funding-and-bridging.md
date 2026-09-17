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

The calldata path does not bridge yet: every bundle executes on its
instrument's own chain, from the Safe's USDC there.

- **Deposits** are built on the vault's chain, from the Safe's USDC there; a
  leg pinned to another source chain is refused, and USDC held only on another
  chain is not bridged in. Each chain's deposits go out as one Safe operation.
  A deposit is sized down only to leave the paymaster's maximum gas charge in
  the Safe, and the dry run names every deposit it sized down. A chain that
  holds less than its deposits by more than that is left at full size and
  reported as a funding shortfall, which blocks execution.
- **Rebalances** fund each chain's buys from that chain alone: the Safe's USDC
  there plus the conservative proceeds (`minOut`, else `expectedOut` less
  `slippage_bps`) of the sells on the same chain. Each chain's sells and buys go
  out as one atomic Safe operation, sells first, so no staging or settle wait
  is needed. A buy is sized down only to absorb the gap between a sell's dollar
  value and its conservative proceeds, plus the paymaster's maximum gas charge;
  the dry run names every buy it sized down.
- A rebalance whose buys on one chain need proceeds from sells on another is
  **refused as cross-chain** until client-side CCTP lands. One that needs more
  money than the chain holds and sells is left at full size and reported as a
  funding shortfall, which blocks execution.

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
required on each chain that will be signed. A bridged leg is handed to 1Tx once its
source-chain transaction lands; 1Tx settles the destination mint. Confirm the landed
position with `positions` rather than re-running the leg — checkpoint and resume
idempotently, never blind-retry.
