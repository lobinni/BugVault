"""Offline consistency tests for the checked-in Studionet deployment record.

These tests do not contact the network. They prevent a superseded address,
an unresolved placeholder, or mismatched CredPass target from silently
becoming the project's default again. Live state is covered separately by
tests/integration/test_live_smoke.py.
"""
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "deployments" / "studionet.json"

V2 = "0xD57A790cDc4A6a456A2C064D80153B0cb679d724"
V1 = "0xAe717Dd46C96D06cE766d27c04f3e6385563aA3B"
GATE = "0x2c9d68A00f110DCB2f9CBb41b77A748c080cBF39"


class DeploymentManifestTestCase(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def test_v2_is_the_current_vault(self):
        vault = self.data["contracts"]["BugVault"]
        self.assertEqual(vault["current_version"], "v2")
        self.assertEqual(vault["address"], V2)
        self.assertEqual(vault["constructor_args"], [200])
        self.assertIn("challenge_window_seconds = 259200", vault["deployment_marker"])

    def test_v1_is_historical_and_explicitly_unsafe(self):
        old = self.data["contracts"]["BugVault"]["supersedes"]
        self.assertEqual(old["address"], V1)
        self.assertIn("do not send value", old["status"].lower())

    def test_credpass_target_is_v2(self):
        gate = self.data["contracts"]["CredPass"]
        self.assertEqual(gate["address"], GATE)
        self.assertEqual(gate["expected_oracle"], V2)

    def test_no_pending_deployment_placeholder_remains(self):
        targets = [
            ROOT / "README.md",
            ROOT / "DEPLOYMENTS.md",
            ROOT / "deployments" / "studionet.json",
            ROOT / "samples" / "quickstart_cli.sh",
            ROOT / "tests" / "integration" / "test_live_smoke.py",
        ]
        for path in targets:
            with self.subTest(path=path.name):
                self.assertNotIn("PENDING_DEPLOYMENT", path.read_text(encoding="utf-8"))

    def test_all_runtime_defaults_use_v2_not_v1(self):
        for rel in ["samples/quickstart_cli.sh", "tests/integration/test_live_smoke.py"]:
            text = (ROOT / rel).read_text(encoding="utf-8")
            with self.subTest(path=rel):
                self.assertIn(V2, text)
                self.assertNotIn(V1, text)

    def test_payable_samples_require_distinct_roles(self):
        for rel in ["samples/deploy_and_fund.ts", "samples/dispute_flow.ts"]:
            text = (ROOT / rel).read_text(encoding="utf-8")
            with self.subTest(path=rel):
                self.assertIn("SPONSOR_PRIVATE_KEY", text)
                self.assertIn("RESEARCHER_PRIVATE_KEY", text)
                self.assertIn("sponsorAccount", text)
                self.assertIn("researcherAccount", text)


if __name__ == "__main__":
    unittest.main()