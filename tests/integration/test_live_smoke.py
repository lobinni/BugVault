"""Live smoke tests — read-only sanity checks against the deployed pair on
Studionet. No GEN, no writes, no LLM: these only call views through the
`genlayer` CLI and assert the deployment is exactly the one this repo ships.

    LIVE_SMOKE=1 python3 -m pytest tests/integration/test_live_smoke.py -v
    (requires: npm i -g genlayer && genlayer network set studionet)

Defaults point at the corrected v2 vault and the existing CredPass; override
with env vars:
    LIVE_VAULT_ADDRESS=0x... LIVE_GATE_ADDRESS=0x... LIVE_SMOKE=1 pytest ...

`challenge_window_seconds` in get_config is the deployment marker for the
challenge-deadline fix (Claim.rejected_epoch). The gate-wiring test is
intentionally strict: it fails until CredPass.get_stats().oracle points at
this v2 vault.
"""
import json
import os
import shutil
import subprocess

import pytest

VAULT = os.environ.get(
    "LIVE_VAULT_ADDRESS", "0xD57A790cDc4A6a456A2C064D80153B0cb679d724")
GATE = os.environ.get(
    "LIVE_GATE_ADDRESS", "0x2c9d68A00f110DCB2f9CBb41b77A748c080cBF39")

ZERO = "0x0000000000000000000000000000000000000000"

pytestmark = pytest.mark.skipif(
    os.environ.get("LIVE_SMOKE") != "1",
    reason="set LIVE_SMOKE=1 (with the genlayer CLI on studionet) to run")

if shutil.which("genlayer") is None:
    pytestmark = pytest.mark.skipif(
        True, reason="genlayer CLI not found on PATH")


def _cli_call(address, method, args=()):
    """Call a view through the CLI and unwrap the view's JSON-string result.
    `genlayer call` prints the raw return value; our views return JSON
    strings, which may arrive doubly-encoded — unwrap until it is a dict."""
    cmd = ["genlayer", "call", address, method]
    if args:
        cmd += ["--args"] + [str(a) for a in args]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, method + " failed: " + out.stderr.strip()
    raw = out.stdout.strip()
    val = json.loads(raw)
    if isinstance(val, str):          # view JSON-string arriving quoted
        val = json.loads(val)
    return val


def test_vault_config_matches_this_build():
    cfg = _cli_call(VAULT, "get_config")
    assert cfg["platform_fee_bps"] == 200
    assert cfg["max_fee_bps"] == 800
    assert cfg["dispute_bond"] == 2 * 10**16
    assert cfg["paused"] is False
    assert cfg["review_timeout_seconds"] == 5 * 86400
    assert cfg["challenge_window_seconds"] == 3 * 86400
    assert cfg["bounty_ttl_seconds"] == 45 * 86400


def test_vault_stats_shape():
    stats = _cli_call(VAULT, "get_platform_stats")
    for key in ("total_bounties", "total_claims", "total_paid_out",
                "total_escrow_locked", "total_pending_fees"):
        assert key in stats and isinstance(stats[key], int)


def test_zero_address_has_no_credibility():
    assert _cli_call(VAULT, "has_credibility", (ZERO, 1)) is False
    rep = _cli_call(VAULT, "get_reputation", (ZERO,))
    assert rep["valid_wins"] == 0 and rep["total_earned"] == 0


def test_feeds_answer():
    feed = _cli_call(VAULT, "list_bounties", (0, 5, ""))
    assert feed["ok"] is True and isinstance(feed["bounties"], list)
    programs = _cli_call(GATE, "list_programs", (0, 5))
    assert programs["ok"] is True and isinstance(programs["programs"], list)


def test_gate_tiers_and_oracle_wiring():
    tiers = _cli_call(GATE, "get_tier_thresholds")
    assert tiers == {"APPRENTICE": 0, "PROVEN": 1, "TRUSTED": 5, "ELITE": 15}
    stats = _cli_call(GATE, "get_stats")
    # The single load-bearing relationship of the whole system:
    assert stats["oracle"].lower() == VAULT.lower()
    assert _cli_call(GATE, "is_eligible", (1, ZERO)) in (True, False)
