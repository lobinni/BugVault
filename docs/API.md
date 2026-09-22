# API Reference

All responses are JSON strings. Views return them from `call`; writes return them in the transaction result. Payable methods **never revert** — read `ok`. Non-payable writes revert with `gl.vm.UserError` on rule violations.

## Current deployment compatibility

The current source layout (including `Claim.rejected_epoch`) is deployed as BugVault v2 at
`0xD57A790cDc4A6a456A2C064D80153B0cb679d724`. Confirm
`get_config().challenge_window_seconds == 259200` before assuming an address implements this API.
The old v1 address does not implement the protected post-rejection challenge window and is
superseded. See `DEPLOYMENTS.md` for wiring and verification commands.

## BugVault (`contracts/bug_vault.py`)

### Payable writes

| Method | Value | Rules | Returns |
| --- | --- | --- | --- |
| `create_bounty(title, scope, severity_policy)` | escrow | not paused; value ≤ `MAX_REWARD`; net ≥ `MIN_REWARD`; scope 20–900 chars | `{ok, bounty_id, reward_amount, fee_withheld}` or `{ok:false, reason, refunded}` |
| `dispute_claim(claim_id)` | exactly `DISPUTE_BOND` | caller = claim's researcher; bounty OPEN; claim REJECTED **and within `CHALLENGE_WINDOW_SECONDS` of the rejection** (challenge) — or SUBMITTED past `REVIEW_TIMEOUT_SECONDS` (timeout) | `{ok, verdict: VALID/INVALID/INCONCLUSIVE/RETRY_LATER, ...}` |

### Writes

| Method | Caller | Effect |
| --- | --- | --- |
| `submit_claim(bounty_id, report_url, note)` | researcher (≠ sponsor) | new `SUBMITTED` claim; cap `MAX_CLAIMS_PER_BOUNTY` (8); **one live claim per researcher per bounty** — a `REJECTED` claim never blocks re-entry |
| `approve_claim(claim_id)` | sponsor | payout; bounty `PAID_OUT`; siblings `VOIDED` |
| `reject_claim(claim_id, reason)` | sponsor | claim `REJECTED`; stamps `rejected_epoch`; challengeable for `CHALLENGE_WINDOW_SECONDS` |
| `cancel_bounty(bounty_id)` | sponsor | refund; **reverts while any claim is pending OR any rejection is still challengeable** |
| `reclaim_expired_bounty(bounty_id)` | anyone | refund after `BOUNTY_TTL_SECONDS` (45d); **same challenge-window guard** — TTL never beats an open dispute right |
| `preview_arbitration(claim_id)` | sponsor or researcher | judge under consensus; mutates nothing |
| `set_paused(bool)` | owner | gates `create_bounty` only |
| `set_platform_fee_bps(n)` | owner | ≤ `MAX_FEE_BPS` (800) |
| `set_treasury(addr)` | owner | fee recipient |
| `sweep_fees()` | owner | transfers pending fees; ≥ `SWEEP_DELAY_SECONDS` after last outbound transfer |
| `transfer_ownership(addr)` | owner | — |

### Views

| Method | Returns |
| --- | --- |
| `get_config()` | constants + owner + fee + paused |
| `get_bounty(id)` / `get_claim(id)` | full record or `{ok:false}` — claims carry `rejected_epoch`, `challenge_deadline_epoch`, `challengeable` |
| `get_claims_by_bounty(id, cursor, limit)` | newest-first page, `next_cursor` (-1 = done) |
| `get_bounties_by_sponsor(addr, cursor, limit)` | same paging |
| `get_claims_by_researcher(addr, cursor, limit)` | same paging |
| `list_bounties(cursor, limit, status_filter)` | global newest-first feed, `""` filter = everything |
| `get_reputation(addr)` | `{valid_wins, attempts, win_rate_bps, total_earned}` — computed live |
| `has_credibility(addr, min_valid_wins)` | `bool` — the composability surface |
| `get_platform_stats()` | totals + `total_escrow_locked` + `total_pending_fees` |

### Status & verdict enums

```
bounty:  OPEN · PAID_OUT · CANCELED · RECLAIMED
claim:   SUBMITTED · APPROVED · REJECTED · RESOLVED_VALID · RESOLVED_INVALID · VOIDED
verdict: VALID · INVALID · INCONCLUSIVE   (+ RETRY_LATER responses on transient failure)
severity: NONE · LOW · MEDIUM · HIGH · CRITICAL
```

### Constants

| Name | Value | Meaning |
| --- | --- | --- |
| `DISPUTE_BOND` | 0.02 GEN | arbitration bond |
| `MIN_REWARD` / `MAX_REWARD` | 0.001 / 100 GEN | escrow bounds |
| `DEFAULT_FEE_BPS` / `MAX_FEE_BPS` | 200 / 800 | fee once at escrow time |
| `REVIEW_TIMEOUT_SECONDS` | 5 days | silent-sponsor escalation |
| `CHALLENGE_WINDOW_SECONDS` | 3 days | a rejection stays disputable this long; both refund paths are blocked meanwhile |
| `BOUNTY_TTL_SECONDS` | 45 days | permissionless sponsor refund (never beats an open challenge window) |
| `SWEEP_DELAY_SECONDS` | 1 hour | fee-sweep safety cooldown |
| `RENDER_WAIT_AFTER_LOADED` | 4s | let SPAs paint before judging |

## CredPass (`contracts/cred_pass.py`)

Holds no funds — write methods revert freely.

| Method | Caller | Notes |
| --- | --- | --- |
| `create_program(title, tier)` | anyone (becomes admin) | snapshots tier→`min_valid_wins` at creation |
| `enroll(program_id)` | the researcher themself | live `has_credibility` read; reverts if under the bar |
| `preview_enrollment(program_id, addr)` | anyone | never reverts; explains |
| `set_program_requirement(id, tier)` | program admin | affects future enrollments only |
| `set_program_active(id, bool)` | program admin | — |
| `set_tier_threshold(tier, n)` | owner | tiers: APPRENTICE 0 · PROVEN 1 · TRUSTED 5 · ELITE 15 |
| `set_oracle(addr)` · `transfer_ownership(addr)` | owner | repoint / hand over |
| `is_eligible(program_id, addr)` | view | live read |
| `get_credibility_snapshot(addr)` | view | passthrough of the vault's reputation |
| `list_programs(cursor, limit)` | view | newest-first feed of all programs |
| `get_program` / `get_enrollees` / `get_enrollments_by_researcher` / `get_tier_thresholds` / `get_stats` | views | listings & counters |
