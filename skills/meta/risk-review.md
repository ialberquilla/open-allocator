# Risk Review Meta-Skill

Risk review is an advisory self-review before presenting an allocation, rebalance, or withdrawal. It does not replace deterministic checks and must never become a hidden hard block. Blocking remains with `check-policy`, execution confirmation gates, and the human's decision.

## Scope

Review the artifacts already produced by CLI/code:

- Allocation, simulation, policy result, and score artifacts for allocation/rebalance.
- Positions, rebalance, or withdrawal dry-run reports for position changes.
- Transaction plan and gas/preflight details before execution.

## Required Checks

- Concentration: instrument, protocol, curator, chain, and single-failure exposure.
- Reward dependence: emissions-driven APY and policy caps.
- Liquidity: low liquidity flags, TVL depth, withdrawal messages, and exit size.
- Policy conformance: cite `check-policy` or rebalance policy result; do not reinterpret violations.
- APY-descriptive framing: APY/current yield is historical/current, not promised or predicted.

## Finding Grades

- `critical`: Material risk, missing artifact, policy violation, approval mismatch, or unsafe execution ambiguity. Critical findings are advisory unless they are also deterministic policy failures or missing human confirmation.
- `suggestion`: Improve diversification, explanation, artifact retention, or announcement clarity.
- `nitpick`: Wording, formatting, or non-blocking clarity issue.

## Workflow

1. Run review after `simulate` and `check-policy`, and again after `build-tx` if execution is proposed.
2. Produce findings with grade, artifact reference, reason, and suggested action.
3. If changes are made, rerun affected CLI commands and perform one follow-up review.
4. Stop after at most 2 review rounds. Present remaining advisory risks to the human instead of looping.

## Quality Bar

- Findings cite JSON artifacts or CLI outputs, not intuition.
- The review never invents new allocation, scoring, or policy logic.
- The review clearly distinguishes policy-blocking facts from advisory risk notes.
- APY language remains descriptive in both findings and final presentation.

## Output Shape

Use concise bullets or JSON-like records:

```text
grade: critical|suggestion|nitpick
focus: concentration|reward_dependence|liquidity|policy_conformance|apy_descriptive
artifact: <file or command output>
finding: <specific issue>
action: <rerun command, revise proposal, or disclose to human>
```
