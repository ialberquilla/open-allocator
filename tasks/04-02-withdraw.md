# 04-02 — Withdraw (ERC-4626 share-balance exit)

**Phase:** 4 — Rebalance & exit
**Depends on:** 04-00, 03-02
**Status:** done

## Goal
Exit a position back to USDC correctly — selling the **yield-token share amount**, never a USD figure.

## Scope / Deliverables
- `src/open_allocator/core/withdraw.py`:
  - `withdraw(client, signer, position, policy, amount=None, confirm=False)`:
    - full exit → sell the entire **share** balance from `positions` (04-00);
    - partial exit → convert requested USD to a share amount using current share price, then sell shares.
  - `POST /transactions/sell` uses `yieldTokenAmount` (shares); execute via 03-02.
- `withdraw --position <id> [--amount <usd>] --confirm` CLI; result includes realized USDC.

## Tests
- Full exit sells the exact share balance (assert shares, not USD, sent to the builder).
- Partial exit converts USD→shares correctly; rounding leaves no over-sell.
- A USD-denominated sell path does **not** exist (regression guard against the classic bug).
- Confirm-gate enforced.

## Acceptance criteria
- [x] Exits are share-denominated; no dust/over-sell from USD conversion.
- [x] `withdraw` composes with `positions` (04-00) output.

## References
plan_allocator.md § DeFi Reality Checks (ERC-4626 share balance), § Gotchas
