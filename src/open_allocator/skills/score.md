# Score Skill

Use this stage to inspect deterministic vault scores from `open_allocator.core.scoring`. Do not move scoring formulas into prompts; call the CLI or core code and explain the returned factors.

## Runnable Workflow

1. Start from a discovery artifact produced by [discover.md](discover.md).
2. For each candidate instrument, run `open-allocator score-vault --instrument-id <instrument_id>`.
3. Keep the factor table visible: raw input, normalized value, weight, and `unknown` status.
4. Use `open-allocator list-vaults --sort score` only as a ranking aid; score details come from `score-vault`.
5. Summarize why candidates rank high or low using returned factors, not protocol reputation guesses.

## Quality Bar

- Every proposed instrument has a current score artifact.
- Reward dependence, liquidity, TVL, LLTV, APY stability, and unknown factors are called out when present.
- APY is framed as a current/descriptive input, not a forecast.
- Low scores or unknown critical fields are surfaced; do not hide them by averaging prose.

## Relevant CLI Commands

- `open-allocator score-vault --instrument-id <instrument_id>`
- `open-allocator list-vaults --sort score`

## Produced Artifacts

- Vault score JSON per reviewed instrument.
- Candidate ranking summary with score drivers.

## Safety Gates

- Do not score instruments absent from live discovery unless policy/human review explicitly approves first-touch handling.
- Do not override or reweight deterministic scores in the prompt.
- Stop if `score-vault` cannot find an instrument that appears in the candidate set.

## Review Focus

- Scoring factor visibility.
- Reward-dependence and liquidity penalties.
- Unknown values that affect policy or allocation confidence.
- APY-descriptive language.
