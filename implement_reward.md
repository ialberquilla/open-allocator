# Reward-aware APY implementation and production rollout

## Objective

Make every consumer distinguish between:

- `apyBase`: yield that accrues into the yield token/share price;
- `apyReward`: incentives paid separately and requiring a claim;
- `currentApy`: advertised total APY (`apyBase + apyReward` when both are known);
- claimable rewards: wallet-specific assets available through `GET /rewards`.

The rollout must preserve existing API consumers, avoid changing allocation
semantics silently, and never claim or swap rewards without the same explicit
transaction confirmation used for deposits, rebalances, and withdrawals.

## Implementation status — 2026-09-09

- Pond3r PR #340 is merged. A read-only production audit found 62 instruments:
  all 62 expose `apyBase`, 52 expose both split fields, 10 have an unknown
  `apyReward`, and 13 report a positive reward APY. No positive-reward
  instrument was missing `rewardTokens`. The maximum observed difference
  between `currentApy` and `apyBase + apyReward` was `0.0001` percentage points.
- Darex PR #342 (`a89c30b`) adds
  `GET /positions?address=<address>&chainId=<id>`, retains the legacy
  `POST /positions` route for compatibility, marks POST deprecated in Swagger,
  converts the query `chainId` to a number, and corrects APY Swagger examples
  to use percentage points. The positions API reference now recommends GET.
- OpenAllocator PR #41 (`091d647`) changes its positions client to GET and sends
  `address` and optional `chainId` as query parameters.
- Deploy Darex PR #342 before merging or deploying OpenAllocator PR #41. The
  OpenAllocator unit tests exercise the new contract, but its live positions
  call will fail until the GET route is deployed.
- The production-wallet `/rewards` smoke check remains pending because the local
  environment provides API credentials but no configured public wallet address.
- A control-address production probe on 2026-09-09 confirmed the deployed
  `/rewards` response shape: top-level `wallet`, `rewards`, `errors`, and numeric
  `expiresAt`; each reward contains `provider`, `chainId`, `rewardToken`, raw
  integer-string `claimableAmount` and `pendingAmount`, `claim`, and `swap`.
  `rewardToken` supplies `address`, `chainId`, `decimals`, and `symbol`.
- A probe for `0x0000000000000000000000000000000000000001` unexpectedly returned
  seven non-zero claimable rewards and claim calldata, while the adjacent
  address ending in `2` returned none. The API echoed both requested wallets
  and produced wallet-specific results, so this is not obviously a shared-cache
  response. Darex must explain or fix this result before claim execution is
  enabled; arbitrary dummy addresses are not valid zero-balance fixtures.

## Current production exposure

The read-only production snapshot from 2026-09-09 (run 100) contains seven
positions worth $102.75. Four are reward-bearing according to their live yield
feeds: Tokemak baseUSD, Morpho Hyperithm USDC Apex, Morpho August USDC V2, and
Euler eAUSD-16. They represent approximately $63.18, or 61.5% of deployed
capital.

The dashboard currently reports a 7.340% blended headline APY and derives its
`$/year` figures from that total. Current base/reward splits imply that roughly
1.63 percentage points, or about $1.67/year at the current book size, require
separate reward collection. This estimate changes with the campaigns and must
not be persisted as a forecast.

This is an existing semantic/accounting issue, not a breaking change introduced
by Pond3r PR #340. The PR makes the information needed to correct it available.

## Desired semantics

Use these meanings consistently across all repositories:

| Field | Meaning | Portfolio treatment |
| --- | --- | --- |
| `apyBase` | Organic yield reflected in the yield-token share price | Default input for accrued-income and net-return estimates |
| `apyReward` | Separately distributed incentive yield | Conditional upside; include only in advertised totals and reward-risk analysis |
| `currentApy` | Headline total advertised by the source | Descriptive comparison field, never silently presented as automatically accruing |
| `rewardTokens` | Tokens in which `apyReward` is paid | Disclosure and claim routing metadata |
| claimable amount | Wallet-specific amount executable now | Balance-like contingent asset; do not count as received cash until claimed |
| pending amount | Reward not yet committed/claimable | Disclosure only; do not include in NAV |

When the split is unavailable:

- retain `currentApy` for backward-compatible descriptive output;
- represent `apyBase` and `apyReward` as unknown, not zero;
- do not infer that the whole headline APY is base yield;
- surface a warning anywhere an accrued-income estimate cannot be produced
  honestly.

Do not rewrite historical observations. A historical `apyReward = null` means
unknown, while `apyReward = 0` means the source explicitly reported no reward.

## Safe rollout order

The correct order is:

1. Verify the merged additive Darex/1Tx API change in production.
2. Update OpenAllocator against the deployed API, release it under a new immutable tag.
3. Update `agent-showcase` both directly and by pinning the new OpenAllocator tag.
4. Deploy the reward-aware read/display path first.
5. Add claim transaction execution only as a separate, later rollout.

Do not update the `agent-showcase` OpenAllocator pin before the new tag exists,
and do not make either downstream consumer require the new fields before the
production API has been verified.

---

## Phase 1 — Darex / Pond3r API

Repository: `../darex` (`Pond3rxyz/pond3r-hook`)

### 1.1 PR #340 additive contract (merged)

Keep all existing response fields and endpoint behavior intact:

- retain `currentApy` with its existing total/headline meaning;
- add nullable/optional `apyBase`, `apyReward`, and `rewardTokens` to instrument
  responses;
- retain historical `apy`, `apyBase`, and `apyReward` on metric points;
- add `GET /rewards` without altering `/positions`, transaction builders, or
  existing execution endpoints;
- continue returning instruments when the split is unavailable;
- return partial reward results plus explicit per-chain errors rather than
  failing the entire request when one provider/chain fails.

### 1.2 Contract tests required before merge

Add or retain tests proving:

- an old instrument payload still has the same `currentApy` value;
- missing `apyBase`/`apyReward` is serialized as absent or null according to the
  declared API contract, never coerced to zero;
- `currentApy = apyBase + apyReward` within a documented numeric tolerance when
  all three are known;
- `rewardTokens` is empty only when there is no known current token, not because
  a relation failed to load;
- Aave, Neverland, Merkl, Morpho, Euler, Tokemak, and a no-reward vault are
  covered by fixtures;
- Aave history remains unknown when the upstream source has no historical
  incentive series;
- `GET /rewards` distinguishes claimable and pending amounts;
- `GET /rewards` never hides a claimable reward just because no swap route is
  available;
- claim calldata is built for the requested wallet and correct chain;
- pagination/query behavior of `GET /instruments` is unchanged.

### 1.3 Source and unit conventions

API APYs are percentage points (`5.25` means 5.25%), not ratios (`0.0525`).
Production and existing consumers use that convention; `currentApy`, `apyBase`,
and `apyReward` must always use the same unit. Swagger examples must use
percentage points as well.

Check current feeds for:

- reward tokens duplicated across several campaigns;
- ended campaigns leaving stale reward-token rows;
- negative, null, or non-finite values;
- source totals whose rounding differs from base plus reward;
- reward data refreshed at a different time from headline APY.

### 1.4 Merge and deploy Darex first

Merge PR #340 only after its backend tests and existing endpoint contract tests
pass. Deploy Darex/1Tx production before changing downstream requirements.

### 1.5 Production verification gate

Run read-only smoke checks against production:

1. `GET /instruments` still returns the existing pagination shape and all
   previously active instruments.
2. Known reward-bearing instruments return `currentApy`, `apyBase`,
   `apyReward`, and `rewardTokens` in the same APY unit.
3. Known base-only instruments return an explicit zero reward or an honestly
   unknown split, as appropriate.
4. `GET /metrics/bulk` remains compatible.
5. `GET /instruments/{id}/analysis` still returns `rewardSharePct`.
6. `GET /positions?address=<address>&chainId=<id>` returns the production
   wallet's seven positions. The legacy `POST /positions` body form remains
   available for backward compatibility.
7. `GET /rewards?wallet=<address>&chainId=<id>` works independently per chain
   and returns no transactions for zero claimable balances.

For the rewards check, use a fixture wallet whose zero-claim state is known;
do not assume an arbitrary burn, precompile-like, or dummy address has no
provider records. Verify that the response echoes the requested wallet, every
reward and token uses the requested chain when one was supplied, and any claim
calldata is constructed for that wallet. Treat an unexpected reward for a
control address as a failed gate, not useful test data.

The currently deployed rewards wire contract is:

- response: `wallet`, `rewards`, `errors`, and response-level numeric
  `expiresAt`;
- reward: `provider`, `chainId`, `rewardToken`, `claimableAmount`,
  `pendingAmount`, `claim`, and `swap`;
- reward token: `address`, `chainId`, `decimals`, and `symbol`;
- `claimableAmount` and `pendingAmount` are raw integer strings. Consumers must
  normalize them with token decimals using decimal/integer arithmetic, never a
  binary float;
- instrument `rewardTokens` currently contains token addresses, not the richer
  reward-token objects returned by `/rewards`.

Keep the typed shape of `errors` provisional until a real per-chain provider
failure has been captured as a sanitized fixture.

Record one sanitized response for a reward-bearing instrument and one
base-only instrument as downstream fixtures.

### 1.6 Darex rollback

Because the change is additive, rollback should mean reverting the deployed API
revision while leaving nullable reward data in the database. Do not delete
captured split/history data. Downstream consumers must continue to operate with
the fields absent.

---

## Phase 2 — OpenAllocator

Repository: this repository (`../open-allocator`)

Implement this phase as three independently reviewable PRs:

1. **Additive data plumbing.** Add the instrument and vault split fields,
   populate them during discovery, and cover old/nullable/zero/positive
   payloads. Preserve allocation selection, sorting, scoring, drift, cost, and
   simulation behavior exactly.
2. **Explicit APY accounting and artifacts.** Classify every calculation,
   expose advertised and measured-base outputs with coverage, add warnings and
   allocation basis metadata, and change cost and opportunity-drift semantics
   together rather than allowing them to disagree.
3. **Read-only rewards.** Add the typed client, CLI command, schema, and fixtures
   for expiry, partial errors, no-route rewards, raw amounts, and the guarantee
   that displaying calldata can never execute it.

Do not combine any of these with claim execution.

### 2.1 Extend API models without changing behavior first

Add to the `Instrument` client model:

- `apy_base` aliased from `apyBase`;
- `apy_reward` aliased from `apyReward`;
- `reward_tokens` aliased from `rewardTokens`.

The metric model already parses `apyBase` and `apyReward`, and reward dependence
already uses `rewardSharePct` with historical `apyReward / apy` as a fallback.
Preserve that behavior.

Add explicit fields to the `Vault` domain model rather than relying on Pydantic
extra fields:

- `apy` remains headline/advertised APY for backward compatibility;
- `apy_base` is optional/unknown;
- `apy_reward` is optional/unknown;
- `reward_tokens` is an immutable tuple;
- optionally expose a named `accruing_apy` property which returns base APY only
  when it is known.

Do not silently redefine the existing `Vault.apy` field in the same release.
That would reorder allocations, alter drift signals, and change output artifacts
without an explicit migration.

### 2.2 Separate advertised and accruing calculations

Audit every use of `Vault.apy` and classify it:

- discovery sorting and advertised comparisons may use headline APY but must
  name it as advertised;
- projected share-price income, cost breakeven, and net year-one APY should use
  base APY when known;
- reward dependence continues to use `apyReward / currentApy`;
- APY-series volatility and diversification remain based on the historical
  series, with a caveat that Aave reward history may be unavailable;
- opportunity drift should compare base-to-base by default, and may report a
  separate reward-inclusive opportunity;
- strategies with `apy_weight > 0` must make the selected APY basis explicit in
  allocation metadata.

Until this classification is complete, preserve allocation selection and add
warnings rather than partially switching some calculations to base APY.

### 2.3 Artifact/schema changes

Extend JSON artifacts additively:

- vault/list output: `advertised_apy`, `base_apy`, `reward_apy`,
  `reward_tokens`, and `reward_dependence`;
- allocation metadata: `apy_basis` (`advertised`, `base`, or `mixed_unknown`);
- simulation/cost metadata: separate base and advertised blended APY;
- warnings: total weight whose base/reward split is unknown;
- positions: retain upstream `currentApy`, while joining split fields from
  discovery by instrument ID when available.

Never manufacture a whole-book base APY by using `apyBase` for measured vaults
and falling back to `currentApy` for unknown splits. Report advertised blended
APY over the whole book, measured base APY together with
`base_apy_coverage_bps`, and an unavailable whole-book accruing-income or
breakeven result when coverage is incomplete. `mixed_unknown` describes that
incomplete basis; it is not permission to publish a mixed calculation as base
yield.

All new fields should initially be optional so older saved artifacts and tests
remain readable.

### 2.4 Read-only rewards support

Add a typed `GET /rewards` client method and a read-only CLI command such as
`rewards` or `list-rewards`. It should:

- accept an explicit wallet and optional chain;
- emit claimable, pending, token, provider, route status, expiry, and API errors;
- never sign, submit, approve, claim, or swap;
- label amounts in raw units and normalized token units;
- derive normalized amounts from `rewardToken.decimals` with `Decimal` or
  integer arithmetic, never `float`;
- avoid treating pending rewards as NAV;
- preserve claimable rewards even when `swap.status = no-route`.

The client must additionally validate the echoed wallet, chain consistency,
non-negative raw amounts, and response-level expiry. Claim and swap objects are
opaque, untrusted, read-only data in this rollout.

Adding this command requires updating `AGENT_GUIDE.md` command inventory and the
relevant JSON schema/tests.

### 2.5 Claim execution is a separate feature

Do not combine reward-aware display with automatic claiming. A later
`claim-rewards` implementation must:

- build a plan first;
- announce wallet, chain, provider, reward token, raw/normalized amount,
  destination, claim target, approvals, swap venue, minimum output, gas model,
  expiry, and failure modes;
- require explicit confirmation under the repository's execution rules;
- re-fetch short-lived proofs and quotes immediately before execution;
- never execute expired calldata;
- support claim-only when no swap route exists;
- batch same-chain steps where the execution invariant requires it;
- record transaction hashes and settlement separately from advertised reward
  accrual.

### 2.6 OpenAllocator tests

Add regression tests for:

- old instrument payloads without split fields;
- new payloads with all split fields;
- `null` versus zero reward APY;
- reward dependence from analysis and from metrics fallback;
- base/advertised blended calculations;
- unknown split weight and warnings;
- allocation metadata naming its APY basis;
- cost and drift calculations using the declared basis;
- rewards endpoint partial failures, expiry, no-route, and raw units;
- no execution from the read-only rewards command;
- serialization and schema compatibility with old saved artifacts.

Run the complete unit suite and the credential-gated live API audit against the
deployed Darex API.

### 2.7 Release OpenAllocator

After the OpenAllocator PR is merged:

1. bump the package version in both `pyproject.toml` and
   `src/open_allocator/__init__.py`;
2. create a new immutable Git tag (for example `v0.5.0`; use the project's
   actual release policy);
3. push the tag;
4. verify that a clean environment can install the Git dependency by tag;
5. record the resolved commit SHA.

Do not move or reuse an existing tag. `agent-showcase` locks both tag and commit
SHA, so a moved tag creates an unauditable deployment.

---

## Phase 3 — agent-showcase

Repository: `../agent-showcase`

`agent-showcase` has two independent integrations that must both be updated:

1. the Python job consumes OpenAllocator from a pinned Git tag;
2. the TypeScript collector/web app talks to the 1Tx API directly.

Updating only the OpenAllocator version will not update what the dashboard
stores or displays.

### 3.1 Add backward-compatible database columns

Create a new numbered SQL migration. Prefer nullable columns:

- `shelf_snapshot.apy_base`;
- `shelf_snapshot.apy_reward`;
- `shelf_snapshot.reward_tokens` (JSON/text array with an explicit encoding);
- `position_snapshot.apy_base`;
- `position_snapshot.apy_reward`;
- optionally `position_snapshot.reward_tokens` if position-level display needs
  a frozen copy;
- reward snapshot tables for wallet-specific claimable/pending balances if the
  product will retain their history.

Do not rewrite old rows. Their split must remain unknown. The deploy workflow
runs migrations before services/jobs, so code deployed in the same release may
write the new columns after migration succeeds.

If reward snapshots are persisted, use a schema that records provider, chain,
token, amount, claimed amount, pending amount, observed time, expiry, route
status, and source response identity. Never store claim calldata long-term as
though it remains executable after expiry.

### 3.2 Update the direct TypeScript 1Tx client

Add optional/nullable fields to `web/lib/onetx/types.ts`:

- instrument `apyBase`, `apyReward`, and `rewardTokens`;
- typed reward response models;
- no requirement that old API responses contain the new fields.

The current metric-point type already includes `apyBase` and `apyReward`; keep
its null semantics.

### 3.3 Join position snapshots to instrument splits

PR #340 adds the split to instrument responses, not necessarily to
`GET /positions`. During collection, the showcase already reads both the shelf
and positions. Join by `instrumentId` and persist the instrument split alongside
the position snapshot.

Do not assume `position.currentApy` is base APY. It remains the headline total.
If a held instrument is absent from the shelf, persist the position and leave
the split unknown.

### 3.4 Correct app presentation and calculations

Change the Book and home pages so that:

- the primary position table shows Base APY and Reward APY separately;
- Total/Advertised APY remains visible but explicitly named;
- ordinary `$/year` uses base APY when known;
- conditional reward dollars/year are shown separately;
- blended figures show base and advertised totals;
- unknown split weight is visible;
- “Best leg” and “Worst leg” say whether they rank base or advertised APY;
- tooltips explain that reward APY requires a separate claim and may end;
- mobile/narrow layouts retain the distinction rather than dropping it.

Do not label API-history APY as delivered or realised yield. The existing
share-price-derived measurement in `showcase_job.delivered` is the appropriate
source for what the position token actually paid. Keep performance/NAV yield
and advertised API yield visibly distinct.

### 3.5 Reward discovery in production

Initially collect rewards read-only:

- query once per supported chain or use the all-chain endpoint with partial
  errors;
- show claimable versus pending amounts;
- show the token and USD valuation timestamp/source;
- show route availability without implying a swap happened;
- do not add pending rewards to NAV;
- decide explicitly whether claimable rewards enter NAV. A conservative first
  rollout should display them outside NAV until valuation and reconciliation
  rules are tested;
- alert on repeated endpoint errors without failing the daily position snapshot.

The current public wallet lookup confirms that Monad rewards exist for current
Morpho/Euler holdings. The API may aggregate Merkl claims by reward token; keep
campaign breakdown or attribution when available instead of inventing a single
instrument attribution.

### 3.6 Pin the released OpenAllocator version

After the OpenAllocator tag exists:

1. change the Git dependency in `job/pyproject.toml` from the current tag to the
   new tag;
2. regenerate `job/uv.lock` with the normal `uv` workflow;
3. verify the lock resolves to the recorded release commit SHA;
4. build the job Docker image from a clean cache;
5. run Python job tests, database migration tests, TypeScript tests, lint, and
   the derive parity check.

Commit the direct TypeScript/database/UI changes and the OpenAllocator pin in
the same `agent-showcase` PR, unless an earlier compatibility-only PR is useful.
The production behavior should not enter a state where the job and web app use
different APY meanings.

### 3.7 Staging/canary validation

Against a copy or isolated schema containing representative production rows:

- ingest an old API fixture with no split;
- ingest a new reward-bearing fixture;
- ingest a base-only fixture;
- confirm seven current positions remain present and values/shares are
  unchanged;
- confirm headline APY remains numerically unchanged after the schema rollout;
- verify base and reward sums and weighted totals;
- verify old historical pages render null splits without exceptions;
- verify the daily job makes no allocation or transaction decision merely
  because display fields were added;
- verify a rewards API failure does not abort position/NAV ingestion;
- verify the migration is idempotent through the production migration job.

### 3.8 Production deployment sequence

Use the existing `agent-showcase` deployment workflow, which builds one job
image, deploys/runs migrations, and then deploys services/jobs.

Deployment gates:

1. Confirm Darex production smoke checks still pass.
2. Confirm the OpenAllocator tag and resolved SHA.
3. Merge the `agent-showcase` PR only after CI passes.
4. Let the migration job add nullable columns first.
5. Deploy the web/agent services and allocator job from the same tested commit.
6. Trigger or wait for one collection-only run; do not force a rebalance.
7. Compare the new snapshot with the previous snapshot:
   - same wallet;
   - same seven positions unless chain state genuinely changed;
   - same share balances and USD values within ordinary market movement;
   - same total headline APY;
   - expected split on the four known reward-bearing holdings;
   - no database or endpoint errors.
8. Verify `/`, `/book`, `/shelf`, `/history`, `/performance`, and the agent's
   current-position answer.
9. Keep claim execution disabled.

### 3.9 agent-showcase rollback

If the service/UI release fails:

- redeploy the previous immutable image;
- leave nullable database columns in place;
- do not reverse/drop the migration during an incident;
- confirm the old code still reads the unchanged legacy columns;
- keep collection running if position/NAV ingestion is healthy;
- disable only reward discovery if that endpoint is the failing dependency.

Dropping columns is not an operational rollback. It risks losing captured data
and makes the previous image no safer.

---

## Phase 4 — Claim execution rollout (later)

Ship this only after reward-aware read/display behavior has operated correctly
in production for several collection cycles.

### 4.1 Decide ownership

Prefer OpenAllocator as the owner of transaction planning, confirmation,
submission, and checkpointing. `agent-showcase` should invoke the library/job
and render its artifacts rather than independently signing Darex-provided
calldata.

### 4.2 Required controls

- feature flag defaults off;
- read-only discovery remains available while execution is off;
- explicit human confirmation for every claim/swap batch unless a separately
  reviewed autonomous mandate authorizes bounded claiming;
- allowlisted claim distributors and swap routers per chain;
- calldata target validation;
- proof/quote expiry validation;
- minimum output and slippage bound;
- gas/paymaster readiness;
- claim-only fallback when no route exists;
- idempotency/checkpoint key preventing duplicate submission;
- receipt and post-claim balance reconciliation;
- accounting distinction between accrued, claimable, claimed, swapped, and
  settled rewards.

### 4.3 Canary

Use one small claim on one chain first. Announce the exact operation and obtain
confirmation. Verify:

- claim transaction receipt;
- reward-token balance delta;
- optional swap receipt and USDC delta;
- no duplicate claim on retry;
- NAV treatment before and after settlement;
- audit trail in the production database.

Only then enable additional chains/providers.

## Cross-repository acceptance criteria

The rollout is complete when:

- old consumers remain compatible with Darex responses;
- every known held reward-bearing instrument exposes a split or an explicit
  unknown state;
- OpenAllocator names the APY basis used by allocation, drift, simulation, and
  cost calculations;
- `agent-showcase` no longer presents reward-inclusive APY as automatically
  accruing income;
- current production positions and share balances are unchanged by the rollout;
- claimable and pending rewards are visible but not confused with settled cash;
- failures in reward discovery do not stop position/NAV collection;
- no claim or swap can execute without the required plan and confirmation;
- each deployed repository version/commit is recorded and rollback-tested.

## Release checklist

### Darex

- [ ] PR #340 tests and contract tests pass.
- [ ] `currentApy` backward compatibility verified.
- [ ] APY units and null/zero semantics verified.
- [ ] PR merged and production deployed.
- [ ] Production instrument, metrics, analysis, positions, and rewards smoke checks pass.
- [ ] Sanitized downstream fixtures captured.

### OpenAllocator

- [ ] Instrument and Vault split fields implemented.
- [ ] Every APY calculation classified as advertised or accruing.
- [ ] JSON schemas/artifacts remain backward-compatible.
- [ ] Read-only rewards client/command tested.
- [ ] Full unit suite and live API audit pass.
- [ ] Version bumped and immutable Git tag pushed.
- [ ] Clean tagged install resolves to the recorded commit.

### agent-showcase

- [ ] Nullable database migration added and tested.
- [ ] Direct TypeScript 1Tx types updated.
- [ ] Position-to-instrument split join implemented.
- [ ] UI and projected-income semantics corrected.
- [ ] Read-only reward collection degrades independently.
- [ ] OpenAllocator dependency and lockfile updated to the new tag/SHA.
- [ ] Python, TypeScript, migration, lint, build, and parity checks pass.
- [ ] Migration deploys before new writers.
- [ ] First production run reconciles with the previous seven-position book.
- [ ] Claim execution remains disabled.

### Later claim rollout

- [ ] Feature flag and target allowlists reviewed.
- [ ] Plan/confirmation/checkpoint flow implemented.
- [ ] Expiry, slippage, gas, idempotency, and reconciliation tested.
- [ ] One-chain canary confirmed and audited.
- [ ] Gradual provider/chain rollout completed.

## Sources and implementation anchors

- Darex reward API and APY split: Pond3rxyz/pond3r-hook PR #340,
  <https://github.com/Pond3rxyz/pond3r-hook/pull/340>.
- OpenAllocator API models: `src/open_allocator/exec/client.py`.
- OpenAllocator discovery semantics: `src/open_allocator/core/universe.py`.
- OpenAllocator reward-dependence calculation:
  `src/open_allocator/core/metrics.py`.
- OpenAllocator execution contract: `AGENT_GUIDE.md`.
- Showcase direct API types: `../agent-showcase/web/lib/onetx/types.ts`.
- Showcase shelf derivation: `../agent-showcase/web/lib/derive/shelf.ts`.
- Showcase position persistence: `../agent-showcase/web/lib/ingest/writers.ts`.
- Showcase displayed APY/income calculation: `../agent-showcase/web/lib/book.ts`.
- Showcase OpenAllocator pin: `../agent-showcase/job/pyproject.toml` and
  `../agent-showcase/job/uv.lock`.
- Showcase deploy/migration order: `../agent-showcase/.github/workflows/deploy.yml`.
- Production evidence: `agent-showcase` production PostgreSQL run 100,
  inspected read-only on 2026-09-09; DeFiLlama yield-pool API and Merkl public
  wallet rewards response inspected on the same date.
