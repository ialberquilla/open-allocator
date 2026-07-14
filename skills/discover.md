# Discover Skill

Use this stage to establish wallet readiness and the live 1Tx universe before any scoring or allocation. Discovery must start broad; policy can narrow the universe after discovery, but prompts must not hardcode protocol, chain, or instrument lists.

## Runnable Workflow

1. Load repo rules from [../AGENT_GUIDE.md](../AGENT_GUIDE.md) and the active policy file.
2. Run wallet discovery: `open-allocator wallet-status`.
3. Run full universe discovery: `open-allocator list-vaults`.
4. Only after the full run, optionally narrow for inspection with CLI filters such as `open-allocator list-vaults --asset USDC --sort score`.
5. Preserve the raw JSON outputs as artifacts for later scoring, policy checks, and announcements.

## Quality Bar

- The first universe pass is unfiltered except by the CLI's live 1Tx source.
- Wallet address, USDC balances, and native gas readiness are visible before any spend proposal.
- Unknown or missing fields stay unknown; do not infer metrics from protocol names.
- Discovery failures stop the workflow until the 1Tx/API/RPC configuration is fixed.

## Relevant CLI Commands

- `open-allocator wallet-status`
- `open-allocator list-vaults`
- `open-allocator list-vaults --chain <chain_id> --asset <asset> --protocol <protocol> --sort score`

## Produced Artifacts

- Wallet status JSON.
- Live vault universe JSON.
- Optional narrowed candidate list JSON.

## Safety Gates

- Do not proceed to allocation if the wallet cannot be identified.
- Do not proceed to execution planning if native gas is missing on any chain that could be used.
- Treat policy allowlists as narrowing filters, never substitutes for discovery.

## Review Focus

- Dynamic universe coverage.
- Wallet identity and gas readiness.
- Unknown fields and API omissions.
- Candidate narrowing against policy, not against hidden preferences.
