# Testing BugVault

Four runtime layers, from "runs anywhere in 2 seconds" to a live deployment signal, plus a
static deployment-manifest consistency suite.

## Layer 1 — offline white-box tests (no SDK, no chain, no network)

```bash
python3 -m pytest tests/offline -v
# or stdlib-only:
python3 -m unittest discover -s tests/offline -p "test_*.py" -v
```

**How it works.** `tests/offline/genlayer_stub.py` registers itself in `sys.modules` under the name `genlayer` on import, so `import bug_vault` executes the **real contract file** against an in-memory storage shim: `TreeMap`/`DynArray` with `get_or_insert_default` semantics, `gl.message` sender/value, `gl.transfer` (recorded for assertions), `gl.eq_principle` (leader decides), `gl.contract_interface` + a registry so CredPass's IC-to-IC reads hit a real deployed BugVault instance, plus `load_contract(path)` for file-based loading.

The nondeterministic seam is scripted, not stubbed away — `gl.nondet.web.render` / `gl.nondet.exec_prompt` are routed through `NONDET_HOOKS`, so the REAL `_judge` runs end to end in every arbitration test: web fetch, prompt building, JSON cleanup, coherence checks, and the transient/verdict error taxonomy:

```python
stub.NONDET_HOOKS["web_render"] = lambda url, mode, wait: "report content"
stub.NONDET_HOOKS["exec_prompt"] = lambda prompt: json.dumps(
    {"verdict": "VALID", "severity": "HIGH", "confidence": 90, "reasoning": "ok"})
bv._now_epoch = lambda: self.now   # time is the other controllable seam
```

What this layer proves (50+ cases): the pure helpers themselves (`_clean_json`, `_coherent`, float-confidence coercion), fee math and bounds, refund-on-reject for every payable branch, the claim cap **and** the one-live-claim-per-researcher anti-grief rule, approval paying exactly the net reward and voiding siblings, both dispute modes (challenge forfeits the bond; silent-sponsor never does), `INCONCLUSIVE` leaving state untouched and re-arbitrable, end-to-end `RETRY_LATER` on fetch outages **and** on garbage LLM output (real tagged raises), challenge-window protection on **both** refund paths, late-challenge bond refunds, TTL reclaim, sweep cooldown, owner gating, escrow-locked accounting, reputation/`earned`/credibility views, both paged feeds — and the entire CredPass allow/deny/preview/admin/oracle-swap surface against a live vault.

The three challenge-window regressions are intentionally named and easy to run alone:

```bash
python3 -m pytest tests/offline/test_bug_vault_unit.py \
  -k "cancel_blocked_while_rejection_challengeable or \
      reclaim_blocked_while_rejection_challengeable or \
      dispute_after_challenge_deadline_refunded" -v
```

## Layer 2 — structural money-safety test

```bash
python3 -m pytest tests/offline/test_payable_never_raises.py -v
```

Parses both contract sources with `ast` and fails if any `@gl.public.write.payable` method contains a `raise` anywhere in its body — plus a companion heuristic that every payable method has a refund path (`_send`), and a **whitelist** asserting the payable surface is exactly `{create_bounty, dispute_claim}` (and empty for CredPass). A new payable door can't sneak in through an unreviewed edit, and the load-bearing invariant (raising strands inbound GEN) is enforced mechanically rather than by review.

## Layer 3 — on-chain integration (real validators, real consensus)

```bash
npm install -g genlayer
genlayer init     # docker + GenVM; pick an LLM provider (llama3 needs no key)
genlayer up       # local network on :4000
gltest tests/integration -v
```

Deterministic flows (deploy, fund with `value=`, submit, approve, reject) run as-is. For genuine multi-validator LLM arbitration:

The deterministic chain suite also asserts the v2 deployment marker and proves that an immediate
`cancel_bounty` transaction fails after rejection while the claim remains challengeable.

```bash
RUN_LLM_TESTS=1 REPORT_URL="https://a.public/page-clearly-in-scope" gltest tests/integration -v
```

Pick a `REPORT_URL` any sane LLM would vote `VALID` for the sample scope (a detailed write-up of an auth bypass). The test accepts any of `VALID`/`INVALID`/`INCONCLUSIVE`/`RETRY_LATER` — the point is consensus machinery, not the model's opinion — and asserts the payout chain when `VALID`.

## Layer 4 — live smoke tests (against the real deployment)

Read-only sanity checks against the pair already deployed on Studionet — no GEN, no writes, no LLM. They call views through the genlayer CLI and assert the on-chain config matches this repo's constants, the oracle wiring points at the right vault, and the feeds answer:

```bash
genlayer network set studionet
LIVE_SMOKE=1 python3 -m pytest tests/integration/test_live_smoke.py -v
```

Addresses default to the recorded deployment (see `DEPLOYMENTS.md`) and can be overridden with `LIVE_VAULT_ADDRESS` / `LIVE_GATE_ADDRESS` after a redeploy. Run this after any deployment change — it is the cheapest way to catch a miswired oracle or a wrong constructor argument.

Current defaults:

```text
BugVault v2  0xD57A790cDc4A6a456A2C064D80153B0cb679d724
CredPass     0x2c9d68A00f110DCB2f9CBb41b77A748c080cBF39
```

The suite checks `challenge_window_seconds == 259200`, proving the address runs the corrected
build. It also checks `CredPass.get_stats().oracle == BugVault v2`. If only that wiring test
fails, run the owner-only `set_oracle` command in `DEPLOYMENTS.md`, then rerun the suite.

## Deployment-manifest consistency (offline)

```bash
python3 -m pytest tests/offline/test_deployment_manifest.py -v
```

This layer is deliberately network-free. It asserts that v2 is current, v1 is historical and
unsafe, CredPass's expected oracle is v2, runtime defaults never point at v1, and no
`PENDING_DEPLOYMENT` placeholder remains in release-facing files.

## Linting against the real GenVM SDK

```bash
pip install -r requirements.txt
genvm-lint check contracts/bug_vault.py --json
genvm-lint check contracts/cred_pass.py --json
```

AST safety checks + semantic validation (method counts, view/write classification).

## Writing new tests

* Extend `tests/offline` first — it's the fastest loop and executes real code. Every new payable branch must appear here **with its refund assertion**, and the AST whitelist must be updated if you add a payable entry point deliberately.
* If you add storage fields, the stub needs no changes (annotations drive construction); if you add new nondeterminism, route it through `gl.nondet.*` so `NONDET_HOOKS` can script it rather than inventing a new seam.
* Add integration cases only for behaviour whose correctness depends on consensus / real networking — not for things the stub already settles deterministically.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| offline tests can't import `genlayer` | import `genlayer_stub` (or run via the provided files) before importing contracts |
| `gltest` can't find validators | `genlayer up` not running; the `setup_validators` fixture handles setup per run |
| LLM arbitration returns `RETRY_LATER` | validators can't reach `REPORT_URL` (must be public), or the provider key is missing |
| payable write can't attach value via CLI | CLI limitation by design — use the genlayer-js samples |
| sample fails with "sponsor cannot claim their own bounty" | sponsor and researcher keys are identical; the samples require two accounts |
| v2 vault smoke checks pass but gate wiring fails | CredPass still points at v1; call owner-only `set_oracle(v2)` and rerun |
| `challenge_window_seconds` missing | the address is not the corrected v2 build; do not use it for new escrow |
