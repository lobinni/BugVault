"""Structural money-safety test, enforced mechanically rather than by review.

Rule: no method decorated `@gl.public.write.payable` may contain a `raise`
ANYWHERE in its body. On GenVM, raising rolls back contract storage but does
NOT return the value that rode in with the call — a payable method that
raises on rejection would strand the sender's GEN inside the contract,
unaccounted for. Rejections must refund via gl.transfer and return
{"ok": false, ...} instead.

Pure stdlib: parses both contract sources with `ast`, runs anywhere:
    python3 -m pytest tests/offline/test_payable_never_raises.py -v
"""
import ast
import os
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))
CONTRACTS_DIR = os.path.abspath(os.path.join(BASE, "..", "..", "contracts"))
CONTRACT_FILES = ["bug_vault.py", "cred_pass.py"]


def _decorator_name(deco) -> str:
    """Render a decorator expression as a dotted string, best effort."""
    parts = []
    node = deco
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _payable_methods(tree) -> list:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for deco in node.decorator_list:
                if _decorator_name(deco) == "gl.public.write.payable":
                    out.append(node)
    return out


class PayableNeverRaises(unittest.TestCase):
    def test_no_raise_inside_payable_methods(self):
        found_any = False
        offenders = []
        for fname in CONTRACT_FILES:
            path = os.path.join(CONTRACTS_DIR, fname)
            with open(path, "r", encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=fname)
            methods = _payable_methods(tree)
            found_any = found_any or len(methods) > 0
            for m in methods:
                for sub in ast.walk(m):
                    if isinstance(sub, ast.Raise):
                        offenders.append(
                            fname + ":" + m.name + " line " + str(sub.lineno))
        self.assertTrue(found_any, "no payable methods found — is the contract layout intact?")
        self.assertEqual(offenders, [], "raise found inside a payable method: " + "; ".join(offenders))

    def test_payable_entry_points_are_exactly_the_expected_set(self):
        """Whitelist: a NEW payable method appearing by accident (funds
        entering through an un-audited door) fails this test loudly."""
        for fname, expected in [
            ("bug_vault.py", {"create_bounty", "dispute_claim"}),
            ("cred_pass.py", set()),
        ]:
            path = os.path.join(CONTRACTS_DIR, fname)
            with open(path, "r", encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=fname)
            names = {m.name for m in _payable_methods(tree)}
            self.assertEqual(names, expected, fname + " payable surface changed: " + str(names))

    def test_every_payable_rejection_refunds(self):
        """Companion heuristic: every payable method must contain at least
        one gl.transfer( ... sender ... ) call — i.e. a refund path exists."""
        for fname in CONTRACT_FILES:
            path = os.path.join(CONTRACTS_DIR, fname)
            with open(path, "r", encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=fname)
            for m in _payable_methods(tree):
                src = ast.dump(m)
                self.assertIn("_send", src,
                              fname + ":" + m.name + " has no refund path (_send)")


if __name__ == "__main__":
    unittest.main()
