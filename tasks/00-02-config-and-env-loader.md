# 00-02 — Config & env loader

**Phase:** 0 — Foundations
**Depends on:** 00-01
**Status:** done

## Goal
Typed, validated configuration from env, covering 1Tx API access, RPC endpoints, and signer selection —
with dotenvx-friendly at-rest key handling.

## Scope / Deliverables
- `src/open_allocator/exec/config.py` (pydantic-settings `BaseSettings`):
  - `onetx_api_url` (`ONE_TX_API_URL`, required), `onetx_api_key` (`ONE_TX_API_KEY`, required).
  - `slippage_bps` (`ONE_TX_SLIPPAGE_BPS`, default 50), `fast_transfer` (`ONE_TX_FAST_TRANSFER`, default false),
    optional referral (`ONE_TX_REFERRAL_FEE_BPS` ≤ 500 + `ONE_TX_REFERRAL_WALLET`, both-or-neither).
  - `signer_mode` (`local-eoa` | `remote` | `safe`, default `local-eoa`).
  - `private_key` (`ONE_TX_PRIVATE_KEY`, 32-byte 0x hex) — required only when `signer_mode=local-eoa`.
  - RPC config (net-new vs 1tx-skill): `RPC_URL_<chainId>` overrides + a public-RPC default registry
    (see 03-01). Config exposes `rpc_url(chain_id) -> str | None`.
- Use shell-safe `ONE_TX_*` names so either dotenvx or manual exports work.
- **dotenvx note**: values may be dotenvx-encrypted at rest; the process runs under `dotenvx run --`,
  which decrypts into env before Python reads it. Config never decrypts itself; document that at-rest ≠
  hidden-from-agent (see plan § Key management).
- `.env.example` documenting every var.

## Tests
- Required-var missing → clear `ValidationError` (parametrized over `ONE_TX_API_URL`, `ONE_TX_API_KEY`).
- `private_key` validated only in `local-eoa` mode; bad length/format rejected.
- Referral applies only when both bps>0 and wallet set; bps>500 rejected.
- `rpc_url()` returns override when set, else the default registry value, else `None`.
- Booleans parse `true/1/yes` case-insensitively.

## Acceptance criteria
- [x] All 1Tx + RPC + signer vars load and validate with actionable errors.
- [x] No secret is logged or included in `__repr__`.
- [x] `.env.example` complete.

## References
plan_allocator.md § Prerequisites to Run, § Key management; 1tx-skill `src/config.ts` + `.env.example`
