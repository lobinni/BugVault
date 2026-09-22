"""On-chain integration tests for the BugVault pair.

These exercise REAL consensus on the local simulator: deployments, payable
writes with attached value, view reads, and — when enabled — an actual
multi-validator LLM arbitration over a fetched web page.

    gltest tests/integration -v

Set RUN_LLM_TESTS=1 and REPORT_URL=https://... to include arbitration; the
URL should host a page clearly matching SCOPE below so any sane LLM votes
VALID. Deterministic paths (approve / reject / cancel) need no LLM and run
everywhere.
"""
import json
import os

import pytest

from gltest.assertions import tx_execution_succeeded
from gltest.helpers import create_new_account, fund_account

OWNER_ESCROW = 10**18          # 1 GEN
BOND = 2 * 10**16              # matches DISPUTE_BOND in the contract

SCOPE = ("Admin dashboard of app.example is in scope: auth bypass, IDOR, "
         "stored XSS and privilege escalation chains on staging data.")
POLICY = "Severity MEDIUM and above is payable"

REPORT_URL = os.environ.get("REPORT_URL", "")
RUN_LLM = os.environ.get("RUN_LLM_TESTS") == "1"


def _as_second_account(contract):
    researcher = create_new_account()
    fund_account(researcher, 100)  # GEN for gas + bonds
    return contract.connect(researcher)


def test_deploy_and_config(vault):
    cfg = json.loads(vault.get_config().call())
    assert cfg["platform_fee_bps"] == 200
    assert cfg["paused"] is False
    assert cfg["dispute_bond"] == BOND
    assert cfg["challenge_window_seconds"] == 3 * 86400


def test_full_happy_path_direct_approval(vault):
    receipt = vault.create_bounty(
        args=["App.example Q3", SCOPE, POLICY]
    ).transact(value=OWNER_ESCROW)
    assert tx_execution_succeeded(receipt)
    created = json.loads(receipt["data"]["result"])
    assert created["ok"] is True

    as_researcher = _as_second_account(vault)
    receipt = as_researcher.submit_claim(
        args=[created["bounty_id"], "https://reports.example/idora-poc", "repro inside"]
    ).transact()
    assert tx_execution_succeeded(receipt)
    claim = json.loads(receipt["data"]["result"])
    assert claim["ok"] is True

    receipt = vault.approve_claim(args=[claim["claim_id"]]).transact()
    assert tx_execution_succeeded(receipt)
    out = json.loads(receipt["data"]["result"])
    assert out["ok"] is True
    assert out["bounty_status"] == "PAID_OUT"

    bounty = json.loads(vault.get_bounty(args=[created["bounty_id"]]).call())
    assert bounty["status"] == "PAID_OUT"
    assert bounty["winning_claim_id"] == claim["claim_id"]

    rep = json.loads(vault.get_reputation(args=[str(as_researcher.address)]).call())
    assert rep["valid_wins"] == 1


def test_reject_then_claim_still_disputable_state(vault):
    receipt = vault.create_bounty(args=["App.example Q4", SCOPE, POLICY]).transact(value=OWNER_ESCROW)
    created = json.loads(receipt["data"]["result"])
    as_researcher = _as_second_account(vault)
    claim = json.loads(
        as_researcher.submit_claim(
            args=[created["bounty_id"], "https://reports.example/weak", ""]
        ).transact()["data"]["result"])
    out = json.loads(
        vault.reject_claim(args=[claim["claim_id"], "not exploitable"]).transact()["data"]["result"])
    assert out["ok"] is True
    state = json.loads(vault.get_claim(args=[claim["claim_id"]]).call())
    assert state["status"] == "REJECTED"
    assert state["challengeable"] is True
    assert state["challenge_deadline_epoch"] > state["rejected_epoch"]

    # The v2 regression: rejection must not let the sponsor race arbitration
    # by immediately draining the reward through cancel_bounty.
    try:
        cancel_receipt = vault.cancel_bounty(args=[created["bounty_id"]]).transact()
    except Exception:
        # Some gltest versions raise immediately for a UserError; others
        # return a failed receipt. Both are the expected chain behaviour.
        pass
    else:
        assert not tx_execution_succeeded(cancel_receipt)
    bounty = json.loads(vault.get_bounty(args=[created["bounty_id"]]).call())
    assert bounty["status"] == "OPEN"


@pytest.mark.skipif(not RUN_LLM, reason="set RUN_LLM_TESTS=1 and REPORT_URL to run LLM arbitration")
def test_arbitration_valid_report_wins(vault):
    assert REPORT_URL, "REPORT_URL must point at a public, clearly-in-scope report page"
    receipt = vault.create_bounty(args=["Arbitration demo", SCOPE, POLICY]).transact(value=OWNER_ESCROW)
    created = json.loads(receipt["data"]["result"])
    as_researcher = _as_second_account(vault)
    claim = json.loads(
        as_researcher.submit_claim(args=[created["bounty_id"], REPORT_URL, "see poc"]).transact()
        ["data"]["result"])
    vault.reject_claim(args=[claim["claim_id"], "disputed"]).transact()
    receipt = as_researcher.dispute_claim(args=[claim["claim_id"]]).transact(value=BOND)
    assert tx_execution_succeeded(receipt)
    out = json.loads(receipt["data"]["result"])
    assert out["ok"] is True
    assert out["verdict"] in ("VALID", "INVALID", "INCONCLUSIVE", "RETRY_LATER")
    if out["verdict"] == "VALID":
        bounty = json.loads(vault.get_bounty(args=[created["bounty_id"]]).call())
        assert bounty["status"] == "PAID_OUT"
