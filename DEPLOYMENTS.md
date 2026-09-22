# Deployments

## Current Studionet deployment

| Contract | Version | Address | Status | Explorer |
| --- | --- | --- | --- | --- |
| BugVault | v2 | `0xD57A790cDc4A6a456A2C064D80153B0cb679d724` | **current — challenge-window fix included** | [open](https://explorer-studio.genlayer.com/address/0xD57A790cDc4A6a456A2C064D80153B0cb679d724) |
| CredPass | current source | `0x2c9d68A00f110DCB2f9CBb41b77A748c080cBF39` | deployed; oracle wiring must be verified | [open](https://explorer-studio.genlayer.com/address/0x2c9d68A00f110DCB2f9CBb41b77A748c080cBF39) |

The v2 vault replaces v1 after review found that a sponsor could reject a claim and refund the
bounty before the researcher invoked arbitration. The corrected deployment exposes
`challenge_window_seconds = 259200` and enforces the same guard in both refund paths.

## Required CredPass wiring

CredPass reads reputation live from its configured oracle. Deploying v2 does not automatically
change that address. Verify it before treating the two contracts as a working pair:

```bash
genlayer network set studionet

VAULT=0xD57A790cDc4A6a456A2C064D80153B0cb679d724
GATE=0x2c9d68A00f110DCB2f9CBb41b77A748c080cBF39

genlayer call $GATE get_stats
# required: "oracle" == "0xD57A790cDc4A6a456A2C064D80153B0cb679d724"

# Run this owner-only write if get_stats still reports the old v1 vault:
genlayer write $GATE set_oracle --args $VAULT
```

Do not infer wiring from the addresses in this repository: `get_stats().oracle` is the on-chain
source of truth. The live smoke suite below checks it strictly.

## Read-only verification

```bash
genlayer network set studionet

VAULT=0xD57A790cDc4A6a456A2C064D80153B0cb679d724
GATE=0x2c9d68A00f110DCB2f9CBb41b77A748c080cBF39

# 1) Prove this is the corrected vault build
genlayer call $VAULT get_config
# expect all of:
#   "platform_fee_bps": 200
#   "challenge_window_seconds": 259200
#   "review_timeout_seconds": 432000
#   "bounty_ttl_seconds": 3888000
#   "paused": false

# 2) Check accounting views
genlayer call $VAULT get_platform_stats
# expect integer counters including total_escrow_locked and total_pending_fees

# 3) Check the one load-bearing cross-contract relationship
genlayer call $GATE get_stats
# expect: "oracle": "0xD57A790cDc4A6a456A2C064D80153B0cb679d724"

genlayer call $GATE get_tier_thresholds
# expect: {"APPRENTICE":0,"PROVEN":1,"TRUSTED":5,"ELITE":15}

# 4) Exercise harmless read paths
genlayer call $VAULT list_bounties --args 0 5 ""
genlayer call $GATE list_programs --args 0 5
```

Scripted equivalent:

```bash
LIVE_SMOKE=1 python3 -m pytest tests/integration/test_live_smoke.py -v
```

The smoke suite defaults to the addresses above. A failure in
`test_gate_tiers_and_oracle_wiring` means CredPass has not been repointed yet; it is not safe to
describe the pair as composable until that test passes.

## Historical deployment — do not use

| Contract | Version | Address | Reason superseded |
| --- | --- | --- | --- |
| BugVault | v1 | `0xAe717Dd46C96D06cE766d27c04f3e6385563aA3B` | No enforced challenge window after rejection; sponsor refund could race arbitration |

The v1 address remains documented only for provenance. Do not send GEN to it.

## Upgrade policy

Intelligent Contracts are immutable: source edits do not modify a deployed address.

1. Deploy changed vault source to a fresh address.
2. Verify the new build through a unique config marker and the live smoke suite.
3. Repoint the existing CredPass with `set_oracle(NEW_VAULT)`.
4. Update both this document and `deployments/studionet.json` in the same commit.
5. Keep superseded addresses as clearly unsafe historical records, never as defaults.

## Interacting with v2

* `samples/quickstart_cli.sh` defaults to v2 for read-only checks.
* Payable flows need a signing client. Set
  `VAULT_ADDRESS=0xD57A790cDc4A6a456A2C064D80153B0cb679d724`,
  `SPONSOR_PRIVATE_KEY` and `RESEARCHER_PRIVATE_KEY` when running `samples/dispute_flow.ts`.
  The two identities must differ.
* `samples/deploy_and_fund.ts` deploys a fresh pair by design; it does not target this live vault.