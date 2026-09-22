"""Integration fixtures — require a live GenLayer environment.

Prereqs:
    npm install -g genlayer
    genlayer init          # docker + GenVM, pick an LLM provider
    genlayer up            # local validator network on :4000

Run:  gltest tests/integration -v        (or: pytest tests/integration -v)

LLM-backed arbitration tests are gated behind RUN_LLM_TESTS=1 and a real,
publicly reachable REPORT_URL the simulator's validators can fetch.
"""
import os
import pytest

from gltest import get_contract_factory

HERE = os.path.dirname(os.path.abspath(__file__))
CONTRACTS_DIR = os.path.abspath(os.path.join(HERE, "..", "..", "contracts"))

FEE_BPS = 200


@pytest.fixture(scope="module")
def vault(setup_validators):
    """Deploy BugVault once per module against the local simulator's
    validators (the `setup_validators` fixture is provided by gltest)."""
    factory = get_contract_factory(os.path.join(CONTRACTS_DIR, "bug_vault.py"))
    return factory.deploy(args=[FEE_BPS])


@pytest.fixture(scope="module")
def gate(vault):
    """CredPass wired to the deployed vault oracle."""
    factory = get_contract_factory(os.path.join(CONTRACTS_DIR, "cred_pass.py"))
    return factory.deploy(args=[str(vault.address)])
