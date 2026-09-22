"""Offline white-box tests for contracts/bug_vault.py.

These tests execute the REAL contract code — not a re-implementation — via
the stdlib-only genlayer_stub. Nondeterminism is scripted through
NONDET_HOOKS, so the REAL _judge runs end to end: web render, prompt
building, JSON cleanup (_clean_json), coherence checks (_coherent), and the
transient/verdict error taxonomy. Time is controlled via _now_epoch.

Runs under BOTH runners with zero dependencies:
    python3 -m pytest tests/offline -v
    python3 -m unittest discover -s tests/offline -p "test_*.py" -v
"""
import os
import sys
import json
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(BASE, "..", "..", "contracts")))
sys.path.insert(0, BASE)

import genlayer_stub as stub  # noqa: E402,F401 — registers itself as `genlayer`
import bug_vault as bv        # noqa: E402

OWNER = "0x" + "01" * 20
SPONSOR = "0x" + "02" * 20
RESEARCHER = "0x" + "03" * 20
RESEARCHER_B = "0x" + "04" * 20
OUTSIDER = "0x" + "05" * 20

T0 = 1_800_000_000
ESCROW = 10**18          # 1 GEN
FEE_BPS = 200            # 2%
NET = ESCROW - ESCROW * FEE_BPS // bv.BPS_DENOM

_REAL_NOW = bv._now_epoch

SCOPE = ("Admin dashboard of app.example is in scope: auth bypass, IDOR, "
         "stored XSS and privilege escalation chains on staging data.")
POLICY = "Severity MEDIUM and above is payable"


class BugVaultCase(unittest.TestCase):
    def setUp(self):
        stub.reset_state()
        self.now = T0
        bv._now_epoch = lambda: self.now
        stub.NONDET_HOOKS["web_render"] = lambda url, mode, wait: "detailed vulnerability writeup"
        self.patch_judge(bv.VERDICT_VALID)
        self._act_as(OWNER)
        self.vault = bv.BugVault(FEE_BPS)

    def tearDown(self):
        bv._now_epoch = _REAL_NOW

    # ── helpers ──────────────────────────────────────────────────────────

    def _act_as(self, who, value=0):
        stub.gl.message.sender_address = stub.Address(who)
        stub.gl.message.value = int(value)

    def patch_judge(self, verdict=bv.VERDICT_VALID, severity="HIGH", confidence=90,
                    reasoning="matches scope"):
        """Swap only the LLM's answer — the real fetch→prompt→parse pipeline
        still executes underneath."""
        payload = {"verdict": verdict, "severity": severity,
                   "confidence": confidence, "reasoning": reasoning}
        stub.NONDET_HOOKS["exec_prompt"] = lambda prompt: json.dumps(payload)

    def transfers_to(self, addr):
        return [t["amount"] for t in stub.TRANSFERS if t["to"] == addr]

    def fund(self, value=ESCROW, title="App.example Q3", scope=SCOPE, policy=POLICY):
        self._act_as(SPONSOR, value)
        return json.loads(self.vault.create_bounty(title, scope, policy))

    def submit(self, bounty_id, who=RESEARCHER, url="https://reports.example/idora-poc"):
        self._act_as(who)
        return json.loads(self.vault.submit_claim(bounty_id, url, "repro steps inside"))

    def claim(self, cid):
        return json.loads(self.vault.get_claim(cid))

    def bounty(self, bid):
        return json.loads(self.vault.get_bounty(bid))

    def stats(self):
        return json.loads(self.vault.get_platform_stats())

    # ── pure helpers under test ──────────────────────────────────────────

    def test_clean_json_strips_prose_and_fences(self):
        self.assertEqual(bv._clean_json('blah {"a": 1} tail'), {"a": 1})
        self.assertEqual(bv._clean_json('```json\n{"a": 2}\n```'), {"a": 2})
        self.assertIsNone(bv._clean_json("no json here"))
        self.assertIsNone(bv._clean_json("{unclosed"))
        self.assertIsNone(bv._clean_json("{} }"))

    def test_coherent_rejects_malformed_reports(self):
        good = {"verdict": "VALID", "severity": "HIGH", "confidence": 90, "reasoning": "ok"}
        self.assertTrue(bv._coherent(dict(good)))
        self.assertFalse(bv._coherent({**good, "verdict": "MAYBE"}))
        self.assertFalse(bv._coherent({**good, "severity": "SPECTACULAR"}))
        self.assertFalse(bv._coherent({**good, "confidence": 200}))
        self.assertFalse(bv._coherent({**good, "confidence": "high"}))
        self.assertFalse(bv._coherent({**good, "reasoning": "x" * (bv.MAX_REASONING_CHARS + 1)}))
        self.assertFalse(bv._coherent("not a dict"))

    def test_judge_pipeline_accepts_float_confidence(self):
        stub.NONDET_HOOKS["exec_prompt"] = lambda prompt: json.dumps(
            {"verdict": "VALID", "severity": "LOW", "confidence": 87.5, "reasoning": "ok"})
        out = bv._judge("scope text", "policy", "https://x.example/r", "")
        self.assertEqual(out["confidence"], 87)

    def test_judge_pipeline_empty_page_is_real_inconclusive(self):
        stub.NONDET_HOOKS["web_render"] = lambda url, mode, wait: "   "
        out = bv._judge("scope", "policy", "https://x.example/blank", "")
        self.assertEqual(out["verdict"], bv.VERDICT_INCONCLUSIVE)

    def test_judge_pipeline_malformed_llm_raises_tagged(self):
        stub.NONDET_HOOKS["exec_prompt"] = lambda prompt: "I cannot decide, sorry."
        with self.assertRaises(stub.gl.vm.UserError) as ctx:
            bv._judge("scope", "policy", "https://x.example/r", "")
        self.assertIn(bv.ERR_LLM_MALFORMED, str(ctx.exception))

    # ── deployment ───────────────────────────────────────────────────────

    def test_deploy_rejects_out_of_range_fee(self):
        self._act_as(OWNER)
        with self.assertRaises(stub.gl.vm.UserError):
            bv.BugVault(bv.MAX_FEE_BPS + 1)

    def test_config_view(self):
        cfg = json.loads(self.vault.get_config())
        self.assertEqual(cfg["platform_fee_bps"], FEE_BPS)
        self.assertEqual(cfg["owner"], OWNER)
        self.assertFalse(cfg["paused"])
        self.assertEqual(cfg["dispute_bond"], bv.DISPUTE_BOND)

    # ── funding (payable — must never raise) ────────────────────────────

    def test_funding_splits_fee_once_and_locks_escrow(self):
        r = self.fund()
        self.assertTrue(r["ok"])
        self.assertEqual(r["reward_amount"], NET)
        b = self.bounty(r["bounty_id"])
        self.assertEqual(b["status"], bv.BOUNTY_OPEN)
        s = self.stats()
        self.assertEqual(s["total_pending_fees"], r["fee_withheld"])
        self.assertEqual(s["total_escrow_locked"], NET)

    def test_funding_below_min_reward_refunds(self):
        r = self.fund(value=10**14)  # net < MIN_REWARD
        self.assertFalse(r["ok"])
        self.assertEqual(r["refunded"], 10**14)
        self.assertEqual(self.transfers_to(SPONSOR), [10**14])
        self.assertEqual(self.stats()["total_escrow_locked"], 0)

    def test_funding_over_cap_refunds(self):
        r = self.fund(value=bv.MAX_REWARD + 1)
        self.assertFalse(r["ok"])
        self.assertEqual(self.transfers_to(SPONSOR), [bv.MAX_REWARD + 1])

    def test_funding_short_scope_refunds(self):
        r = self.fund(scope="too short")
        self.assertFalse(r["ok"])
        self.assertEqual(r["refunded"], ESCROW)

    def test_funding_with_no_value(self):
        r = self.fund(value=0)
        self.assertFalse(r["ok"])
        self.assertEqual(stub.TRANSFERS, [])

    # ── pause gates only new money ──────────────────────────────────────

    def test_pause_gates_funding_only(self):
        self._act_as(OWNER)
        self.vault.set_paused(True)
        r = self.fund()
        self.assertFalse(r["ok"])
        self.assertEqual(r["refunded"], ESCROW)
        # settlement paths keep working while paused
        self._act_as(OWNER)
        self.vault.set_paused(False)
        r = self.fund()
        c = self.submit(r["bounty_id"])
        self._act_as(OWNER)
        self.vault.set_paused(True)
        self._act_as(SPONSOR)
        out = json.loads(self.vault.approve_claim(c["claim_id"]))
        self.assertTrue(out["ok"])

    def test_pause_owner_only(self):
        self._act_as(OUTSIDER)
        with self.assertRaises(stub.gl.vm.UserError):
            self.vault.set_paused(True)

    # ── submissions ──────────────────────────────────────────────────────

    def test_submit_validations(self):
        r = self.fund()
        bid = r["bounty_id"]
        self._act_as(SPONSOR)
        with self.assertRaises(stub.gl.vm.UserError):  # sponsor self-claim
            self.vault.submit_claim(bid, "https://x.example/report", "")
        self._act_as(RESEARCHER)
        with self.assertRaises(stub.gl.vm.UserError):  # short URL
            self.vault.submit_claim(bid, "http://a", "")
        with self.assertRaises(stub.gl.vm.UserError):  # unknown bounty
            self.vault.submit_claim(999, "https://x.example/report", "")
        out = self.submit(bid)
        self.assertTrue(out["ok"])
        rep = json.loads(self.vault.get_reputation(RESEARCHER))
        self.assertEqual(rep["attempts"], 1)
        self.assertEqual(rep["valid_wins"], 0)
        self.assertEqual(rep["total_earned"], 0)

    def test_claim_cap_enforced(self):
        r = self.fund()
        who = ["0x" + ("1" + str(i)) * 20 for i in range(bv.MAX_CLAIMS_PER_BOUNTY)]
        for w in who:
            out = self.submit(r["bounty_id"], who=w)
            self.assertTrue(out["ok"])
        with self.assertRaises(stub.gl.vm.UserError):
            self.submit(r["bounty_id"], who=OUTSIDER)

    def test_one_live_claim_per_researcher(self):
        r = self.fund()
        out = self.submit(r["bounty_id"])
        self.assertTrue(out["ok"])
        with self.assertRaises(stub.gl.vm.UserError):  # second live claim blocked
            self.submit(r["bounty_id"])
        cid = out["claim_id"]
        self._act_as(SPONSOR)
        self.vault.reject_claim(cid, "weak")
        out2 = self.submit(r["bounty_id"])  # rejected -> free to try again
        self.assertTrue(out2["ok"])

    # ── direct approval (fast path, no AI) ──────────────────────────────

    def test_direct_approval_pays_and_voids_siblings(self):
        r = self.fund()
        c1 = self.submit(r["bounty_id"], who=RESEARCHER)
        c2 = self.submit(r["bounty_id"], who=RESEARCHER_B, url="https://reports.example/b")
        self._act_as(SPONSOR)
        out = json.loads(self.vault.approve_claim(c2["claim_id"]))
        self.assertTrue(out["ok"])
        self.assertEqual(self.transfers_to(RESEARCHER_B), [NET])
        self.assertEqual(self.claim(c1["claim_id"])["status"], bv.CLAIM_VOIDED)
        self.assertEqual(self.claim(c2["claim_id"])["status"], bv.CLAIM_APPROVED)
        b = self.bounty(r["bounty_id"])
        self.assertEqual(b["status"], bv.BOUNTY_PAID_OUT)
        self.assertEqual(b["winning_claim_id"], c2["claim_id"])
        rep = json.loads(self.vault.get_reputation(RESEARCHER_B))
        self.assertEqual(rep["valid_wins"], 1)
        self.assertEqual(rep["win_rate_bps"], 10000)
        self.assertEqual(rep["total_earned"], NET)
        s = self.stats()
        self.assertEqual(s["total_escrow_locked"], 0)
        self.assertEqual(s["total_paid_out"], NET)
        self._act_as(SPONSOR)
        with self.assertRaises(stub.gl.vm.UserError):
            self.vault.cancel_bounty(r["bounty_id"])

    def test_approve_reject_permissions(self):
        r = self.fund()
        c = self.submit(r["bounty_id"])
        self._act_as(OUTSIDER)
        with self.assertRaises(stub.gl.vm.UserError):
            self.vault.approve_claim(c["claim_id"])
        with self.assertRaises(stub.gl.vm.UserError):
            self.vault.reject_claim(c["claim_id"], "nope")
        self._act_as(SPONSOR)
        out = json.loads(self.vault.reject_claim(c["claim_id"], "out of scope"))
        self.assertTrue(out["ok"])
        with self.assertRaises(stub.gl.vm.UserError):
            self.vault.approve_claim(c["claim_id"])

    # ── arbitration: challenge mode (sponsor said REJECTED) ─────────────

    def _rejected_claim(self):
        r = self.fund()
        c = self.submit(r["bounty_id"])
        self._act_as(SPONSOR)
        self.vault.reject_claim(c["claim_id"], "not exploitable")
        return r, c

    def test_challenge_win_pays_reward_and_returns_bond(self):
        r, c = self._rejected_claim()
        self.patch_judge(bv.VERDICT_VALID)
        self._act_as(RESEARCHER, bv.DISPUTE_BOND)
        out = json.loads(self.vault.dispute_claim(c["claim_id"]))
        self.assertTrue(out["ok"])
        self.assertEqual(out["verdict"], bv.VERDICT_VALID)
        self.assertEqual(self.transfers_to(RESEARCHER), [NET, bv.DISPUTE_BOND])
        cl = self.claim(c["claim_id"])
        self.assertEqual(cl["status"], bv.CLAIM_RESOLVED_VALID)
        self.assertEqual(cl["ai_severity"], "HIGH")
        self.assertEqual(self.bounty(r["bounty_id"])["status"], bv.BOUNTY_PAID_OUT)

    def test_challenge_loss_forfeits_bond_to_sponsor(self):
        r, c = self._rejected_claim()
        self.patch_judge(bv.VERDICT_INVALID, severity="NONE", confidence=95)
        self._act_as(RESEARCHER, bv.DISPUTE_BOND)
        out = json.loads(self.vault.dispute_claim(c["claim_id"]))
        self.assertTrue(out["ok"])
        self.assertEqual(out["bond"], "bond_forfeited_to_sponsor")
        self.assertEqual(self.transfers_to(SPONSOR), [bv.DISPUTE_BOND])
        self.assertEqual(self.claim(c["claim_id"])["status"], bv.CLAIM_RESOLVED_INVALID)
        self.assertEqual(self.bounty(r["bounty_id"])["status"], bv.BOUNTY_OPEN)
        self.assertEqual(self.stats()["total_escrow_locked"], NET)

    def test_dispute_requires_exact_bond_and_researcher(self):
        r, c = self._rejected_claim()
        self._act_as(OUTSIDER, bv.DISPUTE_BOND)
        out = json.loads(self.vault.dispute_claim(c["claim_id"]))
        self.assertFalse(out["ok"])
        self.assertEqual(self.transfers_to(OUTSIDER), [bv.DISPUTE_BOND])
        self._act_as(RESEARCHER, bv.DISPUTE_BOND + 1)
        out = json.loads(self.vault.dispute_claim(c["claim_id"]))
        self.assertFalse(out["ok"])
        self.assertEqual(self.transfers_to(RESEARCHER), [bv.DISPUTE_BOND + 1])

    # ── arbitration: timeout mode (sponsor went silent) ──────────────────

    def test_silent_sponsor_bond_never_forfeited(self):
        r = self.fund()
        c = self.submit(r["bounty_id"])
        self.now += bv.REVIEW_TIMEOUT_SECONDS + 1
        self.patch_judge(bv.VERDICT_INVALID, severity="NONE", confidence=85)
        self._act_as(RESEARCHER, bv.DISPUTE_BOND)
        out = json.loads(self.vault.dispute_claim(c["claim_id"]))
        self.assertTrue(out["ok"])
        self.assertEqual(out["bond"], "bond_returned")
        self.assertEqual(self.transfers_to(RESEARCHER), [bv.DISPUTE_BOND])
        self.assertEqual(self.transfers_to(SPONSOR), [])

    def test_dispute_before_timeout_refunded(self):
        r = self.fund()
        c = self.submit(r["bounty_id"])
        self.now += bv.REVIEW_TIMEOUT_SECONDS - 100
        self._act_as(RESEARCHER, bv.DISPUTE_BOND)
        out = json.loads(self.vault.dispute_claim(c["claim_id"]))
        self.assertFalse(out["ok"])
        self.assertIn("review window", out["reason"])

    # ── arbitration: inconclusive and transient failures ─────────────────

    def test_inconclusive_is_not_terminal(self):
        r, c = self._rejected_claim()
        self.patch_judge(bv.VERDICT_INCONCLUSIVE, severity="LOW", confidence=40,
                         reasoning="partial poc, unreadable payload")
        self._act_as(RESEARCHER, bv.DISPUTE_BOND)
        out = json.loads(self.vault.dispute_claim(c["claim_id"]))
        self.assertTrue(out["ok"])
        cl = self.claim(c["claim_id"])
        self.assertEqual(cl["status"], bv.CLAIM_REJECTED)   # unchanged
        self.assertEqual(cl["ai_verdict"], bv.VERDICT_INCONCLUSIVE)  # evidence kept
        self.assertEqual(self.transfers_to(RESEARCHER), [bv.DISPUTE_BOND])
        self.patch_judge(bv.VERDICT_VALID)
        self._act_as(RESEARCHER, bv.DISPUTE_BOND)
        out2 = json.loads(self.vault.dispute_claim(c["claim_id"]))
        self.assertEqual(out2["verdict"], bv.VERDICT_VALID)

    def test_transient_fetch_yields_retry_later_through_real_judge(self):
        r, c = self._rejected_claim()
        def down(url, mode, wait):
            raise ConnectionError("connection reset")
        stub.NONDET_HOOKS["web_render"] = down
        self._act_as(RESEARCHER, bv.DISPUTE_BOND)
        out = json.loads(self.vault.dispute_claim(c["claim_id"]))
        self.assertTrue(out["ok"])
        self.assertEqual(out["verdict"], "RETRY_LATER")
        self.assertEqual(out["bond_returned"], bv.DISPUTE_BOND)
        cl = self.claim(c["claim_id"])
        self.assertEqual(cl["status"], bv.CLAIM_REJECTED)
        self.assertEqual(cl["ai_verdict"], "")   # nothing recorded as evidence

    def test_empty_report_page_inconclusive_end_to_end(self):
        r, c = self._rejected_claim()
        stub.NONDET_HOOKS["web_render"] = lambda url, mode, wait: ""
        self._act_as(RESEARCHER, bv.DISPUTE_BOND)
        out = json.loads(self.vault.dispute_claim(c["claim_id"]))
        self.assertEqual(out["verdict"], bv.VERDICT_INCONCLUSIVE)
        self.assertEqual(self.claim(c["claim_id"])["status"], bv.CLAIM_REJECTED)

    def test_clock_zero_blocks_dispute(self):
        r, c = self._rejected_claim()
        self.now = 0
        self._act_as(RESEARCHER, bv.DISPUTE_BOND)
        out = json.loads(self.vault.dispute_claim(c["claim_id"]))
        self.assertFalse(out["ok"])
        self.assertIn("clock", out["reason"])

    # ── preview (no bond, no state) ──────────────────────────────────────

    def test_preview_arbitration_mutates_nothing(self):
        r = self.fund()
        c = self.submit(r["bounty_id"])
        self._act_as(SPONSOR)
        out = json.loads(self.vault.preview_arbitration(c["claim_id"]))
        self.assertTrue(out["ok"])
        self.assertEqual(out["verdict"], bv.VERDICT_VALID)
        cl = self.claim(c["claim_id"])
        self.assertEqual(cl["status"], bv.CLAIM_SUBMITTED)
        self.assertEqual(cl["ai_verdict"], "")
        self._act_as(OUTSIDER)
        with self.assertRaises(stub.gl.vm.UserError):
            self.vault.preview_arbitration(c["claim_id"])

    def test_preview_survives_malformed_llm(self):
        r = self.fund()
        c = self.submit(r["bounty_id"])
        stub.NONDET_HOOKS["exec_prompt"] = lambda prompt: "garbage, not json"
        self._act_as(RESEARCHER)
        out = json.loads(self.vault.preview_arbitration(c["claim_id"]))
        self.assertTrue(out["ok"])
        self.assertEqual(out["verdict"], "RETRY_LATER")

    # ── cancel / reclaim ─────────────────────────────────────────────────

    def test_cancel_blocked_by_pending_claim(self):
        r = self.fund()
        bid = r["bounty_id"]
        self.submit(bid)
        self._act_as(SPONSOR)
        with self.assertRaises(stub.gl.vm.UserError):
            self.vault.cancel_bounty(bid)

    def test_cancel_blocked_while_rejection_challengeable(self):
        """The escrow must back a dispute the researcher still has a right to
        open — the sponsor cannot reject and immediately drain the bounty."""
        r = self.fund()
        bid = r["bounty_id"]
        c = self.submit(bid)
        self._act_as(SPONSOR)
        self.vault.reject_claim(c["claim_id"], "weak")
        with self.assertRaises(stub.gl.vm.UserError):   # window still open
            self.vault.cancel_bounty(bid)
        cl = self.claim(c["claim_id"])
        self.assertTrue(cl["challengeable"])
        self.assertEqual(cl["challenge_deadline_epoch"], T0 + bv.CHALLENGE_WINDOW_SECONDS)
        self.now += bv.CHALLENGE_WINDOW_SECONDS          # rejection now final
        self.assertFalse(self.claim(c["claim_id"])["challengeable"])
        out = json.loads(self.vault.cancel_bounty(bid))
        self.assertTrue(out["ok"])
        self.assertEqual(self.transfers_to(SPONSOR), [NET])
        self.assertEqual(self.stats()["total_escrow_locked"], 0)

    def test_cancel_by_non_sponsor_reverts(self):
        r = self.fund()
        self._act_as(OUTSIDER)
        with self.assertRaises(stub.gl.vm.UserError):
            self.vault.cancel_bounty(r["bounty_id"])

    def test_reclaim_after_ttl_permissionless(self):
        r = self.fund()
        self._act_as(OUTSIDER)
        with self.assertRaises(stub.gl.vm.UserError):
            self.vault.reclaim_expired_bounty(r["bounty_id"])
        self.now += bv.BOUNTY_TTL_SECONDS
        out = json.loads(self.vault.reclaim_expired_bounty(r["bounty_id"]))
        self.assertTrue(out["ok"])
        self.assertEqual(self.transfers_to(SPONSOR), [NET])
        self.assertEqual(self.bounty(r["bounty_id"])["status"], bv.BOUNTY_RECLAIMED)
        self.assertEqual(self.stats()["total_escrow_locked"], 0)

    def test_reclaim_blocked_by_pending_claim(self):
        r = self.fund()
        self.submit(r["bounty_id"])
        self.now += bv.BOUNTY_TTL_SECONDS
        self._act_as(OUTSIDER)
        with self.assertRaises(stub.gl.vm.UserError):
            self.vault.reclaim_expired_bounty(r["bounty_id"])

    def test_reclaim_blocked_while_rejection_challengeable(self):
        """TTL + an open challenge window: the window wins, expiry must wait
        until the rejection is final."""
        r = self.fund()
        bid = r["bounty_id"]
        self.now += bv.BOUNTY_TTL_SECONDS               # bounty expires first
        c = self.submit(bid)
        self._act_as(SPONSOR)
        self.vault.reject_claim(c["claim_id"], "weak") # fresh challenge window
        self._act_as(OUTSIDER)
        with self.assertRaises(stub.gl.vm.UserError):   # expired, but dispute still possible
            self.vault.reclaim_expired_bounty(bid)
        self.now += bv.CHALLENGE_WINDOW_SECONDS          # rejection now final
        out = json.loads(self.vault.reclaim_expired_bounty(bid))
        self.assertTrue(out["ok"])
        self.assertEqual(self.transfers_to(SPONSOR), [NET])

    def test_dispute_after_challenge_deadline_refunded(self):
        r, c = self._rejected_claim()
        self.now += bv.CHALLENGE_WINDOW_SECONDS          # too late to challenge
        self._act_as(RESEARCHER, bv.DISPUTE_BOND)
        out = json.loads(self.vault.dispute_claim(c["claim_id"]))
        self.assertFalse(out["ok"])
        self.assertIn("challenge window", out["reason"])
        self.assertEqual(self.transfers_to(RESEARCHER), [bv.DISPUTE_BOND])
        # ...but a timeout-mode escalation is unaffected by that deadline:
        # the claim below is fresh, the sponsor simply vanishes.

    # ── fees & ownership ─────────────────────────────────────────────────

    def test_sweep_respects_cooldown(self):
        r = self.fund()
        c = self.submit(r["bounty_id"])
        self._act_as(SPONSOR)
        self.vault.approve_claim(c["claim_id"])   # outbound transfer at self.now
        self._act_as(OWNER)
        with self.assertRaises(stub.gl.vm.UserError):
            self.vault.sweep_fees()
        self.now += bv.SWEEP_DELAY_SECONDS + 1
        out = json.loads(self.vault.sweep_fees())
        fee = ESCROW * FEE_BPS // bv.BPS_DENOM
        self.assertEqual(out["swept"], fee)
        self.assertEqual(self.transfers_to(OWNER), [fee])
        self.now += bv.SWEEP_DELAY_SECONDS + 1
        out2 = json.loads(self.vault.sweep_fees())
        self.assertEqual(out2["swept"], 0)

    def test_fee_setter_cap_and_effect(self):
        self._act_as(OWNER)
        with self.assertRaises(stub.gl.vm.UserError):
            self.vault.set_platform_fee_bps(bv.MAX_FEE_BPS + 1)
        self.vault.set_platform_fee_bps(0)
        r = self.fund()
        self.assertEqual(r["fee_withheld"], 0)

    def test_ownership_transfer(self):
        self._act_as(OUTSIDER)
        with self.assertRaises(stub.gl.vm.UserError):
            self.vault.set_treasury(OUTSIDER)
        self._act_as(OWNER)
        self.vault.transfer_ownership(OUTSIDER)
        self._act_as(OUTSIDER)
        out = json.loads(self.vault.set_treasury(OUTSIDER))
        self.assertTrue(out["ok"])

    # ── reputation surface used by CredPass ──────────────────────────────

    def test_credibility_view(self):
        self.assertFalse(self.vault.has_credibility(RESEARCHER, 1))
        r = self.fund()
        c = self.submit(r["bounty_id"])
        self._act_as(SPONSOR)
        self.vault.approve_claim(c["claim_id"])
        self.assertTrue(self.vault.has_credibility(RESEARCHER, 1))
        self.assertFalse(self.vault.has_credibility(RESEARCHER, 2))

    # ── pagination & feeds ───────────────────────────────────────────────

    def test_pagination_newest_first(self):
        ids = [self.fund()["bounty_id"] for _ in range(3)]
        page1 = json.loads(self.vault.get_bounties_by_sponsor(SPONSOR, 0, 2))
        self.assertEqual([b["bounty_id"] for b in page1["bounties"]], [3, 2])
        self.assertGreaterEqual(page1["next_cursor"], 0)
        page2 = json.loads(self.vault.get_bounties_by_sponsor(SPONSOR, page1["next_cursor"], 2))
        self.assertEqual([b["bounty_id"] for b in page2["bounties"]], [1])
        self.assertEqual(page2["next_cursor"], -1)
        empty = json.loads(self.vault.get_bounties_by_sponsor(OUTSIDER, 0, 10))
        self.assertEqual(empty["bounties"], [])

    def test_list_bounties_feed_with_status_filter(self):
        b1 = self.fund()["bounty_id"]
        b2 = self.fund()["bounty_id"]
        c = self.submit(b2)
        self._act_as(SPONSOR)
        self.vault.approve_claim(c["claim_id"])
        everything = json.loads(self.vault.list_bounties(0, 10, ""))
        self.assertEqual([b["bounty_id"] for b in everything["bounties"]], [2, 1])
        only_open = json.loads(self.vault.list_bounties(0, 10, bv.BOUNTY_OPEN))
        self.assertEqual([b["bounty_id"] for b in only_open["bounties"]], [1])
        only_paid = json.loads(self.vault.list_bounties(0, 10, bv.BOUNTY_PAID_OUT))
        self.assertEqual([b["bounty_id"] for b in only_paid["bounties"]], [2])
        page = json.loads(self.vault.list_bounties(0, 1, ""))
        self.assertEqual([b["bounty_id"] for b in page["bounties"]], [2])
        self.assertGreaterEqual(page["next_cursor"], 0)
        page2 = json.loads(self.vault.list_bounties(page["next_cursor"], 1, ""))
        self.assertEqual([b["bounty_id"] for b in page2["bounties"]], [1])
        self.assertEqual(page2["next_cursor"], -1)


if __name__ == "__main__":
    unittest.main()
