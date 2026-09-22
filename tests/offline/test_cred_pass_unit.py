"""Offline tests for contracts/cred_pass.py against a REAL deployed
BugVault instance — the IC-to-IC view call is served through the stub's
REGISTRY, exactly as it would on-chain.

Run:  python3 -m pytest tests/offline -v   (or unittest discover)
"""
import os
import sys
import json
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(BASE, "..", "..", "contracts")))
sys.path.insert(0, BASE)

import genlayer_stub as stub  # noqa: E402,F401
import bug_vault as bv        # noqa: E402
import cred_pass as cp        # noqa: E402

OWNER = "0x" + "01" * 20
ADMIN = "0x" + "09" * 20
NEWBIE = "0x" + "0a" * 20
PROVEN = "0x" + "0b" * 20
VETERAN = "0x" + "0c" * 20

VAULT_ADDR = "0x" + "aa" * 20


class CredPassCase(unittest.TestCase):
    def setUp(self):
        stub.reset_state()
        self._act_as(OWNER)
        vault = bv.BugVault(200)
        stub.REGISTRY[VAULT_ADDR] = vault
        self.vault = vault
        # Seed credibility directly in storage — cheap, deterministic, and
        # the read path (has_credibility) is identical to what BugVault
        # itself would compute after real wins.
        self.vault.wins[stub.Address(PROVEN)] = stub.u32(2)
        self.vault.attempts[stub.Address(PROVEN)] = stub.u32(3)
        self.vault.wins[stub.Address(VETERAN)] = stub.u32(9)
        self.gate = cp.CredPass(VAULT_ADDR)

    def _act_as(self, who):
        stub.gl.message.sender_address = stub.Address(who)
        stub.gl.message.value = 0

    def _program(self, who=ADMIN, title="Private Exchange Audit", tier=cp.TIER_TRUSTED):
        self._act_as(who)
        return json.loads(self.gate.create_program(title, tier))

    # ── programs ─────────────────────────────────────────────────────────

    def test_program_snapshots_threshold(self):
        p = self._program(tier=cp.TIER_TRUSTED)
        self.assertEqual(p["min_valid_wins"], 5)
        # later tier edits must NOT silently move an existing program's bar
        self._act_as(OWNER)
        self.gate.set_tier_threshold(cp.TIER_TRUSTED, 99)
        view = json.loads(self.gate.get_program(p["program_id"]))
        self.assertEqual(view["min_valid_wins"], 5)
        # ...but new programs see the new bar
        p2 = self._program(title="Second", tier=cp.TIER_TRUSTED)
        self.assertEqual(p2["min_valid_wins"], 99)

    def test_create_program_unknown_tier_reverts(self):
        self._act_as(ADMIN)
        with self.assertRaises(stub.gl.vm.UserError):
            self.gate.create_program("X", "NOPE")

    # ── enrollment (live oracle reads) ──────────────────────────────────

    def test_enroll_allow_and_revert_paths(self):
        p = self._program(tier=cp.TIER_TRUSTED)  # needs 5 wins
        self._act_as(VETERAN)  # 9 wins
        out = json.loads(self.gate.enroll(p["program_id"]))
        self.assertTrue(out["ok"])
        self._act_as(PROVEN)  # 2 wins
        with self.assertRaises(stub.gl.vm.UserError):
            self.gate.enroll(p["program_id"])
        stats = json.loads(self.gate.get_stats())
        self.assertEqual(stats["count_admitted"], 1)
        self.assertEqual(stats["count_denied"], 1)

    def test_enroll_twice_and_closed_program_revert(self):
        p = self._program(tier=cp.TIER_APPRENTICE)
        self._act_as(NEWBIE)
        self.gate.enroll(p["program_id"])
        with self.assertRaises(stub.gl.vm.UserError):
            self.gate.enroll(p["program_id"])
        self._act_as(ADMIN)
        self.gate.set_program_active(p["program_id"], False)
        self._act_as(VETERAN)
        with self.assertRaises(stub.gl.vm.UserError):
            self.gate.enroll(p["program_id"])

    def test_apprentice_program_is_open_to_everyone(self):
        p = self._program(tier=cp.TIER_APPRENTICE)  # 0 wins required
        self._act_as(NEWBIE)
        out = json.loads(self.gate.enroll(p["program_id"]))
        self.assertTrue(out["ok"])
        self.assertEqual(out["required_valid_wins"], 0)

    # ── preview never reverts ────────────────────────────────────────────

    def test_preview_degrades_instead_of_reverting(self):
        self.assertFalse(json.loads(self.gate.preview_enrollment(999, VETERAN))["ok"])
        p = self._program(tier=cp.TIER_TRUSTED)
        r1 = json.loads(self.gate.preview_enrollment(p["program_id"], VETERAN))
        self.assertTrue(r1["eligible"])
        r2 = json.loads(self.gate.preview_enrollment(p["program_id"], NEWBIE))
        self.assertFalse(r2["eligible"])
        self.assertEqual(r2["required_valid_wins"], 5)

    # ── admin & owner gating ─────────────────────────────────────────────

    def test_program_admin_ops_are_gated(self):
        p = self._program(tier=cp.TIER_TRUSTED)
        self._act_as(NEWBIE)
        with self.assertRaises(stub.gl.vm.UserError):
            self.gate.set_program_requirement(p["program_id"], cp.TIER_APPRENTICE)
        self._act_as(ADMIN)
        out = json.loads(self.gate.set_program_requirement(p["program_id"], cp.TIER_APPRENTICE))
        self.assertEqual(out["min_valid_wins"], 0)

    def test_owner_ops_are_gated(self):
        self._act_as(NEWBIE)
        with self.assertRaises(stub.gl.vm.UserError):
            self.gate.set_tier_threshold(cp.TIER_ELITE, 50)
        with self.assertRaises(stub.gl.vm.UserError):
            self.gate.set_oracle(NEWBIE)
        self._act_as(OWNER)
        out = json.loads(self.gate.set_tier_threshold(cp.TIER_ELITE, 50))
        self.assertTrue(out["ok"])

    # ── views / listings ─────────────────────────────────────────────────

    def test_eligibility_is_a_live_read(self):
        p = self._program(tier=cp.TIER_PROVEN)  # needs 1 win
        self.assertFalse(self.gate.is_eligible(p["program_id"], NEWBIE))
        # NEWBIE earns a win on BugVault -> eligibility flips, no sync needed
        self.vault.wins[stub.Address(NEWBIE)] = stub.u32(1)
        self.assertTrue(self.gate.is_eligible(p["program_id"], NEWBIE))

    def test_lists_and_snapshot_passthrough(self):
        p = self._program(tier=cp.TIER_PROVEN)
        self._act_as(PROVEN)
        self.gate.enroll(p["program_id"])
        enr = json.loads(self.gate.get_enrollees(p["program_id"]))
        self.assertEqual(enr["enrollees"], [PROVEN])
        mine = json.loads(self.gate.get_enrollments_by_researcher(PROVEN))
        self.assertEqual(mine["program_ids"], [p["program_id"]])
        snap = json.loads(self.gate.get_credibility_snapshot(PROVEN))
        self.assertEqual(snap["valid_wins"], 2)
        self.assertEqual(snap["attempts"], 3)

    def test_list_programs_feed(self):
        self._program(title="One", tier=cp.TIER_APPRENTICE)
        self._program(title="Two", tier=cp.TIER_PROVEN)
        self._program(title="Three", tier=cp.TIER_TRUSTED)
        page1 = json.loads(self.gate.list_programs(0, 2))
        self.assertEqual([p["program_id"] for p in page1["programs"]], [3, 2])
        self.assertGreaterEqual(page1["next_cursor"], 0)
        page2 = json.loads(self.gate.list_programs(page1["next_cursor"], 2))
        self.assertEqual([p["program_id"] for p in page2["programs"]], [1])
        self.assertEqual(page2["next_cursor"], -1)
        self.assertEqual(page2["programs"][0]["min_valid_wins"], 0)

    def test_oracle_swap(self):
        self._act_as(OWNER)
        other = bv.BugVault(0)
        other_addr = "0x" + "bb" * 20
        stub.REGISTRY[other_addr] = other
        self.vault.wins[stub.Address(NEWBIE)] = stub.u32(9)
        p = self._program(tier=cp.TIER_TRUSTED)
        self.assertTrue(self.gate.is_eligible(p["program_id"], NEWBIE))   # old oracle
        self._act_as(OWNER)
        self.gate.set_oracle(other_addr)
        self.assertFalse(self.gate.is_eligible(p["program_id"], NEWBIE))  # new oracle, 0 wins


if __name__ == "__main__":
    unittest.main()
