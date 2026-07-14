# Security Policy

`open-allocator` signs and broadcasts real on-chain transactions and handles
wallet credentials. Please treat security issues seriously.

## Reporting a vulnerability

Do **not** open a public GitHub issue for security-sensitive reports.

Email **ivan.alberquilla@gmail.com** with:

- a description of the issue and its impact,
- steps to reproduce (or a proof of concept), and
- the affected version / commit.

I aim to acknowledge reports within 72 hours and will keep you updated on a
fix and disclosure timeline.

## Scope

This is **alpha** software (`v0.1.0`). It is not audited. Use it only with
funds you can afford to lose, and always review proposed transactions before
confirming.

Key handling notes:

- Secrets are read from environment variables only (`.env`, `.env.keys`), which
  are git-ignored — never commit them.
- Policy allowlists and caps are block-only guardrails; they reduce but do not
  eliminate risk.
- APY figures are yield-path only and never account for principal, depeg,
  bridge, or smart-contract-loss risk.
