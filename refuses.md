# 1Tx calldata API refusals

Found by the Phase 7 live gate on 2026-09-17
(`OPEN_ALLOCATOR_LIVE_CALLDATA_PROBE=1 uv run pytest -m integration tests/test_calldata_probe.py`)
against `https://api.1tx.fi/api/v1`.

The API contract itself passed everywhere 1Tx answered:

- No `executor` was needed.
- Every response passed the strict parse, with scope `protocol_bundle` and engine `wallet_neutral_atomic`.
- Every response matched its request.

Safe wallet gas estimates (UserOperations) passed on Base, Arbitrum and Monad, for both the deployed Safe and a counterfactual one.

What failed is coverage: 1Tx refuses 14 of the 57 active instruments. Each was probed with one whole unit of the instrument's token, first with an account that has no code, then with the deployed Safe. Both accounts got the same answer. A calldata-mode plan that includes any of these fails at dry run.

## Unavailable to the calldata API (501)

`This instrument is unavailable to the calldata API`, for both deposit and withdraw.

| Instrument | Chain | Protocol | Yield token |
| --- | --- | --- | --- |
| `0x000021053a846b64b310324cfd96a29473b19dc05495f37cb6c87b8f3d721228` | Base | Fluid | fUSDC |
| `0x00002105927eaf7d74858d0241fb00e75d8f519093042667cf0df17cbdd7e37e` | Base | Fluid | fGHO |
| `0x000021056b6d09c15812cf4d0b80184c57f1abd1da536becf4f42dd444e01f23` | Base | Fluid | fEURC |
| `0x00002105bbb2bef3f7b15da825cf967932dfff01ed107b8b18ffdd0d90bbf60f` | Base | Morpho | CSUSDC |
| `0x000021057d36355ffddcae0bede6d9c8f4a73b6c2b3e3a66565c7cd350d72f9f` | Base | Morpho | gtUSDCprime |
| `0x000021055b188115404f4be66d6cc3b540d3e9876b67059e1140ffafacf7b446` | Base | Morpho | steakUSDCprime |
| `0x00002105502f8247374b4bee34e398712f3df7b74c545f3f7b9aec39884ab022` | Base | Morpho | sparkUSDC |
| `0x00002105a9bdcb222682fd224470c8ed2ae152dbc308a4154c5a332e0d94dccb` | Base | Morpho | steakUSDC |
| `0x0000210525c637909dd4e86800ee0cf82cbef39b332f124e3578c58c7e6c5a3d` | Base | Morpho | mwcBTC |
| `0x0000a4b15f2a5083c04410a4302b68957f25ff58ab633244750cf29ba2af5c5d` | Arbitrum | Morpho | BBQUSDC |
| `0x0000a4b10b72c929e4226de63c0c29b99d9464f3263b713e814a9d5d3864f518` | Arbitrum | Morpho | GTUSDCC |
| `0x0000a4b1025d5d650f3667db4f963a2fb9d8842f47ed8c056c9a7166b0ba55cd` | Arbitrum | Morpho | HYPERUSDC |

## No swap route (400)

| Instrument | Chain | Protocol | Yield token | Message |
| --- | --- | --- | --- | --- |
| `0x0000008fb4a7350a0ac2ef38919fa70f138ed3da494b3daf01c3da91bef52180` | Monad | Aave | aMonGHO | `No liquid direct Uniswap V3 route is available for this deposit` (and `... for this withdrawal`) |

## Withdraw simulation reverts (400)

Deposits for this instrument are quoted; only exact withdrawals are refused.

| Instrument | Chain | Protocol | Yield token | Message |
| --- | --- | --- | --- | --- |
| `0x0000210590596a84c3defff747434f651a8a01b84bf0ba3b4e5574929e768d53` | Base | Morpho | primeUSDC | `Atomic bundle simulation reverted` (withdraw of 1 USDC, account with no code) |

## Also worth raising with 1Tx

- A Monad Aave withdraw quote reported `simulation.gasUsed = 150000000`, which looks like a cap or fallback rather than a measurement.
