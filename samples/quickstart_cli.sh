#!/usr/bin/env bash
# ============================================================================
# BugVault quickstart — deployment + read-only poking via the GenLayer CLI.
#
# Defaults point at the LIVE Studionet deployment (see DEPLOYMENTS.md), so the
# read sections work out of the box. Override with env vars after a redeploy.
#
# Covers everything EXCEPT payable writes (the genlayer CLI cannot attach GEN
# to a write call) — for create_bounty / dispute_claim use the TypeScript
# samples next to this file (deploy_and_fund.ts, dispute_flow.ts).
#
#   chmod +x samples/quickstart_cli.sh && ./samples/quickstart_cli.sh
# ============================================================================
set -euo pipefail

genlayer network set studionet

VAULT="${LIVE_VAULT_ADDRESS:-0xD57A790cDc4A6a456A2C064D80153B0cb679d724}"
GATE="${LIVE_GATE_ADDRESS:-0x2c9d68A00f110DCB2f9CBb41b77A748c080cBF39}"

# ── A. Verify the recorded deployment (read-only, free) ────────────────────
genlayer call "$VAULT" get_config
genlayer call "$VAULT" get_platform_stats
genlayer call "$GATE"  get_tier_thresholds
genlayer call "$GATE"  get_stats          # MUST report "oracle" == $VAULT
genlayer call "$VAULT" has_credibility --args "0x0000000000000000000000000000000000000000" 1

# ── B. Open a private program on the gate (non-payable write) ──────────────
# genlayer write "$GATE" create_program --args "Exchange Audit Squad" "TRUSTED"
# genlayer call  "$GATE" get_program --args 1
# check eligibility BEFORE spending gas on enroll():
# genlayer write "$GATE" preview_enrollment --args 1 "0xRESEARCHER_ADDRESS"

# ── C. Once bounties exist (funded via deploy_and_fund.ts): ────────────────
# genlayer call "$VAULT" list_bounties --args 0 10 ""
# genlayer call "$VAULT" get_bounty --args 1
# genlayer call "$VAULT" get_claims_by_bounty --args 1 0 10
# genlayer call "$VAULT" get_reputation --args "0xRESEARCHER_ADDRESS"

# ── D. Deploying your OWN pair instead of using the recorded one: ──────────
# genlayer deploy --contract contracts/bug_vault.py --args 200
# genlayer deploy --contract contracts/cred_pass.py --args "0xYOUR_VAULT_ADDRESS"
# remember to record new addresses in DEPLOYMENTS.md + deployments/studionet.json
echo "done — see DEPLOYMENTS.md for verification details"
