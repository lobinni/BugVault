# Release Notes

## v2 — enforced rejection challenge window (2026)

### Deployment

* **BugVault v2:** `0xD57A790cDc4A6a456A2C064D80153B0cb679d724`
* **CredPass:** `0x2c9d68A00f110DCB2f9CBb41b77A748c080cBF39`
* CredPass must report the v2 address from `get_stats().oracle`. If it still reports v1, the
  owner must call `set_oracle(0xD57A790cDc4A6a456A2C064D80153B0cb679d724)`.

### Security fix

The original refund policy treated `REJECTED` as non-blocking. That created a race:

1. The sponsor rejected a claim.
2. The researcher still had a documented right to call `dispute_claim`.
3. The sponsor could call `cancel_bounty`, or anyone could call `reclaim_expired_bounty`, first.
4. The reward left the contract and the later dispute could no longer be funded.

v2 makes the dispute opportunity enforceable on-chain:

* `reject_claim` stamps `Claim.rejected_epoch`.
* `CHALLENGE_WINDOW_SECONDS` is 259,200 seconds (3 days).
* `_challengeable` computes whether the dispute right is still open.
* `_refund_blocker` is shared by **both** refund paths and blocks on either a `SUBMITTED` claim
  or a challengeable `REJECTED` claim.
* `dispute_claim` refuses challenges at or after the deadline and refunds the attached bond.
* `get_claim` exposes `rejected_epoch`, `challenge_deadline_epoch` and `challengeable` for UIs
  and off-chain agents.
* `get_config` exposes `challenge_window_seconds`, which is also the v2 deployment marker.

### Regression coverage

The offline suite now proves all three boundaries:

* `test_cancel_blocked_while_rejection_challengeable`
* `test_reclaim_blocked_while_rejection_challengeable`
* `test_dispute_after_challenge_deadline_refunded`

The deployment-manifest suite additionally prevents v1 or `PENDING_DEPLOYMENT` from becoming a
default again. The live smoke suite checks the unique v2 config marker and fails until CredPass
is wired to v2.

Both payable TypeScript samples now use distinct sponsor and researcher signers. The previous
single-signer shape could only hit the contract's intentional self-claim rejection.

### Migration

Storage changed (`Claim.rejected_epoch`), so v1 could not be upgraded in place. v2 was deployed
at a fresh address. The v1 address
`0xAe717Dd46C96D06cE766d27c04f3e6385563aA3B` is retained in deployment records for provenance
only and must not receive new escrow.

No CredPass redeployment is required: its owner can repoint the oracle through `set_oracle`.

## v1 — historical

Initial competitive bounty escrow and live CredPass composition. Superseded because the
post-rejection dispute opportunity was not protected against sponsor cancellation or expiry
reclaim.